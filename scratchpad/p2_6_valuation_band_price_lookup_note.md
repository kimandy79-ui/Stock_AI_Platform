# P2.6 — `valuation_band` / `price_lookup` wiring: Scoping + Implementation Note

**Status:** implemented, uncommitted, held for sign-off.
**Date:** 2026-07-10.
**Not on the backfill critical path.** Land before the first daily run after reinit.

---

## What was wrong

`EdgarFundamentalsProvider` has always accepted a `price_lookup` callable and
threaded it into `compute_valuation_band` (`edgar_provider.py:891`). **Nothing
ever passed one.** The provider's own docstring says so plainly:

> `price_lookup`: … Omitted in production by default; `valuation_band` then
> reports `"unknown"` rather than reaching into another provider.

`VALUATION_BAND_QUALITY` deliberately has no `"unknown"` key, so the band was
excluded from the mean rather than scored. **Phase 4 fundamentals quality has
been computing from 4 of its 5 inputs** (Piotroski, Altman Z′, EPS growth trend,
leverage ratio — but never valuation band) since Phase 4 shipped.

Note this is a *different* set from the two permanently-`None` fields
(`insider_trade_flag`, `institutional_ownership_delta`): those are not inputs to
`compute_fundamentals_quality` at all. The quality formula has exactly five
inputs, and one of them was dead.

---

## Effort: a wiring fix, not a build

| Piece | Lines | Notes |
|---|---|---|
| `edgar_provider.py` | **0** | param exists, threaded, consumed |
| `_SQL_LATEST_CLOSE_AS_OF` | 6 | latest `close_raw` per ticker, `date <= run_date` |
| `_make_price_lookup` | ~45 (incl. docstring) | prefetch once, return closure |
| `_resolve_fundamentals_provider` | ~8 | lazy default construction |
| `__init__` / `_step_fundamentals` edits | ~4 | |
| Tests | ~215 | new `test_p2_6_valuation_band_price_lookup.py`, 21 tests |

One structural obstacle, and the reason this wasn't a one-liner: the default
provider is constructed in `PipelineOrchestrator.__init__`, but `price_lookup`
must be bound to a `db_role` **and** a `run_date`, neither of which exists until
`run()`. Resolved by constructing the default provider lazily inside
`_step_fundamentals`. An **injected** provider is returned verbatim and never
rebuilt, so every existing DI test keeps working. (Changing
`get_fundamentals(...)` to take a price instead would have touched
`provider_interface.py` — M04, a frozen module. Avoided.)

---

## Three caveats — real limitations, not bugs

### 1. `close_raw`, never `close_adj`

As-reported EPS pairs with an unadjusted price. `close_adj` is retro-restated as
later splits/dividends land (hence `daily_prices.adjustment_factor` and M10's
mutation detection), so a P/E built on it would embed corporate actions
post-dating the bar and would silently change on every backfill. Same reasoning
as `market_cap` in P2.4. Enforced by a test asserting `close_adj` does not appear
in the query.

### 2. Structurally one trading day stale

`fundamentals_refresh` is `STEP_NAMES[3]`; `price_ingestion` is `[4]`. When
`_step_fundamentals` runs, `run_date`'s own bar has **not** been ingested, so the
lookup resolves to the **prior trading day's close**.

Accepted, not worked around. The buckets sit at P/E 15 and 25, so an overnight
move reclassifies a ticker only when its P/E is within roughly 1% of a boundary.
Reordering the pipeline steps to fix this would touch resume/delete semantics
(`_DELETE_*` reset ordering, `resume_from` index arithmetic) — a disproportionate
change for that edge case. **Logged as a theoretical future item, explicitly not
motivated by this.** A test pins the step ordering so the staleness stays visible.

### 3. Trailing-**FY** P/E, not TTM

`compute_valuation_band` reads `_val(eps_series, 0)`, and `extract_annual_series`
filters to `form == "10-K"`. So the divisor is the most recent **full-year**
diluted EPS, which can be up to ~15 months stale by the time it is divided into
today's price.

This is coarse, and **that coarseness is plausibly why the wiring was never done
in the first place** — a band computed from a stale annual EPS is a weak signal,
and the original author may reasonably have judged it not worth reaching across
providers for a price. That is a hypothesis about intent, not something the code
records; stating it so a future reader doesn't assume the omission was an
oversight.

Fixing this properly means TTM EPS from quarterly (10-Q) aggregation — a
different, larger feature. Not attempted. Documented, in the same spirit as
Phase 1.5's approximated RS criteria and Phase 4's two permanently-`None` fields.

---

## Why this is risk-free *now* — and what that does NOT mean

Step 5's `fundamentals_score_weight` is seeded `0.0` (P2.5), and **no**
`setup_config` sets `fundamentals.enabled`, so M14's adjustment is exactly `0.0`
everywhere. Populating `valuation_band` therefore changes:

- the stored `ticker_fundamentals.valuation_band` column, and
- the `compute_fundamentals_quality` mean (now 5 inputs, not 4),

**and nothing else.** No proposal score moves. No disposition changes. No ranking
changes. That is precisely why this is the cheap moment to do it.

> **A future reader must not read "fixed and wired" as "validated as a good
> signal."** Nothing here establishes that valuation band carries predictive
> information, or that a stale-FY P/E bucketed at 15/25 is the right
> parameterisation. It establishes only that the field is now *populated* instead
> of silently absent, so that when diagnostics eventually run an information-
> coefficient analysis on fundamentals quality, they measure the formula that was
> designed rather than a four-fifths subset of it. Whether the signal earns its
> place remains an open, diagnostics-gated question — the same bar every other
> scoring component in this project has to clear.

---

## Degradation behaviour

- `daily_prices` unreadable or missing (e.g. a fresh DB before the first price
  ingestion, or the simulation role): `_make_price_lookup` logs a warning and
  returns `None`; the provider gets no lookup; `valuation_band` is `"unknown"`.
  Exactly the pre-P2.6 behaviour, so a price problem can never fail the run.
- Ticker absent from `daily_prices`, or `close_raw` NULL/non-positive: `None` →
  `"unknown"`.
- Loss-making company (EPS ≤ 0): `"unknown"` even with a valid price. A P/E on
  negative earnings is meaningless, not "cheap". Pinned by a test.
- The returned callable **raises** if asked for a date other than the `run_date`
  it was bound to, rather than silently answering with the wrong day's price —
  that silence is how look-ahead gets in.

---

## Tests

`tests/test_p2_6_valuation_band_price_lookup.py` (21, offline):

- point-in-time — query bounded by `date <= run_date`, parameterised with it, and
  the callable refuses any other date;
- `close_raw` selected, `close_adj` absent from the query;
- band buckets `cheap` / `fair` / `expensive` from a supplied price; `"unknown"`
  on absent price and on non-positive EPS;
- end-to-end through `EdgarFundamentalsProvider` with and without a lookup — the
  latter pins the old `"unknown"` behaviour so the regression stays visible;
- injected provider used verbatim and triggers no price read; default provider
  built lazily with a working lookup;
- both documented limitations pinned (`_ANNUAL_FORM == "10-K"`;
  `fundamentals_refresh` precedes `price_ingestion` in `STEP_NAMES`).

One existing test (`test_default_fundamentals_provider_is_edgar`) asserted eager
construction in `__init__`; updated to assert the resolved default instead, which
is the contract that now matters.

Regression: 319 passed across the orchestrator, provider, P2.4 and P2.5 suites.

---

## Related

- `[[p2_3_p2_4_implementation_note.md]]` — where this gap surfaced, and the
  `close_raw` vs `close_adj` reasoning it shares.
- Backlog, unchanged: pipeline step reordering (theoretical); TTM EPS via
  quarterly aggregation (larger feature); retiring M14's fundamentals adjustment.
