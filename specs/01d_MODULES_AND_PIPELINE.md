# 01d_MODULES_AND_PIPELINE

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.
Rewritten 2026-06-19 for the setup-mode migration (AD-22.19–22.24).
Corrected 2026-06-19 per architect review (fixes 1, 5).

Fix 1: Step 3 = universal eligibility + setup routing (once per signal date).
       Step 4 = setup-specific validation + trade plan (iterates per setup_config_id).
Fix 5: Orchestrator explicitly runs Step 3 once, then Step 4/5 iterate setup configs.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

## Setup-mode logical flow (authoritative)

```
1. Universal eligibility filters       (data ready, stock, price, liquidity, anomaly)
   — runs ONCE per signal date; reads daily_features_current + daily_prices + ticker_master
2. Setup routing / classification      (routes to: breakout | pullback | trend_continuation | consolidation_base)
   — also runs as part of Step 3 (once per signal date)
3. Setup-specific validation + scoring (iterates per active setup_config_id)
4. Trade plan creation                 (entry_proxy_raw / stop / target; RR is an output)
   — raw/adj conversion applied: level_raw = level_adj * (close_raw / close_adj)
5. Risk labeling                       (low | medium | high — output, not config)
6. Ranking + proposal selection        (disposition: BUY | WATCHLIST_ONLY | REJECTED)
7. Diagnostics / export / dashboard
```

---

## FILE: `MODULES/30_Module_01_project_skeleton.md`

# Module 01: Project Skeleton
Standard module contract: inputs (db_manager, config, run_id), outputs
(ServiceResult; writes defined tables), rules (no look-ahead, config-driven,
no unrelated logic, tests required). Unchanged by setup mode.

---

## FILE: `MODULES/31_Module_02_duckdb_manager.md`

# Module 02: DuckDB Manager
Standard module contract. Frozen; unchanged by setup mode.

---

## FILE: `MODULES/32_Module_03_schema_manager.md`

# Module 03: Schema Manager

## Purpose
Create the final merged + setup-mode schema directly (no base+patch ALTER on a
fresh DB). Old strategy data is not preserved; clean reset is permitted.

## Critical merged-schema rule
For a fresh database, create the final schema from the CREATE TABLE definitions
in `01b_SCHEMA_AND_DATA.md`. Do NOT create old base tables then ALTER.

Required final setup-mode schema state:
- `setup_configs` + `risk_label_config` tables (replace `strategy_configs`)
- `step3_candidates`: `routing_status` (NOT NULL), `routing_fail_reason`
- `step4_analysis`: `setup_config_id` (NOT NULL), `setup_type` (NOT NULL),
  `target_is_structural`, structural-level columns (raw-converted)
- `step5_proposals`: setup/risk/disposition columns, `target_is_structural`
- `signal_outcomes`: `setup_type`, `risk_label`, `stop_price_raw`, `target_price_raw`
- `daily_features`: features_v02 structural columns
- Sim tables: parity fields per 01b §SCHEMA/11

---

## FILE: `MODULES/33-39` (Modules 04–10)
Provider interface, Yahoo provider, universe snapshot, benchmark/sector ETF
loader, daily price ingestion, data validator, mutation detector. Standard
module contract. Frozen; unchanged by setup mode.

---

## FILE: `MODULES/40_Module_11_feature_engine.md`

# Module 11: Feature Engine

## Purpose
Compute `features_v02`, adding structural-level features required by setup mode.
Stored on adjusted basis; Step 4 applies raw conversion at trade-plan time.

New columns (all nullable; populated only for features_v02 rows):
`ema20_slope`, `ema50_slope`, `atr_compression_score`, `pullback_depth_pct`,
`swing_high`, `swing_low`, `support_level`, `resistance_level`,
`next_resistance_level`, `base_high`, `base_low`, `range_width_pct`,
`range_duration`, `range_tightness_score`, `volume_dry_up_score`,
`volume_expansion_score`, `relative_strength_vs_spy`.

Plus all v01 features retained.

## Rules
No look-ahead (date <= feature_cutoff_date). Raw/adjusted guardrails preserved.
Clean recompute/reset path (historical preservation not required).

## Tests
Feature presence + version, no-look-ahead, structural levels sane
(support < close_adj < resistance where applicable), recompute idempotency,
raw/adj conversion sanity check.

---

## FILE: `MODULES/41_Module_12_market_regime_engine.md`

# Module 12: Market Regime Engine
Unchanged (SPY/QQQ/VIX). Regime stored as VARCHAR, may be NULL; NULL is not
defaulted to neutral at any layer (fix 9).

---

## FILE: `MODULES/42_Module_13_step3_universal_eligibility.md`

# Module 13: Step 3 — Universal Eligibility + Setup Routing

## Purpose
Apply universal tradability/data filters, then route eligible tickers to
candidate setup types. No RVOL/score/ATR hard gate (AD-22.23).

**Runs ONCE per signal date. Produces one `step3_candidates` row per ticker.**

## Inputs (fix 3)
- `daily_features_current`
- `daily_prices` WHERE date = signal_date — for `close_raw`, `data_quality_status`, OHLCV anomaly checks
- `ticker_master` — for `symbol_type`

## Universal config rule (fix 4)
Uses the `universe` block from setup configs (all four must be identical) or a
dedicated global universe config. Service layer asserts universe config parity
across all active setup configs before running.

## Outputs
- `step3_candidates` (one row per ticker; `routing_status` NOT NULL)

## Eligibility filters
`feature_ready`, `symbol_type = 'stock'`, `close_raw >= min_price`,
`avg_dollar_volume_20d >= min_avg_dollar_volume_20d`, `data_quality_status = 'ok'`,
no OHLCV anomaly.

## Routing
Per `FORMULAS/61_Step3_Universal_Eligibility.md`. Routing predicates are coarse
only; full validation is Step 4. No-route: `routing_status = 'no_route'`,
`routing_fail_reason = 'no_route'`. Ineligible: `routing_status = 'ineligible'`.

## Tests
Eligibility pass/fail, RVOL is NOT a gate, multi-route recorded,
no-route/ineligible routing_status set correctly, daily_prices join verified,
universe config parity assertion fires on mismatch.

---

## FILE: `MODULES/43_Module_14_step4_setup_validation.md`

# Module 14: Step 4 — Setup Validation + Trade Plan

## Purpose
For each ticker in `step3_candidates` where `routing_status = 'routed'`,
validate each setup_type in `routed_setup_types` under the corresponding
active `setup_config_id`. Produce structural trade plan.

**Iterates active setup_config_ids matching routed setup types.**

## Inputs
- `step3_candidates` WHERE `routing_status = 'routed'`
- `daily_features_current` — structural-level features (adjusted basis)
- `daily_prices` WHERE date = signal_date — `close_raw`, `close_adj` for raw/adj conversion
- `setup_configs` WHERE `active_flag = TRUE`

## Outputs
- `step4_analysis` (one row per ticker × setup_type combination)

## Rules
Per `FORMULAS/62_Step4_Setup_Validation.md`. Raw conversion applied before stop/target.
`estimated_rr` always an output. `target_is_structural` records whether
structural or fixed-R fallback target was used. `market_regime` never defaulted
to neutral; NULL regime blocks BUY in Step 5.

## Tests
Per-setup validation pass/fail, stop < entry < target, RR positive,
`target_is_structural` correctly set, RVOL rule per setup (pullback never
hard-rejects on low RVOL), raw/adj conversion sanity (level_raw ≈ level_adj
when close_raw ≈ close_adj), NULL regime propagates correctly.

---

## FILE: `MODULES/44_Module_15_step5_proposal_engine.md`

# Module 15: Step 5 — Risk Labeling, Disposition, Proposals

## Purpose
Assign risk_score/risk_label/risk_reasons, set disposition
(BUY/WATCHLIST_ONLY/REJECTED), dedupe multi-route tickers to the best
risk-adjusted route, rank raw + diversified, select Top N.

## Rules
Per `FORMULAS/63_Step5_Risk_Labeling_And_Proposals.md`. NULL regime blocks BUY.
Fixed-R fallback (`target_is_structural = FALSE`) sets `target_room` component
to 0. Only BUY/WATCHLIST_ONLY ranked; REJECTED stored for diagnostics.
Exactly one active row per setup_type checked before seeding; one active
risk_label_config row checked before running.

## Tests
Risk label monotonic in risk_score, NULL regime → at most WATCHLIST_ONLY,
disposition gates, fixed-R fallback target_room = 0, dedupe keeps best route,
raw vs diversified ranking, hard-cap no-double-penalty.

---

## FILE: `MODULES/45_Module_16_outcome_queue.md`

# Module 16: Outcome Queue
Create outcome tasks for proposals where `in_raw_top_n OR in_diversified_top_n`.
Store `stop_price_raw` + `target_price_raw` from proposal in outcome row.
Outcomes carry `setup_type` + `risk_label`. Standard contract otherwise.

---

## FILE: `MODULES/46_Module_17_simulation_engine.md`

# Module 17: Simulation Engine
Walk-forward over setup configs. Group outcomes by `setup_type` and `risk_label`.
Report expectancy/win rate/stop-hit/target-hit/MFE/MAE per setup type and
risk label. Writes only sim_* tables; attaches prod read-only. Sim may hold
multiple active configs per setup_type for comparison; one-active rule is prod/debug only.

---

## FILE: `MODULES/47_Module_18_export_package_engine.md`

# Module 18: Export Package Engine
Standard contract. Export includes `setup_type`, `setup_score`, `risk_label`,
`disposition`, `target_is_structural`, trade plan, structural levels.

---

## FILE: `MODULES/48_Module_19_ai_review_engine.md`

# Module 19: AI Review Engine
Setup-specific review checklists (breakout / pullback / trend_continuation /
consolidation_base). No mechanical attribution contamination.

---

## FILE: `MODULES/49_Module_20_pipeline_orchestrator.md`

# Module 20: Pipeline Orchestrator

## Purpose
Run the setup-mode pipeline. Primary iteration key is `setup_config_id`.

## Step execution model (fix 5)

```
for each signal_date:
    # Step 3: runs ONCE, not per setup config
    run_step3_universal_eligibility(signal_date)
    # → writes step3_candidates (one row per universe ticker)

    # Step 4: iterates per active setup_config_id
    for each active setup_config in setup_configs where active_flag = TRUE:
        run_step4_validation(signal_date, setup_config)
    # → writes step4_analysis (one row per ticker × setup_type that routed)

    # Step 5: runs once per signal_date across all setup types
    run_step5_risk_and_proposals(signal_date)
    # → writes step5_proposals (deduped to best route per ticker)
```

Step-major execution is retained for resume-from-step. Partial failure on any
step records `steps_completed` and allows resume from the failed step.
Lock/heartbeat pattern unchanged.

## Tests
Step 3 called exactly once per signal date, Step 4 called once per active
setup_config_id, step-major resume logic, lock/heartbeat.

---

## FILE: `MODULES/50_Module_21_streamlit_dashboard.md`

# Module 21: Streamlit Dashboard
Primary filters: `setup_type` and `risk_label`. Proposal table: ticker,
setup_type, setup_score, risk_label, entry, stop, target, estimated_rr,
`target_is_structural`, support, resistance, next_resistance, earnings_days,
market_regime, disposition. Config view manages setup_configs + risk_label_config.
Pipeline-health tab includes setup-mode funnel diagnostics.

---

## FILE: `MODULES/51_Module_22_debug_mode.md`

# Module 22: Debug Mode + Funnel Diagnostics
Setup-mode funnel: universal eligibility → routing → per-setup validation →
trade plan → risk labeling → proposals. Per-setup counts: routed / passed /
failed-by-reason. Risk-label and disposition counts. `target_is_structural`
breakdown. Forced debug role/run_type preserved.

---

## FILE: `PIPELINE/70_Pipeline_Orchestrator_Spec.md`

# Pipeline Orchestrator Spec

## run_id
UUID4 string.

## Lock
`pipeline_locks`: acquire before run, heartbeat during, release after.

## Already-run check
Check `pipeline_runs` where `run_date = today AND status IN (success, success_with_warnings)`.
If exists: block normal run; allow `force_rerun`.

## Step order
1. benchmark/sector ETF ingestion
2. stock universe ingestion
3. stock price ingestion
4. validation
5. mutation detection
6. feature calculation (features_v02)
7. market regime
8. Step 3: universal eligibility + setup routing (ONCE per signal date)
9. Step 4: setup validation + trade plan (PER active setup_config_id)
10. Step 5: risk labeling + disposition + proposals (ONCE per signal date)
11. outcome queue generation
12. due outcome processing
13. dashboard materialization

## Partial failure
On step failure: update `pipeline_runs`, keep completed-step state, allow resume
from failed step.

## Outcome queue generation rule
Create tasks where `in_raw_top_n = TRUE OR in_diversified_top_n = TRUE`.

---

## FILE: `PIPELINE/72_Error_Handling_Reference.md`

# Error Handling Reference
Warning (continue): missing sector, low-confidence earnings, sector RS
unavailable, small provider timeout count.
Recoverable (continue degraded): some downloads failed, repair queue populated,
non-critical dashboard failure.
Critical (stop): DB connection failure, schema mismatch, invalid config, feature
crash, look-ahead validation failure, universe config parity mismatch (fix 4).
Critical failures set `pipeline_runs.status = 'failed'`.

---

## FILE: `PIPELINE/73_Trading_Calendar_Spec.md`

# Trading Calendar Spec
Use `pandas_market_calendars` NYSE calendar. Functions: `is_trading_day`,
`previous_trading_day`, `next_trading_day`, `add_trading_days`,
`trading_days_between`. Unchanged.

---

## FILE: `SIMULATION/80_Simulation_Engine_Spec.md`

# Simulation Engine Spec
Writes only to `simulation.duckdb`. Attach prod read-only.
V1 uses precomputed prod `daily_features` (features_v02). If feature schema
changes, rebuild prod features first. Sim may hold multiple active configs per
`setup_type` for comparison; one-active rule is `prod`/`debug` only.

---

## FILE: `SIMULATION/81_WalkForward_Protocol.md`

# Walk-Forward Protocol
Expanding window. Initial train 12 months minimum. Test fold = calendar quarter.
Config selection maximizes expectancy subject to `resolved_outcomes_pct >= 0.85`
and `max_drawdown_pct <= 25`, evaluated per setup config. Cross-fold outcomes
stored with `cross_fold_outcome = TRUE` and excluded from fold metrics.

---

## FILE: `SIMULATION/82_Simulation_DB_Access_Spec.md`

# Simulation DB Access
1. Connect to `simulation.duckdb`.
2. `ATTACH 'data/duckdb/prod.duckdb' AS prod (READ_ONLY)`.
3. Query `prod.daily_prices` / `prod.daily_features_current`.
4. Write only `sim_*` tables.
Never write to prod from a simulation session.
