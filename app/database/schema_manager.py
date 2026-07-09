"""Schema Manager (Module 03) — setup-mode migration (AD-22.19–22.24).

Creates the final merged DuckDB schema for setup mode directly. No base-schema
+ ALTER patching on a fresh DB. Old strategy_configs table is replaced by
setup_configs + risk_label_config. All setup-mode columns are created inline.

Scope:
- Production schema for prod and debug roles (setup-mode tables, indexes, views).
- Simulation schema (sim_* tables + shared config tables).
- Seeds one schema_versions row per database.

Does NOT:
- Open DuckDB connections directly (all access via duckdb_manager).
- Create DuckDB ENUM types (service-layer validation).
- Run migrations, ALTER TABLE, or any trading/pipeline logic.

Two distinct versions (never confused):
- DATABASE_SCHEMA_VERSION = "schema_v02" — seeded into schema_versions.
- FEATURE_SCHEMA_VERSION = "features_v03" — written into daily_features rows by M11
  (P1.1, 2026-07-08: adds rs_percentile_126d; bumped from features_v02).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

if TYPE_CHECKING:  # pragma: no cover
    from duckdb import DuckDBPyConnection

_LOG = logging_config.get_logger(__name__)

DATABASE_SCHEMA_VERSION: Final[str] = "schema_v02"
_SEED_NOTES: Final[str] = "setup-mode schema (AD-22.19-22.24)"

DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
DB_ROLE_SIMULATION: Final[str] = duckdb_manager.DB_ROLE_SIMULATION


# --------------------------------------------------------------------------- #
# Shared
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
# Production tables (01b_SCHEMA_AND_DATA.md — setup-mode final form)
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
    # features_v02 adds structural-level columns (AD-22.19; 01b schema)
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
        ema20_slope DOUBLE,
        ema50_slope DOUBLE,
        distance_to_ema20_pct DOUBLE,
        distance_to_ema50_pct DOUBLE,
        distance_to_ema200_pct DOUBLE,
        rsi14 DOUBLE,
        roc20 DOUBLE,
        atr14 DOUBLE,
        atr_pct DOUBLE,
        atr_compression_score DOUBLE,
        rvol20 DOUBLE,
        avg_volume_20d DOUBLE,
        avg_dollar_volume_20d DOUBLE,
        distance_from_52w_high_pct DOUBLE,
        pullback_from_recent_high_pct DOUBLE,
        pullback_depth_pct DOUBLE,
        breakout_proximity DOUBLE,
        consolidation_score DOUBLE,
        swing_high DOUBLE,
        swing_low DOUBLE,
        support_level DOUBLE,
        resistance_level DOUBLE,
        next_resistance_level DOUBLE,
        base_high DOUBLE,
        base_low DOUBLE,
        range_width_pct DOUBLE,
        range_duration INTEGER,
        range_tightness_score DOUBLE,
        volume_dry_up_score DOUBLE,
        volume_expansion_score DOUBLE,
        relative_strength_vs_spy DOUBLE,
        rs_percentile_126d DOUBLE,
        sector_relative_strength DOUBLE,
        market_regime VARCHAR,
        market_breadth_pct DOUBLE,
        days_to_earnings_bd INTEGER,
        earnings_confidence VARCHAR,
        macro_event_risk_flag BOOLEAN,
        calculated_at TIMESTAMP NOT NULL,
        PRIMARY KEY (ticker, feature_date, feature_schema_version)
    );
    """,
    # setup_configs replaces strategy_configs (AD-22.22)
    # Activation constraint: exactly one active_flag=TRUE per setup_type in prod/debug.
    # Enforced by service layer (not DB constraint).
    """
    CREATE TABLE IF NOT EXISTS setup_configs (
        config_id VARCHAR PRIMARY KEY,
        setup_type VARCHAR NOT NULL,
        version VARCHAR NOT NULL,
        parent_config_id VARCHAR,
        config_json JSON NOT NULL,
        config_hash VARCHAR NOT NULL,
        active_flag BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL,
        notes TEXT
    );
    """,
    # risk_label_config: one active row per prod/debug (AD-22.22)
    """
    CREATE TABLE IF NOT EXISTS risk_label_config (
        config_id VARCHAR PRIMARY KEY,
        version VARCHAR NOT NULL,
        config_json JSON NOT NULL,
        config_hash VARCHAR NOT NULL,
        active_flag BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL,
        notes TEXT
    );
    """,
    # sector_alias_map: raw->canonical sector normalization (retained from legacy)
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
    # step3_candidates: universal eligibility + setup routing (AD-22.21)
    # routing_status NOT NULL: 'routed' | 'no_route' | 'ineligible'
    """
    CREATE TABLE IF NOT EXISTS step3_candidates (
        candidate_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        eligibility_score DOUBLE,
        passed_eligibility BOOLEAN NOT NULL,
        routing_status VARCHAR NOT NULL,
        routing_fail_reason VARCHAR,
        eligibility_fail_reasons JSON,
        routed_setup_types JSON,
        feature_snapshot_json JSON,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # step4_analysis: per (ticker, setup_type) — setup_type NOT NULL (AD-22.21)
    # target_is_structural: TRUE=structural target, FALSE=fixed-R fallback
    # market_regime: NULL if unavailable — NEVER defaulted to neutral
    """
    CREATE TABLE IF NOT EXISTS step4_analysis (
        analysis_id VARCHAR PRIMARY KEY,
        candidate_id VARCHAR NOT NULL,
        run_id VARCHAR NOT NULL,
        setup_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        setup_type VARCHAR NOT NULL,
        setup_score DOUBLE,
        setup_passed BOOLEAN NOT NULL,
        setup_reasons JSON,
        setup_fail_reason VARCHAR,
        entry_price_raw DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        estimated_rr DOUBLE,
        target_is_structural BOOLEAN,
        stop_distance_pct DOUBLE,
        support_level DOUBLE,
        resistance_level DOUBLE,
        next_resistance_level DOUBLE,
        atr_pct DOUBLE,
        distance_to_ema20_pct DOUBLE,
        distance_to_ema50_pct DOUBLE,
        rvol DOUBLE,
        earnings_days INTEGER,
        market_regime VARCHAR,
        earnings_penalty DOUBLE,
        macro_penalty DOUBLE,
        explanation_json JSON,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # step5_proposals: setup/risk/disposition columns (AD-22.21)
    """
    CREATE TABLE IF NOT EXISTS step5_proposals (
        proposal_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        setup_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        setup_type VARCHAR NOT NULL,
        setup_score DOUBLE,
        risk_score DOUBLE,
        risk_label VARCHAR,
        risk_reasons JSON,
        disposition VARCHAR NOT NULL,
        entry_price_raw DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        estimated_rr DOUBLE,
        target_is_structural BOOLEAN,
        support_level DOUBLE,
        resistance_level DOUBLE,
        next_resistance_level DOUBLE,
        earnings_days INTEGER,
        market_regime VARCHAR,
        proposal_score_raw DOUBLE,
        diversity_penalty DOUBLE,
        proposal_score_final DOUBLE,
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
        setup_reasons JSON,
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
    # signal_outcomes: setup_type + risk_label + stop/target hit (AD-22.21)
    """
    CREATE TABLE IF NOT EXISTS signal_outcomes (
        outcome_id VARCHAR PRIMARY KEY,
        proposal_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        setup_config_id VARCHAR NOT NULL,
        setup_type VARCHAR NOT NULL,
        risk_label VARCHAR,
        signal_date DATE NOT NULL,
        entry_date DATE NOT NULL,
        entry_price_raw DOUBLE,
        entry_price_sim DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        return_5bd_pct DOUBLE,
        return_10bd_pct DOUBLE,
        return_20bd_pct DOUBLE,
        return_40bd_pct DOUBLE,
        mfe_40bd_pct DOUBLE,
        mae_40bd_pct DOUBLE,
        stop_hit BOOLEAN,
        target_hit BOOLEAN,
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
    # Module 04/Phase 4 delta — fundamentals/events companion table. Kept
    # separate from daily_features (companion table, not new columns there)
    # since fundamentals update quarterly/irregularly vs daily_features'
    # daily cadence; FEATURE_SCHEMA_VERSION is unaffected by this table.
    # as_of_date is the point-in-time anchor (Phase 0 no-look-ahead
    # discipline) -- the value known/computable as of that date, never a
    # later restatement. Prod/debug only (mirrors earnings_calendar's scope;
    # not present in the simulation schema).
    """
    CREATE TABLE IF NOT EXISTS ticker_fundamentals (
        ticker VARCHAR NOT NULL,
        as_of_date DATE NOT NULL,
        eps_growth_trend DOUBLE,
        leverage_ratio DOUBLE,
        valuation_band VARCHAR,
        piotroski_f_score INTEGER,
        altman_z_score DOUBLE,
        insider_trade_flag BOOLEAN,
        institutional_ownership_delta DOUBLE,
        source_provider VARCHAR NOT NULL,
        calculated_at TIMESTAMP NOT NULL,
        PRIMARY KEY (ticker, as_of_date)
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
        review_kind VARCHAR,
        proposal_id VARCHAR,
        sim_run_id VARCHAR,
        setup_type VARCHAR,
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
    # Setup-mode funnel diagnostics (Phase 6 — M20/M22).
    # Stores per-step, per-setup_type metric rows for every pipeline run.
    # setup_type is nullable (NULL = pipeline-level metrics, not per-setup).
    # reason is nullable (populated for rejection/failure breakdowns).
    # metadata_json holds ancillary counts or detail JSON for the metric.
    """
    CREATE TABLE IF NOT EXISTS pipeline_run_diagnostics (
        diag_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        db_role VARCHAR NOT NULL,
        step_name VARCHAR NOT NULL,
        setup_type VARCHAR,
        metric_name VARCHAR NOT NULL,
        metric_value DOUBLE,
        reason VARCHAR,
        metadata_json JSON,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # Module 23 — Config Recommender (learning layer). Proposals are always
    # human-gated: activation happens exclusively via a human calling
    # ConfigService.activate_setup_config() directly; this table only ever
    # records a proposal + its evidence, never an activation.
    """
    CREATE TABLE IF NOT EXISTS config_recommendations (
        recommendation_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        setup_type VARCHAR NOT NULL,
        regime VARCHAR,
        incumbent_config_id VARCHAR NOT NULL,
        candidate_config_id VARCHAR NOT NULL,
        proposal_json JSON NOT NULL,
        evidence_json JSON NOT NULL,
        status VARCHAR NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP NOT NULL
    );
    """,
)

_PROD_INDEX_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_daily_prices_ticker_date ON daily_prices(ticker, date);",
    "CREATE INDEX IF NOT EXISTS idx_daily_features_ticker_date ON daily_features(ticker, feature_date);",
    "CREATE INDEX IF NOT EXISTS idx_step3_run_date ON step3_candidates(run_id, signal_date);",
    "CREATE INDEX IF NOT EXISTS idx_step4_run_setup ON step4_analysis(run_id, signal_date, setup_type);",
    "CREATE INDEX IF NOT EXISTS idx_step5_run_date_selected ON step5_proposals(run_id, signal_date, selected_flag);",
    "CREATE INDEX IF NOT EXISTS idx_step5_run_setup ON step5_proposals(run_id, signal_date, setup_type);",
    "CREATE INDEX IF NOT EXISTS idx_step5_run_raw_rank ON step5_proposals(run_id, signal_date, raw_rank);",
    "CREATE INDEX IF NOT EXISTS idx_step5_run_div_rank ON step5_proposals(run_id, signal_date, diversified_rank);",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_setup_date ON signal_outcomes(setup_config_id, setup_type, signal_date);",
    "CREATE INDEX IF NOT EXISTS idx_queue_status_eval ON outcome_tracking_queue(status, eval_date);",
    "CREATE INDEX IF NOT EXISTS idx_repair_status ON data_repair_queue(status, repair_date);",
    "CREATE INDEX IF NOT EXISTS idx_diag_run_date ON pipeline_run_diagnostics(run_id, signal_date);",
    "CREATE INDEX IF NOT EXISTS idx_diag_run_step ON pipeline_run_diagnostics(run_id, step_name, setup_type);",
    "CREATE INDEX IF NOT EXISTS idx_config_recs_setup_regime_status ON config_recommendations(setup_type, regime, status);",
)

_PROD_INDEX_NAMES: Final[tuple[str, ...]] = (
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
)

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
# Simulation tables (01b §SCHEMA/11)
# --------------------------------------------------------------------------- #
_SIM_CONFIG_TABLE_DDL: tuple[str, ...] = tuple(
    ddl
    for ddl in _PROD_TABLE_DDL
    if any(
        marker in ddl
        for marker in (
            "CREATE TABLE IF NOT EXISTS setup_configs",
            "CREATE TABLE IF NOT EXISTS risk_label_config",
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
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        eligibility_score DOUBLE,
        passed_eligibility BOOLEAN NOT NULL,
        routing_status VARCHAR NOT NULL,
        routing_fail_reason VARCHAR,
        eligibility_fail_reasons JSON,
        routed_setup_types JSON,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_step4_analysis (
        analysis_id VARCHAR PRIMARY KEY,
        candidate_id VARCHAR NOT NULL,
        sim_run_id VARCHAR NOT NULL,
        fold_id VARCHAR,
        setup_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        setup_type VARCHAR NOT NULL,
        setup_score DOUBLE,
        setup_passed BOOLEAN NOT NULL,
        estimated_rr DOUBLE,
        target_is_structural BOOLEAN,
        entry_price_raw DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # sim_step5_proposals: prod-equivalent fields for audit/debug parity (fix 8)
    """
    CREATE TABLE IF NOT EXISTS sim_step5_proposals (
        proposal_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        fold_id VARCHAR,
        setup_config_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        signal_date DATE NOT NULL,
        setup_type VARCHAR NOT NULL,
        setup_score DOUBLE,
        risk_score DOUBLE,
        risk_label VARCHAR,
        risk_reasons JSON,
        disposition VARCHAR NOT NULL,
        entry_price_raw DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        estimated_rr DOUBLE,
        target_is_structural BOOLEAN,
        support_level DOUBLE,
        resistance_level DOUBLE,
        next_resistance_level DOUBLE,
        market_regime VARCHAR,
        earnings_days INTEGER,
        proposal_score_raw DOUBLE,
        diversity_penalty DOUBLE,
        proposal_score_final DOUBLE,
        raw_rank INTEGER,
        diversified_rank INTEGER,
        in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
        in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
        diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
        selected_top_n BOOLEAN NOT NULL,
        selected_flag BOOLEAN NOT NULL DEFAULT FALSE,
        rejection_reason VARCHAR,
        created_at TIMESTAMP NOT NULL
    );
    """,
    # sim_signal_outcomes: stop/target prices for hit-rate audit
    """
    CREATE TABLE IF NOT EXISTS sim_signal_outcomes (
        outcome_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        fold_id VARCHAR,
        proposal_id VARCHAR NOT NULL,
        ticker VARCHAR NOT NULL,
        setup_config_id VARCHAR NOT NULL,
        setup_type VARCHAR NOT NULL,
        risk_label VARCHAR,
        signal_date DATE NOT NULL,
        entry_date DATE NOT NULL,
        entry_price_raw DOUBLE,
        entry_price_sim DOUBLE,
        stop_price_raw DOUBLE,
        target_price_raw DOUBLE,
        list_membership VARCHAR,
        return_5bd_pct DOUBLE,
        return_10bd_pct DOUBLE,
        return_20bd_pct DOUBLE,
        return_40bd_pct DOUBLE,
        mfe_40bd_pct DOUBLE,
        mae_40bd_pct DOUBLE,
        stop_hit BOOLEAN,
        target_hit BOOLEAN,
        realized_r_multiple DOUBLE,
        cross_fold_outcome BOOLEAN NOT NULL DEFAULT FALSE,
        outcome_status VARCHAR NOT NULL,
        calculated_at TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_config_comparisons (
        comparison_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        config_id VARCHAR NOT NULL,
        setup_type VARCHAR NOT NULL,
        risk_label VARCHAR,
        horizon_bd INTEGER NOT NULL,
        list_type VARCHAR NOT NULL DEFAULT 'diversified',
        expectancy DOUBLE,
        win_rate DOUBLE,
        avg_win DOUBLE,
        avg_loss DOUBLE,
        profit_factor DOUBLE,
        stop_hit_rate DOUBLE,
        target_hit_rate DOUBLE,
        max_drawdown_pct DOUBLE,
        resolved_outcomes_pct DOUBLE,
        created_at TIMESTAMP NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sim_ai_reviews (
        ai_review_id VARCHAR PRIMARY KEY,
        sim_run_id VARCHAR NOT NULL,
        review_kind VARCHAR,
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

_SIM_INDEX_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_sim_props_run_date ON sim_step5_proposals(sim_run_id, signal_date);",
    "CREATE INDEX IF NOT EXISTS idx_sim_outcomes_setup_date ON sim_signal_outcomes(setup_config_id, setup_type, signal_date);",
)

_SIM_INDEX_NAMES: Final[tuple[str, ...]] = (
    "idx_sim_props_run_date",
    "idx_sim_outcomes_setup_date",
)


# --------------------------------------------------------------------------- #
# Role -> schema mapping
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _RoleSchema:
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

_ROLE_SCHEMAS: Final[dict[str, _RoleSchema]] = {
    DB_ROLE_PROD: _PRODUCTION_SCHEMA,
    DB_ROLE_DEBUG: _PRODUCTION_SCHEMA,
    DB_ROLE_SIMULATION: _SIMULATION_SCHEMA,
}


# --------------------------------------------------------------------------- #
# Introspection helpers
# --------------------------------------------------------------------------- #
def _present_base_tables(connection: "DuckDBPyConnection") -> set[str]:
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {row[0] for row in rows}


def _present_views(connection: "DuckDBPyConnection") -> set[str]:
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'VIEW'"
    ).fetchall()
    return {row[0] for row in rows}


def _present_indexes(connection: "DuckDBPyConnection") -> set[str]:
    rows = connection.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    return {row[0] for row in rows}


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #
def _seed_schema_version(connection: "DuckDBPyConnection", schema_name: str) -> bool:
    existing = connection.execute(
        "SELECT COUNT(*) FROM schema_versions WHERE schema_name = ? AND version = ?",
        [schema_name, DATABASE_SCHEMA_VERSION],
    ).fetchone()
    already_present = bool(existing and existing[0])
    connection.execute(
        "INSERT INTO schema_versions (schema_name, version, applied_at, notes) "
        "SELECT ?, ?, CAST(now() AS TIMESTAMP), ? "
        "WHERE NOT EXISTS ("
        "    SELECT 1 FROM schema_versions WHERE schema_name = ? AND version = ?"
        ")",
        [schema_name, DATABASE_SCHEMA_VERSION, _SEED_NOTES, schema_name, DATABASE_SCHEMA_VERSION],
    )
    return not already_present


def _apply_role_schema(
    connection: "DuckDBPyConnection",
    db_role: str,
    schema: _RoleSchema,
) -> dict[str, Any]:
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
    """Create the final setup-mode schema for db_role on its DuckDB database."""
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

    log.info("applying setup-mode schema role=%s version=%s", db_role, DATABASE_SCHEMA_VERSION)
    try:
        connection = duckdb_manager.connect(db_role)
        try:
            metadata = _apply_role_schema(connection, db_role, schema)
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001
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
    """Create the production schema on the prod database."""
    return apply_schema(DB_ROLE_PROD)


def apply_debug_schema() -> ServiceResult:
    """Create the production schema on the debug database."""
    return apply_schema(DB_ROLE_DEBUG)


def apply_simulation_schema() -> ServiceResult:
    """Create the simulation schema on the simulation database."""
    return apply_schema(DB_ROLE_SIMULATION)
