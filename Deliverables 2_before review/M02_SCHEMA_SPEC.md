# SCHEMA_SPEC.md — Final Merged DuckDB Schema

> **Status**: source-of-truth for Module 03 (Schema Manager).
>
> **Derived from**:
> - `SwingTradingSystem_Master_TZ_v1_FULL/SCHEMA/10_Schema_prod_duckdb.sql`
> - `SwingTradingSystem_Master_TZ_v1_FULL/SCHEMA/11_Schema_simulation_duckdb.sql`
> - `SwingTradingSystem_Master_TZ_v1_FULL/SCHEMA/12_Enum_Values_Reference.md`
> - `SwingTradingSystem_Master_TZ_v1_FULL/SCHEMA/13_Views_Reference.md`
> - `SwingTradingSystem_Master_TZ_v1_FULL/SCHEMA/14_Index_And_Constraint_Reference.md`
> - `SwingTradingSystem_Master_TZ_PATCH_1/PATCH_01_Entry_Price_And_Slippage.md`
> - `SwingTradingSystem_Master_TZ_PATCH_1/PATCH_03_Feature_Schema_Versioning.md`
> - `SwingTradingSystem_Master_TZ_PATCH_1/PATCH_06_Raw_vs_Diversified_Ranking.md`
> - `SwingTradingSystem_Master_TZ_PATCH_1/PATCH_08_Simulation_Raw_Diversified_Fields.md`
> - `SwingTradingSystem_Master_TZ_PATCH_1/PATCH_10_Updated_Selected_Proposals_View.md`
> - `SwingTradingSystem_Master_TZ_MINI_PATCH_2/05_Enum_Reference_Update.md`
> - `SwingTradingSystem_Master_TZ_MINI_PATCH_2/06_Schema_Manager_Implementation_Note.md`
>
> **Critical rule** (`MINI_PATCH_2/06_Schema_Manager_Implementation_Note.md`):
> Module 03 must merge every PATCH 1 and MINI-PATCH 2 column into the final
> `CREATE TABLE` definitions. It must NOT create the base schema and then
> apply ALTER TABLE statements on a fresh database.

---

## 1. Scope

This document defines the **final merged schema** that the Schema Manager
(Module 03) must create on fresh DuckDB databases. The schema is split across
two database roles:

- **prod / debug**: receive the *production* schema (20 tables + 9 indexes + 2 views).
- **simulation**: receives the *simulation* schema (8 sim_* tables + the
  shared `schema_versions` metadata table = 9 tables total, plus 2 indexes
  and no views).

Prod and debug share the identical schema. Simulation has its own narrower
schema; it reads production data via Module 02's read-only attach helper, not
by duplicating production tables.

The schema version registered on first creation is `schema_v01`. This is a
**database schema version** and is distinct from the per-row
`feature_schema_version` column (which uses
`constants.FEATURE_SCHEMA_VERSION = "features_v01"` from Module 01).

---

## 2. Schema version constants

| Constant | Value | Where used |
|---|---|---|
| Database schema version (prod / debug) | `schema_v01` | seeded into `schema_versions(schema_name='prod', version='schema_v01', …)` and `schema_versions(schema_name='debug', version='schema_v01', …)` on first creation |
| Database schema version (simulation) | `schema_v01` | seeded into `schema_versions(schema_name='simulation', version='schema_v01', …)` on first creation |
| Feature schema version | `features_v01` | written by Module 11 into `daily_features.feature_schema_version`; comes from `app.config.constants.FEATURE_SCHEMA_VERSION` |

`schema_versions` is created in **both** prod/debug and simulation, so each
database tracks its own applied version independently. This is consistent with
the Master TZ schema file (which defines `schema_versions` in
`10_Schema_prod_duckdb.sql`) and with the design constraint that simulation
must be able to record its own schema state without reaching across into prod.

---

## 3. Production schema (prod.duckdb, debug.duckdb)

20 tables, 9 indexes, 2 views. All table definitions below are the **merged
final** form — PATCH 1 and MINI-PATCH 2 columns are inlined into the
`CREATE TABLE` statements; there are no `ALTER TABLE` follow-ups.

### 3.1 `schema_versions`

```sql
CREATE TABLE IF NOT EXISTS schema_versions (
    schema_name VARCHAR NOT NULL,
    version VARCHAR NOT NULL,
    applied_at TIMESTAMP NOT NULL,
    notes TEXT,
    PRIMARY KEY (schema_name, version)
);
```

Seed row on fresh-DB creation (prod): `('prod', 'schema_v01', now(), 'initial merged schema')`.
Seed row on fresh-DB creation (debug): `('debug', 'schema_v01', now(), 'initial merged schema')`.

### 3.2 `pipeline_runs`

```sql
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id VARCHAR PRIMARY KEY,
    run_date DATE NOT NULL,
    run_type VARCHAR NOT NULL,           -- enum: scheduled, manual, force_rerun, catchup, debug
    status VARCHAR NOT NULL,             -- enum run_status
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    duration_sec DOUBLE,
    steps_completed JSON,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL
);
```

### 3.3 `pipeline_locks`

```sql
CREATE TABLE IF NOT EXISTS pipeline_locks (
    lock_name VARCHAR PRIMARY KEY,
    is_locked BOOLEAN NOT NULL DEFAULT FALSE,
    run_id VARCHAR,
    locked_at TIMESTAMP,
    heartbeat_at TIMESTAMP
);
```

### 3.4 `ticker_master`

```sql
CREATE TABLE IF NOT EXISTS ticker_master (
    ticker VARCHAR PRIMARY KEY,
    yahoo_symbol VARCHAR,
    company_name VARCHAR,
    exchange VARCHAR,
    sector VARCHAR,
    industry VARCHAR,
    security_type VARCHAR,
    symbol_type VARCHAR NOT NULL,        -- enum symbol_type
    active_flag BOOLEAN NOT NULL DEFAULT TRUE,
    delisted_flag BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen DATE,
    last_seen DATE,
    last_updated TIMESTAMP
);
```

### 3.5 `ticker_universe_snapshot`

```sql
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
```

### 3.6 `sector_etf_map`

```sql
CREATE TABLE IF NOT EXISTS sector_etf_map (
    sector VARCHAR PRIMARY KEY,
    etf_ticker VARCHAR NOT NULL,
    active_flag BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL
);
```

### 3.7 `daily_prices`

```sql
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
    data_quality_status VARCHAR NOT NULL,  -- enum data_quality_status
    mutation_flag BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    PRIMARY KEY (ticker, date)
);
```

### 3.8 `daily_features`

```sql
CREATE TABLE IF NOT EXISTS daily_features (
    ticker VARCHAR NOT NULL,
    feature_date DATE NOT NULL,
    feature_cutoff_date DATE NOT NULL,
    feature_schema_version VARCHAR NOT NULL,   -- e.g. 'features_v01' (zero-padded; PATCH 03)
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
    market_regime VARCHAR,                     -- enum market_regime
    days_to_earnings_bd INTEGER,
    earnings_confidence VARCHAR,               -- enum confidence
    macro_event_risk_flag BOOLEAN,
    calculated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, feature_date, feature_schema_version)
);
```

### 3.9 `strategy_configs`

```sql
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
```

### 3.10 `step3_candidates`

```sql
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
```

### 3.11 `step4_analysis`

```sql
CREATE TABLE IF NOT EXISTS step4_analysis (
    analysis_id VARCHAR PRIMARY KEY,
    candidate_id VARCHAR NOT NULL,
    run_id VARCHAR NOT NULL,
    strategy_config_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    setup_type VARCHAR,                        -- enum setup_type
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
```

### 3.12 `step5_proposals` (merged with PATCH 06)

PATCH 06 adds `raw_rank`, `diversified_rank`, `in_raw_top_n`,
`in_diversified_top_n`, `diversification_applied`. MINI-PATCH 2 documents that
`selected_flag` and `selected_top_n` are kept, with the semantics:

```text
selected_flag = in_diversified_top_n
selected_top_n = in_raw_top_n OR in_diversified_top_n
```

Module 03 only creates the columns. The semantics are enforced by Module 15
(Step 5 Proposal Engine) at write time, not by DB constraints.

```sql
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
    -- PATCH 06 fields (merged):
    raw_rank INTEGER,
    diversified_rank INTEGER,
    in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
    -- Legacy selected columns (semantics defined by Module 15; see MINI-PATCH 2):
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
```

### 3.13 `outcome_tracking_queue`

```sql
CREATE TABLE IF NOT EXISTS outcome_tracking_queue (
    tracking_id VARCHAR PRIMARY KEY,
    proposal_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    entry_date DATE NOT NULL,
    eval_date DATE NOT NULL,
    horizon_bd INTEGER NOT NULL,               -- one of 5, 10, 20, 40
    status VARCHAR NOT NULL,                   -- enum queue status
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    last_repair_attempt TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP
);
```

### 3.14 `signal_outcomes` (merged with PATCH 01)

PATCH 01 adds `entry_price_sim`.

```sql
CREATE TABLE IF NOT EXISTS signal_outcomes (
    outcome_id VARCHAR PRIMARY KEY,
    proposal_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    strategy_config_id VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    entry_date DATE NOT NULL,
    entry_price_raw DOUBLE,
    entry_price_sim DOUBLE,                    -- PATCH 01 (merged)
    return_5bd_pct DOUBLE,
    return_10bd_pct DOUBLE,
    return_20bd_pct DOUBLE,
    return_40bd_pct DOUBLE,
    mfe_40bd_pct DOUBLE,
    mae_40bd_pct DOUBLE,
    realized_r_multiple DOUBLE,
    earnings_within_window BOOLEAN DEFAULT FALSE,
    cross_fold_outcome BOOLEAN DEFAULT FALSE,
    outcome_status VARCHAR NOT NULL,           -- enum outcome_status
    calculated_at TIMESTAMP
);
```

### 3.15 `earnings_calendar`

```sql
CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker VARCHAR NOT NULL,
    earnings_date DATE NOT NULL,
    session VARCHAR,                           -- enum session
    source VARCHAR NOT NULL,
    confidence VARCHAR NOT NULL,               -- enum confidence
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, earnings_date)
);
```

### 3.16 `macro_events_calendar`

```sql
CREATE TABLE IF NOT EXISTS macro_events_calendar (
    event_date DATE NOT NULL,
    event_type VARCHAR NOT NULL,               -- FOMC, CPI, PPI, NFP, POWELL
    importance VARCHAR NOT NULL,               -- high, medium, low
    source VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (event_date, event_type)
);
```

### 3.17 `data_repair_queue`

```sql
CREATE TABLE IF NOT EXISTS data_repair_queue (
    repair_id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    repair_date DATE,
    repair_reason VARCHAR NOT NULL,            -- missing_price, bad_ohlc, mutation, provider_empty, outcome_missing
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_attempt TIMESTAMP,
    status VARCHAR NOT NULL,                   -- enum queue status
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP
);
```

### 3.18 `feature_rebuild_log`

```sql
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
```

### 3.19 `ai_reviews`

```sql
CREATE TABLE IF NOT EXISTS ai_reviews (
    ai_review_id VARCHAR PRIMARY KEY,
    review_type VARCHAR NOT NULL,              -- enum review_type
    proposal_id VARCHAR,
    sim_run_id VARCHAR,
    provider VARCHAR NOT NULL,
    model VARCHAR NOT NULL,
    prompt_version VARCHAR NOT NULL,
    prompt_text TEXT NOT NULL,
    selected_tickers_json JSON,
    ai_response_text TEXT,
    human_action VARCHAR,                      -- enum human_action
    created_at TIMESTAMP NOT NULL
);
```

### 3.20 `execution_decisions`

```sql
CREATE TABLE IF NOT EXISTS execution_decisions (
    decision_id VARCHAR PRIMARY KEY,
    proposal_id VARCHAR NOT NULL,
    ai_review_id VARCHAR,
    decision_source VARCHAR NOT NULL,          -- enum decision_source
    action VARCHAR NOT NULL,                   -- enum execution action
    decision_notes TEXT,
    created_at TIMESTAMP NOT NULL
);
```

### 3.21 Production indexes

Base indexes from the v1 SQL plus the two added by PATCH 06:

```sql
CREATE INDEX IF NOT EXISTS idx_daily_prices_ticker_date     ON daily_prices(ticker, date);
CREATE INDEX IF NOT EXISTS idx_daily_features_ticker_date   ON daily_features(ticker, feature_date);
CREATE INDEX IF NOT EXISTS idx_step3_run_date               ON step3_candidates(run_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_step5_run_date_selected      ON step5_proposals(run_id, signal_date, selected_flag);
CREATE INDEX IF NOT EXISTS idx_outcomes_config_date         ON signal_outcomes(strategy_config_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_queue_status_eval            ON outcome_tracking_queue(status, eval_date);
CREATE INDEX IF NOT EXISTS idx_repair_status                ON data_repair_queue(status, repair_date);
CREATE INDEX IF NOT EXISTS idx_step5_run_raw_rank           ON step5_proposals(run_id, signal_date, raw_rank);
CREATE INDEX IF NOT EXISTS idx_step5_run_div_rank           ON step5_proposals(run_id, signal_date, diversified_rank);
```

### 3.22 Production views

`daily_features_current` (from `13_Views_Reference.md`):

```sql
CREATE OR REPLACE VIEW daily_features_current AS
SELECT *
FROM daily_features
WHERE feature_schema_version = (
    SELECT MAX(feature_schema_version) FROM daily_features
);
```

`selected_proposals_current` (from PATCH 10; supersedes the original
`selected_flag = TRUE` version):

```sql
CREATE OR REPLACE VIEW selected_proposals_current AS
SELECT *
FROM step5_proposals
WHERE in_diversified_top_n = TRUE;
```

Note on `CREATE OR REPLACE VIEW` idempotency: this statement is naturally
idempotent — it replaces the view definition on every call without erroring,
so it is safe to run on both first creation and re-runs.

---

## 4. Simulation schema (simulation.duckdb)

8 sim_* tables, 2 sim indexes, plus the `schema_versions` metadata table
(shared definition, simulation seed row) — 9 tables total. Simulation reads
production data through Module 02's read-only attach helper at *run* time;
it never duplicates production tables, and the attach is **not** needed
during schema creation. Module 03 must NOT create any of the production
views (`daily_features_current`, `selected_proposals_current`) in the
simulation database — those reference production tables that do not exist
there.

### 4.1 `schema_versions` (simulation copy)

Same DDL as in §3.1. Seed row on fresh-DB creation:
`('simulation', 'schema_v01', now(), 'initial merged schema')`.

### 4.2 `sim_runs`

```sql
CREATE TABLE IF NOT EXISTS sim_runs (
    sim_run_id VARCHAR PRIMARY KEY,
    sim_name VARCHAR,
    mode VARCHAR NOT NULL,                     -- research, walk_forward, config_comparison
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    created_at TIMESTAMP NOT NULL,
    config_ids JSON NOT NULL,
    status VARCHAR NOT NULL,                   -- pending, running, success, failed
    notes TEXT
);
```

### 4.3 `sim_folds`

```sql
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
```

### 4.4 `sim_step3_candidates`

```sql
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
```

### 4.5 `sim_step4_analysis`

```sql
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
```

### 4.6 `sim_step5_proposals` (merged with PATCH 06)

```sql
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
    -- PATCH 06 fields (merged):
    raw_rank INTEGER,
    diversified_rank INTEGER,
    in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
    rejection_reason VARCHAR,
    created_at TIMESTAMP NOT NULL
);
```

### 4.7 `sim_signal_outcomes` (merged with PATCH 01 and PATCH 08)

PATCH 01 adds `entry_price_sim`. PATCH 08 adds `list_membership`.

```sql
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
    entry_price_sim DOUBLE,                    -- PATCH 01 (merged)
    return_5bd_pct DOUBLE,
    return_10bd_pct DOUBLE,
    return_20bd_pct DOUBLE,
    return_40bd_pct DOUBLE,
    mfe_40bd_pct DOUBLE,
    mae_40bd_pct DOUBLE,
    realized_r_multiple DOUBLE,
    list_membership VARCHAR,                   -- PATCH 08: raw_only, diversified_only, both
    cross_fold_outcome BOOLEAN NOT NULL DEFAULT FALSE,
    outcome_status VARCHAR NOT NULL,
    calculated_at TIMESTAMP
);
```

### 4.8 `sim_config_comparisons` (merged with PATCH 08)

PATCH 08 adds `list_type` with default `'diversified'`.

```sql
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
    list_type VARCHAR NOT NULL DEFAULT 'diversified',  -- PATCH 08: raw, diversified
    created_at TIMESTAMP NOT NULL
);
```

### 4.9 `sim_ai_reviews`

```sql
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
```

### 4.10 Simulation indexes

```sql
CREATE INDEX IF NOT EXISTS idx_sim_props_run_date       ON sim_step5_proposals(sim_run_id, signal_date);
CREATE INDEX IF NOT EXISTS idx_sim_outcomes_config_date ON sim_signal_outcomes(strategy_config_id, signal_date);
```

---

## 5. Enums

These are **value catalogs**, not DuckDB `ENUM` types. Validation is performed
at the service layer (Pydantic), not enforced by the DB schema. Module 03
must not create DuckDB `ENUM` types.

| Domain | Allowed values |
|---|---|
| `data_quality_status` | `ok`, `warning`, `suspect`, `failed`, `quarantined` |
| `outcome_status` | `pending`, `complete`, `partial`, `unresolvable`, `failed` |
| queue `status` | `pending`, `processing`, `done`, `failed`, `unresolvable` |
| `market_regime` | `bull`, `neutral`, `bear`, `high_risk`, `extreme_risk` |
| `symbol_type` | `stock`, `etf`, `benchmark`, `index` |
| `setup_type` | `trend_pullback`, `breakout`, `volatility_squeeze`, `trend_resume`, `high_tight_flag`, `unknown` |
| `decision_source` | `mechanical_only`, `human_only`, `ai_assisted` |
| `human_action` | `ignored`, `accepted`, `overrode`, `deferred` |
| `session` | `pre_market`, `post_market`, `during_market`, `unknown` |
| `confidence` | `high`, `medium`, `low` |
| `review_type` | `ticker_review`, `simulation_review`, `config_review` |
| execution `action` | `watch`, `paper_trade`, `skip`, `real_trade` |
| `run_status` | `pending`, `running`, `success`, `success_with_warnings`, `failed`, `cancelled` |
| `list_membership` | `raw_only`, `diversified_only`, `both` |
| `list_type` | `raw`, `diversified` |

---

## 6. Idempotency

Every `CREATE TABLE`, `CREATE INDEX`, and `CREATE OR REPLACE VIEW` statement
above is intrinsically idempotent. Module 03 must additionally:

- Skip inserting the `schema_versions` seed row if a row with the same
  `(schema_name, version)` already exists. Use an
  `INSERT INTO schema_versions … WHERE NOT EXISTS (…)` pattern, or check first
  and insert second. Do not rely on PK conflict swallowing.
- Make `apply_schema(db_role)` safe to call repeatedly — calling it twice on
  the same DB must not raise, must not duplicate rows, and must leave the DB
  in the same final state.

---

## 7. What Module 03 must NOT do

- Must NOT execute any `ALTER TABLE` statement.
- Must NOT create base tables and then patch them.
- Must NOT open DB connections directly. All connections must go through
  `app.database.duckdb_manager`.
- Must NOT create DuckDB `ENUM` types — enums are validated at the service
  layer.
- Must NOT enforce semantics of `selected_flag` / `selected_top_n` at the DB
  level — that is Module 15's job.
- Must NOT create production views in the simulation database.
- Must NOT modify Module 01 or Module 02 files.

---

## 7.1 ServiceResult contract for Module 03

Public entry points (`apply_schema`, `apply_prod_schema`,
`apply_debug_schema`, `apply_simulation_schema`) must return a
`ServiceResult` from `app.utils.service_result`. Recommended `metadata` keys:

| Key | Type | Meaning |
|---|---|---|
| `db_role` | `str` | The role the schema was applied to (`prod`, `debug`, or `simulation`) |
| `tables_created` | `list[str]` | Names of tables present after the call (alphabetically sorted) |
| `indexes_created` | `int` | Count of indexes present after the call |
| `views_created` | `int` | Count of views present after the call (0 for simulation) |
| `schema_version` | `str` | The seeded database schema version, i.e. `schema_v01` |
| `seed_row_inserted` | `bool` | `True` on first creation, `False` on idempotent re-application |

`rows_processed` should be set to the count of tables created (or already
present). `status` should be `success` on first creation and on idempotent
re-application; `success_with_warnings` if a non-fatal anomaly was detected;
`failed` only on hard errors.

## 7.2 Testing tolerance for the `schema_versions` seed row

Tests must assert on the exact composite key `(schema_name, version)` —
i.e. one row in `schema_versions` matching the role and `schema_v01`. The
`notes` column is informational; tests should not assert on its exact text
beyond confirming it is non-empty, so that minor wording changes do not
break the suite. The `applied_at` timestamp should be checked as
"non-NULL", not for an exact value.

---

## 8. Quick summary

| Database role | Tables created | Indexes created | Views created | Seed rows in `schema_versions` |
|---|---|---|---|---|
| prod | 20 (incl. `schema_versions`) | 9 | 2 | 1 (`schema_name='prod'`) |
| debug | 20 (incl. `schema_versions`) | 9 | 2 | 1 (`schema_name='debug'`) |
| simulation | 9 (incl. `schema_versions`) | 2 | 0 | 1 (`schema_name='simulation'`) |

Production table list (20, including `schema_versions`): `schema_versions`,
`pipeline_runs`, `pipeline_locks`, `ticker_master`,
`ticker_universe_snapshot`, `sector_etf_map`, `daily_prices`,
`daily_features`, `strategy_configs`, `step3_candidates`, `step4_analysis`,
`step5_proposals`, `outcome_tracking_queue`, `signal_outcomes`,
`earnings_calendar`, `macro_events_calendar`, `data_repair_queue`,
`feature_rebuild_log`, `ai_reviews`, `execution_decisions`.

Simulation table list (9, including `schema_versions`): `schema_versions`,
`sim_runs`, `sim_folds`, `sim_step3_candidates`, `sim_step4_analysis`,
`sim_step5_proposals`, `sim_signal_outcomes`, `sim_config_comparisons`,
`sim_ai_reviews`.
