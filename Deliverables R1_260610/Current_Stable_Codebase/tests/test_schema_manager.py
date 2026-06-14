"""Tests for Module 03 — Schema Manager.

Covers, per the Module 03 task requirements and ``CODING_STANDARDS.md``
section 12:

- fresh schema creation succeeds for ``prod``, ``debug``, ``simulation``;
- schema creation is idempotent (calling twice does not raise and does not
  duplicate ``schema_versions`` rows);
- exact expected production tables exist for ``prod`` / ``debug`` and no extra
  tables are created;
- exact expected simulation tables exist for ``simulation`` and no production
  table leaks into the simulation database;
- production indexes / views exist for ``prod`` / ``debug``; simulation indexes
  exist; production views are absent from simulation;
- ``selected_proposals_current`` uses ``in_diversified_top_n = TRUE`` (PATCH 10)
  and not the legacy ``selected_flag = TRUE``;
- ``schema_versions`` is seeded with the exact ``(schema_name, version)`` key,
  non-NULL ``applied_at`` and non-empty ``notes``;
- ``daily_features.feature_schema_version`` exists and is compatible with
  ``constants.FEATURE_SCHEMA_VERSION``;
- a static source scan forbids ``ALTER TABLE``, direct ``duckdb.connect(``,
  ``CREATE TYPE`` / ``ENUM``, and migration framework usage;
- the public entry points return a valid ``ServiceResult`` with the required
  metadata keys;
- type/import smoke checks.

CRITICAL: every test that opens a DuckDB connection redirects the settings DB
paths into pytest ``tmp_path`` via ``monkeypatch.setattr(settings, ...)`` so
that no real file under the real ``data/duckdb/`` folder is created or modified.
This mirrors the isolation discipline of ``tests/test_duckdb_manager.py``.
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
# Expected object names (kept local to the test so the test fails loudly if the
# implementation drifts from SCHEMA_SPEC.md, rather than re-using the module's
# own constants as ground truth).
# --------------------------------------------------------------------------- #
EXPECTED_PROD_TABLES: frozenset[str] = frozenset(
    {
        "schema_versions",
        "pipeline_runs",
        "pipeline_locks",
        "ticker_master",
        "ticker_universe_snapshot",
        "sector_etf_map",
        "daily_prices",
        "daily_features",
        "strategy_configs",
        "step3_candidates",
        "step4_analysis",
        "step5_proposals",
        "outcome_tracking_queue",
        "signal_outcomes",
        "earnings_calendar",
        "macro_events_calendar",
        "data_repair_queue",
        "feature_rebuild_log",
        "ai_reviews",
        "execution_decisions",
    }
)

EXPECTED_SIM_TABLES: frozenset[str] = frozenset(
    {
        "schema_versions",
        "sim_runs",
        "sim_folds",
        "sim_step3_candidates",
        "sim_step4_analysis",
        "sim_step5_proposals",
        "sim_signal_outcomes",
        "sim_config_comparisons",
        "sim_ai_reviews",
    }
)

# Production tables that must NEVER appear in the simulation database.
FORBIDDEN_IN_SIM: frozenset[str] = frozenset(
    {
        "daily_prices",
        "daily_features",
        "ticker_master",
        "step5_proposals",
        "signal_outcomes",
    }
)

EXPECTED_PROD_INDEXES: frozenset[str] = frozenset(
    {
        "idx_daily_prices_ticker_date",
        "idx_daily_features_ticker_date",
        "idx_step3_run_date",
        "idx_step5_run_date_selected",
        "idx_outcomes_config_date",
        "idx_queue_status_eval",
        "idx_repair_status",
        "idx_step5_run_raw_rank",
        "idx_step5_run_div_rank",
    }
)

EXPECTED_SIM_INDEXES: frozenset[str] = frozenset(
    {
        "idx_sim_props_run_date",
        "idx_sim_outcomes_config_date",
    }
)

EXPECTED_PROD_VIEWS: frozenset[str] = frozenset(
    {
        "daily_features_current",
        "selected_proposals_current",
    }
)

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "db_role",
        "tables_created",
        "indexes_created",
        "views_created",
        "schema_version",
        "seed_row_inserted",
    }
)

# Exact, ordered column lists for every table, transcribed directly from
# ``docs/SCHEMA_SPEC.md`` (sections 3 and 4). This is the column-level drift
# guard: if any CREATE TABLE in schema_manager adds, drops, renames, or (when
# checked as a set) reorders a column away from the spec, the per-table column
# test fails loudly. Table constraints (PRIMARY KEY (...)) are not columns and
# are intentionally excluded here.
EXPECTED_COLUMNS: dict[str, list[str]] = {
    "schema_versions": ["schema_name", "version", "applied_at", "notes"],
    "pipeline_runs": ["run_id", "run_date", "run_type", "status", "started_at", "completed_at", "duration_sec", "steps_completed", "error_message", "created_at"],
    "pipeline_locks": ["lock_name", "is_locked", "run_id", "locked_at", "heartbeat_at"],
    "ticker_master": ["ticker", "yahoo_symbol", "company_name", "exchange", "sector", "industry", "security_type", "symbol_type", "active_flag", "delisted_flag", "first_seen", "last_seen", "last_updated"],
    "ticker_universe_snapshot": ["snapshot_month", "ticker", "exchange", "sector", "industry", "market_cap_bucket", "active_flag", "source", "created_at"],
    "sector_etf_map": ["sector", "etf_ticker", "active_flag", "created_at"],
    "daily_prices": ["ticker", "date", "open_raw", "high_raw", "low_raw", "close_raw", "volume_raw", "open_adj", "high_adj", "low_adj", "close_adj", "volume_adj", "dividend_amount", "split_ratio", "adjustment_factor", "source_provider", "data_quality_status", "mutation_flag", "created_at", "updated_at"],
    "daily_features": ["ticker", "feature_date", "feature_cutoff_date", "feature_schema_version", "feature_ready", "ema20", "ema50", "ema200", "ema_alignment_score", "distance_to_ema20_pct", "distance_to_ema50_pct", "distance_to_ema200_pct", "rsi14", "roc20", "atr14", "atr_pct", "rvol20", "avg_volume_20d", "avg_dollar_volume_20d", "distance_from_52w_high_pct", "pullback_from_recent_high_pct", "breakout_proximity", "consolidation_score", "sector_relative_strength", "market_regime", "days_to_earnings_bd", "earnings_confidence", "macro_event_risk_flag", "calculated_at"],
    "strategy_configs": ["config_id", "strategy_name", "version", "parent_config_id", "config_json", "config_hash", "active_flag", "created_at", "notes"],
    "step3_candidates": ["candidate_id", "run_id", "strategy_config_id", "ticker", "signal_date", "screening_score", "passed_hard_filters", "hard_filter_fail_reasons", "soft_score_components", "feature_snapshot_json", "created_at"],
    "step4_analysis": ["analysis_id", "candidate_id", "run_id", "strategy_config_id", "ticker", "signal_date", "setup_type", "setup_score", "breakout_quality_score", "squeeze_score", "timing_score", "confirmation_score", "estimated_rr", "stop_price_raw", "target_price_raw", "earnings_penalty", "macro_penalty", "explanation_json", "created_at"],
    "step5_proposals": ["proposal_id", "run_id", "strategy_config_id", "ticker", "signal_date", "proposal_score_raw", "diversity_penalty", "proposal_score_final", "rank_position", "raw_rank", "diversified_rank", "in_raw_top_n", "in_diversified_top_n", "diversification_applied", "selected_top_n", "selected_flag", "ai_reviewed", "executed_flag", "rejection_reason", "mechanical_explanation", "sector_count_at_selection", "industry_count_at_selection", "created_at"],
    "outcome_tracking_queue": ["tracking_id", "proposal_id", "ticker", "signal_date", "entry_date", "eval_date", "horizon_bd", "status", "repair_attempts", "last_repair_attempt", "created_at", "completed_at"],
    "signal_outcomes": ["outcome_id", "proposal_id", "ticker", "strategy_config_id", "signal_date", "entry_date", "entry_price_raw", "entry_price_sim", "return_5bd_pct", "return_10bd_pct", "return_20bd_pct", "return_40bd_pct", "mfe_40bd_pct", "mae_40bd_pct", "realized_r_multiple", "earnings_within_window", "cross_fold_outcome", "outcome_status", "calculated_at"],
    "earnings_calendar": ["ticker", "earnings_date", "session", "source", "confidence", "updated_at"],
    "macro_events_calendar": ["event_date", "event_type", "importance", "source", "created_at"],
    "data_repair_queue": ["repair_id", "ticker", "repair_date", "repair_reason", "attempts", "max_attempts", "last_attempt", "status", "created_at", "updated_at"],
    "feature_rebuild_log": ["rebuild_id", "ticker", "reason", "affected_start_date", "affected_end_date", "feature_schema_version", "triggered_at", "status"],
    "ai_reviews": ["ai_review_id", "review_type", "proposal_id", "sim_run_id", "provider", "model", "prompt_version", "prompt_text", "selected_tickers_json", "ai_response_text", "human_action", "created_at"],
    "execution_decisions": ["decision_id", "proposal_id", "ai_review_id", "decision_source", "action", "decision_notes", "created_at"],
    "sim_runs": ["sim_run_id", "sim_name", "mode", "start_date", "end_date", "created_at", "config_ids", "status", "notes"],
    "sim_folds": ["fold_id", "sim_run_id", "fold_number", "train_start", "train_end", "test_start", "test_end", "selected_config_id", "created_at"],
    "sim_step3_candidates": ["candidate_id", "sim_run_id", "fold_id", "strategy_config_id", "ticker", "signal_date", "screening_score", "passed_hard_filters", "hard_filter_fail_reasons", "soft_score_components", "created_at"],
    "sim_step4_analysis": ["analysis_id", "candidate_id", "sim_run_id", "fold_id", "strategy_config_id", "ticker", "signal_date", "setup_type", "setup_score", "estimated_rr", "stop_price_raw", "target_price_raw", "created_at"],
    "sim_step5_proposals": ["proposal_id", "sim_run_id", "fold_id", "strategy_config_id", "ticker", "signal_date", "proposal_score_raw", "diversity_penalty", "proposal_score_final", "rank_position", "selected_top_n", "raw_rank", "diversified_rank", "in_raw_top_n", "in_diversified_top_n", "diversification_applied", "rejection_reason", "created_at"],
    "sim_signal_outcomes": ["outcome_id", "sim_run_id", "fold_id", "proposal_id", "ticker", "strategy_config_id", "signal_date", "entry_date", "entry_price_raw", "entry_price_sim", "return_5bd_pct", "return_10bd_pct", "return_20bd_pct", "return_40bd_pct", "mfe_40bd_pct", "mae_40bd_pct", "realized_r_multiple", "list_membership", "cross_fold_outcome", "outcome_status", "calculated_at"],
    "sim_config_comparisons": ["comparison_id", "sim_run_id", "config_id", "horizon_bd", "expectancy", "win_rate", "avg_win", "avg_loss", "profit_factor", "max_drawdown_pct", "resolved_outcomes_pct", "list_type", "created_at"],
    "sim_ai_reviews": ["ai_review_id", "sim_run_id", "provider", "model", "prompt_version", "prompt_text", "ai_response_text", "human_action", "created_at"],
}

# Which role each table belongs to (for the column test to pick a DB that has it).
_PROD_ONLY_TABLES: frozenset[str] = EXPECTED_PROD_TABLES - {"schema_versions"}
_SIM_ONLY_TABLES: frozenset[str] = EXPECTED_SIM_TABLES - {"schema_versions"}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect all DuckDB settings paths into ``tmp_path``.

    Mirrors the fixture in ``tests/test_duckdb_manager.py`` so no test touches
    the real ``data/duckdb/`` tree.
    """
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
# Local DB introspection helpers (read via the approved manager connection)
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


def _table_columns(role: str, table_name: str) -> list[str]:
    """Return the table's columns in definition order (by ordinal_position)."""
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ? "
            "ORDER BY ordinal_position",
            [table_name],
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
# 1-3. Fresh creation succeeds per role
# --------------------------------------------------------------------------- #
class TestFreshCreation:
    """Fresh schema creation succeeds for every role."""

    @pytest.mark.parametrize(
        "role, apply_fn",
        [
            ("prod", sm.apply_prod_schema),
            ("debug", sm.apply_debug_schema),
            ("simulation", sm.apply_simulation_schema),
        ],
    )
    def test_fresh_creation_succeeds(
        self,
        role: str,
        apply_fn: Callable[[], ServiceResult],
        tmp_db_paths: dict[str, Path],
    ) -> None:
        result = apply_fn()
        assert isinstance(result, ServiceResult)
        assert result.status == service_result.STATUS_SUCCESS
        assert result.is_ok()
        assert not result.errors
        assert tmp_db_paths[role].exists()

    def test_apply_schema_generic_matches_helpers(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        # The generic entry point and the role-specific helper agree.
        result = sm.apply_schema("prod")
        assert result.status == service_result.STATUS_SUCCESS
        assert result.metadata["db_role"] == "prod"

    def test_unknown_role_returns_failed(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_schema("nope")
        assert result.status == service_result.STATUS_FAILED
        assert result.errors
        assert not result.is_ok()


# --------------------------------------------------------------------------- #
# 4. Idempotency
# --------------------------------------------------------------------------- #
class TestIdempotency:
    """Calling apply twice is safe and does not duplicate rows."""

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_double_apply_does_not_raise_and_is_stable(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        first = sm.apply_schema(role)
        second = sm.apply_schema(role)

        assert first.status == service_result.STATUS_SUCCESS
        assert second.status == service_result.STATUS_SUCCESS

        # The same set of tables exists after both calls.
        assert sorted(first.metadata["tables_created"]) == sorted(
            second.metadata["tables_created"]
        )
        # First creation seeds the row; the second must not.
        assert first.metadata["seed_row_inserted"] is True
        assert second.metadata["seed_row_inserted"] is False

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_double_apply_single_schema_version_row(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        sm.apply_schema(role)
        rows = _schema_version_rows(role)
        # Exactly one seed row, even after two applications.
        assert len(rows) == 1


# --------------------------------------------------------------------------- #
# 5. Exact production tables
# --------------------------------------------------------------------------- #
class TestProductionTables:
    """prod and debug create exactly the expected production tables."""

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_exact_production_tables(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        present = _tables(role)
        assert present == set(EXPECTED_PROD_TABLES)

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_no_unexpected_extra_tables(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        present = _tables(role)
        extra = present - set(EXPECTED_PROD_TABLES)
        assert extra == set()

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_metadata_tables_match_db(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_schema(role)
        assert set(result.metadata["tables_created"]) == set(EXPECTED_PROD_TABLES)
        assert result.rows_processed == len(EXPECTED_PROD_TABLES)


# --------------------------------------------------------------------------- #
# 6. Exact simulation tables / no production leakage
# --------------------------------------------------------------------------- #
class TestSimulationTables:
    """simulation creates exactly the sim_* tables and no production tables."""

    def test_exact_simulation_tables(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_simulation_schema()
        present = _tables("simulation")
        assert present == set(EXPECTED_SIM_TABLES)

    def test_no_production_tables_in_simulation(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_simulation_schema()
        present = _tables("simulation")
        assert present.isdisjoint(FORBIDDEN_IN_SIM)

    def test_simulation_metadata_tables(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_simulation_schema()
        assert set(result.metadata["tables_created"]) == set(EXPECTED_SIM_TABLES)
        assert result.rows_processed == len(EXPECTED_SIM_TABLES)


# --------------------------------------------------------------------------- #
# 7-8. Indexes
# --------------------------------------------------------------------------- #
class TestIndexes:
    """Production and simulation indexes exist as specified."""

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_production_indexes_present(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_schema(role)
        present = _indexes(role)
        assert EXPECTED_PROD_INDEXES <= present
        # Metadata count reflects exactly the nine explicit production indexes.
        assert result.metadata["indexes_created"] == len(EXPECTED_PROD_INDEXES)

    def test_simulation_indexes_present(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_simulation_schema()
        present = _indexes("simulation")
        assert EXPECTED_SIM_INDEXES <= present
        assert result.metadata["indexes_created"] == len(EXPECTED_SIM_INDEXES)


# --------------------------------------------------------------------------- #
# 9-11. Views
# --------------------------------------------------------------------------- #
class TestViews:
    """Production views exist for prod/debug; simulation has none of them."""

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_production_views_present(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_schema(role)
        present = _views(role)
        assert EXPECTED_PROD_VIEWS <= present
        assert result.metadata["views_created"] == len(EXPECTED_PROD_VIEWS)

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_selected_proposals_view_uses_diversified_condition(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        conn = dbm.connect(role, read_only=True)
        try:
            row = conn.execute(
                "SELECT sql FROM duckdb_views() "
                "WHERE view_name = 'selected_proposals_current'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        view_sql = row[0].lower()
        assert "in_diversified_top_n" in view_sql
        # Must NOT use the legacy selected_flag = TRUE condition.
        assert "selected_flag" not in view_sql

    def test_simulation_has_no_production_views(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_simulation_schema()
        present = _views("simulation")
        assert present.isdisjoint(EXPECTED_PROD_VIEWS)
        assert result.metadata["views_created"] == 0


# --------------------------------------------------------------------------- #
# 12. schema_versions seed row content
# --------------------------------------------------------------------------- #
class TestSchemaVersionsSeed:
    """schema_versions is seeded with the exact (schema_name, version) key."""

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_exact_seed_key(self, role: str, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_schema(role)
        rows = _schema_version_rows(role)
        assert len(rows) == 1
        schema_name, version, applied_at, notes = rows[0]
        assert (schema_name, version) == (role, "schema_v01")
        # applied_at non-NULL; do not assert an exact timestamp.
        assert applied_at is not None
        # notes non-empty; do not overfit exact wording.
        assert notes is not None and str(notes).strip() != ""

    def test_schema_version_metadata_value(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_prod_schema()
        assert result.metadata["schema_version"] == "schema_v01"


# --------------------------------------------------------------------------- #
# 13. daily_features.feature_schema_version column
# --------------------------------------------------------------------------- #
class TestFeatureSchemaVersionColumn:
    """daily_features carries the feature_schema_version column (PATCH 03)."""

    @pytest.mark.parametrize("role", ["prod", "debug"])
    def test_column_exists_and_is_varchar(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_schema(role)
        conn = dbm.connect(role, read_only=True)
        try:
            rows = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = 'daily_features'"
            ).fetchall()
        finally:
            conn.close()
        columns = {name: dtype for name, dtype in rows}
        assert "feature_schema_version" in columns
        # The constant value is a VARCHAR-compatible string.
        assert isinstance(constants.FEATURE_SCHEMA_VERSION, str)
        assert columns["feature_schema_version"].upper() in {"VARCHAR", "TEXT"}

    def test_feature_value_fits_column(self, tmp_db_paths: dict[str, Path]) -> None:
        # A value equal to constants.FEATURE_SCHEMA_VERSION can be written into
        # the column (Module 03 only creates the column; this just confirms
        # type compatibility, not feature population).
        sm.apply_prod_schema()
        conn = dbm.connect("prod", read_only=False)
        try:
            conn.execute(
                "INSERT INTO daily_features "
                "(ticker, feature_date, feature_cutoff_date, "
                " feature_schema_version, calculated_at) "
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
# 14. Static source scan
# --------------------------------------------------------------------------- #
class TestStaticSourceScan:
    """The schema_manager *executable* source obeys forbidden-pattern rules.

    The scan separates SQL string literals (where DDL lives) from descriptive
    prose in docstrings/comments, so a docstring that *mentions* "no ALTER
    TABLE" cannot trigger a false positive. SQL-level patterns are checked
    against the string literals; ``duckdb.connect(`` is checked against the
    full raw source for maximum strictness.
    """

    def _raw_source(self) -> str:
        return Path(sm.__file__).read_text(encoding="utf-8")

    def _code_source(self) -> str:
        """Return source with comments and string literals removed."""
        import io
        import token
        import tokenize

        raw = self._raw_source()
        out_parts: list[str] = []
        readline = io.StringIO(raw).readline
        for tok in tokenize.generate_tokens(readline):
            if tok.type in (token.COMMENT, token.STRING):
                continue
            out_parts.append(tok.string)
        return " ".join(out_parts)

    def _sql_literals(self) -> str:
        """Return the concatenation of the module's actual DDL constants.

        These are exactly the SQL strings the module executes, so scanning them
        is the ground truth for SQL-level forbidden patterns — and it avoids
        false positives from descriptive prose in the module docstring.
        """
        chunks: list[str] = [sm._SCHEMA_VERSIONS_DDL]
        chunks.extend(sm._PROD_TABLE_DDL)
        chunks.extend(sm._PROD_INDEX_DDL)
        chunks.extend(sm._PROD_VIEW_DDL)
        chunks.extend(sm._SIM_TABLE_DDL)
        chunks.extend(sm._SIM_INDEX_DDL)
        return "\n".join(chunks)

    def test_no_alter_table_in_sql(self) -> None:
        # No ALTER TABLE in any SQL literal (the only place DDL lives).
        assert "ALTER TABLE" not in self._sql_literals().upper()

    def test_no_direct_duckdb_connect(self) -> None:
        # No direct duckdb.connect( call anywhere; connections go through the
        # manager. Checked against the full raw source for maximum strictness.
        assert "duckdb.connect(" not in self._raw_source()

    def test_no_create_type_in_sql(self) -> None:
        assert "CREATE TYPE" not in self._sql_literals().upper()

    def test_no_enum_in_sql(self) -> None:
        # No DuckDB ENUM type usage in any SQL literal.
        assert "ENUM" not in self._sql_literals().upper()

    def test_no_migration_framework_usage(self) -> None:
        code = self._code_source()
        # No migration package references in executable code.
        assert "app.database.migrations" not in code
        # No migration framework imports.
        assert "import alembic" not in code
        assert "from alembic" not in code


# --------------------------------------------------------------------------- #
# Column-level schema drift guard (full column-by-column verification)
# --------------------------------------------------------------------------- #
class TestColumnLevelSchema:
    """Every created table has exactly the columns specified in SCHEMA_SPEC.md.

    This is the strongest guard against silent schema drift: it compares the
    actual ``information_schema.columns`` for each table against the
    spec-derived ``EXPECTED_COLUMNS`` mapping. Production tables are checked on
    the ``prod`` DB and sim_* tables on the ``simulation`` DB; ``schema_versions``
    is checked in both (its definition is shared).
    """

    def test_expected_columns_covers_every_table(self) -> None:
        # Guard the guard: the mapping must cover exactly the union of the prod
        # and sim table sets, so no table escapes column-level verification.
        assert set(EXPECTED_COLUMNS) == (
            set(EXPECTED_PROD_TABLES) | set(EXPECTED_SIM_TABLES)
        )

    @pytest.mark.parametrize("table_name", sorted(EXPECTED_PROD_TABLES))
    def test_production_table_columns(
        self, table_name: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_prod_schema()
        present = _table_columns("prod", table_name)
        expected = EXPECTED_COLUMNS[table_name]
        # Ordered comparison: verifies both membership AND column order.
        assert present == expected, (
            f"{table_name} column drift: "
            f"missing={set(expected) - set(present)}, "
            f"extra={set(present) - set(expected)}, "
            f"order_ok={set(present) == set(expected)}"
        )

    @pytest.mark.parametrize("table_name", sorted(EXPECTED_SIM_TABLES))
    def test_simulation_table_columns(
        self, table_name: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        sm.apply_simulation_schema()
        present = _table_columns("simulation", table_name)
        expected = EXPECTED_COLUMNS[table_name]
        # Ordered comparison: verifies both membership AND column order.
        assert present == expected, (
            f"{table_name} column drift: "
            f"missing={set(expected) - set(present)}, "
            f"extra={set(present) - set(expected)}, "
            f"order_ok={set(present) == set(expected)}"
        )

    def test_debug_matches_prod_columns(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        # debug shares the production schema; spot-check a merged-column table
        # with an ordered comparison.
        sm.apply_debug_schema()
        assert (
            _table_columns("debug", "step5_proposals")
            == EXPECTED_COLUMNS["step5_proposals"]
        )


# --------------------------------------------------------------------------- #
# 15-16. ServiceResult + metadata contract
# --------------------------------------------------------------------------- #
class TestServiceResultContract:
    """Public entry points return a valid ServiceResult with the right keys."""

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_valid_service_result(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_schema(role)
        assert isinstance(result, ServiceResult)
        assert result.has_valid_status()
        assert result.run_id  # non-empty run id
        assert isinstance(result.rows_processed, int)

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_metadata_keys_present(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        result = sm.apply_schema(role)
        assert REQUIRED_METADATA_KEYS <= set(result.metadata.keys())
        assert result.metadata["db_role"] == role
        assert isinstance(result.metadata["tables_created"], list)
        assert isinstance(result.metadata["indexes_created"], int)
        assert isinstance(result.metadata["views_created"], int)
        assert result.metadata["schema_version"] == "schema_v01"
        assert isinstance(result.metadata["seed_row_inserted"], bool)

    def test_tables_created_sorted(self, tmp_db_paths: dict[str, Path]) -> None:
        result = sm.apply_prod_schema()
        tables = result.metadata["tables_created"]
        assert tables == sorted(tables)


# --------------------------------------------------------------------------- #
# 18. Type / import smoke
# --------------------------------------------------------------------------- #
class TestImportSmoke:
    """Import and basic symbol presence smoke checks."""

    def test_public_symbols_present(self) -> None:
        for name in (
            "apply_schema",
            "apply_prod_schema",
            "apply_debug_schema",
            "apply_simulation_schema",
            "DATABASE_SCHEMA_VERSION",
        ):
            assert hasattr(sm, name)

    def test_module_has_docstring(self) -> None:
        assert sm.__doc__ is not None and sm.__doc__.strip()

    def test_database_schema_version_value(self) -> None:
        assert sm.DATABASE_SCHEMA_VERSION == "schema_v01"
