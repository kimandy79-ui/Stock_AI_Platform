"""Tests for Module 22 — Debug Mode.

All tests run fully offline: orchestrator, DuckDB manager, FeatureEngine,
Step3ScreeningEngine, and the market-data provider are replaced with in-process
fakes injected via the controller's factory parameters.  No real DuckDB, Polars,
network, or orchestrator import ever occurs.

Key design decision captured here
-----------------------------------
``_ScopedStep3ScreeningProxy.screen()`` calls the real engine with the
**standard Step3ScreeningEngine.screen() signature** — no ``tickers`` kwarg is
added to the public API.  Ticker filtering is applied inside the real engine at
the private ``_read()`` level via a dynamic subclass created by the default
factory.  Offline tests verify:

  * The right engine type is injected for each preset.
  * ``selected_tickers`` on the proxy/engine match the sampled ticker list.
  * ``proxy.screen()`` delegates to the real engine with the standard signature.

The actual Polars filter in ``_read()`` is tested only with a live duckdb /
polars environment (integration tests) — it is out of scope for this offline
suite.
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
    _ScopedFeatureEngine,
    _ScopedStep3ScreeningProxy,
    _needs_feature_scope,
    _needs_step3_scope,
)

RUN_DATE = date(2025, 6, 2)
FAKE_CONFIGS = {
    "normal":       {"strategy_name": "normal"},
    "aggressive":   {"strategy_name": "aggressive"},
    "conservative": {"strategy_name": "conservative"},
}


# --------------------------------------------------------------------------- #
# Fakes.
# --------------------------------------------------------------------------- #
class _TickerInfoLike:
    """Object with a .ticker attribute (stands in for TickerInfo)."""

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker

    def __repr__(self) -> str:
        return f"TI({self.ticker!r})"


class FakeProvider:
    def __init__(self, symbols, *, result=None):
        self._symbols = list(symbols)
        self._result = result
        self.calls: list[tuple] = []

    def list_symbols(self, symbol_type=None):
        self.calls.append(("list_symbols", symbol_type))
        if self._result is not None:
            return self._result
        return ServiceResult(
            service_result.STATUS_SUCCESS, "prov",
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
                "steps_completed": [],
            },
        )


# Testable scoped-engine stubs returned by the injected fake factories.
# These replace FeatureEngine / Step3ScreeningEngine so no duckdb/polars is
# needed offline.
class FakeScopedFeatureEngine:
    """Records selected_tickers; mimics _ScopedFeatureEngine interface."""

    def __init__(self, tickers: list[str]) -> None:
        self.selected_tickers = list(tickers)

    def calculate(self, start_date, end_date, tickers=None, db_role="prod", run_id=None):
        return ServiceResult(
            service_result.STATUS_SUCCESS, run_id or "feat",
            rows_processed=len(self.selected_tickers),
        )


class FakeScopedStep3Engine:
    """Records selected_tickers; mimics _ScopedStep3ScreeningProxy interface."""

    def __init__(self, tickers: list[str]) -> None:
        self.selected_tickers = list(tickers)

    def screen(self, signal_date, strategy_config, strategy_config_id, db_role, run_id=None):
        return ServiceResult(service_result.STATUS_SUCCESS, run_id or "s3")


@pytest.fixture(autouse=True)
def _reset():
    FakeOrchestrator.instances = []
    yield
    FakeOrchestrator.instances = []


def make_controller(provider=None, *, configs=FAKE_CONFIGS):
    return DebugModeController(
        db_manager=object(),
        provider=provider if provider is not None else FakeProvider(["AAA"]),
        orchestrator_factory=FakeOrchestrator,
        strategy_configs=configs,
        scoped_feature_engine_factory=lambda db, tickers: FakeScopedFeatureEngine(tickers),
        scoped_screening_engine_factory=lambda db, tickers: FakeScopedStep3Engine(tickers),
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
def test_sampling_limits_and_sorts():
    out = SamplingProvider(FakeProvider(["MSFT", "AAPL", "TSLA", "NVDA"]), sample_count=2).list_symbols()
    assert out.metadata["symbols"] == ["AAPL", "MSFT"]
    assert out.metadata["debug_sample_count"] == 2
    assert out.metadata["debug_sampled"] is True


def test_sampling_is_deterministic():
    syms = ["d", "a", "c", "b"]
    r1 = SamplingProvider(FakeProvider(syms), sample_count=3).list_symbols()
    r2 = SamplingProvider(FakeProvider(syms), sample_count=3).list_symbols()
    assert r1.metadata["symbols"] == r2.metadata["symbols"] == ["a", "b", "c"]


def test_sampling_handles_dict_entries():
    base = FakeProvider([{"symbol": "ZZZ"}, {"symbol": "AAA"}])
    assert SamplingProvider(base, sample_count=1).list_symbols().metadata["symbols"] == [{"symbol": "AAA"}]


def test_sampling_handles_ticker_info_objects():
    entries = [_TickerInfoLike("MSFT"), _TickerInfoLike("AAPL"), _TickerInfoLike("TSLA")]
    out = SamplingProvider(FakeProvider(entries), sample_count=2).list_symbols()
    keys = [SamplingProvider._symbol_key(e) for e in out.metadata["symbols"]]
    assert keys == ["AAPL", "MSFT"]


def test_sampling_watchlist_filter():
    out = SamplingProvider(FakeProvider(["AAPL", "MSFT", "TSLA"]), watchlist=["TSLA", "AAPL"]).list_symbols()
    assert out.metadata["symbols"] == ["AAPL", "TSLA"]


def test_sampling_passes_through_failed():
    failed = ServiceResult(service_result.STATUS_FAILED, "x", errors=["boom"])
    out = SamplingProvider(FakeProvider([], result=failed), sample_count=5).list_symbols()
    assert out is failed


def test_sampling_delegates_other_methods():
    base = FakeProvider(["A"])
    wrapped = SamplingProvider(base, sample_count=1)
    wrapped.get_capabilities()
    wrapped.get_price_history({"r": 1})
    wrapped.get_earnings("A")
    assert [c[0] for c in base.calls] == ["get_capabilities", "get_price_history", "get_earnings"]


# --------------------------------------------------------------------------- #
# _NoOpStepEngine.
# --------------------------------------------------------------------------- #
def test_noop_any_method_returns_success_zero_rows():
    e = _NoOpStepEngine("step5_proposals")
    for m in ("screen", "analyze", "propose", "ingest", "load"):
        r = getattr(e, m)(run_id="r", signal_date=RUN_DATE)
        assert r.status == service_result.STATUS_SUCCESS
        assert r.rows_processed == 0
        assert r.metadata["debug_noop_step"] == "step5_proposals"


# --------------------------------------------------------------------------- #
# _ScopedFeatureEngine — unit tests.
# --------------------------------------------------------------------------- #
class _FakeRealFeatureEngine:
    def __init__(self):
        self.calls: list[dict] = []

    def calculate(self, start_date, end_date, tickers=None, db_role="prod", run_id=None):
        self.calls.append({"tickers": tickers, "db_role": db_role})
        return ServiceResult(service_result.STATUS_SUCCESS, "fe")


def test_scoped_feature_engine_always_passes_selected_tickers():
    real = _FakeRealFeatureEngine()
    eng = _ScopedFeatureEngine(real, ["AAPL", "MSFT"])
    eng.calculate(start_date=RUN_DATE, end_date=RUN_DATE, db_role="debug", run_id="t")
    assert real.calls[-1]["tickers"] == ["AAPL", "MSFT"]
    assert real.calls[-1]["db_role"] == "debug"


def test_scoped_feature_engine_overrides_caller_none():
    real = _FakeRealFeatureEngine()
    eng = _ScopedFeatureEngine(real, ["AAPL"])
    eng.calculate(start_date=RUN_DATE, end_date=RUN_DATE, tickers=None, db_role="debug")
    assert real.calls[-1]["tickers"] == ["AAPL"]   # never None


def test_scoped_feature_engine_exposes_selected_tickers():
    eng = _ScopedFeatureEngine(object(), ["TSLA", "AAPL"])
    assert eng.selected_tickers == ["TSLA", "AAPL"]


# --------------------------------------------------------------------------- #
# _ScopedStep3ScreeningProxy — unit tests.
# Verify the proxy delegates to the real engine with the STANDARD Step3
# signature (no tickers kwarg added to the public API).
# --------------------------------------------------------------------------- #
class _FakeRealStep3Engine:
    """Mimics Step3ScreeningEngine.screen() with its exact public signature."""

    def __init__(self):
        self.calls: list[dict] = []

    def screen(
        self,
        signal_date,
        strategy_config,
        strategy_config_id,
        db_role,
        run_id=None,
    ):
        self.calls.append({
            "signal_date": signal_date,
            "strategy_config": strategy_config,
            "strategy_config_id": strategy_config_id,
            "db_role": db_role,
            "run_id": run_id,
        })
        return ServiceResult(service_result.STATUS_SUCCESS, "s3")


def test_scoped_step3_proxy_delegates_with_standard_api():
    """proxy.screen() forwards args with the unmodified Step3 public signature."""
    real = _FakeRealStep3Engine()
    proxy = _ScopedStep3ScreeningProxy(real, ["AAPL", "MSFT"])
    proxy.screen(
        signal_date=RUN_DATE,
        strategy_config={"k": "v"},
        strategy_config_id="normal",
        db_role="debug",
        run_id="t",
    )
    call = real.calls[-1]
    assert call["signal_date"] == RUN_DATE
    assert call["strategy_config"] == {"k": "v"}
    assert call["strategy_config_id"] == "normal"
    assert call["db_role"] == "debug"
    assert call["run_id"] == "t"
    # No 'tickers' key — the standard Step3 public API is unmodified.
    assert "tickers" not in call


def test_scoped_step3_proxy_exposes_selected_tickers():
    proxy = _ScopedStep3ScreeningProxy(object(), ["NVDA", "AMD"])
    assert proxy.selected_tickers == ["NVDA", "AMD"]


def test_scoped_step3_proxy_returns_real_engine_result():
    real = _FakeRealStep3Engine()
    proxy = _ScopedStep3ScreeningProxy(real, ["A"])
    result = proxy.screen(RUN_DATE, {}, "n", "debug")
    assert result.status == service_result.STATUS_SUCCESS


# --------------------------------------------------------------------------- #
# Scope-detection helpers.
# --------------------------------------------------------------------------- #
def test_needs_feature_scope_true_for_indicator_validation():
    preset = DEBUG_PRESETS["indicator_validation"]
    plan = _plan(start_step=preset.start_step, end_step=preset.end_step)
    assert _needs_feature_scope(plan) is True
    assert _needs_step3_scope(plan) is False


def test_needs_step3_scope_true_for_config_tuning():
    preset = DEBUG_PRESETS["config_tuning_test"]
    plan = _plan(start_step=preset.start_step, end_step=preset.end_step)
    assert _needs_step3_scope(plan) is True
    assert _needs_feature_scope(plan) is False


def test_no_scope_needed_for_full_pipeline_presets():
    for name in ("fast_smoke_test", "pipeline_sanity"):
        p = DEBUG_PRESETS[name]
        plan = _plan(start_step=p.start_step, end_step=p.end_step)
        assert not _needs_feature_scope(plan), name
        assert not _needs_step3_scope(plan), name


# --------------------------------------------------------------------------- #
# Guards — no orchestrator built on failure.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["prod", "simulation"])
def test_forbidden_db_role_rejected(role):
    result = make_controller().run(_plan(db_role=role))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []
    assert any("must not target db_role" in e for e in result.errors)


def test_non_debug_role_rejected():
    result = make_controller().run(_plan(db_role="other"))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_sample_count_cap_rejected():
    result = make_controller().run(_plan(sample_count=MAX_DEBUG_SAMPLE + 1))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []
    assert any("exceeds the debug cap" in e for e in result.errors)


def test_sample_count_zero_rejected():
    assert make_controller().run(_plan(sample_count=0)).status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_inverted_step_range_rejected():
    result = make_controller().run(_plan(start_step="step5_proposals", end_step="feature_calculation"))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_empty_strategy_names_rejected():
    assert make_controller().run(_plan(strategy_names=())).status == service_result.STATUS_FAILED


def test_unknown_preset_rejected():
    result = make_controller().run_preset("does_not_exist", RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []


def test_unknown_strategy_rejected():
    assert make_controller().run(_plan(strategy_names=("ghost",))).status == service_result.STATUS_FAILED


def test_oversized_watchlist_rejected():
    big = tuple(f"T{i:04d}" for i in range(MAX_DEBUG_SAMPLE + 1))
    result = make_controller().run(_plan(watchlist=big, sample_count=20))
    assert result.status == service_result.STATUS_FAILED
    assert FakeOrchestrator.instances == []
    assert any("de-duplication" in e for e in result.errors)


def test_deduped_watchlist_within_cap_accepted():
    prov = FakeProvider([f"T{i:04d}" for i in range(MAX_DEBUG_SAMPLE)])
    dup_wl = tuple(f"T{i:04d}" for i in range(MAX_DEBUG_SAMPLE)) + tuple(f"T{i:04d}" for i in range(50))
    result = make_controller(provider=prov).run(_plan(watchlist=dup_wl, sample_count=20))
    assert result.status == service_result.STATUS_SUCCESS


# --------------------------------------------------------------------------- #
# Ticker-scope — indicator_validation (feature_calculation start).
# --------------------------------------------------------------------------- #
def test_indicator_validation_injects_scoped_feature_engine():
    """feature engine is FakeScopedFeatureEngine (not None) with sampled tickers."""
    prov = FakeProvider([f"T{i:02d}" for i in range(15)])
    controller = make_controller(provider=prov)
    controller.run_preset("indicator_validation", RUN_DATE)

    feat = FakeOrchestrator.instances[-1].init_kwargs["feature_engine"]
    assert isinstance(feat, FakeScopedFeatureEngine), type(feat)
    assert len(feat.selected_tickers) == 10            # preset cap
    assert feat.selected_tickers == sorted(feat.selected_tickers)


def test_indicator_validation_tickers_match_provider_sorted_head():
    symbols = ["TSLA", "AAPL", "MSFT", "GOOG", "AMZN",
               "NVDA", "META", "NFLX", "ORCL", "IBM", "HPQ"]
    controller = make_controller(provider=FakeProvider(symbols))
    controller.run_preset("indicator_validation", RUN_DATE)
    feat = FakeOrchestrator.instances[-1].init_kwargs["feature_engine"]
    assert feat.selected_tickers == sorted(symbols)[:10]


def test_indicator_validation_selected_tickers_never_none():
    controller = make_controller(provider=FakeProvider(["AAPL", "MSFT", "TSLA"]))
    controller.run_preset("indicator_validation", RUN_DATE)
    feat = FakeOrchestrator.instances[-1].init_kwargs["feature_engine"]
    assert feat.selected_tickers is not None
    assert len(feat.selected_tickers) > 0


def test_indicator_validation_bridge_steps_are_noops():
    controller = make_controller(provider=FakeProvider(["A", "B"]))
    controller.run_preset("indicator_validation", RUN_DATE)
    init = FakeOrchestrator.instances[-1].init_kwargs
    for kwarg in ("ingestion_engine", "validation_engine", "mutation_engine"):
        assert isinstance(init[kwarg], _NoOpStepEngine), kwarg
    assert isinstance(init["screening_engine"], _NoOpStepEngine)  # after end


# --------------------------------------------------------------------------- #
# Ticker-scope — config_tuning_test (step3_screening start).
# The Step3 engine must be FakeScopedStep3Engine (from the injected factory)
# with the sampled ticker list, proving it evaluates only those tickers instead
# of all daily_features_current rows.
# --------------------------------------------------------------------------- #
def test_config_tuning_injects_scoped_step3_engine():
    """step3 engine is FakeScopedStep3Engine (not None) capped at preset limit."""
    prov = FakeProvider([f"T{i:03d}" for i in range(600)])
    controller = make_controller(provider=prov)
    controller.run_preset("config_tuning_test", RUN_DATE)

    s3 = FakeOrchestrator.instances[-1].init_kwargs["screening_engine"]
    assert isinstance(s3, FakeScopedStep3Engine), type(s3)
    assert len(s3.selected_tickers) == 500             # preset cap
    assert s3.selected_tickers == sorted(s3.selected_tickers)


def test_config_tuning_step3_tickers_scoped_not_all_db():
    """With 10-ticker provider and 500 cap, exactly those 10 are selected."""
    prov = FakeProvider([f"S{i}" for i in range(10)])
    controller = make_controller(provider=prov)
    controller.run_preset("config_tuning_test", RUN_DATE)
    s3 = FakeOrchestrator.instances[-1].init_kwargs["screening_engine"]
    assert len(s3.selected_tickers) == 10
    assert set(s3.selected_tickers) == {f"S{i}" for i in range(10)}


def test_config_tuning_step3_tickers_are_deterministic():
    syms = [f"X{i:03d}" for i in range(20)]
    c1 = make_controller(provider=FakeProvider(syms[:]))
    c2 = make_controller(provider=FakeProvider(syms[:]))
    c1.run_preset("config_tuning_test", RUN_DATE, sample_count=5)
    c2.run_preset("config_tuning_test", RUN_DATE, sample_count=5)
    t1 = FakeOrchestrator.instances[-2].init_kwargs["screening_engine"].selected_tickers
    t2 = FakeOrchestrator.instances[-1].init_kwargs["screening_engine"].selected_tickers
    assert t1 == t2


def test_config_tuning_bridge_steps_are_noops():
    controller = make_controller(provider=FakeProvider(["A", "B"]))
    controller.run_preset("config_tuning_test", RUN_DATE)
    init = FakeOrchestrator.instances[-1].init_kwargs
    for kwarg in ("ingestion_engine", "validation_engine",
                  "mutation_engine", "feature_engine"):
        assert isinstance(init[kwarg], _NoOpStepEngine), kwarg
    # Step4, Step5 are in range — real engines.
    assert init["analysis_engine"] is None
    assert init["proposal_engine"] is None


def test_config_tuning_selected_tickers_capped():
    """With 600-symbol provider and preset cap 500, exactly 500 are selected."""
    prov = FakeProvider([f"T{i:03d}" for i in range(600)])
    controller = make_controller(provider=prov)
    controller.run_preset("config_tuning_test", RUN_DATE)
    s3 = FakeOrchestrator.instances[-1].init_kwargs["screening_engine"]
    assert len(s3.selected_tickers) == 500


# --------------------------------------------------------------------------- #
# Full-pipeline presets — no scoped engines injected.
# --------------------------------------------------------------------------- #
def test_full_pipeline_no_scoped_engines():
    for name in ("fast_smoke_test", "pipeline_sanity"):
        FakeOrchestrator.instances.clear()
        controller = make_controller(provider=FakeProvider(["A", "B"]))
        controller.run_preset(name, RUN_DATE)
        init = FakeOrchestrator.instances[-1].init_kwargs
        assert init["feature_engine"] is None, name
        assert init["screening_engine"] is None, name


# --------------------------------------------------------------------------- #
# Force-rerun forwarding.
# --------------------------------------------------------------------------- #
def test_force_rerun_true_by_default():
    make_controller().run(_plan())
    assert FakeOrchestrator.instances[-1].run_kwargs["force_rerun"] is True


def test_force_rerun_false_explicit():
    make_controller().run(_plan(force_rerun=False))
    assert FakeOrchestrator.instances[-1].run_kwargs["force_rerun"] is False


def test_run_preset_force_rerun_override():
    make_controller(provider=FakeProvider(["A"])).run_preset(
        "fast_smoke_test", RUN_DATE, force_rerun=False
    )
    assert FakeOrchestrator.instances[-1].run_kwargs["force_rerun"] is False


# --------------------------------------------------------------------------- #
# DB-role / run-type isolation.
# --------------------------------------------------------------------------- #
def test_run_forces_debug_role_and_type():
    make_controller().run(_plan())
    rk = FakeOrchestrator.instances[-1].run_kwargs
    assert rk["db_role"] == DB_ROLE_DEBUG
    assert rk["run_type"] == RUN_TYPE_DEBUG


def test_run_never_uses_forbidden_role():
    make_controller().run(_plan())
    assert FakeOrchestrator.instances[-1].run_kwargs["db_role"] not in dm.FORBIDDEN_DB_ROLES


# --------------------------------------------------------------------------- #
# All presets runnable.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(DEBUG_PRESETS))
def test_all_presets_runnable(name):
    controller = make_controller(provider=FakeProvider([f"T{i}" for i in range(600)]))
    result = controller.run_preset(name, RUN_DATE)
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["debug"]["db_role"] == DB_ROLE_DEBUG


# --------------------------------------------------------------------------- #
# Result metadata.
# --------------------------------------------------------------------------- #
def test_metadata_selected_tickers_for_indicator_validation():
    result = make_controller(provider=FakeProvider(["AAPL", "MSFT"])).run_preset(
        "indicator_validation", RUN_DATE
    )
    dbg = result.metadata["debug"]
    assert dbg["selected_tickers"] == ["AAPL", "MSFT"]
    assert dbg["needs_feature_scope"] is True
    assert dbg["needs_step3_scope"] is False


def test_metadata_selected_tickers_for_config_tuning():
    result = make_controller(provider=FakeProvider(["A", "B", "C"])).run_preset(
        "config_tuning_test", RUN_DATE
    )
    dbg = result.metadata["debug"]
    assert dbg["selected_tickers"] == ["A", "B", "C"]
    assert dbg["needs_step3_scope"] is True


def test_metadata_selected_tickers_none_for_full_pipeline():
    result = make_controller(provider=FakeProvider(["A"])).run_preset("fast_smoke_test", RUN_DATE)
    dbg = result.metadata["debug"]
    assert dbg["selected_tickers"] is None
    assert dbg["needs_feature_scope"] is False
    assert dbg["needs_step3_scope"] is False


def test_metadata_effective_start_step():
    result = make_controller().run(
        _plan(start_step="feature_calculation", end_step="feature_calculation")
    )
    dbg = result.metadata["debug"]
    assert dbg["start_step"] == "feature_calculation"
    assert dbg["effective_start_step"] == "universe_ingestion"


def test_metadata_force_rerun_recorded():
    assert make_controller().run(_plan()).metadata["debug"]["force_rerun"] is True
    assert make_controller().run(_plan(force_rerun=False)).metadata["debug"]["force_rerun"] is False


def test_metadata_watchlist_recorded():
    prov = FakeProvider(["AAPL", "MSFT"])
    result = make_controller(provider=prov).run(_plan(watchlist=("AAPL",), sample_count=20))
    dbg = result.metadata["debug"]
    assert dbg["watchlist"] == ["AAPL"]
    assert dbg["sample_count"] is None


# --------------------------------------------------------------------------- #
# Config id fix (M21 review fix #1)
# --------------------------------------------------------------------------- #
class FakeConfigSvcDebug:
    """Minimal ConfigService fake: returns real DB-style config ids."""

    def get_active_strategy_configs(self, db_role):
        from app.utils.service_result import ServiceResult
        from app.utils import service_result as sr
        return ServiceResult(
            sr.STATUS_SUCCESS,
            "r",
            metadata={
                "configs": {"normal": {"k": 1}},
                "configs_by_strategy": {"normal": {"k": 1}},
                "configs_by_id": {"seed_strategy_debug_normal_v1": {"k": 1}},
                "config_ids_by_strategy": {"normal": "seed_strategy_debug_normal_v1"},
                "config_ids": ["seed_strategy_debug_normal_v1"],
            },
        )

    def seed_default_strategy_configs(self, db_role):
        from app.utils.service_result import ServiceResult
        from app.utils import service_result as sr
        return ServiceResult(sr.STATUS_SUCCESS, "r", metadata={"seeded": 0})


def test_debug_selected_strategy_uses_real_config_id() -> None:
    """Configs passed to orchestrator must be keyed by real DB config_id (fix #1)."""
    received_keys: list[str] = []

    class _CapturingOrchestrator:
        def __init__(self, **kwargs):
            self._kw = kwargs
            self._received = None

        def run(self, **kwargs):
            self._received = kwargs.get("strategy_configs", {})
            received_keys.extend(self._received.keys())
            from app.utils.service_result import ServiceResult
            from app.utils import service_result as sr
            return ServiceResult(
                sr.STATUS_SUCCESS,
                "r",
                metadata={
                    "steps_completed": [],
                    "warnings": [],
                    "run_id": "r",
                    "run_date": str(RUN_DATE),
                    "run_type": "debug",
                    "db_role": "debug",
                    "failed_step": None,
                    "error": None,
                },
            )

    controller = DebugModeController(
        db_manager=object(),
        provider=FakeProvider(["AAA"]),
        orchestrator_factory=_CapturingOrchestrator,
        scoped_feature_engine_factory=lambda db, t: FakeScopedFeatureEngine(t),
        scoped_screening_engine_factory=lambda db, t: FakeScopedStep3Engine(t),
        config_service=FakeConfigSvcDebug(),
    )
    plan = _plan(strategy_names=("normal",))
    result = controller.run(plan)
    assert result.is_ok(), result.errors
    # The key passed to the orchestrator must be the real DB config_id.
    assert received_keys == ["seed_strategy_debug_normal_v1"], (
        f"expected real config_id, got {received_keys}"
    )
