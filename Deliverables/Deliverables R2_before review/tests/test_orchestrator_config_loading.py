"""Pipeline orchestrator config-loading + traceability tests (M21 addendum §7).

Fully offline: the DB manager, every step engine, the provider, and the
ConfigService are replaced with in-process fakes. Verifies the new
config-resolution contract (explicit override vs. DB-active vs. seed-on-empty)
and that each run persists strategy/runtime config ids + a deterministic
config snapshot hash into ``pipeline_runs``.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.utils import service_result
from app.utils.service_result import ServiceResult
from app.services.pipeline.pipeline_orchestrator import (
    STEP_NAMES,
    PipelineOrchestrator,
)

RUN_DATE = date(2025, 6, 2)
_CONFIG_META_FRAGMENT = "strategy_config_ids_json"


@pytest.fixture(autouse=True)
def _neutralize_backup(tmp_path, monkeypatch):
    """Redirect DB paths to tmp and stub the backup file copy.

    The recoverable ``backup`` step performs a real ``shutil.copy2`` of the prod
    DB file; isolating it keeps these config-focused tests deterministic and
    off the real filesystem.
    """
    from app.config import settings
    from app.services.pipeline import pipeline_orchestrator as po

    duckdb_dir = tmp_path / "duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=False)
    monkeypatch.setattr(settings, "PROD_DB_PATH", duckdb_dir / "prod.duckdb", raising=False)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", duckdb_dir / "debug.duckdb", raising=False)
    monkeypatch.setattr(
        settings, "SIMULATION_DB_PATH", duckdb_dir / "simulation.duckdb", raising=False
    )
    monkeypatch.setattr(po.shutil, "copy2", lambda *a, **k: None, raising=True)


# --------------------------------------------------------------------------- #
# Fakes (mirrors tests/test_pipeline_orchestrator.py).
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
        self._db.executed.append((sql, params, self._read_only))
        upper = sql.upper()
        if upper.startswith("SELECT") and "PIPELINE_LOCKS" in upper:
            return FakeCursor([self._db.lock_row] if self._db.lock_row else [])
        if upper.startswith("SELECT") and "PIPELINE_RUNS" in upper:
            return FakeCursor([self._db.already_row] if self._db.already_row else [])
        return FakeCursor([])

    def close(self):
        self._db.closed += 1


class FakeDb:
    def __init__(self, lock_row=None, already_row=None):
        self.lock_row = lock_row
        self.already_row = already_row
        self.executed: list[tuple] = []
        self.closed = 0

    def connect(self, db_role, read_only=False):
        return FakeConnection(self, read_only)

    def config_meta_writes(self):
        return [
            (sql, params)
            for sql, params, _ro in self.executed
            if _CONFIG_META_FRAGMENT in sql
        ]


class FakeStep:
    def __init__(self, label, recorder):
        self._label = label
        self._rec = recorder

    def __getattr__(self, _name):
        def _call(**kwargs):
            self._rec.append(self._label)
            return ServiceResult(
                service_result.STATUS_SUCCESS, kwargs.get("run_id", "r")
            )

        return _call


class FakeProvider:
    def list_symbols(self, symbol_type=None):
        return ServiceResult(service_result.STATUS_SUCCESS, "r", metadata={"symbols": []})


class FakeConfigService:
    """Records calls and serves canned active strategy/runtime configs."""

    def __init__(self, active=None, runtime=None):
        self._active = active if active is not None else {}
        self._runtime = runtime or []
        self.calls: list[str] = []
        self.seeded = False

    def get_active_strategy_configs(self, db_role):
        self.calls.append("get_active")
        configs_by_strategy = dict(self._active)
        config_ids_by_strategy = {k: f"id_{k}" for k in self._active}
        configs_by_id = {f"id_{k}": v for k, v in self._active.items()}
        return ServiceResult(
            service_result.STATUS_SUCCESS,
            "r",
            metadata={
                "configs": configs_by_strategy,
                "configs_by_strategy": configs_by_strategy,
                "configs_by_id": configs_by_id,
                "config_ids_by_strategy": config_ids_by_strategy,
                "config_ids": list(configs_by_id),
            },
        )

    def seed_default_strategy_configs(self, db_role):
        self.calls.append("seed")
        self.seeded = True
        self._active = {
            "normal": {"x": 1},
            "aggressive": {"y": 2},
            "conservative": {"z": 3},
        }
        return ServiceResult(
            service_result.STATUS_SUCCESS, "r", metadata={"seeded": 3}
        )
    def list_runtime_configs(self, db_role):
        self.calls.append("list_runtime")
        return ServiceResult(
            service_result.STATUS_SUCCESS, "r", metadata={"versions": self._runtime}
        )


def build(db, cfg_service, recorder=None):
    recorder = recorder if recorder is not None else []

    def step(label):
        return FakeStep(label, recorder)

    return PipelineOrchestrator(
        db_manager=db,
        provider=FakeProvider(),
        benchmark_loader=step("benchmark_etf_ingestion"),
        universe_engine=step("universe_ingestion"),
        ingestion_engine=step("price_ingestion"),
        validation_engine=step("validation"),
        mutation_engine=step("mutation_detection"),
        feature_engine=step("feature_calculation"),
        screening_engine=step("step3_screening"),
        analysis_engine=step("step4_analysis"),
        proposal_engine=step("step5_proposals"),
        outcome_creator=step("outcome_queue_creation"),
        outcome_processor=step("outcome_processing"),
        config_service=cfg_service,
    )


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #
def test_no_explicit_configs_loads_active_db_configs():
    cfg = FakeConfigService(
        active={"normal": {"a": 1}},
        runtime=[{"config_id": "rt1", "active_flag": True}],
    )
    db = FakeDb()
    result = build(db, cfg).run(RUN_DATE)
    assert result.is_ok()
    assert "get_active" in cfg.calls
    assert cfg.seeded is False
    writes = db.config_meta_writes()
    assert writes, "config traceability must be written to pipeline_runs"
    _sql, params = writes[0]
    strat_ids = json.loads(params[0])
    runtime_ids = json.loads(params[1])
    snapshot_hash = params[2]
    assert strat_ids == ["id_normal"]
    assert runtime_ids == ["rt1"]
    assert isinstance(snapshot_hash, str) and len(snapshot_hash) == 64


def test_missing_active_configs_triggers_seed_then_load():
    cfg = FakeConfigService(active={})
    db = FakeDb()
    result = build(db, cfg).run(RUN_DATE)
    assert result.is_ok()
    assert cfg.seeded is True
    assert cfg.calls.count("get_active") >= 2  # load, (seed), reload
    writes = db.config_meta_writes()
    strat_ids = json.loads(writes[0][1][0])
    assert sorted(strat_ids) == ["id_aggressive", "id_conservative", "id_normal"]


def test_explicit_configs_override_db_configs():
    cfg = FakeConfigService(active={"normal": {"a": 1}})
    db = FakeDb()
    result = build(db, cfg).run(
        RUN_DATE, strategy_configs={"cfg-A": {"k": 1}}
    )
    assert result.is_ok()
    # ConfigService is never consulted for active configs on the override path.
    assert "get_active" not in cfg.calls
    writes = db.config_meta_writes()
    strat_ids = json.loads(writes[0][1][0])
    # Override keys are the ids used downstream and recorded for traceability.
    assert strat_ids == ["cfg-A"]


def test_strategy_steps_receive_real_db_config_id():
    """The id passed to the strategy engines is the DB config_id, not the name."""
    cfg = FakeConfigService(active={"normal": {"a": 1}, "aggressive": {"b": 2}})
    db = FakeDb()
    recorder: list[str] = []
    orch = build(db, cfg, recorder)
    seen: list[str] = []
    orig = orch._call_screen

    def spy(run_date, db_role, run_id, config_id, config_dict, log):
        seen.append(config_id)
        return orig(run_date, db_role, run_id, config_id, config_dict, log)

    orch._call_screen = spy  # type: ignore[assignment]
    result = orch.run(RUN_DATE)
    assert result.is_ok()
    # Engines receive the real DB config_ids, not strategy names.
    assert set(seen) == {"id_normal", "id_aggressive"}
    # And the recorded traceability ids match exactly what was used.
    strat_ids = json.loads(db.config_meta_writes()[0][1][0])
    assert set(strat_ids) == {"id_normal", "id_aggressive"}


def test_config_meta_targets_pipeline_runs_only():
    cfg = FakeConfigService(active={"normal": {"a": 1}})
    db = FakeDb()
    build(db, cfg).run(RUN_DATE)
    for sql, _params in db.config_meta_writes():
        assert "PIPELINE_RUNS" in sql.upper()
        assert sql.upper().strip().startswith("UPDATE")


def test_run_still_completes_all_steps_with_db_configs():
    cfg = FakeConfigService(active={"normal": {"a": 1}})
    db = FakeDb()
    result = build(db, cfg).run(RUN_DATE)
    assert result.metadata["steps_completed"] == list(STEP_NAMES)


def test_snapshot_hash_includes_runtime_ids():
    """Hash must change when runtime config ids change (fix #5)."""
    from app.services.config import config_validator as cv

    cfg = {"id_normal": {"a": 1}}
    hash_a = cv.snapshot_hash(
        {"strategy_configs_by_id": cfg, "runtime_config_ids": ["rt1"]}
    )
    hash_b = cv.snapshot_hash(
        {"strategy_configs_by_id": cfg, "runtime_config_ids": ["rt2"]}
    )
    assert hash_a != hash_b, "hash must differ when runtime_ids differ"
    hash_c = cv.snapshot_hash(
        {"strategy_configs_by_id": cfg, "runtime_config_ids": ["rt1"]}
    )
    assert hash_a == hash_c, "same inputs must produce same hash"
