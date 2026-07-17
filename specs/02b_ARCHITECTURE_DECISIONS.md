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

## 22.26 consolidation_base score recalibration

**Status: IMPLEMENTED (2026-07-15) — Option 1 (rescale scorer).** Independently
re-verified 2026-07-15 (diff match, fresh test runs, fresh stash-based
isolation proof, fresh empirical spot-check against stored data — see
`reports/AD-22.26_reverification_2026-07-15.md`). Written per the
diagnostics-first / no-pre-diagnostic-tuning
principle: this AD exists *because* 5-date empirical data now supports a
calibration decision that was explicitly deferred pending exactly this data
(see `funnel_diagnostics_2026-07-10.md` §Architect notes and the
2026-06-11/18/26, 07-02/08 campaign).

**Problem:** `consolidation_base` validator pass rate is pinned at 1.2–2.6%
across 5 independent trading dates spanning both low-RVOL (0.76–0.87) and
high-RVOL (1.56–1.83) regimes — unlike `breakout`, whose pass rate swings an
order of magnitude with RVOL (4.4–8.1% low-vol vs. 59.0–68.2% high-vol),
`consolidation_base` shows **no regime sensitivity at all**. This rules out
"quiet market" as an explanation.

The routed-population `setup_score` distribution for `consolidation_base` is
stationary and sits entirely below the shared 55.0 pass threshold on every
date:

| Date | n | p25 | p50 | p75 | max |
|---|--:|--:|--:|--:|--:|
| 2026-06-11 | 630 | 40.69 | 44.86 | 48.18 | 66.87 |
| 2026-06-18 | 598 | 37.94 | 42.49 | 45.41 | 71.38 |
| 2026-06-26 | 612 | 40.71 | 44.17 | 47.39 | 69.06 |
| 2026-07-02 | 642 | 39.37 | 43.34 | 46.27 | 85.39 |
| 2026-07-08 | 633 | 38.66 | 41.51 | 44.88 | 77.78 |

Median sits 10–13 points below threshold on every date; even p75 (top
quartile of the *routed* population, before any validation) never clears
48.2 — 6.8+ points short of 55.0. `score_below_threshold` is consistently the
#2 or #3 failure reason (17–24% of failures) alongside
`range_tightness_too_low` (38–43%) and `price_above_base_high` (20–33%),
which are separate, apparently well-behaved gates showing no evidence of the
same miscalibration.

### Regime scope of this evidence

All empirical data behind this ADR (the 5-date campaign plus the confirmatory
6th date) classifies as `bull` regime — no `neutral`, `bear`, `high_risk`, or
`extreme_risk` date has been diagnosed. A check of existing price history
(2026-07-15) found no currently-usable non-bull date: the only VIX≥25 window
in loaded data (March 2026, peak VIX 31.05 on 2026-03-27) predates the
252-bar `feature_ready` floor (2026-06-02), so the universe would be mostly
`feature_not_ready` if diagnosed against that period — the same failure mode
already documented in the Part A backfill findings. Extending price history
to make that period usable was declined as disproportionate cost for one
historical data point (same reasoning as the earlier RVOL campaign's
Option A decision).

This does not block a decision here — `range_tightness_too_low` and
`price_above_base_high` show no regime-dependence in their mechanism (pure
price-structure gates), and the score-scale mismatch is stationary across
every RVOL regime tested, which is suggestive but not conclusive that it is
also regime-stable. Whichever option is chosen should be treated as
**validated under bull regime only**, with an explicit follow-up: re-check
the chosen fix (rescaled scorer or per-setup threshold) the first time a
live date after 2026-06-02 classifies as `neutral`, `bear`, `high_risk`, or
`extreme_risk`.

**Root-cause framing (not yet confirmed at code level — see open item
below):** two non-exclusive explanations fit the data: (1) the
`consolidation_base` scoring formula's output scale does not match the 55.0
threshold's intended scale (formula produces systematically lower values
than the other three setups' scorers, which sit comfortably above their
thresholds on the same dates — see the 07-10 report §6: pullback p50=74.98,
trend_continuation p50=66.13 against the same 55.0 threshold); (2) the 55.0
threshold was calibrated against an earlier/different version of the
`consolidation_base` scoring formula and was never re-validated after a
formula change.

**Options:**

- **Option 1 — Rescale/recalibrate the `consolidation_base` scorer.** Adjust
  the scoring formula's output range so its distribution sits in the same
  rough band as the other three setups' scorers (all cluster with p50 in the
  low-to-high 60s–70s against the shared 55.0 threshold). Keeps a single
  shared pass threshold across all setups — simpler mental model, but
  requires identifying exactly which scoring sub-components are responsible
  for the scale gap and confirming the fix doesn't change *relative* ranking
  within `consolidation_base` candidates (only the absolute scale).
- **Option 2 — Give `consolidation_base` its own threshold.** Leave the
  scorer as-is; set a setup-specific pass threshold calibrated to its actual
  distribution (e.g., in the 45–48 range, informed by the p75 ceiling
  above). Smaller, safer change (config-only, no formula code touched) but
  breaks the "one shared 55.0 threshold" simplicity and requires deciding
  the new threshold value defensibly rather than by feel.

**Architect's leaning (not a decision):** Option 1. `range_tightness_too_low`
and `price_above_base_high` are separately-gating and show no sign of the
same problem, which suggests the score component specifically is the
outlier needing correction — not that `consolidation_base` as a setup type
is inherently harder to qualify for. Rescaling the scorer to match the other
three setups' bands preserves the "one shared threshold across setups"
design and avoids introducing a fifth calibration knob (`consolidation_base`
would be the only setup with its own bespoke threshold under Option 2). This
is a lean, not a final call — flagging for owner sign-off since it touches
the scoring formula, which is closer to core logic than a config value.

**Impact if Option 1 is chosen:**
- Touches `consolidation_base` scoring formula code (frozen-module
  implications — not part of the original AD-22.24 migration exemption;
  needs its own frozen-module carve-out, narrow and scoped to this formula
  only, same pattern as AD-22.25's M12 carve-out).
- Requires before/after golden-diff-style validation: rescaled scores must
  preserve relative ranking order among `consolidation_base` candidates
  (i.e., a candidate that scored higher than another before rescaling must
  still score higher after) — this is a scale transform, not a re-ranking.
- `risk_label_config` and any downstream consumer of `setup_score` for
  `consolidation_base` (ranking, diversification) should be checked for any
  hardcoded assumption about the setup's score range.

### Frozen-module exemption (granted 2026-07-15)

A narrow exemption to frozen-module discipline is granted for this work,
scoped strictly to: the `consolidation_base` setup_score formula/weighting
inside `m14_setup_validators.py` (or wherever the scoring computation
actually lives — confirm exact location as part of Part B below). No other
setup's scoring, no gate logic (range_tightness, price_above_base_high, or
any other consolidation_base validator check), and no schema change are
covered by this exemption. Scope ends at the scoring formula's output value;
everything downstream (threshold comparison, ranking, disposition) is
unaffected and not exempted.

**Impact if Option 2 is chosen:**
- Config-only change to `consolidation_base`'s entry in `setup_configs`
  (`setup_consolidation_base_v1`) — no code/formula change, no
  frozen-module question.
- Exact threshold value needs to be picked defensibly — recommend deriving
  from a percentile target (e.g., "threshold = p75 minus small margin" or
  similar) rather than an arbitrary round number, and validating against a
  few additional out-of-sample dates before locking it in.

**Not decided here (explicitly out of scope for this AD):**
- The exact rescaled formula or exact new threshold value — that's
  implementation detail for whichever option is chosen, done in a follow-up
  coder note after sign-off.
- Any change to `range_tightness_too_low` or `price_above_base_high` gates —
  no evidence in the campaign data that these need adjustment.
- Any change to `breakout`'s RVOL gate — separately confirmed as correctly
  calibrated by the same campaign (see campaign verdict, 2026-07-15).

**Decision:** Option 1. See implementation note "§22.26 Decided (Option 1):
Investigate + Propose Rescale Approach" (coder note, 2026-07-15) for design
and rollout.

### Implementation summary (2026-07-15)

Full derivation, diff, and validation live in
`reports/ad_22_26_option1_rescale_implementation_2026-07-15.md`; re-verified
independently 2026-07-15 in `reports/AD-22.26_reverification_2026-07-15.md`.
Condensed here so the decision record is self-contained without duplicating
either report in full:

- **Transform:** `new_score = clamp(0.52 * penalized_score + 48.0, 0, 100)`,
  applied once, as the final step of `validate_consolidation_base()` only —
  after the existing `penalized_score = _clamp(raw_score + earnings_pen +
  macro_pen + fundamentals_adj)` line, not to `raw_score` or any individual
  scoring component. `breakout`/`pullback`/`trend_continuation` are untouched
  (confirmed byte-identical both by the original stash comparison and by an
  independent re-run).
- **Constants:** anchored at `f(100)=100` (ceiling maps to itself, keeping
  the transform clamp-free over the whole domain) and `f(43.27)≈70.5`
  (5-date campaign mean p50 mapped to the mean of pullback/trend_continuation
  p50 from the same campaign) — a defensible anchor pair, not an arbitrary
  round number, though the campaign-mean-vs-single-date mixing noted in the
  original coder note means the exact constants are a close approximation,
  not a uniquely-derivable pair.
- **Ordering proof:** the transform is strictly increasing (slope 0.52 > 0),
  so it preserves relative ranking among `consolidation_base` candidates
  exactly. Verified exhaustively (every integer pair in [0,100], and a
  1001-point fractional grid) in `TestConsolidationBaseRescaleAD2226`.
- **Clamp-never-fires:** `f(0)=48.0` and `f(100)=100.0` are the actual min/max
  outputs over the legal [0,100] input domain — the outer `clamp()` never
  activates in practice.
- **Empirical outcome:** aggregate `consolidation_base` pass rate across the
  5 campaign dates rises from 1.86% to 21.57%; `score_below_threshold` drops
  from 19.7% of the population to 0%, leaving `range_tightness_too_low`
  (38.7%) and `price_above_base_high` (27.0%) as the sole effective gates —
  matching the ADR's root-cause framing that the score component, not the
  structural gates, was the outlier. Two of the five dates (2026-06-11,
  2026-07-02) were independently recomputed from stored `step4_analysis` rows
  during re-verification and matched exactly.
- **Downstream-consumer findings:** `_derive_confidence`'s 75/50 thresholds
  and Step 5's `_W_SETUP=0.40` / `setup_confirmation` (weight 0.10 by
  default) are the only two places found with a hardcoded assumption about
  `consolidation_base`'s score range; both were checked and produce the
  intended effect (e.g. HTGC 2026-07-02: `setup_score` 59.29→78.83,
  `proposal_score_raw` 59.09→66.91, `risk_score` 30.33→28.37, independently
  reproduced during re-verification). `simulation_engine.py`,
  `dashboard/data_access.py`, and `dashboard/ticker_report.py` pass
  `setup_score` through without any range assumption.
- **Known gaps (not yet closed):** the ~614 rows that flip
  `score_below_threshold` → `passed` were stored with `disposition =
  REJECTED` and no stop/target/RR/risk_score — determining their real
  post-rescale disposition requires a live Step 4 → Step 5 re-run against the
  5 campaign dates, not yet performed. Diversity-cap/ranking-pool interaction
  from `consolidation_base`'s ~10x larger passing population is flagged but
  not independently verified. Both are candidates for a follow-up coder note,
  not blockers on this AD's IMPLEMENTED status.

---
