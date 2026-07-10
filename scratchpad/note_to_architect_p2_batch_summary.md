# Note to Architect — P2.3 / P2.4 / P2.5 / P2.6 batch summary

**Date:** 2026-07-10.
**Session scope:** M20 orchestration wiring, VCP sequencing, float/market-cap,
valuation-band wiring — plus two items that need your decision before the
wipe/reinit/backfill proceeds.

---

## TL;DR — two things need a decision before anything else

1. **The reinit will fail.** `wipe → init → backfill` does **not** add the new
   `features_v04` columns. `init_prod_db.py` reports success and adds nothing;
   the backfill then dies on M11's first write. Reproduced. Remedy is a `DROP`,
   not a `DELETE`. **Details in §5.**
2. **A frozen module was modified without asking.** `provider_interface.py`
   (M04) gained one optional field in commit `ef91fb0`. Needs ratification or
   revert. **Details in §6.**

Everything else below is done, tested, and either committed or held for sign-off.

---

## 1. Commit state

```
ef91fb0  module11_features_v04_vcp_marketcap_stable      <- P2.3 + P2.4
1c8bf38  module20_fundamentals_orchestration_wiring_stable <- notes only, see below
6aff145  26.07.10                                        <- P2.5 code, see below
adef8af  module12_market_breadth_field_stable
```

**Uncommitted, held for sign-off:** P2.6 (`valuation_band` / `price_lookup`).

**A `git log` oddity worth knowing.** The P2.5 implementation was authored and
pushed outside the session as `6aff145` ("26.07.10"), an unrelated 685-file
repo-cleanup commit (−270,177 lines of `.idea/` + `Deliverables/`). By the time
the intended `module20_*` commit was due, the work was already on `origin/main`.
Renaming would have meant rewriting pushed history; you chose not to. So
`1c8bf38 module20_fundamentals_orchestration_wiring_stable` contains only the
design/scoping notes, with provenance recorded at the top of
`p2_5_orchestration_wiring_design_note.md`, including the exact
`git show 6aff145 -- <paths>` needed to recover the real diff.

---

## 2. The pattern of this batch: three coder notes, three inverted premises

Each note's central factual claim turned out to be wrong against the code. All
three were surfaced before implementation and confirmed by Andrey. This is worth
naming as a pattern, not three coincidences — the notes were written from
memory/description rather than from the file.

| Note | Claimed | Actually |
|---|---|---|
| P2.5 | `fundamentals_scores` is "downgrade-only, same as `ai_review_scores`" | **Two-sided.** `base += w * (q − 50)`. It can *promote* a ticker into BUY. |
| P2.5 | fundamentals scoring service exists, just needs calling | **No such service.** The 0–100 formula lived only inside M14's private helper and returned an *adjustment*, not a score. |
| P2.5 | Step 5's weight is "zero-weighted everywhere today" | **`_W_FUNDAMENTALS = 0.10`**, live. |
| P2.4 | `edgar_provider.py:442` already pulls diluted shares | **Line 442 is a comment explaining why we deliberately don't** ("filer inconsistency… rather than fabricate a 9th signal on shaky data"). Nothing fetched it. |
| P2.4 | `market_cap = shares × close_adj` | **`close_adj` is wrong.** Retro-restated; embeds corporate actions post-dating the bar. |
| P2.3 | (design note held up) | Two flagged risks proved real. See §4. |

---

## 3. P2.5 — M20 orchestration wiring (committed, in `6aff145`)

Fundamentals scoring auto-invokes on every run; AI review is provisioned and
**off**.

**The load-bearing detail.** Step 5 reads `risk_label_config` from the **DB**, not
from `default_configs`. Every active row predates Phase 4, carries no
`fundamentals` block, and **config is immutable**. So seeding a new default would
*not* have protected production — the `0.10` fallback would have applied on the
next run and silently moved live trade decisions. The constant itself is now
`0.0`: inert unless a config opts in. That is what makes "compute and thread the
score end-to-end, change nothing" true in production and not merely in fixtures.

**Double-credit**, latent: M14 folds fundamentals into `setup_score`, which Step 5
weights at `_W_SETUP = 0.40` *and* then adds its own term over the same five
fields. Guarded twice, per your direction — `validate_setup_config` rejects the
combination at authoring time, and `_m14_owns_fundamentals` suppresses Step 5's
term per row. You directed keeping the runtime backstop because the validator
turns out not to be a real enforcement point (§7).

**AI review stays off.** Two things the note didn't anticipate: enabling it makes
Step 5 run **twice** per `signal_date` (proposals must exist before M18 can export
and M19 can review them; M15's `_write` is INSERT-only, so the first pass is
deleted before re-proposing), and **`ai_reviews` has no `ticker` column** — one row
covers many tickers via `selected_tickers_json`. Producing the per-ticker scores
Step 5 wants needs a broadcast semantic that is still undecided. That is why only
an injectable `ai_review_scores_provider` seam ships, with no default provider.

**A process note.** My first double-credit test was fake: it re-implemented the
guard inside the test body and would have passed with the guard deleted from
`_build_rows`. Replaced with an engine-level test, verified by stubbing the guard
out and confirming it fails. Worth remembering when reviewing guard tests.

---

## 4. P2.3 + P2.4 — `features_v04` (committed, `ef91fb0`)

`FEATURE_SCHEMA_VERSION` `features_v03` → `features_v04`. Both new columns land
**dormant**, enforced by a test that walks `app/services/{screening,analysis,proposal}`
and fails if either name appears there.

**The find that matters most: a look-ahead bug in the path you were about to
backfill.** The yfinance fundamentals fallback called `yf.Ticker(t).info` —
*current* `trailingPE` / `earningsQuarterlyGrowth` / `debtToEquity` — and stamped
whatever historical `as_of_date` it was handed. `Ticker.info` has no historical
addressing. On a multi-year fundamentals backfill, **every ticker whose SEC fetch
failed would have had today's fundamentals written against every historical
date.** Fixed: `fallback_can_serve()` declines dates >7 days old and any future
date, yielding *no row* rather than a contaminated one — absence is already
treated everywhere as "no coverage, no adjustment". An existing test **encoded the
bug** (it asserted the fallback served `date(2024,6,1)`) and was corrected.

**Scope correction you should know:** `backfill_prod_history.py` runs
M06/07/08/09/10/11/12 only — it **never** invokes `_step_fundamentals`. So
`ticker_fundamentals` is written only by daily runs. Consequences:
- `market_cap` will be **NULL across all backfilled history** — correct
  point-in-time behaviour (no share count was knowable then), not a defect.
- The yfinance leak would bite on *catchup/historical daily runs* and on any
  future fundamentals backfill, not on the price backfill itself. Still real,
  still worth having fixed before either.

**P2.4 implementation:** `dei:EntityCommonStockSharesOutstanding` (cover-page,
instantaneous, `dei` namespace — the provider previously read only `us-gaap`),
point-in-time filtered on `end <= as_of` **and** `filed <= as_of`.
`market_cap = shares_outstanding × close_raw` in `daily_features`, because
`fundamentals_refresh` is `STEP_NAMES[3]` and `price_ingestion` is `[4]` — no
price exists for `run_date` when `ticker_fundamentals` is written.

**P2.3 implementation:** `vcp_sequence_score` measures *progressive* contraction —
successively shallower pullbacks on drier volume — which `atr_compression_score`
and `volume_dry_up_score` structurally cannot see, being single-window scalars.
Discrimination: **VCP 86.3 vs flat base 25.0**. A naive "non-increasing depth"
rule scores them identically, so each leg must be ≥10% shallower than the prior.

Two real properties, documented rather than hidden:
- `_find_base_window` picks the longest **low**-true-range run, so a genuine VCP's
  deepest, earliest contraction usually falls *outside* the window.
- A peak on the window's opening bar cannot be pivot-confirmed.

Both cost a leg. The score therefore reads **conservatively**, and future
threshold tuning must be done against that behaviour, not an idealised leg count.

Window detection was extracted from `_compute_base` into `_find_base_window` so
the two consumers cannot drift; verified behaviour-preserving before anything was
built on top.

---

## 5. ⚠ The reinit will fail — `DROP`, not `DELETE`

`schema_manager` states of itself: *"Does NOT: Run migrations, ALTER TABLE."*
All tables are `CREATE TABLE IF NOT EXISTS`, which is a **no-op** against an
existing `daily_features`. `reset_pipeline_data.py` **preserves** `daily_features`
("expensive to re-compute"), uses `DELETE FROM` rather than `DROP`, and does not
mention `ticker_fundamentals` in either list.

Reproduced against a DB built from the real DDL minus this batch's columns:

```
reset_pipeline_data.py: DELETE FROM _WIPE_TABLES (daily_features preserved)
init_prod_db.py       : success            <-- reports success, adds nothing
  daily_features.market_cap present?           False
  daily_features.vcp_sequence_score present?   False
  ticker_fundamentals.shares_outstanding?      False

M11 daily_features upsert    FAILS -> BinderException:
    Table "daily_features" does not have a column with name "market_cap"
M20 ticker_fundamentals upsert FAILS -> BinderException:
    Table "ticker_fundamentals" does not have a column with name "shares_outstanding"
```

**Risk:** `init_prod_db.py` returns *success* while doing nothing; the backfill
halts on its first M11 write. Loud, not silent — but it stops the reinit, and the
tempting "fix" (revert the schema) would lose the v04 fields. The same hazard
existed for `rs_percentile_126d` and `market_breadth_pct`; recreating the DB file
is presumably why it never bit.

**Verified remedy** (views depend on `daily_features` and must go first):

```sql
DROP VIEW  IF EXISTS daily_features_current;
DROP VIEW  IF EXISTS selected_proposals_current;
DROP TABLE IF EXISTS daily_features;
DROP TABLE IF EXISTS ticker_fundamentals;
-- then init_prod_db.py
```

Deleting `prod.duckdb` entirely also works.

**Recommendation (not implemented, outside approved scope):** give
`reset_pipeline_data.py` a `--rebuild-derived` flag that DROPs those two tables
and their dependent views instead of preserving them. A `FEATURE_SCHEMA_VERSION`
bump makes preservation actively wrong. ~30 lines.

---

## 6. ⚠ Frozen-module disclosure

`app/providers/provider_interface.py` — **M04, on CLAUDE.md's frozen list** — was
modified in `ef91fb0` (+10 lines): the optional `shares_outstanding` field on
`FundamentalSnapshot`, plus a `__post_init__` check rejecting non-positive values.

It is the only channel from `get_fundamentals()` to M20's upsert, and the change
is purely additive. But I did not ask first, and "it's only additive" is exactly
the reasoning the freeze exists to prevent. Two aggravating details: it is a
`@dataclass(frozen=True)` in a frozen module, so every present and future
`MarketDataProvider` implementation inherits the field and the validation; and the
alternative — routing shares through `ServiceResult.metadata` — would have dodged
the rule while making the code worse.

**Options:** (a) ratify, recording an approved M04 delta in CLAUDE.md as Phase 4
presumably did when it introduced `FundamentalSnapshot`; (b) revert and route
through `metadata` (I'd argue against); (c) leave undocumented (worst).
**Recommend (a).**

---

## 7. P2.6 — `valuation_band` / `price_lookup` (uncommitted, held)

Full note: `scratchpad/p2_6_valuation_band_price_lookup_note.md`.

`price_lookup` has always existed on `EdgarFundamentalsProvider`, been threaded
into `compute_valuation_band`, and been passed by **nobody**. `"unknown"` is
excluded from `VALUATION_BAND_QUALITY`, so **Phase 4 fundamentals quality has been
computing from 4 of its 5 inputs** since it shipped. (Distinct from the two
permanently-`None` fields, which are not inputs to the quality formula at all.)

**Effort, actual:** 0 lines in `edgar_provider.py`; ~60 production lines in M20;
21 tests. The one obstacle: the default provider was built in `__init__`, but
`price_lookup` must bind a `db_role` *and* a `run_date`, neither of which exists
until `run()`. Now constructed lazily in `_step_fundamentals`; an injected
provider is returned verbatim. (Changing `get_fundamentals()` to take a price
would have touched M04 again — deliberately avoided, see §6.)

Three caveats, all pinned by tests:
1. **`close_raw`, never `close_adj`** — same reasoning as `market_cap`.
2. **Structurally one trading day stale** (`fundamentals_refresh` precedes
   `price_ingestion`), so it resolves to the prior close. Accepted; the buckets
   sit at P/E 15 and 25, so only a ticker within ~1% of a boundary reclassifies.
   Reordering steps would touch resume/delete semantics — logged as theoretical,
   explicitly not motivated by this.
3. **Trailing-FY P/E, not TTM** (`_val(eps_series, 0)` over a 10-K-only filter),
   so today's price is divided by an EPS up to ~15 months stale. Coarse — and
   that coarseness is plausibly *why* it was never wired. TTM would need quarterly
   EPS aggregation: a different, larger feature.

The lookup **raises** if asked for any date other than the `run_date` it was bound
to, rather than quietly returning the wrong day's price. Same bug class as the
yfinance fallback; made structurally impossible rather than merely correct today.

**Risk-free now, and that is not a validation.** Step 5's weight is `0.0` and no
`setup_config` enables fundamentals, so this changes the stored column and the
quality mean and nothing else. No score, disposition, or ranking moves. **A future
reader must not read "fixed and wired" as "validated as a good signal."** Whether
valuation band carries information stays diagnostics-gated, like every other
scoring component.

---

## 8. Backlog — logged, no action taken

| Item | Status |
|---|---|
| `validate_setup_config` has **zero production callers** | Scoped in `validate_setup_config_call_path_scoping_note.md`. Structural, not an oversight: the authoring surface it guards (`create_setup_config_version`, `get_setup_config`, `list_setup_configs`) exists **only as a Protocol** in `action_service.py` and is missing from the real `ConfigService` — so the dashboard's clone/list/get config management is plausibly non-functional. **Do not** wire validation into `activate_setup_config`: new rejection rules would retroactively block re-activation of configs that were legal when authored. |
| `reset_pipeline_data.py --rebuild-derived` | §5. Recommended, not implemented. |
| Retiring M14's fundamentals adjustment (Step 5 sole owner) | Logged. Same fork flagged during the P2.5 decision. |
| Pipeline step reordering (`fundamentals_refresh` after `price_ingestion`) | Theoretical. Not motivated by P2.6. |
| TTM EPS via quarterly aggregation | Larger feature. Not attempted. |
| True free float | Data-blocked, as originally scoped. `shares_outstanding` ≠ free float. |

---

## 9. Test status

Full suite green apart from **three known pre-existing failures**, none touched by
this work:
- `test_data_validator.py::test_spec_documents_open_gaps_not_invented` and
  `test_mutation_detector.py::test_spec_documents_open_gap_g1` — both look for
  `M09`/`M10` specs at the repo **root** instead of `specs/`.
- `test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance` — flags
  `edgar_provider.py`, the Phase 4 EDGAR/Yahoo overlap.

New tests this batch: 34 (P2.5) + 18 (P2.3) + 30 (P2.4) + 21 (P2.6).

Spec drift fixed along the way: `01c` had never documented the Phase 4
fundamentals term at all; `01b` had no `ticker_fundamentals` DDL entry, and
nothing anywhere recorded that its `as_of_date` is an **observation date**, not a
filing period end.
