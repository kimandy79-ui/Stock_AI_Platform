# DECISIONS_LOG.md

This file is the historical memory of the project.

Every major architecture/business decision must be logged here.

---

# 2026-05-28

## Decision: Use DuckDB only

Reason:
Local analytical workload, simple deployment, excellent columnar SQL performance.

Impact:
- no PostgreSQL in V1
- no server DB
- local files: prod/debug/simulation

---

# 2026-05-28

## Decision: Polars-first processing

Reason:
Faster than pandas for large analytical transformations and window calculations.

Impact:
- feature engine uses Polars
- pandas only when unavoidable for provider/library compatibility

---

# 2026-05-28

## Decision: Research-grade V1, not institutional production-grade

Reason:
YahooProvider has no SLA and historical universe is imperfect.

Impact:
- honest simulation caveats
- no overengineering
- provider abstraction required

---

# 2026-05-28

## Decision: Separate DB files

Decision:
Use:
- prod.duckdb
- debug.duckdb
- simulation.duckdb

Reason:
Avoid contamination between production, debug, and simulation.

Impact:
- simulation attaches prod read-only
- debug never writes to prod

---

# 2026-05-28

## Decision: YahooProvider V1 behind provider abstraction

Reason:
Free, easy to start, broad data coverage.

Impact:
- MarketDataProvider interface required
- future migration to Polygon/Tiingo possible

---

# 2026-05-28

## Decision: Store raw and adjusted prices

Reason:
Raw prices needed for execution realism.
Adjusted prices needed for indicators and performance.

Impact:
- daily_prices has raw and adjusted OHLC
- indicators use adjusted
- stop/target uses raw
- returns use adjusted close with simulated entry price

---

# 2026-05-28

## Decision: Use feature_cutoff_date

Reason:
Prevent look-ahead bias.

Impact:
- all feature calculations anchor on feature_cutoff_date
- simulation queries must enforce cutoff

---

# 2026-05-28

## Decision: Use zero-padded feature schema versions

Decision:
Use `features_v01`, `features_v02`, etc.

Reason:
Avoid lexicographic MAX bug.

Impact:
- constants.py uses FEATURE_SCHEMA_VERSION = "features_v01"

---

# 2026-05-28

## Decision: Use monthly universe snapshots

Reason:
Reduce survivorship bias without institutional CRSP/Compustat data.

Impact:
- ticker_universe_snapshot table
- simulation uses historical snapshot nearest and not after sim date

Limitation:
Residual delisted-price survivorship bias remains.

---

# 2026-05-28

## Decision: Market regime uses SPY, QQQ, VIX

Reason:
Simple, explainable V1 macro context.

Impact:
- benchmark loader must load SPY, QQQ, ^VIX
- benchmarks excluded from screening

---

# 2026-05-28

## Decision: Use raw Top 20 and diversified Top 20

Reason:
User wants to see true strongest candidates and how diversification changes the list.

Impact:
- Step 5 stores raw_rank and diversified_rank
- UI checkbox controls display
- outcome queue tracks raw OR diversified Top 20
- simulation compares raw vs diversified performance
- export includes both rankings

---

# 2026-05-28

## Decision: Diversification penalties are optional / display-toggle visible

Reason:
User wants visibility into raw strongest candidates.

Impact:
- ranking engine calculates both rankings
- UI table shows both raw and diversified ranks
- export includes rank comparison
- feedback analyzes rejected-by-diversification candidates

---

# 2026-05-28

## Decision: Hard cap mode means no soft penalty in V1

Reason:
Avoid double punishment and simplify interpretation.

Impact:
- if hard_cap_enabled = TRUE, over-cap candidates rejected
- non-rejected candidates keep proposal_score_final = proposal_score_raw
- if hard_cap_enabled = FALSE, soft penalty applies

---

# 2026-05-28

## Decision: Outcome queue tracks raw OR diversified Top 20

Reason:
Need to compare performance of raw strongest names vs diversified list.

Impact:
- outcome queue condition:
  in_raw_top_n OR in_diversified_top_n

---

# 2026-05-28

## Decision: Use entry_price_sim for performance

Reason:
Slippage-adjusted simulated entry must affect returns.

Impact:
- entry_price_raw = audit reference
- entry_price_sim = denominator for return/MFE/MAE/R-multiple

---

# 2026-05-28

## Decision: Step 4 uses signal-date close as entry proxy

Reason:
Next-day open is unknown at signal time.

Impact:
- entry_proxy_raw = close_raw on signal_date
- stop/target/RR estimated from proxy
- if actual open gap >5%, log warning but do not recompute signal

---

# 2026-05-28

## Decision: Streamlit dashboard is local single-user UI

Reason:
V1 is personal research tool.

Impact:
- no authentication
- no multi-user concurrency
- local state acceptable

---

# 2026-05-28

## Decision: Define trend_resume setup detection rule

Decision:
A setup is classified as `trend_resume` when:
- close_adj was below EMA20 for at least 3 of the prior 10 trading days;
- close_adj is now above EMA20;
- pullback_from_recent_high_pct is between -20% and -3%.

Reason:
Claude audit found that trend_resume was the only setup type listed without a detection rule.

Impact:
- Step 4 setup analysis
- setup_type classification tests
- feature/setup documentation consistency

---

# 2026-05-28

## Decision: Module 03 must use merged final schema

Decision:
Module 03 Schema Manager must use the merged final schema from:
- Master ТЗ v1 FULL
- PATCH 1
- MINI-PATCH 2

Reason:
Avoid creating outdated base tables and then immediately applying patch ALTER statements on a fresh database.

Impact:
- Schema Manager
- database migrations
- tests for schema creation
- AI coding prompts for Module 03

---

# 2026-05-28

## Decision: Structural domain constants live in constants.py

Decision:
Values fixed by MASTER_SPEC.md identically across all strategy presets are
treated as structural domain constants, not tunable strategy thresholds, and
may live in `app/config/constants.py`. This explicitly covers:

- VIX market regime boundaries 25/30 (MASTER_SPEC.md §11)
- outcome horizons 5/10/20/40 bd (MASTER_SPEC.md §16)
- screening block weights 0.30/0.25/0.20/0.15/0.10 (MASTER_SPEC.md §12)
- simulation vocabulary: list_membership / list_type (MASTER_SPEC.md §17)
- AI review attribution vocabulary (MASTER_SPEC.md §19)

Tunable thresholds are ONLY those that vary between the normal / aggressive /
conservative presets (MASTER_SPEC.md §20) — e.g. min_price, min_rvol,
min_screening_score, sector_max_positions, earnings_avoid_window. Those live in
strategy config (immutable presets in `app/config/settings.py`), per
CODING_STANDARDS.md §10.

Reason:
The rule "do not hardcode trading thresholds outside config"
(CODING_STANDARDS.md §1, §10) targets preset-tunable parameters. Spec-fixed
domain numbers (VIX regime bounds, horizons, score weights) are not tunable and
do not vary by preset. Logging this avoids repeated reviewer disputes over
whether spec-fixed constants violate that rule.

Verification (Module 01):
- VIX_EXTREME_RISK_THRESHOLD / VIX_HIGH_RISK_THRESHOLD, the simulation
  vocabulary, and the AI review attribution vocabulary are referenced ONLY at
  their definitions in constants.py — confirmed by grep across app/ and tests/.
  Removing any of them later would not break imports or tests.

Impact:
- Module 01 constants.py is accepted as in-scope.
- Future modules (12 Market Regime, 13 Step 3, 16 Outcome Queue, 17 Simulation,
  19 AI Review) consume these constants instead of redefining the values.
