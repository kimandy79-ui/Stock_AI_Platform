"""Schema Manager (Module 03) for the Swing Trading Stock Analyzer.

This module creates the **final merged DuckDB schema** on fresh databases. The
column-level source of truth is ``docs/SCHEMA_SPEC.md``; every PATCH 1 and
MINI-PATCH 2 column is already inlined into the ``CREATE TABLE`` statements
below. There is no base-schema-then-patch step and there are no
``ALTER TABLE`` statements: the merged schema is created directly.

Scope (per the Module 03 task and ``ARCHITECTURE.md`` section 3):

- Create the production schema for the ``prod`` and ``debug`` roles
  (24 tables incl. ``schema_versions``, 9 indexes, 2 views). The M21 Config
  Management Addendum added ``runtime_configs``, ``config_activation_log`` and
  ``sector_alias_map``, extended ``strategy_configs`` with ``db_role`` /
  ``created_by``, and added config-traceability columns to ``pipeline_runs``.
- Create the narrower simulation schema (``sim_*`` tables + the four shared
  config tables: ``strategy_configs``, ``runtime_configs``,
  ``config_activation_log``, ``sector_alias_map``).
- Create the simulation schema for the ``simulation`` role
  (9 tables incl. ``schema_versions``, 2 indexes, no views).
- Seed exactly one ``schema_versions`` row per database recording the database
  schema version ``schema_v01``.

This module deliberately does NOT:

- open DuckDB connections directly (all access goes through
  :mod:`app.database.duckdb_manager`);
- create DuckDB ``ENUM`` types (enum domains are validated at the service
  layer, see ``SCHEMA_SPEC.md`` section 5);
- run migrations, ``ALTER TABLE``, or any provider / screening / scoring /
  trading / simulation / AI-review / dashboard logic.

Two distinct version values must not be confused (``SCHEMA_SPEC.md`` section 2):

- the **database schema version** ``schema_v01`` seeded into
  ``schema_versions`` by this module; and
- the per-row **feature schema version** ``features_v01`` from
  :data:`app.config.constants.FEATURE_SCHEMA_VERSION`, which is written into
  ``daily_features.feature_schema_version`` by a later module. Module 03 only
  creates that column; it does not populate feature rows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import needed
    from duckdb import DuckDBPyConnection

_LOG = logging_config.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Version / seed constants
# --------------------------------------------------------------------------- #
# Database schema version seeded into ``schema_versions`` on first creation.
# This is the *database* schema version and is intentionally distinct from
# ``constants.FEATURE_SCHEMA_VERSION`` (the per-row feature schema version).
DATABASE_SCHEMA_VERSION: Final[str] = "schema_v01"

# Informational notes column for the seed row. Tests only assert it is
# non-empty (see SCHEMA_SPEC.md section 7.2), so the exact wording is free.
_SEED_NOTES: Final[str] = "initial merged schema"

# Approved roles handled by this module (mirror duckdb_manager roles).
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
DB_ROLE_SIMULATION: Final[str] = duckdb_manager.DB_ROLE_SIMULATION


# --------------------------------------------------------------------------- #
# Shared metadata table (created in every database role)
# --------------------------------------------------------------------------- #
_SCHEMA_VERSIONS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_versions (
    schema_name VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    applied_at TIMESTAMP NOT NULL,
    notes TEXT,
    PRIMARY KEY (schema_name, version)
);
"""


# --------------------------------------------------------------------------- #
# Production tables (SCHEMA_SPEC.md section 3) — merged final form
# --------------------------------------------------------------------------- #
_PROD_TABLE_DDL: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        run_id VARCHAR PRIMARY KEY,
        run_date DATE NOT NULL,
        run_type VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        started_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP,
        duration_sec DOUBLE,
        steps_completed JSON,
        error_message TEXT,
        strategy_config_ids_json JSON,
        runtime_config_ids_json JSON,
        config_snapshot_hash VARCHAR,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS pipeline_locks (
        lock_name VARCHAR PRIMARY KEY,
        is_locked BOOLEAN NOT NULL DEFAULT FALSE,
        run_id VARCHAR,
        locked_at TIMESTAMP,
        heartbeat_at TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ticker_master (
        ticker VARCHAR PRIMARY KEY,
        yahoo_symbol VARCHAR,
        company_name VARCHAR,
        exchange VARCHAR,
        sector VARCHAR,
        industry VARCHAR,
        security_type VARCHAR,
        symbol_type VARCHAR NOT NULL,
        active_flag BOOLEAN NOT NULL DEFAULT TRUE,
        delisted_flag BOOLEAN NOT NULL DEFAULT FALSE,
        first_seen DATE,
        last_seen DATE,
        last_updated TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ticker_universe_snapshot (
        snapshot_month DATE NOT NULL,
        ticker VARCHAR NOT NULL,
        exchange VARCHAR,
        sector VARCHAR,
        industry VARCHAR,
        market_cap_bucket VARCHAR,
        active_flag BOOLEAN NOT NULL,
        source VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY (snapshot_month, ticker)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sector_etf_map (
        sector VARCHAR PRIMARY KEY,
        etf_ticker VARCHAR NOT NULL,
        active_flag BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_prices (
        ticker VARCHAR NOT NULL,
        date DATE NOT NULL,
        open_raw DOUBLE,
        high_raw DOUBLE,
        low_raw DOUBLE,
        close_raw DOUBLE,
        volume_raw BIGINT,
        open_adj DOUBLE,
        high_adj DOUBLE,
        low_adj DOUBLE,
        close_adj DOUBLE,
        volume_adj BIGINT,
        dividend_amount DOUBLE DEFAULT 0,
        split_ratio DOUBLE DEFAULT 1,
        adjustment_factor DOUBLE,
        source_provider VARCHAR NOT NULL,
        data_quality_status VARCHAR NOT NULL,
        mutation_flag BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP,
        PRIMARY KEY (ticker, date)
    );
    """,
    # daily_features: PATCH 03 feature_schema_version is part of the merged
    # primary key. Module 03 creates the column only; it does not populate rows.
    """
    CREATE TABLE IF NOT EXISTS daily_features (
        ticker VARCHAR NOT NULL,
        feature_date DATE NOT NULL,
        feature_cutoff_date DATE NOT NULL,
        feature_schema_version VARCHAR NOT NULL,
        feature_ready BOOLEAN NOT NULL DEFAULT FALSE,
        ema20 DOUBLE,
        ema50 DOUBLE,
        ema200 DOUBLE,
        ema_alignment_score DOUBLE,
        distance_to_ema20_pct DOUBLE,
        distance_to_ema50_pct DOUBLE,
        distance_to_ema200_pct DOUBLE,
        rsi14 DOUBLE,
        roc20 DOUBLE,
        atr14 DOUBLE,
        atr_pct DOUBLE,
        rvol20 DOUBLE,
        avg_volume_20d DOUBLE,
        avg_dollar_volume_20d DOUBLE,
        distance_from_52w_high_pct DOUBLE,
        pullback_from_recent_high_pct DOUBLE,
        breakout_proximity DOUBLE,
        consolidation_score DOUBLE,
        sector_relative_strength DOUBLE,
        market_regime VARCHAR,
        days_to_earnings_bd INTEGER,
        earnings_confidence VARCHAR,
        macro_event_risk_flag BOOLEAN,
        calculated_at TIMESTAMP NOT NULL,
        PRIMARY KEY (ticker, feature_date, feature_schema_version)
    );
    """,
    # strategy_configs: M21 Config Management Addendum §3.1 adds db_role (for
    # db-role-scoped active configs) and created_by (provenance). Result tables
    # continue to reference strategy_config_id (= config_id).
    """
    CREATE TABLE IF NOT EXISTS strategy_configs (
        config_id VARCHAR PRIMARY KEY,
        db_role VARCHAR NOT NULL,
        strategy_name VARCHAR NOT NULL,
        version VARCHAR NOT NULL,
        parent_config_id VARCHAR,
        config_json JSON NOT NULL,
        config_hash VARCHAR NOT NULL,
        active_flag BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL,
        created_by VARCHAR,
        notes TEXT
    );
    """,
    # runtime_configs: M21 Config Management Addendum §3.2 — generic versioned
    # store for non-strategy runtime settings (pipeline/provider/etc.).
    """
    CREATE TABLE IF NOT EXISTS runtime_configs (
        config_id VARCHAR PRIMARY KEY,
        db_role VARCHAR NOT NULL,
        config_type VARCHAR NOT NULL,
        version VARCHAR NOT NULL,
        parent_config_id VARCHAR,
        config_json JSON NOT NULL,
        config_hash VARCHAR NOT NULL,
        active_flag BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL,
        created_by VARCHAR,
        notes TEXT
    );
    """,
    # config_activation_log: M21 Config Management Addendum §3.3 — append-only
    # audit trail of config activations (strategy and runtime).
    """
    CREATE TABLE IF NOT EXISTS config_activation_log (
        activation_id VARCHAR PRIMARY KEY,
        config_id VARCHAR NOT NULL,
        db_role VARCHAR NOT NULL,
        config_type VARCHAR NOT NULL,
        profile_name VARCHAR,
        activated_at TIMESTAMP NOT NULL,
        activated_by VARCHAR,
        reason TEXT
    );
    """,
    # sector_alias_map: M21 Config Management Addendum §3.4 — raw->canonical
    # sector normalization, seeded from constants.SECTOR_ALIAS_MAP.
    """
    CREATE TABLE IF NOT EXISTS sector_alias_map (
        source VARCHAR NOT NULL,
        raw_sector VARCHAR NOT NULL,
        canonical_sector VARCHAR NOT NULL,
        active_flag BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY (source, raw_sector)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS step3_candidates (
        candidate_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        strategy_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        screening_score DOUBLE,
        passed_hard_filters BOOLEAN NOT NULL,
        hard_filter_fail_reasons JSON,
        soft_score_components JSON,
        feature_snapshot_json JSON,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS step4_analysis (
        analysis_id VARCHAR PRIMARY KEY,
        candidate_id VARCHAR NOT NULL,
        run_id VARCHAR NOT NULL,
        strategy_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        setup_type VARCHAR,
        setup_score DOUBLE,
        breakout_quality_score DOUBLE,
        squeeze_score DOUBLE,
        timing_score DOUBLE,
        confirmation_score DOUBLE,
        estimated_rr DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        earnings_penalty DOUBLE,
        macro_penalty DOUBLE,
        explanation_json JSON,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # step5_proposals: PATCH 06 ranking fields merged in. The legacy
    # selected_top_n / selected_flag columns are kept; their semantics are
    # enforced by Module 15 at write time, NOT by DB constraints here.
    """
    CREATE TABLE IF NOT EXISTS step5_proposals (
        proposal_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        strategy_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        proposal_score_raw DOUBLE,
        diversity_penalty DOUBLE,
        proposal_score_final DOUBLE,
        rank_position INTEGER,
        raw_rank INTEGER,
        diversified_rank INTEGER,
        in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
        in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
        diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
        selected_top_n BOOLEAN NOT NULL DEFAULT FALSE,
        selected_flag BOOLEAN NOT NULL DEFAULT FALSE,
        ai_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
        executed_flag BOOLEAN NOT NULL DEFAULT FALSE,
        rejection_reason VARCHAR,
        mechanical_explanation TEXT,
        sector_count_at_selection INTEGER,
        industry_count_at_selection INTEGER,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS outcome_tracking_queue (
        tracking_id VARCHAR PRIMARY KEY,
        proposal_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        entry_date DATE NOT NULL,
        eval_date DATE NOT NULL,
        horizon_bd INTEGER NOT NULL,
        status VARCHAR NOT NULL,
        repair_attempts INTEGER NOT NULL DEFAULT 0,
        last_repair_attempt TIMESTAMP,
        created_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP
    );
    """,
    # signal_outcomes: PATCH 01 entry_price_sim merged in.
    """
    CREATE TABLE IF NOT EXISTS signal_outcomes (
        outcome_id VARCHAR PRIMARY KEY,
        proposal_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        strategy_config_id VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        entry_date DATE NOT NULL,
        entry_price_raw DOUBLE,
        entry_price_sim DOUBLE,
        return_5bd_pct DOUBLE,
        return_10bd_pct DOUBLE,
        return_20bd_pct DOUBLE,
        return_40bd_pct DOUBLE,
        mfe_40bd_pct DOUBLE,
        mae_40bd_pct DOUBLE,
        realized_r_multiple DOUBLE,
        earnings_within_window BOOLEAN DEFAULT FALSE,
        cross_fold_outcome BOOLEAN DEFAULT FALSE,
        outcome_status VARCHAR NOT NULL,
        calculated_at TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS earnings_calendar (
        ticker VARCHAR NOT NULL,
        earnings_date DATE NOT NULL,
        session VARCHAR,
        source VARCHAR NOT NULL,
        confidence VARCHAR NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        PRIMARY KEY (ticker, earnings_date)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_events_calendar (
        event_date DATE NOT NULL,
        event_type VARCHAR NOT NULL,
        importance VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY (event_date, event_type)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS data_repair_queue (
        repair_id VARCHAR PRIMARY KEY,
        ticker VARCHAR NOT NULL,
        repair_date DATE,
        repair_reason VARCHAR NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        last_attempt TIMESTAMP,
        status VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS feature_rebuild_log (
        rebuild_id VARCHAR PRIMARY KEY,
        ticker VARCHAR NOT NULL,
        reason VARCHAR NOT NULL,
        affected_start_date DATE,
        affected_end_date DATE,
        feature_schema_version VARCHAR,
        triggered_at TIMESTAMP NOT NULL,
        status VARCHAR NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_reviews (
        ai_review_id VARCHAR PRIMARY KEY,
        review_type VARCHAR NOT NULL,
        proposal_id VARCHAR,
        sim_run_id VARCHAR,
        provider VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        prompt_version VARCHAR NOT NULL,
        prompt_text TEXT NOT NULL,
        selected_tickers_json JSON,
        ai_response_text TEXT,
        human_action VARCHAR,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_decisions (
        decision_id VARCHAR PRIMARY KEY,
        proposal_id VARCHAR NOT NULL,
        ai_review_id VARCHAR,
        decision_source VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        decision_notes TEXT,
        created_at TIMESTAMP NOT NULL
    );
    """,
)

# Production indexes (SCHEMA_SPEC.md section 3.21): base indexes plus the two
# PATCH 06 ranking indexes.
_PROD_INDEX_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_daily_prices_ticker_date "
    "ON daily_prices(ticker, date);",
    "CREATE INDEX IF NOT EXISTS idx_daily_features_ticker_date "
    "ON daily_features(ticker, feature_date);",
    "CREATE INDEX IF NOT EXISTS idx_step3_run_date "
    "ON step3_candidates(run_id, signal_date);",
    "CREATE INDEX IF NOT EXISTS idx_step5_run_date_selected "
    "ON step5_proposals(run_id, signal_date, selected_flag);",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_config_date "
    "ON signal_outcomes(strategy_config_id, signal_date);",
    "CREATE INDEX IF NOT EXISTS idx_queue_status_eval "
    "ON outcome_tracking_queue(status, eval_date);",
    "CREATE INDEX IF NOT EXISTS idx_repair_status "
    "ON data_repair_queue(status, repair_date);",
    "CREATE INDEX IF NOT EXISTS idx_step5_run_raw_rank "
    "ON step5_proposals(run_id, signal_date, raw_rank);",
    "CREATE INDEX IF NOT EXISTS idx_step5_run_div_rank "
    "ON step5_proposals(run_id, signal_date, diversified_rank);",
)

_PROD_INDEX_NAMES: Final[tuple[str, ...]] = (
    "idx_daily_prices_ticker_date",
    "idx_daily_features_ticker_date",
    "idx_step3_run_date",
    "idx_step5_run_date_selected",
    "idx_outcomes_config_date",
    "idx_queue_status_eval",
    "idx_repair_status",
    "idx_step5_run_raw_rank",
    "idx_step5_run_div_rank",
)

# Production views (SCHEMA_SPEC.md section 3.22). selected_proposals_current
# uses the PATCH 10 condition (in_diversified_top_n = TRUE), NOT the legacy
# selected_flag = TRUE condition. CREATE OR REPLACE VIEW is naturally idempotent.
_PROD_VIEW_DDL: Final[tuple[str, ...]] = (
    """
    CREATE OR REPLACE VIEW daily_features_current AS
    SELECT *
    FROM daily_features
    WHERE feature_schema_version = (
        SELECT MAX(feature_schema_version) FROM daily_features
    );
    """,
    """
    CREATE OR REPLACE VIEW selected_proposals_current AS
    SELECT *
    FROM step5_proposals
    WHERE in_diversified_top_n = TRUE;
    """,
)

_PROD_VIEW_NAMES: Final[tuple[str, ...]] = (
    "daily_features_current",
    "selected_proposals_current",
)


# --------------------------------------------------------------------------- #
# Simulation tables (SCHEMA_SPEC.md section 4) — merged final form
# --------------------------------------------------------------------------- #
# Config tables shared with the production schema (M21 Config Management
# Addendum): strategy_configs, runtime_configs, config_activation_log,
# sector_alias_map. Referenced by sim_* tables via strategy_config_id.
_SIM_CONFIG_TABLE_DDL: Final[tuple[str, ...]] = _PROD_TABLE_DDL[
    # Slice: strategy_configs, runtime_configs, config_activation_log,
    # sector_alias_map are items 10-13 in _PROD_TABLE_DDL (0-indexed).
    # Using a filter by DDL content keeps this robust to future reordering.
    # We include all four config tables and only those.
    :  # all items filtered below
]
# Rather than fragile index slicing, grab the four config DDL strings by name.
_SIM_CONFIG_TABLE_DDL = tuple(
    ddl
    for ddl in _PROD_TABLE_DDL
    if any(
        marker in ddl
        for marker in (
            "CREATE TABLE IF NOT EXISTS strategy_configs",
            "CREATE TABLE IF NOT EXISTS runtime_configs",
            "CREATE TABLE IF NOT EXISTS config_activation_log",
            "CREATE TABLE IF NOT EXISTS sector_alias_map",
        )
    )
)

_SIM_TABLE_DDL: Final[tuple[str, ...]] = _SIM_CONFIG_TABLE_DDL + (
    """
    CREATE TABLE IF NOT EXISTS sim_runs (
        sim_run_id VARCHAR PRIMARY KEY,
        sim_name VARCHAR,
        mode VARCHAR NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        created_at TIMESTAMP NOT NULL,
        config_ids JSON NOT NULL,
        status VARCHAR NOT NULL,
        notes TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_folds (
        fold_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        fold_number INTEGER NOT NULL,
        train_start DATE NOT NULL,
        train_end DATE NOT NULL,
        test_start DATE NOT NULL,
        test_end DATE NOT NULL,
        selected_config_id VARCHAR,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_step3_candidates (
        candidate_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        fold_id VARCHAR,
        strategy_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        screening_score DOUBLE,
        passed_hard_filters BOOLEAN NOT NULL,
        hard_filter_fail_reasons JSON,
        soft_score_components JSON,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_step4_analysis (
        analysis_id VARCHAR PRIMARY KEY,
        candidate_id VARCHAR NOT NULL,
        sim_run_id VARCHAR NOT NULL,
        fold_id VARCHAR,
        strategy_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        setup_type VARCHAR,
        setup_score DOUBLE,
        estimated_rr DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # sim_step5_proposals: PATCH 06 ranking fields merged in.
    """
    CREATE TABLE IF NOT EXISTS sim_step5_proposals (
        proposal_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        fold_id VARCHAR,
        strategy_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        proposal_score_raw DOUBLE,
        diversity_penalty DOUBLE,
        proposal_score_final DOUBLE,
        rank_position INTEGER,
        selected_top_n BOOLEAN NOT NULL,
        raw_rank INTEGER,
        diversified_rank INTEGER,
        in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
        in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
        diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
        rejection_reason VARCHAR,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # sim_signal_outcomes: PATCH 01 entry_price_sim + PATCH 08 list_membership.
    """
    CREATE TABLE IF NOT EXISTS sim_signal_outcomes (
        outcome_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        fold_id VARCHAR,
        proposal_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        strategy_config_id VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        entry_date DATE NOT NULL,
        entry_price_raw DOUBLE,
        entry_price_sim DOUBLE,
        return_5bd_pct DOUBLE,
        return_10bd_pct DOUBLE,
        return_20bd_pct DOUBLE,
        return_40bd_pct DOUBLE,
        mfe_40bd_pct DOUBLE,
        mae_40bd_pct DOUBLE,
        realized_r_multiple DOUBLE,
        list_membership VARCHAR,
        cross_fold_outcome BOOLEAN NOT NULL DEFAULT FALSE,
        outcome_status VARCHAR NOT NULL,
        calculated_at TIMESTAMP
    );
    """,
    # sim_config_comparisons: PATCH 08 list_type (default 'diversified').
    """
    CREATE TABLE IF NOT EXISTS sim_config_comparisons (
        comparison_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        config_id VARCHAR NOT NULL,
        horizon_bd INTEGER NOT NULL,
        expectancy DOUBLE,
        win_rate DOUBLE,
        avg_win DOUBLE,
        avg_loss DOUBLE,
        profit_factor DOUBLE,
        max_drawdown_pct DOUBLE,
        resolved_outcomes_pct DOUBLE,
        list_type VARCHAR NOT NULL DEFAULT 'diversified',
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_ai_reviews (
        ai_review_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        provider VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        prompt_version VARCHAR NOT NULL,
        prompt_text TEXT NOT NULL,
        ai_response_text TEXT,
        human_action VARCHAR,
        created_at TIMESTAMP NOT NULL
    );
    """,
)

# Simulation indexes (SCHEMA_SPEC.md section 4.10).
_SIM_INDEX_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_sim_props_run_date "
    "ON sim_step5_proposals(sim_run_id, signal_date);",
    "CREATE INDEX IF NOT EXISTS idx_sim_outcomes_config_date "
    "ON sim_signal_outcomes(strategy_config_id, signal_date);",
)

_SIM_INDEX_NAMES: Final[tuple[str, ...]] = (
    "idx_sim_props_run_date",
    "idx_sim_outcomes_config_date",
)


# --------------------------------------------------------------------------- #
# Role -> schema mapping
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _RoleSchema:
    """Immutable bundle of the DDL and expected object names for a role.

    ``schema_versions`` is shared across all roles and is prepended to
    ``table_ddl`` so that every database gets its own version table.
    """

    table_ddl: tuple[str, ...]
    index_ddl: tuple[str, ...]
    view_ddl: tuple[str, ...]
    index_names: tuple[str, ...]
    view_names: tuple[str, ...]


_PRODUCTION_SCHEMA: Final[_RoleSchema] = _RoleSchema(
    table_ddl=(_SCHEMA_VERSIONS_DDL,) + _PROD_TABLE_DDL,
    index_ddl=_PROD_INDEX_DDL,
    view_ddl=_PROD_VIEW_DDL,
    index_names=_PROD_INDEX_NAMES,
    view_names=_PROD_VIEW_NAMES,
)

_SIMULATION_SCHEMA: Final[_RoleSchema] = _RoleSchema(
    table_ddl=(_SCHEMA_VERSIONS_DDL,) + _SIM_TABLE_DDL,
    index_ddl=_SIM_INDEX_DDL,
    view_ddl=(),
    index_names=_SIM_INDEX_NAMES,
    view_names=(),
)

# prod and debug share the identical production schema (SCHEMA_SPEC.md section 1).
_ROLE_SCHEMAS: Final[dict[str, _RoleSchema]] = {
    DB_ROLE_PROD: _PRODUCTION_SCHEMA,
    DB_ROLE_DEBUG: _PRODUCTION_SCHEMA,
    DB_ROLE_SIMULATION: _SIMULATION_SCHEMA,
}


# --------------------------------------------------------------------------- #
# Introspection helpers
# --------------------------------------------------------------------------- #
def _present_base_tables(connection: "DuckDBPyConnection") -> set[str]:
    """Return the set of user base-table names in the ``main`` schema."""
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {row[0] for row in rows}


def _present_views(connection: "DuckDBPyConnection") -> set[str]:
    """Return the set of user view names in the ``main`` schema."""
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'VIEW'"
    ).fetchall()
    return {row[0] for row in rows}


def _present_indexes(connection: "DuckDBPyConnection") -> set[str]:
    """Return the set of explicitly-created index names known to DuckDB.

    Primary-key / unique constraints may produce implicit indexes; those are
    ignored by callers, which intersect this set with the explicit index names
    they created, yielding a deterministic count.
    """
    rows = connection.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    return {row[0] for row in rows}


# --------------------------------------------------------------------------- #
# Core application logic
# --------------------------------------------------------------------------- #
def _seed_schema_version(
    connection: "DuckDBPyConnection",
    schema_name: str,
) -> bool:
    """Insert the ``schema_versions`` seed row if it is not already present.

    Uses an explicit ``INSERT ... SELECT ... WHERE NOT EXISTS`` guard keyed on
    the composite ``(schema_name, version)`` so repeated application never
    duplicates the row and never relies on swallowing a primary-key conflict.

    Returns
    -------
    bool
        ``True`` if a new seed row was inserted on this call, ``False`` if the
        row already existed (idempotent re-application).
    """
    existing = connection.execute(
        "SELECT COUNT(*) FROM schema_versions "
        "WHERE schema_name = ? AND version = ?",
        [schema_name, DATABASE_SCHEMA_VERSION],
    ).fetchone()
    already_present = bool(existing and existing[0])

    # CAST(now() AS TIMESTAMP) keeps the value's type aligned with the
    # TIMESTAMP column (now() is TIMESTAMP WITH TIME ZONE in DuckDB).
    connection.execute(
        "INSERT INTO schema_versions (schema_name, version, applied_at, notes) "
        "SELECT ?, ?, CAST(now() AS TIMESTAMP), ? "
        "WHERE NOT EXISTS ("
        "    SELECT 1 FROM schema_versions "
        "    WHERE schema_name = ? AND version = ?"
        ")",
        [
            schema_name,
            DATABASE_SCHEMA_VERSION,
            _SEED_NOTES,
            schema_name,
            DATABASE_SCHEMA_VERSION,
        ],
    )
    return not already_present


def _apply_role_schema(
    connection: "DuckDBPyConnection",
    db_role: str,
    schema: _RoleSchema,
) -> dict[str, Any]:
    """Create all schema objects for ``db_role`` and return metadata.

    Order is tables -> indexes -> views -> seed row, so views can reference
    the tables they depend on. Every statement is intrinsically idempotent.
    """
    for ddl in schema.table_ddl:
        connection.execute(ddl)
    for ddl in schema.index_ddl:
        connection.execute(ddl)
    for ddl in schema.view_ddl:
        connection.execute(ddl)

    seed_row_inserted = _seed_schema_version(connection, db_role)

    tables_present = _present_base_tables(connection)
    indexes_present = _present_indexes(connection)
    views_present = _present_views(connection)

    indexes_created = len(set(schema.index_names) & indexes_present)
    views_created = len(set(schema.view_names) & views_present)

    return {
        "db_role": db_role,
        "tables_created": sorted(tables_present),
        "indexes_created": indexes_created,
        "views_created": views_created,
        "schema_version": DATABASE_SCHEMA_VERSION,
        "seed_row_inserted": seed_row_inserted,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def apply_schema(db_role: str) -> ServiceResult:
    """Create the final merged schema for ``db_role`` on its DuckDB database.

    Parameters
    ----------
    db_role:
        One of ``prod``, ``debug``, ``simulation``. ``prod`` and ``debug``
        receive the identical production schema; ``simulation`` receives the
        narrower simulation schema.

    Returns
    -------
    ServiceResult
        ``success`` on first creation and on idempotent re-application;
        ``failed`` only on a hard error. The ``metadata`` carries the keys
        described in ``SCHEMA_SPEC.md`` section 7.1 and ``rows_processed`` is
        the number of tables present after the call.
    """
    run_id = str(uuid.uuid4())
    log = logging_config.get_logger(__name__, run_id)

    schema = _ROLE_SCHEMAS.get(db_role)
    if schema is None:
        message = (
            f"Unknown database role {db_role!r}. "
            f"Valid roles: {sorted(_ROLE_SCHEMAS)}"
        )
        log.error("schema application failed: %s", message)
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata={"db_role": db_role},
        )

    log.info("applying merged schema role=%s version=%s", db_role, DATABASE_SCHEMA_VERSION)
    try:
        connection = duckdb_manager.connect(db_role)
        try:
            metadata = _apply_role_schema(connection, db_role, schema)
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 - surface as a failed ServiceResult
        log.error("schema application failed for role=%s: %s", db_role, exc)
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[f"{type(exc).__name__}: {exc}"],
            metadata={"db_role": db_role},
        )

    rows_processed = len(metadata["tables_created"])
    log.info(
        "schema applied role=%s tables=%d indexes=%d views=%d seed_inserted=%s",
        db_role,
        rows_processed,
        metadata["indexes_created"],
        metadata["views_created"],
        metadata["seed_row_inserted"],
    )
    return ServiceResult(
        status=service_result.STATUS_SUCCESS,
        run_id=run_id,
        rows_processed=rows_processed,
        metadata=metadata,
    )


def apply_prod_schema() -> ServiceResult:
    """Create the production schema on the ``prod`` database."""
    return apply_schema(DB_ROLE_PROD)


def apply_debug_schema() -> ServiceResult:
    """Create the production schema on the ``debug`` database."""
    return apply_schema(DB_ROLE_DEBUG)


def apply_simulation_schema() -> ServiceResult:
    """Create the simulation schema on the ``simulation`` database."""
    return apply_schema(DB_ROLE_SIMULATION)
