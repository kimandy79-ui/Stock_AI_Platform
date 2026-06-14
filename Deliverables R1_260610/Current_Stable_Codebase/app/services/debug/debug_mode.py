"""Module 22 — Debug Mode.

Fast, local debug/testing mode for *partial*, *sampled* pipeline runs. Module 22
is a thin **control plane** on top of the frozen Module 20 pipeline orchestrator;
it performs no market-data, screening, proposal, outcome, simulation, or dashboard
logic of its own.

Guarantees
----------
* **Write isolation.** ``db_role="debug"`` / ``run_type="debug"`` always forwarded;
  ``prod`` and ``simulation`` roles rejected before any orchestrator is constructed.
* **Real ticker scoping for partial runs.**

  * **Feature start** (``indicator_validation``):
    :class:`_ScopedFeatureEngine` calls
    ``FeatureEngine.calculate(tickers=selected_tickers, ...)`` — ``daily_prices``
    is only read for sampled tickers.  ``FeatureEngine.calculate`` already accepts
    a ``tickers`` keyword so Module 11 is not modified.

  * **Step3 start** (``config_tuning_test``):
    :class:`_ScopedStep3ScreeningProxy` wraps a *dynamically-created subclass*
    of ``Step3ScreeningEngine`` that overrides the private ``_read()`` method to
    apply a polars ticker filter before the parent's ``screen()`` evaluates and
    writes candidates.  The **public** ``screen()`` signature of
    ``Step3ScreeningEngine`` is **completely unchanged** — Module 13 is frozen.

* **Rerunnability.** ``force_rerun=True`` forwarded by default.
* **Bounded scope.** ``sample_count`` / unique watchlist size hard-capped at
  :data:`MAX_DEBUG_SAMPLE` (500).
* **Determinism.** Stable sort + head; same inputs → same ticker list.

Lazy imports
------------
Heavy dependencies (orchestrator, YahooProvider, FeatureEngine,
Step3ScreeningEngine, polars) are imported only inside factory callables, never
at module import time.  Tests inject lightweight fakes so no ``duckdb`` /
``polars`` import occurs offline.

Contract source of truth: ``M22_DEBUG_MODE_SPEC.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Final, Mapping, Sequence

from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# Constants.
# --------------------------------------------------------------------------- #
DB_ROLE_DEBUG: Final[str] = "debug"
RUN_TYPE_DEBUG: Final[str] = "debug"
FORBIDDEN_DB_ROLES: Final[tuple[str, ...]] = ("prod", "simulation")
DEFAULT_DEBUG_SAMPLE: Final[int] = 20
MAX_DEBUG_SAMPLE: Final[int] = 500

# Local copy of orchestrator step names (avoids importing orchestrator → duckdb).
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
    "dashboard_materialization", # 11  internal, not injectable
    "backup",                    # 12  internal, not injectable
)

_UNIVERSE_STEP_IDX: Final[int] = STEP_NAMES.index("universe_ingestion")

# Injectable step → PipelineOrchestrator.__init__ kwarg.
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

_STEP_FEATURES:  Final[str] = "feature_calculation"
_STEP_SCREEN:    Final[str] = "step3_screening"
_STEP_PRICE:     Final[str] = "price_ingestion"
_STEP_PROPOSALS: Final[str] = "step5_proposals"
_STEP_FIRST:     Final[str] = STEP_NAMES[0]
_STEP_LAST:      Final[str] = STEP_NAMES[-1]


# --------------------------------------------------------------------------- #
# Scope-detection helpers (used by plan, controller, and tests).
# --------------------------------------------------------------------------- #
def _needs_feature_scope(plan: "DebugRunPlan") -> bool:
    """``True`` when feature_calculation is in range but price_ingestion was
    skipped (bridge no-op).

    Without scoping, ``FeatureEngine.calculate(tickers=None)`` reads ALL tickers
    from ``daily_prices``.
    """
    return (
        not plan._is_noop(_STEP_FEATURES)
        and plan._is_noop(_STEP_PRICE)
    )


def _needs_step3_scope(plan: "DebugRunPlan") -> bool:
    """``True`` when step3_screening is in range but feature_calculation was
    skipped (bridge no-op).

    Without scoping, ``Step3ScreeningEngine.screen()`` reads ALL rows from
    ``daily_features_current``.
    """
    return (
        not plan._is_noop(_STEP_SCREEN)
        and plan._is_noop(_STEP_FEATURES)
    )


# --------------------------------------------------------------------------- #
# Presets.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DebugPreset:
    """Immutable debug-run preset (01e §92)."""

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
# Run plan.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DebugRunPlan:
    """Fully-resolved, validated description of one debug run.

    ``db_role`` / ``run_type`` fixed to ``"debug"``.  ``force_rerun`` defaults
    to ``True`` so the same ``run_date`` can be debugged repeatedly.
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

    @property
    def start_index(self) -> int:
        return STEP_NAMES.index(self.start_step)

    @property
    def end_index(self) -> int:
        return STEP_NAMES.index(self.end_step)

    @property
    def effective_start_step(self) -> str:
        """When ``start_step > universe_ingestion``, lower to ``universe_ingestion``
        so the debug universe snapshot is updated to the sampled set before
        downstream steps run.  Bridge steps fill the gap with no-ops."""
        if self.start_index > _UNIVERSE_STEP_IDX:
            return STEP_NAMES[_UNIVERSE_STEP_IDX]
        return self.start_step

    @property
    def effective_start_index(self) -> int:
        return STEP_NAMES.index(self.effective_start_step)

    @property
    def resume_from(self) -> str | None:
        eff_idx = self.effective_start_index
        return None if eff_idx == 0 else self.effective_start_step

    def _is_noop(self, step: str) -> bool:
        """``True`` when ``step`` receives a ``_NoOpStepEngine``."""
        s_idx = STEP_NAMES.index(step)
        if s_idx > self.end_index:
            return True
        eff_idx = self.effective_start_index
        if eff_idx < s_idx < self.start_index:
            return True
        return False

    def executed_steps(self) -> list[str]:
        eff_idx = self.effective_start_index
        return [
            s for s in _STEP_ENGINE_KWARG
            if STEP_NAMES.index(s) >= eff_idx and not self._is_noop(s)
        ]

    def noop_steps(self) -> list[str]:
        eff_idx = self.effective_start_index
        return [
            s for s in _STEP_ENGINE_KWARG
            if STEP_NAMES.index(s) >= eff_idx and self._is_noop(s)
        ]


# --------------------------------------------------------------------------- #
# SamplingProvider.
# --------------------------------------------------------------------------- #
class SamplingProvider:
    """Provider decorator that limits ``list_symbols`` to the sampled set.

    Handles plain strings, dicts with a ``"symbol"``/``"ticker"`` key, and
    objects with a ``.ticker`` attribute (``TickerInfo``).  All other provider
    methods delegate unchanged.
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
            return sorted(
                (s for s in symbols if self._symbol_key(s) in wanted),
                key=self._symbol_key,
            )
        ordered = sorted(symbols, key=self._symbol_key)
        if self._sample_count is None:
            return ordered
        return ordered[: self._sample_count]

    @staticmethod
    def _symbol_key(entry: Any) -> str:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            for k in ("symbol", "ticker", "Symbol", "Ticker"):
                if k in entry and entry[k] is not None:
                    return str(entry[k])
        for attr in ("ticker", "symbol", "Symbol", "Ticker"):
            val = getattr(entry, attr, None)
            if val is not None:
                return str(val)
        return str(entry)

    def get_capabilities(self) -> ServiceResult:
        return self._provider.get_capabilities()

    def get_price_history(self, request: Any) -> ServiceResult:
        return self._provider.get_price_history(request)

    def get_earnings(self, ticker: str) -> ServiceResult:
        return self._provider.get_earnings(ticker)


# --------------------------------------------------------------------------- #
# Engine wrappers.
# --------------------------------------------------------------------------- #
class _NoOpStepEngine:
    """Returns an immediate success for any method call (bridge / out-of-range)."""

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


class _ScopedFeatureEngine:
    """Wraps a real FeatureEngine and always supplies ``selected_tickers`` to
    ``calculate()``.

    ``FeatureEngine.calculate`` already accepts a ``tickers`` keyword; Module 11
    is not modified.  ``selected_tickers`` is exposed for test assertion.
    """

    def __init__(self, real_engine: Any, selected_tickers: list[str]) -> None:
        self._real = real_engine
        self.selected_tickers: list[str] = list(selected_tickers)

    def calculate(
        self,
        start_date: date,
        end_date: date,
        tickers: list[str] | None = None,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        return self._real.calculate(
            start_date=start_date,
            end_date=end_date,
            tickers=self.selected_tickers,  # always override with scoped list
            db_role=db_role,
            run_id=run_id,
        )


class _ScopedStep3ScreeningProxy:
    """Proxy for Step3 scoping that keeps the public ``screen()`` API frozen.

    The ``screen()`` method delegates to ``self._real.screen()`` with the
    **identical signature** the orchestrator uses — no extra arguments.  Ticker
    filtering is not done here; it is applied inside ``self._real`` at the
    private ``_read()`` level (see :func:`_make_filtered_step3_engine`).

    In production, ``self._real`` is an instance of a dynamic subclass created
    by the default factory (``_resolve_scoped_step3_factory``).  In offline
    tests, the injected fake factory returns any object with ``selected_tickers``
    and ``screen()``.  ``selected_tickers`` is exposed for test assertion.
    """

    def __init__(self, real_engine: Any, selected_tickers: list[str]) -> None:
        self._real = real_engine
        self.selected_tickers: list[str] = list(selected_tickers)

    def screen(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str,
        run_id: str | None = None,
    ) -> ServiceResult:
        # Call the real engine with the standard Step3 signature — no tickers kwarg.
        # Filtering is done inside self._real._read() via the dynamic subclass.
        return self._real.screen(
            signal_date=signal_date,
            strategy_config=strategy_config,
            strategy_config_id=strategy_config_id,
            db_role=db_role,
            run_id=run_id,
        )


# --------------------------------------------------------------------------- #
# Controller.
# --------------------------------------------------------------------------- #
class DebugModeController:
    """Drive fast, sampled, partial pipeline runs against ``debug.duckdb`` only.

    All dependencies are injectable.  Real defaults (orchestrator, YahooProvider,
    FeatureEngine, Step3ScreeningEngine, polars) are imported only inside lazy
    factory callables — never at module import time — so offline tests that inject
    fakes never trigger those imports.
    """

    def __init__(
        self,
        db_manager: Any | None = None,
        provider: Any | None = None,
        orchestrator_factory: Callable[..., Any] | None = None,
        strategy_configs: Mapping[str, dict] | None = None,
        scoped_feature_engine_factory: Callable[..., Any] | None = None,
        scoped_screening_engine_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._db_manager = db_manager
        self._provider = provider
        self._orchestrator_factory = orchestrator_factory
        self._strategy_configs = strategy_configs
        self._scoped_feat_factory = scoped_feature_engine_factory
        self._scoped_step3_factory = scoped_screening_engine_factory

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
        """Resolve a named preset into a plan and execute it."""
        preset = DEBUG_PRESETS.get(preset_name)
        if preset is None:
            return self._failed(
                run_id,
                f"unknown debug preset {preset_name!r}; "
                f"valid: {sorted(DEBUG_PRESETS)}",
            )
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
        """Validate the plan, resolve selected tickers, build engines, and run."""
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

        # Resolve selected tickers before orchestrator construction when a partial
        # run bypasses price ingestion (feature scope) or feature calculation (Step3
        # scope).  The provider is called once here; the orchestrator will call it
        # again during universe_ingestion — acceptable for a debug tool.
        selected_tickers: list[str] | None = None
        if _needs_feature_scope(plan) or _needs_step3_scope(plan):
            t_result = sampling_provider.list_symbols()
            if t_result.status == service_result.STATUS_FAILED:
                err = t_result.errors[0] if t_result.errors else "provider failed"
                log.error("ticker resolution failed: %s", err)
                return self._failed(
                    run_id, f"ticker resolution failed: {err}", plan=plan
                )
            selected_tickers = [
                SamplingProvider._symbol_key(s)
                for s in t_result.metadata.get("symbols", [])
            ]
            log.info(
                "debug ticker scope resolved: %d tickers for partial run",
                len(selected_tickers),
            )

        engine_kwargs = self._build_engine_kwargs(
            plan,
            selected_tickers,
            self._db_manager,
            self._resolve_scoped_feat_factory(),
            self._resolve_scoped_step3_factory(),
        )
        factory = self._resolve_orchestrator_factory()
        orchestrator = factory(
            db_manager=self._db_manager,
            provider=sampling_provider,
            **engine_kwargs,
        )

        log.info(
            "debug run start preset=%s sample=%s steps=%s..%s "
            "(eff=%s) strategies=%s force_rerun=%s scoped=%s",
            plan.preset_name,
            "watchlist" if plan.watchlist else plan.sample_count,
            plan.start_step,
            plan.end_step,
            plan.effective_start_step,
            ",".join(plan.strategy_names),
            plan.force_rerun,
            len(selected_tickers) if selected_tickers is not None else "n/a",
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
        return self._augment(result, plan, selected_tickers)

    # ------------------------------------------------------------------ #
    # Validation.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_plan(plan: DebugRunPlan) -> str | None:
        if plan.db_role in FORBIDDEN_DB_ROLES:
            return (
                f"debug mode must not target db_role {plan.db_role!r}; "
                f"debug runs write only to {DB_ROLE_DEBUG!r}"
            )
        if plan.db_role != DB_ROLE_DEBUG:
            return f"invalid debug db_role {plan.db_role!r}; must be {DB_ROLE_DEBUG!r}"
        if plan.run_type != RUN_TYPE_DEBUG:
            return f"invalid debug run_type {plan.run_type!r}; must be {RUN_TYPE_DEBUG!r}"
        if plan.start_step not in STEP_NAMES:
            return f"invalid start_step {plan.start_step!r}"
        if plan.end_step not in STEP_NAMES:
            return f"invalid end_step {plan.end_step!r}"
        if plan.start_index > plan.end_index:
            return f"start_step {plan.start_step!r} is after end_step {plan.end_step!r}"
        if not plan.strategy_names:
            return "strategy_names must be non-empty"
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
    # Engine construction.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_engine_kwargs(
        plan: DebugRunPlan,
        selected_tickers: list[str] | None,
        db_manager: Any,
        scoped_feat_factory: Callable[..., Any],
        scoped_step3_factory: Callable[..., Any],
    ) -> dict[str, Any]:
        """Build the orchestrator engine kwargs dict.

        Steps fall into four categories:

        * **No-op** (bridge or after end): :class:`_NoOpStepEngine`.
        * **Scoped feature**: :class:`_ScopedFeatureEngine` when needed.
        * **Scoped Step3**: :class:`_ScopedStep3ScreeningProxy` when needed.
        * **Real** (all other in-range steps): ``None`` — the orchestrator builds
          the real engine via its own lazy imports.
        """
        do_feat_scope  = _needs_feature_scope(plan)  and selected_tickers is not None
        do_step3_scope = _needs_step3_scope(plan)    and selected_tickers is not None

        kwargs: dict[str, Any] = {}
        for step, kwarg in _STEP_ENGINE_KWARG.items():
            if plan._is_noop(step):
                kwargs[kwarg] = _NoOpStepEngine(step)
            elif step == _STEP_FEATURES and do_feat_scope:
                kwargs[kwarg] = scoped_feat_factory(db_manager, selected_tickers)
            elif step == _STEP_SCREEN and do_step3_scope:
                kwargs[kwarg] = scoped_step3_factory(db_manager, selected_tickers)
            else:
                kwargs[kwarg] = None
        return kwargs

    # ------------------------------------------------------------------ #
    # Lazy dependency resolution.
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
                return f"unknown strategy config {name!r}; valid: {sorted(source)}"
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

    def _resolve_scoped_feat_factory(self) -> Callable[..., Any]:
        if self._scoped_feat_factory is not None:
            return self._scoped_feat_factory

        def _default(db_manager: Any, tickers: list[str]) -> _ScopedFeatureEngine:
            from app.services.features.feature_engine import FeatureEngine
            return _ScopedFeatureEngine(FeatureEngine(db_manager=db_manager), tickers)

        return _default

    def _resolve_scoped_step3_factory(self) -> Callable[..., Any]:
        if self._scoped_step3_factory is not None:
            return self._scoped_step3_factory

        def _default(
            db_manager: Any, tickers: list[str]
        ) -> _ScopedStep3ScreeningProxy:
            """Build a Step3 subclass that overrides the private ``_read()`` to
            filter ``daily_features_current`` to the sampled tickers before the
            inherited ``screen()`` evaluates and writes candidates.

            The **public** ``screen()`` signature of ``Step3ScreeningEngine`` is
            completely unchanged — Module 13 is frozen.  Filtering is applied
            purely at the internal data-frame level.
            """
            from app.services.screening.step3_screening import Step3ScreeningEngine
            import polars as pl

            tickers_frozen = frozenset(tickers)

            class _FilteredStep3(Step3ScreeningEngine):
                """Override ``_read()`` only; everything else is inherited."""

                def __init__(self_inner) -> None:  # noqa: N805
                    super().__init__(db_manager=db_manager)

                def _read(  # noqa: N805
                    self_inner, db_role: str, signal_date: date
                ) -> Any:
                    frame = super()._read(db_role, signal_date)
                    return frame.filter(
                        pl.col("ticker").is_in(tickers_frozen)
                    )

            return _ScopedStep3ScreeningProxy(_FilteredStep3(), tickers)

        return _default

    # ------------------------------------------------------------------ #
    # Result assembly.
    # ------------------------------------------------------------------ #
    @classmethod
    def _debug_metadata(
        cls, plan: DebugRunPlan, selected_tickers: list[str] | None
    ) -> dict[str, Any]:
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
            "selected_tickers": selected_tickers,
            "needs_feature_scope": _needs_feature_scope(plan),
            "needs_step3_scope": _needs_step3_scope(plan),
        }

    @classmethod
    def _augment(
        cls,
        result: Any,
        plan: DebugRunPlan,
        selected_tickers: list[str] | None = None,
    ) -> ServiceResult:
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["debug"] = cls._debug_metadata(plan, selected_tickers)
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
        selected_tickers: list[str] | None = None,
    ) -> ServiceResult:
        metadata: dict[str, Any] = {"db_role": DB_ROLE_DEBUG}
        if plan is not None:
            metadata["debug"] = cls._debug_metadata(plan, selected_tickers)
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=str(run_id) if run_id is not None else "debug",
            rows_processed=0,
            warnings=[],
            errors=[error],
            metadata=metadata,
        )
