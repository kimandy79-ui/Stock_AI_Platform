# 01d_MODULES_AND_PIPELINE

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

## FILE: `MODULES/30_Module_01_project_skeleton.md`

# Module 01: Project Skeleton

## Purpose
Implement the `project_skeleton` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/31_Module_02_duckdb_manager.md`

# Module 02: Duckdb Manager

## Purpose
Implement the `duckdb_manager` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/32_Module_03_schema_manager.md`

# Module 03: Schema Manager

## Purpose
Implement the `schema_manager` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.


## Critical merged-schema rule
PATCH 1 and MINI-PATCH 2 are already merged into this source-of-truth file.

For a fresh database, create the final merged schema directly from the final CREATE TABLE definitions.

Do NOT:
1. create old base tables;
2. immediately run patch ALTER TABLE statements on the fresh database.

Required final schema state includes:
- `signal_outcomes.entry_price_sim`
- `sim_signal_outcomes.entry_price_sim`
- `sim_signal_outcomes.list_membership`
- `step5_proposals.raw_rank`
- `step5_proposals.diversified_rank`
- `step5_proposals.in_raw_top_n`
- `step5_proposals.in_diversified_top_n`
- `step5_proposals.diversification_applied`
- `sim_step5_proposals.raw_rank`
- `sim_step5_proposals.diversified_rank`
- `sim_step5_proposals.in_raw_top_n`
- `sim_step5_proposals.in_diversified_top_n`
- `sim_step5_proposals.diversification_applied`
- `sim_config_comparisons.list_type`

---

## FILE: `MODULES/33_Module_04_provider_interface.md`

# Module 04: Provider Interface

## Purpose
Implement the `provider_interface` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/34_Module_05_yahoo_provider.md`

# Module 05: Yahoo Provider

## Purpose
Implement the `yahoo_provider` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/35_Module_06_universe_snapshot_engine.md`

# Module 06: Universe Snapshot Engine

## Purpose
Implement the `universe_snapshot_engine` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/36_Module_07_benchmark_etf_loader.md`

# Module 07: Benchmark / Sector ETF Loader

## Purpose
Load SPY, QQQ, ^VIX and sector ETFs before feature calculation.

## Symbols
- SPY: benchmark ETF
- QQQ: benchmark ETF
- ^VIX: index benchmark
- XLK, XLF, XLV, XLY, XLP, XLC, XLI, XLE, XLB, XLU, XLRE: sector ETFs

## VIX handling
Use Yahoo symbol `^VIX`.
Store:
- close_raw = close_adj = VIX close
- open/high/low if available
- volume_raw = NULL or 0
- symbol_type = index
- exclude from screening

## ETF handling
Sector ETFs are symbol_type = benchmark or etf and excluded from screening.
They are used only for market regime and sector_relative_strength.

---

## FILE: `MODULES/37_Module_08_daily_price_ingestion.md`

# Module 08: Daily Price Ingestion

## Purpose
Implement the `daily_price_ingestion` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/38_Module_09_data_validator.md`

# Module 09: Data Validator

## Purpose
Implement the `data_validator` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/39_Module_10_mutation_detector.md`

# Module 10: Mutation Detector

## Purpose
Implement the `mutation_detector` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/40_Module_11_feature_engine.md`

# Module 11: Feature Engine

## Purpose
Implement the `feature_engine` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/41_Module_12_market_regime_engine.md`

# Module 12: Market Regime Engine

## Purpose
Implement the `market_regime_engine` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/42_Module_13_step3_screening.md`

# Module 13: Step3 Screening

## Purpose
Apply hard filters and soft scoring.

## Inputs
- daily_features_current
- ticker_master
- strategy_config

## Outputs
- step3_candidates

## Hard filters
- feature_ready TRUE
- symbol_type stock
- close_raw >= min_price
- avg_dollar_volume_20d >= min_avg_dollar_volume_20d
- rvol20 >= min_rvol
- data quality OK

## Scoring
Use FORMULAS/61_Scoring_Formulas_Step3.md.

## Tests
- hard filter pass/fail
- score reproducibility
- missing sector RS handled as neutral/no boost

---

## FILE: `MODULES/43_Module_14_step4_setup_analysis.md`

# Module 14: Step4 Setup Analysis

## Purpose
Classify setup type and estimate RR.

## Inputs
- step3_candidates
- daily_features_current
- daily_prices

## Outputs
- step4_analysis

## Rules
Use FORMULAS/62_Scoring_Formulas_Step4.md.

## Tests
- stop below entry
- target above entry
- estimated_rr finite and positive
- setup_type enum valid

---

## FILE: `MODULES/44_Module_15_step5_proposal_engine.md`

# Module 15: Step5 Proposal Engine

## Purpose
Implement the `step5_proposal_engine` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/45_Module_16_outcome_queue.md`

# Module 16: Outcome Queue

## Purpose
Implement the `outcome_queue` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/46_Module_17_simulation_engine.md`

# Module 17: Simulation Engine

## Purpose
Implement the `simulation_engine` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/47_Module_18_export_package_engine.md`

# Module 18: Export Package Engine

## Purpose
Implement the `export_package_engine` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/48_Module_19_ai_review_engine.md`

# Module 19: Ai Review Engine

## Purpose
Implement the `ai_review_engine` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/49_Module_20_pipeline_orchestrator.md`

# Module 20: Pipeline Orchestrator

## Purpose
Implement the `pipeline_orchestrator` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/50_Module_21_streamlit_dashboard.md`

# Module 21: Streamlit Dashboard

## Purpose
Implement the `streamlit_dashboard` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `MODULES/51_Module_22_debug_mode.md`

# Module 22: Debug Mode

## Purpose
Implement the `debug_mode` component according to the Master TZ.

## Inputs
- db_manager where applicable
- config where applicable
- run_id where applicable

## Outputs
- ServiceResult
- writes to defined tables where applicable

## Rules
- no look-ahead
- config-driven
- no direct unrelated module logic
- tests required

## Testing
Unit tests and integration hooks must be provided.

---

## FILE: `PIPELINE/70_Pipeline_Orchestrator_Spec.md`

# Pipeline Orchestrator Spec

## run_id
Use UUID4 string.

## Lock
Use pipeline_locks table:
- acquire lock before run
- heartbeat during run
- release lock after success/failure

## Already-run check
Before scheduled/manual run:
check pipeline_runs where run_date = today and status in success/success_with_warnings.

If exists:
- block normal run
- allow force_rerun

## Step order
1. benchmark/sector ETF ingestion
2. stock universe ingestion
3. stock price ingestion
4. validation
5. mutation detection
6. feature calculation
7. Step3
8. Step4
9. Step5
10. outcome queue generation
11. due outcome processing
12. dashboard materialization
13. backup

## Partial failure
If a step fails:
- update pipeline_runs
- keep completed step state
- allow resume from failed step


## Outcome queue generation rule
Create outcome tracking tasks for proposals where:

`in_raw_top_n = TRUE OR in_diversified_top_n = TRUE`

Reason: the system must track both raw Top 20 and diversified Top 20 performance.

---

## FILE: `PIPELINE/72_Error_Handling_Reference.md`

# Error Handling Reference

## Warning
Continue:
- missing sector
- low confidence earnings
- small provider timeout count

## Recoverable failure
Continue degraded:
- some tickers failed download
- repair queue populated
- non-critical dashboard materialization failure

## Critical failure
Stop:
- DB connection failure
- schema mismatch
- invalid config
- feature calculation crash
- look-ahead validation failure

All critical failures write pipeline_runs.status = failed.

---

## FILE: `PIPELINE/73_Trading_Calendar_Spec.md`

# Trading Calendar Spec

Use `pandas_market_calendars` with NYSE calendar.

All business day calculations use NYSE trading sessions.

Functions required:
- is_trading_day(date)
- previous_trading_day(date)
- next_trading_day(date)
- add_trading_days(date, n)
- trading_days_between(start, end)

---

## FILE: `SIMULATION/80_Simulation_Engine_Spec.md`

# Simulation Engine Spec

## Storage
Writes only to simulation.duckdb.

## Read access
Attach prod read-only:
`ATTACH 'data/duckdb/prod.duckdb' AS prod (READ_ONLY);`

## Simulation flow
For each sim date:
1. load universe snapshot as of date
2. load daily_features_current where feature_cutoff_date <= sim date
3. run Step3/4/5 into sim tables
4. create simulated outcomes
5. calculate outcomes once eval dates available

## Feature recalculation
V1 uses precomputed prod features.
If feature schema changes, rebuild prod features first or run feature-specific simulation version.

---

## FILE: `SIMULATION/81_WalkForward_Protocol.md`

# Walk-Forward Protocol

## Default
Expanding window.

Initial train:
12 months minimum.

Test fold:
calendar quarter.

## Config selection
Calculate metrics on train window.
Select config maximizing expectancy subject to:
- resolved_outcomes_pct >= 0.85
- max_drawdown_pct <= 25

## Cross-fold outcomes
If signal outcome extends beyond test fold:
- store it
- cross_fold_outcome = TRUE
- exclude from fold metrics

---

## FILE: `SIMULATION/82_Simulation_DB_Access_Spec.md`

# Simulation DB Access

Simulation session:
1. connect to simulation.duckdb
2. ATTACH prod.duckdb read-only
3. query prod.daily_prices / prod.daily_features_current
4. write only sim_* tables

Never write to prod from simulation session.

---
