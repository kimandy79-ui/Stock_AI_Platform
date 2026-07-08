# Phase 4 — Compact Fundamentals/Events Layer: Design Note

## Frozen-module gate — resolved, additive only

M04 (`provider_interface.py`) is on the frozen list but the note explicitly
allows additive extension. `FundamentalSnapshot` is a new frozen dataclass;
`get_fundamentals` is a **concrete-default** method on `MarketDataProvider`
(not `@abstractmethod` like the other four) specifically so the frozen M05
(`yahoo_provider.py`) needs zero code changes to keep instantiating —
verified directly: `YahooProvider()` constructs and its inherited
`get_fundamentals` correctly returns `status="failed"` /
`error_detail.kind="unsupported_capability"` without any edit to that file.
`ProviderCapabilities.supports_fundamentals` defaults to `False` for the same
reason. Existing M04/M05 test suites (32 + 23 tests) pass unchanged.

## Schema decision — companion table, not new `daily_features` columns

Chose a new `ticker_fundamentals` table over adding 7 columns to
`daily_features`. Reasoning: `daily_features` is daily-cadence, high-traffic
(written and read every trading day for every ticker); the 7 fundamentals
fields are quarterly/irregular-cadence and would sit mostly-NULL in that hot
path. A companion table keyed `(ticker, as_of_date)` keeps the write/read
pattern natural (point-in-time snapshots, upserted on a slower cadence) and
avoids touching the wide, heavily-tested `daily_features` schema at all.

**`FEATURE_SCHEMA_VERSION` is NOT bumped** — `daily_features` itself is
completely untouched by this phase. This was one of the two items the coder
note said to confirm rather than assume; confirming here.

## Field-sourcing decisions (the note's mandatory "flag, don't substitute silently" clause)

Reversed the note's stated "Finnhub primary, EDGAR secondary" order to
**EDGAR primary, sole implemented source**. Reasoning: SEC EDGAR's XBRL
`companyfacts` API (`data.sec.gov`) is free, keyless, and its schema is
fully public and stable; Finnhub's live free-tier limits could not be
verified in this environment, and building against unverifiable API
behavior risks silent breakage. All 5 of the 7 fields that are actually
computed come from EDGAR alone (`app/providers/edgar_provider.py`):

- `eps_growth_trend`, `leverage_ratio`, `piotroski_f_score`,
  `altman_z_score` — computed self-contained from annual (10-K) XBRL facts.
- `valuation_band` — computed **only if** a `price_lookup` callable is
  injected (kept optional so this provider never reaches into another
  provider by default); otherwise reports `"unknown"` (a valid catalog
  value, not a failure) — a P/E ratio has no meaningful book-value-only
  substitute.

**Two fields are permanently `None` from this provider — flagged as
blocking gaps, not silently substituted:**

- **`insider_trade_flag`** — SEC EDGAR Form 4s are filed under the
  *insider's* own CIK, not the issuer's; the issuer's own `submissions.json`
  does not list them. Resolving "which Form 4s reference this issuer"
  reliably needs EDGAR full-text search with query semantics this
  implementation could not verify with confidence from documentation alone.
  Not guessed at. **Decision needed:** pursue a best-effort Finnhub
  `/stock/insider-transactions` client (unverified tier), or invest in
  correctly integrating EDGAR full-text search, or accept permanent gap.
- **`institutional_ownership_delta`** — true institutional ownership needs
  aggregating 13F filings across *all* institutional filers, quarter over
  quarter, for a given issuer. This is a substantial data-engineering
  undertaking (not a per-ticker API call) and is out of scope for this
  phase, per the note's own explicit instruction to flag rather than
  substitute a proxy.

**Two documented formula substitutions (standard alternates, not invented):**

- **Altman Z-Score uses the Z'-Score (private-firm/book-value) variant** —
  substitutes book value of equity for market value of equity in the `D`
  term. This keeps the provider fully self-contained (no price-feed
  dependency, no cross-provider reach-in). Flagging in case a market-cap-
  based Z-Score specifically is required downstream later.
- **Piotroski F-Score computed on 8 of the standard 9 signals** — the
  "no new share issuance" signal is omitted because reliably sourcing
  weighted-average diluted shares outstanding from XBRL needs yet another
  concept-alias family with its own filer inconsistency; rather than
  fabricate a shaky 9th signal, the score is computed on 8 signals and
  scaled (`round(score * 9 / 8)`) to stay comparable to the textbook 0-9
  range. Flagging for review if the 9th signal is later worth the added
  XBRL-parsing risk.

## Second OHLCV provider — reinterpreted, not literal

The note asks for a "yfinance fallback," but `YahooProvider` *is* the
yfinance-backed provider already — a literal reading would be circular.
Implemented **Stooq** instead (`app/providers/stooq_provider.py`), a
genuinely independent free/keyless data source, as the configurable
fallback: callers construct whichever provider instance they want (no
implicit provider-selection logic added). Stooq's plain daily CSV has no
split/dividend adjustment, so `supports_adjusted_prices=False` lets callers
detect this rather than silently receiving mislabeled data.

## Point-in-time discipline (Phase 0 leak test — mandatory, all 7 fields)

`extract_annual_series` filters on **both** `end <= as_of_date` and
`filed <= as_of_date` — filtering on `end` alone would leak (a fiscal year
that ended before `as_of_date` may not have been *filed*, and thus knowable,
until well after it). New leak-test suite
(`tests/test_fundamentals_point_in_time.py`, 9 tests, companion to the
existing `test_point_in_time_integrity.py`) constructs a 3-period fixture
where the newest period is filed *after* `as_of_date` with deliberately
extreme values, and asserts every computed field matches the non-leaked
result — plus a control test proving the guard, not unreachability, is what
keeps the result stable (advancing `as_of_date` past the filing date changes
the result). `insider_trade_flag` / `institutional_ownership_delta` carry no
data at all, so there's nothing for them to leak.

## Ingestion — mirrors `_step_earnings` exactly

New `fundamentals_refresh` pipeline step in `pipeline_orchestrator.py`,
positioned immediately after `earnings_calendar_refresh` in `STEP_NAMES`
(both are recoverable event/calendar refreshers ahead of `price_ingestion`).
Same shape as `_step_earnings`: already-refreshed-today guard (`SELECT
COUNT(*) ... WHERE CAST(calculated_at AS DATE) = ?`), same active-ticker
universe query, one batch-upsert transaction, per-ticker fetch failures are
warnings (not hard failures) — a missing snapshot just leaves
`ticker_fundamentals` at its pre-step state, same as `_step_earnings` leaving
`days_to_earnings_bd` NULL on failure. New constructor param
`fundamentals_provider` (default `EdgarFundamentalsProvider()`), mirroring
the existing `provider` param. Updated the orchestrator's own architectural
guard test (`test_sql_write_targets_only_pipeline_tables`) to add
`ticker_fundamentals` as a documented exception, same as the pre-existing
`earnings_calendar` exception.

## Step 3/4/5 integration — optional, config-weighted, never a hard gate

**Step 3: deliberately untouched.** Mirrors the RVOL precedent (AD-22.23:
RVOL is not a universal Step 3 gate, setup-specific only). Fundamentals are
equally not-universal quality signals; they stay a Step 4/5 concern. Adding
them to Step 3 just to touch all three steps would be scope-widening without
a precedent basis.

**Step 4** (`step4_setup_validation_engine.py` + `m14_setup_validators.py`):
new `_read_fundamentals` point-in-time query (same `as_of_date <= signal_date`
+ most-recent-per-ticker pattern as the leak-test discipline above), merged
into the `feat` dict alongside features/prices. New
`_compute_fundamentals_adjustment(feat, setup_config.get("fundamentals", {}))`
pure helper, called from all 4 `validate_*` functions in the same additive
slot as `earnings_pen`/`macro_pen`. **Disabled by default**
(`fundamentals_cfg.get("enabled", False)`) — every existing setup_config
without a `fundamentals` block gets an adjustment of exactly `0.0`, verified
byte-identical to pre-Phase-4 behavior across the full 142-test
`test_m14_setup_validators.py` suite (unchanged, all green). When enabled,
weight lives in `setup_configs.<type>.fundamentals.weight` — never
hardcoded. A ticker with no `ticker_fundamentals` coverage yet gets `0.0`
(absence is not treated as a penalty).

**Step 5** (`step5_proposal_engine.py`): mirrors the Phase 3
`ai_review_scores` pass-through pattern exactly rather than adding a second,
independent DB read inside M15. New optional `fundamentals_scores:
dict[str, float] | None` param on `propose()`/`_build_rows()`, keyed by
ticker → a 0-100 quality score computed the same way M14's helper does.
Config-weighted via `risk_label_config.fundamentals.score_weight` (default
0.10, same explicit-None-check back-compat pattern as `ai_review`'s
weights). Additive and **two-sided** (unlike the Phase 3 penalties, which
only ever subtract): centered at quality_score 50, so above-average
fundamentals add points and below-average subtract, capped at roughly ±5
points at the default weight — never enough alone to force a gate, and there
is deliberately no disposition-forcing threshold (unlike
`audit_consistency_min_for_buy`). Byte-identical to pre-Phase-4 when the
param is omitted — verified directly.

**Known asymmetry, flagged rather than silently left:** Step 4 independently
reads `ticker_fundamentals` itself; Step 5 does not — it only accepts a
pre-computed pass-through, exactly like `ai_review_scores` before it. No
orchestration wiring exists yet that reads `ticker_fundamentals`, computes a
quality score, and calls `propose(fundamentals_scores=...)` end-to-end. This
is the same "real architectural gap" already flagged and accepted in the
Phase 3 design note for `ai_review_scores` — consistent, not a new problem,
but worth the architect's attention if live Step 5 consumption is wanted
before a future phase builds that orchestration.

## What shipped

- `app/providers/provider_interface.py`: `FundamentalSnapshot` DTO,
  `VALUATION_BANDS`, `get_fundamentals` concrete-default method,
  `ProviderCapabilities.supports_fundamentals`.
- `app/providers/edgar_provider.py` (new): pure XBRL extraction/computation
  functions + `EdgarFundamentalsProvider`.
- `app/providers/stooq_provider.py` (new): pure CSV parsing + `StooqProvider`.
- `app/database/schema_manager.py`: `ticker_fundamentals` table (prod/debug
  only, not simulation).
- `app/services/pipeline/pipeline_orchestrator.py`: `fundamentals_refresh`
  step, `_step_fundamentals`, `fundamentals_provider` constructor param.
- `app/services/analysis/step4_setup_validation_engine.py`:
  `_read_fundamentals`, `_build_feat_dict` fundamentals merge.
- `app/services/screening/m14_setup_validators.py`:
  `_compute_fundamentals_adjustment`, wired into all 4 validators.
- `app/services/proposal/step5_proposal_engine.py`: `fundamentals_scores`
  pass-through, `fundamentals_quality_score`/`fundamentals_score_weight` in
  `_proposal_score_raw`, `fundamentals` block in `_parse_risk_label_config`.

## Testing

New: `tests/test_edgar_provider.py` (28), `tests/test_stooq_provider.py`
(18), `tests/test_fundamentals_point_in_time.py` (9, Phase 0 leak tests),
`tests/test_pipeline_fundamentals_step.py` (8), `tests/test_m14_fundamentals_scoring.py`
(14), `tests/test_step4_fundamentals_integration.py` (5), plus additions to
`tests/test_step5_proposal_engine.py` (fundamentals back-compat/scoring/
config tests) and `tests/test_phase6_orchestrator.py` (SQL-scan allowlist
update). All pre-existing suites touched by this phase
(`test_provider_interface.py`, `test_yahoo_provider.py`,
`test_schema_manager.py`, `test_pipeline_orchestrator.py`,
`test_phase6_orchestrator.py`, `test_m14_setup_validators.py`,
`test_step5_proposal_engine.py`) confirmed green with zero behavior change
when the new optional features are absent. Full repo-wide run performed
before commit.

## Exit criterion status

7 fields defined in the DTO/schema; 5 computed for real from EDGAR, 2
explicitly flagged as blocking gaps (not silently substituted) per the
note's own instruction. Point-in-time discipline verified with a dedicated
leak-test suite. Ingestion mirrors the earnings pattern exactly. Step 4/5
integration is optional, config-weighted, and provably never a hard gate
(byte-identical when absent, verified for both). Step 3 deliberately
untouched, mirroring the RVOL precedent. Second OHLCV provider (Stooq)
added behind the same `MarketDataProvider` abstraction. No paid data
sources, no ML, no changes to `get_earnings`/`EarningsEvent`, no schema
changes beyond the 7 fields' companion table.
