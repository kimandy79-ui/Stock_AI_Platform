# 02b_ARCHITECTURE_DECISIONS

Status: split active architecture decisions file for Claude Project Files.
Generated from `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §22 on 2026-05-31.
Setup-mode migration entries (22.19–22.24) added 2026-06-19.

Use this file only when the task needs active architecture decision details.

---

# 22. Active Architecture Decisions

This section contains active decisions only.
Historical manifest/change-log data is intentionally excluded.

## 22.1 Use DuckDB only
Reason: local analytical workload, simple deployment, excellent columnar SQL.
Impact: no PostgreSQL in V1; no server DB; local files prod/debug/simulation.

## 22.2 Polars-first processing
Reason: faster than pandas for large analytical transformations.
Impact: feature engine uses Polars; pandas only for provider/library compat.

## 22.3 Research-grade V1, not institutional production-grade
Reason: YahooProvider has no SLA and historical universe is imperfect.
Impact: honest simulation caveats; no overengineering; provider abstraction.

## 22.4 Separate DB files
Use prod.duckdb / debug.duckdb / simulation.duckdb.
Impact: simulation attaches prod read-only; debug never writes prod.

## 22.5 YahooProvider V1 behind provider abstraction
Reason: free, easy, broad coverage.
Impact: MarketDataProvider interface required; future Polygon/Tiingo possible.

## 22.6 Store raw and adjusted prices
Impact: daily_prices has raw and adjusted OHLC; indicators use adjusted;
stop/target uses raw; returns use adjusted close with simulated entry price.

## 22.7 Use feature_cutoff_date
Reason: prevent look-ahead bias.
Impact: all feature calcs anchor on feature_cutoff_date; simulation enforces cutoff.

## 22.8 Use zero-padded feature schema versions
Use features_v01, features_v02, etc. Reason: avoid lexicographic MAX bug.
Impact: constants.py FEATURE_SCHEMA_VERSION (now "features_v02"; see 22.19).

## 22.9 Use monthly universe snapshots
Reason: reduce survivorship bias without CRSP/Compustat.
Impact: ticker_universe_snapshot; simulation uses nearest snapshot not after sim date.
Limitation: residual delisted-price survivorship bias remains.

## 22.10 Market regime uses SPY, QQQ, VIX
Impact: benchmark loader loads SPY, QQQ, ^VIX; benchmarks excluded from screening.

## 22.11 Use raw Top 20 and diversified Top 20
Impact: Step 5 stores raw_rank and diversified_rank; UI checkbox controls
display; outcome queue tracks raw OR diversified Top 20; simulation compares;
export includes both rankings.

## 22.12 Hard cap mode means no soft penalty in V1
Impact: if hard_cap_enabled, over-cap candidates rejected; non-rejected keep
proposal_score_final = proposal_score_raw; if disabled, soft penalty applies.

## 22.13 Outcome queue tracks raw OR diversified Top 20
Condition: in_raw_top_n OR in_diversified_top_n.

## 22.14 Use entry_price_sim for performance
Impact: entry_price_raw = audit reference; entry_price_sim = denominator for
return/MFE/MAE/R-multiple.

## 22.15 Step 4 uses signal-date close as entry proxy
Impact: entry_proxy_raw = close_raw on signal_date; stop/target/RR estimated
from proxy; if actual open gap >5%, log warning but do not recompute.

## 22.16 Define trend_resume detection rule (RECONCILED for setup mode)
Original rule: a setup is classified as `trend_resume` when close_adj was below
EMA20 for >=3 of prior 10 td, is now above EMA20, and pullback_from_recent_high
is between -20% and -3%.
Setup-mode reconciliation (AD-22.19/22.20): the `trend_resume` *condition* is
retained inside the `trend_continuation` setup validator as a qualifying
pattern. There is no longer a separate `trend_resume` setup_type value; the
single `setup_type` field carries `trend_continuation`.

## 22.17 Schema Manager must use merged final schema
For a fresh DB, create the final merged + setup-mode schema directly (no base
tables + ALTER). See 22.21 for the setup-mode required state.

## 22.18 Structural domain constants live in constants.py
Values fixed identically across presets are structural domain constants, not
tunable thresholds: VIX regime boundaries 25/30; outcome horizons 5/10/20/40 bd;
simulation vocabulary (list_membership / list_type); AI review attribution
vocabulary. Setup-mode note: per-setup validation thresholds are tunable and
live in setup_configs, not here; the active setup_type vocabulary
(breakout/pullback/trend_continuation/consolidation_base), risk_label, and
disposition enums are structural and live in constants.py.

---

# Setup-mode migration decisions (2026-06-19)

## 22.19 Setup mode is the primary selection architecture (supersedes 3-strategy mode)

Owner decision (final):
- Old 3-strategy mode (aggressive / normal / conservative) is **retired** as the
  primary selection architecture.
- **Setup mode** becomes the primary selection architecture.
- Aggressive / Normal / Conservative are no longer primary strategy configs;
  they appear only as deprecated legacy terms.
- Risk is an **output label** assigned after setup validation: low / medium /
  high. Not a selection configuration.
- Historical compatibility with old strategy-mode outputs is **not required**.
  Clean schema reset and full recompute permitted.
- Existing modules reused where safe, but public contracts MAY change for this
  approved migration. The frozen-module rule is relaxed for the modules in
  §22.24 for the duration of the migration only.

Primary selection driver changes from `strategy_config_id` to
`setup_config_id` + `setup_type`.

Impact: schema (22.21), configs (22.22), M11/M13/M14/M15/M16/M17/M19/M20/M21/M22,
diagnostics, dashboard, tests, constants, feature schema (features_v02).

## 22.20 Active setup taxonomy (single overloaded setup_type)

Active `setup_type` values (the selection unit):
```text
breakout
pullback
trend_continuation
consolidation_base
```
The single `setup_type` field carries exactly one of these. The legacy
six-value vocabulary (trend_pullback, breakout, volatility_squeeze,
trend_resume, high_tight_flag, momentum_extension, unknown) is **retired** —
there is no separate `setup_subtype` field. Legacy classifier patterns are
folded into the four validators (e.g. the old volatility_squeeze pattern is a
qualifying condition inside consolidation_base; trend_resume inside
trend_continuation; high_tight_flag inside breakout). Candidates matching no
active setup are recorded only via `setup_fail_reason` and never receive a BUY
disposition.

## 22.21 Setup-mode schema model

- `setup_config_id` replaces `strategy_config_id` as the primary active config
  reference on step3/step4/step5/signal_outcomes and sim_* counterparts.
- `setup_configs` and `risk_label_config` tables replace `strategy_configs`.
- step4/step5 require `setup_type` (4-value) NOT NULL and `setup_score`.
- step5 adds `risk_score`, `risk_label`, `risk_reasons`, `disposition`, the
  structural trade plan (entry/stop/target/estimated_rr,
  support/resistance/next_resistance), `earnings_days`, `market_regime`.
- step3 becomes universal eligibility (`passed_eligibility`,
  `eligibility_fail_reasons`, `routed_setup_types`); `setup_config_id` nullable.
- daily_features gains features_v02 structural columns.
- signal_outcomes gains `setup_type`, `risk_label`, `stop_hit`, `target_hit`.
- Legacy `strategy_config_id` columns are not retained as active keys.
DDL in 01b.

## 22.22 Setup configs replace strategy configs

Retire aggressive / normal / conservative. Seed:
```text
setup_breakout_v1
setup_pullback_v1
setup_trend_continuation_v1
setup_consolidation_base_v1
risk_label_config_v1
```
A strategy × setup matrix (e.g. aggressive_breakout) is **forbidden**. Risk is
an output label, not a config dimension.

## 22.23 RVOL is setup-specific, not a universal hard gate

Universal eligibility (pre-classification) is limited to data ready, valid stock
type, valid OHLCV history, minimum valid price, minimum liquidity, no obvious
anomaly. RVOL, setup score, momentum, ATR%, EMA extension, and consolidation
quality MUST NOT be universal hard gates. Per-setup RVOL:
- breakout: hard / near-hard confirmation
- pullback: soft confirmation / penalty only (never hard reject)
- trend_continuation: moderate confirmation
- consolidation_base: high RVOL not required; controlled volume acceptable

Rationale: the prior universal rvol_gate alone removed 76–93% of the universe
before any setup logic ran, eliminating Normal/Conservative candidates entirely.

## 22.24 Frozen-module exemption scope for this migration

Frozen-module discipline is suspended ONLY for these modules and ONLY for the
setup-mode migration:
```text
M11 feature engine (features_v02), M13 step3 (universal eligibility + routing),
M14 step4 (setup validation), M15 step5 (stop/target/risk/disposition),
M16 outcome queue, M17 simulation, M19 ai review/export, M20 orchestrator,
M21 dashboard, M22 debug/funnel, schema_manager, default_configs, constants,
config seeders, tools/init_*_db
```
All other modules (DuckDB manager, providers, universe snapshot, benchmark
loader, ingestion, validator, mutation detector, regime engine,
trading_calendar, service_result) remain frozen with unchanged public
contracts. Each migration phase freezes its modules on acceptance with a
`moduleNN_..._stable` commit.

---

## 22.25 Narrow frozen-module carve-out: M12 market breadth field (P2.2)

**Status: granted 2026-07-09.** AD-22.24 lists the regime engine (M12,
`market_regime_engine.py`) among the modules that stay **frozen** and are NOT
covered by the migration exemption. This AD grants a **single, strictly-scoped
exception** to that freeze.

**Exactly what is permitted:**
- Add ONE additive, initially-**inert** market-breadth field to M12's output —
  `market_breadth_pct` (and/or a `breadth_regime` label), computed as the
  percentage of the active feature-ready universe trading above its own
  `ema200` on the signal date, from data already materialized in
  `daily_features`. No new provider, no new price fetch.
- The corresponding additive schema column(s) on the regime write path.

**Explicitly NOT permitted under this carve-out (each needs its own decision):**
- No change to `_build_predicates`, the priority ordering, or the existing
  `market_regime` enum / its taxonomy.
- No change to any *consumption* of the regime signal — Step 3 routing stays
  regime-independent; Step 5 gating logic is untouched. The new field is stored
  and **not read by any disposition/routing path** in this change.
- No penalty-gating and no regime-conditional routing on breadth. Those are
  separate future decisions, not authorized here.

**Proof obligation:** zero-behavior-change must be demonstrated with a genuine
before/after golden diff (pre-change vs post-change output on a fixed dataset,
identical — same standard as the P2.1 [HC->CFG] promotion), covering both the
existing `market_regime` classification and all downstream Step-5 output. The
field is purely additive; existing outputs must be byte-identical.

**Rationale:** the change is narrow, additive, and inert — it lands the
*measurement* only, mirroring the "seed inactive / scoring-only first" pattern
used throughout this migration (fundamentals weight 0.0, rs_percentile_126d
scoring-only, `enforce_compression_floor` default-False). The M12 freeze
remains the default; this carve-out does not reopen M12 for any other change.

---
