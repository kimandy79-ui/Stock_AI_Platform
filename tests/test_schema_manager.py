"""Tests for Module 03 Schema Manager — setup-mode migration (AD-22.19–22.24).

Covers:
- Fresh schema creation succeeds for prod/debug/simulation.
- Schema creation is idempotent.
- Exact expected production tables exist; no extras.
- Exact expected simulation tables; no prod leakage.
- Production indexes and views exist; simulation has no prod views.
- selected_proposals_current uses in_diversified_top_n = TRUE.
- schema_versions seeded with correct (schema_name, version) key.
- daily_features has features_v02 structural columns, plus the features_v03
  rs_percentile_126d addition (P1.1, 2026-07-08).
- Static source scan: no ALTER TABLE, no duckdb.connect(, no CREATE TYPE/ENUM.
- ServiceResult metadata contract.

All DB paths redirected into tmp_path; no real data/duckdb/ files touched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.utils import service_result
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# Expected objects (setup-mode schema)
# --------------------------------------------------------------------------- #
EXPECTED_PROD_TABLES: frozenset[str] = frozenset({
    "schema_versions",
    "pipeline_runs",
    "pipeline_locks",
    "ticker_master",
    "ticker_universe_snapshot",
    "sector_etf_map",
    "daily_prices",
    "daily_features",
    "setup_configs",
    "risk_label_config",
    "sector_alias_map",
    "step3_candidates",
    "step4_analysis",
    "step5_proposals",
    "outcome_tracking_queue",
    "signal_outcomes",
    "earnings_calendar",
    "ticker_fundamentals",
    "macro_events_calendar",
    "data_repair_queue",
    "feature_rebuild_log",
    "ai_reviews",
    "execution_decisions",
    "pipeline_run_diagnostics",
    "config_recommendations",
})

EXPECTED_SIM_TABLES: frozenset[str] = frozenset({
    "schema_versions",
    # Config tables shared with prod/debug
    "setup_configs",
    "risk_label_config",
    "sector_alias_map",
    # Simulation-specific
    "sim_runs",
    "sim_folds",
    "sim_step3_candidates",
    "sim_step4_analysis",
    "sim_step5_proposals",
    "sim_signal_outcomes",
    "sim_config_comparisons",
    "sim_ai_reviews",
})

FORBIDDEN_IN_SIM: frozenset[str] = frozenset({
    "daily_prices",
    "daily_features",
    "ticker_master",
    "step5_proposals",
    "signal_outcomes",
    "pipeline_runs",
    "pipeline_locks",
    "config_recommendations",
    "ticker_fundamentals",
})

EXPECTED_PROD_INDEXES: frozenset[str] = frozenset({
    "idx_daily_prices_ticker_date",
    "idx_daily_features_ticker_date",
    "idx_step3_run_date",
    "idx_step4_run_setup",
    "idx_step5_run_date_selected",
    "idx_step5_run_setup",
    "idx_step5_run_raw_rank",
    "idx_step5_run_div_rank",
    "idx_outcomes_setup_date",
    "idx_queue_status_eval",
    "idx_repair_status",
    "idx_diag_run_date",
    "idx_diag_run_step",
    "idx_config_recs_setup_regime_status",
})

EXPECTED_SIM_INDEXES: frozenset[str] = frozenset({
    "idx_sim_props_run_date",
    "idx_sim_outcomes_setup_date",
})

EXPECTED_PROD_VIEWS: frozenset[str] = frozenset({
    "daily_features_current",
    "selected_proposals_current",
})

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset({
    "db_role",
    "tables_created",
    "indexes_created",
    "views_created",
    "schema_version",
    "seed_row_inserted",
})

# features_v02 structural columns (01b daily_features schema)
FEATURES_V02_COLUMNS: frozenset[str] = frozenset({
    "ema20_slope", "ema50_slope", "atr_compression_score",
    "pullback_depth_pct", "swing_high", "swing_low",
    "support_level", "resistance_level", "next_resistance_level",
    "base_high", "base_low", "range_width_pct", "range_duration",
    "range_tightness_score", "volume_dry_up_score", "volume_expansion_score",
    "relative_strength_vs_spy",
})

# P1.1 (2026-07-08): features_v03 adds exactly one column over v02.
FEATURES_V03_COLUMNS: frozenset[str] = frozenset({
    "rs_percentile_126d",
})


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
    return {
        dbm.DB_ROLE_PROD: prod,
        dbm.DB_ROLE_DEBUG: debug,
        dbm.DB_ROLE_SIMULATION: simulation,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _tables(role: str) -> set[str]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _views(role: str) -> set[str]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'VIEW'"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _indexes(role: str) -> set[str]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


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
    return [row[0] for row in rows]


def _schema_version_rows(role: str) -> list[tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT schema_name, version, applied_at, notes FROM schema_versions"
        ).fetchall()
    finally:
        conn.close()
    return rows


# --------------------------------------------------------------------------- #
# Fresh creation
# --------------------------------------------------------------------------- #
class TestFreshCreation:
    @pytest.mark.parametrize(
        "role, apply_fn",
        [
            ("prod", sm.apply_prod_schema),
            ("debug", sm.apply_debug_schema),
            ("simulation", sm.apply_simulation_schema),
        ],
    )
    def test_fresh_creation_succeeds(
        self, role: str, apply_fn: Callable[[], ServiceResult], tmp_db_paths: dict[str, Path]
    ) -> None:
        result = apply_fn()
        assert isinstance(result, ServiceResult)
        assert result.status == service_result.STATUS_SUCCESS
        assert result.is_ok()
        assert not result.errors
        assert tmp_db_paths[role].exists()

    def test_apply_schema_generic_matches_helpers(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema("prod")
        assert result.status == service_result.STATUS_SUCCESS
        assert result.metadata["db_role"] == "prod"

    def test_unknown_role_returns_failed(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema("nope")
        assert result.status == service_result.STATUS_FAILED
        assert result.errors
        assert not result.is_ok()


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
class TestIdempotency:
    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_double_apply_is_stable(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        first = sm.apply_schema(role)
        second = sm.apply_schema(role)
        assert first.status == service_result.STATUS_SUCCESS
        assert second.status == service_result.STATUS_SUCCESS
        assert sorted(first.metadata["tables_created"]) == sorted(
            second.metadata["tables_created"]
        )
        assert first.metadata["seed_row_inserted"] is True
        assert second.metadata["seed_row_inserted"] is False

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_double_apply_single_schema_version_row(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        sm.apply_schema(role)
        rows = _schema_version_rows(role)
        assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Production tables
# --------------------------------------------------------------------------- #
class TestProductionTables:
    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_exact_production_tables(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_schema(role)
        assert _tables(role) == set(EXPECTED_PROD_TABLES)

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_no_unexpected_extra_tables(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_schema(role)
        extra = _tables(role) - set(EXPECTED_PROD_TABLES)
        assert extra == set()

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_metadata_tables_match_db(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema(role)
        assert set(result.metadata["tables_created"]) == set(EXPECTED_PROD_TABLES)
        assert result.rows_processed == len(EXPECTED_PROD_TABLES)

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_no_strategy_configs_table(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        """Legacy strategy_configs must not exist in the setup-mode schema."""
        sm.apply_schema(role)
        assert "strategy_configs" not in _tables(role)

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_setup_configs_and_risk_label_config_exist(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        tables = _tables(role)
        assert "setup_configs" in tables
        assert "risk_label_config" in tables


# --------------------------------------------------------------------------- #
# Simulation tables
# --------------------------------------------------------------------------- #
class TestSimulationTables:
    def test_exact_simulation_tables(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_simulation_schema()
        assert _tables("simulation") == set(EXPECTED_SIM_TABLES)

    def test_no_production_tables_in_simulation(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_simulation_schema()
        assert _tables("simulation").isdisjoint(FORBIDDEN_IN_SIM)

    def test_simulation_metadata_tables(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_simulation_schema()
        assert set(result.metadata["tables_created"]) == set(EXPECTED_SIM_TABLES)
        assert result.rows_processed == len(EXPECTED_SIM_TABLES)


# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #
class TestIndexes:
    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_production_indexes_present(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema(role)
        assert EXPECTED_PROD_INDEXES <= _indexes(role)
        assert result.metadata["indexes_created"] == len(EXPECTED_PROD_INDEXES)

    def test_simulation_indexes_present(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_simulation_schema()
        assert EXPECTED_SIM_INDEXES <= _indexes("simulation")
        assert result.metadata["indexes_created"] == len(EXPECTED_SIM_INDEXES)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
class TestViews:
    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_production_views_present(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema(role)
        assert EXPECTED_PROD_VIEWS <= _views(role)
        assert result.metadata["views_created"] == len(EXPECTED_PROD_VIEWS)

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_selected_proposals_view_uses_diversified_condition(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        conn = dbm.connect(role, read_only=True)
        try:
            row = conn.execute(
                "SELECT sql FROM duckdb_views() WHERE view_name = 'selected_proposals_current'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        view_sql = row[0].lower()
        assert "in_diversified_top_n" in view_sql
        assert "selected_flag" not in view_sql

    def test_simulation_has_no_production_views(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_simulation_schema()
        assert _views("simulation").isdisjoint(EXPECTED_PROD_VIEWS)
        assert result.metadata["views_created"] == 0


# --------------------------------------------------------------------------- #
# schema_versions seed
# --------------------------------------------------------------------------- #
class TestSchemaVersionsSeed:
    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_exact_seed_key(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_schema(role)
        rows = _schema_version_rows(role)
        assert len(rows) == 1
        schema_name, version, applied_at, notes = rows[0]
        assert schema_name == role
        assert version == "schema_v02"
        assert applied_at is not None
        assert notes is not None and str(notes).strip() != ""

    def test_schema_version_metadata_value(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_prod_schema()
        assert result.metadata["schema_version"] == "schema_v02"


# --------------------------------------------------------------------------- #
# features_v02 structural columns
# --------------------------------------------------------------------------- #
class TestFeaturesV02Columns:
    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_features_v02_structural_columns_exist(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        cols = set(_columns(role, "daily_features"))
        missing = FEATURES_V02_COLUMNS - cols
        assert missing == set(), f"Missing features_v02 columns: {missing}"

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_feature_schema_version_column_exists(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        assert "feature_schema_version" in _columns(role, "daily_features")
        assert isinstance(constants.FEATURE_SCHEMA_VERSION, str)
        # P1.1 (2026-07-08): bumped from features_v02 to features_v03.
        assert constants.FEATURE_SCHEMA_VERSION == "features_v03"

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_features_v03_columns_exist(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        cols = set(_columns(role, "daily_features"))
        missing = FEATURES_V03_COLUMNS - cols
        assert missing == set(), f"Missing features_v03 columns: {missing}"

    def test_feature_value_fits_column(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        conn = dbm.connect("prod", read_only=False)
        try:
            conn.execute(
                "INSERT INTO daily_features "
                "(ticker, feature_date, feature_cutoff_date, feature_schema_version, calculated_at) "
                "VALUES (?, DATE '2024-01-02', DATE '2024-01-02', ?, now())",
                ["TEST", constants.FEATURE_SCHEMA_VERSION],
            )
            value = conn.execute(
                "SELECT feature_schema_version FROM daily_features WHERE ticker = 'TEST'"
            ).fetchone()
        finally:
            conn.close()
        assert value == (constants.FEATURE_SCHEMA_VERSION,)


# --------------------------------------------------------------------------- #
# setup_configs / risk_label_config schema
# --------------------------------------------------------------------------- #
class TestSetupConfigSchema:
    def test_setup_configs_columns(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "setup_configs")
        expected = [
            "config_id", "setup_type", "version", "parent_config_id",
            "config_json", "config_hash", "active_flag", "created_at", "notes",
        ]
        assert cols == expected

    def test_risk_label_config_columns(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "risk_label_config")
        expected = [
            "config_id", "version", "config_json", "config_hash",
            "active_flag", "created_at", "notes",
        ]
        assert cols == expected

    def test_step3_has_routing_columns(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "step3_candidates")
        assert "routing_status" in cols
        assert "routing_fail_reason" in cols
        assert "routed_setup_types" in cols
        assert "passed_eligibility" in cols
        # Legacy columns must NOT exist
        assert "strategy_config_id" not in cols
        assert "passed_hard_filters" not in cols

    def test_step4_has_setup_mode_columns(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "step4_analysis")
        assert "setup_config_id" in cols
        assert "setup_type" in cols
        assert "setup_passed" in cols
        assert "target_is_structural" in cols
        assert "market_regime" in cols
        # Legacy columns must NOT exist
        assert "strategy_config_id" not in cols

    def test_step5_has_risk_label_and_disposition(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "step5_proposals")
        assert "setup_config_id" in cols
        assert "setup_type" in cols
        assert "risk_score" in cols
        assert "risk_label" in cols
        assert "disposition" in cols
        assert "target_is_structural" in cols
        # Legacy columns must NOT exist
        assert "strategy_config_id" not in cols

    def test_signal_outcomes_has_setup_type_risk_label(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "signal_outcomes")
        assert "setup_type" in cols
        assert "risk_label" in cols
        assert "stop_hit" in cols
        assert "target_hit" in cols
        assert "stop_price_raw" in cols
        assert "target_price_raw" in cols


# --------------------------------------------------------------------------- #
# Static source scan
# --------------------------------------------------------------------------- #
class TestStaticSourceScan:
    def _raw_source(self) -> str:
        return Path(sm.__file__).read_text(encoding="utf-8")

    def _sql_literals(self) -> str:
        chunks: list[str] = [sm._SCHEMA_VERSIONS_DDL]
        chunks.extend(sm._PROD_TABLE_DDL)
        chunks.extend(sm._PROD_INDEX_DDL)
        chunks.extend(sm._PROD_VIEW_DDL)
        chunks.extend(sm._SIM_TABLE_DDL)
        chunks.extend(sm._SIM_INDEX_DDL)
        return "\n".join(chunks)

    def test_no_alter_table_in_sql(self) -> None:
        assert "ALTER TABLE" not in self._sql_literals().upper()

    def test_no_direct_duckdb_connect(self) -> None:
        assert "duckdb.connect(" not in self._raw_source()

    def test_no_create_type_in_sql(self) -> None:
        assert "CREATE TYPE" not in self._sql_literals().upper()

    def test_no_enum_in_sql(self) -> None:
        assert "ENUM" not in self._sql_literals().upper()

    def test_no_strategy_configs_in_sql(self) -> None:
        """setup-mode: strategy_configs must not appear as a created table."""
        sql = self._sql_literals().lower()
        # Must not create strategy_configs
        assert "create table if not exists strategy_configs" not in sql


# --------------------------------------------------------------------------- #
# ServiceResult contract
# --------------------------------------------------------------------------- #
class TestServiceResultContract:
    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_valid_service_result(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema(role)
        assert isinstance(result, ServiceResult)
        assert result.has_valid_status()
        assert result.run_id
        assert isinstance(result.rows_processed, int)

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_metadata_keys_present(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema(role)
        assert REQUIRED_METADATA_KEYS <= set(result.metadata.keys())
        assert result.metadata["db_role"] == role
        assert isinstance(result.metadata["tables_created"], list)
        assert result.metadata["schema_version"] == "schema_v02"
        assert isinstance(result.metadata["seed_row_inserted"], bool)

    def test_tables_created_sorted(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_prod_schema()
        tables = result.metadata["tables_created"]
        assert tables == sorted(tables)


# --------------------------------------------------------------------------- #
# Import smoke
# --------------------------------------------------------------------------- #
class TestImportSmoke:
    def test_public_symbols_present(self) -> None:
        for name in (
            "apply_schema",
            "apply_prod_schema",
            "apply_debug_schema",
            "apply_simulation_schema",
            "DATABASE_SCHEMA_VERSION",
        ):
            assert hasattr(sm, name)

    def test_database_schema_version_value(self) -> None:
        assert sm.DATABASE_SCHEMA_VERSION == "schema_v02"


# --------------------------------------------------------------------------- #
# Phase 3 — review_kind column (multi-pass AI review)
# --------------------------------------------------------------------------- #
class TestReviewKindColumn:
    def test_ai_reviews_has_review_kind(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "ai_reviews")
        assert "review_kind" in cols
        assert "review_type" in cols  # unchanged, distinct dimension

    def test_sim_ai_reviews_has_review_kind_not_review_type(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_simulation_schema()
        cols = _columns("simulation", "sim_ai_reviews")
        assert "review_kind" in cols
        assert "review_type" not in cols  # sim_ai_reviews never had this column

    def test_review_kind_column_idempotent_on_reapply(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
        first = _columns("prod", "ai_reviews")
        assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
        second = _columns("prod", "ai_reviews")
        assert first == second
        assert first.count("review_kind") == 1

    def test_config_recommendations_table_still_present(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        """Unrelated table added the same session (Phase 2) -- quick sanity
        check the two schema deltas coexist without collision."""
        sm.apply_prod_schema()
        assert "config_recommendations" in _tables("prod")


# --------------------------------------------------------------------------- #
# Phase 4 — ticker_fundamentals companion table
# --------------------------------------------------------------------------- #
class TestTickerFundamentalsSchema:
    def test_columns(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_prod_schema()
        cols = _columns("prod", "ticker_fundamentals")
        expected = [
            "ticker", "as_of_date", "eps_growth_trend", "leverage_ratio",
            "valuation_band", "piotroski_f_score", "altman_z_score",
            "insider_trade_flag", "institutional_ownership_delta",
            "source_provider", "calculated_at",
        ]
        assert cols == expected

    def test_not_in_simulation_schema(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_simulation_schema()
        assert "ticker_fundamentals" not in _tables("simulation")

    def test_idempotent_on_reapply(self, tmp_db_paths: dict[str, Path]) -> None:
        assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
        first = _columns("prod", "ticker_fundamentals")
        assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
        assert _columns("prod", "ticker_fundamentals") == first
