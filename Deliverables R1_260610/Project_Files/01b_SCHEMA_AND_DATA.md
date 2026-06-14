# 01b_SCHEMA_AND_DATA

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

## FILE: `SCHEMA/10_Schema_prod_duckdb.sql`

```sql
-- SwingTradingSystem Production DuckDB Schema v1 FULL

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
    run_type VARCHAR NOT NULL, -- scheduled, manual, force_rerun, catchup, debug
    status VARCHAR NOT NULL, -- pending, running, success, success_with_warnings, failed, cancelled
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
    symbol_type VARCHAR NOT NULL, -- stock, etf, benchmark, index
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
    volume_adj BIGINT, -- reserved in V1; not used in feature formulas
    dividend_amount DOUBLE DEFAULT 0,
    split_ratio DOUBLE DEFAULT 1,
    adjustment_factor DOUBLE,
    source_provider VARCHAR NOT NULL,
    data_quality_status VARCHAR NOT NULL, -- ok, warning, suspect, failed, quarantined
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

CREATE TABLE IF NOT EXISTS strategy_configs (
    config_id VARCHAR PRIMARY KEY,
    strategy_name VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    parent_config_id VARCHAR,
    config_json JSON NOT NULL,
    config_hash VARCHAR NOT NULL,
    active_flag BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL,
    notes TEXT
);

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

CREATE TABLE IF NOT EXISTS step5_proposals (
    proposal_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    strategy_config_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    proposal_score_raw DOUBLE,
    diversity_penalty DOUBLE,
    proposal_score_final DOUBLE,
    raw_rank INTEGER,
    diversified_rank INTEGER,
    in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
    selected_top_n BOOLEAN NOT NULL DEFAULT FALSE, -- legacy: in_raw_top_n OR in_diversified_top_n
    selected_flag BOOLEAN NOT NULL DEFAULT FALSE, -- legacy: in_diversified_top_n
    ai_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    executed_flag BOOLEAN NOT NULL DEFAULT FALSE,
    rejection_reason VARCHAR,
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
    status VARCHAR NOT NULL, -- pending, processing, done, failed, unresolvable
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    last_repair_attempt TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP
);

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
    outcome_status VARCHAR NOT NULL, -- pending, complete, partial, unresolvable, failed
    calculated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker VARCHAR NOT NULL,
    earnings_date DATE NOT NULL,
    session VARCHAR, -- pre_market, post_market, during_market, unknown
    source VARCHAR NOT NULL,
    confidence VARCHAR NOT NULL, -- high, medium, low
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, earnings_date)
);

CREATE TABLE IF NOT EXISTS macro_events_calendar (
    event_date DATE NOT NULL,
    event_type VARCHAR NOT NULL, -- FOMC, CPI, PPI, NFP, POWELL
    importance VARCHAR NOT NULL, -- high, medium, low
    source VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (event_date, event_type)
);

CREATE TABLE IF NOT EXISTS data_repair_queue (
    repair_id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    repair_date DATE,
    repair_reason VARCHAR NOT NULL, -- missing_price, bad_ohlc, mutation, provider_empty, outcome_missing
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_attempt TIMESTAMP,
    status VARCHAR NOT NULL, -- pending, processing, repaired, failed, unresolvable
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
    review_type VARCHAR NOT NULL, -- ticker_review, simulation_review, config_review
    proposal_id VARCHAR,
    sim_run_id VARCHAR,
    provider VARCHAR NOT NULL,
    model VARCHAR NOT NULL,
    prompt_version VARCHAR NOT NULL,
    prompt_text TEXT NOT NULL,
    selected_tickers_json JSON,
    ai_response_text TEXT,
    human_action VARCHAR, -- ignored, accepted, overrode, deferred
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_decisions (
    decision_id VARCHAR PRIMARY KEY,
    proposal_id VARCHAR NOT NULL,
    ai_review_id VARCHAR,
    decision_source VARCHAR NOT NULL, -- mechanical_only, human_only, ai_assisted
    action VARCHAR NOT NULL, -- watch, paper_trade, skip, real_trade
    decision_notes TEXT,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_prices_ticker_date ON daily_prices(ticker, date);
CREATE INDEX IF NOT EXISTS idx_daily_features_ticker_date ON daily_features(ticker, feature_date);
CREATE INDEX IF NOT EXISTS idx_step3_run_date ON step3_candidates(run_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_step5_run_date_selected ON step5_proposals(run_id, signal_date, selected_flag);
CREATE INDEX IF NOT EXISTS idx_step5_run_raw_rank ON step5_proposals(run_id, signal_date, raw_rank);
CREATE INDEX IF NOT EXISTS idx_step5_run_div_rank ON step5_proposals(run_id, signal_date, diversified_rank);
CREATE INDEX IF NOT EXISTS idx_outcomes_config_date ON signal_outcomes(strategy_config_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_queue_status_eval ON outcome_tracking_queue(status, eval_date);
CREATE INDEX IF NOT EXISTS idx_repair_status ON data_repair_queue(status, repair_date);
```

---

## FILE: `SCHEMA/11_Schema_simulation_duckdb.sql`

```sql
-- Simulation DuckDB Schema v1 FULL

CREATE TABLE IF NOT EXISTS sim_runs (
    sim_run_id VARCHAR PRIMARY KEY,
    sim_name VARCHAR,
    mode VARCHAR NOT NULL, -- research, walk_forward, config_comparison
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    created_at TIMESTAMP NOT NULL,
    config_ids JSON NOT NULL,
    status VARCHAR NOT NULL, -- pending, running, success, failed
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
    strategy_config_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    screening_score DOUBLE,
    passed_hard_filters BOOLEAN NOT NULL,
    hard_filter_fail_reasons JSON,
    soft_score_components JSON,
    created_at TIMESTAMP NOT NULL
);

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
    raw_rank INTEGER,
    diversified_rank INTEGER,
    in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
    selected_top_n BOOLEAN NOT NULL, -- legacy: in_raw_top_n OR in_diversified_top_n
    rejection_reason VARCHAR,
    created_at TIMESTAMP NOT NULL
);

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
    list_membership VARCHAR, -- raw_only, diversified_only, both
    return_5bd_pct DOUBLE,
    return_10bd_pct DOUBLE,
    return_20bd_pct DOUBLE,
    return_40bd_pct DOUBLE,
    mfe_40bd_pct DOUBLE,
    mae_40bd_pct DOUBLE,
    realized_r_multiple DOUBLE,
    cross_fold_outcome BOOLEAN NOT NULL DEFAULT FALSE,
    outcome_status VARCHAR NOT NULL,
    calculated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sim_config_comparisons (
    comparison_id VARCHAR PRIMARY KEY,
    sim_run_id VARCHAR NOT NULL,
    config_id VARCHAR NOT NULL,
    horizon_bd INTEGER NOT NULL,
    list_type VARCHAR NOT NULL DEFAULT 'diversified', -- raw, diversified
    expectancy DOUBLE,
    win_rate DOUBLE,
    avg_win DOUBLE,
    avg_loss DOUBLE,
    profit_factor DOUBLE,
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
CREATE INDEX IF NOT EXISTS idx_sim_outcomes_config_date ON sim_signal_outcomes(strategy_config_id, signal_date);
```

---

## FILE: `SCHEMA/13_Views_Reference.md`

# Views Reference

## daily_features_current

Purpose:
Prevent accidental duplicate joins across feature schema versions.

Preferred implementation uses a feature schema registry. V1 simple version:

```sql
CREATE OR REPLACE VIEW daily_features_current AS
SELECT *
FROM daily_features
WHERE feature_schema_version = (
    SELECT MAX(feature_schema_version) FROM daily_features -- safe because versions are zero-padded, e.g. features_v01
);
```

All application queries must use `daily_features_current` unless explicitly testing historical schema versions.

## selected_proposals_current

Recommended dashboard view:

```sql
CREATE OR REPLACE VIEW selected_proposals_current AS
SELECT *
FROM step5_proposals
WHERE in_diversified_top_n = TRUE;
```


Raw list remains accessible with `WHERE in_raw_top_n = TRUE`.

---

## FILE: `SCHEMA/14_Index_And_Constraint_Reference.md`

# Index and Constraint Reference

## Performance indexes

Required:
- daily_prices(ticker, date)
- daily_features(ticker, feature_date)
- step3_candidates(run_id, signal_date)
- step5_proposals(run_id, signal_date, selected_flag)
- signal_outcomes(strategy_config_id, signal_date)
- outcome_tracking_queue(status, eval_date)
- data_repair_queue(status, repair_date)

## Constraint philosophy
DuckDB supports constraints but this system also validates at service layer.

Hard constraints:
- Primary keys for all fact tables
- Not null for IDs, dates, run IDs, statuses
- All enum values validated by Pydantic before insert

## Important uniqueness
- one daily price per ticker/date
- one feature row per ticker/feature_date/feature_schema_version
- one queue task per proposal/horizon_bd


## Additional indexes required by merged patches
- step5_proposals(run_id, signal_date, raw_rank)
- step5_proposals(run_id, signal_date, diversified_rank)

## Versioning rule
Feature schema versions must be zero-padded strings: `features_v01`, `features_v02`, ..., `features_v10`.

---

## FILE: `PIPELINE/71_Module_Interface_Contracts.md`

# Module Interface Contracts

Each service returns:
- status: success / success_with_warnings / failed
- run_id
- rows_processed
- warnings
- errors
- output_table_names

Python recommended return dataclass:

```python
@dataclass
class ServiceResult:
    status: str
    run_id: str
    rows_processed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

All modules accept:
- db_manager
- run_id
- config
- date range or signal date

---
