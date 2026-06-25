# 01b_SCHEMA_AND_DATA

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.
Rewritten 2026-06-19 for the setup-mode migration (AD-22.19–22.24).
Corrected 2026-06-19 per architect review (fixes 1–11).

Primary selection key is `setup_config_id` (replaces `strategy_config_id`).
`setup_type` carries one of: breakout | pullback | trend_continuation |
consolidation_base. Setup/risk/structural-level columns are added to
step4/step5/outcomes. Old strategy data is not preserved; clean reset applies.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

## FILE: `SCHEMA/10_Schema_prod_duckdb.sql`

```sql
-- SwingTradingSystem Production DuckDB Schema — setup mode (AD-22.21)
-- Architect review fixes: routing_status/routing_fail_reason on step3 (fix 2);
-- target_is_structural on step4/step5 (fix 10); market_regime NULL-safe (fix 9);
-- config activation constraint (fix 11); sim parity fields (fix 8).

CREATE TABLE IF NOT EXISTS schema_versions (
    schema_name VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    applied_at TIMESTAMP NOT NULL,
    notes TEXT,
    PRIMARY KEY (schema_name, version)
);

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

CREATE TABLE IF NOT EXISTS pipeline_locks (
    lock_name VARCHAR PRIMARY KEY,
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    run_id VARCHAR,
    locked_at TIMESTAMP,
    heartbeat_at TIMESTAMP
);

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

CREATE TABLE IF NOT EXISTS sector_etf_map (
    sector VARCHAR PRIMARY KEY,
    etf_ticker VARCHAR NOT NULL,
    active_flag BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL
);

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
    sector_relative_strength DOUBLE,
    market_regime VARCHAR,
    days_to_earnings_bd INTEGER,
    earnings_confidence VARCHAR,
    macro_event_risk_flag BOOLEAN,
    calculated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, feature_date, feature_schema_version)
);

-- Active config tables (setup mode). strategy_configs RETIRED.
-- Activation constraint (fix 11): exactly one active row per setup_type in prod/debug;
-- exactly one active risk_label_config row in prod/debug. Enforced by service layer.
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

CREATE TABLE IF NOT EXISTS risk_label_config (
    config_id VARCHAR PRIMARY KEY,
    version VARCHAR NOT NULL,
    config_json JSON NOT NULL,
    config_hash VARCHAR NOT NULL,
    active_flag BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL,
    notes TEXT
);

-- step3_candidates: universal eligibility + setup routing (fix 1/2/3).
-- Step 3 runs ONCE per signal date (fix 5).
-- Inputs: daily_features_current JOIN daily_prices ON signal_date, ticker_master.
-- daily_prices is required for: close_raw (eligibility price gate),
--   data_quality_status (OHLCV sanity), open/high/low/close anomaly checks.
-- routing_status: 'routed' | 'no_route' | 'ineligible'
-- routing_fail_reason: e.g. 'no_route', 'feature_not_ready', 'not_stock',
--   'price_below_min', 'liquidity_below_min', 'data_quality_fail', 'ohlcv_anomaly'
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

-- step4_analysis: per (ticker, setup_type) validation row (fix 1).
-- Step 4 iterates active setup_config_ids matching routed_setup_types (fix 5).
-- target_is_structural: TRUE = structural target used; FALSE = fixed-R fallback (fix 10).
-- market_regime: NULL if not available; NEVER defaulted to neutral (fix 9).
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

CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker VARCHAR NOT NULL,
    earnings_date DATE NOT NULL,
    session VARCHAR,
    source VARCHAR NOT NULL,
    confidence VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, earnings_date)
);

CREATE TABLE IF NOT EXISTS macro_events_calendar (
    event_date DATE NOT NULL,
    event_type VARCHAR NOT NULL,
    importance VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (event_date, event_type)
);

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

CREATE TABLE IF NOT EXISTS ai_reviews (
    ai_review_id VARCHAR PRIMARY KEY,
    review_type VARCHAR NOT NULL,
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

CREATE TABLE IF NOT EXISTS execution_decisions (
    decision_id VARCHAR PRIMARY KEY,
    proposal_id VARCHAR NOT NULL,
    ai_review_id VARCHAR,
    decision_source VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    decision_notes TEXT,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_ticker_date ON daily_prices(ticker, date);
CREATE INDEX IF NOT EXISTS idx_daily_features_ticker_date ON daily_features(ticker, feature_date);
CREATE INDEX IF NOT EXISTS idx_step3_run_date ON step3_candidates(run_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_step4_run_setup ON step4_analysis(run_id, signal_date, setup_type);
CREATE INDEX IF NOT EXISTS idx_step5_run_date_selected ON step5_proposals(run_id, signal_date, selected_flag);
CREATE INDEX IF NOT EXISTS idx_step5_run_setup ON step5_proposals(run_id, signal_date, setup_type);
CREATE INDEX IF NOT EXISTS idx_step5_run_raw_rank ON step5_proposals(run_id, signal_date, raw_rank);
CREATE INDEX IF NOT EXISTS idx_step5_run_div_rank ON step5_proposals(run_id, signal_date, diversified_rank);
CREATE INDEX IF NOT EXISTS idx_outcomes_setup_date ON signal_outcomes(setup_config_id, setup_type, signal_date);
CREATE INDEX IF NOT EXISTS idx_queue_status_eval ON outcome_tracking_queue(status, eval_date);
CREATE INDEX IF NOT EXISTS idx_repair_status ON data_repair_queue(status, repair_date);
```

---

## FILE: `SCHEMA/11_Schema_simulation_duckdb.sql`

```sql
-- Simulation DuckDB Schema — setup mode
-- fix 8: sim_step5_proposals and sim_signal_outcomes include prod-equivalent
-- fields for audit/debug parity.

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

-- sim_step5_proposals: includes prod-equivalent fields for audit/debug (fix 8).
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

-- sim_signal_outcomes: includes stop/target prices for hit-rate audit (fix 8).
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

CREATE INDEX IF NOT EXISTS idx_sim_props_run_date ON sim_step5_proposals(sim_run_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_sim_outcomes_setup_date ON sim_signal_outcomes(setup_config_id, setup_type, signal_date);
```

---

## FILE: `SCHEMA/13_Views_Reference.md`

# Views Reference

## daily_features_current

```sql
CREATE OR REPLACE VIEW daily_features_current AS
SELECT *
FROM daily_features
WHERE feature_schema_version = (
    SELECT MAX(feature_schema_version) FROM daily_features
);
```

All application queries must use `daily_features_current` unless explicitly
testing historical schema versions.

## selected_proposals_current

```sql
CREATE OR REPLACE VIEW selected_proposals_current AS
SELECT *
FROM step5_proposals
WHERE in_diversified_top_n = TRUE;
```

---

## FILE: `SCHEMA/14_Index_And_Constraint_Reference.md`

# Index and Constraint Reference

## Config activation constraints (fix 11)

Enforced at service layer (not DB constraint):
- `setup_configs`: exactly one `active_flag = TRUE` row per `setup_type` in
  `prod` and `debug`. Before activating a config for a given `setup_type`,
  deactivate all existing active configs for that type in the same transaction.
- `risk_label_config`: exactly one `active_flag = TRUE` row in `prod` and
  `debug`. Same deactivate-then-activate pattern.
- Simulation may hold multiple active configs of the same `setup_type` for
  comparison runs; the one-active rule applies to `prod` and `debug` only.

## Constraint philosophy

Hard constraints: primary keys, NOT NULL on IDs/dates/statuses.
`setup_type` NOT NULL on `step4_analysis` and `step5_proposals`.
`routing_status` NOT NULL on `step3_candidates`.
All enum values validated by service layer before insert.

## Uniqueness
- One daily price per ticker/date.
- One feature row per ticker/feature_date/feature_schema_version.
- One queue task per proposal/horizon_bd.

## Versioning rule
Feature schema versions must be zero-padded: `features_v01`, `features_v02`, ..., `features_v10`.

---

## FILE: `PIPELINE/71_Module_Interface_Contracts.md`

# Module Interface Contracts

```python
@dataclass
class ServiceResult:
    status: str        # success | success_with_warnings | failed
    run_id: str
    rows_processed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

All modules accept: db_manager, run_id, config (setup_config and/or
risk_label_config as applicable), date range or signal date.
