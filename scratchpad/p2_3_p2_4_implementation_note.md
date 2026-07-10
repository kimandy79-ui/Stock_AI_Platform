# P2.3 (VCP sequencing) + P2.4 (shares / market cap) — Implementation Note

**Status:** implemented, signed off, committed as
`module11_features_v04_vcp_marketcap_stable`.
**Date:** 2026-07-10.
**Schema:** `FEATURE_SCHEMA_VERSION` `features_v03` → **`features_v04`**;
`ticker_fundamentals` gains `shares_outstanding`. Reinit, not migrate.

---

## P2.4: the scoping note's central premise was inverted

The coder note said *"`edgar_provider.py:442` already pulls diluted
shares-outstanding … market-cap path is low-effort."*

Line 442 is **a comment saying the opposite**:

> Signal 7 (no new share issuance) is intentionally omitted: reliably sourcing
> weighted-average diluted shares from XBRL needs yet another concept alias family
> (`WeightedAverageNumberOfDilutedSharesOutstanding`) with its own filer
> inconsistency; rather than fabricate a 9th signal on shaky data…

Nothing fetched it. There was no `_CONCEPT_SHARES`. P2.4 therefore required
adding the exact concept family the code documents as untrustworthy — not a
"reuse what's already there" change.

Three further corrections, all confirmed before implementing:

1. **Weighted-average diluted shares is the wrong quantity.** It is a period
   *average* over dilutive instruments. Market cap needs an instantaneous common
   share count. Implemented against `dei:EntityCommonStockSharesOutstanding` —
   the cover-page figure, in the `dei` namespace (the provider previously read
   only `us-gaap`). Taken from the freshest filing of **any** form (10-K *or*
   10-Q), which is fresher than the last annual and no less point-in-time.

2. **`close_adj` is the wrong price.** `market_cap = shares_outstanding ×
   close_raw`. The adjusted series is retro-restated as later splits/dividends
   occur — that is what `daily_prices.adjustment_factor` / `mutation_flag` and
   M10 exist for. Using it would multiply a split-adjusted price by an unadjusted
   share count *and* embed corporate actions that had not happened as of the bar:
   a look-ahead leak, and a stored value that silently changes on each backfill.

3. **Step ordering forbade the note's storage plan.** `fundamentals_refresh` is
   `STEP_NAMES[3]`; `price_ingestion` is `STEP_NAMES[4]`. When `_step_fundamentals`
   runs there is no `daily_prices` row for `run_date`, so `market_cap` cannot be
   computed there. Resolved as agreed: `shares_outstanding` → `ticker_fundamentals`
   (filing-grain, point-in-time); `market_cap` → `daily_features` (M11 runs after
   price ingestion and already holds `close_raw`), folded into the `features_v04`
   bump P2.3 needed anyway.

---

## Two live point-in-time bugs found in the path about to be backfilled

### 1. The yfinance fallback wrote today's fundamentals into historical dates — FIXED

`_build_default_yfinance_fallback` called `yf.Ticker(ticker).info` — *current*
`trailingPE`, `earningsQuarterlyGrowth`, `debtToEquity` — and stamped whatever
historical `as_of_date` it was handed. `Ticker.info` has no historical
addressing; it can only answer "what is true now".

On the planned multi-year backfill, **every ticker whose SEC fetch failed would
have had present-day fundamentals written against every historical
`as_of_date`.** That is the Phase 0 look-ahead class, and it would have silently
contaminated the training/diagnostics data the whole batch exists to produce.

Fixed via `fallback_can_serve(as_of_date, today)`: the fallback declines dates
more than `_FALLBACK_MAX_STALENESS_DAYS` (7) in the past, and any future date.
Declining yields **no row**, which every consumer already treats as "no coverage,
no adjustment". Absence is honest; a wrong value is not.

A `today_fn` seam was added for testability. One existing test
(`test_yf_module_is_used_to_build_default_fallback`) asserted the fallback served
`date(2024, 6, 1)` — i.e. it *encoded the bug*. Its intent was to prove `yf_module`
wiring, so it now pins `today_fn` to that date; the restriction has its own tests.

The SEC path itself was already clean: `extract_annual_series` correctly requires
both `end <= as_of_date` **and** `filed <= as_of_date`.

### 2. `valuation_band` has never been populated on the SEC path — FLAGGED, NOT FIXED

`price_lookup` is documented as *"Omitted in production by default;
`valuation_band` then reports `"unknown"`."* It is wired nowhere outside the
provider's own tests. `_VALUATION_BAND_QUALITY` excludes `"unknown"`, so the band
is silently dropped from the quality mean.

Combined with `insider_trade_flag` and `institutional_ownership_delta` being
permanently `None`, **Phase 4 fundamentals quality runs on 4 of its 5 nominal
fields** (Piotroski, Altman, EPS growth, leverage). Not in P2.3/P2.4 scope, and
it interacts with the step-ordering problem above (a `price_lookup` for `run_date`
would find nothing, since prices are ingested later). Logged for a decision.

---

## P2.3: design note held up; two real properties surfaced while building

`_compute_base` / `_compute_swing_pivots` / `_BASE_MAX_DURATION` all existed as
described. The feature is implemented as designed. Two things the note flagged as
risks turned out to be real, and are now documented and tested rather than papered
over:

**The base window systematically clips the deepest, earliest leg.**
`_find_base_window` picks the longest *low*-true-range run. A genuine VCP's first
contraction is its deepest, so its bars carry the largest true range and are the
first to fall outside the window. Measuring strictly inside the window (as the
note specified) therefore usually sees legs 2..n, not leg 1.

**A peak on the window's opening bar cannot be pivot-confirmed**, because
confirmation needs `k` neighbours on both sides *inside* the window. That costs
another leg at the boundary.

Both are inherent to "measure inside the detected base", not defects. Neither
breaks discrimination — the score still separates a coil from a flat base by a
wide margin — but they mean `vcp_sequence_score` is a *conservative* reading, and
the eventual threshold tuning must be done against this behaviour, not against an
idealized leg count. Documented in `01c` and in the module docstring.

**Pivot sensitivity, as predicted.** `_compute_swing_pivots` was unusable here:
it is capped to the last `_SWING_LOOKBACK` (20) bars, returns prices rather than
indices, and confirms with `k=2`, which under-counts the small swings inside a
tight base. Implemented `_base_scoped_pivots` with `k=1`, window-bounded.

**Flat-base discrimination required a material-step test.** A naive
"non-increasing depth" rule scores a flat base identically to a coil, since equal
depths satisfy `<=`. Each leg must therefore be at least `_VCP_MIN_CONTRACTION`
(10%) shallower than the prior. Measured on synthetic fixtures: **VCP 86.3 vs flat
base 25.0**, where the flat base's entire score is the volume term's "did not
rise" credit (`100 × _VCP_W_VOLUME`) — pinned by a test so the number can't drift
unexplained.

**Refactor to avoid drift.** `_compute_base` and the VCP scorer must agree on
where the base is, so window detection was extracted into `_find_base_window` and
`_compute_base` now calls it. Verified behaviour-preserving (full v02/v03 feature
suites green before adding anything on top), and a test asserts `range_duration`
still equals the shared window's length.

---

## Dormancy

Both fields are landed and read by nothing. A test walks
`app/services/{screening,analysis,proposal}` and fails if either column name
appears — so wiring one into a validator without an explicit decision breaks the
build rather than quietly changing trade behaviour.

`enforce_compression_floor` and the existing `atr_compression_score` /
`volume_dry_up_score` checks are untouched.

---

## Golden diffs

- **market_cap** — identical prices, one M11 run with no `ticker_fundamentals`
  row and one with. `market_cap` goes `NULL → shares × close_raw`; every other
  `daily_features` column is byte-identical (`calculated_at`, a run timestamp,
  excluded). Purely additive, proven rather than asserted.
- **vcp_sequence_score** — a pure function of price/volume; independence from
  `atr_compression_score` / `volume_dry_up_score` is proven structurally (the
  function is handed a frame containing neither column) and by pollution (adding
  those columns with absurd values does not change the output).

---

## Reinit, not migrate

`daily_features` is derived and rebuilt by M11 from `daily_prices`; column
additions change `CREATE TABLE` only. `ticker_fundamentals.shares_outstanding` is
likewise only populated going forward. Both land before the planned wipe/backfill,
which is what actually materializes them. Older-version feature rows stay frozen
and are never back-filled — same policy as `v01→v02` and `v02→v03`.

Version-bump housekeeping done: `constants.FEATURE_SCHEMA_VERSION`,
`schema_manager` DDL + module docstring, `01a`, `01b`, `01c`, `M11` spec,
`CLAUDE.md`, and the four tests that pinned the literal `"features_v03"`
(`test_schema_manager`, `test_feature_engine_v02`, `test_feature_engine_v03`,
`test_step3_universal_eligibility`).

---

## Backlog (not implemented)

1. **`valuation_band` is dead on the SEC path** (above). Deciding it needs the
   `price_lookup`/step-ordering question answered together.
2. ~~**`ticker_fundamentals` has no DDL entry in `01b_SCHEMA_AND_DATA.md`**~~ —
   pre-existing Phase 4 spec drift, noticed while adding `shares_outstanding`.
   Same class as `01c` never documenting the fundamentals term (fixed in P2.5).
   **Fixed in this commit**; the entry also records that `as_of_date` is an
   observation date, not a filing period end, which nothing previously stated.
3. **True free float** remains data-blocked, exactly as the original scoping note
   said. `shares_outstanding` ≠ free float. Unchanged.
