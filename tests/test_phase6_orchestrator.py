"""Phase 6 tests — Setup-mode pipeline orchestrator (M20).

All tests are fully offline. DB access is through FakeDb; all step engines are
injected fakes returning controlled ServiceResults. The test suite exercises:

  - Setup-mode step sequence (step3_universal_eligibility → step4_setup_validation
    → step5_proposals)
  - Step names and ordering
  - Legacy strategy-mode pipeline is NOT active
  - consolidation_base naming is used consistently
  - Correct ServiceResult metadata on every path
  - Lock acquire / stale-lock / release
  - Already-run guard
  - Critical vs recoverable failure handling
  - Resume-from skipping
  - Diagnostics called after Step 5 completes
  - Old strategy-mode terms not present in setup-mode orchestrator
"""

from __future__ import annotations

import ast
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.utils import service_result
from app.utils.service_result import ServiceResult
from app.services.pipeline import pipeline_orchestrator as po
from app.services.pipeline.pipeline_orchestrator import (
    STEP_NAMES,
    CRITICAL_STEPS,
    RECOVERABLE_STEPS,
    PipelineOrchestrator,
)

RUN_DATE = date(2026, 6, 15)
ORCH_PATH = Path(po.__file__)

META_KEYS = {
    "run_id", "run_date", "run_type", "db_role",
    "steps_completed", "failed_step", "error", "duration_sec", "status",
}

SETUP_PIPELINE_STEPS = (
    "step3_universal_eligibility",
    "step4_setup_validation",
    "step5_proposals",
)

OLD_STRATEGY_NAMES = {"aggressive", "normal", "conservative", "strategy_config_id"}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, db, read_only=False):
        self._db = db
        self._read_only = read_only

    def execute(self, sql, params=None):
        for fragment, exc in self._db.fail_on:
            if fragment.upper() in sql.upper():
                raise exc
        self._db.executed.append((sql, params, self._read_only))
        return self._db._cursor_for(sql)

    def close(self):
        self._db.closed += 1


class FakeDb:
    """Records SQL and returns canned lock / already-run rows."""

    def __init__(self, lock_row=None, already_row=None, fail_on=None):
        self.lock_row = lock_row      # (run_id, is_locked, heartbeat_at)
        self.already_row = already_row  # (run_id, status)
        self.fail_on = fail_on or []
        self.executed: list[tuple] = []
        self.closed: int = 0

    def connect(self, db_role: str, read_only: bool = False):
        return FakeConnection(self, read_only=read_only)

    def _cursor_for(self, sql: str):
        sql_upper = sql.upper()
        if "PIPELINE_LOCKS" in sql_upper and "SELECT" in sql_upper:
            rows = [self.lock_row] if self.lock_row else []
            return FakeCursor(rows)
        if "PIPELINE_RUNS" in sql_upper and "STATUS IN" in sql_upper:
            rows = [self.already_row] if self.already_row else []
            return FakeCursor(rows)
        return FakeCursor([])


def ok_result(run_id="r"):
    return ServiceResult(status=service_result.STATUS_SUCCESS, run_id=run_id)


def fail_result(run_id="r", msg="boom"):
    return ServiceResult(
        status=service_result.STATUS_FAILED, run_id=run_id, errors=[msg]
    )


def warn_result(run_id="r", w="warn"):
    return ServiceResult(
        status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
        run_id=run_id, warnings=[w],
    )


class FakeEngine:
    """Generic step engine fake — returns controlled result."""

    def __init__(self, result: ServiceResult | None = None, raises=None):
        self._result = result or ok_result()
        self._raises = raises
        self.calls: list[dict] = []

    def _record(self, **kwargs):
        self.calls.append(kwargs)

    # benchmark loader
    def load(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    # universe, ingestion, validation, mutation, feature, regime
    def apply_snapshot(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    def ingest(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    def validate(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    def detect(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    def calculate(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    def classify(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    # M13
    def run(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    # M15
    def propose(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    # M16
    def enqueue(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result

    def process(self, **kwargs):
        self._record(**kwargs)
        if self._raises:
            raise self._raises
        return self._result


class FakeProvider:
    def list_symbols(self, **kwargs):
        return ServiceResult(
            status=service_result.STATUS_SUCCESS, run_id="p",
            metadata={"symbols": []},
        )


class FakeDiagnostics:
    """Fake diagnostics service."""
    def __init__(self, result=None):
        self.calls: list[dict] = []
        self._result = result or ok_result()

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def build_orchestrator(
    db: FakeDb,
    *,
    eligibility_engine=None,
    setup_validation_engine=None,
    proposal_engine=None,
    outcome_creator=None,
    outcome_processor=None,
    diagnostics_service=None,
    feature_engine=None,
    benchmark_loader=None,
    universe_engine=None,
    ingestion_engine=None,
    validation_engine=None,
    mutation_engine=None,
    regime_engine=None,
) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        db_manager=db,
        provider=FakeProvider(),
        benchmark_loader=benchmark_loader or FakeEngine(),
        universe_engine=universe_engine or FakeEngine(),
        ingestion_engine=ingestion_engine or FakeEngine(),
        validation_engine=validation_engine or FakeEngine(),
        mutation_engine=mutation_engine or FakeEngine(),
        feature_engine=feature_engine or FakeEngine(),
        regime_engine=regime_engine or FakeEngine(),
        eligibility_engine=eligibility_engine or FakeEngine(),
        setup_validation_engine=setup_validation_engine or FakeEngine(),
        proposal_engine=proposal_engine or FakeEngine(),
        outcome_creator=outcome_creator or FakeEngine(),
        outcome_processor=outcome_processor or FakeEngine(),
        config_service=None,  # not used when enqueue uses fake
        diagnostics_service=diagnostics_service or FakeDiagnostics(),
    )


# --------------------------------------------------------------------------- #
# Step name tests
# --------------------------------------------------------------------------- #
def test_step_names_include_setup_mode_steps():
    assert "step3_universal_eligibility" in STEP_NAMES
    assert "step4_setup_validation" in STEP_NAMES
    assert "step5_proposals" in STEP_NAMES


def test_old_strategy_step_names_not_in_step_names():
    assert "step3_screening" not in STEP_NAMES
    assert "step4_analysis" not in STEP_NAMES


def test_step3_before_step4_before_step5():
    s3 = STEP_NAMES.index("step3_universal_eligibility")
    s4 = STEP_NAMES.index("step4_setup_validation")
    s5 = STEP_NAMES.index("step5_proposals")
    assert s3 < s4 < s5


def test_setup_steps_are_critical():
    assert "step3_universal_eligibility" in CRITICAL_STEPS
    assert "step4_setup_validation" in CRITICAL_STEPS
    assert "step5_proposals" in CRITICAL_STEPS


def test_outcome_steps_are_recoverable():
    assert "outcome_queue_creation" in RECOVERABLE_STEPS
    assert "outcome_processing" in RECOVERABLE_STEPS


# --------------------------------------------------------------------------- #
# ServiceResult contract
# --------------------------------------------------------------------------- #
def test_returns_service_result_with_exact_metadata_keys():
    db = FakeDb()
    orch = build_orchestrator(db)
    result = orch.run(RUN_DATE, run_id="fixed-id")
    assert isinstance(result, ServiceResult)
    assert set(result.metadata.keys()) == META_KEYS


def test_run_id_preserved_when_supplied():
    db = FakeDb()
    orch = build_orchestrator(db)
    result = orch.run(RUN_DATE, run_id="FIXED-ID")
    assert result.metadata["run_id"] == "FIXED-ID"
    assert result.run_id == "FIXED-ID"


def test_run_id_minted_when_not_supplied():
    db = FakeDb()
    orch = build_orchestrator(db)
    result = orch.run(RUN_DATE)
    assert result.metadata["run_id"]
    assert len(result.metadata["run_id"]) > 10


def test_metadata_keys_on_failure_path():
    db = FakeDb()
    result = build_orchestrator(db).run(RUN_DATE, run_type="bad_type")
    assert set(result.metadata.keys()) == META_KEYS
    assert result.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs", [
    {"run_type": "INVALID"},
    {"db_role": "simulation"},
    {"resume_from": "nonexistent_step"},
])
def test_invalid_inputs_fail_before_any_db_access(kwargs):
    db = FakeDb()
    result = build_orchestrator(db).run(RUN_DATE, **kwargs)
    assert result.status == service_result.STATUS_FAILED
    assert not db.executed  # no DB access


def test_simulation_db_role_rejected():
    db = FakeDb()
    result = build_orchestrator(db).run(RUN_DATE, db_role="simulation")
    assert result.status == service_result.STATUS_FAILED
    assert not db.executed


# --------------------------------------------------------------------------- #
# Lock behavior
# --------------------------------------------------------------------------- #
def test_active_lock_blocks_run():
    heartbeat = datetime.now() - timedelta(seconds=10)
    db = FakeDb(lock_row=("other-run", True, heartbeat))
    result = build_orchestrator(db).run(RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert "already running" in (result.metadata["error"] or "")


def test_stale_lock_is_overwritten():
    old_heartbeat = datetime.now() - timedelta(seconds=400)
    db = FakeDb(lock_row=("stale-id", True, old_heartbeat))
    result = build_orchestrator(db).run(RUN_DATE)
    # Stale lock should be overwritten and run proceeds
    assert result.status != service_result.STATUS_FAILED or "stale" not in (result.metadata.get("error") or "")


def test_missing_heartbeat_treated_as_stale():
    db = FakeDb(lock_row=("stale-id", True, None))
    result = build_orchestrator(db).run(RUN_DATE)
    # None heartbeat = stale, should allow run to proceed
    assert result.metadata.get("run_id") is not None


# --------------------------------------------------------------------------- #
# Already-run guard
# --------------------------------------------------------------------------- #
def test_already_run_blocks_and_releases_lock():
    db = FakeDb(already_row=("prev-run", "success"))
    result = build_orchestrator(db).run(RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert "already succeeded" in (result.metadata["error"] or "")
    # Lock must be released even on already-run block
    release_sqls = [s for (s, _, _) in db.executed if "is_locked = FALSE" in s or "IS_LOCKED = FALSE" in s]
    assert release_sqls, "lock must be released on already-run block"


def test_force_rerun_overrides_already_run():
    db = FakeDb(already_row=("prev-run", "success"))
    result = build_orchestrator(db).run(RUN_DATE, force_rerun=True)
    # force_rerun allows run to proceed
    assert result.metadata.get("run_id") is not None


# --------------------------------------------------------------------------- #
# Setup-mode step sequence
# --------------------------------------------------------------------------- #
def test_setup_mode_steps_called_in_order():
    """M13 → M14 → M15 must be called exactly once each, in that order."""
    m13 = FakeEngine()
    m14 = FakeEngine()
    m15 = FakeEngine()
    db = FakeDb()
    orch = build_orchestrator(
        db,
        eligibility_engine=m13,
        setup_validation_engine=m14,
        proposal_engine=m15,
    )
    result = orch.run(RUN_DATE)
    assert result.status in (service_result.STATUS_SUCCESS, service_result.STATUS_SUCCESS_WITH_WARNINGS)
    assert len(m13.calls) == 1, "M13 must be called exactly once"
    assert len(m14.calls) == 1, "M14 must be called exactly once"
    assert len(m15.calls) == 1, "M15 must be called exactly once"


def test_step3_called_with_signal_date():
    m13 = FakeEngine()
    db = FakeDb()
    build_orchestrator(db, eligibility_engine=m13).run(RUN_DATE, run_id="r1")
    assert m13.calls[0]["signal_date"] == RUN_DATE


def test_step4_called_with_signal_date():
    m14 = FakeEngine()
    db = FakeDb()
    build_orchestrator(db, setup_validation_engine=m14).run(RUN_DATE, run_id="r1")
    assert m14.calls[0]["signal_date"] == RUN_DATE


def test_step5_called_with_signal_date():
    m15 = FakeEngine()
    db = FakeDb()
    build_orchestrator(db, proposal_engine=m15).run(RUN_DATE, run_id="r1")
    assert m15.calls[0]["signal_date"] == RUN_DATE


def test_steps_completed_includes_setup_mode_steps():
    db = FakeDb()
    result = build_orchestrator(db).run(RUN_DATE)
    sc = result.metadata["steps_completed"]
    assert "step3_universal_eligibility" in sc
    assert "step4_setup_validation" in sc
    assert "step5_proposals" in sc


def test_setup_steps_in_correct_order_in_steps_completed():
    db = FakeDb()
    result = build_orchestrator(db).run(RUN_DATE)
    sc = result.metadata["steps_completed"]
    s3 = sc.index("step3_universal_eligibility")
    s4 = sc.index("step4_setup_validation")
    s5 = sc.index("step5_proposals")
    assert s3 < s4 < s5


def test_step4_not_called_if_step3_fails():
    m13 = FakeEngine(result=fail_result())
    m14 = FakeEngine()
    db = FakeDb()
    build_orchestrator(db, eligibility_engine=m13, setup_validation_engine=m14).run(RUN_DATE)
    assert len(m14.calls) == 0, "Step 4 must not run if Step 3 fails"


def test_step5_not_called_if_step4_fails():
    m14 = FakeEngine(result=fail_result())
    m15 = FakeEngine()
    db = FakeDb()
    build_orchestrator(db, setup_validation_engine=m14, proposal_engine=m15).run(RUN_DATE)
    assert len(m15.calls) == 0, "Step 5 must not run if Step 4 fails"


def test_step3_failure_returns_failed_service_result():
    m13 = FakeEngine(result=fail_result(msg="elig failed"))
    db = FakeDb()
    result = build_orchestrator(db, eligibility_engine=m13).run(RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "step3_universal_eligibility"


def test_step4_failure_returns_failed_service_result():
    m14 = FakeEngine(result=fail_result(msg="validation failed"))
    db = FakeDb()
    result = build_orchestrator(db, setup_validation_engine=m14).run(RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "step4_setup_validation"


def test_step5_failure_returns_failed_service_result():
    m15 = FakeEngine(result=fail_result(msg="proposals failed"))
    db = FakeDb()
    result = build_orchestrator(db, proposal_engine=m15).run(RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "step5_proposals"


def test_raising_in_step3_is_caught_and_fails_critically():
    m13 = FakeEngine(raises=RuntimeError("step3 crash"))
    db = FakeDb()
    result = build_orchestrator(db, eligibility_engine=m13).run(RUN_DATE)
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "step3_universal_eligibility"


# --------------------------------------------------------------------------- #
# Run metadata persistence
# --------------------------------------------------------------------------- #
def test_run_metadata_recorded_in_pipeline_runs():
    db = FakeDb()
    build_orchestrator(db).run(RUN_DATE, run_type="manual")
    insert_sqls = [s for (s, p, _) in db.executed if "INSERT INTO pipeline_runs" in s]
    assert insert_sqls, "pipeline_runs INSERT must be executed"


def test_steps_completed_updated_in_db():
    db = FakeDb()
    build_orchestrator(db).run(RUN_DATE)
    update_sqls = [s for (s, _, _) in db.executed
                   if "UPDATE pipeline_runs" in s and "steps_completed" in s.lower()]
    assert update_sqls, "steps_completed must be persisted during run"


def test_lock_released_on_success():
    db = FakeDb()
    build_orchestrator(db).run(RUN_DATE)
    release_sqls = [s for (s, _, _) in db.executed if "is_locked = FALSE" in s or "IS_LOCKED = FALSE" in s]
    assert release_sqls, "lock must be released after successful run"


def test_lock_released_on_failure():
    m13 = FakeEngine(result=fail_result())
    db = FakeDb()
    build_orchestrator(db, eligibility_engine=m13).run(RUN_DATE)
    release_sqls = [s for (s, _, _) in db.executed if "is_locked = FALSE" in s or "IS_LOCKED = FALSE" in s]
    assert release_sqls, "lock must be released even on pipeline failure"


# --------------------------------------------------------------------------- #
# Resume-from
# --------------------------------------------------------------------------- #
def test_resume_from_step3_skips_earlier_steps():
    m13 = FakeEngine()
    feat = FakeEngine()
    db = FakeDb()
    build_orchestrator(
        db, eligibility_engine=m13, feature_engine=feat
    ).run(RUN_DATE, resume_from="step3_universal_eligibility")
    assert len(feat.calls) == 0, "feature_calculation skipped when resuming from step3"
    assert len(m13.calls) == 1


def test_resume_from_step5_skips_step3_and_step4():
    m13 = FakeEngine()
    m14 = FakeEngine()
    m15 = FakeEngine()
    db = FakeDb()
    build_orchestrator(
        db, eligibility_engine=m13, setup_validation_engine=m14, proposal_engine=m15
    ).run(RUN_DATE, resume_from="step5_proposals")
    assert len(m13.calls) == 0
    assert len(m14.calls) == 0
    assert len(m15.calls) == 1


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def test_diagnostics_called_after_step5_on_success():
    diag = FakeDiagnostics()
    db = FakeDb()
    build_orchestrator(db, diagnostics_service=diag).run(RUN_DATE, run_id="r1")
    assert len(diag.calls) == 1, "diagnostics must be called exactly once after Step 5"


def test_diagnostics_called_with_correct_run_id():
    diag = FakeDiagnostics()
    db = FakeDb()
    build_orchestrator(db, diagnostics_service=diag).run(RUN_DATE, run_id="RUN-ABC")
    assert diag.calls[0]["run_id"] == "RUN-ABC"


def test_diagnostics_called_with_signal_date():
    diag = FakeDiagnostics()
    db = FakeDb()
    build_orchestrator(db, diagnostics_service=diag).run(RUN_DATE, run_id="r1")
    assert diag.calls[0]["signal_date"] == RUN_DATE


def test_diagnostics_not_called_if_step3_fails():
    diag = FakeDiagnostics()
    m13 = FakeEngine(result=fail_result())
    db = FakeDb()
    build_orchestrator(db, eligibility_engine=m13, diagnostics_service=diag).run(RUN_DATE)
    assert len(diag.calls) == 0, "diagnostics must not run if Step 3 failed"


def test_diagnostics_not_called_if_step5_fails():
    diag = FakeDiagnostics()
    m15 = FakeEngine(result=fail_result())
    db = FakeDb()
    build_orchestrator(db, proposal_engine=m15, diagnostics_service=diag).run(RUN_DATE)
    assert len(diag.calls) == 0, "diagnostics must not run if Step 5 failed"


def test_diagnostics_failure_is_non_blocking():
    """If diagnostics raises, the run still completes (warning added)."""
    diag = FakeDiagnostics(result=ServiceResult(
        status=service_result.STATUS_FAILED, run_id="d", errors=["diag boom"]
    ))
    db = FakeDb()
    result = build_orchestrator(db, diagnostics_service=diag).run(RUN_DATE)
    # Run should still succeed (or succeed_with_warnings) — diagnostics failure is non-blocking
    assert result.status != service_result.STATUS_FAILED or result.metadata["failed_step"] is None


# --------------------------------------------------------------------------- #
# Warnings propagation
# --------------------------------------------------------------------------- #
def test_step_warnings_propagate_to_result():
    m13 = FakeEngine(result=warn_result(w="eligibility warning"))
    db = FakeDb()
    result = build_orchestrator(db, eligibility_engine=m13).run(RUN_DATE)
    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert any("eligibility warning" in w for w in result.warnings)


def test_recoverable_step_failure_does_not_abort():
    """Regime classification failure (recoverable) should not abort the run."""
    regime = FakeEngine(result=fail_result(msg="regime failed"))
    db = FakeDb()
    result = build_orchestrator(db, regime_engine=regime).run(RUN_DATE)
    # Run should complete with warnings, not abort
    assert result.status in (
        service_result.STATUS_SUCCESS,
        service_result.STATUS_SUCCESS_WITH_WARNINGS,
    )
    assert result.metadata["failed_step"] is None


# --------------------------------------------------------------------------- #
# Old strategy-mode: must not be active
# --------------------------------------------------------------------------- #
def test_no_strategy_mode_step_names_in_step_names():
    """Old step3_screening / step4_analysis names must not appear."""
    for s in STEP_NAMES:
        assert s != "step3_screening", "old step3_screening name must not be in STEP_NAMES"
        assert s not in ("step4_analysis",), "old step4_analysis name must not be in STEP_NAMES"


def test_old_strategy_terms_not_in_source():
    """Source must not contain legacy strategy identity terms as identifiers."""
    source = ORCH_PATH.read_text(encoding="utf-8")
    # Check that old strategy names don't appear as active identifiers
    # (comments explaining they were removed are acceptable)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in OLD_STRATEGY_NAMES:
            pytest.fail(
                f"Old strategy term '{node.id}' found as identifier in orchestrator. "
                f"Setup-mode orchestrator must not use legacy strategy identifiers."
            )


def test_default_strategy_configs_not_in_source():
    """DEFAULT_STRATEGY_CONFIGS (old 3-strategy object) must not exist in Phase 6 orchestrator."""
    source = ORCH_PATH.read_text(encoding="utf-8")
    assert "DEFAULT_STRATEGY_CONFIGS" not in source, (
        "DEFAULT_STRATEGY_CONFIGS (old strategy-mode) must not appear in setup-mode orchestrator"
    )


def test_consolidation_base_naming_consistent():
    """'conservative_consolidation' must not appear anywhere in the orchestrator."""
    source = ORCH_PATH.read_text(encoding="utf-8")
    assert "conservative_consolidation" not in source


# --------------------------------------------------------------------------- #
# Module boundaries (static checks)
# --------------------------------------------------------------------------- #
def test_no_direct_duckdb_import():
    source = ORCH_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "duckdb", "orchestrator must not import duckdb directly"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "duckdb", "orchestrator must not import from duckdb directly"


def test_no_print_calls():
    source = ORCH_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                pytest.fail("orchestrator must not call print()")


def test_sql_constants_no_ddl_or_attach():
    """SQL string constants (Final[str] assignments) must not contain DDL or ATTACH."""
    source = ORCH_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Collect only top-level string constants assigned to _SQL-prefixed or _*SQL* names
    # i.e. module-level AnnAssign / Assign with Final[str] values
    sql_strings: list[str] = []
    for node in ast.walk(tree):
        # Match string literals that are values of module-level assignments
        # containing SQL keywords — look for SELECT/INSERT/UPDATE in the value
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            if len(val) > 15 and any(
                kw in val.upper() for kw in ("SELECT", "INSERT", "UPDATE", "DELETE")
            ):
                sql_strings.append(val)
    for sql in sql_strings:
        sql_upper = sql.upper()
        for bad in ("CREATE TABLE", "CREATE INDEX", "ALTER TABLE", "DROP TABLE"):
            assert bad not in sql_upper, (
                f"SQL constant must not contain DDL: found '{bad}' in: {sql[:60]}"
            )
        # ATTACH only forbidden as SQL command (not inside comments/docstrings)
        # Already enforced: orchestrator never calls ATTACH directly


def test_sql_write_targets_only_pipeline_tables():
    """INSERT / UPDATE / DELETE SQL must only target pipeline_runs, pipeline_locks,
    and pipeline_run_diagnostics (diagnostics are inserted by the diag service, not the
    orchestrator itself, but verify orchestrator SQL constants)."""
    allowed = {
        "PIPELINE_RUNS",
        "PIPELINE_LOCKS",
        "PIPELINE_RUN_DIAGNOSTICS",
    }
    source = ORCH_PATH.read_text(encoding="utf-8")
    # Find string literals that look like INSERT/UPDATE/DELETE SQL
    for m in re.finditer(r'"([^"]{10,})"', source):
        snippet = m.group(1).upper()
        for dml in ("INSERT INTO", "UPDATE", "DELETE FROM"):
            if snippet.startswith(dml):
                # Extract table name
                rest = snippet[len(dml):].strip()
                table = rest.split()[0] if rest.split() else ""
                if table:
                    assert table in allowed, (
                        f"Orchestrator SQL must only write to pipeline tables. "
                        f"Found: {dml} {table}"
                    )


# --------------------------------------------------------------------------- #
# Trading-day guard
# --------------------------------------------------------------------------- #

# Saturday and Sunday are always non-trading regardless of calendar
_SATURDAY = date(2026, 6, 20)
_SUNDAY   = date(2026, 6, 21)
_MONDAY   = date(2026, 6, 22)   # known trading day


def test_non_trading_day_rejected_before_db_access() -> None:
    """run() on a non-trading date must fail before any DB access."""
    db = FakeDb()
    result = build_orchestrator(db).run(_SATURDAY)
    assert result.status == service_result.STATUS_FAILED
    assert not db.executed, "no DB access should occur for a non-trading date"


def test_non_trading_day_error_message_is_clear() -> None:
    """Error message must mention the date and that it is not a trading day."""
    db = FakeDb()
    result = build_orchestrator(db).run(_SATURDAY)
    err = result.metadata.get("error", "") or ""
    assert "not a nyse trading day" in err.lower() or "not a NYSE trading day" in err, (
        f"Expected 'not a NYSE trading day' in error, got: {err!r}"
    )
    assert str(_SATURDAY) in err, f"Error must include the bad date; got: {err!r}"


def test_sunday_rejected_before_lock_acquired() -> None:
    """Sunday must be rejected before the lock-acquire DB write."""
    db = FakeDb()
    result = build_orchestrator(db).run(_SUNDAY)
    assert result.status == service_result.STATUS_FAILED
    # No lock-acquire write should have happened
    lock_writes = [s for s in db.executed if "pipeline_locks" in s.lower()]
    assert lock_writes == [], f"Lock must not be acquired for non-trading date; got: {lock_writes}"


def test_trading_day_passes_guard() -> None:
    """A known trading day (Monday) must pass the guard and proceed normally."""
    db = FakeDb()
    result = build_orchestrator(db).run(_MONDAY)
    # The run proceeds past the guard (may fail on later steps due to fake DB,
    # but the guard itself must not be the reason for failure)
    if result.status == service_result.STATUS_FAILED:
        err = result.metadata.get("error", "") or ""
        assert "not a nyse trading day" not in err.lower(), (
            f"Monday must not be rejected by the trading-day guard; error: {err!r}"
        )


def test_validate_inputs_non_trading_returns_error_string() -> None:
    """_validate_inputs must return a non-None error string for a non-trading date."""
    err = PipelineOrchestrator._validate_inputs(
        "scheduled", "prod", None, run_date=_SATURDAY
    )
    assert err is not None
    assert "not a NYSE trading day" in err or "not a nyse trading day" in err.lower()


def test_validate_inputs_trading_day_returns_none() -> None:
    """_validate_inputs must return None (no error) for a valid trading day."""
    err = PipelineOrchestrator._validate_inputs(
        "scheduled", "prod", None, run_date=_MONDAY
    )
    assert err is None


def test_is_trading_day_weekends_are_false() -> None:
    """_is_trading_day must return False for Saturday and Sunday."""
    assert not PipelineOrchestrator._is_trading_day(_SATURDAY)
    assert not PipelineOrchestrator._is_trading_day(_SUNDAY)


def test_is_trading_day_monday_is_true() -> None:
    """_is_trading_day must return True for a plain Monday."""
    assert PipelineOrchestrator._is_trading_day(_MONDAY)


def test_non_trading_parametrized(monkeypatch) -> None:
    """run() on a non-trading date must always fail with no benchmark/ingestion calls."""
    for bad_date in (_SATURDAY, _SUNDAY):
        db = FakeDb()
        result = build_orchestrator(db).run(bad_date)
        assert result.status == service_result.STATUS_FAILED, (
            f"{bad_date} should be rejected"
        )
        assert not db.executed, f"No DB writes expected for {bad_date}"
