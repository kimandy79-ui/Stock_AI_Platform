"""Tests for ConfigService and setup-mode config schema (Phase 1).

Covers:
- Fresh DB has setup_configs and risk_label_config (not strategy_configs).
- Seeding is idempotent.
- Exactly one active setup config per setup_type.
- Exactly one active risk-label config.
- Invalid setup_type is rejected.
- config_hash is stable (deterministic).
- get_active_setup_config returns the right config.
- get_active_risk_label_config returns the right config.
- validate_setup_config / validate_risk_label_config work correctly.
- assert_universe_config_parity passes when all four configs share universe block.
- sector_alias_map seeding.
- ServiceResult contract on all paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager
from app.services.config import config_validator as cv
from app.services.config import default_configs
from app.services.config.config_service import ConfigService
from app.utils import service_result


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


@pytest.fixture
def prod_schema(tmp_db_paths: dict[str, Path]) -> ConfigService:
    result = schema_manager.apply_prod_schema()
    assert result.is_ok(), result.errors
    return ConfigService(db_manager=dbm)


@pytest.fixture
def seeded_prod(prod_schema: ConfigService) -> ConfigService:
    svc = prod_schema
    r = svc.seed_defaults("prod")
    assert r.is_ok(), r.errors
    return svc


def _columns(role: str, table: str) -> list[str]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
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
# 1. Fresh DB has setup-mode tables (not legacy)
# --------------------------------------------------------------------------- #
def test_fresh_schema_has_setup_configs_not_strategy_configs(
    prod_schema: ConfigService,
) -> None:
    conn = dbm.connect("prod", read_only=True)
    try:
        names = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    finally:
        conn.close()
    assert "setup_configs" in names
    assert "risk_label_config" in names
    # Legacy tables must not exist
    assert "strategy_configs" not in names
    assert "runtime_configs" not in names
    assert "config_activation_log" not in names


def test_setup_configs_has_setup_type_column(prod_schema: ConfigService) -> None:
    cols = _columns("prod", "setup_configs")
    assert "setup_type" in cols
    assert "config_id" in cols
    assert "active_flag" in cols
    # No legacy columns
    assert "strategy_name" not in cols
    assert "db_role" not in cols


# --------------------------------------------------------------------------- #
# 2. Seeding
# --------------------------------------------------------------------------- #
def test_seed_default_setup_configs_inserts_four_rows(seeded_prod: ConfigService) -> None:
    n = _count("prod", "SELECT COUNT(*) FROM setup_configs")
    assert n == 4


def test_seed_default_setup_configs_all_four_setup_types(seeded_prod: ConfigService) -> None:
    conn = dbm.connect("prod", read_only=True)
    try:
        rows = conn.execute("SELECT setup_type FROM setup_configs ORDER BY setup_type").fetchall()
    finally:
        conn.close()
    types = {r[0] for r in rows}
    assert types == set(constants.ALLOWED_SETUP_TYPES)


def test_seed_default_risk_label_config_inserts_one_row(seeded_prod: ConfigService) -> None:
    n = _count("prod", "SELECT COUNT(*) FROM risk_label_config")
    assert n == 1


def test_seeding_is_idempotent(prod_schema: ConfigService) -> None:
    svc = prod_schema
    r1 = svc.seed_defaults("prod")
    r2 = svc.seed_defaults("prod")
    assert r1.is_ok()
    assert r2.is_ok()
    # Second seed inserts 0 rows (ON CONFLICT DO NOTHING)
    assert r2.rows_processed == 0
    # DB still has exactly 4 setup configs and 1 risk label config
    assert _count("prod", "SELECT COUNT(*) FROM setup_configs") == 4
    assert _count("prod", "SELECT COUNT(*) FROM risk_label_config") == 1


# --------------------------------------------------------------------------- #
# 3. Activation constraints
# --------------------------------------------------------------------------- #
def test_one_active_setup_config_per_setup_type(seeded_prod: ConfigService) -> None:
    for setup_type in constants.ALLOWED_SETUP_TYPES:
        n = _count(
            "prod",
            "SELECT COUNT(*) FROM setup_configs WHERE setup_type = ? AND active_flag = TRUE",
            [setup_type],
        )
        assert n == 1, f"Expected 1 active setup config for {setup_type!r}, got {n}"


def test_one_active_risk_label_config(seeded_prod: ConfigService) -> None:
    n = _count("prod", "SELECT COUNT(*) FROM risk_label_config WHERE active_flag = TRUE")
    assert n == 1


# --------------------------------------------------------------------------- #
# 4. Validation: invalid setup_type is rejected
# --------------------------------------------------------------------------- #
def test_get_active_setup_config_invalid_type_fails(prod_schema: ConfigService) -> None:
    svc = prod_schema
    result = svc.get_active_setup_config("prod", "aggressive")  # legacy strategy name
    assert result.status == service_result.STATUS_FAILED
    assert result.errors


def test_get_active_setup_config_legacy_name_fails(prod_schema: ConfigService) -> None:
    for bad in ("normal", "conservative", "trend_pullback", "volatility_squeeze"):
        result = prod_schema.get_active_setup_config("prod", bad)
        assert result.status == service_result.STATUS_FAILED, f"Expected failure for {bad!r}"


def test_seed_with_invalid_db_role_fails(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_default_setup_configs("bad_role")
    assert result.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# 5. Config hash is stable (deterministic)
# --------------------------------------------------------------------------- #
def test_config_hash_is_deterministic() -> None:
    cfg = {"setup_type": "breakout", "min_rvol": 1.5, "min_rr": 1.8}
    h1 = cv.deterministic_hash(cfg)
    h2 = cv.deterministic_hash(cfg)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_config_hash_differs_on_content_change() -> None:
    cfg1 = {"min_rvol": 1.5}
    cfg2 = {"min_rvol": 2.0}
    assert cv.deterministic_hash(cfg1) != cv.deterministic_hash(cfg2)


def test_config_hash_key_order_independent() -> None:
    cfg_a = {"b": 2, "a": 1}
    cfg_b = {"a": 1, "b": 2}
    assert cv.deterministic_hash(cfg_a) == cv.deterministic_hash(cfg_b)


def test_seeded_configs_have_config_hash(seeded_prod: ConfigService) -> None:
    conn = dbm.connect("prod", read_only=True)
    try:
        rows = conn.execute(
            "SELECT config_hash FROM setup_configs WHERE config_hash IS NULL"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 0  # all rows have a hash


# --------------------------------------------------------------------------- #
# 6. get_active_setup_config reads
# --------------------------------------------------------------------------- #
def test_get_active_setup_config_returns_correct_type(seeded_prod: ConfigService) -> None:
    for setup_type in constants.ALLOWED_SETUP_TYPES:
        result = seeded_prod.get_active_setup_config("prod", setup_type)
        assert result.is_ok(), f"{setup_type}: {result.errors}"
        assert result.metadata["setup_type"] == setup_type
        assert "config_json" in result.metadata
        cfg = result.metadata["config_json"]
        assert isinstance(cfg, dict)
        assert cfg.get("setup_type") == setup_type


def test_get_all_active_setup_configs_returns_four(seeded_prod: ConfigService) -> None:
    result = seeded_prod.get_all_active_setup_configs("prod")
    assert result.is_ok()
    assert result.rows_processed == 4
    configs = result.metadata["configs_by_type"]
    assert set(configs.keys()) == set(constants.ALLOWED_SETUP_TYPES)


# --------------------------------------------------------------------------- #
# 7. get_active_risk_label_config reads
# --------------------------------------------------------------------------- #
def test_get_active_risk_label_config_returns_config(seeded_prod: ConfigService) -> None:
    result = seeded_prod.get_active_risk_label_config("prod")
    assert result.is_ok()
    cfg = result.metadata["config_json"]
    assert isinstance(cfg, dict)
    assert "factor_weights" in cfg
    assert "thresholds" in cfg
    assert "buy_rules" in cfg
    assert "ranking" in cfg


def test_get_active_risk_label_config_no_config_fails(prod_schema: ConfigService) -> None:
    # Not seeded yet — should fail
    result = prod_schema.get_active_risk_label_config("prod")
    assert result.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# 8. validate_setup_config
# --------------------------------------------------------------------------- #
def test_validate_setup_config_valid_breakout(prod_schema: ConfigService) -> None:
    cfg = default_configs.get_default_setup_configs()["breakout"]
    result = prod_schema.validate_setup_config(cfg)
    assert result.is_ok()
    assert "config_hash" in result.metadata


def test_validate_setup_config_invalid_setup_type_fails(prod_schema: ConfigService) -> None:
    cfg = {"setup_type": "aggressive", "scoring_weights": {"a": 1.0}}
    result = prod_schema.validate_setup_config(cfg)
    assert result.status == service_result.STATUS_FAILED


def test_validate_setup_config_bad_weights_sum_fails(prod_schema: ConfigService) -> None:
    cfg = {
        "setup_type": "breakout",
        "scoring_weights": {"a": 0.5, "b": 0.3},  # sum = 0.8
    }
    result = prod_schema.validate_setup_config(cfg)
    assert result.status == service_result.STATUS_FAILED


def test_validate_setup_config_good_weights_sum_passes(prod_schema: ConfigService) -> None:
    cfg = {
        "setup_type": "pullback",
        "scoring_weights": {"a": 0.5, "b": 0.5},
    }
    result = prod_schema.validate_setup_config(cfg)
    assert result.is_ok()


# --------------------------------------------------------------------------- #
# 9. validate_risk_label_config
# --------------------------------------------------------------------------- #
def test_validate_risk_label_config_valid(prod_schema: ConfigService) -> None:
    cfg = default_configs.get_default_risk_label_config()
    result = prod_schema.validate_risk_label_config(cfg)
    assert result.is_ok()


def test_validate_risk_label_config_bad_weights_fails(prod_schema: ConfigService) -> None:
    cfg = {
        "factor_weights": {"stop_distance_pct": 0.5, "atr_pct": 0.3},  # sum = 0.8
        "thresholds": {"low_max": 33, "med_max": 66},
    }
    result = prod_schema.validate_risk_label_config(cfg)
    assert result.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# 10. Universe config parity
# --------------------------------------------------------------------------- #
def test_universe_config_parity_passes_on_default_seeds(seeded_prod: ConfigService) -> None:
    result = seeded_prod.assert_universe_config_parity("prod")
    assert result.is_ok(), result.errors


# --------------------------------------------------------------------------- #
# 11. Sector alias seeding
# --------------------------------------------------------------------------- #
def test_seed_sector_alias_map_inserts_rows(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_sector_alias_map("prod")
    assert result.is_ok()
    n = _count("prod", "SELECT COUNT(*) FROM sector_alias_map")
    assert n > 0


def test_seed_sector_alias_map_is_idempotent(prod_schema: ConfigService) -> None:
    prod_schema.seed_sector_alias_map("prod")
    prod_schema.seed_sector_alias_map("prod")
    n = _count("prod", "SELECT COUNT(*) FROM sector_alias_map")
    assert n == len(constants.SECTOR_ALIAS_MAP)


# --------------------------------------------------------------------------- #
# 12. seed_defaults convenience
# --------------------------------------------------------------------------- #
def test_seed_defaults_all_succeed(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_defaults("prod")
    assert result.is_ok(), result.errors
    assert result.metadata["setup_seeded"] == 4
    assert result.metadata["risk_label_seeded"] == 1
    assert result.metadata["sector_alias_seeded"] > 0


# --------------------------------------------------------------------------- #
# 13. ServiceResult contract
# --------------------------------------------------------------------------- #
def test_seed_setup_configs_returns_service_result(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_default_setup_configs("prod")
    assert result.has_valid_status()
    assert result.run_id


def test_get_active_setup_config_returns_service_result(seeded_prod: ConfigService) -> None:
    result = seeded_prod.get_active_setup_config("prod", "breakout")
    assert result.has_valid_status()
    assert isinstance(result.rows_processed, int)


# --------------------------------------------------------------------------- #
# 14. config_validator module
# --------------------------------------------------------------------------- #
def test_allowed_setup_types_match_constants() -> None:
    assert set(cv.ALLOWED_SETUP_TYPES) == set(constants.ALLOWED_SETUP_TYPES)


def test_validate_setup_type_valid() -> None:
    for st in constants.ALLOWED_SETUP_TYPES:
        assert cv.validate_setup_type(st) == st


def test_validate_setup_type_rejects_legacy() -> None:
    for bad in ("normal", "aggressive", "conservative", "trend_pullback",
                "volatility_squeeze", "trend_resume", "high_tight_flag", "unknown"):
        with pytest.raises(cv.ConfigValidationError):
            cv.validate_setup_type(bad)


def test_validate_db_role_valid() -> None:
    for role in ("prod", "debug", "simulation"):
        assert cv.validate_db_role(role) == role


def test_validate_db_role_rejects_bad() -> None:
    with pytest.raises(cv.ConfigValidationError):
        cv.validate_db_role("nope")


# --------------------------------------------------------------------------- #
# 15. default_configs module
# --------------------------------------------------------------------------- #
def test_default_setup_configs_four_keys() -> None:
    cfgs = default_configs.get_default_setup_configs()
    assert set(cfgs.keys()) == set(constants.ALLOWED_SETUP_TYPES)


def test_default_setup_config_universe_blocks_identical() -> None:
    """All four universe blocks must be identical (AD-22.23 / 01d Module 13)."""
    cfgs = default_configs.get_default_setup_configs()
    blocks = [cv.canonical_json(c["universe"]) for c in cfgs.values()]
    assert len(set(blocks)) == 1, "universe blocks differ across setup configs"


def test_default_setup_config_scoring_weights_sum_to_one() -> None:
    cfgs = default_configs.get_default_setup_configs()
    for setup_type, cfg in cfgs.items():
        weights = cfg.get("scoring_weights", {})
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-6, (
            f"{setup_type} scoring_weights sum {total} != 1.0"
        )


def test_default_risk_label_config_factor_weights_sum_to_one() -> None:
    cfg = default_configs.get_default_risk_label_config()
    weights = cfg.get("factor_weights", {})
    total = sum(weights.values())
    assert abs(total - 1.0) < 1e-6, f"factor_weights sum {total} != 1.0"


def test_default_setup_configs_feature_schema_version() -> None:
    cfgs = default_configs.get_default_setup_configs()
    for setup_type, cfg in cfgs.items():
        fsv = cfg.get("features", {}).get("feature_schema_version")
        assert fsv == constants.FEATURE_SCHEMA_VERSION, (
            f"{setup_type}: feature_schema_version {fsv!r} != {constants.FEATURE_SCHEMA_VERSION!r}"
        )


def test_default_risk_label_config_top_n() -> None:
    cfg = default_configs.get_default_risk_label_config()
    top_n = cfg.get("ranking", {}).get("top_n")
    assert top_n == 20


def test_pullback_rvol_is_hard_false() -> None:
    """pullback must never hard-reject on low RVOL (AD-22.23)."""
    cfgs = default_configs.get_default_setup_configs()
    assert cfgs["pullback"]["validation"]["rvol_is_hard"] is False


def test_consolidation_base_rvol_not_required() -> None:
    """consolidation_base: RVOL not required (AD-22.23)."""
    cfgs = default_configs.get_default_setup_configs()
    assert cfgs["consolidation_base"]["validation"]["rvol_required"] is False


def test_breakout_rvol_is_hard_true() -> None:
    """breakout: RVOL is a hard gate (AD-22.23)."""
    cfgs = default_configs.get_default_setup_configs()
    assert cfgs["breakout"]["validation"]["rvol_is_hard"] is True
