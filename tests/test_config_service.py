"""Tests for the ConfigService and fresh config schema (M21 Config Mgmt Addendum).

These tests exercise the real DuckDB-backed schema and ConfigService against
temp databases (no real prod/debug/simulation files are touched). The DuckDB
manager paths are redirected into ``tmp_path`` via the ``settings`` monkeypatch
fixture, mirroring the other DB-backed test modules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import constants
from app.database import duckdb_manager as dbm
from app.database import schema_manager
from app.config import settings
from app.services.config import config_validator as cv
from app.services.config import default_configs
from app.services.config.config_service import ConfigService
from app.utils import service_result


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


@pytest.fixture
def prod_schema(tmp_db_paths: dict[str, Path]) -> ConfigService:
    result = schema_manager.apply_prod_schema()
    assert result.is_ok(), result.errors
    return ConfigService(db_manager=dbm)


def _columns(role: str, table: str) -> list[str]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? "
            "ORDER BY ordinal_position",
            [table],
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _count(role: str, sql: str, params: list | None = None) -> int:
    conn = dbm.connect(role, read_only=True)
    try:
        return conn.execute(sql, params or []).fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. Fresh DB schema includes the config tables / columns.
# --------------------------------------------------------------------------- #
def test_fresh_schema_has_config_tables(prod_schema: ConfigService) -> None:
    conn = dbm.connect("prod", read_only=True)
    try:
        names = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    finally:
        conn.close()
    assert {
        "strategy_configs",
        "runtime_configs",
        "config_activation_log",
        "sector_alias_map",
    } <= names


def test_strategy_configs_has_db_role_and_created_by(
    prod_schema: ConfigService,
) -> None:
    cols = _columns("prod", "strategy_configs")
    assert "db_role" in cols
    assert "created_by" in cols


def test_pipeline_runs_has_config_traceability_columns(
    prod_schema: ConfigService,
) -> None:
    cols = _columns("prod", "pipeline_runs")
    assert {
        "strategy_config_ids_json",
        "runtime_config_ids_json",
        "config_snapshot_hash",
    } <= set(cols)


# --------------------------------------------------------------------------- #
# 2. Default strategy config seeding.
# --------------------------------------------------------------------------- #
def test_seed_strategy_configs_creates_three_active(
    prod_schema: ConfigService,
) -> None:
    result = prod_schema.seed_default_strategy_configs("prod")
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["seeded"] == 3
    active = _count(
        "prod", "SELECT COUNT(*) FROM strategy_configs WHERE active_flag = TRUE"
    )
    assert active == 3
    names = {
        r[0]
        for r in dbm.connect("prod", read_only=True).execute(
            "SELECT strategy_name FROM strategy_configs"
        ).fetchall()
    }
    assert names == {"normal", "aggressive", "conservative"}


def test_seed_strategy_configs_is_idempotent(prod_schema: ConfigService) -> None:
    prod_schema.seed_default_strategy_configs("prod")
    second = prod_schema.seed_default_strategy_configs("prod")
    # Second seed inserts nothing (deterministic seed id + ON CONFLICT).
    assert second.metadata["seeded"] == 0
    assert _count("prod", "SELECT COUNT(*) FROM strategy_configs") == 3


def test_seed_uses_current_default_values(prod_schema: ConfigService) -> None:
    prod_schema.seed_default_strategy_configs("prod")
    got = prod_schema.get_active_strategy_configs("prod")
    defaults = default_configs.get_default_strategy_configs()
    assert got.metadata["configs"]["normal"]["screening"]["min_rvol"] == (
        defaults["normal"]["screening"]["min_rvol"]
    )


def test_seed_strategy_accepts_simulation_role(sim_schema: ConfigService) -> None:
    """simulation is a supported config db_role (M21 review fix #4)."""
    result = sim_schema.seed_default_strategy_configs("simulation")
    assert result.is_ok(), result.errors
    assert result.metadata["seeded"] == 3


# --------------------------------------------------------------------------- #
# 3. Runtime config seeding.
# --------------------------------------------------------------------------- #
def test_seed_runtime_configs_creates_expected_types(
    prod_schema: ConfigService,
) -> None:
    result = prod_schema.seed_default_runtime_configs("prod")
    assert result.metadata["seeded"] == len(cv.ALLOWED_CONFIG_TYPES)
    types_present = {
        r[0]
        for r in dbm.connect("prod", read_only=True).execute(
            "SELECT config_type FROM runtime_configs WHERE active_flag = TRUE"
        ).fetchall()
    }
    assert types_present == set(cv.ALLOWED_CONFIG_TYPES)


def test_seed_runtime_configs_is_idempotent(prod_schema: ConfigService) -> None:
    prod_schema.seed_default_runtime_configs("prod")
    second = prod_schema.seed_default_runtime_configs("prod")
    assert second.metadata["seeded"] == 0
    assert _count("prod", "SELECT COUNT(*) FROM runtime_configs") == len(
        cv.ALLOWED_CONFIG_TYPES
    )


def test_seed_defaults_seeds_all_three_kinds(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_defaults("prod")
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["strategy_seeded"] == 3
    assert result.metadata["runtime_seeded"] == len(cv.ALLOWED_CONFIG_TYPES)
    assert result.metadata["sector_alias_seeded"] == len(constants.SECTOR_ALIAS_MAP)


# --------------------------------------------------------------------------- #
# 4. Config hash determinism.
# --------------------------------------------------------------------------- #
def test_config_hash_is_order_independent() -> None:
    a = {"b": 1, "a": {"y": 2, "x": 1}}
    b = {"a": {"x": 1, "y": 2}, "b": 1}
    assert cv.deterministic_hash(a) == cv.deterministic_hash(b)


def test_config_hash_changes_with_payload() -> None:
    a = {"a": 1}
    b = {"a": 2}
    assert cv.deterministic_hash(a) != cv.deterministic_hash(b)


# --------------------------------------------------------------------------- #
# 5. Versioning + activation + rollback.
# --------------------------------------------------------------------------- #
def test_create_activate_and_rollback(prod_schema: ConfigService) -> None:
    prod_schema.seed_default_strategy_configs("prod")
    # The seeded normal config id is deterministic.
    seed_id = "seed_strategy_prod_normal_v1"

    created = prod_schema.create_strategy_config_version(
        "prod", "normal", {"screening": {"min_rvol": 9.0}}, activate=True
    )
    assert created.is_ok(), created.errors
    new_id = created.metadata["config_id"]

    # Exactly one active 'normal' config, and it is the new one.
    active_id = dbm.connect("prod", read_only=True).execute(
        "SELECT config_id FROM strategy_configs "
        "WHERE strategy_name = 'normal' AND active_flag = TRUE"
    ).fetchone()[0]
    assert active_id == new_id

    # Rollback: re-activate the seeded version.
    rolled = prod_schema.activate_strategy_config(
        seed_id, "prod", reason="rollback"
    )
    assert rolled.is_ok(), rolled.errors
    active_id = dbm.connect("prod", read_only=True).execute(
        "SELECT config_id FROM strategy_configs "
        "WHERE strategy_name = 'normal' AND active_flag = TRUE"
    ).fetchone()[0]
    assert active_id == seed_id

    # Activation log captured both activations (create+activate, rollback).
    log_rows = _count(
        "prod",
        "SELECT COUNT(*) FROM config_activation_log WHERE config_id IN (?, ?)",
        [new_id, seed_id],
    )
    assert log_rows >= 2


def test_get_active_runtime_config_returns_payload(
    prod_schema: ConfigService,
) -> None:
    prod_schema.seed_default_runtime_configs("prod")
    result = prod_schema.get_active_runtime_config("prod", "pipeline")
    assert result.is_ok(), result.errors
    assert "lock_stale_seconds" in result.metadata["config_json"]


def test_activate_unknown_config_fails(prod_schema: ConfigService) -> None:
    result = prod_schema.activate_strategy_config("does-not-exist", "prod")
    assert result.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# 6. config_activation_log semantics (fix #3).
# --------------------------------------------------------------------------- #
def test_activation_log_stores_strategy_type_and_profile_name(
    prod_schema: ConfigService,
) -> None:
    """config_type must be 'strategy'; profile_name must be the strategy name."""
    prod_schema.seed_default_strategy_configs("prod")
    prod_schema.activate_strategy_config(
        "seed_strategy_prod_normal_v1", "prod", reason="test"
    )
    conn = dbm.connect("prod", read_only=True)
    try:
        row = conn.execute(
            "SELECT config_type, profile_name FROM config_activation_log "
            "WHERE config_id = 'seed_strategy_prod_normal_v1' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "strategy", f"config_type was {row[0]!r}, expected 'strategy'"
    assert row[1] == "normal", f"profile_name was {row[1]!r}, expected 'normal'"


def test_create_activate_log_semantics(prod_schema: ConfigService) -> None:
    result = prod_schema.create_strategy_config_version(
        "prod", "aggressive", {"screening": {"min_rvol": 2.5}}, activate=True
    )
    assert result.is_ok(), result.errors
    new_id = result.metadata["config_id"]
    conn = dbm.connect("prod", read_only=True)
    try:
        row = conn.execute(
            "SELECT config_type, profile_name FROM config_activation_log "
            "WHERE config_id = ? LIMIT 1",
            [new_id],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "strategy"
    assert row[1] == "aggressive"


# --------------------------------------------------------------------------- #
# 7. Simulation db_role config support (fix #4).
# --------------------------------------------------------------------------- #
@pytest.fixture
def sim_schema(tmp_db_paths: dict[str, Path]) -> ConfigService:
    from app.database import schema_manager as sm

    result = sm.apply_simulation_schema()
    assert result.is_ok(), result.errors
    return ConfigService(db_manager=dbm)


def test_simulation_schema_has_config_tables(sim_schema: ConfigService) -> None:
    conn = dbm.connect("simulation", read_only=True)
    try:
        names = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    finally:
        conn.close()
    assert {"strategy_configs", "runtime_configs", "config_activation_log",
            "sector_alias_map"} <= names


def test_seed_defaults_for_simulation(sim_schema: ConfigService) -> None:
    result = sim_schema.seed_defaults("simulation")
    assert result.is_ok(), result.errors
    assert result.metadata["strategy_seeded"] == 3
    assert result.metadata["runtime_seeded"] > 0
    assert result.metadata["sector_alias_seeded"] > 0


def test_seed_simulation_is_idempotent(sim_schema: ConfigService) -> None:
    sim_schema.seed_defaults("simulation")
    second = sim_schema.seed_defaults("simulation")
    assert second.is_ok()
    assert second.metadata["strategy_seeded"] == 0
    assert second.metadata["runtime_seeded"] == 0
