"""Tests for operator runner scripts under tools/ — setup-mode (Phase 1).

Fully offline. Covers:
- init_prod_db: success/failure exit codes, schema+seed delegation.
- init_debug_db: success/failure exit codes.
- init_simulation_db: pass-through.
- reset_pipeline_data: dry-run, wipes setup_configs/risk_label_config not strategy_configs.
- Integration: init_prod_db against a real temp DB initializes setup-mode schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.utils import service_result
from app.utils.service_result import ServiceResult

from tools import (
    init_debug_db,
    init_prod_db,
    init_simulation_db,
)


def _ok(metadata: dict | None = None) -> ServiceResult:
    return ServiceResult(
        status=service_result.STATUS_SUCCESS,
        run_id="rid-test",
        rows_processed=1,
        metadata=metadata or {"tables_created": ["t1"], "schema_version": "schema_v02",
                              "seed_row_inserted": True},
    )


def _failed() -> ServiceResult:
    return ServiceResult(
        status=service_result.STATUS_FAILED,
        run_id="rid-test",
        rows_processed=0,
        errors=["boom"],
    )


def _ok_seed() -> ServiceResult:
    return ServiceResult(
        status=service_result.STATUS_SUCCESS,
        run_id="rid-test",
        rows_processed=5,
        metadata={"setup_seeded": 4, "risk_label_seeded": 1, "sector_alias_seeded": 16},
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    return {"prod": prod, "debug": debug, "simulation": simulation}


# --------------------------------------------------------------------------- #
# init_prod_db
# --------------------------------------------------------------------------- #
class TestInitProdDb:
    def test_success_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(init_prod_db, "_apply_prod_schema", lambda: _ok())
        monkeypatch.setattr(init_prod_db, "_seed_defaults", lambda: _ok_seed())
        monkeypatch.setattr(init_prod_db, "_resolve_prod_path", lambda: "fake.duckdb")
        assert init_prod_db.main([]) == 0

    def test_schema_failure_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(init_prod_db, "_apply_prod_schema", lambda: _failed())
        assert init_prod_db.main([]) == 1

    def test_seed_failure_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(init_prod_db, "_apply_prod_schema", lambda: _ok())
        monkeypatch.setattr(init_prod_db, "_seed_defaults", lambda: _failed())
        monkeypatch.setattr(init_prod_db, "_resolve_prod_path", lambda: "fake.duckdb")
        assert init_prod_db.main([]) == 1

    def test_schema_exception_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise():
            raise RuntimeError("connection refused")
        monkeypatch.setattr(init_prod_db, "_apply_prod_schema", _raise)
        assert init_prod_db.main([]) == 1


# --------------------------------------------------------------------------- #
# init_debug_db
# --------------------------------------------------------------------------- #
class TestInitDebugDb:
    def test_success_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(init_debug_db, "_apply_debug_schema", lambda: _ok())
        monkeypatch.setattr(init_debug_db, "_seed_defaults", lambda: _ok_seed())
        assert init_debug_db.main([]) == 0

    def test_schema_failure_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(init_debug_db, "_apply_debug_schema", lambda: _failed())
        assert init_debug_db.main([]) == 1


# --------------------------------------------------------------------------- #
# Integration: init_prod_db against real temp DB
# --------------------------------------------------------------------------- #
class TestInitProdDbIntegration:
    def test_init_prod_db_creates_setup_mode_schema(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_prod_schema()
        assert result.is_ok()
        # setup_configs must exist
        conn = dbm.connect("prod", read_only=True)
        try:
            tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        finally:
            conn.close()
        assert "setup_configs" in tables
        assert "risk_label_config" in tables
        assert "strategy_configs" not in tables

    def test_init_prod_db_seeds_four_setup_configs(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_prod_schema()
        from app.services.config.config_service import ConfigService
        svc = ConfigService(db_manager=dbm)
        r = svc.seed_defaults("prod")
        assert r.is_ok(), r.errors
        conn = dbm.connect("prod", read_only=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM setup_configs").fetchone()[0]
        finally:
            conn.close()
        assert n == 4

    def test_init_prod_db_schema_version_is_v02(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_prod_schema()
        assert result.metadata["schema_version"] == "schema_v02"


# --------------------------------------------------------------------------- #
# reset_pipeline_data
# --------------------------------------------------------------------------- #
class TestResetPipelineData:
    def test_dry_run_returns_zero(
        self, tmp_db_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tools import reset_pipeline_data
        monkeypatch.setattr(
            reset_pipeline_data,
            "_parse_args",
            lambda argv=None: type("NS", (), {"db_path": None, "dry_run": True})(),
        )
        # need a db path — apply schema first
        sm.apply_prod_schema()
        # Set db_path via monkeypatch attr override
        import types
        ns = types.SimpleNamespace(
            db_path=tmp_db_paths["prod"],
            dry_run=True,
        )
        monkeypatch.setattr(reset_pipeline_data, "_parse_args", lambda argv=None: ns)
        rc = reset_pipeline_data.main([])
        assert rc == 0

    def test_wipe_tables_contains_setup_configs_not_strategy_configs(self) -> None:
        from tools import reset_pipeline_data
        wipe = reset_pipeline_data._WIPE_TABLES
        assert "setup_configs" in wipe
        assert "risk_label_config" in wipe
        # Legacy tables must not be in wipe list
        assert "strategy_configs" not in wipe
        assert "runtime_configs" not in wipe

    def test_preserve_tables_unchanged(self) -> None:
        from tools import reset_pipeline_data
        preserve = reset_pipeline_data._PRESERVE_TABLES
        for t in ("daily_prices", "daily_features", "ticker_master", "sector_alias_map"):
            assert t in preserve
