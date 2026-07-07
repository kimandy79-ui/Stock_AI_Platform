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


def test_dead_key_template_edit_does_not_touch_already_active_row(
    prod_schema: ConfigService,
) -> None:
    """CODER_NOTE v3 items 1/4/5 — proves the idempotency assumption: editing
    a seed template (removing dead keys) never mutates a row that was already
    inserted under the same config_id. Simulates a pre-existing DB by
    inserting an 'old-shaped' payload (dead keys present) directly, then
    re-running the seeder with today's (dead-key-free) template and confirming
    the stored row is untouched."""
    old_shaped_payload = default_configs.get_default_setup_configs()["breakout"]
    old_shaped_payload["validation"] = {
        **old_shaped_payload["validation"],
        "min_close_strength": 0.5,       # removed from the real template (item 5)
        "max_stop_distance_pct": 0.10,   # removed from the real template (item 1)
    }
    old_shaped_json = cv.canonical_json(old_shaped_payload)

    conn = dbm.connect("prod")
    try:
        conn.execute(
            "INSERT INTO setup_configs "
            "(config_id, setup_type, version, config_json, config_hash, "
            " active_flag, created_at, notes) "
            "VALUES (?, ?, ?, ?, ?, TRUE, now(), 'pre-existing row')",
            [
                old_shaped_payload["config_id"],
                old_shaped_payload["setup_type"],
                old_shaped_payload["version"],
                old_shaped_json,
                cv.deterministic_hash(old_shaped_payload),
            ],
        )
    finally:
        conn.close()

    result = prod_schema.seed_default_setup_configs("prod")
    assert result.is_ok()
    # Only the other 3 setup_types get inserted; "setup_breakout_v1" already
    # exists under that config_id, so ON CONFLICT DO NOTHING skips it.
    assert result.rows_processed == 3

    conn = dbm.connect("prod", read_only=True)
    try:
        row = conn.execute(
            "SELECT config_json FROM setup_configs WHERE config_id = ?",
            [old_shaped_payload["config_id"]],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    stored_json = row[0] if isinstance(row[0], str) else cv.canonical_json(row[0])
    # Old-shaped row (with dead keys) is exactly as inserted — untouched by
    # re-seeding with the new (dead-key-free) template.
    assert "min_close_strength" in stored_json
    assert "max_stop_distance_pct" in stored_json


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
# 8b. validate_setup_config — pullback rvol_is_hard rejection (CODER_NOTE v3
# item 3: validate at authoring time instead of silently overriding at runtime)
# --------------------------------------------------------------------------- #
def test_validate_setup_config_pullback_rvol_is_hard_true_fails(
    prod_schema: ConfigService,
) -> None:
    cfg = {
        "setup_type": "pullback",
        "scoring_weights": {"a": 1.0},
        "validation": {"rvol_is_hard": True},
    }
    result = prod_schema.validate_setup_config(cfg)
    assert result.status == service_result.STATUS_FAILED
    assert "rvol_is_hard" in "; ".join(result.errors)


def test_validate_setup_config_pullback_rvol_is_hard_false_passes(
    prod_schema: ConfigService,
) -> None:
    cfg = default_configs.get_default_setup_configs()["pullback"]
    result = prod_schema.validate_setup_config(cfg)
    assert result.is_ok()


def test_validate_setup_config_pullback_rvol_is_hard_absent_passes(
    prod_schema: ConfigService,
) -> None:
    cfg = {
        "setup_type": "pullback",
        "scoring_weights": {"a": 1.0},
        "validation": {},
    }
    result = prod_schema.validate_setup_config(cfg)
    assert result.is_ok()


def test_validate_setup_config_non_pullback_rvol_is_hard_true_passes(
    prod_schema: ConfigService,
) -> None:
    """rvol_is_hard=True is the correct/expected value for breakout — only
    pullback is restricted (AD-22.23 is pullback-specific)."""
    cfg = default_configs.get_default_setup_configs()["breakout"]
    assert cfg["validation"]["rvol_is_hard"] is True
    result = prod_schema.validate_setup_config(cfg)
    assert result.is_ok()


def test_validate_setup_config_every_preset_still_passes(prod_schema: ConfigService) -> None:
    """All 6 presets (incl. the 2 pullback ones) keep rvol_is_hard=False and
    must still pass after adding the new pullback check."""
    for p in default_configs.get_preset_setup_configs():
        result = prod_schema.validate_setup_config(p)
        assert result.is_ok(), f"{p['config_id']}: {result.errors}"


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


# --------------------------------------------------------------------------- #
# 16. Preset setup configs (Phase 1.5 — literature-anchored simulation-sweep
# inputs). Never active; never touch prod/debug's one-active-per-setup_type
# invariant.
# --------------------------------------------------------------------------- #
_EXPECTED_PRESET_IDS = {
    "setup_breakout_canonical",
    "setup_breakout_strict",
    "setup_consolidation_base_strict",
    "setup_trend_continuation_template",
    "setup_pullback_shallow",
    "setup_pullback_fib",
}


def test_preset_setup_configs_six_entries_all_setup_types_covered() -> None:
    presets = default_configs.get_preset_setup_configs()
    assert {p["config_id"] for p in presets} == _EXPECTED_PRESET_IDS
    covered_types = {p["setup_type"] for p in presets}
    assert covered_types == set(constants.ALLOWED_SETUP_TYPES)


def test_preset_setup_configs_scoring_weights_sum_to_one() -> None:
    for p in default_configs.get_preset_setup_configs():
        total = sum(p["scoring_weights"].values())
        assert abs(total - 1.0) < 1e-6, f"{p['config_id']} weights sum {total} != 1.0"


def test_preset_setup_configs_universe_block_matches_v1() -> None:
    """Presets share the same universe block as the v1 defaults (parity is a
    run-wide invariant across every config replayed together, per
    ConfigService.assert_universe_config_parity / step3_universal_eligibility)."""
    v1_universe = cv.canonical_json(default_configs.DEFAULT_SETUP_CONFIGS["breakout"]["universe"])
    for p in default_configs.get_preset_setup_configs():
        assert cv.canonical_json(p["universe"]) == v1_universe, p["config_id"]


def test_preset_setup_configs_parent_config_id_references_existing_v1() -> None:
    v1_ids = {cfg["config_id"] for cfg in default_configs.DEFAULT_SETUP_CONFIGS.values()}
    for p in default_configs.get_preset_setup_configs():
        assert p["parent_config_id"] in v1_ids, p["config_id"]


def test_seed_preset_setup_configs_inserts_six_rows(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_preset_setup_configs("prod")
    assert result.is_ok(), result.errors
    assert result.rows_processed == 6
    n = _count(
        "prod",
        "SELECT COUNT(*) FROM setup_configs WHERE config_id = ANY(?)",
        [list(_EXPECTED_PRESET_IDS)],
    )
    assert n == 6


def test_seed_preset_setup_configs_is_idempotent(prod_schema: ConfigService) -> None:
    r1 = prod_schema.seed_preset_setup_configs("prod")
    r2 = prod_schema.seed_preset_setup_configs("prod")
    assert r1.is_ok() and r2.is_ok()
    assert r1.rows_processed == 6
    assert r2.rows_processed == 0  # ON CONFLICT DO NOTHING — no duplicates
    n = _count(
        "prod",
        "SELECT COUNT(*) FROM setup_configs WHERE config_id = ANY(?)",
        [list(_EXPECTED_PRESET_IDS)],
    )
    assert n == 6


def test_seed_preset_setup_configs_never_active(prod_schema: ConfigService) -> None:
    """Presets are simulation-sweep inputs only — never active in prod/debug."""
    prod_schema.seed_preset_setup_configs("prod")
    n_active = _count(
        "prod",
        "SELECT COUNT(*) FROM setup_configs WHERE config_id = ANY(?) AND active_flag = TRUE",
        [list(_EXPECTED_PRESET_IDS)],
    )
    assert n_active == 0


def _active_config_id(setup_type: str) -> str:
    conn = dbm.connect("prod", read_only=True)
    try:
        row = conn.execute(
            "SELECT config_id FROM setup_configs WHERE setup_type = ? AND active_flag = TRUE",
            [setup_type],
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_seed_preset_setup_configs_does_not_disturb_v1_activation(seeded_prod: ConfigService) -> None:
    """Seeding presets after the v1 defaults must not change which config is
    active per setup_type (presets never call activate_setup_config)."""
    before = {st: _active_config_id(st) for st in constants.ALLOWED_SETUP_TYPES}
    seeded_prod.seed_preset_setup_configs("prod")
    after = {st: _active_config_id(st) for st in constants.ALLOWED_SETUP_TYPES}
    assert after == before  # same config_id active per setup_type, unchanged
    for setup_type in constants.ALLOWED_SETUP_TYPES:
        n = _count(
            "prod",
            "SELECT COUNT(*) FROM setup_configs WHERE setup_type = ? AND active_flag = TRUE",
            [setup_type],
        )
        assert n == 1, f"expected exactly 1 active {setup_type} config, got {n}"


def test_seed_preset_setup_configs_with_invalid_db_role_fails(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_preset_setup_configs("bad_role")
    assert result.status == service_result.STATUS_FAILED


def test_validate_setup_config_accepts_every_preset(prod_schema: ConfigService) -> None:
    """Each preset passes the same structural validation as the v1 defaults
    (required fields present, setup_type valid, scoring_weights sum to 1.0)."""
    for p in default_configs.get_preset_setup_configs():
        result = prod_schema.validate_setup_config(p)
        assert result.is_ok(), f"{p['config_id']}: {result.errors}"


def test_preset_setup_configs_use_only_existing_validator_fields() -> None:
    """Every validation-block key in every preset already appears in the
    matching v1 default's validation block — confirms no new fields were
    invented (Phase 1.5 coder note constraint)."""
    v1_by_type = default_configs.DEFAULT_SETUP_CONFIGS
    for p in default_configs.get_preset_setup_configs():
        st = p["setup_type"]
        allowed_keys = set(v1_by_type[st]["validation"].keys())
        preset_keys = set(p["validation"].keys())
        assert preset_keys <= allowed_keys, (
            f"{p['config_id']} introduces new validation field(s): "
            f"{preset_keys - allowed_keys}"
        )


# --------------------------------------------------------------------------- #
# 10. risk_label_config_v2 (CODER_NOTE v3 item 6) — cloned, never edits v1 in
# place, always seeded inactive, never auto-activated.
# --------------------------------------------------------------------------- #
def test_risk_label_config_v2_not_equal_to_v1_config_id() -> None:
    v1 = default_configs.get_default_risk_label_config()
    v2 = default_configs.get_risk_label_config_v2()
    assert v1["config_id"] != v2["config_id"]
    assert v2["config_id"] == "risk_label_config_v2"


def test_risk_label_config_v2_carries_shared_earnings_macro_block() -> None:
    v2 = default_configs.get_risk_label_config_v2()
    assert v2["earnings"] == {"avoid_within_bd": 5, "penalty_points_max": -15}
    assert v2["macro_event_risk"]["enabled"] is True
    assert v2["macro_event_risk"]["penalty_points"] == -10


def test_risk_label_config_v2_values_match_per_setup_copies() -> None:
    """Zero-behavior-change guarantee: v2's shared block must equal what every
    setup_config's own earnings/macro_event_risk block already carries."""
    v2 = default_configs.get_risk_label_config_v2()
    for cfg in default_configs.get_default_setup_configs().values():
        assert cfg["earnings"] == v2["earnings"]
        assert cfg["macro_event_risk"] == v2["macro_event_risk"]


def test_default_risk_label_config_v1_has_no_shared_earnings_macro_block() -> None:
    """v1 (currently active in every prod/debug DB) must NOT carry these keys —
    this is what makes the dual-read fallback in m14_setup_validators.py a
    no-op today (falls back to each setup_config's own copy)."""
    v1 = default_configs.get_default_risk_label_config()
    assert "earnings" not in v1
    assert "macro_event_risk" not in v1


def test_seed_risk_label_config_v2_inserts_one_row(prod_schema: ConfigService) -> None:
    result = prod_schema.seed_risk_label_config_v2("prod")
    assert result.is_ok(), result.errors
    assert result.rows_processed == 1
    n = _count(
        "prod",
        "SELECT COUNT(*) FROM risk_label_config WHERE config_id = ?",
        ["risk_label_config_v2"],
    )
    assert n == 1


def test_seed_risk_label_config_v2_is_idempotent(prod_schema: ConfigService) -> None:
    r1 = prod_schema.seed_risk_label_config_v2("prod")
    r2 = prod_schema.seed_risk_label_config_v2("prod")
    assert r1.is_ok() and r2.is_ok()
    assert r1.rows_processed == 1
    assert r2.rows_processed == 0  # ON CONFLICT DO NOTHING


def test_seed_risk_label_config_v2_never_active(prod_schema: ConfigService) -> None:
    prod_schema.seed_risk_label_config_v2("prod")
    n_active = _count(
        "prod",
        "SELECT COUNT(*) FROM risk_label_config WHERE config_id = ? AND active_flag = TRUE",
        ["risk_label_config_v2"],
    )
    assert n_active == 0


def test_seed_risk_label_config_v2_does_not_disturb_v1_activation(
    seeded_prod: ConfigService,
) -> None:
    before = _count(
        "prod", "SELECT COUNT(*) FROM risk_label_config WHERE active_flag = TRUE"
    )
    seeded_prod.seed_risk_label_config_v2("prod")
    after = _count(
        "prod", "SELECT COUNT(*) FROM risk_label_config WHERE active_flag = TRUE"
    )
    assert before == after == 1  # still exactly one active risk_label_config


def test_seed_risk_label_config_v2_with_invalid_db_role_fails(
    prod_schema: ConfigService,
) -> None:
    result = prod_schema.seed_risk_label_config_v2("bad_role")
    assert result.status == service_result.STATUS_FAILED


def test_validate_risk_label_config_accepts_v2(prod_schema: ConfigService) -> None:
    v2 = default_configs.get_risk_label_config_v2()
    result = prod_schema.validate_risk_label_config(v2)
    assert result.is_ok(), result.errors
