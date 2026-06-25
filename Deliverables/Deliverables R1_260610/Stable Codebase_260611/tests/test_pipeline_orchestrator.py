"""Tests for Module 20 — Pipeline Orchestrator.

All tests run fully offline: every step engine, the market-data provider, and
the DB manager are replaced with in-process fakes, so no real DuckDB/Polars
execution, no network, and no trading-calendar import ever occurs. The
orchestrator's only DB surface (``pipeline_runs`` / ``pipeline_locks``) is
exercised through a :class:`FakeDb` that records executed SQL and returns canned
lock / already-run rows. The backup step's file copy is monkeypatched over
``pipeline_orchestrator.shutil.copy2`` and ``settings`` paths are redirected
into ``tmp_path`` so no real files are touched.

These tests assert the Module 20 contract: a ``ServiceResult`` on every path
with the exact 9-key metadata block, run_id mint/preserve, pre-DB input
validation with zero I/O, lock acquire/stale-override/release, the already-run
guard, canonical step ordering, resume-from skipping, critical-vs-recoverable
failure handling, success_with_warnings propagation, the step-major strategy
loop, and the static module boundaries (no ``import duckdb``, no ``print()``,
no DDL/``ATTACH`` in SQL, only pipeline tables as write targets).
"""

from __future__ import annotations

import ast
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.utils import service_result
from app.utils.service_result import ServiceResult
from app.services.pipeline import pipeline_orchestrator as po
from app.services.pipeline.pipeline_orchestrator import (
    DEFAULT_STRATEGY_CONFIGS,
    STEP_NAMES,
    PipelineOrchestrator,
)

RUN_DATE = date(2025, 6, 2)
META_KEYS = {
    "run_id",
    "run_date",
    "run_type",
    "db_role",
    "steps_completed",
    "failed_step",
    "error",
    "duration_sec",
    "status",
}
STRATEGY_STEP_NAMES = (
    "step3_screening",
    "step4_analysis",
    "step5_proposals",
    "outcome_queue_creation",
    "outcome_processing",
)
ORCH_PATH = Path(po.__file__)


# --------------------------------------------------------------------------- #
# Fakes.
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, db, read_only):
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
    """Records SQL and serves canned lock / already-run rows.

    ``fail_on`` is a list of ``(fragment, exception)`` pairs; if any fragment
    appears in the uppercased SQL of an ``execute`` call, that exception is
    raised — used to simulate DB failures in tests.
    """

    def __init__(self, lock_row=None, already_row=None, fail_on=None):
        self.lock_row = lock_row
        self.already_row = already_row
        self.executed: list[tuple] = []
        self.connects: list[tuple] = []
        self.closed = 0
        self.fail_on: list[tuple] = fail_on or []

    def connect(self, db_role, read_only=False):
        self.connects.append((db_role, read_only))
        return FakeConnection(self, read_only)

    def _cursor_for(self, sql):
        upper = sql.upper()
        if upper.startswith("SELECT") and "PIPELINE_LOCKS" in upper:
            return FakeCursor([self.lock_row] if self.lock_row else [])
        if upper.startswith("SELECT") and "PIPELINE_RUNS" in upper:
            return FakeCursor([self.already_row] if self.already_row else [])
        return FakeCursor([])

    # Convenience accessors for assertions.
    def write_targets(self):
        targets = []
        for sql, _params, _ro in self.executed:
            upper = sql.upper().strip()
            if upper.startswith(("INSERT", "UPDATE")):
                if "PIPELINE_RUNS" in upper:
                    targets.append("pipeline_runs")
                elif "PIPELINE_LOCKS" in upper:
                    targets.append("pipeline_locks")
                else:
                    targets.append("OTHER")
        return targets

    def ran(self, fragment):
        frag = fragment.upper()
        return any(frag in sql.upper() for sql, _p, _ro in self.executed)


class FakeStep:
    """Records the engine label on any method call and returns a result."""

    def __init__(self, recorder, label, result_fn):
        self._rec = recorder
        self._label = label
        self._fn = result_fn

    def __getattr__(self, _name):
        def _call(**kwargs):
            self._rec.append(self._label)
            return self._fn(**kwargs)

        return _call


class FakeProvider:
    def __init__(self, recorder, symbols, result=None):
        self._rec = recorder
        self._symbols = symbols
        self._result = result

    def list_symbols(self, symbol_type=None):
        self._rec.append("provider.list_symbols")
        if self._result is not None:
            return self._result
        return ServiceResult(
            service_result.STATUS_SUCCESS, "r", metadata={"symbols": self._symbols}
        )


def build_orchestrator(db, recorder, *, results=None, raises=None, symbols=None):
    """Construct an orchestrator with every dependency faked."""
    results = results or {}
    raises = raises or set()

    def make(label):
        def fn(**kwargs):
            if label in raises:
                raise RuntimeError(f"{label} boom")
            preset = results.get(label)
            if preset is not None:
                return preset
            return ServiceResult(
                service_result.STATUS_SUCCESS, kwargs.get("run_id", "r")
            )

        return FakeStep(recorder, label, fn)

    return PipelineOrchestrator(
        db_manager=db,
        provider=FakeProvider(recorder, symbols or [], results.get("list_symbols")),
        benchmark_loader=make("benchmark_etf_ingestion"),
        universe_engine=make("universe_ingestion"),
        ingestion_engine=make("price_ingestion"),
        validation_engine=make("validation"),
        mutation_engine=make("mutation_detection"),
        feature_engine=make("feature_calculation"),
        screening_engine=make("step3_screening"),
        analysis_engine=make("step4_analysis"),
        proposal_engine=make("step5_proposals"),
        outcome_creator=make("outcome_queue_creation"),
        outcome_processor=make("outcome_processing"),
    )


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg1():
    return {"normal": {"k": 1}}


@pytest.fixture
def cfg2():
    return {"normal": {"k": 1}, "aggressive": {"k": 2}}


@pytest.fixture(autouse=True)
def _no_real_backup(monkeypatch, tmp_path):
    """Never touch the filesystem during backup; redirect settings paths."""
    monkeypatch.setattr(po.shutil, "copy2", lambda *a, **k: None)
    monkeypatch.setattr(settings, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(settings, "PROD_DB_PATH", tmp_path / "prod.duckdb")
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", tmp_path / "debug.duckdb")


# --------------------------------------------------------------------------- #
# 1. ServiceResult contract: status, run_id mint/preserve, exact metadata.
# --------------------------------------------------------------------------- #
def test_returns_service_result_with_exact_metadata_and_minted_run_id(cfg1):
    db = FakeDb()
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert isinstance(result, ServiceResult)
    assert result.status == service_result.STATUS_SUCCESS
    assert set(result.metadata) == META_KEYS
    assert isinstance(result.run_id, str) and result.run_id
    assert result.metadata["run_id"] == result.run_id
    assert result.metadata["run_date"] == RUN_DATE.isoformat()


def test_run_id_is_preserved_when_supplied(cfg1):
    db = FakeDb()
    result = build_orchestrator(db, []).run(
        RUN_DATE, strategy_configs=cfg1, run_id="FIXED-RUN-ID"
    )
    assert result.run_id == "FIXED-RUN-ID"
    assert result.metadata["run_id"] == "FIXED-RUN-ID"


def test_failure_path_also_has_exact_metadata_keys(cfg1):
    db = FakeDb()
    result = build_orchestrator(db, []).run(RUN_DATE, run_type="bogus")
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == META_KEYS
    assert result.metadata["failed_step"] is None
    assert result.metadata["error"]


# --------------------------------------------------------------------------- #
# 2. Pre-DB input validation performs no I/O.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_type": "not-a-type"},
        {"db_role": "simulation"},
        {"resume_from": "not-a-step"},
    ],
)
def test_invalid_inputs_fail_before_any_db_access(kwargs, cfg1):
    db = FakeDb()
    result = build_orchestrator(db, []).run(
        RUN_DATE, strategy_configs=cfg1, **kwargs
    )
    assert result.status == service_result.STATUS_FAILED
    assert db.connects == []  # no I/O at all
    assert db.executed == []
    assert set(result.metadata) == META_KEYS


def test_empty_strategy_configs_fail_before_any_db_access():
    db = FakeDb()
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs={})
    assert result.status == service_result.STATUS_FAILED
    assert db.connects == []
    assert "strategy_configs" in result.metadata["error"]


# --------------------------------------------------------------------------- #
# 3. Active, non-stale lock blocks the run with no pipeline_runs insert.
# --------------------------------------------------------------------------- #
def test_active_lock_blocks_run_without_inserting_pipeline_runs(cfg1):
    db = FakeDb(lock_row=("other-run", True, datetime.now()))
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert result.status == service_result.STATUS_FAILED
    assert "already running" in result.metadata["error"]
    assert "pipeline_runs" not in db.write_targets()
    # We never owned the lock, so we must not have released it either.
    assert not db.ran("SET is_locked = FALSE")


# --------------------------------------------------------------------------- #
# 4. Stale lock is overwritten and the run proceeds.
# --------------------------------------------------------------------------- #
def test_stale_lock_is_overwritten_and_run_proceeds(cfg1):
    stale = datetime.now() - timedelta(seconds=po.LOCK_STALE_SECONDS + 60)
    db = FakeDb(lock_row=("stale-run", True, stale))
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert result.status == service_result.STATUS_SUCCESS
    assert db.ran("INSERT INTO pipeline_locks")  # lock upsert / override


def test_missing_heartbeat_is_treated_as_stale(cfg1):
    db = FakeDb(lock_row=("stale-run", True, None))
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert result.status == service_result.STATUS_SUCCESS


# --------------------------------------------------------------------------- #
# 5. Already-run guard (force_rerun=False) releases the lock and fails.
# --------------------------------------------------------------------------- #
def test_already_run_blocks_and_releases_lock(cfg1):
    db = FakeDb(already_row=("prev-run", "success"))
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert result.status == service_result.STATUS_FAILED
    assert "already succeeded" in result.metadata["error"]
    assert db.ran("SET is_locked = FALSE")  # released in finally


# --------------------------------------------------------------------------- #
# 6. Already-run with force_rerun=True proceeds.
# --------------------------------------------------------------------------- #
def test_force_rerun_overrides_already_run(cfg1):
    db = FakeDb(already_row=("prev-run", "success_with_warnings"))
    result = build_orchestrator(db, []).run(
        RUN_DATE, strategy_configs=cfg1, force_rerun=True
    )
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["steps_completed"] == list(STEP_NAMES)


# --------------------------------------------------------------------------- #
# 7. Happy path: exact call order, all 13 steps, lock released.
# --------------------------------------------------------------------------- #
def test_happy_path_runs_all_steps_in_order(cfg1):
    db = FakeDb()
    recorder: list[str] = []
    result = build_orchestrator(db, recorder).run(RUN_DATE, strategy_configs=cfg1)
    assert result.status == service_result.STATUS_SUCCESS
    assert recorder == [
        "benchmark_etf_ingestion",
        "provider.list_symbols",
        "universe_ingestion",
        "price_ingestion",
        "validation",
        "mutation_detection",
        "feature_calculation",
        "step3_screening",
        "step4_analysis",
        "step5_proposals",
        "outcome_queue_creation",
        "outcome_processing",
    ]
    assert result.metadata["steps_completed"] == list(STEP_NAMES)
    assert result.metadata["failed_step"] is None
    assert result.metadata["duration_sec"] is not None
    assert db.ran("INSERT INTO pipeline_runs")
    assert db.ran("SET is_locked = FALSE")


# --------------------------------------------------------------------------- #
# 8. resume_from skips earlier steps (absent from steps_completed).
# --------------------------------------------------------------------------- #
def test_resume_from_skips_earlier_steps(cfg1):
    db = FakeDb()
    recorder: list[str] = []
    result = build_orchestrator(db, recorder).run(
        RUN_DATE, strategy_configs=cfg1, resume_from="feature_calculation"
    )
    completed = result.metadata["steps_completed"]
    assert completed == list(STEP_NAMES[STEP_NAMES.index("feature_calculation"):])
    for skipped in STEP_NAMES[: STEP_NAMES.index("feature_calculation")]:
        assert skipped not in completed
    # Skipped engines were never called.
    assert "benchmark_etf_ingestion" not in recorder
    assert "price_ingestion" not in recorder
    assert "feature_calculation" in recorder


def test_resume_from_within_strategy_block(cfg1):
    db = FakeDb()
    recorder: list[str] = []
    result = build_orchestrator(db, recorder).run(
        RUN_DATE, strategy_configs=cfg1, resume_from="step5_proposals"
    )
    completed = result.metadata["steps_completed"]
    assert completed == list(STEP_NAMES[STEP_NAMES.index("step5_proposals"):])
    assert "step3_screening" not in recorder
    assert "step4_analysis" not in recorder
    assert recorder[0] == "step5_proposals"


# --------------------------------------------------------------------------- #
# 9. Critical failure stops the pipeline.
# --------------------------------------------------------------------------- #
def test_critical_failure_stops_pipeline(cfg1):
    db = FakeDb()
    recorder: list[str] = []
    results = {
        "price_ingestion": ServiceResult(
            service_result.STATUS_FAILED, "r", errors=["bad price data"]
        )
    }
    result = build_orchestrator(db, recorder, results=results).run(
        RUN_DATE, strategy_configs=cfg1
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "price_ingestion"
    assert "price_ingestion" not in result.metadata["steps_completed"]
    # Later steps never ran.
    assert "validation" not in recorder
    assert "step3_screening" not in recorder
    assert db.ran("status = 'failed'")
    assert db.ran("SET is_locked = FALSE")


def test_critical_step_raising_is_caught_and_classified(cfg1):
    db = FakeDb()
    result = build_orchestrator(db, [], raises={"feature_calculation"}).run(
        RUN_DATE, strategy_configs=cfg1
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "feature_calculation"


# --------------------------------------------------------------------------- #
# 10. Recoverable failure degrades to success_with_warnings and continues.
# --------------------------------------------------------------------------- #
def test_recoverable_failure_continues_with_warning(cfg1):
    db = FakeDb()
    recorder: list[str] = []
    results = {
        "universe_ingestion": ServiceResult(
            service_result.STATUS_FAILED, "r", errors=["universe download failed"]
        )
    }
    result = build_orchestrator(db, recorder, results=results).run(
        RUN_DATE, strategy_configs=cfg1
    )
    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert any("universe" in w for w in result.warnings)
    # Pipeline continued past the recoverable failure.
    assert "price_ingestion" in recorder
    assert "feature_calculation" in recorder
    # Executed (non-skipped, non-critically-aborted) steps are recorded.
    assert "universe_ingestion" in result.metadata["steps_completed"]
    assert result.metadata["steps_completed"] == list(STEP_NAMES)


# --------------------------------------------------------------------------- #
# 11. success_with_warnings from a step propagates to the final result.
# --------------------------------------------------------------------------- #
def test_step_warnings_propagate_to_final_result(cfg1):
    db = FakeDb()
    results = {
        "validation": ServiceResult(
            service_result.STATUS_SUCCESS_WITH_WARNINGS,
            "r",
            warnings=["minor anomaly"],
        )
    }
    result = build_orchestrator(db, [], results=results).run(
        RUN_DATE, strategy_configs=cfg1
    )
    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert "minor anomaly" in result.warnings
    assert result.metadata["steps_completed"] == list(STEP_NAMES)


# --------------------------------------------------------------------------- #
# 12. Backup failure is recoverable.
# --------------------------------------------------------------------------- #
def test_backup_failure_is_recoverable(monkeypatch, cfg1):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(po.shutil, "copy2", boom)
    db = FakeDb()
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.metadata["steps_completed"] == list(STEP_NAMES)
    assert result.metadata["failed_step"] is None


# --------------------------------------------------------------------------- #
# 13. Strategy steps execute step-major: all configs per step, then record.
# --------------------------------------------------------------------------- #
def test_strategy_steps_run_per_config_in_order(cfg2):
    """With step-major execution: step3 for all configs, then step4 for all, etc."""
    db = FakeDb()
    recorder: list[str] = []
    result = build_orchestrator(db, recorder).run(RUN_DATE, strategy_configs=cfg2)
    strat_calls = [c for c in recorder if c in STRATEGY_STEP_NAMES]
    # Step-major order: each logical step runs for ALL configs before the next.
    expected = []
    for step in STRATEGY_STEP_NAMES:
        expected.extend([step] * len(cfg2))  # all configs before next logical step
    assert strat_calls == expected
    assert result.status == service_result.STATUS_SUCCESS
    # Each logical strategy step appears exactly once in steps_completed.
    for name in STRATEGY_STEP_NAMES:
        assert result.metadata["steps_completed"].count(name) == 1


def test_critical_strategy_failure_aborts_remaining_configs(cfg2):
    """Step-major: step3 runs for ALL configs first, then step4 fails on cfg1."""
    db = FakeDb()
    recorder: list[str] = []
    results = {
        "step4_analysis": ServiceResult(
            service_result.STATUS_FAILED, "r", errors=["analysis blew up"]
        )
    }
    result = build_orchestrator(db, recorder, results=results).run(
        RUN_DATE, strategy_configs=cfg2
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "step4_analysis"
    # step3 ran for both configs (step-major: step3-for-all before step4-for-any).
    assert recorder.count("step3_screening") == len(cfg2)
    # step4 ran for cfg1 only, then failed — cfg2 never reached.
    assert recorder.count("step4_analysis") == 1
    assert "step5_proposals" not in recorder
    # step3 is in steps_completed (all configs finished it); step4 is not.
    assert "step3_screening" in result.metadata["steps_completed"]
    assert "step4_analysis" not in result.metadata["steps_completed"]


# --------------------------------------------------------------------------- #
# DB failure tests (BLOCKER 1): always return ServiceResult, exact metadata.
# --------------------------------------------------------------------------- #

# SQL fragments that identify each critical DB operation.
_FRAG_LOCK_READ = "SELECT run_id, is_locked, heartbeat_at"
_FRAG_LOCK_UPSERT = "INSERT INTO pipeline_locks"
_FRAG_ALREADY_RUN = "status IN ('success', 'success_with_warnings')"
_FRAG_INSERT_RUNNING = "INSERT INTO pipeline_runs"
_FRAG_UPDATE_STEPS = "SET steps_completed"
_FRAG_FINALIZE = "completed_at = CAST"


def _db_err(fragment):
    return FakeDb(fail_on=[(fragment, RuntimeError(f"DB unavailable: {fragment}"))])


def test_db_failure_on_lock_read_returns_service_result(cfg1):
    db = _db_err(_FRAG_LOCK_READ)
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert isinstance(result, ServiceResult)
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == META_KEYS
    assert "failed to read pipeline lock" in result.metadata["error"]
    # Never held the lock, so no lock-related pipeline_runs insert.
    assert "pipeline_runs" not in db.write_targets()


def test_db_failure_on_lock_upsert_returns_service_result(cfg1):
    db = _db_err(_FRAG_LOCK_UPSERT)
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert isinstance(result, ServiceResult)
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == META_KEYS
    assert "failed to acquire pipeline lock" in result.metadata["error"]
    assert "pipeline_runs" not in db.write_targets()


def test_db_failure_on_already_run_query_returns_service_result(cfg1):
    db = _db_err(_FRAG_ALREADY_RUN)
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert isinstance(result, ServiceResult)
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == META_KEYS
    assert "failed to query pipeline_runs" in result.metadata["error"]


def test_db_failure_on_insert_running_returns_service_result(cfg1):
    db = _db_err(_FRAG_INSERT_RUNNING)
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert isinstance(result, ServiceResult)
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == META_KEYS
    assert "failed to insert pipeline_runs running row" in result.metadata["error"]


def test_db_failure_on_step_progress_update_returns_service_result(cfg1):
    db = _db_err(_FRAG_UPDATE_STEPS)
    recorder: list[str] = []
    result = build_orchestrator(db, recorder).run(RUN_DATE, strategy_configs=cfg1)
    assert isinstance(result, ServiceResult)
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == META_KEYS
    assert "DB error recording step" in result.metadata["error"]
    # steps_completed reflects the step that was appended before the write failed
    assert len(result.metadata["steps_completed"]) >= 1


def test_db_failure_on_final_update_returns_failed(cfg1):
    """Final pipeline_runs UPDATE failure is critical — returns STATUS_FAILED."""
    db = _db_err(_FRAG_FINALIZE)
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert isinstance(result, ServiceResult)
    assert set(result.metadata) == META_KEYS
    assert result.status == service_result.STATUS_FAILED
    assert "failed to finalize pipeline_runs" in result.metadata["error"]
    # steps_completed is preserved even though the DB write failed.
    assert result.metadata["steps_completed"] == list(STEP_NAMES)


# --------------------------------------------------------------------------- #
# BLOCKER 1 new test: multi-config partial failure never records premature steps.
# --------------------------------------------------------------------------- #

def test_multi_config_cfg2_step3_fails_later_steps_not_in_steps_completed(cfg2):
    """cfg1 full success, cfg2 step3 critical failure.

    With step-major execution, step3 runs for cfg1 then cfg2.  cfg2's step3
    fails critically before all configs finish step3, so step3 is NOT recorded.
    All later strategy steps (step4..outcome_processing) must also be absent.
    """
    db = FakeDb()
    recorder: list[str] = []
    call_count: dict[str, int] = {}

    class ConfigAwareStep:
        def __init__(self, name):
            self.name = name
        def __getattr__(self, method):
            def _call(**kwargs):
                cfg_id = kwargs.get("strategy_config_id", "?")
                recorder.append(self.name)
                call_count[self.name] = call_count.get(self.name, 0) + 1
                # cfg2's step3 fails
                if self.name == "step3_screening" and cfg_id == list(cfg2)[1]:
                    return ServiceResult(
                        service_result.STATUS_FAILED, "r", errors=["cfg2 step3 down"]
                    )
                return ServiceResult(service_result.STATUS_SUCCESS, "r")
            return _call

    from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
    orch = PipelineOrchestrator(
        db_manager=db,
        provider=FakeStep(recorder, "provider.list_symbols",
                          lambda **kw: ServiceResult(
                              service_result.STATUS_SUCCESS, "r",
                              metadata={"symbols": []})),
        benchmark_loader=ConfigAwareStep("benchmark_etf_ingestion"),
        universe_engine=ConfigAwareStep("universe_ingestion"),
        ingestion_engine=ConfigAwareStep("price_ingestion"),
        validation_engine=ConfigAwareStep("validation"),
        mutation_engine=ConfigAwareStep("mutation_detection"),
        feature_engine=ConfigAwareStep("feature_calculation"),
        screening_engine=ConfigAwareStep("step3_screening"),
        analysis_engine=ConfigAwareStep("step4_analysis"),
        proposal_engine=ConfigAwareStep("step5_proposals"),
        outcome_creator=ConfigAwareStep("outcome_queue_creation"),
        outcome_processor=ConfigAwareStep("outcome_processing"),
    )
    result = orch.run(RUN_DATE, strategy_configs=cfg2)

    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "step3_screening"
    sc = result.metadata["steps_completed"]
    # step3 did not complete for all configs, so it must not appear.
    assert "step3_screening" not in sc
    # No later strategy steps should appear either.
    for later in ("step4_analysis", "step5_proposals",
                  "outcome_queue_creation", "outcome_processing"):
        assert later not in sc, f"{later} should not be in steps_completed"


# --------------------------------------------------------------------------- #
# Strategy progress tests (BLOCKER 2): partial progress retained on failure.
# --------------------------------------------------------------------------- #

def test_strategy_step3_retained_when_step4_fails(cfg1):
    """step3 succeeds then step4 fails: step3 must appear in steps_completed."""
    db = FakeDb()
    recorder: list[str] = []
    results = {
        "step4_analysis": ServiceResult(
            service_result.STATUS_FAILED, "r", errors=["analysis down"]
        )
    }
    result = build_orchestrator(db, recorder, results=results).run(
        RUN_DATE, strategy_configs=cfg1
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["failed_step"] == "step4_analysis"
    # step3_screening ran and completed before step4 failed — must be retained.
    assert "step3_screening" in result.metadata["steps_completed"]
    assert "step4_analysis" not in result.metadata["steps_completed"]


def test_strategy_progress_and_heartbeat_written_before_block_finishes(cfg1):
    """_record_step (steps_completed UPDATE + heartbeat) fires within the strategy
    block, not only after the whole block completes."""
    heartbeats_before_end: list[int] = []
    step3_heartbeats: list[int] = []

    class TrackingDb(FakeDb):
        def _cursor_for(self, sql):
            upper = sql.upper()
            if "HEARTBEAT_AT = CAST" in upper:
                # Record how many step3 records exist at heartbeat time.
                sc_count = sum(
                    1 for s, _p, _ro in self.executed
                    if "SET steps_completed" in s
                    and "step3_screening" in str(_p)
                )
                step3_heartbeats.append(sc_count)
            return super()._cursor_for(sql)

    db = TrackingDb()
    build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    # At least one heartbeat must have been issued while step3 was already
    # recorded (i.e., within the strategy block, not only at the very end).
    assert any(n >= 1 for n in step3_heartbeats), (
        "Expected a heartbeat after step3_screening was recorded"
    )


def test_final_update_failure_returns_failed_with_error_metadata(cfg1):
    """Explicit BLOCKER 2 contract: final pipeline_runs update failure
    returns STATUS_FAILED with error in metadata, preserving steps_completed."""
    db = _db_err(_FRAG_FINALIZE)
    result = build_orchestrator(db, []).run(RUN_DATE, strategy_configs=cfg1)
    assert result.status == service_result.STATUS_FAILED
    assert "failed to finalize pipeline_runs" in (result.metadata["error"] or "")
    assert set(result.metadata) == META_KEYS
    assert result.metadata["steps_completed"] == list(STEP_NAMES)
    # Lock must still be released in finally.
    assert db.ran("SET is_locked = FALSE")


# --------------------------------------------------------------------------- #
# 14. Static module boundaries (AST + SQL-constant scan).
# --------------------------------------------------------------------------- #
def _module_ast():
    return ast.parse(ORCH_PATH.read_text(encoding="utf-8"))


def test_no_direct_duckdb_import():
    tree = _module_ast()
    offending = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offending |= any(
                alias.name == "duckdb" or alias.name.startswith("duckdb.")
                for alias in node.names
            )
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            offending |= mod == "duckdb" or mod.startswith("duckdb.")
    assert not offending, "Module 20 must not import duckdb directly"


def test_no_print_calls():
    tree = _module_ast()
    has_print = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        for node in ast.walk(tree)
    )
    assert not has_print, "Module 20 must log, not print"


def _sql_constants(tree):
    consts = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value_node = node.value
            if isinstance(value_node, ast.Constant) and isinstance(
                value_node.value, str
            ):
                value = value_node.value
                if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", value.upper()):
                    consts.append(value)
    return consts


def test_sql_constants_have_no_ddl_or_attach():
    joined = " ".join(_sql_constants(_module_ast())).upper()
    assert not re.search(
        r"\b(CREATE TABLE|ALTER TABLE|DROP TABLE|ATTACH)\b", joined
    )


def test_sql_write_targets_are_only_pipeline_tables():
    joined = " ".join(_sql_constants(_module_ast())).upper()
    inserts = set(re.findall(r"INSERT INTO\s+([A-Z_]+)", joined))
    updates = set(re.findall(r"UPDATE\s+([A-Z_]+)\s+SET", joined))
    targets = inserts | updates
    assert targets, "expected at least one write target"
    assert targets <= {"PIPELINE_RUNS", "PIPELINE_LOCKS"}


# --------------------------------------------------------------------------- #
# Bonus: DEFAULT_STRATEGY_CONFIGS shape sanity (offline, no engine import).
# --------------------------------------------------------------------------- #
def test_default_strategy_configs_have_required_engine_keys():
    assert set(DEFAULT_STRATEGY_CONFIGS) == {"normal", "aggressive", "conservative"}
    for cfg in DEFAULT_STRATEGY_CONFIGS.values():
        assert {"min_price", "min_avg_dollar_volume_20d"} <= set(cfg["universe"])
        assert "min_rvol" in cfg["screening"]
        assert {"trend", "momentum", "setup", "volume", "market"} <= set(
            cfg["scoring_weights"]
        )
        assert cfg["step4"]["target_R"] > 0
        assert cfg["diversification"]["hard_cap_enabled"] is True
        assert cfg["diversification"]["top_n"] > 0
        assert cfg["simulation"]["slippage_bps"] >= 0
        assert cfg["earnings"]["avoid_within_bd"] >= 0
