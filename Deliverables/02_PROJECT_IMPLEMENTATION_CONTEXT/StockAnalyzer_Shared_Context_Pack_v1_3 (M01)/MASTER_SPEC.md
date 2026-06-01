# MASTER_SPEC.md — Swing Trading Stock Analyzer

Status: shared source of truth for ChatGPT + Claude coding work.

Last updated: 2026-05-28

## 1. Product Goal

Build a local Windows-based US daily swing trading research platform.

The system must:
- download and validate daily EOD market data;
- store data in DuckDB;
- calculate reusable daily features with Polars;
- screen stocks through Step 3;
- analyze setups through Step 4;
- rank proposals through Step 5;
- generate Top 20 candidates;
- track 5/10/20/40 business-day outcomes;
- support simulation / walk-forward testing;
- compare raw Top 20 vs diversified Top 20;
- support AI review/export;
- provide Streamlit local dashboard.

Primary KPI:
expectancy > hit rate.

System type:
research-grade V1, not institutional-grade infrastructure.

## 2. Core Stack

- Python 3.11+
- DuckDB local files
- Polars-first processing
- Streamlit local dashboard
- YahooProvider V1 through provider abstraction
- pandas-market-calendars for US trading days
- keyring / Windows Credential Manager for API keys
- pytest for testing

## 3. Database Files

Use separate DuckDB files:

- `prod.duckdb`
- `debug.duckdb`
- `simulation.duckdb`

Simulation may ATTACH prod.duckdb read-only.

Debug data must not contaminate production.

## 4. Core Pipeline

Daily pipeline order:

1. Acquire pipeline lock
2. Check already-run state
3. Load benchmarks and sector ETFs
4. Refresh ticker universe if due
5. Download daily stock data
6. Validate data
7. Detect splits / mutations
8. Repair missing data
9. Calculate daily features
10. Step 3 screening
11. Step 4 setup analysis
12. Step 5 proposal engine
13. Create outcome queue
14. Process due outcomes
15. Refresh dashboard/materialized views
16. Export/log/backup
17. Release lock

## 5. Market Data Rules

V1 provider:
YahooProvider via provider interface.

Do not call Yahoo directly outside provider layer.

Required benchmark symbols:
- SPY
- QQQ
- ^VIX
- XLK, XLF, XLV, XLY, XLP, XLC, XLI, XLE, XLB, XLU, XLRE

VIX handling:
- Yahoo symbol: `^VIX`
- symbol_type = `index`
- close_raw = close_adj = VIX close
- volume may be NULL or 0
- excluded from screening

Sector ETFs:
- symbol_type = `benchmark` or `etf`
- loaded before feature engine
- excluded from screening

## 6. Symbol Types

Allowed:
- stock
- etf
- benchmark
- index

Only `stock` enters screening.

## 7. Raw vs Adjusted Price Rules

Raw prices:
- execution realism
- entry price
- stop/target
- gaps
- UI audit

Adjusted prices:
- indicators
- trend
- RSI
- ATR
- 52-week metrics
- relative strength
- outcome performance returns

`volume_raw` is used for V1 volume features.

`volume_adj` is reserved / unused in V1.

## 8. Feature Cutoff Rule

All features must be point-in-time safe.

Every feature row has:
- `feature_date`
- `feature_cutoff_date`
- `feature_schema_version`

No row with `date > feature_cutoff_date` may be used in feature calculations.

Feature schema versions must use zero-padded names:
- `features_v01`
- `features_v02`
- ...
- `features_v10`

## 9. Final Feature Formulas

All rolling windows anchor on `feature_cutoff_date`.

### EMA
EMA20 / EMA50 / EMA200 calculated on `close_adj`.

### EMA Alignment Score
- 100 if EMA20 > EMA50 > EMA200
- 50 if close_adj > EMA200 but full alignment false
- 0 otherwise

### RSI14
Wilder RSI14 using adjusted close.

### ROC20
`roc20 = close_adj_t / close_adj_{t-20} - 1`

### ATR14
Wilder ATR14 using adjusted OHLC.

### ATR%
`atr_pct = atr14 / close_adj_t`

### RVOL20
`rvol20 = volume_raw_t / mean(volume_raw over t-20 to t-1)`

Denominator excludes current day.

### Avg Dollar Volume 20d
`avg_dollar_volume_20d = mean(close_raw * volume_raw over prior 20 trading days)`

### 52W Distance
`distance_from_52w_high_pct = close_adj_t / max(close_adj over 252 trading days ending at t) - 1`

### Pullback From Recent High
`pullback_from_recent_high_pct = close_adj_t / max(close_adj over 20 trading days ending at t) - 1`

### Breakout Proximity
`breakout_proximity_t = (close_adj_t - rolling_20d_high_t) / atr14_t`

where:

`rolling_20d_high_t = max(close_adj over 20 trading days ending on and including feature_cutoff_date)`

### Consolidation Score

`atr_contraction = 1 - min(ATR14_current / mean(ATR14 over prior 60 trading days), 1)`

`range_contraction = 1 - min(mean(high_adj - low_adj over prior 10 trading days) / mean(high_adj - low_adj over prior 60 trading days), 1)`

`volume_contraction = 1 - min(mean(volume_raw over prior 10 trading days) / mean(volume_raw over prior 60 trading days), 1)`

`consolidation_score = 100 * (0.4 * atr_contraction + 0.4 * range_contraction + 0.2 * volume_contraction)`

Clip 0–100.

### Sector Relative Strength

`sector_relative_strength = ticker_20d_return_adj - sector_etf_20d_return_adj`

If sector is missing/unmapped:
- value = NULL
- no sector RS boost

## 10. Sector ETF Mapping

| Sector | ETF |
|---|---|
| Technology | XLK |
| Financials | XLF |
| Healthcare | XLV |
| Consumer Discretionary | XLY |
| Consumer Staples | XLP |
| Communication Services | XLC |
| Industrials | XLI |
| Energy | XLE |
| Materials | XLB |
| Utilities | XLU |
| Real Estate | XLRE |

## 11. Market Regime

Inputs:
- SPY vs EMA200
- QQQ vs EMA200
- VIX close

Priority:
1. extreme_risk (VIX >= 30)
2. high_risk (VIX >= 25)
3. bear (SPY < EMA200 AND QQQ < EMA200)
4. bull (SPY > EMA200 AND QQQ > EMA200 AND VIX < 25)
5. neutral (everything else)

Rules:
- extreme_risk: VIX >= 30
- high_risk: VIX >= 25 and VIX < 30
- bear: SPY < EMA200 AND QQQ < EMA200, unless extreme_risk/high_risk already applies
- bull: SPY > EMA200 AND QQQ > EMA200 AND VIX < 25
- neutral: everything else

## 12. Step 3 Screening

Hard filters:
- feature_ready = TRUE
- symbol_type = stock
- close_raw >= min_price
- avg_dollar_volume_20d >= min_avg_dollar_volume_20d
- rvol20 >= min_rvol
- data_quality_status = ok

Final score:

`screening_score = 0.30*trend + 0.25*momentum + 0.20*setup + 0.15*volume + 0.10*market`

Sub-scores must be normalized 0–100.

Default block weights:
- trend: 0.30
- momentum: 0.25
- setup: 0.20
- volume: 0.15
- market: 0.10

## 13. Step 4 Setup Analysis

Setup types:
- trend_pullback
- breakout
- volatility_squeeze
- trend_resume
- high_tight_flag
- unknown

### trend_resume detection rule

A setup is classified as `trend_resume` when:
- close_adj was below EMA20 for at least 3 of the prior 10 trading days;
- close_adj is now above EMA20;
- pullback_from_recent_high_pct is between -20% and -3%.

Step 4 happens after signal-date close. Next-day open is not known.

Use:

`entry_proxy_raw = close_raw on signal_date`

Stop formula:

`stop_price_raw = min(recent_20d_low_raw, entry_proxy_raw - 1.5 * atr14_raw_equivalent)`

Target formula:

`target_price_raw = entry_proxy_raw + target_R * (entry_proxy_raw - stop_price_raw)`

Estimated RR:

`estimated_rr = (target_price_raw - entry_proxy_raw) / (entry_proxy_raw - stop_price_raw)`

If actual next-day open differs from entry_proxy_raw by more than 5%, log warning but do not recompute signal.

Default target_R:
- aggressive: 1.8
- normal: 2.2
- conservative: 2.8

## 14. Step 5 Proposal Engine

Step 5 must always calculate both:
- raw ranking
- diversified ranking

### Raw ranking

Sort:
1. proposal_score_raw DESC
2. estimated_rr DESC
3. ticker ASC

Assign:
`raw_rank`

Mark:
`in_raw_top_n = TRUE` if raw_rank <= top_n.

### Diversified ranking

If hard_cap_enabled = TRUE:
- candidates exceeding sector/industry cap are rejected from diversified list
- rejected candidates keep raw_rank
- rejected candidates have diversified_rank = NULL
- rejection_reason stores cap reason
- no soft penalty in V1 hard-cap mode

If hard_cap_enabled = FALSE:
- no hard rejection
- soft penalties apply

Legacy fields:
- `selected_flag = in_diversified_top_n`
- `selected_top_n = in_raw_top_n OR in_diversified_top_n`

## 15. Diversification Defaults

- hard_cap_enabled = TRUE
- sector_max_positions = 3
- industry_max_positions = 2
- sector_penalty_factor = 0.90
- industry_penalty_factor = 0.85

## 16. Outcome Tracking

Outcome horizons:
- 5bd
- 10bd
- 20bd
- 40bd

Use US trading business days.

Outcome queue must track proposals where:

`in_raw_top_n = TRUE OR in_diversified_top_n = TRUE`

Entry:
- `entry_price_raw = next trading day open_raw`
- `entry_price_sim = open_raw * (1 + slippage_bps / 10000)`

Performance calculations use `entry_price_sim`.

Return formula:

`return_Nbd_pct = close_adj_Nbd / entry_price_sim - 1`

MFE:

`mfe_40bd_pct = max(high_adj over entry_date to 40bd eval date) / entry_price_sim - 1`

MAE:

`mae_40bd_pct = min(low_adj over entry_date to 40bd eval date) / entry_price_sim - 1`

Missing eval candle:
1. add repair queue item
2. retry up to 3 business days
3. if unresolved, mark UNRESOLVABLE
4. return = NULL
5. exclude from aggregate metrics
6. log warning

## 17. Simulation

Use `simulation.duckdb`.

Simulation may:

`ATTACH prod.duckdb READ_ONLY`

Simulation writes only to `simulation.duckdb`.

Simulation must support:
- raw_top20
- diversified_top20

`sim_signal_outcomes.list_membership` values:
- raw_only
- diversified_only
- both

`sim_config_comparisons.list_type` values:
- raw
- diversified

## 18. UI Rules

Daily Proposals tab:

Checkbox:
`Show diversified shortlist`

Default:
checked TRUE.

If checked:
- show `in_diversified_top_n = TRUE`
- sort by diversified_rank

If unchecked:
- show `in_raw_top_n = TRUE`
- sort by raw_rank

Table columns:
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

Highlight rows where:

`in_raw_top_n != in_diversified_top_n`

## 19. AI Review

AI review is qualitative overlay only.

It must not replace the mechanical screener.

Store attribution:
- mechanical_only
- human_only
- ai_assisted

AI review output must not contaminate mechanical outcome tracking.

## 20. Config Defaults

Normal:
- min_price = 10
- min_avg_dollar_volume_20d = 20,000,000
- min_rvol = 1.5
- min_screening_score = 65
- sector_max_positions = 3
- industry_max_positions = 2
- earnings avoid window = 10bd

Aggressive:
- min_price = 5
- min_avg_dollar_volume_20d = 5,000,000
- min_rvol = 1.2
- min_screening_score = 55
- sector_max_positions = 5
- industry_max_positions = 3
- earnings avoid window = 3bd

Conservative:
- min_price = 15
- min_avg_dollar_volume_20d = 50,000,000
- min_rvol = 1.8
- min_screening_score = 75
- sector_max_positions = 2
- industry_max_positions = 1
- earnings avoid window = 15bd

## 21. Accepted V1 Limitations

- YahooProvider has no SLA.
- Historical delisted data is incomplete.
- Monthly universe snapshots are survivorship-bias mitigation, not perfect solution.
- Macro calendar may be manually maintained CSV.
- Earnings source may be LOW confidence in V1.
- No intraday fill modeling.
- No broker integration.
- No auto-trading.
- No cloud deployment.
