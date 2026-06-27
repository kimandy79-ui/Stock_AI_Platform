"""Module 20 — Pipeline Orchestrator (setup-mode migration, Phase 6).

Coordinates one daily pipeline run end-to-end in setup mode.

Setup-mode step sequence (01d_MODULES_AND_PIPELINE.md §70):
  1. benchmark_etf_ingestion      (critical)
  2. universe_ingestion            (recoverable)
  3. earnings_calendar_refresh     (recoverable) — skipped if calendar already updated today
  4. price_ingestion               (critical)
  5. validation                    (critical)
  6. mutation_detection            (recoverable)
  7. feature_calculation           (critical)
  8. market_regime_classification  (recoverable)
  9. step3_universal_eligibility   (critical, ONCE per signal_date — M13)
 10. step4_setup_validation        (critical, ONCE per signal_date — M14, iterates setup configs internally)
 11. step5_proposals               (critical, ONCE per signal_date — M15)
 12. outcome_queue_creation        (critical)
 13. outcome_processing            (recoverable)
 14. dashboard_materialization     (recoverable, V1 no-op)

Hard boundaries:
- No direct ``duckdb`` import.
- No ``print()`` in this module.
- No DDL / ``ATTACH`` in executed SQL.
- No simulation-DB writes.
- No market-data logic.
- DB writes target only ``pipeline_runs``, ``pipeline_locks``, and
  ``pipeline_run_diagnostics``.  All domain tables are written by the step engines.
  Exceptions: ``cleanup_calculated_outputs_for_date`` deletes stale rows from
  ``daily_features``, ``step3_candidates``, ``step4_analysis``, and
  ``step5_proposals`` before a (re-)run; ``_step_earnings`` upserts into
  ``earnings_calendar`` when the calendar was not already refreshed today.
- All step engines and the default provider are instantiated in ``__init__`` only.
- The public surface always returns a ``ServiceResult``.
- No legacy strategy-mode paths (aggressive / normal / conservative).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Final

from app.config import constants
from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# DB role constants.
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = "prod"
DB_ROLE_DEBUG: Final[str] = "debug"
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

ALLOWED_RUN_TYPES: Final[tuple[str, ...]] = (
    "scheduled",
    "manual",
    "force_rerun",
    "catchup",
    "debug",
)

# --------------------------------------------------------------------------- #
# Step names — setup-mode order.
# --------------------------------------------------------------------------- #
STEP_NAMES: Final[tuple[str, ...]] = (
    "benchmark_etf_ingestion",
    "universe_ingestion",
    "earnings_calendar_refresh",
    "price_ingestion",
    "validation",
    "mutation_detection",
    "feature_calculation",
    "market_regime_classification",
    "step3_universal_eligibility",
    "step4_setup_validation",
    "step5_proposals",
    "outcome_queue_creation",
    "outcome_processing",
    "dashboard_materialization",
)

CRITICAL_STEPS: Final[frozenset[str]] = frozenset(
    {
        "benchmark_etf_ingestion",
        "price_ingestion",
        "validation",
        "feature_calculation",
        "step3_universal_eligibility",
        "step4_setup_validation",
        "step5_proposals",
        # outcome_queue_creation is critical in final state; recoverable in Phase 6
        # outcome queue (M16) called in _step_enqueue / _step_process.
    }
)
RECOVERABLE_STEPS: Final[frozenset[str]] = frozenset(
    {
        "universe_ingestion",
        "earnings_calendar_refresh",
        "mutation_detection",
        "market_regime_classification",
        "outcome_queue_creation",   # M16 legacy API; full setup-mode migration is Phase 7
        "outcome_processing",
        "dashboard_materialization",
    }
)

PIPELINE_LOCK_NAME: Final[str] = "daily_pipeline"
LOCK_STALE_SECONDS: Final[int] = 300

_UNIVERSE_SOURCE: Final[str] = "yahoo"

# --------------------------------------------------------------------------- #
# ServiceResult metadata keys.
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
# SQL — targets only pipeline_runs, pipeline_locks, pipeline_run_diagnostics.
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
_INSERT_DIAG: Final[str] = (
    "INSERT INTO pipeline_run_diagnostics "
    "(diag_id, run_id, signal_date, db_role, step_name, setup_type, "
    "metric_name, metric_value, reason, metadata_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

# --------------------------------------------------------------------------- #
# SQL — date-scoped cleanup of calculated outputs (used by cleanup step).
# Deletes are safe no-ops when the date has no rows.
# --------------------------------------------------------------------------- #
_DELETE_DAILY_FEATURES: Final[str] = (
    "DELETE FROM daily_features WHERE feature_date = ?"
)
_DELETE_STEP3: Final[str] = (
    "DELETE FROM step3_candidates WHERE signal_date = ?"
)
_DELETE_STEP4: Final[str] = (
    "DELETE FROM step4_analysis WHERE signal_date = ?"
)
_DELETE_STEP5: Final[str] = (
    "DELETE FROM step5_proposals WHERE signal_date = ?"
)

# --------------------------------------------------------------------------- #
# SQL — earnings calendar refresh (used only by _step_earnings).
# _SQL_EARNINGS_CHECK   : 1 = already refreshed today; 0 = needs refresh.
# _SQL_EARNINGS_TICKERS : active stock tickers eligible for earnings lookup.
# _SQL_EARNINGS_UPSERT  : insert or update one earnings event.
# --------------------------------------------------------------------------- #
_SQL_EARNINGS_CHECK: Final[str] = (
    "SELECT COUNT(*) FROM earnings_calendar "
    "WHERE CAST(updated_at AS DATE) = ?"
)
_SQL_EARNINGS_TICKERS: Final[str] = (
    "SELECT ticker FROM ticker_master "
    "WHERE symbol_type = 'stock' AND active_flag = TRUE "
    "ORDER BY ticker"
)
_SQL_EARNINGS_UPSERT: Final[str] = (
    "INSERT INTO earnings_calendar "
    "(ticker, earnings_date, session, source, confidence, updated_at) "
    "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
    "ON CONFLICT (ticker, earnings_date) DO UPDATE SET "
    "session = EXCLUDED.session, source = EXCLUDED.source, "
    "confidence = EXCLUDED.confidence, updated_at = EXCLUDED.updated_at"
)


# --------------------------------------------------------------------------- #
# Internal step-result helper.
# --------------------------------------------------------------------------- #
class _StepOutcome:
    """Normalized result of attempting one logical pipeline step."""

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


# --------------------------------------------------------------------------- #
# Orchestrator.
# --------------------------------------------------------------------------- #
class PipelineOrchestrator:
    """Daily pipeline run coordinator — setup mode (Phase 6).

    Primary iteration key is ``setup_config_id`` via the setup-mode engines.
    Steps 8–10 (M13/M14/M15) each run ONCE per signal_date; the setup-specific
    iteration (one pass per active setup_config) happens inside M14.
    """

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
        regime_engine: Any | None = None,
        eligibility_engine: Any | None = None,
        setup_validation_engine: Any | None = None,
        proposal_engine: Any | None = None,
        outcome_creator: Any | None = None,
        outcome_processor: Any | None = None,
        config_service: Any | None = None,
        diagnostics_service: Any | None = None,
    ) -> None:
        self._db = db_manager if db_manager is not None else duckdb_manager

        if config_service is None:
            from app.services.config.config_service import ConfigService
            config_service = ConfigService(db_manager=self._db)
        self._config_service = config_service

        if provider is None:
            from app.providers.yahoo_provider import YahooProvider
            provider = YahooProvider()
        self._provider = provider

        if benchmark_loader is None:
            from app.services.benchmarks.benchmark_etf_loader import BenchmarkEtfLoader
            benchmark_loader = BenchmarkEtfLoader(db_manager=self._db)
        self._benchmark_loader = benchmark_loader

        if universe_engine is None:
            from app.services.universe.universe_snapshot import UniverseSnapshotEngine
            universe_engine = UniverseSnapshotEngine(db_manager=self._db)
        self._universe_engine = universe_engine

        if ingestion_engine is None:
            from app.services.ingestion.daily_price_ingestion import DailyPriceIngestionEngine
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

        if regime_engine is None:
            from app.services.regime.market_regime_engine import MarketRegimeEngine
            regime_engine = MarketRegimeEngine(db_manager=self._db)
        self._regime_engine = regime_engine

        # Setup-mode M13 — Step 3 universal eligibility + routing.
        if eligibility_engine is None:
            from app.services.screening.step3_universal_eligibility import (
                Step3UniversalEligibilityEngine,
            )
            eligibility_engine = Step3UniversalEligibilityEngine(db_manager=self._db)
        self._eligibility_engine = eligibility_engine

        # Setup-mode M14 — Step 4 per-setup validation + trade plan.
        if setup_validation_engine is None:
            from app.services.analysis.step4_setup_validation_engine import (
                Step4SetupValidationEngine,
            )
            setup_validation_engine = Step4SetupValidationEngine(db_manager=self._db)
        self._setup_validation_engine = setup_validation_engine

        # Setup-mode M15 — Step 5 risk labeling + proposals.
        if proposal_engine is None:
            from app.services.proposal.step5_proposal_engine import Step5ProposalEngine
            proposal_engine = Step5ProposalEngine(db_manager=self._db)
        self._proposal_engine = proposal_engine

        if outcome_creator is None:
            from app.services.outcomes.outcome_queue import OutcomeQueueCreator
            outcome_creator = OutcomeQueueCreator(db_manager=self._db)
        self._outcome_creator = outcome_creator

        if outcome_processor is None:
            from app.services.outcomes.outcome_queue import OutcomeQueueProcessor
            outcome_processor = OutcomeQueueProcessor(db_manager=self._db)
        self._outcome_processor = outcome_processor

        # Diagnostics service — writes pipeline_run_diagnostics rows.
        if diagnostics_service is None:
            from app.services.diagnostics.funnel_diagnostics import (
                SetupModeFunnelDiagnosticsService,
            )
            diagnostics_service = SetupModeFunnelDiagnosticsService(db_manager=self._db)
        self._diagnostics_service = diagnostics_service

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
        run_id: str | None = None,
    ) -> ServiceResult:
        """Execute one daily setup-mode pipeline run; always returns a ServiceResult."""
        started = time.monotonic()
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        # Pre-DB validation (includes trading-day guard — no DB writes yet).
        pre_error = self._validate_inputs(run_type, db_role, resume_from,
                                          run_date=run_date)
        if pre_error is not None:
            log.error("pre-db validation failed: %s", pre_error)
            return self._result(
                status=service_result.STATUS_FAILED,
                run_id=run_id, run_date=run_date, run_type=run_type, db_role=db_role,
                steps_completed=[], failed_step=None, error=pre_error,
                duration_sec=time.monotonic() - started, warnings=[],
            )

        # Lock acquire.
        lock_error = self._acquire_lock(run_id, db_role, log)
        if lock_error is not None:
            return self._result(
                status=service_result.STATUS_FAILED,
                run_id=run_id, run_date=run_date, run_type=run_type, db_role=db_role,
                steps_completed=[], failed_step=None, error=lock_error,
                duration_sec=time.monotonic() - started, warnings=[],
            )

        steps_completed: list[str] = []
        warnings: list[str] = []
        failed_step: str | None = None
        error: str | None = None
        status = service_result.STATUS_SUCCESS
        try:
            # Already-run guard.
            try:
                already = self._already_run(run_date, db_role)
            except Exception as exc:  # noqa: BLE001
                error = f"failed to query pipeline_runs: {exc}"
                log.error(error)
                return self._result(
                    status=service_result.STATUS_FAILED,
                    run_id=run_id, run_date=run_date, run_type=run_type, db_role=db_role,
                    steps_completed=[], failed_step=None, error=error,
                    duration_sec=time.monotonic() - started, warnings=[],
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
                    run_id=run_id, run_date=run_date, run_type=run_type, db_role=db_role,
                    steps_completed=[], failed_step=None, error=error,
                    duration_sec=time.monotonic() - started, warnings=[],
                )
            if already is not None and force_rerun:
                log.warning(
                    "run_date already succeeded but force_rerun=True; continuing (prev=%s)",
                    already[0],
                )

            # Lifecycle row.
            try:
                self._write(db_role, _INSERT_RUNNING, [run_id, run_date, run_type])
            except Exception as exc:  # noqa: BLE001
                error = f"failed to insert pipeline_runs running row: {exc}"
                log.error(error)
                return self._result(
                    status=service_result.STATUS_FAILED,
                    run_id=run_id, run_date=run_date, run_type=run_type, db_role=db_role,
                    steps_completed=[], failed_step=None, error=error,
                    duration_sec=time.monotonic() - started, warnings=[],
                )

            resume_index = STEP_NAMES.index(resume_from) if resume_from is not None else 0
            step_timings: dict[str, float] = {}

            # Cleanup stale calculated outputs for this date before recalculating.
            # Runs in one transaction; scoped to steps that will actually execute.
            try:
                self.cleanup_calculated_outputs_for_date(db_role, run_date, resume_from)
                log.info(
                    "cleanup_calculated_outputs: cleared signal_date=%s resume_from=%s",
                    run_date, resume_from,
                )
            except Exception as exc:  # noqa: BLE001
                failed_step = "cleanup_calculated_outputs"
                error = f"cleanup_calculated_outputs failed: {type(exc).__name__}: {exc}"
                log.error(error)
                status = service_result.STATUS_FAILED

            # Linear steps 1–8.
            if status != service_result.STATUS_FAILED:
                linear_steps = (
                    ("benchmark_etf_ingestion",       self._step_benchmark),
                    ("universe_ingestion",             self._step_universe),
                    ("earnings_calendar_refresh",      self._step_earnings),
                    ("price_ingestion",                self._step_price),
                    ("validation",                     self._step_validation),
                    ("mutation_detection",             self._step_mutation),
                    ("feature_calculation",            self._step_features),
                    ("market_regime_classification",   self._step_market_regime),
                )
                for name, func in linear_steps:
                    idx = STEP_NAMES.index(name)
                    if idx < resume_index:
                        log.info("step %s skipped (resume_from=%s)", name, resume_from)
                        continue
                    _t0 = time.monotonic()
                    outcome = self._safe_step(name, func, log, run_date, db_role, run_id)
                    step_timings[name] = time.monotonic() - _t0
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

            # Setup-mode pipeline steps 8–10 (once each per signal_date).
            if status != service_result.STATUS_FAILED:
                setup_steps = (
                    ("step3_universal_eligibility", self._step_step3),
                    ("step4_setup_validation",      self._step_step4),
                    ("step5_proposals",             self._step_step5),
                    ("outcome_queue_creation",      self._step_enqueue),
                    ("outcome_processing",          self._step_process),
                )
                for name, func in setup_steps:
                    idx = STEP_NAMES.index(name)
                    if idx < resume_index:
                        log.info("step %s skipped (resume_from=%s)", name, resume_from)
                        continue
                    _t0 = time.monotonic()
                    outcome = self._safe_step(name, func, log, run_date, db_role, run_id)
                    step_timings[name] = time.monotonic() - _t0
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

            # Funnel diagnostics — mandatory, persisted, non-blocking on error.
            if status != service_result.STATUS_FAILED:
                diag_idx = STEP_NAMES.index("step5_proposals")
                if resume_index <= diag_idx:
                    self._run_diagnostics(run_date, db_role, run_id, log, warnings, step_timings)

            # Recoverable tail steps.
            if status != service_result.STATUS_FAILED:
                tail_steps = (
                    ("dashboard_materialization", self._step_dashboard),
                )
                for name, func in tail_steps:
                    idx = STEP_NAMES.index(name)
                    if idx < resume_index:
                        log.info("step %s skipped (resume_from=%s)", name, resume_from)
                        continue
                    _t0 = time.monotonic()
                    outcome = self._safe_step(name, func, log, run_date, db_role, run_id)
                    step_timings[name] = time.monotonic() - _t0
                    warnings.extend(outcome.warnings)
                    if not outcome.ok or outcome.warnings:
                        status = service_result.STATUS_SUCCESS_WITH_WARNINGS
                    db_err = self._record_step(name, steps_completed, db_role, run_id, log)
                    if db_err:
                        error = db_err
                        status = service_result.STATUS_FAILED
                        break

            # Finalize run row.
            duration = time.monotonic() - started
            try:
                if status == service_result.STATUS_FAILED:
                    self._write(
                        db_role, _UPDATE_FAILED,
                        [duration, error or f"critical failure at {failed_step}", run_id],
                    )
                else:
                    self._write(
                        db_role, _UPDATE_SUCCESS,
                        [status, duration, json.dumps(steps_completed), run_id],
                    )
            except Exception as exc:  # noqa: BLE001
                log.error("failed to finalize pipeline_runs: %s", exc)
                error = f"failed to finalize pipeline_runs: {exc}"
                status = service_result.STATUS_FAILED

            return self._result(
                status=status,
                run_id=run_id, run_date=run_date, run_type=run_type, db_role=db_role,
                steps_completed=steps_completed, failed_step=failed_step, error=error,
                duration_sec=duration, warnings=warnings,
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
        run_date: "date | None" = None,
    ) -> str | None:
        if run_type not in ALLOWED_RUN_TYPES:
            return f"invalid run_type {run_type!r}; valid: {sorted(ALLOWED_RUN_TYPES)}"
        if db_role not in ALLOWED_DB_ROLES:
            return f"invalid db_role {db_role!r}; valid: {sorted(ALLOWED_DB_ROLES)}"
        if resume_from is not None and resume_from not in STEP_NAMES:
            return f"invalid resume_from {resume_from!r}; valid: {list(STEP_NAMES)}"
        if run_date is not None and not PipelineOrchestrator._is_trading_day(run_date):
            return (
                f"run_date {run_date} is not a NYSE trading day; "
                "the daily pipeline only runs on trading days. "
                "Use a valid trading date or check the NYSE calendar."
            )
        return None

    @staticmethod
    def _is_trading_day(day: "date") -> bool:
        """Return True if *day* is a NYSE trading day.

        Delegates to :func:`app.utils.trading_calendar.is_trading_day` when
        available; falls back to weekday-only check (no holiday exclusion) so
        the guard never hard-crashes on a missing calendar dependency.
        """
        try:
            from app.utils.trading_calendar import is_trading_day
            return is_trading_day(day)
        except Exception:  # noqa: BLE001
            return day.weekday() < 5  # Mon–Fri weekday fallback

    # ------------------------------------------------------------------ #
    # Lock protocol.
    # ------------------------------------------------------------------ #
    def _acquire_lock(self, run_id: str, db_role: str, log: Any) -> str | None:
        try:
            row = self._read_lock(db_role)
        except Exception as exc:  # noqa: BLE001
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
                    "overwriting stale pipeline lock (stale run_id=%s, heartbeat_at=%s)",
                    lock_run_id, heartbeat_at,
                )
        try:
            self._write(db_role, _UPSERT_LOCK, [run_id])
        except Exception as exc:  # noqa: BLE001
            msg = f"failed to acquire pipeline lock: {exc}"
            log.error(msg)
            return msg
        return None

    @staticmethod
    def _is_stale(heartbeat_at: Any) -> bool:
        if heartbeat_at is None:
            return True
        if not isinstance(heartbeat_at, datetime):
            return True
        return (datetime.now() - heartbeat_at) > timedelta(seconds=LOCK_STALE_SECONDS)

    def _read_lock(self, db_role: str) -> tuple[Any, Any, Any] | None:
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
        try:
            self._write(db_role, _RELEASE_LOCK, [])
        except Exception as exc:  # noqa: BLE001
            log.error("failed to release pipeline lock: %s", exc)

    # ------------------------------------------------------------------ #
    # Already-run guard.
    # ------------------------------------------------------------------ #
    def _already_run(self, run_date: date, db_role: str) -> tuple[Any, Any] | None:
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
    # Calculated-output cleanup.
    # ------------------------------------------------------------------ #
    def cleanup_calculated_outputs_for_date(
        self,
        db_role: str,
        signal_date: date,
        resume_from: str | None = None,
    ) -> None:
        """Delete calculated outputs for *signal_date* in one atomic transaction.

        Only tables corresponding to steps that will actually execute are
        cleaned (respects *resume_from*). Safe on a clean DB — each DELETE
        is a no-op when the date has no rows.

        Cleaned tables (date-scoped only; raw data is never touched):
          daily_features     (feature_date = signal_date)
          step3_candidates   (signal_date = signal_date)
          step4_analysis     (signal_date = signal_date)
          step5_proposals    (signal_date = signal_date)
        """
        resume_idx = (
            STEP_NAMES.index(resume_from)
            if resume_from is not None and resume_from in STEP_NAMES
            else 0
        )

        deletes: list[tuple[str, list[Any]]] = []
        if resume_idx <= STEP_NAMES.index("feature_calculation"):
            deletes.append((_DELETE_DAILY_FEATURES, [signal_date]))
        if resume_idx <= STEP_NAMES.index("step3_universal_eligibility"):
            deletes.append((_DELETE_STEP3, [signal_date]))
        if resume_idx <= STEP_NAMES.index("step4_setup_validation"):
            deletes.append((_DELETE_STEP4, [signal_date]))
        if resume_idx <= STEP_NAMES.index("step5_proposals"):
            deletes.append((_DELETE_STEP5, [signal_date]))

        if not deletes:
            return

        connection = self._db.connect(db_role, read_only=False)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for sql, params in deletes:
                    connection.execute(sql, params)
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # DB write helper.
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
        steps_completed.append(name)
        try:
            self._write(db_role, _UPDATE_STEPS, [json.dumps(steps_completed), run_id])
            self._write(db_role, _HEARTBEAT_LOCK, [])
        except Exception as exc:  # noqa: BLE001
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
        critical = name in CRITICAL_STEPS
        try:
            result = func(run_date, db_role, run_id, log)
        except Exception as exc:  # noqa: BLE001
            log.error("step %s raised: %s", name, exc)
            if critical:
                return _StepOutcome(False, True, [], f"{name} raised: {exc}")
            return _StepOutcome(False, False, [f"{name} raised (recoverable): {exc}"], None)
        return self._classify(name, result, critical, log)

    @staticmethod
    def _classify(name: str, result: Any, critical: bool, log: Any) -> _StepOutcome:
        status = getattr(result, "status", None)
        errs = list(getattr(result, "errors", []) or [])
        warns = list(getattr(result, "warnings", []) or [])
        if status == service_result.STATUS_FAILED:
            if critical:
                log.error("critical step %s failed: %s", name, errs)
                return _StepOutcome(False, True, warns, errs[0] if errs else f"{name} failed")
            log.warning("recoverable step %s failed: %s", name, errs)
            msg = errs[0] if errs else f"{name} failed (recoverable)"
            return _StepOutcome(False, False, warns + [msg], None)
        if status == service_result.STATUS_SUCCESS_WITH_WARNINGS:
            return _StepOutcome(True, False, warns, None)
        return _StepOutcome(True, False, warns, None)

    # ------------------------------------------------------------------ #
    # Linear step bodies.
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

    def _step_earnings(self, run_date: date, db_role: str, run_id: str, log: Any):
        """Ensure the earnings calendar is refreshed for *run_date*.

        Skips all provider calls if any row was already written today.
        Otherwise fetches earnings for every active stock ticker via
        ``provider.get_earnings()`` and batch-upserts results in one write
        connection. Step is recoverable: failure leaves ``days_to_earnings_bd``
        NULL (same as the pre-step state).
        """
        import gc

        # Check whether the calendar was already refreshed today.
        conn = self._db.connect(db_role, read_only=True)
        try:
            cursor = conn.execute(_SQL_EARNINGS_CHECK, [run_date])
            row = cursor.fetchone()
            already_refreshed = (row[0] > 0) if row else False
        finally:
            conn.close()

        if already_refreshed:
            log.info(
                "earnings_calendar_refresh: calendar already updated today (%s); skipping",
                run_date,
            )
            return ServiceResult(
                status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id, rows_processed=0,
                warnings=["earnings_calendar already refreshed today; skipped"],
            )

        # Load active stock tickers.
        conn = self._db.connect(db_role, read_only=True)
        try:
            cursor = conn.execute(_SQL_EARNINGS_TICKERS)
            tickers: list[str] = [r[0] for r in cursor.fetchall()]
        finally:
            conn.close()

        if not tickers:
            log.warning("earnings_calendar_refresh: no active stock tickers found")
            return ServiceResult(
                status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id, rows_processed=0,
                warnings=["earnings_calendar_refresh: no active stock tickers"],
            )

        # Fetch earnings from provider and collect upsert rows.
        upsert_rows: list[tuple] = []
        fetch_warnings: list[str] = []
        for ticker in tickers:
            try:
                result = self._provider.get_earnings(ticker)
            except Exception as exc:  # noqa: BLE001
                fetch_warnings.append(f"get_earnings({ticker}) raised: {exc}")
                continue
            if getattr(result, "status", None) == service_result.STATUS_FAILED:
                fetch_warnings.append(
                    f"get_earnings({ticker}) failed: "
                    f"{(result.errors or ['unknown'])[0]}"
                )
                continue
            events = (result.metadata or {}).get("events") or []
            for evt in events:
                upsert_rows.append((
                    evt.ticker,
                    evt.earnings_date,
                    evt.session,
                    evt.source_provider,
                    evt.confidence,
                ))

        if not upsert_rows:
            msg = f"earnings_calendar_refresh: 0 events fetched for {len(tickers)} tickers"
            log.warning(msg)
            return ServiceResult(
                status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id, rows_processed=0,
                warnings=[msg] + fetch_warnings,
            )

        # Batch-upsert in one write connection.
        gc.collect()  # release Windows mmap regions from read connections
        conn = self._db.connect(db_role, read_only=False)
        try:
            conn.execute("BEGIN TRANSACTION")
            try:
                for row in upsert_rows:
                    conn.execute(_SQL_EARNINGS_UPSERT, list(row))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        finally:
            conn.close()

        all_warnings = fetch_warnings
        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if all_warnings else service_result.STATUS_SUCCESS
        )
        log.info(
            "earnings_calendar_refresh: upserted %d events for %d tickers",
            len(upsert_rows), len(tickers),
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=len(upsert_rows),
            warnings=all_warnings,
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
        return self._regime_engine.classify(
            start_date=run_date,
            end_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    # ------------------------------------------------------------------ #
    # Setup-mode pipeline step bodies (M13 → M14 → M15).
    # ------------------------------------------------------------------ #
    def _step_step3(self, run_date: date, db_role: str, run_id: str, log: Any):
        """Step 3 — M13 universal eligibility + setup routing (once per signal_date)."""
        return self._eligibility_engine.run(
            signal_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_step4(self, run_date: date, db_role: str, run_id: str, log: Any):
        """Step 4 — M14 setup validation + trade plan (iterates setup configs internally)."""
        return self._setup_validation_engine.run(
            signal_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_step5(self, run_date: date, db_role: str, run_id: str, log: Any):
        """Step 5 — M15 risk labeling + proposals (once per signal_date)."""
        return self._proposal_engine.propose(
            signal_date=run_date,
            db_role=db_role,
            run_id=run_id,
        )

    def _step_enqueue(self, run_date: date, db_role: str, run_id: str, log: Any):
        """Outcome queue creation — setup-mode compatibility shim.

        Calls M16 OutcomeQueueCreator once per active setup_config_id.
        Treats the whole step as recoverable if any individual call fails.
        """
        from app.services.config.config_service import ConfigService
        cs = ConfigService(db_manager=self._db)
        try:
            active_result = cs.get_all_active_setup_configs(db_role)
            configs_by_type: dict[str, dict] = (
                active_result.metadata.get("configs_by_type") or {}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("enqueue: could not load setup configs: %s", exc)
            configs_by_type = {}

        if not configs_by_type:
            log.warning(
                "enqueue: no active setup configs found for db_role=%s; skipping", db_role
            )
            return ServiceResult(
                status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id, rows_processed=0,
                warnings=["no active setup configs; outcome queue skipped"],
            )

        # configs_by_id: {config_id -> config_dict}
        configs_by_id: dict[str, dict] = (
            active_result.metadata.get("configs_by_id") or {}
        )

        # Build a minimal config dict with the simulation block M16 needs.
        _SIM_BLOCK = {
            "simulation": {
                "slippage_bps": 10,
                "horizons_bd": list(constants.OUTCOME_HORIZONS_BD),
            }
        }
        all_warnings: list[str] = []
        total_enqueued = 0
        for setup_config_id, cfg in configs_by_id.items():
            merged_cfg = dict(cfg)
            merged_cfg.setdefault("simulation", _SIM_BLOCK["simulation"])
            try:
                r = self._outcome_creator.enqueue(
                    signal_date=run_date,
                    setup_config_id=setup_config_id,
                    setup_config=merged_cfg,
                    db_role=db_role,
                    run_id=run_id,
                )
                total_enqueued += getattr(r, "rows_processed", 0)
                if r.warnings:
                    all_warnings.extend(r.warnings)
            except Exception as exc:  # noqa: BLE001
                all_warnings.append(f"enqueue failed for {setup_config_id}: {exc}")

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if all_warnings
            else service_result.STATUS_SUCCESS
        )
        return ServiceResult(
            status=status, run_id=run_id, rows_processed=total_enqueued,
            warnings=all_warnings,
        )

    def _step_process(self, run_date: date, db_role: str, run_id: str, log: Any):
        """Outcome processing — setup-mode compatibility shim.

        Calls M16 OutcomeQueueProcessor with a minimal config dict.
        """
        _SIM_BLOCK = {
            "simulation": {
                "slippage_bps": 10,
                "horizons_bd": list(constants.OUTCOME_HORIZONS_BD),
            }
        }
        try:
            return self._outcome_processor.process(
                run_date=run_date,
                setup_config=_SIM_BLOCK,
                db_role=db_role,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("outcome_processing raised (recoverable): %s", exc)
            return ServiceResult(
                status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id, rows_processed=0,
                warnings=[f"outcome_processing raised (recoverable): {exc}"],
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
    # Funnel diagnostics — mandatory, non-blocking on error.
    # ------------------------------------------------------------------ #
    def _run_diagnostics(
        self,
        run_date: date,
        db_role: str,
        run_id: str,
        log: Any,
        warnings: list[str],
        step_timings: dict[str, float] | None = None,
    ) -> None:
        """Compute and persist setup-mode funnel diagnostics.

        Non-blocking: failure adds a warning and continues.
        """
        log.info("funnel diagnostics start signal_date=%s", run_date)
        try:
            result = self._diagnostics_service.run(
                signal_date=run_date,
                db_role=db_role,
                run_id=run_id,
                step_timings=step_timings or {},
            )
            if result.status == service_result.STATUS_FAILED:
                msg = (
                    f"funnel diagnostics failed: "
                    f"{'; '.join(result.errors) if result.errors else 'unknown error'}"
                )
                log.warning(msg)
                warnings.append(msg)
            else:
                log.info(
                    "funnel diagnostics complete: %d metric rows written",
                    result.rows_processed,
                )
                if result.warnings:
                    warnings.extend(result.warnings)
        except Exception as exc:  # noqa: BLE001
            msg = f"funnel diagnostics raised: {exc}"
            log.warning(msg)
            warnings.append(msg)

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
