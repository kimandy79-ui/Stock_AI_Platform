# 01e_UI_AND_TESTING

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

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
