"""Module 22 — Debug Mode.

Fast, local debug/testing mode for *partial*, *sampled* pipeline runs. Module 22
is a thin **control plane** on top of the frozen Module 20 pipeline
orchestrator: it performs no market-data, screening, proposal, outcome,
simulation, or dashboard logic of its own. Every domain action is delegated to
the orchestrator (and, through it, the frozen step engines), so debug runs
exercise exactly the same code paths as a production run — just with fewer
tickers and a narrower step range.

What this module guarantees (the reason it exists):

* **Write isolation.** A debug run *always* targets ``db_role="debug"`` and
  ``run_type="debug"``. The ``prod`` and ``simulation`` roles are rejected
  before any orchestrator is constructed, so a debug run can never write to
  ``prod.duckdb`` or ``simulation.duckdb`` (02b §22.4, 02 §21,
  ``01b_SCHEMA_AND_DATA.md`` ``run_type`` enum).
* **Bounded scope.** Tickers are limited deterministically through a
  :class:`SamplingProvider` wrapper, capped at :data:`MAX_DEBUG_SAMPLE` (both
  sample_count and de-duplicated watchlist size), and the executed step range is
  bounded by ``start_step`` / ``end_step``. A debug run therefore cannot
  accidentally trigger full production-scale processing.
* **Determinism / configurability.** Behaviour is described by immutable
  :class:`DebugPreset` presets (the four presets from
  ``01e_UI_AND_TESTING.md`` §92) resolved into an immutable
  :class:`DebugRunPlan`. Sampling is a stable sort + head, so the same inputs
  always select the same tickers.
* **Rerunnability.** ``force_rerun=True`` is forwarded to the orchestrator by
  default so repeated debug runs against the same ``run_date`` are not blocked
  by the already-run guard.

Mechanism (no frozen module is modified):

* **Start boundary** uses the orchestrator's existing ``resume_from`` argument.
* **Ticker-scope for partial runs.**  When ``start_step`` comes *after*
  ``universe_ingestion``, the controller *lowers* the effective start to
  ``universe_ingestion`` and runs the universe step with the
  :class:`SamplingProvider`. This updates ``debug.duckdb``'s
  ``ticker_master`` / ``ticker_universe_snapshot`` to the sampled set only, so
  all downstream engines (features, Step 3–5) naturally operate on those tickers
  without any engine modification. Steps between ``universe_ingestion`` and the
  declared ``start_step`` are injected as :class:`_NoOpStepEngine` (bridge
  no-ops). ``prod.duckdb`` is never touched.
* **End boundary.** Every *injectable* step engine whose step index exceeds
  ``end_step`` receives a :class:`_NoOpStepEngine`. In-range engines are passed
  as ``None`` so the orchestrator builds the real one. (The orchestrator's two
  internal tail steps, ``dashboard_materialization`` — a V1 no-op — and
  ``backup`` — a copy of ``debug.duckdb`` — are not injectable and may still
  run; this is a harmless debug tail artifact documented in the spec.)
* **Ticker limiting.** The provider is always wrapped in
  :class:`SamplingProvider`, which rewrites ``list_symbols`` metadata to at most
  ``sample_count`` entries (stable sort + head) or exactly the watchlist entries.
  All four provider methods are covered, including objects with a ``.ticker``
  attribute (:class:`~app.providers.provider_interface.TickerInfo`).

Heavy dependencies (the orchestrator, the DuckDB manager, the default provider)
are imported lazily so the module — and its offline tests — import cleanly
without ``duckdb`` when fakes are injected.

Contract source of truth: ``M22_DEBUG_MODE_SPEC.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Final, Mapping, Sequence

from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# Roles / run type / guards.
# --------------------------------------------------------------------------- #
DB_ROLE_DEBUG: Final[str] = "debug"
RUN_TYPE_DEBUG: Final[str] = "debug"

# Roles a debug run must never target (write-isolation guard).
FORBIDDEN_DB_ROLES: Final[tuple[str, ...]] = ("prod", "simulation")

# Sampling caps.
DEFAULT_DEBUG_SAMPLE: Final[int] = 20
MAX_DEBUG_SAMPLE: Final[int] = 500

# --------------------------------------------------------------------------- #
# Orchestrator step names (kept local so importing this module never pulls
# the orchestrator -> duckdb at import time).
# --------------------------------------------------------------------------- #
STEP_NAMES: Final[tuple[str, ...]] = (
    "benchmark_etf_ingestion",   # 0
    "universe_ingestion",        # 1
    "price_ingestion",           # 2
    "validation",                # 3
    "mutation_detection",        # 4
    "feature_calculation",       # 5
    "step3_screening",           # 6
    "step4_analysis",            # 7
    "step5_proposals",           # 8
    "outcome_queue_creation",    # 9
    "outcome_processing",        # 10
    "dashboard_materialization", # 11 — internal, not injectable
    "backup",                    # 12 — internal, not injectable
)

# Index of universe_ingestion; used by the ticker-scope logic.
_UNIVERSE_STEP_IDX: Final[int] = STEP_NAMES.index("universe_ingestion")

# Injectable step → PipelineOrchestrator.__init__ kwarg name.
# dashboard_materialization and backup are internal (no injection point).
_STEP_ENGINE_KWARG: Final[Mapping[str, str]] = {
    "benchmark_etf_ingestion": "benchmark_loader",
    "universe_ingestion":      "universe_engine",
    "price_ingestion":         "ingestion_engine",
    "validation":              "validation_engine",
    "mutation_detection":      "mutation_engine",
    "feature_calculation":     "feature_engine",
    "step3_screening":         "screening_engine",
    "step4_analysis":          "analysis_engine",
    "step5_proposals":         "proposal_engine",
    "outcome_queue_creation":  "outcome_creator",
    "outcome_processing":      "outcome_processor",
}

# Convenience step anchors for preset definitions.
_STEP_FEATURES:  Final[str] = "feature_calculation"
_STEP_SCREEN:    Final[str] = "step3_screening"
_STEP_PROPOSALS: Final[str] = "step5_proposals"
_STEP_FIRST:     Final[str] = STEP_NAMES[0]
_STEP_LAST:      Final[str] = STEP_NAMES[-1]


# --------------------------------------------------------------------------- #
# Presets (01e_UI_AND_TESTING.md §92 "Debug Mode Presets").
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DebugPreset:
    """Immutable debug-run preset (01e §92).

    Attributes
    ----------
    name:
        Stable key used in ``DEBUG_PRESETS``.
    sample_count:
        Universe ticker limit (≤ :data:`MAX_DEBUG_SAMPLE`).
    trading_days:
        Intended price-history depth in NYSE sessions — plan metadata only (see
        spec §7 / ``M22_DEBUG_MODE_SPEC.md``).
    start_step / end_step:
        Inclusive orchestrator step range (members of :data:`STEP_NAMES`).
    strategy_names:
        Strategy configs forwarded to the orchestrator.
    description:
        Human-readable preset summary from 01e §92.
    """

    name: str
    sample_count: int
    trading_days: int
    start_step: str
    end_step: str
    strategy_names: tuple[str, ...]
    description: str


DEBUG_PRESETS: Final[Mapping[str, DebugPreset]] = {
    "fast_smoke_test": DebugPreset(
        name="fast_smoke_test",
        sample_count=20,
        trading_days=5,
        start_step=_STEP_FIRST,
        end_step=_STEP_LAST,
        strategy_names=("normal",),
        description="20 tickers, 5 trading days, full pipeline.",
    ),
    "indicator_validation": DebugPreset(
        name="indicator_validation",
        sample_count=10,
        trading_days=90,
        start_step=_STEP_FEATURES,
        end_step=_STEP_FEATURES,
        strategy_names=("normal",),
        description="10 tickers, 90 trading days, indicators (Step2) only.",
    ),
    "pipeline_sanity": DebugPreset(
        name="pipeline_sanity",
        sample_count=100,
        trading_days=30,
        start_step=_STEP_FIRST,
        end_step=_STEP_PROPOSALS,
        strategy_names=("normal",),
        description="100 tickers, 30 trading days, Step1-Step5.",
    ),
    "config_tuning_test": DebugPreset(
        name="config_tuning_test",
        sample_count=500,
        trading_days=126,
        start_step=_STEP_SCREEN,
        end_step=_STEP_PROPOSALS,
        strategy_names=("normal", "aggressive", "conservative"),
        description="500 tickers, ~6 months, Step3-Step5.",
    ),
}


# --------------------------------------------------------------------------- #
# Resolved run plan.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DebugRunPlan:
    """Fully-resolved, validated description of one debug run.

    ``db_role`` and ``run_type`` are fixed to the debug values at construction
    and are never caller-controllable through this dataclass. ``force_rerun``
    defaults to ``True`` so repeated debug runs against the same ``run_date``
    are not blocked by the orchestrator's already-run guard.
    """

    run_date: date
    sample_count: int
    start_step: str
    end_step: str
    strategy_names: tuple[str, ...]
    watchlist: tuple[str, ...] | None = None
    preset_name: str | None = None
    db_role: str = DB_ROLE_DEBUG
    run_type: str = RUN_TYPE_DEBUG
    trading_days: int | None = None
    force_rerun: bool = True

    # ------------------------------------------------------------------ #
    # Step-index helpers.
    # ------------------------------------------------------------------ #
    @property
    def start_index(self) -> int:
        return STEP_NAMES.index(self.start_step)

    @property
    def end_index(self) -> int:
        return STEP_NAMES.index(self.end_step)

    @property
    def effective_start_step(self) -> str:
        """Actual first step the orchestrator will execute.

        When ``start_step`` is *after* ``universe_ingestion`` the controller
        lowers the start to ``universe_ingestion`` so the universe snapshot in
        ``debug.duckdb`` is updated to the sampled ticker set before the
        declared start step runs (the ticker-scope mechanism).  Steps between
        ``universe_ingestion`` and ``start_step`` are injected as bridge no-ops.
        """
        if self.start_index > _UNIVERSE_STEP_IDX:
            return STEP_NAMES[_UNIVERSE_STEP_IDX]
        return self.start_step

    @property
    def effective_start_index(self) -> int:
        return STEP_NAMES.index(self.effective_start_step)

    @property
    def resume_from(self) -> str | None:
        """``resume_from`` value for ``PipelineOrchestrator.run``.

        ``None`` when starting at step 0 (benchmark); otherwise the effective
        start step name.  The effective start may be ``universe_ingestion``
        even when the declared ``start_step`` is later (ticker-scope mechanism).
        """
        eff_idx = self.effective_start_index
        return None if eff_idx == 0 else self.effective_start_step

    # ------------------------------------------------------------------ #
    # No-op classification (used by the controller and metadata).
    # ------------------------------------------------------------------ #
    def _is_noop(self, step: str) -> bool:
        """Return ``True`` if ``step`` will receive a ``_NoOpStepEngine``.

        A step is a no-op when it falls into either of two regions:

        * **After end** — step index > ``end_index`` (out-of-range tail).
        * **Bridge** — step index is strictly between ``effective_start_index``
          and ``start_index`` (steps skipped for ticker-scope purposes but
          needed to avoid full data re-ingestion).
        """
        s_idx = STEP_NAMES.index(step)
        if s_idx > self.end_index:
            return True
        eff_idx = self.effective_start_index
        if eff_idx < s_idx < self.start_index:
            return True
        return False

    def executed_steps(self) -> list[str]:
        """Injectable steps that will run with a real engine."""
        eff_idx = self.effective_start_index
        return [
            s for s in _STEP_ENGINE_KWARG
            if STEP_NAMES.index(s) >= eff_idx and not self._is_noop(s)
        ]

    def noop_steps(self) -> list[str]:
        """Injectable steps replaced by a no-op (bridge or after end)."""
        eff_idx = self.effective_start_index
        return [
            s for s in _STEP_ENGINE_KWARG
            if STEP_NAMES.index(s) >= eff_idx and self._is_noop(s)
        ]


# --------------------------------------------------------------------------- #
# Deterministic ticker-sampling provider wrapper.
# --------------------------------------------------------------------------- #
class SamplingProvider:
    """Provider decorator that deterministically limits the symbol universe.

    Only :meth:`list_symbols` is altered: its ``ServiceResult`` is rewritten so
    ``metadata["symbols"]`` contains at most ``sample_count`` entries (or
    exactly the ``watchlist`` entries), selected by a stable sort on the symbol
    key.  Every other provider method delegates to the wrapped provider
    unchanged.

    The ``_symbol_key`` helper handles plain strings, dicts, and objects with
    a ``.ticker`` or ``.symbol`` attribute (covers
    :class:`~app.providers.provider_interface.TickerInfo`).
    """

    def __init__(
        self,
        provider: Any,
        *,
        sample_count: int | None = None,
        watchlist: Sequence[str] | None = None,
    ) -> None:
        self._provider = provider
        self._sample_count = sample_count
        self._watchlist = tuple(watchlist) if watchlist else None

    # -- altered method ------------------------------------------------- #
    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        result = self._provider.list_symbols(symbol_type=symbol_type)
        if getattr(result, "status", None) == service_result.STATUS_FAILED:
            return result
        symbols = list(getattr(result, "metadata", {}).get("symbols", []))
        sampled = self._select(symbols)
        metadata = dict(getattr(result, "metadata", {}))
        metadata["symbols"] = sampled
        metadata["debug_sampled"] = True
        metadata["debug_sample_count"] = len(sampled)
        return ServiceResult(
            status=getattr(result, "status", service_result.STATUS_SUCCESS),
            run_id=getattr(result, "run_id", "debug"),
            rows_processed=len(sampled),
            warnings=list(getattr(result, "warnings", []) or []),
            errors=list(getattr(result, "errors", []) or []),
            metadata=metadata,
        )

    def _select(self, symbols: list[Any]) -> list[Any]:
        if self._watchlist is not None:
            wanted = set(self._watchlist)
            picked = [s for s in symbols if self._symbol_key(s) in wanted]
            return sorted(picked, key=self._symbol_key)
        ordered = sorted(symbols, key=self._symbol_key)
        if self._sample_count is None:
            return ordered
        return ordered[: self._sample_count]

    @staticmethod
    def _symbol_key(entry: Any) -> str:
        """Return a stable sort key for a provider symbol entry."""
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            for k in ("symbol", "ticker", "Symbol", "Ticker"):
                if k in entry and entry[k] is not None:
                    return str(entry[k])
        # Dataclass / namedtuple / TickerInfo with named attribute.
        for attr in ("ticker", "symbol", "Symbol", "Ticker"):
            val = getattr(entry, attr, None)
            if val is not None:
                return str(val)
        return str(entry)

    # -- delegated methods ---------------------------------------------- #
    def get_capabilities(self) -> ServiceResult:
        return self._provider.get_capabilities()

    def get_price_history(self, request: Any) -> ServiceResult:
        return self._provider.get_price_history(request)

    def get_earnings(self, ticker: str) -> ServiceResult:
        return self._provider.get_earnings(ticker)


# --------------------------------------------------------------------------- #
# No-op engine for bridge / out-of-range steps.
# --------------------------------------------------------------------------- #
class _NoOpStepEngine:
    """Trivial engine that satisfies any step call with an immediate success.

    Injected for bridge no-ops (between ``universe_ingestion`` and the declared
    ``start_step``) and for steps after ``end_step``. Any method invoked on it
    returns a ``success`` :class:`ServiceResult` with ``rows_processed=0`` so
    the orchestrator records the step without running its heavy domain logic.
    """

    __slots__ = ("_step",)

    def __init__(self, step_name: str) -> None:
        self._step = step_name

    def __getattr__(self, _name: str) -> Callable[..., ServiceResult]:
        def _call(**kwargs: Any) -> ServiceResult:
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id=str(kwargs.get("run_id", "debug-noop")),
                rows_processed=0,
                metadata={"debug_noop_step": self._step},
            )

        return _call


# --------------------------------------------------------------------------- #
# Controller.
# --------------------------------------------------------------------------- #
class DebugModeController:
    """Drive fast, sampled, partial pipeline runs against ``debug.duckdb`` only.

    Guarantees
    ----------
    * ``db_role="debug"``/``run_type="debug"`` are always forwarded; prod and
      simulation roles are rejected before an orchestrator is constructed.
    * ``force_rerun=True`` is forwarded by default so the same ``run_date`` can
      be debugged repeatedly.
    * ``sample_count`` (or de-duplicated watchlist size) is capped at
      :data:`MAX_DEBUG_SAMPLE`.
    * When ``start_step > universe_ingestion``, the effective orchestrator start
      is lowered to ``universe_ingestion`` so the debug universe snapshot is
      scoped to sampled tickers before downstream steps run.

    All dependencies are injectable for offline tests; real defaults are imported
    lazily so no ``duckdb`` import occurs when fakes are in use.
    """

    def __init__(
        self,
        db_manager: Any | None = None,
        provider: Any | None = None,
        orchestrator_factory: Callable[..., Any] | None = None,
        strategy_configs: Mapping[str, dict] | None = None,
    ) -> None:
        self._db_manager = db_manager
        self._provider = provider
        self._orchestrator_factory = orchestrator_factory
        self._strategy_configs = strategy_configs

    # ------------------------------------------------------------------ #
    # Public API.
    # ------------------------------------------------------------------ #
    def run_preset(
        self,
        preset_name: str,
        run_date: date,
        *,
        sample_count: int | None = None,
        watchlist: Sequence[str] | None = None,
        strategy_names: Sequence[str] | None = None,
        force_rerun: bool = True,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Resolve a named preset into a plan and execute it.

        Caller overrides (``sample_count`` / ``watchlist`` / ``strategy_names``
        / ``force_rerun``) take precedence over the preset's defaults.  An
        unknown preset returns a ``failed`` result without touching the DB.
        """
        preset = DEBUG_PRESETS.get(preset_name)
        if preset is None:
            return self._failed(
                run_id,
                f"unknown debug preset {preset_name!r}; "
                f"valid: {sorted(DEBUG_PRESETS)}",
            )
        # De-duplicate watchlist before building the plan so the cap check
        # inside _validate_plan sees the true unique count.
        resolved_watchlist: tuple[str, ...] | None = None
        if watchlist is not None:
            resolved_watchlist = tuple(dict.fromkeys(watchlist))  # ordered dedup
        plan = DebugRunPlan(
            run_date=run_date,
            sample_count=(
                sample_count if sample_count is not None else preset.sample_count
            ),
            start_step=preset.start_step,
            end_step=preset.end_step,
            strategy_names=(
                tuple(strategy_names)
                if strategy_names is not None
                else preset.strategy_names
            ),
            watchlist=resolved_watchlist,
            preset_name=preset.name,
            trading_days=preset.trading_days,
            force_rerun=force_rerun,
        )
        return self.run(plan, run_id=run_id)

    def run(self, plan: DebugRunPlan, run_id: str | None = None) -> ServiceResult:
        """Validate ``plan``, build the orchestrator, and execute the debug run.

        Always returns a :class:`ServiceResult`; guard failures are reported as
        ``failed`` results and never construct an orchestrator or open the DB.
        """
        log = logging_config.get_logger(__name__, run_id)

        guard_error = self._validate_plan(plan)
        if guard_error is not None:
            log.error("debug-mode guard failed: %s", guard_error)
            return self._failed(run_id, guard_error, plan=plan)

        configs = self._resolve_strategy_configs(plan.strategy_names)
        if isinstance(configs, str):
            log.error("debug-mode config resolution failed: %s", configs)
            return self._failed(run_id, configs, plan=plan)

        base_provider = self._resolve_base_provider()
        sampling_provider = SamplingProvider(
            base_provider,
            sample_count=None if plan.watchlist else plan.sample_count,
            watchlist=plan.watchlist,
        )
        engine_kwargs = self._build_engine_kwargs(plan)
        factory = self._resolve_orchestrator_factory()
        orchestrator = factory(
            db_manager=self._db_manager,
            provider=sampling_provider,
            **engine_kwargs,
        )

        log.info(
            "debug run start preset=%s sample=%s steps=%s..%s "
            "(effective_start=%s) strategies=%s force_rerun=%s",
            plan.preset_name,
            "watchlist" if plan.watchlist else plan.sample_count,
            plan.start_step,
            plan.end_step,
            plan.effective_start_step,
            ",".join(plan.strategy_names),
            plan.force_rerun,
        )

        result = orchestrator.run(
            run_date=plan.run_date,
            run_type=RUN_TYPE_DEBUG,
            db_role=DB_ROLE_DEBUG,
            resume_from=plan.resume_from,
            strategy_configs=configs,
            force_rerun=plan.force_rerun,
            run_id=run_id,
        )
        return self._augment(result, plan)

    # ------------------------------------------------------------------ #
    # Validation.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_plan(plan: DebugRunPlan) -> str | None:
        """Return an error string if the plan is unsafe/invalid, else ``None``."""
        if plan.db_role in FORBIDDEN_DB_ROLES:
            return (
                f"debug mode must not target db_role {plan.db_role!r}; "
                f"debug runs write only to {DB_ROLE_DEBUG!r}"
            )
        if plan.db_role != DB_ROLE_DEBUG:
            return (
                f"invalid debug db_role {plan.db_role!r}; "
                f"must be {DB_ROLE_DEBUG!r}"
            )
        if plan.run_type != RUN_TYPE_DEBUG:
            return (
                f"invalid debug run_type {plan.run_type!r}; "
                f"must be {RUN_TYPE_DEBUG!r}"
            )
        if plan.start_step not in STEP_NAMES:
            return f"invalid start_step {plan.start_step!r}"
        if plan.end_step not in STEP_NAMES:
            return f"invalid end_step {plan.end_step!r}"
        if plan.start_index > plan.end_index:
            return (
                f"start_step {plan.start_step!r} is after "
                f"end_step {plan.end_step!r}"
            )
        if not plan.strategy_names:
            return "strategy_names must be non-empty"
        # Watchlist cap (checked on unique count after de-duplication).
        if plan.watchlist is not None:
            unique_count = len(set(plan.watchlist))
            if unique_count > MAX_DEBUG_SAMPLE:
                return (
                    f"watchlist has {unique_count} unique tickers after "
                    f"de-duplication, exceeds the debug cap {MAX_DEBUG_SAMPLE}; "
                    f"refusing to run production-scale scope"
                )
        else:
            if plan.sample_count < 1:
                return "sample_count must be >= 1 when no watchlist is given"
            if plan.sample_count > MAX_DEBUG_SAMPLE:
                return (
                    f"sample_count {plan.sample_count} exceeds the debug cap "
                    f"{MAX_DEBUG_SAMPLE}; refusing to run production-scale scope"
                )
        return None

    # ------------------------------------------------------------------ #
    # Dependency resolution (lazy defaults).
    # ------------------------------------------------------------------ #
    def _resolve_strategy_configs(
        self, names: tuple[str, ...]
    ) -> dict[str, dict] | str:
        source = self._strategy_configs
        if source is None:
            from app.services.pipeline.pipeline_orchestrator import (
                DEFAULT_STRATEGY_CONFIGS,
            )
            source = DEFAULT_STRATEGY_CONFIGS
        selected: dict[str, dict] = {}
        for name in names:
            if name not in source:
                return (
                    f"unknown strategy config {name!r}; "
                    f"valid: {sorted(source)}"
                )
            selected[name] = source[name]
        return selected

    def _resolve_base_provider(self) -> Any:
        if self._provider is not None:
            return self._provider
        from app.providers.yahoo_provider import YahooProvider
        return YahooProvider()

    def _resolve_orchestrator_factory(self) -> Callable[..., Any]:
        if self._orchestrator_factory is not None:
            return self._orchestrator_factory
        from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
        return PipelineOrchestrator

    @staticmethod
    def _build_engine_kwargs(plan: DebugRunPlan) -> dict[str, Any]:
        """Inject no-op engines for bridge and out-of-range steps.

        Three regions for injectable steps:

        * **Before effective_start** (index < ``effective_start_index``):
          skipped by the orchestrator via ``resume_from``; pass ``None`` so the
          orchestrator builds the real engine (it will never call it).
        * **Effective_start ≤ index ≤ end (non-bridge)**: real engines (``None``).
        * **Bridge** (``effective_start_index`` < index < ``start_index``):
          :class:`_NoOpStepEngine` — universe runs but data steps are skipped.
        * **After end** (index > ``end_index``): :class:`_NoOpStepEngine`.
        """
        kwargs: dict[str, Any] = {}
        for step, kwarg in _STEP_ENGINE_KWARG.items():
            if plan._is_noop(step):
                kwargs[kwarg] = _NoOpStepEngine(step)
            else:
                kwargs[kwarg] = None
        return kwargs

    # ------------------------------------------------------------------ #
    # Result assembly.
    # ------------------------------------------------------------------ #
    @classmethod
    def _debug_metadata(cls, plan: DebugRunPlan) -> dict[str, Any]:
        return {
            "preset": plan.preset_name,
            "db_role": plan.db_role,
            "run_type": plan.run_type,
            "force_rerun": plan.force_rerun,
            "sample_count": None if plan.watchlist else plan.sample_count,
            "watchlist": list(plan.watchlist) if plan.watchlist else None,
            "start_step": plan.start_step,
            "effective_start_step": plan.effective_start_step,
            "end_step": plan.end_step,
            "strategy_names": list(plan.strategy_names),
            "trading_days": plan.trading_days,
            "executed_steps": plan.executed_steps(),
            "noop_steps": plan.noop_steps(),
        }

    @classmethod
    def _augment(cls, result: Any, plan: DebugRunPlan) -> ServiceResult:
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["debug"] = cls._debug_metadata(plan)
        return ServiceResult(
            status=getattr(result, "status", service_result.STATUS_FAILED),
            run_id=getattr(result, "run_id", "debug"),
            rows_processed=getattr(result, "rows_processed", 0),
            warnings=list(getattr(result, "warnings", []) or []),
            errors=list(getattr(result, "errors", []) or []),
            metadata=metadata,
        )

    @classmethod
    def _failed(
        cls,
        run_id: str | None,
        error: str,
        plan: DebugRunPlan | None = None,
    ) -> ServiceResult:
        metadata: dict[str, Any] = {"db_role": DB_ROLE_DEBUG}
        if plan is not None:
            metadata["debug"] = cls._debug_metadata(plan)
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=str(run_id) if run_id is not None else "debug",
            rows_processed=0,
            warnings=[],
            errors=[error],
            metadata=metadata,
        )
