# 02b_ARCHITECTURE_DECISIONS

Status: split active architecture decisions file for Claude Project Files.
Generated from `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §22 on 2026-05-31.

Use this file only when the task needs active architecture decision details.

---

# 22. Active Architecture Decisions

This section contains active decisions only.
Historical manifest/change-log data is intentionally excluded.

## 22.1 Use DuckDB only

Reason:
local analytical workload, simple deployment, excellent columnar SQL performance.

Impact:
- no PostgreSQL in V1;
- no server DB;
- local files: prod/debug/simulation.

## 22.2 Polars-first processing

Reason:
faster than pandas for large analytical transformations and window calculations.

Impact:
- feature engine uses Polars;
- pandas only when unavoidable for provider/library compatibility.

## 22.3 Research-grade V1, not institutional production-grade

Reason:
YahooProvider has no SLA and historical universe is imperfect.

Impact:
- honest simulation caveats;
- no overengineering;
- provider abstraction required.

## 22.4 Separate DB files

Use:
- `prod.duckdb`;
- `debug.duckdb`;
- `simulation.duckdb`.

Impact:
- simulation attaches prod read-only;
- debug never writes to prod.

## 22.5 YahooProvider V1 behind provider abstraction

Reason:
free, easy to start, broad data coverage.

Impact:
- MarketDataProvider interface required;
- future migration to Polygon/Tiingo possible.

## 22.6 Store raw and adjusted prices

Reason:
- raw prices needed for execution realism;
- adjusted prices needed for indicators and performance.

Impact:
- `daily_prices` has raw and adjusted OHLC;
- indicators use adjusted;
- stop/target uses raw;
- returns use adjusted close with simulated entry price.

## 22.7 Use feature_cutoff_date

Reason:
prevent look-ahead bias.

Impact:
- all feature calculations anchor on `feature_cutoff_date`;
- simulation queries must enforce cutoff.

## 22.8 Use zero-padded feature schema versions

Use:
- `features_v01`;
- `features_v02`;
- etc.

Reason:
avoid lexicographic MAX bug.

Impact:
- constants.py uses `FEATURE_SCHEMA_VERSION = "features_v01"`.

## 22.9 Use monthly universe snapshots

Reason:
reduce survivorship bias without institutional CRSP/Compustat data.

Impact:
- `ticker_universe_snapshot` table;
- simulation uses historical snapshot nearest and not after sim date.

Limitation:
residual delisted-price survivorship bias remains.

## 22.10 Market regime uses SPY, QQQ, VIX

Impact:
- benchmark loader must load SPY, QQQ, ^VIX;
- benchmarks excluded from screening.

## 22.11 Use raw Top 20 and diversified Top 20

Reason:
user wants to see true strongest candidates and how diversification changes the list.

Impact:
- Step 5 stores raw_rank and diversified_rank;
- UI checkbox controls display;
- outcome queue tracks raw OR diversified Top 20;
- simulation compares raw vs diversified performance;
- export includes both rankings.

## 22.12 Hard cap mode means no soft penalty in V1

Reason:
avoid double punishment and simplify interpretation.

Impact:
- if `hard_cap_enabled = TRUE`, over-cap candidates rejected;
- non-rejected candidates keep `proposal_score_final = proposal_score_raw`;
- if `hard_cap_enabled = FALSE`, soft penalty applies.

## 22.13 Outcome queue tracks raw OR diversified Top 20

Condition:

```text
in_raw_top_n OR in_diversified_top_n
```

Reason:
compare performance of raw strongest names vs diversified list.

## 22.14 Use entry_price_sim for performance

Reason:
slippage-adjusted simulated entry must affect returns.

Impact:
- `entry_price_raw` = audit reference;
- `entry_price_sim` = denominator for return/MFE/MAE/R-multiple.

## 22.15 Step 4 uses signal-date close as entry proxy

Reason:
next-day open is unknown at signal time.

Impact:
- `entry_proxy_raw = close_raw` on signal_date;
- stop/target/RR estimated from proxy;
- if actual open gap >5%, log warning but do not recompute signal.

## 22.16 Define trend_resume setup detection rule

A setup is classified as `trend_resume` when:
- close_adj was below EMA20 for at least 3 of the prior 10 trading days;
- close_adj is now above EMA20;
- pullback_from_recent_high_pct is between -20% and -3%.

Impact:
- Step 4 setup analysis;
- setup_type classification tests.

## 22.17 Module 03 must use merged final schema

Module 03 Schema Manager must use the merged final schema from:
- Master TZ v1 FULL;
- PATCH 1;
- MINI-PATCH 2.

Reason:
avoid creating outdated base tables and then immediately applying patch ALTER statements on a fresh database.

Impact:
- Schema Manager;
- database migrations;
- tests for schema creation;
- AI coding prompts for Module 03.

## 22.18 Structural domain constants live in constants.py

Values fixed by master spec identically across all strategy presets are structural domain constants, not tunable strategy thresholds.

This includes:
- VIX market regime boundaries 25/30;
- outcome horizons 5/10/20/40 bd;
- screening block weights 0.30/0.25/0.20/0.15/0.10;
- simulation vocabulary: list_membership / list_type;
- AI review attribution vocabulary.

Tunable thresholds are only those that vary between normal / aggressive / conservative presets.

Impact:
- Module 01 constants.py accepted as in-scope;
- future modules consume these constants instead of redefining values.

---
