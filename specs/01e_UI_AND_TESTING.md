# 01e_UI_AND_TESTING

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.
Rewritten 2026-06-19 for the **setup-mode migration** (AD-22.19–22.24).

UI primary dimensions are setup type and risk label (not strategy profile).
Golden dataset and presets reference setup configs.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

## FILE: `TESTING/90_Test_Plan.md`

# Test Plan

## Unit tests
- schema creation idempotent (setup-mode schema)
- RVOL / RSI / ATR / EMA alignment / ATR compression formulas
- structural-level features (support<close<resistance where applicable)
- market regime priority
- universal eligibility (RVOL is NOT a gate)
- setup routing (multi-route + no-route)
- per-setup validation: breakout / pullback / trend_continuation / consolidation_base
- RVOL rule per setup (pullback never hard-rejects on low RVOL)
- setup-aware stop/target (stop<entry<target; RR is an output)
- risk labeling (monotonic in risk_score; thresholds)
- disposition gates (BUY / WATCHLIST_ONLY / REJECTED)
- diversity hard cap (no double penalty)
- outcome queue missing-day behavior; stop_hit/target_hit

## Integration tests
- 20-ticker debug pipeline (setup mode)
- benchmark ingestion before features
- Step3 (eligibility+routing) -> Step4 (validation) -> Step5 (proposals)
- multi-route ticker dedupe at proposal stage
- outcome queue
- simulation attach read-only; outcomes grouped by setup_type/risk_label

## Regression tests
Golden dataset must produce identical outputs with same DB/config.

---

## FILE: `TESTING/91_Golden_Dataset_Spec.md`

# Golden Dataset Spec

Default:
- tickers: AAPL, MSFT, NVDA, JPM, XOM, XLK, XLF, SPY, QQQ, ^VIX
- range: 2024-01-01 to 2024-06-30
- setup configs: all four active (setup_breakout_v1, setup_pullback_v1,
  setup_trend_continuation_v1, setup_consolidation_base_v1) + risk_label_config_v1
- feature schema: features_v02

Expected outputs are stored after first validated run. Future changes compare
against these unless feature schema version changes.

---

## FILE: `TESTING/92_Debug_Mode_Presets.md`

# Debug Mode Presets

## Fast Smoke Test
20 tickers, 5 trading days, full setup-mode pipeline.

## Indicator Validation
10 tickers, 90 trading days, features only.

## Pipeline Sanity
100 tickers, 30 trading days, eligibility → Step5.

## Config Tuning Test
500 tickers, 6 months, Step3–Step5 across all four setup configs.

---

## FILE: `UI/95_Dashboard_Tab_Specs.md`

# Dashboard Tab Specs

## Daily Proposals
Columns:
rank, ticker, setup_type, setup_score, risk_label, entry, stop, target,
estimated_rr, support, resistance, next_resistance, sector, industry,
earnings status, market_regime, disposition, explanation.

Primary filters: setup type (breakout / pullback / trend_continuation /
consolidation_base) and risk label (low / medium / high).

## Signal Explorer
Filters: date range, ticker, setup_type, risk_label, score range, disposition.

## Setup Performance
Metrics per setup type and per risk label: expectancy, win rate, avg win,
avg loss, profit factor, stop-hit rate, target-hit rate, MFE/MAE, drawdown.
Plus false-breakout rate, pullback-failure rate, consolidation-breakout success.

## Outcome Tracking
Show 5/10/20/40bd returns, MFE, MAE, stop_hit/target_hit, unresolved outcomes.

## Config Manager
View, clone, edit, activate, compare setup configs and risk-label config.

## Pipeline Health
pipeline_runs, repair_queue, provider warnings, failed tickers, setup-mode
funnel diagnostics.

## Simulation Lab
date range, mode, setup-config selection, run simulation, compare results by
setup type / risk label.

## AI Review
select tickers or simulation, download package, copy setup-specific prompt.

## Debug Mode
sample count, watchlist, preset, step selection, run debug.

## Daily Proposals diversified checkbox
Add checkbox: `Show diversified shortlist`. Default checked = TRUE. Persist in
`st.session_state["show_diversified"]`.

If checked: proposals where `in_diversified_top_n = TRUE`, ordered by
`diversified_rank ASC`. If unchecked: proposals where `in_raw_top_n = TRUE`,
ordered by `raw_rank ASC`.

Recommended table columns:
- Raw Rank
- Div Rank
- Ticker
- Setup Type
- Risk Label
- Setup Score
- Raw Score
- Final Score
- Est. RR
- Sector
- Industry
- Div. Reason
- Disposition
- Explanation

Highlight rows where `in_raw_top_n != in_diversified_top_n`.

---

## FILE: `UI/96_Export_Package_Specs.md`

# Export Package Specs

## Ticker review ZIP
Files:
- metadata.json (includes setup_type, risk_label, disposition)
- prices.csv
- features.csv
- step3.csv (eligibility + routing)
- step4.csv (setup validation + trade plan)
- step5.csv (risk label + disposition + ranking)
- explanation.txt (setup-specific checklist)

## Simulation review ZIP
Files:
- configs.json (setup configs + risk-label config)
- performance_metrics.csv (by setup_type and risk_label)
- score_buckets.csv
- setup_performance.csv
- regime_performance.csv
- drawdowns.csv
- unresolved_outcomes.csv
