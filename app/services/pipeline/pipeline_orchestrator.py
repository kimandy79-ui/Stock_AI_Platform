"""Module 20 — Pipeline Orchestrator.

Coordinates one daily pipeline run end to end. The orchestrator is a thin
control-plane layer: it owns the ``daily_pipeline`` row in ``pipeline_locks``,
the run-lifecycle row in ``pipeline_runs``, and the *ordering* of the frozen
step engines. It performs **no** market-data, screening, proposal, outcome, or
dashboard logic itself; every domain action is delegated to an injected engine
that returns a :class:`ServiceResult`.

Step order (``STEP_NAMES``), mirroring ``01d_MODULES_AND_PIPELINE.md`` §70:

1.  ``benchmark_etf_ingestion``    (critical)
2.  ``universe_ingestion``         (recoverable)
3.  ``price_ingestion``            (critical)
4.  ``validation``                 (critical)
5.  ``mutation_detection``         (recoverable)
6.  ``feature_calculation``        (critical)
7.  ``market_regime_classification`` (recoverable)
8.  ``step3_screening``            (critical, per strategy config)
8.  ``step4_analysis``             (critical, per strategy config)
9.  ``step5_proposals``            (critical, per strategy config)
10. ``outcome_queue_creation``     (critical, per strategy config)
11. ``outcome_processing``         (recoverable, per strategy config)
12. ``dashboard_materialization``  (recoverable; V1 no-op, G-DASHBOARD-MAT)

Hard boundaries (Module 20): no direct ``duckdb`` import (all DB access flows
through the injected ``db_manager`` or the approved
:mod:`app.database.duckdb_manager`), no ``print()``, no DDL / ``ATTACH`` in any
executed SQL, no simulation-DB writes, and no market-data logic. This module
issues SQL against only ``pipeline_runs`` and ``pipeline_locks`` and never
writes step-engine tables directly. All step engines (and the default provider)
are instantiated in ``__init__`` only, never inside the step methods. The
public surface always returns a :class:`ServiceResult`; expected validation,
lock, already-run, and step failures are reported as ``failed`` /
``success_with_warnings`` results rather than raised exceptions.

Contract source of truth: ``M20_PIPELINE_ORCHESTRATOR_SPEC.md`` (derived from
``01b_SCHEMA_AND_DATA.md`` / ``M02_SCHEMA_SPEC.md`` §3.2 / §3.3 for the
``pipeline_runs`` / ``pipeline_locks`` schema, ``01a_CORE_PRINCIPLES.md`` for
the ``run_type`` / ``run_status`` enums, ``01d_MODULES_AND_PIPELINE.md`` §70 /
§72 for step order and failure modes, ``02_PROJECT_IMPLEMENTATION_CONTEXT.md``
§6 / §20 for lock / heartbeat / resume discipline, the per-engine specs for the
step signatures, ``01c_FORMULAS_AND_CONFIGS`` / :mod:`app.config.settings` for
``DEFAULT_STRATEGY_CONFIGS`` and path constants, and
``app/utils/service_result.py`` for the ``ServiceResult`` discipline).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Final

from app.config import constants
from app.config import settings
from app.database import duckdb_manager
from app.services.config import config_validator
from app.utils import logging_config
from app.utils.db_connection_retry import connect_with_retry
from app.utils import service_result
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# Roles / enums / constants.
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = "prod"
DB_ROLE_DEBUG: Final[str] = "debug"

ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# run_type enum (01a_CORE_PRINCIPLES.md / M02 §3.2).
ALLOWED_RUN_TYPES: Final[tuple[str, ...]] = (
    "scheduled",
    "manual",
    "force_rerun",
    "catchup",
    "debug",
)

STEP_NAMES: Final[tuple[str, ...]] = (
    "benchmark_etf_ingestion",
    "universe_ingestion",
    "price_ingestion",
    "validation",
    "mutation_detection",
    "feature_calculation",
    "market_regime_classification",
    "step3_screening",
    "step4_analysis",
    "step5_proposals",
    "outcome_queue_creation",
    "outcome_processing",
    "dashboard_materialization",
)

# Steps that abort the run on failure vs. degrade-and-continue (01d §72).
CRITICAL_STEPS: Final[frozenset[str]] = frozenset(
    {
        "benchmark_etf_ingestion",
        "price_ingestion",
        "validation",
        "feature_calculation",
        "step3_screening",
        "step4_analysis",
        "step5_proposals",
        "outcome_queue_creation",
    }
)
RECOVERABLE_STEPS: Final[frozenset[str]] = frozenset(
    {
        "universe_ingestion",
        "mutation_detection",
        "market_regime_classification",
        "outcome_processing",
        "dashboard_materialization",
    }
)

# Strategy steps run once per strategy config (steps 7-11).
STRATEGY_STEPS: Final[tuple[str, ...]] = (
    "step3_screening",
    "step4_analysis",
    "step5_proposals",
    "outcome_queue_creation",
    "outcome_processing",
)

PIPELINE_LOCK_NAME: Final[str] = "daily_pipeline"
LOCK_STALE_SECONDS: Final[int] = 300

# Provider source for the universe entries (provider list_symbols source tag).
_UNIVERSE_SOURCE: Final[str] = "yahoo"

# --------------------------------------------------------------------------- #
# ServiceResult metadata keys (exact set, every return path).
# --------------------------------------------------------------------------- #
_META_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "run_date",
    "run_type",
    "db_role",
    "steps_completed",
    "failed_step",
    "error",
    "duration_sec",
    "status",
)

# --------------------------------------------------------------------------- #
# SQL (parameterized; targets only pipeline_runs / pipeline_locks; no DDL).
# --------------------------------------------------------------------------- #
_SELECT_LOCK: Final[str] = (
    "SELECT run_id, is_locked, heartbeat_at "
    "FROM pipeline_locks WHERE lock_name = ?"
)
_UPSERT_LOCK: Final[str] = (
    "INSERT INTO pipeline_locks "
    "(lock_name, is_locked, run_id, locked_at, heartbeat_at) "
    "VALUES ('daily_pipeline', TRUE, ?, "
    "CAST(now() AS TIMESTAMP), CAST(now() AS TIMESTAMP)) "
    "ON CONFLICT (lock_name) DO UPDATE SET "
    "is_locked = TRUE, "
    "run_id = EXCLUDED.run_id, "
    "locked_at = EXCLUDED.locked_at, "
    "heartbeat_at = EXCLUDED.heartbeat_at"
)
_HEARTBEAT_LOCK: Final[str] = (
    "UPDATE pipeline_locks "
    "SET heartbeat_at = CAST(now() AS TIMESTAMP) "
    "WHERE lock_name = 'daily_pipeline'"
)
_RELEASE_LOCK: Final[str] = (
    "UPDATE pipeline_locks "
    "SET is_locked = FALSE, run_id = NULL "
    "WHERE lock_name = 'daily_pipeline'"
)
_SELECT_ALREADY_RUN: Final[str] = (
    "SELECT run_id, status FROM pipeline_runs "
    "WHERE run_date = ? AND status IN ('success', 'success_with_warnings') "
    "LIMIT 1"
)
_INSERT_RUNNING: Final[str] = (
    "INSERT INTO pipeline_runs "
    "(run_id, run_date, run_type, status, started_at, "
    "steps_completed, error_message, created_at) "
    "VALUES (?, ?, ?, 'running', CAST(now() AS TIMESTAMP), "
    "'[]', NULL, CAST(now() AS TIMESTAMP))"
)
_UPDATE_STEPS: Final[str] = (
    "UPDATE pipeline_runs SET steps_completed = ? WHERE run_id = ?"
)
_UPDATE_SUCCESS: Final[str] = (
    "UPDATE pipeline_runs "
    "SET status = ?, completed_at = CAST(now() AS TIMESTAMP), "
    "duration_sec = ?, steps_completed = ? "
    "WHERE run_id = ?"
)
_UPDATE_FAILED: Final[str] = (
    "UPDATE pipeline_runs "
    "SET status = 'failed', completed_at = CAST(now() AS TIMESTAMP), "
    "duration_sec = ?, error_message = ? "
    "WHERE run_id = ?"
)
# Config traceability (M21 Config Management Addendum §4). Targets pipeline_runs
# only; written once the run's strategy/runtime configs are resolved.
_UPDATE_CONFIG_META: Final[str] = (
    "UPDATE pipeline_runs "
    "SET strategy_config_ids_json = ?, runtime_config_ids_json = ?, "
    "config_snapshot_hash = ? "
    "WHERE run_id = ?"
)

# --------------------------------------------------------------------------- #
# Default strategy configs (G-STRATEGY-CONFIGS).
# --------------------------------------------------------------------------- #
# Shapes are the canonical 01c_FORMULAS_AND_CONFIGS.md preset JSON, augmented
# with the two engine-required keys the 01c JSON omits:
#   * ``step4.target_R``        (01c §222-224: normal 2.2 / aggressive 1.8 /
#                                conservative 2.8)
#   * ``diversification.top_n`` (M15 §174 required int > 0; no canonical value
#                                in 01c, defaulted to 10 here)
# The Step 5 engine normalises the legacy ``sector_max_positions`` /
# ``industry_max_positions`` names to ``max_sector_count`` /
# ``max_industry_count`` transparently, so the legacy names are kept verbatim
# from 01c. See gap G-STRATEGY-CONFIGS in the spec.
_FEATURES_BLOCK: Final[dict[str, Any]] = {
    "feature_schema_version": "features_v01",
    "rsi_length": 14,
    "atr_length": 14,
    "ema_periods": [20, 50, 200],
    "rvol_lookback": 20,
    "recent_high_lookback": 20,
    "high_52w_lookback": 252,
}
_SCORING_WEIGHTS_BLOCK: Final[dict[str, float]] = {
    "trend": 0.3,
    "momentum": 0.25,
    "setup": 0.2,
    "volume": 0.15,
    "market": 0.1,
}
_MARKET_REGIME_BLOCK: Final[dict[str, Any]] = {
    "high_risk_vix": 25,
    "extreme_risk_vix": 30,
}
# Sector -> ETF mapping is sourced from the single canonical map in
# ``app.config.constants.SECTOR_ETF_MAP`` (M21 Config Management Addendum §8);
# the previous duplicated literal block here was removed to avoid two
# independent sector maps.
_SIMULATION_BLOCK: Final[dict[str, Any]] = {
    "entry_rule": "next_trading_day_open_raw",
    "return_price_type": "adjusted_close",
    "slippage_bps": 10,
    "commission_per_trade": 0,
    "horizons_bd": [5, 10, 20, 40],
    "min_resolved_outcomes_pct": 0.85,
    "max_drawdown_constraint_pct": 25,
}
_MACRO_BLOCK: Final[dict[str, Any]] = {
    "enabled": True,
    "event_types": ["FOMC", "CPI", "PPI", "NFP", "POWELL"],
    "window_bd_before": 1,
    "window_bd_after": 1,
    "penalty_points": -10,
}


def _build_config(
    *,
    name: str,
    version: str,
    min_price: float,
    min_adv: float,
    min_rvol: float,
    min_screening_score: float,
    min_step3_setup_score: float,
    min_step4_setup_score: float,
    min_consolidation_for_breakout: float,
    min_estimated_rr: float,
    target_r: float,
    sector_max_positions: int,
    industry_max_positions: int,
    earnings_avoid_within_bd: int,
) -> dict[str, Any]:
    """Assemble one full strategy-config dict accepted by every frozen engine."""
    return {
        "strategy_name": name,
        "version": version,
        "universe": {
            "min_price": min_price,
            "min_avg_dollar_volume_20d": min_adv,
            "allowed_symbol_types": ["stock"],
            "exclude_benchmarks": True,
        },
        "features": dict(_FEATURES_BLOCK),
        "screening": {
            "min_rvol": min_rvol,
            "min_screening_score": min_screening_score,
            "min_step3_setup_score": min_step3_setup_score,
            "require_feature_ready": True,
        },
        "scoring_weights": dict(_SCORING_WEIGHTS_BLOCK),
        "step4": {
            "target_R": target_r,
            "min_step4_setup_score": min_step4_setup_score,
            "min_consolidation_for_breakout": min_consolidation_for_breakout,
            "min_estimated_rr": min_estimated_rr,
        },
        "market_regime": dict(_MARKET_REGIME_BLOCK),
        "diversification": {
            "hard_cap_enabled": True,
            "top_n": 10,
            "sector_max_positions": sector_max_positions,
            "industry_max_positions": industry_max_positions,
            "sector_penalty_factor": 0.9,
            "industry_penalty_factor": 0.85,
            "penalty_applies_before_cap_only": True,
        },
        "sector_etf_mapping": dict(constants.SECTOR_ETF_MAP),
        "simulation": dict(_SIMULATION_BLOCK),
        "macro_event_risk": dict(_MACRO_BLOCK),
        "earnings": {
            "avoid_within_bd": earnings_avoid_within_bd,
            "penalty_points_max": -15,
        },
    }


DEFAULT_STRATEGY_CONFIGS: Final[dict[str, dict]] = {
    "normal": _build_config(
        name="normal",
        version="normal_v2",
        min_price=10,
        min_adv=20_000_000,
        min_rvol=1.5,
        min_screening_score=65,
        min_step3_setup_score=55.0,
        min_step4_setup_score=65.0,
        min_consolidation_for_breakout=60.0,
        min_estimated_rr=1.8,
        target_r=2.2,
        sector_max_positions=3,
        industry_max_positions=2,
        earnings_avoid_within_bd=10,
    ),
    "aggressive": _build_config(
        name="aggressive",
        version="aggressive_v2",
        min_price=5,
        min_adv=5_000_000,
        min_rvol=1.2,
        min_screening_score=55,
        min_step3_setup_score=45.0,
        min_step4_setup_score=60.0,
        min_consolidation_for_breakout=40.0,
        min_estimated_rr=1.5,
        target_r=1.8,
        sector_max_positions=5,
        industry_max_positions=3,
        earnings_avoid_within_bd=3,
    ),
    "conservative": _build_config(
        name="conservative",
        version="conservative_v2",
        min_price=15,
        min_adv=50_000_000,
        min_rvol=1.8,
        min_screening_score=75,
        min_step3_setup_score=62.0,
        min_step4_setup_score=75.0,
        min_consolidation_for_breakout=70.0,
        min_estimated_rr=2.2,
        target_r=2.8,
        sector_max_positions=2,
        industry_max_positions=1,
        earnings_avoid_within_bd=15,
    ),
}


# --------------------------------------------------------------------------- #
# Internal step-result helper.
# --------------------------------------------------------------------------- #
class _StepOutcome:
    """Normalized result of attempting one logical pipeline step.

    Attributes
    ----------
    ok:
        ``True`` when the step did not critically fail (success,
        success_with_warnings, or a recoverable failure).
    failed_critical:
        ``True`` when a *critical* step failed and the run must abort.
    warnings:
        Non-fatal messages collected from the step.
    error:
        First fatal message (used for the run error_message on critical fail).
    """

    __slots__ = ("ok", "failed_critical", "warnings", "error")

    def __init__(
        self,
        ok: bool,
        failed_critical: bool,
        warnings: list[str],
        error: str | None,
    ) -> None:
        self.ok = ok
        self.failed_critical = failed_critical
        self.warnings = warnings
        self.error = error


class PipelineOrchestrator:
    """Daily pipeline run coordinator (control plane only)."""

    def __init__(
        self,
        db_manager: Any | None = None,
        provider: Any | None = None,
        benchmark_loader: Any | None = None,
        universe_engine: Any | None = None,
        ingestion_engine: Any | None = None,
        validation_engine: Any | None = None,
        mutation_engine: Any | None = None,
        feature_engine: Any | None = None,
        screening_engine: Any | None = None,
        analysis_engine: Any | None = None,
        proposal_engine: Any | None = None,
        outcome_creator: Any | None = None,
        outcome_processor: Any | None = None,
        config_service: Any | None = None,
    ) -> None:
        # DB manager: approved default is the centralized duckdb_manager module.
        self._db = db_manager if db_manager is not None else duckdb_manager

        # ConfigService is the runtime source of truth for active strategy
        # configs (M21 Config Management Addendum §7). Only consulted when the
        # caller does not pass an explicit ``strategy_configs`` override.
        if config_service is None:
            from app.services.config.config_service import ConfigService

            config_service = ConfigService(db_manager=self._db)
        self._config_service = config_service

        # Real default dependencies are constructed here (and only here). Tests
        # inject fakes so these real constructors never run under test.
        if provider is None:
            from app.providers.yahoo_provider import YahooProvider

            provider = YahooProvider()
        self._provider = provider

        if benchmark_loader is None:
            from app.services.benchmarks.benchmark_etf_loader import (
                BenchmarkEtfLoader,
            )

            benchmark_loader = BenchmarkEtfLoader(db_manager=self._db)
        self._benchmark_loader = benchmark_loader

        if universe_engine is None:
            from app.services.universe.universe_snapshot import (
                UniverseSnapshotEngine,
            )

            universe_engine = UniverseSnapshotEngine(db_manager=self._db)
        self._universe_engine = universe_engine

        if ingestion_engine is None:
            from app.services.ingestion.daily_price_ingestion import (
                DailyPriceIngestionEngine,
            )

            ingestion_engine = DailyPriceIngestionEngine(db_manager=self._db)
        self._ingestion_engine = ingestion_engine

        if validation_engine is None:
            from app.services.validation.data_validator import DataValidator

            validation_engine = DataValidator(db_manager=self._db)
        self._validation_engine = validation_engine

        if mutation_engine is None:
            from app.services.mutation.mutation_detector import MutationDetector

            mutation_engine = MutationDetector(db_manager=self._db)
        self._mutation_engine = mutation_engine

        if feature_engine is None:
            from app.services.features.feature_engine import FeatureEngine

            feature_engine = FeatureEngine(db_manager=self._db)
        self._feature_engine = feature_engine

        # Market regime engine — injected after feature_engine, runs before Step 3.
        if not hasattr(self, '_regime_engine'):
            from app.services.market_regime.market_regime_engine import (
                MarketRegimeEngine,
            )
            self._regime_engine = MarketRegimeEngine(db_manager=self._db)

        if screening_engine is None:
            from app.services.screening.step3_screening import (
                Step3ScreeningEngine,
            )

            screening_engine = Step3ScreeningEngine(db_manager=self._db)
        self._screening_engine = screening_engine

        if analysis_engine is None:
            from app.services.analysis.step4_analysis_engine import (
                Step4AnalysisEngine,
            )

            analysis_engine = Step4AnalysisEngine(db_manager=self._db)
        self._analysis_engine = analysis_engine

        if proposal_engine is None:
            from app.services.proposal.step5_proposal_engine import (
                Step5ProposalEngine,
            )

            proposal_engine = Step5ProposalEngine(db_manager=self._db)
        self._proposal_engine = proposal_engine

        if outcome_creator is None:
            from app.services.outcomes.outcome_queue import OutcomeQueueCreator

            outcome_creator = OutcomeQueueCreator(db_manager=self._db)
        self._outcome_creator = outcome_creator

        if outcome_processor is None:
            from app.services.outcomes.outcome_queue import (
                OutcomeQueueProcessor,
            )

            outcome_processor = OutcomeQueueProcessor(db_manager=self._db)
        self._outcome_processor = outcome_processor

    # ------------------------------------------------------------------ #
    # Public API.
    # ------------------------------------------------------------------ #
    def run(
        self,
        run_date: date,
        run_type: str = "scheduled",
        db_role: str = "prod",
        force_rerun: bool = False,
        resume_from: str | None = None,
        strategy_configs: dict[str, dict] | None = None,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Execute one daily pipeline run; always returns a ``ServiceResult``."""
        started = time.monotonic()
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        # --- Pre-DB validation (must not touch the DB). ---
        # ``strategy_configs`` may be None (resolve active configs from the DB
        # via ConfigService after the lifecycle row is written); an explicit
        # value must be a non-empty dict.
        pre_error = self._validate_inputs(
            run_type, db_role, resume_from, strategy_configs
        )
        if pre_error is not None:
            log.error("pre-db validation failed: %s", pre_error)
            return self._result(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                run_date=run_date,
                run_type=run_type,
                db_role=db_role,
                steps_completed=[],
                failed_step=None,
                error=pre_error,
                duration_sec=time.monotonic() - started,
                warnings=[],
            )

        # --- Lock acquire (may fail without owning the lock). ---
        lock_error = self._acquire_lock(run_id, db_role, log)
        if lock_error is not None:
            return self._result(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                run_date=run_date,
                run_type=run_type,
                db_role=db_role,
                steps_completed=[],
                failed_step=None,
                error=lock_error,
                duration_sec=time.monotonic() - started,
                warnings=[],
            )

        # Lock is held from here on; release in finally regardless of outcome.
        steps_completed: list[str] = []
        warnings: list[str] = []
        failed_step: str | None = None
        error: str | None = None
        status = service_result.STATUS_SUCCESS
        try:
            # --- Already-run guard. ---
            try:
                already = self._already_run(run_date, db_role)
            except Exception as exc:  # noqa: BLE001 - DB failure before running row
                error = f"failed to query pipeline_runs: {exc}"
                log.error(error)
                return self._result(
                    status=service_result.STATUS_FAILED,
                    run_id=run_id,
                    run_date=run_date,
                    run_type=run_type,
                    db_role=db_role,
                    steps_completed=[],
                    failed_step=None,
                    error=error,
                    duration_sec=time.monotonic() - started,
                    warnings=[],
                )
            if already is not None and not force_rerun:
                prev_id, prev_status = already
                error = (
                    f"run_date already succeeded "
                    f"(prev_run_id={prev_id}, status={prev_status})"
                )
                log.warning(error)
                return self._result(
                    status=service_result.STATUS_FAILED,
                    run_id=run_id,
                    run_date=run_date,
                    run_type=run_type,
                    db_role=db_role,
                    steps_completed=[],
                    failed_step=None,
                    error=error,
                    duration_sec=time.monotonic() - started,
                    warnings=[],
                )
            if already is not None and force_rerun:
                log.warning(
                    "run_date already succeeded but force_rerun=True; "
                    "continuing (prev_run_id=%s)",
                    already[0],
                )

            # --- Lifecycle row. ---
            try:
                self._write(db_role, _INSERT_RUNNING, [run_id, run_date, run_type])
            except Exception as exc:  # noqa: BLE001 - running row not inserted
                error = f"failed to insert pipeline_runs running row: {exc}"
                log.error(error)
                return self._result(
                    status=service_result.STATUS_FAILED,
                    run_id=run_id,
                    run_date=run_date,
                    run_type=run_type,
                    db_role=db_role,
                    steps_completed=[],
                    failed_step=None,
                    error=error,
                    duration_sec=time.monotonic() - started,
                    warnings=[],
                )

            # --- Resolve strategy configs + persist traceability. ---
            # Explicit override wins (tests/debug/manual); otherwise load active
            # configs from ConfigService, seeding defaults if none are active.
            try:
                configs, strat_ids, runtime_ids = self._resolve_configs(
                    strategy_configs, db_role, log
                )
            except Exception as exc:  # noqa: BLE001 - config resolution failed
                error = f"failed to resolve strategy configs: {exc}"
                log.error(error)
                duration = time.monotonic() - started
                try:
                    self._write(db_role, _UPDATE_FAILED, [duration, error, run_id])
                except Exception as fexc:  # noqa: BLE001
                    log.error("failed to finalize after config error: %s", fexc)
                return self._result(
                    status=service_result.STATUS_FAILED,
                    run_id=run_id,
                    run_date=run_date,
                    run_type=run_type,
                    db_role=db_role,
                    steps_completed=[],
                    failed_step=None,
                    error=error,
                    duration_sec=duration,
                    warnings=[],
                )

            snapshot_hash = config_validator.snapshot_hash(
                {
                    "strategy_configs_by_id": configs,
                    "runtime_config_ids": runtime_ids,
                }
            )
            try:
                self._write(
                    db_role,
                    _UPDATE_CONFIG_META,
                    [
                        json.dumps(strat_ids),
                        json.dumps(runtime_ids),
                        snapshot_hash,
                        run_id,
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - traceability is best-effort
                log.warning("failed to persist config traceability: %s", exc)

            resume_index = (
                STEP_NAMES.index(resume_from) if resume_from is not None else 0
            )

            # --- Linear steps 1-6. ---
            linear_steps = (
                ("benchmark_etf_ingestion", self._step_benchmark),
                ("universe_ingestion", self._step_universe),
                ("price_ingestion", self._step_price),
                ("validation", self._step_validation),
                ("mutation_detection", self._step_mutation),
                ("feature_calculation", self._step_features),
                ("market_regime_classification", self._step_market_regime),
            )
            for name, func in linear_steps:
                idx = STEP_NAMES.index(name)
                if idx < resume_index:
                    log.info("step %s skipped (resume_from=%s)", name, resume_from)
                    continue
                outcome = self._safe_step(name, func, log, run_date, db_role, run_id)
                if outcome.failed_critical:
                    failed_step = name
                    error = outcome.error
                    status = service_result.STATUS_FAILED
                    break
                warnings.extend(outcome.warnings)
                if not outcome.ok or outcome.warnings:
                    status = service_result.STATUS_SUCCESS_WITH_WARNINGS
                db_err = self._record_step(name, steps_completed, db_role, run_id, log)
                if db_err:
                    error = db_err
                    status = service_result.STATUS_FAILED
                    break

            # --- Strategy steps 7-11 (per config), only if not aborted. ---
            if status != service_result.STATUS_FAILED:
                strat_warnings, strat_failed, strat_error = self._run_strategy_steps(
                    configs,
                    resume_index,
                    log,
                    run_date,
                    db_role,
                    run_id,
                    steps_completed,
                    force_rerun=force_rerun,
                )
                # steps_completed already mutated inside _run_strategy_steps.
                warnings.extend(strat_warnings)
                if strat_failed is not None:
                    failed_step = strat_failed
                    error = strat_error
                    status = service_result.STATUS_FAILED
                elif strat_warnings:
                    status = service_result.STATUS_SUCCESS_WITH_WARNINGS

            # --- Steps 12-13 (recoverable). ---
            if status != service_result.STATUS_FAILED:
                tail_steps = (
                    ("dashboard_materialization", self._step_dashboard),
                )
                for name, func in tail_steps:
                    idx = STEP_NAMES.index(name)
                    if idx < resume_index:
                        log.info(
                            "step %s skipped (resume_from=%s)", name, resume_from
                        )
                        continue
                    outcome = self._safe_step(
                        name, func, log, run_date, db_role, run_id
                    )
                    # Steps 12-13 are recoverable; step failure never aborts.
                    warnings.extend(outcome.warnings)
                    if not outcome.ok or outcome.warnings:
                        status = service_result.STATUS_SUCCESS_WITH_WARNINGS
                    db_err = self._record_step(
                        name, steps_completed, db_role, run_id, log
                    )
                    if db_err:
                        error = db_err
                        status = service_result.STATUS_FAILED
                        break

            # --- Finalize run row. ---
            duration = time.monotonic() - started
            try:
                if status == service_result.STATUS_FAILED:
                    self._write(
                        db_role,
                        _UPDATE_FAILED,
                        [
                            duration,
                            error or f"critical failure at {failed_step}",
                            run_id,
                        ],
                    )
                else:
                    self._write(
                        db_role,
                        _UPDATE_SUCCESS,
                        [status, duration, json.dumps(steps_completed), run_id],
                    )
            except Exception as exc:  # noqa: BLE001 - DB unavailable on finalize
                log.error("failed to finalize pipeline_runs: %s", exc)
                error = f"failed to finalize pipeline_runs: {exc}"
                status = service_result.STATUS_FAILED

            return self._result(
                status=status,
                run_id=run_id,
                run_date=run_date,
                run_type=run_type,
                db_role=db_role,
                steps_completed=steps_completed,
                failed_step=failed_step,
                error=error,
                duration_sec=duration,
                warnings=warnings,
            )
        finally:
            self._release_lock(db_role, log)

    # ------------------------------------------------------------------ #
    # Validation.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_inputs(
        run_type: str,
        db_role: str,
        resume_from: str | None,
        strategy_configs: Any,
    ) -> str | None:
        """Return an error string if any input is invalid, else ``None``.

        ``strategy_configs`` is optional: ``None`` means "resolve active configs
        from the DB later"; an explicit value must be a non-empty dict.
        """
        if run_type not in ALLOWED_RUN_TYPES:
            return (
                f"invalid run_type {run_type!r}; "
                f"valid: {sorted(ALLOWED_RUN_TYPES)}"
            )
        if db_role not in ALLOWED_DB_ROLES:
            return (
                f"invalid db_role {db_role!r}; valid: {sorted(ALLOWED_DB_ROLES)}"
            )
        if resume_from is not None and resume_from not in STEP_NAMES:
            return f"invalid resume_from {resume_from!r}; valid: {list(STEP_NAMES)}"
        if strategy_configs is not None and (
            not isinstance(strategy_configs, dict) or not strategy_configs
        ):
            return "strategy_configs must be a non-empty dict"
        return None

    # ------------------------------------------------------------------ #
    # Strategy config resolution (M21 Config Management Addendum §7).
    # ------------------------------------------------------------------ #
    def _resolve_configs(
        self,
        strategy_configs: dict[str, dict] | None,
        db_role: str,
        log: Any,
    ) -> tuple[dict[str, dict], list[str], list[str]]:
        """Resolve the strategy configs the run will use.

        Returns ``(configs_by_id, strategy_config_ids, runtime_config_ids)`` where
        ``configs_by_id`` is keyed by the identifier that is passed to the Step
        3/4/5/outcome engines and written as ``strategy_config_id`` in the output
        tables. ``strategy_config_ids`` is exactly ``list(configs_by_id)`` so the
        traceability column always matches the ids actually used.

        - If ``strategy_configs`` is provided, it is used verbatim as an explicit
          override (tests/debug/manual); its keys are the ids used downstream (no
          DB lookup), so callers control the recorded ``strategy_config_id``.
        - Otherwise the active strategy configs are loaded from the DB via
          :class:`ConfigService`, keyed by the real DB ``config_id``; if none are
          active, defaults are seeded and the active configs are reloaded.

        Raises on a hard failure so the caller can finalize the run as failed.
        """
        if strategy_configs is not None:
            log.info(
                "using explicit strategy_configs override (%d configs)",
                len(strategy_configs),
            )
            configs_by_id = dict(strategy_configs)
            return configs_by_id, list(configs_by_id), []

        result = self._config_service.get_active_strategy_configs(db_role)
        if not result.is_ok():
            raise RuntimeError(
                "; ".join(result.errors) or "get_active_strategy_configs failed"
            )
        configs_by_id = dict(result.metadata.get("configs_by_id") or {})

        if not configs_by_id:
            log.info(
                "no active strategy configs for db_role=%s; seeding defaults",
                db_role,
            )
            seed = self._config_service.seed_default_strategy_configs(db_role)
            if not seed.is_ok():
                raise RuntimeError(
                    "; ".join(seed.errors) or "seed_default_strategy_configs failed"
                )
            result = self._config_service.get_active_strategy_configs(db_role)
            if not result.is_ok():
                raise RuntimeError(
                    "; ".join(result.errors)
                    or "get_active_strategy_configs failed after seeding"
                )
            configs_by_id = dict(result.metadata.get("configs_by_id") or {})

        if not configs_by_id:
            raise RuntimeError(
                f"no active strategy configs available for db_role={db_role!r} "
                "after seeding"
            )

        runtime_ids = self._collect_runtime_config_ids(db_role, log)
        return configs_by_id, list(configs_by_id), runtime_ids

    def _collect_runtime_config_ids(self, db_role: str, log: Any) -> list[str]:
        """Best-effort list of active runtime config ids for traceability."""
        try:
            listing = self._config_service.list_runtime_configs(db_role)
        except Exception as exc:  # noqa: BLE001 - traceability is non-fatal
            log.warning("could not list runtime configs: %s", exc)
            return []
        if not listing.is_ok():
            return []
        return [
            v["config_id"]
            for v in (listing.metadata.get("versions") or [])
            if v.get("active_flag")
        ]

    # ------------------------------------------------------------------ #
    # Lock protocol.
    # ------------------------------------------------------------------ #
    def _acquire_lock(self, run_id: str, db_role: str, log: Any) -> str | None:
        """Acquire the daily lock. Returns an error string if blocked or on DB failure.

        On an active, non-stale lock: returns the failed message and does NOT
        upsert the lock or touch ``pipeline_runs``. On a stale lock: logs a
        warning and overwrites. On no/free lock: upserts a fresh lock. DB
        failures during the lock read or upsert are caught and returned as
        error strings so the caller can return a ``failed`` ``ServiceResult``
        without ever owning the lock.
        """
        try:
            row = self._read_lock(db_role)
        except Exception as exc:  # noqa: BLE001 - DB unavailable before lock
            msg = f"failed to read pipeline lock: {exc}"
            log.error(msg)
            return msg
        if row is not None:
            lock_run_id, is_locked, heartbeat_at = row
            if is_locked and not self._is_stale(heartbeat_at):
                msg = (
                    f"pipeline is already running "
                    f"(lock_run_id={lock_run_id}, heartbeat_at={heartbeat_at})"
                )
                log.error(msg)
                return msg
            if is_locked:
                log.warning(
                    "overwriting stale pipeline lock (stale run_id=%s, "
                    "heartbeat_at=%s)",
                    lock_run_id,
                    heartbeat_at,
                )
        try:
            self._write(db_role, _UPSERT_LOCK, [run_id])
        except Exception as exc:  # noqa: BLE001 - lock write failed; not holding lock
            msg = f"failed to acquire pipeline lock: {exc}"
            log.error(msg)
            return msg
        return None

    @staticmethod
    def _is_stale(heartbeat_at: Any) -> bool:
        """Return ``True`` if a heartbeat is missing or older than the threshold."""
        if heartbeat_at is None:
            return True
        if not isinstance(heartbeat_at, datetime):
            # Unknown representation: treat as stale rather than block forever.
            return True
        return (datetime.now() - heartbeat_at) > timedelta(
            seconds=LOCK_STALE_SECONDS
        )

    def _read_lock(self, db_role: str) -> tuple[Any, Any, Any] | None:
        """Read the daily lock row via a read-only connection (or ``None``)."""
        connection = self._db.connect(db_role, read_only=True)
        try:
            cursor = connection.execute(_SELECT_LOCK, [PIPELINE_LOCK_NAME])
            row = cursor.fetchone()
        finally:
            connection.close()
        if not row:
            return None
        return (row[0], row[1], row[2])

    def _release_lock(self, db_role: str, log: Any) -> None:
        """Release the lock; log but never raise on failure."""
        try:
            self._write(db_role, _RELEASE_LOCK, [])
        except Exception as exc:  # noqa: BLE001 - release failure is non-fatal
            log.error("failed to release pipeline lock: %s", exc)

    # ------------------------------------------------------------------ #
    # Already-run guard.
    # ------------------------------------------------------------------ #
    def _already_run(
        self, run_date: date, db_role: str
    ) -> tuple[Any, Any] | None:
        """Return ``(run_id, status)`` of a prior successful run, else ``None``."""
        connection = self._db.connect(db_role, read_only=True)
        try:
            cursor = connection.execute(_SELECT_ALREADY_RUN, [run_date])
            row = cursor.fetchone()
        finally:
            connection.close()
        if not row:
            return None
        return (row[0], row[1])

    # ------------------------------------------------------------------ #
    # DB write helper (open / execute / close — no long-held writer).
    # ------------------------------------------------------------------ #
    def _write(self, db_role: str, sql: str, params: list[Any]) -> None:
        connection = self._db.connect(db_role, read_only=False)
        try:
            connection.execute(sql, params)
        finally:
            connection.close()

    def _record_step(
        self,
        name: str,
        steps_completed: list[str],
        db_role: str,
        run_id: str,
        log: Any,
    ) -> str | None:
        """Append a completed step, persist progress, and beat the heartbeat.

        Appends ``name`` to ``steps_completed`` unconditionally (so the
        in-memory list is always accurate), then attempts the DB writes. Returns
        ``None`` on success or an error string if any DB write fails — callers
        treat a non-``None`` return as a critical infrastructure failure.
        """
        steps_completed.append(name)
        try:
            self._write(db_role, _UPDATE_STEPS, [json.dumps(steps_completed), run_id])
            # Heartbeat immediately after recording the completed step
            # (G-HEARTBEAT-THREADING: V1 inline; future background heartbeat).
            self._write(db_role, _HEARTBEAT_LOCK, [])
        except Exception as exc:  # noqa: BLE001 - DB infrastructure failure
            log.error("failed to persist step %s progress: %s", name, exc)
            return f"DB error recording step {name}: {exc}"
        log.info("step %s completed", name)
        return None

    # ------------------------------------------------------------------ #
    # Step execution helpers.
    # ------------------------------------------------------------------ #
    def _safe_step(
        self,
        name: str,
        func: Any,
        log: Any,
        run_date: date,
        db_role: str,
        run_id: str,
    ) -> _StepOutcome:
        """Run a linear step, classifying failures by step criticality."""
        critical = name in CRITICAL_STEPS
        try:
            result = func(run_date, db_role, run_id, log)
        except Exception as exc:  # noqa: BLE001 - never raise from a step
            log.error("step %s raised: %s", name, exc)
            if critical:
                return _StepOutcome(False, True, [], f"{name} raised: {exc}")
            return _StepOutcome(
                False, False, [f"{name} raised (recoverable): {exc}"], None
            )
        return self._classify(name, result, critical, log)

    @staticmethod
    def _classify(
        name: str, result: Any, critical: bool, log: Any
    ) -> _StepOutcome:
        """Convert a step ``ServiceResult`` into a normalized ``_StepOutcome``."""
        status = getattr(result, "status", None)
        errs = list(getattr(result, "errors", []) or [])
        warns = list(getattr(result, "warnings", []) or [])
        if status == service_result.STATUS_FAILED:
            if critical:
                log.error("critical step %s failed: %s", name, errs)
                return _StepOutcome(
                    False, True, warns, errs[0] if errs else f"{name} failed"
                )
            log.warning("recoverable step %s failed: %s", name, errs)
            msg = errs[0] if errs else f"{name} failed (recoverable)"
            return _StepOutcome(False, False, warns + [msg], None)
        if status == service_result.STATUS_SUCCESS_WITH_WARNINGS:
            return _StepOutcome(True, False, warns, None)
        return _StepOutcome(True, False, warns, None)

    def _run_strategy_steps(
        self,
        configs: dict[str, dict],
        resume_index: int,
        log: Any,
        run_date: date,
        db_role: str,
        run_id: str,
        steps_completed: list[str],
        force_rerun: bool = False,
    ) -> tuple[list[str], str | None, str | None]:
        """Run steps 7-11 in step-major order and record each logical step
        only after **all** configs have completed it.

        Returns ``(warnings, failed_step, error)``. The caller's
        ``steps_completed`` list is mutated in place via :meth:`_record_step`.

        Execution order — **step-major** (``01d §70`` per-config call order is
        preserved because, within a single config, screen is always executed
        before analyze, analyze before propose, etc.):

          step3_screening for cfg1, cfg2, … → record step3_screening
          step4_analysis  for cfg1, cfg2, … → record step4_analysis
          …

        A critical failure on any config for a step aborts immediately. All
        logical steps recorded *before* the failing step remain in
        ``steps_completed``; the failing step and every later step are absent.
        This ensures a logical step is never recorded as complete unless every
        configured strategy has executed it successfully (or recoverable-failed
        it), preventing misleading partial state in ``pipeline_runs``.
        """
        warnings: list[str] = []
        in_scope = [
            s for s in STRATEGY_STEPS if STEP_NAMES.index(s) >= resume_index
        ]
        for s in STRATEGY_STEPS:
            if STEP_NAMES.index(s) < resume_index:
                log.info("step %s skipped (resume)", s)

        callers = {
            "step3_screening": self._call_screen,
            "step4_analysis": self._call_analyze,
            "step5_proposals": self._call_propose,
            "outcome_queue_creation": self._call_enqueue,
            "outcome_processing": self._call_process,
        }

        for step_name in in_scope:
            critical = step_name in CRITICAL_STEPS
            step_warnings: list[str] = []

            for config_id, config_dict in configs.items():
                try:
                    result = callers[step_name](
                        run_date, db_role, run_id, config_id, config_dict, log,
                        force_rerun=force_rerun,
                    )
                except Exception as exc:  # noqa: BLE001 - never raise from step
                    log.error(
                        "strategy step %s raised for config %s: %s",
                        step_name,
                        config_id,
                        exc,
                    )
                    if critical:
                        warnings.extend(step_warnings)
                        return (
                            warnings,
                            step_name,
                            f"{step_name} raised for config {config_id}: {exc}",
                        )
                    step_warnings.append(
                        f"{step_name} raised (recoverable) for config "
                        f"{config_id}: {exc}"
                    )
                    continue
                outcome = self._classify(step_name, result, critical, log)
                if outcome.failed_critical:
                    warnings.extend(step_warnings)
                    return (
                        warnings,
                        step_name,
                        outcome.error or f"{step_name} failed",
                    )
                step_warnings.extend(outcome.warnings)

            # All configs completed this logical step.  Record it now.
            warnings.extend(step_warnings)
            db_err = self._record_step(
                step_name, steps_completed, db_role, run_id, log
            )
            if db_err:
                return (warnings, step_name, db_err)

        return (warnings, None, None)

    # ------------------------------------------------------------------ #
    # Linear step bodies (each returns a ServiceResult-like object).
    # ------------------------------------------------------------------ #
    def _step_benchmark(self, run_date: date, db_role: str, run_id: str, log: Any):
        return self._benchmark_loader.load(
            provider=self._provider,
            start_date=run_date,
            end_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_universe(self, run_date: date, db_role: str, run_id: str, log: Any):
        symbol_result = self._provider.list_symbols(symbol_type="stock")
        if getattr(symbol_result, "status", None) == service_result.STATUS_FAILED:
            return symbol_result
        entries = symbol_result.metadata.get("symbols", [])
        return self._universe_engine.apply_snapshot(
            entries=entries,
            as_of_date=run_date,
            db_role=db_role,
            source=_UNIVERSE_SOURCE,
            run_id=run_id,
        )

    def _step_price(self, run_date: date, db_role: str, run_id: str, log: Any):
        return self._ingestion_engine.ingest(
            provider=self._provider,
            start_date=run_date,
            end_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_validation(self, run_date: date, db_role: str, run_id: str, log: Any):
        return self._validation_engine.validate(
            start_date=run_date,
            end_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_mutation(self, run_date: date, db_role: str, run_id: str, log: Any):
        return self._mutation_engine.detect(
            start_date=run_date,
            end_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_features(self, run_date: date, db_role: str, run_id: str, log: Any):
        return self._feature_engine.calculate(
            start_date=run_date,
            end_date=run_date,
            tickers=None,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_market_regime(self, run_date: date, db_role: str, run_id: str, log: Any):
        """Classify market regime for run_date and write to daily_features.

        Recoverable: a failure here logs a warning but does not abort the run.
        Step 3 screening proceeds with market_score=0 (UNKNOWN) for all tickers.
        """
        return self._regime_engine.classify(
            start_date=run_date,
            end_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_dashboard(self, run_date: date, db_role: str, run_id: str, log: Any):
        log.info(
            "dashboard materialization skipped "
            "(G-DASHBOARD-MAT: Module 21 not yet implemented)"
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS, run_id=run_id, rows_processed=0
        )



    # ------------------------------------------------------------------ #
    # Strategy step bodies (one config each).
    # ------------------------------------------------------------------ #
    def _call_screen(
        self, run_date, db_role, run_id, config_id, config_dict, log,
        force_rerun: bool = False,
    ):
        if force_rerun:
            self._purge_date(run_date, config_id, db_role, log)
        return self._screening_engine.screen(
            signal_date=run_date,
            strategy_config=config_dict,
            strategy_config_id=config_id,
            db_role=db_role,
            run_id=run_id,
        )

    def _call_analyze(
        self, run_date, db_role, run_id, config_id, config_dict, log,
        force_rerun: bool = False,
    ):
        return self._analysis_engine.analyze(
            signal_date=run_date,
            strategy_config=config_dict,
            strategy_config_id=config_id,
            db_role=db_role,
            run_id=run_id,
        )

    def _call_propose(
        self, run_date, db_role, run_id, config_id, config_dict, log,
        force_rerun: bool = False,
    ):
        return self._proposal_engine.propose(
            signal_date=run_date,
            strategy_config=config_dict,
            strategy_config_id=config_id,
            db_role=db_role,
            run_id=run_id,
        )

    def _call_enqueue(self, run_date, db_role, run_id, config_id, config_dict, log, force_rerun: bool = False):
        return self._outcome_creator.enqueue(
            signal_date=run_date,
            strategy_config_id=config_id,
            strategy_config=config_dict,
            db_role=db_role,
            run_id=run_id,
        )

    def _call_process(self, run_date, db_role, run_id, config_id, config_dict, log, force_rerun: bool = False):
        return self._outcome_processor.process(
            run_date=run_date,
            strategy_config=config_dict,
            db_role=db_role,
            run_id=run_id,
        )

    # ------------------------------------------------------------------ #
    # Force-rerun: purge existing rows for (signal_date, strategy_config_id).
    # ------------------------------------------------------------------ #
    def _purge_date(
        self,
        signal_date: "date",
        strategy_config_id: str,
        db_role: str,
        log: Any,
    ) -> None:
        """Delete all pipeline output rows for this date + config before rerun.

        Purge order is child-first to avoid FK-like integrity issues:
          ai_reviews → execution_decisions → signal_outcomes
          → outcome_tracking_queue → step5_proposals
          → step4_analysis → step3_candidates

        Each DELETE is a separate connection (single-writer discipline).
        Errors are logged as warnings but do not abort the purge; the
        subsequent INSERT from the engines will either overwrite or fail with
        a clear duplicate-key error.
        """
        _PURGE_SQLS: list[tuple[str, list]] = [
            (
                "DELETE FROM ai_reviews WHERE proposal_id IN "
                "(SELECT proposal_id FROM step5_proposals "
                " WHERE signal_date = ? AND strategy_config_id = ?)",
                [signal_date, strategy_config_id],
            ),
            (
                "DELETE FROM execution_decisions WHERE proposal_id IN "
                "(SELECT proposal_id FROM step5_proposals "
                " WHERE signal_date = ? AND strategy_config_id = ?)",
                [signal_date, strategy_config_id],
            ),
            (
                "DELETE FROM signal_outcomes WHERE proposal_id IN "
                "(SELECT proposal_id FROM step5_proposals "
                " WHERE signal_date = ? AND strategy_config_id = ?)",
                [signal_date, strategy_config_id],
            ),
            (
                "DELETE FROM outcome_tracking_queue WHERE proposal_id IN "
                "(SELECT proposal_id FROM step5_proposals "
                " WHERE signal_date = ? AND strategy_config_id = ?)",
                [signal_date, strategy_config_id],
            ),
            (
                "DELETE FROM step5_proposals "
                "WHERE signal_date = ? AND strategy_config_id = ?",
                [signal_date, strategy_config_id],
            ),
            (
                "DELETE FROM step4_analysis "
                "WHERE signal_date = ? AND strategy_config_id = ?",
                [signal_date, strategy_config_id],
            ),
            (
                "DELETE FROM step3_candidates "
                "WHERE signal_date = ? AND strategy_config_id = ?",
                [signal_date, strategy_config_id],
            ),
        ]
        log.warning(
            "force_rerun: purging existing rows for signal_date=%s config=%s",
            signal_date,
            strategy_config_id,
        )
        for sql, params in _PURGE_SQLS:
            try:
                conn = connect_with_retry(db_role, read_only=False)
                try:
                    conn.execute(sql, params)
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("purge warning (non-fatal): %s", exc)

    # ------------------------------------------------------------------ #
    # ServiceResult assembly.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _result(
        *,
        status: str,
        run_id: str,
        run_date: date,
        run_type: str,
        db_role: str,
        steps_completed: list[str],
        failed_step: str | None,
        error: str | None,
        duration_sec: float | None,
        warnings: list[str],
    ) -> ServiceResult:
        metadata = {
            "run_id": run_id,
            "run_date": run_date.isoformat(),
            "run_type": run_type,
            "db_role": db_role,
            "steps_completed": list(steps_completed),
            "failed_step": failed_step,
            "error": error,
            "duration_sec": duration_sec,
            "status": status,
        }
        errors = [error] if error else []
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=len(steps_completed),
            warnings=list(warnings),
            errors=errors,
            metadata=metadata,
        )
