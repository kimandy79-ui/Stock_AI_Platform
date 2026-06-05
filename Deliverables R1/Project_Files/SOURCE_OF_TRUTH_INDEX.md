# Source-of-Truth Index — Stock AI Platform

This index is the first file to read before implementation or review.

## Current accepted baseline

- Accepted implementation baseline: Modules 01–22.
- Current stable codebase: `stock_ai_platform_260604-02.zip`, with stale codebase docs removed in the cleaned baseline package.
- Next planned work: **Module 21 Dashboard V2 Update**.
- M21 Dashboard V1 remains accepted stable and is not incomplete.
- M22 Debug Mode remains accepted stable.

## Source priority

Use this priority order:

1. Current task prompt.
2. `SOURCE_OF_TRUTH_INDEX.md` and `PROJECT_STATUS_CURRENT.md`.
3. Active module-specific spec.
4. Dependent module specs.
5. Split Project Files.
6. Current stable codebase.
7. Tests as behavior evidence.

## Active next-work spec

- `M21_STREAMLIT_DASHBOARD_V2_WORKFLOW_SPEC.md`

## Accepted module specs

Keep these as accepted contracts. Do not rewrite them unless an explicit architecture decision says so.

- `M02_SCHEMA_SPEC.md`
- `M03_PROVIDER_INTERFACE_SPEC.md`
- `M04_PROVIDER_INTERFACE_SPEC.md`
- `M05_YAHOO_PROVIDER_SPEC.md`
- `M06_UNIVERSE_SNAPSHOT_SPEC.md`
- `M07_BENCHMARK_ETF_LOADER_SPEC.md`
- `M08_DAILY_PRICE_INGESTION_SPEC.md`
- `M09_DATA_VALIDATOR_SPEC.md`
- `M10_MUTATION_DETECTOR_SPEC.md`
- `M11_FEATURE_ENGINE_SPEC.md`
- `M12_MARKET_REGIME_ENGINE_SPEC.md`
- `M13_STEP3_SCREENING_SPEC.md`
- `M14_STEP4_ANALYSIS_SPEC.md`
- `M15_STEP5_PROPOSAL_ENGINE_SPEC.md`
- `M16_OUTCOME_QUEUE_SPEC.md`
- `M17_SIMULATION_ENGINE_SPEC.md`
- `M18_EXPORT_PACKAGE_ENGINE_SPEC.md`
- `M19_AI_REVIEW_ENGINE_SPEC.md`
- `M20_PIPELINE_ORCHESTRATOR_SPEC.md`
- `M21_STREAMLIT_DASHBOARD_SPEC.md`
- `M22_DEBUG_MODE_SPEC.md`

## Split Project Files

- `00_PROJECT_FILE_MAP.md`
- `01a_CORE_PRINCIPLES.md`
- `01b_SCHEMA_AND_DATA.md`
- `01c_FORMULAS_AND_CONFIGS.md`
- `01d_MODULES_AND_PIPELINE.md`
- `01e_UI_AND_TESTING.md`
- `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- `02b_ARCHITECTURE_DECISIONS.md`

## Rules for M21 Dashboard V2 Update

- M21 V1 accepted behavior and tests must remain valid unless the V2 spec explicitly changes them.
- No direct DB writes from Streamlit UI or dashboard `data_access`.
- Write-like actions must call approved service APIs only.
- Approved action services: M17, M18, M19, M20 only if explicitly required, and M22.
- No provider calls from dashboard.
- No heavy domain logic inside dashboard.
- Maintain prod/debug/simulation DB isolation.
