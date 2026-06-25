# 01a_CORE_PRINCIPLES

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.
Rewritten 2026-06-19 for the **setup-mode migration** (AD-22.19–22.24). The
active selection architecture is setup-based; the legacy 3-strategy mode
(aggressive / normal / conservative) is retired and appears only as deprecated
terminology.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

# 01. MASTER ARCHITECTURE - SOURCE OF TRUTH — MERGED

Status: canonical merged implementation specification for Claude Project Files / AI-assisted coding.
Generated: 2026-05-29. Setup-mode rewrite: 2026-06-19.

## Absolute precedence rule

This file already merges the source packages in this order:

1. `SwingTradingSystem_Master_TZ_MINI_PATCH_2` — highest authority.
2. `SwingTradingSystem_Master_TZ_PATCH_1` — overrides FULL unless MINI PATCH 2 says otherwise.
3. `SwingTradingSystem_Master_TZ_v1_FULL` — baseline only where not overridden.

The **setup-mode migration (AD-22.19)** is an owner-approved architecture change
that sits above all of the above for any topic it touches (selection unit,
configs, RVOL gating, setup taxonomy, stop/target, risk labeling, schema keys).
Where older merged text conflicts with setup mode, setup mode wins.

Therefore:

- MINI PATCH 2 precedes PATCH 1 and FULL.
- PATCH 1 precedes FULL.
- Setup-mode decisions (AD-22.19–22.24) precede all merged baseline text on the
  topics they cover.
- Do not implement an older FULL rule if it conflicts with PATCH 1, MINI PATCH 2,
  or setup mode.
- Do not re-apply PATCH 1 or MINI PATCH 2 as separate ALTER scripts when creating a fresh database.
- For the Schema Manager, create the final merged + setup-mode schema directly.
- Treat raw patch files, old review prompts, and old module prompts as archival only.

## What was deliberately removed to avoid confusing Claude

The following were omitted from this merged project file:

- old AI coding prompt templates from `PROMPTS/`;
- Claude review prompts;
- patch application checklists;
- manifests;
- standalone ALTER TABLE patch snippets as implementation instructions.

Reason: coding should use this merged final state, not old prompts or old base schema plus patches.

## Selection unit (setup mode — active)

The active selection unit is the **setup group**, not a strategy profile.

Active setup groups (the primary selection driver):

```text
breakout
pullback
trend_continuation
consolidation_base
```

The primary selection driver is `setup_config_id` plus `setup_type`, **not**
`strategy_config_id`. In setup mode the single `setup_type` field carries one of
the four group values above. There is no separate legacy subtype field; the old
six-value setup vocabulary is retired.

Risk is an **output label** assigned after setup validation, never a selection
config:

```text
risk_label: low | medium | high
```

Disposition remains available as a tiering output:

```text
disposition: BUY | WATCHLIST_ONLY | REJECTED
```

A strategy × setup matrix (e.g. `aggressive_breakout`) is **explicitly
forbidden** (AD-22.22).

Deprecated legacy terms (retired by AD-22.19): `aggressive`, `normal`,
`conservative`, `strategy_config_id` as a primary key, and the legacy
six-value `setup_type` enum. These must not be used as the active selection
driver. They may appear only in historical notes.

## Critical merged decisions

### Feature schema version
Use zero-padded version strings:

```text
FEATURE_SCHEMA_VERSION = "features_v02"
```

Setup mode requires structural-level features (support / resistance /
next_resistance / base / ATR compression / EMA slopes / RS). These are added in
`features_v02`. `features_v01` is retained only for historical rows.

### Avg dollar volume

```text
avg_dollar_volume_20d = mean(close_raw * volume_raw over prior 20 trading days)
```

`volume_adj` is reserved and unused in V1 feature formulas.

### Universal eligibility vs setup gating (AD-22.23)
Universal eligibility filters applied before setup classification are limited to
general tradability/data conditions only:

```text
data ready, valid stock type, valid OHLCV history,
minimum valid price, minimum liquidity, no obvious data anomaly
```

RVOL, setup score, momentum, ATR%, EMA extension, and consolidation quality
**must not** be universal hard gates. RVOL is applied inside each setup
validator according to its own rule (breakout: hard/near-hard; pullback: soft
only; trend_continuation: moderate; consolidation_base: not required).

### Step4 entry proxy

```text
entry_proxy_raw = close_raw on signal_date
```

Use `entry_proxy_raw` for setup-stage stop, target, and estimated RR. Actual
next-day entry is recorded later by outcome tracking.

### Setup-aware stop / target (AD, Stage 8)
Fixed-R is no longer the primary target mechanism. Stop and target are derived
from setup structure; `estimated_rr` is an OUTPUT of (entry, stop, target), not
a fixed constant. Fixed-R is retained only as an explicit fallback when no
structural target exists. Per-setup rules live in `01c`.

### Outcome entry prices

```text
entry_price_raw = next trading day open_raw
entry_price_sim = open_raw * (1 + slippage_bps / 10000)
```

Use `entry_price_sim` for return, MFE, MAE, and realized R-multiple
calculations. Keep `entry_price_raw` for audit/execution reference.

### Raw vs diversified ranking
Always calculate both raw and diversified rankings.

- `raw_rank`: ranking before diversification.
- `diversified_rank`: ranking after diversification logic.
- `in_raw_top_n`: raw top-N membership.
- `in_diversified_top_n`: diversified top-N membership.
- `selected_flag = in_diversified_top_n`.
- `selected_top_n = in_raw_top_n OR in_diversified_top_n`.

### Hard cap vs soft penalty
If `hard_cap_enabled = TRUE`, over-cap candidates are rejected from the
diversified list and do not receive a soft penalty.

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
- final decision to use DuckDB, Polars-first, Streamlit local dashboard, YahooProvider V1 via provider abstraction;
- setup-mode migration (AD-22.19), which retires the 3-strategy selection model.

## How to use this package

Use this merged file as the canonical project knowledge source for AI-assisted coding. For each module, provide a short task prompt that points to the relevant sections instead of pasting the entire specification into the chat.

Use module-specific docs:
- Architecture docs for context
- Relevant schema docs
- Relevant formula/config docs
- Relevant module spec

## Folder structure

- `SCHEMA/` — DuckDB schemas, enums, views, indexes
- `CONFIG/` — setup configs, risk-label config, and config reference
- `FORMULAS/` — feature, scoring, setup-validation, outcome formulas
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
11. Feature engine (features_v02 for setup mode)
12. Market regime engine
13. Step 3 universal eligibility + setup routing
14. Step 4 setup-specific validation
15. Step 5 proposal engine (setup-aware stop/target, risk labeling)
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
- screens, classifies, validates and ranks stock candidates by setup;
- proposes Top N stock lists by setup type with a risk label;
- tracks forward outcomes;
- supports walk-forward simulation;
- supports AI review without contaminating mechanical performance attribution.

## Primary KPI
Expectancy > hit rate.

## Selection model
Setup-based (active):
- breakout
- pullback
- trend_continuation
- consolidation_base

Risk label (output): low / medium / high.

Deprecated legacy strategy profiles (retired, AD-22.19): aggressive, normal,
conservative.

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
- No universal RVOL/setup-score/momentum hard gate before setup classification
  (AD-22.23).

---

## FILE: `CONFIG/23_Config_Reference_Guide.md`

# Config Reference Guide

## setup_config (active primary)
One config per setup group: `setup_breakout_v1`, `setup_pullback_v1`,
`setup_trend_continuation_v1`, `setup_consolidation_base_v1`. Each controls that
setup's validation thresholds, RVOL rule, stop/target structure preferences,
and scoring weights.

## risk_label_config
`risk_label_config_v1` controls the objective factors and thresholds that map a
validated trade plan to low / medium / high.

## universe
Controls universal tradability filters only (price, liquidity, symbol type,
data quality). No RVOL/score/ATR here.

## features
Controls feature lookbacks and schema version (`features_v02`).

## scoring_weights
Per-setup weighting of validation components. Must sum to 1.0 within each setup.

## market_regime
VIX thresholds used by Market Regime Engine.

## diversification
Hard cap enabled by default. If hard cap rejects a candidate, soft penalty is
not applied.

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

## setup_type  (active primary selection unit)
- breakout
- pullback
- trend_continuation
- consolidation_base

The `setup_type` field carries exactly one of the four values above. The legacy
six-value vocabulary (trend_pullback, volatility_squeeze, trend_resume,
high_tight_flag, momentum_extension, unknown) is retired. Candidates that match
no active setup are not written as a proposal `setup_type`; they are recorded
only via `setup_fail_reason` for diagnostics and never receive a BUY
disposition.

## risk_label  (output label)
- low
- medium
- high

## disposition
- BUY
- WATCHLIST_ONLY
- REJECTED

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
