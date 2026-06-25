"""Module 22 — Debug Mode (setup-mode).

Fast, local debug/testing mode for *partial*, *sampled* pipeline runs. Module 22
is a thin **control plane** on top of the frozen Module 20 pipeline orchestrator;
it performs no market-data, screening, proposal, outcome, simulation, or dashboard
logic of its own.

Guarantees
----------
* **Write isolation.** ``db_role="debug"`` / ``run_type="debug"`` always forwarded;
  ``prod`` and ``simulation`` roles rejected before any orchestrator is constructed.
* **Setup-mode config loading.** Active setup configs are loaded from
  ``debug.duckdb`` via ``ConfigService.get_all_active_setup_configs("debug")``.
  The caller selects which of the four canonical setup types
  (``breakout``, ``pullback``, ``trend_continuation``, ``consolidation_base``)
  to activate for this debug run.
* **Real ticker scoping for partial runs.**

  * **Feature start** (``indicator_validation``):
    :class:`_ScopedFeatureEngine` calls
    ``FeatureEngine.calculate(tickers=selected_tickers, ...)``.

  * **Step3 start** (``config_tuning_test``):
    :class:`_ScopedStep3UniversalProxy` wraps the real
    ``Step3UniversalEligibilityEngine`` so that only sampled tickers are
    evaluated.

* **Rerunnability.** ``force_rerun=True`` forwarded by default.
* **Bounded scope.** ``sample_count`` / unique watchlist size hard-capped at
  :data:`MAX_DEBUG_SAMPLE` (500).
* **Determinism.** Stable sort + head; same inputs → same ticker list.

Lazy imports
------------
Heavy dependencies (orchestrator, YahooProvider, FeatureEngine, polars) are
imported only inside factory callables, never at module import time.  Tests
inject lightweight fakes so no ``duckdb`` / ``polars`` import occurs offline.

Contract source of truth: ``M22_DEBUG_MODE_SPEC.md``, ``02b_ARCHITECTURE_DECISIONS.md``
§22.19–22.24.
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

# Canonical active setup types (AD-22.20).
CANONICAL_SETUP_TYPES: Final[tuple[str, ...]] = (
    "breakout",
    "pullback",
    "trend_continuation",
    "consolidation_base",
)

# Local copy of orchestrator step names (avoids importing orchestrator → duckdb).
STEP_NAMES: Final[tuple[str, ...]] = (
    "benchmark_etf_ingestion",       # 0
    "universe_ingestion",            # 1
    "price_ingestion",               # 2
    "validation",                    # 3
    "mutation_detection",            # 4
    "feature_calculation",           # 5
    "step3_universal_eligibility",   # 6  — M13 setup-mode
    "step4_setup_validation",        # 7  — M14 setup-mode
    "step5_proposals",               # 8  — M15 setup-mode
    "outcome_queue_creation",        # 9
    "outcome_processing",            # 10
    "dashboard_materialization",     # 11  internal, not injectable
    "backup",                        # 12  internal, not injectable
)

_UNIVERSE_STEP_IDX: Final[int] = STEP_NAMES.index("universe_ingestion")

# Injectable step → PipelineOrchestrator.__init__ kwarg.
_STEP_ENGINE_KWARG: Final[Mapping[str, str]] = {
    "benchmark_etf_ingestion":     "benchmark_loader",
    "universe_ingestion":          "universe_engine",
    "price_ingestion":             "ingestion_engine",
    "validation":                  "validation_engine",
    "mutation_detection":          "mutation_engine",
    "feature_calculation":         "feature_engine",
    "step3_universal_eligibility": "eligibility_engine",   # M13 setup-mode
    "step4_setup_validation":      "setup_validation_engine",  # M14 setup-mode
    "step5_proposals":             "proposal_engine",
    "outcome_queue_creation":      "outcome_creator",
    "outcome_processing":          "outcome_processor",
}

_STEP_FEATURES:  Final[str] = "feature_calculation"
_STEP_SCREEN:    Final[str] = "step3_universal_eligibility"
_STEP_PRICE:     Final[str] = "price_ingestion"
_STEP_PROPOSALS: Final[str] = "step5_proposals"
_STEP_FIRST:     Final[str] = STEP_NAMES[0]
_STEP_LAST:      Final[str] = STEP_NAMES[-1]


# --------------------------------------------------------------------------- #
# Scope-detection helpers.
# --------------------------------------------------------------------------- #
def _needs_feature_scope(plan: "DebugRunPlan") -> bool:
    return (
        not plan._is_noop(_STEP_FEATURES)
        and plan._is_noop(_STEP_PRICE)
    )


def _needs_step3_scope(plan: "DebugRunPlan") -> bool:
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
    setup_types: tuple[str, ...]   # canonical setup types to activate
    description: str


DEBUG_PRESETS: Final[Mapping[str, DebugPreset]] = {
    "fast_smoke_test": DebugPreset(
        name="fast_smoke_test",
        sample_count=20,
        trading_days=5,
        start_step=_STEP_FIRST,
        end_step=_STEP_LAST,
        setup_types=("breakout", "pullback", "trend_continuation", "consolidation_base"),
        description="20 tickers, 5 trading days, full pipeline.",
    ),
    "indicator_validation": DebugPreset(
        name="indicator_validation",
        sample_count=10,
        trading_days=90,
        start_step=_STEP_FEATURES,
        end_step=_STEP_FEATURES,
        setup_types=("breakout", "pullback", "trend_continuation", "consolidation_base"),
        description="10 tickers, 90 trading days, indicators (Step2) only.",
    ),
    "pipeline_sanity": DebugPreset(
        name="pipeline_sanity",
        sample_count=100,
        trading_days=30,
        start_step=_STEP_FIRST,
        end_step=_STEP_PROPOSALS,
        setup_types=("breakout", "pullback", "trend_continuation", "consolidation_base"),
        description="100 tickers, 30 trading days, Step1-Step5.",
    ),
    "config_tuning_test": DebugPreset(
        name="config_tuning_test",
        sample_count=500,
        trading_days=126,
        start_step=_STEP_SCREEN,
        end_step=_STEP_PROPOSALS,
        setup_types=("breakout", "pullback", "trend_continuation", "consolidation_base"),
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

    ``setup_types`` selects which canonical setup configs to activate for this
    run (subset of ``CANONICAL_SETUP_TYPES``).  The orchestrator loads all active
    setup configs from ``debug.duckdb``; the debug metadata records which types
    were requested by the caller.
    """

    run_date: date
    sample_count: int
    start_step: str
    end_step: str
    setup_types: tuple[str, ...]     # canonical setup types requested
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
    """Provider decorator that limits ``list_symbols`` to the sampled set."""

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
    """Wraps a real FeatureEngine and always supplies ``selected_tickers``."""

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
            tickers=self.selected_tickers,
            db_role=db_role,
            run_id=run_id,
        )


class _ScopedStep3UniversalProxy:
    """Proxy for Step3 scoping.

    Delegates to the real ``Step3UniversalEligibilityEngine`` with the standard
    setup-mode ``run()`` API signature.  Ticker filtering is applied inside the
    wrapped engine at the data-read level (``_make_filtered_step3_engine``).
    ``selected_tickers`` is exposed for test assertion.
    """

    def __init__(self, real_engine: Any, selected_tickers: list[str]) -> None:
        self._real = real_engine
        self.selected_tickers: list[str] = list(selected_tickers)

    def run(
        self,
        signal_date: date,
        setup_config_id: str,
        setup_config: dict,
        db_role: str,
        run_id: str | None = None,
    ) -> ServiceResult:
        return self._real.run(
            signal_date=signal_date,
            setup_config_id=setup_config_id,
            setup_config=setup_config,
            db_role=db_role,
            run_id=run_id,
        )


# --------------------------------------------------------------------------- #
# Controller.
# --------------------------------------------------------------------------- #
class DebugModeController:
    """Drive fast, sampled, partial pipeline runs against ``debug.duckdb`` only.

    All dependencies are injectable.  Real defaults (orchestrator, YahooProvider,
    FeatureEngine, polars) are imported only inside lazy factory callables.

    ``setup_configs`` (optional injection): ``{setup_type: config_dict}`` mapping.
    When ``None`` (default), active configs are loaded from ``debug.duckdb`` via
    ``ConfigService.get_all_active_setup_configs("debug")``.  The orchestrator
    itself always self-loads from the DB regardless.
    """

    def __init__(
        self,
        db_manager: Any | None = None,
        provider: Any | None = None,
        orchestrator_factory: Callable[..., Any] | None = None,
        setup_configs: Mapping[str, dict] | None = None,
        scoped_feature_engine_factory: Callable[..., Any] | None = None,
        scoped_screening_engine_factory: Callable[..., Any] | None = None,
        config_service: Any | None = None,
    ) -> None:
        self._db_manager = db_manager
        self._provider = provider
        self._orchestrator_factory = orchestrator_factory
        self._setup_configs = setup_configs        # {setup_type: config_dict} or None
        self._scoped_feat_factory = scoped_feature_engine_factory
        self._scoped_step3_factory = scoped_screening_engine_factory
        self._config_service = config_service

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
        setup_types: Sequence[str] | None = None,
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
            resolved_watchlist = tuple(dict.fromkeys(watchlist))
        plan = DebugRunPlan(
            run_date=run_date,
            sample_count=(
                sample_count if sample_count is not None else preset.sample_count
            ),
            start_step=preset.start_step,
            end_step=preset.end_step,
            setup_types=(
                tuple(setup_types)
                if setup_types is not None
                else preset.setup_types
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

        # Verify requested setup types exist as active configs in debug DB.
        # This is informational — the orchestrator self-loads from DB anyway.
        config_check = self._resolve_setup_configs(plan.setup_types)
        if isinstance(config_check, str):
            log.error("debug-mode setup config check failed: %s", config_check)
            return self._failed(run_id, config_check, plan=plan)

        base_provider = self._resolve_base_provider()
        sampling_provider = SamplingProvider(
            base_provider,
            sample_count=None if plan.watchlist else plan.sample_count,
            watchlist=plan.watchlist,
        )

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
            "(eff=%s) setup_types=%s force_rerun=%s scoped=%s",
            plan.preset_name,
            "watchlist" if plan.watchlist else plan.sample_count,
            plan.start_step,
            plan.end_step,
            plan.effective_start_step,
            ",".join(plan.setup_types),
            plan.force_rerun,
            len(selected_tickers) if selected_tickers is not None else "n/a",
        )

        # The orchestrator.run() has no setup_configs param — it self-loads
        # from debug.duckdb via ConfigService.get_all_active_setup_configs().
        result = orchestrator.run(
            run_date=plan.run_date,
            run_type=RUN_TYPE_DEBUG,
            db_role=DB_ROLE_DEBUG,
            resume_from=plan.resume_from,
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
        if not plan.setup_types:
            return "setup_types must be non-empty"
        invalid = [s for s in plan.setup_types if s not in CANONICAL_SETUP_TYPES]
        if invalid:
            return (
                f"invalid setup_types {invalid}; "
                f"must be a subset of {list(CANONICAL_SETUP_TYPES)}"
            )
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
        """Build the orchestrator engine kwargs dict."""
        do_feat_scope  = _needs_feature_scope(plan) and selected_tickers is not None
        do_step3_scope = _needs_step3_scope(plan)   and selected_tickers is not None

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
    # Setup config resolution.
    # ------------------------------------------------------------------ #
    def _resolve_setup_configs(
        self, setup_types: tuple[str, ...]
    ) -> dict[str, dict] | str:
        """Return ``{setup_type: config_dict}`` for the requested setup types.

        Uses the injected ``setup_configs`` mapping when provided (tests /
        manual overrides); otherwise loads all active configs from
        ``debug.duckdb`` via ``ConfigService.get_all_active_setup_configs``.
        The orchestrator always self-loads from the DB regardless.
        """
        source = self._setup_configs
        if source is not None:
            # Explicit override (tests / manual).
            selected: dict[str, dict] = {}
            for st in setup_types:
                if st not in source:
                    return (
                        f"unknown setup_type {st!r}; "
                        f"valid: {sorted(source)}"
                    )
                selected[st] = source[st]
            return selected

        loaded = self._load_active_debug_setup_configs()
        if isinstance(loaded, str):
            return loaded
        # loaded = {setup_type: config_dict}
        configs_by_type = loaded
        selected = {}
        for st in setup_types:
            if st not in configs_by_type:
                return (
                    f"no active debug setup config for setup_type={st!r}; "
                    f"available: {sorted(configs_by_type)}. "
                    f"Ensure debug.duckdb is seeded with setup_breakout_v1 / "
                    f"setup_pullback_v1 / setup_trend_continuation_v1 / "
                    f"setup_consolidation_base_v1."
                )
            selected[st] = configs_by_type[st]
        return selected

    def _load_active_debug_setup_configs(
        self,
    ) -> dict[str, dict] | str:
        """Load active setup configs from debug.duckdb; seed if missing.

        Returns ``{setup_type: config_dict}`` or an error string.
        Calls ``ConfigService.get_all_active_setup_configs("debug")``.
        """
        service = self._config_service
        if service is None:
            from app.services.config.config_service import ConfigService
            service = ConfigService(db_manager=self._db_manager)

        result = service.get_all_active_setup_configs("debug")
        if not result.is_ok():
            return "; ".join(result.errors) or "failed to load debug setup configs"

        configs_by_type: dict[str, dict] = dict(
            result.metadata.get("configs_by_type") or {}
        )

        if not configs_by_type:
            # Attempt to seed defaults.
            seed = service.seed_default_setup_configs("debug")
            if not seed.is_ok():
                return "; ".join(seed.errors) or "failed to seed debug setup configs"
            result = service.get_all_active_setup_configs("debug")
            if not result.is_ok():
                return "; ".join(result.errors) or "failed to reload debug setup configs"
            configs_by_type = dict(result.metadata.get("configs_by_type") or {})

        if not configs_by_type:
            return "no active setup configs available in debug.duckdb after seeding"

        return configs_by_type

    # ------------------------------------------------------------------ #
    # Lazy dependency resolution.
    # ------------------------------------------------------------------ #
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
        ) -> _ScopedStep3UniversalProxy:
            """Wrap Step3UniversalEligibilityEngine (M13) to scope to sampled tickers."""
            from app.services.screening.step3_universal_eligibility import (
                Step3UniversalEligibilityEngine,
            )
            import polars as pl

            tickers_frozen = frozenset(tickers)

            class _FilteredStep3(Step3UniversalEligibilityEngine):
                """Override ``_read_features()`` only; everything else inherited."""

                def __init__(self_inner) -> None:  # noqa: N805
                    super().__init__(db_manager=db_manager)

                def _read_features(  # noqa: N805
                    self_inner, db_role: str, signal_date: date
                ) -> Any:
                    frame = super()._read_features(db_role, signal_date)
                    return frame.filter(pl.col("ticker").is_in(tickers_frozen))

            return _ScopedStep3UniversalProxy(_FilteredStep3(), tickers)

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
            "setup_types": list(plan.setup_types),
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
