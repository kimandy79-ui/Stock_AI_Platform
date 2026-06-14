# 01c_FORMULAS_AND_CONFIGS

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

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
