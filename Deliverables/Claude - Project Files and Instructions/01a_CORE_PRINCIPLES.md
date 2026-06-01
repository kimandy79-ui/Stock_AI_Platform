# 01a_CORE_PRINCIPLES

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

# 01. MASTER ARCHITECTURE - SOURCE OF TRUTH — MERGED

Status: canonical merged implementation specification for Claude Project Files / AI-assisted coding.
Generated: 2026-05-29

## Absolute precedence rule

This file already merges the source packages in this order:

1. `SwingTradingSystem_Master_TZ_MINI_PATCH_2` — highest authority.
2. `SwingTradingSystem_Master_TZ_PATCH_1` — overrides FULL unless MINI PATCH 2 says otherwise.
3. `SwingTradingSystem_Master_TZ_v1_FULL` — baseline only where not overridden.

Therefore:

- MINI PATCH 2 precedes PATCH 1 and FULL.
- PATCH 1 precedes FULL.
- Do not implement an older FULL rule if it conflicts with PATCH 1 or MINI PATCH 2.
- Do not re-apply PATCH 1 or MINI PATCH 2 as separate ALTER scripts when creating a fresh database.
- For Module 03 Schema Manager, create the final merged schema directly.
- Treat raw patch files, old review prompts, and old module prompts as archival only; they are intentionally not included as coding instructions in this merged file.

## What was deliberately removed to avoid confusing Claude

The following were omitted from this merged project file:

- old AI coding prompt templates from `PROMPTS/`;
- Claude review prompts;
- patch application checklists;
- manifests;
- standalone ALTER TABLE patch snippets as implementation instructions.

Reason: coding should use this merged final state, not old prompts or old base schema plus patches.

## Critical merged decisions

### Feature schema version
Use zero-padded version strings:

```text
FEATURE_SCHEMA_VERSION = "features_v01"
```

### Avg dollar volume

```text
avg_dollar_volume_20d = mean(close_raw * volume_raw over prior 20 trading days)
```

`volume_adj` is reserved and unused in V1 feature formulas.

### Step4 entry proxy

```text
entry_proxy_raw = close_raw on signal_date
```

Use `entry_proxy_raw` for Step4 stop, target, and estimated RR. Actual next-day entry is recorded later by outcome tracking.

### Outcome entry prices

```text
entry_price_raw = next trading day open_raw
entry_price_sim = open_raw * (1 + slippage_bps / 10000)
```

Use `entry_price_sim` for return, MFE, MAE, and realized R-multiple calculations. Keep `entry_price_raw` for audit/execution reference.

### Raw vs diversified ranking
Always calculate both raw and diversified rankings.

- `raw_rank`: ranking before diversification.
- `diversified_rank`: ranking after diversification logic.
- `in_raw_top_n`: raw top-N membership.
- `in_diversified_top_n`: diversified top-N membership.
- `selected_flag = in_diversified_top_n`.
- `selected_top_n = in_raw_top_n OR in_diversified_top_n`.

### Hard cap vs soft penalty
If `hard_cap_enabled = TRUE`, over-cap candidates are rejected from the diversified list and do not receive a soft penalty.

If `hard_cap_enabled = FALSE`, no hard rejection occurs and soft penalties apply.

### Outcome queue
Create outcome tracking tasks for proposals where:

```text
in_raw_top_n = TRUE OR in_diversified_top_n = TRUE
```

---

# Merged source documents


---

---

## FILE: `00_Master_Index.md`

# SwingTradingSystem Master TZ v1 FULL

Status: Full implementation-grade specification for AI-assisted Python development.

This package is based on:
- architecture review cycle V1.5 → V1.7.1;
- Claude final green-light;
- accepted research-grade V1 limitations;
- final decision to use DuckDB, Polars-first, Streamlit local dashboard, YahooProvider V1 via provider abstraction.

## How to use this package

Use this merged file as the canonical project knowledge source for AI-assisted coding. For each module, provide a short task prompt that points to the relevant sections instead of pasting the entire specification into the chat.

Use module-specific docs:
- Architecture docs for context
- Relevant schema docs
- Relevant formula/config docs
- Relevant module spec

## Folder structure

- `SCHEMA/` — DuckDB schemas, enums, views, indexes
- `CONFIG/` — base/strategy configs and config reference
- `FORMULAS/` — feature, scoring, outcome formulas
- `MODULES/` — module-by-module implementation specs
- `PIPELINE/` — orchestrator, interfaces, error handling, calendar
- `SIMULATION/` — simulation and walk-forward specs
- `TESTING/` — tests, golden dataset, debug presets
- `UI/` — Streamlit tabs and export specs
- `PROMPTS/` — omitted from this merged source to avoid stale prompt conflicts

## Build order

1. Project skeleton
2. DuckDB manager
3. Schema manager
4. Provider interface
5. YahooProvider
6. Universe snapshot engine
7. Benchmark / sector ETF loader
8. Daily price ingestion
9. Data validator
10. Mutation detector
11. Feature engine
12. Market regime engine
13. Step 3 screening
14. Step 4 setup analysis
15. Step 5 proposal engine
16. Outcome queue
17. Simulation engine
18. Export package engine
19. AI review engine
20. Pipeline orchestrator
21. Streamlit dashboard
22. Debug mode

---

## FILE: `01_Project_Scope_And_Constraints.md`

# Project Scope and Constraints

## Objective
Build a local US daily swing trading research system that:
- downloads daily EOD data;
- calculates reusable features;
- screens, analyzes and ranks stock candidates;
- proposes Top N stock lists per strategy;
- tracks forward outcomes;
- supports walk-forward simulation;
- supports AI review without contaminating mechanical performance attribution.

## Primary KPI
Expectancy > hit rate.

## Strategies
- aggressive
- normal
- conservative

## Platform
- Windows local PC
- Python
- DuckDB
- Polars-first processing
- Streamlit local dashboard

## Research-grade V1 limitations
Accepted:
- YahooProvider is V1 source, no SLA.
- Monthly universe snapshots reduce but do not eliminate survivorship bias.
- No intraday execution.
- No broker integration.
- No auto-trading.
- Streamlit is local single-user UI.
- Historical depth may be limited; warm-up and single-regime caveats must be displayed.

## Non-negotiable guardrails
- No look-ahead bias.
- No raw/adjusted mixing.
- No production/debug/simulation contamination.
- No config overwrites.
- No AI attribution contamination.
- No direct provider calls outside provider layer.

---

## FILE: `CONFIG/23_Config_Reference_Guide.md`

# Config Reference Guide

## universe
Controls tradability filters.

## features
Controls feature lookbacks and schema version.

## screening
Controls Step3 hard/soft pass thresholds.

## scoring_weights
Must sum to 1.0.

## market_regime
VIX thresholds used by Market Regime Engine.

## diversification
Hard cap enabled by default.
If hard cap rejects a candidate, soft penalty is not applied.

## sector_etf_mapping
Static mapping for sector relative strength.

## simulation
Entry, returns, slippage, horizons, and fold acceptance thresholds.

## macro_event_risk
Event types and penalty.

## earnings
Earnings avoidance / penalty rules.

---

## FILE: `SCHEMA/12_Enum_Values_Reference.md`

# Enum Values Reference

## data_quality_status
- ok
- warning
- suspect
- failed
- quarantined

## outcome_status
- pending
- complete
- partial
- unresolvable
- failed

## queue status
- pending
- processing
- done
- failed
- unresolvable

## market_regime
- bull
- neutral
- bear
- high_risk
- extreme_risk

## symbol_type
- stock
- etf
- benchmark
- index

## setup_type
- trend_pullback
- breakout
- volatility_squeeze
- trend_resume
- high_tight_flag
- unknown

## decision_source
- mechanical_only
- human_only
- ai_assisted

## human_action
- ignored
- accepted
- overrode
- deferred

## session
- pre_market
- post_market
- during_market
- unknown

## confidence
- high
- medium
- low

## review_type
- ticker_review
- simulation_review
- config_review

## execution action
- watch
- paper_trade
- skip
- real_trade

## run_status
- pending
- running
- success
- success_with_warnings
- failed
- cancelled


## list_membership
Used in `sim_signal_outcomes`.
- raw_only
- diversified_only
- both

## list_type
Used in `sim_config_comparisons`.
- raw
- diversified

## selected column semantics
V1 meanings:
- `selected_flag = in_diversified_top_n`
- `selected_top_n = in_raw_top_n OR in_diversified_top_n`

Preferred new fields:
- `in_raw_top_n`
- `in_diversified_top_n`

---
