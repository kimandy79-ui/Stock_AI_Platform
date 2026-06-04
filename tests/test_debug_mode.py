"""Tests for Module 22 — Debug Mode (post-fix).

All tests run fully offline: the pipeline orchestrator, DuckDB manager, and
market-data provider are replaced with in-process fakes.  No real DuckDB,
Polars, network, or orchestrator import ever occurs.

Coverage:
- SamplingProvider (limit/sort/determinism, TickerInfo-like objects, dict
  entries, watchlist, failed pass-through, delegation).
- _NoOpStepEngine (any method → success, rows_processed=0).
- Guards (forbidden/invalid role, sample cap, sample < 1, inverted range,
  empty/unknown strategies, unknown preset, oversized watchlist).
- Force-rerun forwarding (True by default; False explicit).
- Ticker-scope mechanism: partial runs with start > universe_ingestion lower
  effective_start to universe_ingestion; bridge steps become no-ops.
- Engine injection correctness for each preset shape.
- DB-role / run-type isolation (never forbidden role forwarded).
- Result metadata (executed_steps, noop_steps, effective_start_step,
  force_rerun, watchlist de-dup recording).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.utils import service_result
from app.utils.service_result import ServiceResult
from app.services.debug import debug_mode as dm
from app.services.debug.debug_mode import (
    DB_ROLE_DEBUG,
    DEBUG_PRESETS,
    MAX_DEBUG_SAMPLE,
    RUN_TYPE_DEBUG,
    STEP_NAMES,
    DebugModeController,
    DebugRunPlan,
    SamplingProvider,
    _NoOpStepEngine,
)

RUN_DATE = date(2025, 6, 2)

FAKE_CONFIGS = {
    "normal":       {"strategy_name": "normal"},
    "aggressive":   {"strategy_name": "aggressive"},
    "conservative": {"strategy_name": "conservative"},
}

# Convenience index lookups.
_IDX_UNIVERSE  = STEP_NAMES.index("universe_ingestion")
_IDX_PRICE     = STEP_NAMES.index("price_ingestion")
_IDX_VALID     = STEP_NAMES.index("validation")
_IDX_MUTATION  = STEP_NAMES.index("mutation_detection")
_IDX_FEATURE   = STEP_NAMES.index("feature_calculation")
_IDX_SCREEN    = STEP_NAMES.index("step3_screening")
_IDX_ANALYSIS  = STEP_NAMES.index("step4_analysis")
_IDX_PROPOSALS = STEP_NAMES.index("step5_proposals")


# --------------------------------------------------------------------------- #
# Fakes.
# --------------------------------------------------------------------------- #
class _TickerInfoLike:
    """Minimal TickerInfo-like object (has a .ticker attribute)."""

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    def __repr__(self) -> str:
        return f"TI({self.ticker!r})"


class FakeProvider:
    def __init__(self, symbols, *, result=None):
        self._symbols = symbols
        self._result = result
        self.calls: list[tuple] = []

    def list_symbols(self, symbol_type=None):
        self.calls.append(("list_symbols", symbol_type))
        if self._result is not None:
            return self._result
        return ServiceResult(
            service_result.STATUS_SUCCESS,
            "prov",
            metadata={"symbols": list(self._symbols)},
        )

    def get_capabilities(self):
        self.calls.append(("get_capabilities", None))
        return ServiceResult(service_result.STATUS_SUCCESS, "cap")

    def get_price_history(self, request):
        self.calls.append(("get_price_history", request))
        return ServiceResult(service_result.STATUS_SUCCESS, "px")

    def get_earnings(self, ticker):
        self.calls.append(("get_earnings", ticker))
        return ServiceResult(service_result.STATUS_SUCCESS, "earn")


class FakeOrchestrator:
    instances: list["FakeOrchestrator"] = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.run_kwargs: dict | None = None
        FakeOrchestrator.instances.append(self)

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=str(kwargs.get("run_id") or "orch"),
            rows_processed=3,
            metadata={
                "run_id": str(kwargs.get("run_id") or "orch"),
                "db_role": kwargs.get("db_role"),
                "run_type": kwargs.get("run_type"),
                "steps_completed": ["benchmark_etf_ingestion"],
            },
        )


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeOrchestrator.instances = []
    yield
    FakeOrchestrator.instances = []


def make_controller(provider=None, *, configs=FAKE_CONFIGS):
    return DebugModeController(
        db_manager=object(),
        provider=provider if provider is not None else FakeProvider(["AAA"]),
        orchestrator_factory=FakeOrchestrator,
        strategy_configs=configs,
    )


def _plan(**overrides):
    base = dict(
        run_date=RUN_DATE,
        sample_count=20,
        start_step=STEP_NAMES[0],
        end_step=STEP_NAMES[-1],
        strategy_names=("normal",),
    )
    base.update(overrides)
    return DebugRunPlan(**base)


# --------------------------------------------------------------------------- #
# SamplingProvider.
# --------------------------------------------------------------------------- #
def test_sampling_provider_limits_and_sorts_symbols():
    base = FakeProvider(["MSFT", "AAPL", "TSLA", "NVDA"])
    wrapped = SamplingProvider(base, sample_count=2)
    result = wrapped.list_symbols(symbol_type="stock")
    assert result.metadata["symbols"] == ["AAPL", "MSFT"]
    assert result.metadata["debug_sampled"] is True
    assert result.metadata["debug_sample_count"] == 2
    assert result.rows_processed == 2


def test_sampling_provider_is_deterministic():
    base = FakeProvider(["d", "a", "c", "b"])
    first  = SamplingProvider(base, sample_count=3).list_symbols()
    second = SamplingProvider(FakeProvider(["d", "a", "c", "b"]), sample_count=3)
    assert first.metadata["symbols"] == second.list_symbols().metadata["symbols"]
    assert first.metadata["symbols"] == ["a", "b", "c"]


def test_sampling_provider_handles_dict_entries():
    base = FakeProvider([{"symbol": "ZZZ"}, {"symbol": "AAA"}])
    wrapped = SamplingProvider(base, sample_count=1)
    assert wrapped.list_symbols().metadata["symbols"] == [{"symbol": "AAA"}]


def test_sampling_provider_handles_ticker_info_like_objects():
    entries = [_TickerInfoLike("MSFT"), _TickerInfoLike("AAPL"), _TickerInfoLike("TSLA")]
    base = FakeProvider(entries)
    wrapped = SamplingProvider(base, sample_count=2)
    result = wrapped.list_symbols()
    keys = [SamplingProvider._symbol_key(e) for e in result.metadata["symbols"]]
    assert keys == ["AAPL", "MSFT"]  # stable sort, head-2


def test_sampling_provider_watchlist_filters_exactly():
    base = FakeProvider(["AAPL", "MSFT", "TSLA"])
    wrapped = SamplingProvider(base, watchlist=["TSLA", "AAPL"])
    assert wrapped.list_symbols().metadata["symbols"] == ["AAPL", "TSLA"]


def test_sampling_provider_passes_through_failed():
    failed = ServiceResult(service_result.STATUS_FAILED, "x", errors=["boom"])
    base = FakeProvider([], result=failed)
    out = SamplingProvider(base, sample_count=5).list_symbols()
    assert out is failed


def test_sampling_provider_delegates_other_methods():
    base = FakeProvider(["AAA"])
    wrapped = SamplingProvider(base, sample_count=1)
    wrapped.get_capabilities()
    wrapped.get_price_history({"req": 1})
    wrapped.get_earnings("AAA")
    names = [c[0] for c in base.calls]
    assert names == ["get_capabilities", "get_price_history", "get_earnings"]


# --------------------------------------------------------------------------- #
# _NoOpStepEngine.
# --------------------------------------------------------------------------- #
def test_noop_engine_returns_success_zero_rows_for_any_method():
    engine = _NoOpStepEngine("step5_proposals")
    for method in ("screen", "analyze", "propose", "ingest", "load"):
        result = getattr(engine, method)(run_id="r", signal_date=RUN_DATE)
        assert result.status == service_result.STATUS_SUCCESS
        assert result.rows_processed == 0
        assert result.metadata["debug_noop_step"] == "step5_proposals"


# --------------------------------------------------------------------------- #
# Guards — no orchestrator must be built on failure.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["prod", "simulation"])
def test_forbidden_db_role_rejected(role):
    controller = make_controller()
    result = controller.run(_plan(db_role=role))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []
    assert any("must not target db_role" in e for e in result.errors)


def test_non_debug_role_rejected():
    controller = make_controller()
    result = controller.run(_plan(db_role="other"))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_sample_count_over_cap_rejected():
    controller = make_controller()
    result = controller.run(_plan(sample_count=MAX_DEBUG_SAMPLE + 1))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []
    assert any("exceeds the debug cap" in e for e in result.errors)


def test_sample_count_below_one_rejected():
    controller = make_controller()
    result = controller.run(_plan(sample_count=0))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_invalid_step_range_rejected():
    controller = make_controller()
    result = controller.run(
        _plan(start_step="step5_proposals", end_step="feature_calculation")
    )
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_empty_strategy_names_rejected():
    controller = make_controller()
    result = controller.run(_plan(strategy_names=()))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_unknown_preset_rejected():
    controller = make_controller()
    result = controller.run_preset("does_not_exist", RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_unknown_strategy_name_rejected():
    controller = make_controller()
    result = controller.run(_plan(strategy_names=("ghost",)))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


# New: oversized watchlist rejected before orchestrator construction.
def test_oversized_watchlist_rejected_before_orchestrator():
    controller = make_controller()
    big = tuple(f"T{i:04d}" for i in range(MAX_DEBUG_SAMPLE + 1))  # 501 unique
    result = controller.run(_plan(watchlist=big, sample_count=20))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []
    assert any("watchlist" in e for e in result.errors)
    assert any("de-duplication" in e for e in result.errors)


def test_oversized_watchlist_respects_dedup():
    """A watchlist with duplicates that de-dups to ≤ MAX is accepted."""
    controller = make_controller(
        provider=FakeProvider([f"T{i:04d}" for i in range(MAX_DEBUG_SAMPLE)])
    )
    # MAX_DEBUG_SAMPLE + 50 entries but only MAX_DEBUG_SAMPLE unique.
    dup_watchlist = tuple(f"T{i:04d}" for i in range(MAX_DEBUG_SAMPLE)) + \
                    tuple(f"T{i:04d}" for i in range(50))
    result = controller.run(_plan(watchlist=dup_watchlist, sample_count=20))
    assert result.status == service_result.STATUS_SUCCESS
    assert FakeOrchestrator.instances  # orchestrator was built


# --------------------------------------------------------------------------- #
# Force-rerun forwarding.
# --------------------------------------------------------------------------- #
def test_force_rerun_true_forwarded_by_default():
    controller = make_controller()
    controller.run(_plan())
    assert FakeOrchestrator.instances[-1].run_kwargs["force_rerun"] is True


def test_force_rerun_false_can_be_set_explicitly():
    controller = make_controller()
    controller.run(_plan(force_rerun=False))
    assert FakeOrchestrator.instances[-1].run_kwargs["force_rerun"] is False


def test_run_preset_force_rerun_true_by_default():
    controller = make_controller(provider=FakeProvider(["A"]))
    controller.run_preset("fast_smoke_test", RUN_DATE)
    assert FakeOrchestrator.instances[-1].run_kwargs["force_rerun"] is True


def test_run_preset_force_rerun_override():
    controller = make_controller(provider=FakeProvider(["A"]))
    controller.run_preset("fast_smoke_test", RUN_DATE, force_rerun=False)
    assert FakeOrchestrator.instances[-1].run_kwargs["force_rerun"] is False


# --------------------------------------------------------------------------- #
# DB-role / run-type isolation.
# --------------------------------------------------------------------------- #
def test_run_forces_debug_role_and_type():
    controller = make_controller()
    controller.run(_plan())
    rk = FakeOrchestrator.instances[-1].run_kwargs
    assert rk["db_role"] == DB_ROLE_DEBUG
    assert rk["run_type"] == RUN_TYPE_DEBUG


def test_run_never_uses_forbidden_role():
    controller = make_controller()
    controller.run(_plan())
    assert FakeOrchestrator.instances[-1].run_kwargs["db_role"] not in dm.FORBIDDEN_DB_ROLES


# --------------------------------------------------------------------------- #
# Ticker-scope mechanism (start > universe_ingestion).
# --------------------------------------------------------------------------- #
def test_indicator_validation_lowers_effective_start_to_universe():
    """indicator_validation declared start=feature; effective start=universe.

    The orchestrator must receive resume_from='universe_ingestion' so the
    universe snapshot in debug.duckdb is updated to the sampled ticker set
    before feature_calculation runs (the real ticker-scope mechanism).
    """
    prov = FakeProvider(["AAPL", "MSFT", "TSLA", "NVDA", "GOOG",
                          "AMZN", "META", "NFLX", "ORCL", "IBM", "HPQ"])
    controller = make_controller(provider=prov)
    controller.run_preset("indicator_validation", RUN_DATE)

    orch = FakeOrchestrator.instances[-1]
    # Effective start is universe_ingestion.
    assert orch.run_kwargs["resume_from"] == "universe_ingestion"
    # Universe engine is real (None → orchestrator builds it).
    assert orch.init_kwargs["universe_engine"] is None
    # Provider is a SamplingProvider capped at 10 (preset sample_count).
    provider = orch.init_kwargs["provider"]
    assert isinstance(provider, SamplingProvider)
    sampled = provider.list_symbols().metadata["symbols"]
    assert len(sampled) == 10  # preset cap
    assert sampled == sorted(sampled)  # stable deterministic order
    # Bridge steps between universe (1) and feature (5) are no-ops.
    assert isinstance(orch.init_kwargs["ingestion_engine"], _NoOpStepEngine)
    assert isinstance(orch.init_kwargs["validation_engine"], _NoOpStepEngine)
    assert isinstance(orch.init_kwargs["mutation_engine"], _NoOpStepEngine)
    # Feature engine is real.
    assert orch.init_kwargs["feature_engine"] is None
    # Steps after feature (end_step) are no-ops.
    assert isinstance(orch.init_kwargs["screening_engine"], _NoOpStepEngine)
    assert isinstance(orch.init_kwargs["analysis_engine"], _NoOpStepEngine)


def test_config_tuning_lowers_effective_start_to_universe():
    """config_tuning_test declared start=step3; effective start=universe.

    Steps between universe (1) and step3 (6) — price, validation, mutation,
    feature — are bridge no-ops so the debug run does not re-ingest data but
    still scopes the universe snapshot to the sampled tickers.
    """
    prov = FakeProvider([f"T{i:03d}" for i in range(600)])
    controller = make_controller(provider=prov)
    controller.run_preset("config_tuning_test", RUN_DATE)

    orch = FakeOrchestrator.instances[-1]
    assert orch.run_kwargs["resume_from"] == "universe_ingestion"

    # SamplingProvider must cap at 500.
    provider = orch.init_kwargs["provider"]
    assert isinstance(provider, SamplingProvider)
    assert len(provider.list_symbols().metadata["symbols"]) == 500

    # Bridge no-ops: price, validation, mutation, feature.
    for kwarg in ("ingestion_engine", "validation_engine",
                  "mutation_engine", "feature_engine"):
        assert isinstance(orch.init_kwargs[kwarg], _NoOpStepEngine), kwarg

    # In-range real engines: step3, step4, step5.
    for kwarg in ("screening_engine", "analysis_engine", "proposal_engine"):
        assert orch.init_kwargs[kwarg] is None, kwarg

    # Out-of-range after end_step=step5_proposals: outcome_creator/processor.
    assert isinstance(orch.init_kwargs["outcome_creator"], _NoOpStepEngine)
    assert isinstance(orch.init_kwargs["outcome_processor"], _NoOpStepEngine)


def test_full_start_at_benchmark_has_no_bridge_noops():
    """When start=benchmark_etf_ingestion (idx 0) ≤ universe (idx 1), no bridge."""
    controller = make_controller()
    controller.run(_plan(start_step=STEP_NAMES[0], end_step=STEP_NAMES[-1]))
    orch = FakeOrchestrator.instances[-1]
    assert orch.run_kwargs["resume_from"] is None
    # No engine should be a _NoOpStepEngine (full run, nothing no-op'd).
    for k, v in orch.init_kwargs.items():
        if k not in ("db_manager", "provider"):
            assert v is None, f"{k} should be None for a full run"


# --------------------------------------------------------------------------- #
# Engine injection / resume_from correctness.
# --------------------------------------------------------------------------- #
def test_provider_is_wrapped_in_sampling_provider():
    base = FakeProvider(["AAPL", "MSFT", "TSLA"])
    controller = make_controller(provider=base)
    controller.run(_plan(sample_count=2))
    provider = FakeOrchestrator.instances[-1].init_kwargs["provider"]
    assert isinstance(provider, SamplingProvider)
    assert provider.list_symbols().metadata["symbols"] == ["AAPL", "MSFT"]


def test_resume_from_none_when_starting_at_first_step():
    controller = make_controller()
    controller.run(_plan(start_step=STEP_NAMES[0]))
    assert FakeOrchestrator.instances[-1].run_kwargs["resume_from"] is None


def test_resume_from_is_universe_when_start_is_after_universe():
    """Any declared start after universe lowers effective start to universe."""
    for start in ("price_ingestion", "validation", "feature_calculation",
                  "step3_screening", "step5_proposals"):
        FakeOrchestrator.instances = []
        controller = make_controller()
        end = "step5_proposals" if STEP_NAMES.index(start) <= STEP_NAMES.index("step5_proposals") else start
        controller.run(_plan(start_step=start, end_step=end))
        rk = FakeOrchestrator.instances[-1].run_kwargs
        assert rk["resume_from"] == "universe_ingestion", \
            f"start={start}: expected universe_ingestion, got {rk['resume_from']}"


def test_partial_run_end_step_noops_engines_after_end():
    controller = make_controller()
    controller.run(_plan(start_step=STEP_NAMES[0], end_step="feature_calculation"))
    init = FakeOrchestrator.instances[-1].init_kwargs
    assert init["feature_engine"] is None
    assert init["benchmark_loader"] is None
    assert isinstance(init["screening_engine"], _NoOpStepEngine)
    assert isinstance(init["analysis_engine"], _NoOpStepEngine)
    assert isinstance(init["proposal_engine"], _NoOpStepEngine)
    assert isinstance(init["outcome_creator"], _NoOpStepEngine)
    assert isinstance(init["outcome_processor"], _NoOpStepEngine)


def test_only_requested_strategy_configs_forwarded():
    controller = make_controller()
    controller.run(_plan(strategy_names=("normal", "aggressive")))
    configs = FakeOrchestrator.instances[-1].run_kwargs["strategy_configs"]
    assert set(configs) == {"normal", "aggressive"}


# --------------------------------------------------------------------------- #
# Presets.
# --------------------------------------------------------------------------- #
def test_run_preset_uses_preset_defaults():
    controller = make_controller(provider=FakeProvider(["A", "B", "C"]))
    result = controller.run_preset("fast_smoke_test", RUN_DATE)
    assert result.status == service_result.STATUS_SUCCESS
    dbg = result.metadata["debug"]
    assert dbg["preset"] == "fast_smoke_test"
    assert dbg["sample_count"] == DEBUG_PRESETS["fast_smoke_test"].sample_count
    assert dbg["start_step"] == STEP_NAMES[0]
    assert dbg["end_step"] == STEP_NAMES[-1]


def test_run_preset_overrides_take_precedence():
    controller = make_controller(provider=FakeProvider(["A", "B", "C", "D"]))
    controller.run_preset(
        "fast_smoke_test", RUN_DATE,
        sample_count=3, strategy_names=["aggressive"],
    )
    init = FakeOrchestrator.instances[-1].init_kwargs
    sampled = init["provider"].list_symbols().metadata["symbols"]
    assert sampled == ["A", "B", "C"]
    assert set(FakeOrchestrator.instances[-1].run_kwargs["strategy_configs"]) == {"aggressive"}


@pytest.mark.parametrize("name", sorted(DEBUG_PRESETS))
def test_all_presets_are_valid_and_runnable(name):
    controller = make_controller(provider=FakeProvider([f"T{i}" for i in range(600)]))
    result = controller.run_preset(name, RUN_DATE)
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["debug"]["db_role"] == DB_ROLE_DEBUG


def test_indicator_validation_preset_shape():
    preset = DEBUG_PRESETS["indicator_validation"]
    assert preset.start_step == "feature_calculation"
    assert preset.end_step == "feature_calculation"
    assert preset.sample_count == 10


def test_run_preset_watchlist_deduplication():
    """run_preset de-dups the watchlist before building the plan."""
    prov = FakeProvider(["AAPL", "MSFT", "TSLA"])
    controller = make_controller(provider=prov)
    controller.run_preset(
        "fast_smoke_test", RUN_DATE,
        watchlist=["AAPL", "MSFT", "AAPL"],  # AAPL duplicated
    )
    provider = FakeOrchestrator.instances[-1].init_kwargs["provider"]
    result = provider.list_symbols()
    # Watchlist used; only AAPL+MSFT from the symbol pool.
    assert result.metadata["symbols"] == ["AAPL", "MSFT"]


# --------------------------------------------------------------------------- #
# Result metadata.
# --------------------------------------------------------------------------- #
def test_result_metadata_records_executed_and_noop_steps():
    controller = make_controller()
    result = controller.run(
        _plan(start_step=STEP_NAMES[0], end_step="feature_calculation")
    )
    dbg = result.metadata["debug"]
    assert "feature_calculation" in dbg["executed_steps"]
    assert "step3_screening" in dbg["noop_steps"]
    assert "step3_screening" not in dbg["executed_steps"]
    assert result.metadata["db_role"] == DB_ROLE_DEBUG


def test_metadata_records_effective_start_step():
    controller = make_controller()
    result = controller.run(
        _plan(start_step="feature_calculation", end_step="feature_calculation")
    )
    dbg = result.metadata["debug"]
    assert dbg["start_step"] == "feature_calculation"
    assert dbg["effective_start_step"] == "universe_ingestion"


def test_metadata_records_force_rerun():
    controller = make_controller()
    r1 = controller.run(_plan())
    assert r1.metadata["debug"]["force_rerun"] is True
    r2 = controller.run(_plan(force_rerun=False))
    assert r2.metadata["debug"]["force_rerun"] is False


def test_watchlist_recorded_sample_count_none():
    controller = make_controller(provider=FakeProvider(["AAPL", "MSFT"]))
    result = controller.run(_plan(watchlist=("AAPL",), sample_count=20))
    dbg = result.metadata["debug"]
    assert dbg["watchlist"] == ["AAPL"]
    assert dbg["sample_count"] is None
    provider = FakeOrchestrator.instances[-1].init_kwargs["provider"]
    assert provider.list_symbols().metadata["symbols"] == ["AAPL"]


def test_noop_steps_in_metadata_for_partial_run():
    controller = make_controller()
    result = controller.run(
        _plan(start_step="feature_calculation", end_step="feature_calculation")
    )
    dbg = result.metadata["debug"]
    # Bridge: price, validation, mutation.
    for step in ("price_ingestion", "validation", "mutation_detection"):
        assert step in dbg["noop_steps"], step
    # After end: step3+.
    for step in ("step3_screening", "step4_analysis"):
        assert step in dbg["noop_steps"], step
    # Executed: universe (forced) + feature.
    assert "universe_ingestion" in dbg["executed_steps"]
    assert "feature_calculation" in dbg["executed_steps"]
