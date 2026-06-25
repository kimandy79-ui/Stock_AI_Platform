# Stock AI Platform — CLAUDE.md

## Project type
Local swing-trading research platform.
Stack: Python 3.14 / DuckDB / Polars / Streamlit / Windows.
Active architecture: setup-mode (AD-22.19–22.24). All 7 migration phases complete.
Platform is in production. No architectural changes without explicit instruction.

---

## Source of truth (priority order)
1. This file + specs/01a–01e + specs/02b for product/schema/formula/trading logic
2. specs/02_PROJECT_IMPLEMENTATION_CONTEXT.md for coding standards and workflow
3. specs/MNN_*_SPEC.md for per-module contracts (setup-mode migrated specs only)
4. Stable codebase (current files on disk)

If sources conflict: setup-mode SoT (01a–01e, 02b) wins over any older spec or code.
Do not use old strategy-mode contracts. Do not reopen architecture.

---

## Active selection architecture
```
setup_type:    breakout | pullback | trend_continuation | consolidation_base
setup_config_id: primary config key (replaces legacy strategy_config_id)
risk_label:    low | medium | high  — OUTPUT only, assigned after setup validation
disposition:   BUY | WATCHLIST_ONLY | REJECTED
```
Legacy terms (retired, must not appear as active drivers):
aggressive, normal, conservative, strategy_config_id, strategy_configs

---

## Module-to-file mapping (exact paths)

### Foundation
```
M01  app/config/constants.py
     app/config/settings.py
     app/config/env.py
     app/utils/service_result.py
     app/utils/trading_calendar.py
     app/utils/logging_config.py
M02  app/database/duckdb_manager.py
M03  app/database/schema_manager.py
```

### Providers
```
M04  app/providers/provider_interface.py
M05  app/providers/yahoo_provider.py
```

### Data layer
```
M06  app/services/universe/universe_snapshot.py
M07  app/services/benchmarks/benchmark_etf_loader.py
M08  app/services/ingestion/daily_price_ingestion.py
M09  app/services/validation/data_validator.py
M10  app/services/mutation/mutation_detector.py
```

### Features & screening
```
M11  app/services/features/feature_engine.py
M12  app/services/regime/market_regime_engine.py
M13  app/services/screening/step3_universal_eligibility.py
     app/services/screening/step3_screening.py          ← legacy; active is step3_universal_eligibility
M14  app/services/screening/m14_setup_validators.py
     app/services/analysis/step4_setup_validation_engine.py
     app/services/analysis/step4_analysis_engine.py     ← legacy pre-migration
M15  app/services/proposal/step5_proposal_engine.py
```

### Outcomes, simulation, review
```
M16  app/services/outcomes/outcome_queue.py
M17  app/services/simulation/simulation_engine.py
M18  app/services/export/export_package_engine.py
M19  app/services/ai_review/ai_review_engine.py
```

### Orchestration & UI
```
M20  app/services/pipeline/pipeline_orchestrator.py
M21  app/dashboard/streamlit_app.py
     app/dashboard/data_access.py
     app/dashboard/action_service.py
     app/dashboard/ticker_report.py
M22  app/services/debug/debug_mode.py
     app/services/diagnostics/funnel_diagnostics.py
```

### Config layer (cross-cutting)
```
     app/services/config/config_service.py
     app/services/config/config_validator.py
     app/services/config/default_configs.py
```

### Tools (CLI entry points)
```
     tools/init_prod_db.py
     tools/init_debug_db.py
     tools/init_simulation_db.py
     tools/run_prod_pipeline.py
     tools/run_debug_pipeline.py
     tools/backfill_prod_history.py
     tools/import_legacy_prices.py
     tools/reset_pipeline_data.py
     tools/run_funnel_diagnostics.py
     tools/backfill_company_names.py
     tools/_bootstrap.py
```

### Tests
```
     tests/test_project_skeleton.py          M01
     tests/test_duckdb_manager.py            M02
     tests/test_schema_manager.py            M03
     tests/test_provider_interface.py        M04
     tests/test_yahoo_provider.py            M05
     tests/test_universe_snapshot.py         M06
     tests/test_benchmark_etf_loader.py      M07
     tests/test_daily_price_ingestion.py     M08
     tests/test_data_validator.py            M09
     tests/test_mutation_detector.py         M10
     tests/test_feature_engine.py            M11
     tests/test_feature_engine_v02.py        M11 features_v02
     tests/test_market_regime_engine.py      M12
     tests/test_step3_screening.py           M13 legacy
     tests/test_step3_universal_eligibility.py  M13 setup-mode
     tests/test_step4_analysis.py            M14 legacy
     tests/test_m14_setup_validators.py      M14 setup-mode
     tests/test_step5_proposal_engine.py     M15
     tests/test_outcome_queue.py             M16
     tests/test_simulation_engine.py         M17
     tests/test_export_package_engine.py     M18
     tests/test_ai_review_engine.py          M19
     tests/test_pipeline_orchestrator.py     M20
     tests/test_phase6_orchestrator.py       M20 phase6
     tests/test_dashboard.py                 M21
     tests/test_dashboard_actions.py         M21 actions
     tests/test_debug_mode.py                M22
     tests/test_funnel_diagnostics.py        M22 diagnostics
     tests/test_config_service.py            config layer
     tests/test_sector_normalization.py      config layer
     tests/test_orchestrator_config_loading.py  config+M20
     tests/test_phase6_diagnostics.py        phase6
     tests/test_phase7_setup_mode.py         phase7 integration
     tests/test_tools_runners.py             tools
     tests/test_backfill_prod_history.py     tools
     tests/test_import_legacy_prices.py      tools
     tests/test_run_debug_pipeline.py        tools
```

### Specs
```
     specs/01a_CORE_PRINCIPLES.md
     specs/01b_SCHEMA_AND_DATA.md
     specs/01c_FORMULAS_AND_CONFIGS.md
     specs/01d_MODULES_AND_PIPELINE.md
     specs/01e_UI_AND_TESTING.md
     specs/02_PROJECT_IMPLEMENTATION_CONTEXT.md
     specs/02b_ARCHITECTURE_DECISIONS.md
     specs/M02_SCHEMA_SPEC.md
     specs/M03_SCHEMA_SPEC.md
     specs/M03_SCHEMA_SPEC_CONFIG_DELTA.md
     specs/M04_PROVIDER_INTERFACE_SPEC.md  (also at M03_PROVIDER_INTERFACE_SPEC.md)
     specs/M05_YAHOO_PROVIDER_SPEC.md
     specs/M06_UNIVERSE_SNAPSHOT_SPEC.md
     specs/M06_UNIVERSE_SNAPSHOT_CONFIG_DELTA.md
     specs/M07_BENCHMARK_ETF_LOADER_SPEC.md
     specs/M08_DAILY_PRICE_INGESTION_SPEC.md
     specs/M09_DATA_VALIDATOR_SPEC.md
     specs/M10_MUTATION_DETECTOR_SPEC.md
     specs/M11_FEATURE_ENGINE_SPEC.md
     specs/M11_CONFIG_SERVICE_SPEC.md
     specs/M12_MARKET_REGIME_ENGINE_SPEC.md
     specs/M13_STEP3_SCREENING_SPEC.md
     specs/M14_STEP4_ANALYSIS_SPEC.md
     specs/M15_STEP5_PROPOSAL_ENGINE_SPEC.md
     specs/M16_OUTCOME_QUEUE_SPEC.md
     specs/M17_SIMULATION_ENGINE_SPEC.md
     specs/M18_EXPORT_PACKAGE_ENGINE_SPEC.md
     specs/M19_AI_REVIEW_ENGINE_SPEC.md
     specs/M20_PIPELINE_ORCHESTRATOR_SPEC.md
     specs/M20_PIPELINE_ORCHESTRATOR_CONFIG_DELTA.md
     specs/M21_STREAMLIT_DASHBOARD_SPEC.md
     specs/M21_DASHBOARD_V2_CONFIG_MANAGEMENT_ADDENDUM.md
     specs/M21_CONFIG_REBUILD_DELIVERY_NOTES.md
     specs/M22_DEBUG_MODE_SPEC.md
     specs/M22_DEBUG_MODE_CONFIG_DELTA.md
     specs/M22_FUNNEL_DIAGNOSTICS_SPEC.md
```

---

## DB files
```
data/duckdb/prod.duckdb        production (write via pipeline only)
data/duckdb/debug.duckdb       debug runs only
data/duckdb/simulation.duckdb  simulation only; attaches prod read-only
data/input/                    static CSVs (e.g. backfill_tickers_common_only.csv)
```

---

## Non-negotiable rules

**Pipeline flow:**
- Step 3 (step3_universal_eligibility) runs ONCE per signal date
- Step 4 (m14_setup_validators / step4_setup_validation_engine) iterates per active setup_config_id
- Step 5 runs once per signal date across all setup types

**Data integrity:**
- RVOL is NOT a universal hard gate (AD-22.23); setup-specific only
- market_regime NULL never defaulted to neutral; NULL blocks BUY (WATCHLIST_ONLY at most)
- Fixed-R target only when no structural evidence above entry exists; target_is_structural = FALSE
- estimated_rr is always an OUTPUT, never a fixed constant
- entry_price_sim (not entry_price_raw) for all return/MFE/MAE/R-multiple calculations
- resistance_blocks=True forces WATCHLIST_ONLY; fixed-R fallback not acceptable when structural evidence exists above entry

**DB discipline:**
- All DB access through DuckDBManager — no direct duckdb imports in service modules
- No DDL or ATTACH in executed SQL strings
- Single-writer DuckDB discipline (Windows); each operation opens/executes/closes
- Debug pipeline never writes to prod.duckdb
- Dashboard (M21) is read-only; all mutations through DashboardActionService

**Code quality:**
- No print() in service/library/provider/database modules
- Trading calendar must hard-gate with RuntimeError — no weekday-only fallback
- Trading-day write guards on backfill_prod_history.py and pipeline_orchestrator.py
- entry_price_raw / stop_price_raw / target_price_raw / final_display_status sourced from step5_proposals

**Config:**
- Exactly one active setup_config per setup_type in prod/debug
- Exactly one active risk_label_config in prod/debug
- top_n controlled by risk_label_config.ranking.top_n only; setup configs must not control it
- Config is immutable; clone and version, never edit in place

---

## Frozen modules (do not modify without explicit instruction)
```
M02  app/database/duckdb_manager.py
M04  app/providers/provider_interface.py
M05  app/providers/yahoo_provider.py
M06  app/services/universe/universe_snapshot.py
M07  app/services/benchmarks/benchmark_etf_loader.py
M08  app/services/ingestion/daily_price_ingestion.py
M09  app/services/validation/data_validator.py
M10  app/services/mutation/mutation_detector.py
M12  app/services/regime/market_regime_engine.py
     app/utils/trading_calendar.py
     app/utils/service_result.py
```

---

## Current state & next steps
All 7 migration phases complete. Platform running in production.

Pending (in order):
1. Funnel diagnostics — collect real data on false_breakout_rate, target_hit_rate, pullback_failure_rate
2. Scoring additions (post-diagnostics only):
   - relative_strength_score for BREAKOUT (high priority)
   - sector_strength_score for BREAKOUT/PULLBACK, nearby_resistance_penalty for PULLBACK (medium)
3. Config threshold tuning — deferred until diagnostics provide empirical signal

Do not add scoring components or tune thresholds before diagnostics data is available.

---

## Coding standards (summary)
- Python 3.11+, type hints everywhere, module-level docstrings
- snake_case files/functions, PascalCase classes, UPPER_SNAKE_CASE constants
- Polars-first; pandas only when unavoidable (provider/library compat)
- ServiceResult on all service returns (status: success | success_with_warnings | failed)
- UUID4 for IDs, ISO dates, datetime.date for dates
- pathlib for all paths
- Tests: pytest, tmp_path, no real DB files, offline network tests, monkeypatch for DI
- Commit format: moduleNN_short_name_stable
