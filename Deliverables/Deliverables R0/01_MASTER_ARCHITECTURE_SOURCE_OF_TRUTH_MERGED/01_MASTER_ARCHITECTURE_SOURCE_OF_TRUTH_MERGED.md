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

## FILE: `CONFIG/20_Config_Base_Normal.json`

```json
{
  "strategy_name": "normal",
  "version": "normal_v1",
  "universe": {
    "min_price": 10,
    "min_avg_dollar_volume_20d": 20000000,
    "allowed_symbol_types": [
      "stock"
    ],
    "exclude_benchmarks": true
  },
  "features": {
    "feature_schema_version": "features_v01",
    "rsi_length": 14,
    "atr_length": 14,
    "ema_periods": [
      20,
      50,
      200
    ],
    "rvol_lookback": 20,
    "recent_high_lookback": 20,
    "high_52w_lookback": 252
  },
  "screening": {
    "min_rvol": 1.5,
    "min_screening_score": 65,
    "require_feature_ready": true
  },
  "scoring_weights": {
    "trend": 0.3,
    "momentum": 0.25,
    "setup": 0.2,
    "volume": 0.15,
    "market": 0.1
  },
  "market_regime": {
    "high_risk_vix": 25,
    "extreme_risk_vix": 30
  },
  "diversification": {
    "hard_cap_enabled": true,
    "sector_max_positions": 3,
    "industry_max_positions": 2,
    "sector_penalty_factor": 0.9,
    "industry_penalty_factor": 0.85,
    "penalty_applies_before_cap_only": true
  },
  "sector_etf_mapping": {
    "Technology": "XLK",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE"
  },
  "simulation": {
    "entry_rule": "next_trading_day_open_raw",
    "return_price_type": "adjusted_close",
    "slippage_bps": 10,
    "commission_per_trade": 0,
    "horizons_bd": [
      5,
      10,
      20,
      40
    ],
    "min_resolved_outcomes_pct": 0.85,
    "max_drawdown_constraint_pct": 25
  },
  "macro_event_risk": {
    "enabled": true,
    "event_types": [
      "FOMC",
      "CPI",
      "PPI",
      "NFP",
      "POWELL"
    ],
    "window_bd_before": 1,
    "window_bd_after": 1,
    "penalty_points": -10
  },
  "earnings": {
    "avoid_within_bd": 10,
    "penalty_points_max": -15
  }
}
```

---

## FILE: `CONFIG/21_Config_Aggressive.json`

```json
{
  "strategy_name": "aggressive",
  "version": "aggressive_v1",
  "universe": {
    "min_price": 5,
    "min_avg_dollar_volume_20d": 5000000,
    "allowed_symbol_types": [
      "stock"
    ],
    "exclude_benchmarks": true
  },
  "features": {
    "feature_schema_version": "features_v01",
    "rsi_length": 14,
    "atr_length": 14,
    "ema_periods": [
      20,
      50,
      200
    ],
    "rvol_lookback": 20,
    "recent_high_lookback": 20,
    "high_52w_lookback": 252
  },
  "screening": {
    "min_rvol": 1.2,
    "min_screening_score": 55,
    "require_feature_ready": true
  },
  "scoring_weights": {
    "trend": 0.3,
    "momentum": 0.25,
    "setup": 0.2,
    "volume": 0.15,
    "market": 0.1
  },
  "market_regime": {
    "high_risk_vix": 25,
    "extreme_risk_vix": 30
  },
  "diversification": {
    "hard_cap_enabled": true,
    "sector_max_positions": 5,
    "industry_max_positions": 3,
    "sector_penalty_factor": 0.9,
    "industry_penalty_factor": 0.85,
    "penalty_applies_before_cap_only": true
  },
  "sector_etf_mapping": {
    "Technology": "XLK",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE"
  },
  "simulation": {
    "entry_rule": "next_trading_day_open_raw",
    "return_price_type": "adjusted_close",
    "slippage_bps": 10,
    "commission_per_trade": 0,
    "horizons_bd": [
      5,
      10,
      20,
      40
    ],
    "min_resolved_outcomes_pct": 0.85,
    "max_drawdown_constraint_pct": 25
  },
  "macro_event_risk": {
    "enabled": true,
    "event_types": [
      "FOMC",
      "CPI",
      "PPI",
      "NFP",
      "POWELL"
    ],
    "window_bd_before": 1,
    "window_bd_after": 1,
    "penalty_points": -10
  },
  "earnings": {
    "avoid_within_bd": 3,
    "penalty_points_max": -15
  }
}
```

---

## FILE: `CONFIG/22_Config_Conservative.json`

```json
{
  "strategy_name": "conservative",
  "version": "conservative_v1",
  "universe": {
    "min_price": 15,
    "min_avg_dollar_volume_20d": 50000000,
    "allowed_symbol_types": [
      "stock"
    ],
    "exclude_benchmarks": true
  },
  "features": {
    "feature_schema_version": "features_v01",
    "rsi_length": 14,
    "atr_length": 14,
    "ema_periods": [
      20,
      50,
      200
    ],
    "rvol_lookback": 20,
    "recent_high_lookback": 20,
    "high_52w_lookback": 252
  },
  "screening": {
    "min_rvol": 1.8,
    "min_screening_score": 75,
    "require_feature_ready": true
  },
  "scoring_weights": {
    "trend": 0.3,
    "momentum": 0.25,
    "setup": 0.2,
    "volume": 0.15,
    "market": 0.1
  },
  "market_regime": {
    "high_risk_vix": 25,
    "extreme_risk_vix": 30
  },
  "diversification": {
    "hard_cap_enabled": true,
    "sector_max_positions": 2,
    "industry_max_positions": 1,
    "sector_penalty_factor": 0.9,
    "industry_penalty_factor": 0.85,
    "penalty_applies_before_cap_only": true
  },
  "sector_etf_mapping": {
    "Technology": "XLK",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE"
  },
  "simulation": {
    "entry_rule": "next_trading_day_open_raw",
    "return_price_type": "adjusted_close",
    "slippage_bps": 10,
    "commission_per_trade": 0,
    "horizons_bd": [
      5,
      10,
      20,
      40
    ],
    "min_resolved_outcomes_pct": 0.85,
    "max_drawdown_constraint_pct": 25
  },
  "macro_event_risk": {
    "enabled": true,
    "event_types": [
      "FOMC",
      "CPI",
      "PPI",
      "NFP",
      "POWELL"
    ],
    "window_bd_before": 1,
    "window_bd_after": 1,
    "penalty_points": -10
  },
  "earnings": {
    "avoid_within_bd": 15,
    "penalty_points_max": -15
  }
}
```

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

## FILE: `FORMULAS/60_Feature_Formulas_Complete.md`

# Feature Formulas Complete

All formulas use rows with date <= feature_cutoff_date.

## EMA
EMA20/50/200 on close_adj.

## EMA Alignment Score
- 100 if EMA20 > EMA50 > EMA200
- 50 if close_adj > EMA200 but full alignment is false
- 0 otherwise

## RSI14
Wilder RSI14 on close_adj.

## ROC20
`roc20 = close_adj_t / close_adj_{t-20} - 1`

## ATR14
Wilder ATR using adjusted OHLC.

## ATR%
`atr_pct = atr14 / close_adj_t`

## RVOL20
`rvol20 = volume_raw_t / mean(volume_raw over t-20 to t-1)`

## Avg volume 20d
Mean of volume_raw over prior 20 trading days.

## Avg dollar volume 20d
`avg_dollar_volume_20d = mean(close_raw * volume_raw over prior 20 trading days)`

Liquidity must reflect actual traded dollar value, not split-adjusted historical price.

## volume_adj V1 rule
`volume_adj` is reserved and unused in V1 feature formulas. All V1 volume features use `volume_raw`.

## Distance from 52W high
`close_adj_t / max(close_adj over 252 trading days ending at t) - 1`

## Pullback from recent high
`close_adj_t / max(close_adj over 20 trading days ending at t) - 1`

## Breakout proximity
`(close_adj_t - rolling_20d_high_t) / atr14_t`

`rolling_20d_high_t = max(close_adj over 20 trading days ending at feature_cutoff_date)`

## Consolidation score
`atr_contraction = 1 - min(ATR14_current / mean(ATR14 over prior 60 trading days), 1)`

`range_contraction = 1 - min(mean(high_adj - low_adj over prior 10 trading days) / mean(high_adj - low_adj over prior 60 trading days), 1)`

`volume_contraction = 1 - min(mean(volume_raw over prior 10 trading days) / mean(volume_raw over prior 60 trading days), 1)`

`consolidation_score = 100 * (0.4*atr_contraction + 0.4*range_contraction + 0.2*volume_contraction)`

Clip to 0-100.

## Sector relative strength
`ticker_20d_return_adj - sector_etf_20d_return_adj`

If sector is unmapped, value NULL and no boost applied.

---

## FILE: `FORMULAS/61_Scoring_Formulas_Step3.md`

# Step3 Screening Scoring Formulas

## Hard filters
Fail if:
- feature_ready != TRUE
- symbol_type != stock
- close_raw < min_price
- avg_dollar_volume_20d < min_avg_dollar_volume_20d
- rvol20 < min_rvol
- data_quality_status != ok

## Normalization helpers
Clamp all sub-scores to 0-100.

## Trend score
Inputs:
- ema_alignment_score: 50%
- distance_to_ema50_pct: 25%
- close above EMA200: 25%

Rules:
- ema_alignment_score already 0/50/100.
- distance_to_ema50 ideal range: -3% to +8%.
  - score 100 if within range.
  - score declines linearly outside range.
- close above EMA200: 100 if true else 0.

## Momentum score
Inputs:
- RSI14: 40%
- ROC20: 30%
- sector_relative_strength: 30%

RSI score:
- 100 if 50 <= RSI <= 65
- 70 if 45 <= RSI < 50 or 65 < RSI <= 70
- 30 otherwise

ROC20 score:
- 100 if ROC20 > 0.08
- 70 if 0.03 <= ROC20 <= 0.08
- 30 if 0 <= ROC20 < 0.03
- 0 if ROC20 < 0

Sector RS score:
- 100 if > 0.05
- 70 if 0 to 0.05
- 30 if -0.05 to 0
- 0 if < -0.05
- neutral 50 if NULL

## Setup score
Inputs:
- consolidation_score: 40%
- breakout_proximity: 30%
- pullback_from_recent_high_pct: 30%

Breakout proximity score:
- 100 if -1 <= breakout_proximity <= 0.5
- 70 if -2 <= breakout_proximity < -1
- 30 if breakout_proximity < -2
- 20 if breakout_proximity > 1.5

Pullback score:
- 100 if -0.12 <= pullback <= -0.03
- 70 if -0.20 <= pullback < -0.12
- 30 otherwise

## Volume score
Inputs:
- rvol20: 60%
- avg_dollar_volume_20d: 40%

RVOL score:
- 100 if rvol20 >= 2.0
- 70 if 1.5 <= rvol20 < 2.0
- 40 if 1.2 <= rvol20 < 1.5
- 0 if below 1.2

## Market score
- bull: 100
- neutral: 60
- bear: 20
- high_risk: 0
- extreme_risk: 0

## Final screening score
`screening_score = 0.30*trend + 0.25*momentum + 0.20*setup + 0.15*volume + 0.10*market`

---

## FILE: `FORMULAS/62_Scoring_Formulas_Step4.md`

# Step4 Setup Analysis Formulas

## setup_type enum
- trend_pullback
- breakout
- volatility_squeeze
- trend_resume
- high_tight_flag
- unknown

## Setup type rules
trend_pullback:
- close_adj > EMA200
- pullback_from_recent_high_pct between -12% and -3%
- EMA20 > EMA50

breakout:
- breakout_proximity between -0.5 and 0.5
- rvol20 >= strategy min_rvol

volatility_squeeze:
- consolidation_score >= 70
- ATR contraction positive

trend_resume:
- close_adj crosses back above EMA20 after pullback

high_tight_flag:
- strong ROC20 > 15%
- consolidation_score >= 60

## Step4 entry proxy
Step4 runs after market close on `signal_date`; it cannot know the next trading day's open.

`entry_proxy_raw = close_raw on signal_date`

This proxy is used only for Step4 stop/target/estimated-RR calculations. The actual next-day entry is recorded later by outcome tracking.

## Stop price
`stop_price_raw = min(recent_20d_low_raw, entry_proxy_raw - 1.5 * atr14_raw_equivalent)`

If adjusted ATR only is available:
`atr14_raw_equivalent = atr14 * (close_raw / close_adj)`

## Target price
`target_price_raw = entry_proxy_raw + target_R * (entry_proxy_raw - stop_price_raw)`

Defaults:
- aggressive target_R = 1.8
- normal target_R = 2.2
- conservative target_R = 2.8

## Estimated RR
`estimated_rr = (target_price_raw - entry_proxy_raw) / (entry_proxy_raw - stop_price_raw)`

## Gap warning
When the actual next-day open becomes known:

`if abs(open_raw_next_day / entry_proxy_raw - 1) > 0.05: log warning`

Do not recompute the original mechanical Step4 signal.

## Step4 component score
setup_quality = average of breakout_quality, squeeze_score, timing_score, confirmation_score.

## Earnings penalty
If days_to_earnings_bd <= avoid_within_bd:
penalty = earnings_penalty_max * (1 - days_to_earnings_bd / avoid_within_bd)

Penalty is score points, negative number.

---

## FILE: `FORMULAS/63_Scoring_Formulas_Step5.md`

# Step5 Proposal Scoring

## Raw proposal score
`proposal_score_raw = 0.40*setup_score + 0.25*screening_score + 0.20*estimated_rr_score + 0.15*timing_score`

## RR score
- 100 if estimated_rr >= 3.0
- 80 if 2.2 <= estimated_rr < 3.0
- 60 if 1.8 <= estimated_rr < 2.2
- 0 if < 1.8

## Raw ranking
Always calculate raw ranking without diversification.

Sort candidates by:
1. `proposal_score_raw` DESC
2. `estimated_rr` DESC
3. `ticker` ASC

Assign `raw_rank`.

Set `in_raw_top_n = TRUE` when `raw_rank <= top_n`.

## Diversified ranking
Process candidates in `raw_rank` order.

### If `hard_cap_enabled = TRUE`
If candidate exceeds sector or industry cap:
- reject candidate from diversified list;
- keep `raw_rank`;
- set `diversified_rank = NULL`;
- set `rejection_reason = sector_cap` or `industry_cap`;
- do not apply soft penalty.

If candidate does not exceed cap:
- accept candidate into diversified ordering;
- assign next `diversified_rank`;
- set `proposal_score_final = proposal_score_raw`.

No soft penalty is applied in hard-cap mode in V1. No double punishment.

### If `hard_cap_enabled = FALSE`
No hard rejection.

Apply soft penalties:
`proposal_score_final = proposal_score_raw * sector_penalty * industry_penalty`

Then assign diversified ranks after score adjustment.

## Final selected semantics
- `selected_flag = in_diversified_top_n`
- `selected_top_n = in_raw_top_n OR in_diversified_top_n`

The default selected list is diversified. The raw list remains available for research/comparison.

---

## FILE: `FORMULAS/64_Outcome_Calculation_Rules.md`

# Outcome Calculation Rules

## Entry definitions
Entry date = next US trading day after `signal_date`.

`entry_price_raw = next trading day open_raw`

Used for:
- audit;
- execution reference.

`entry_price_sim = open_raw * (1 + slippage_bps / 10000)`

For long-only V1.

`entry_price_sim` is used for all return/MFE/MAE/R-multiple calculations.

## Horizon returns
Use adjusted close:

`return_Nbd_pct = close_adj_Nbd / entry_price_sim - 1`

Applies to:
- `return_5bd_pct`
- `return_10bd_pct`
- `return_20bd_pct`
- `return_40bd_pct`

## MFE
`mfe_40bd_pct = max(high_adj over entry_date to 40bd eval date) / entry_price_sim - 1`

## MAE
`mae_40bd_pct = min(low_adj over entry_date to 40bd eval date) / entry_price_sim - 1`

## Realized R multiple
`realized_r_multiple = (exit_price_sim_equivalent - entry_price_sim) / (entry_price_sim - stop_price_raw)`

## Missing eval candle
Repair first. If unresolved after 3 business days, mark UNRESOLVABLE and exclude from aggregate metrics.

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

## FILE: `TESTING/90_Test_Plan.md`

# Test Plan

## Unit tests
- schema creation idempotent
- RVOL formula
- RSI formula
- ATR formula
- EMA alignment
- market regime priority
- diversity hard cap
- stop/target formula
- outcome queue missing-day behavior

## Integration tests
- 20 ticker debug pipeline
- benchmark ingestion before features
- Step3 -> Step5
- outcome queue
- simulation attach read-only

## Regression tests
Golden dataset must produce identical outputs with same DB/config.

---

## FILE: `TESTING/91_Golden_Dataset_Spec.md`

# Golden Dataset Spec

Default:
- tickers: AAPL, MSFT, NVDA, JPM, XOM, XLK, XLF, SPY, QQQ, ^VIX
- range: 2024-01-01 to 2024-06-30
- strategy: normal_v1

Expected outputs are stored after first validated run.
Future changes compare against these unless feature schema version changes.

---

## FILE: `TESTING/92_Debug_Mode_Presets.md`

# Debug Mode Presets

## Fast Smoke Test
20 tickers, 5 trading days, full pipeline.

## Indicator Validation
10 tickers, 90 trading days, Step2 only.

## Pipeline Sanity
100 tickers, 30 trading days, Step1-Step5.

## Config Tuning Test
500 tickers, 6 months, Step3-Step5.

---

## FILE: `UI/95_Dashboard_Tab_Specs.md`

# Dashboard Tab Specs

## Daily Proposals
Columns:
rank, ticker, strategy, proposal_score_final, setup_type, estimated_rr, sector, industry, earnings risk, explanation.

## Signal Explorer
Filters:
date range, ticker, strategy, setup_type, score range.

## Strategy Performance
Metrics:
expectancy, win rate, avg win, avg loss, profit factor, drawdown.

## Outcome Tracking
Show 5/10/20/40bd returns, MFE, MAE, unresolved outcomes.

## Config Manager
View, clone, edit, activate, compare configs.

## Pipeline Health
pipeline_runs, repair_queue, provider warnings, failed tickers.

## Simulation Lab
date range, mode, config selection, run simulation, compare results.

## AI Review
select tickers or simulation, download package, copy prompt, send to AI.

## Debug Mode
sample count, watchlist, preset, step selection, run debug.


## Daily Proposals diversified checkbox
Add checkbox: `Show diversified shortlist`.

Default: checked = TRUE.

Persist in Streamlit session state:
`st.session_state["show_diversified"]`

If checked, display proposals where `in_diversified_top_n = TRUE`, ordered by `diversified_rank ASC`.

If unchecked, display proposals where `in_raw_top_n = TRUE`, ordered by `raw_rank ASC`.

Recommended table columns:
- Raw Rank
- Div Rank
- Ticker
- Strategy
- Setup Type
- Raw Score
- Final Score
- Est. RR
- Sector
- Industry
- Div. Reason
- Explanation

Highlight rows where `in_raw_top_n != in_diversified_top_n`.

---

## FILE: `UI/96_Export_Package_Specs.md`

# Export Package Specs

## Ticker review ZIP
Files:
- metadata.json
- prices.csv
- features.csv
- step3.csv
- step4.csv
- step5.csv
- explanation.txt

## Simulation review ZIP
Files:
- configs.json
- performance_metrics.csv
- score_buckets.csv
- setup_performance.csv
- regime_performance.csv
- drawdowns.csv
- unresolved_outcomes.csv
