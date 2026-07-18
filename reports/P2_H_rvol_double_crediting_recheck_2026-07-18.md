# P2 Item H — Re-verify M15 RVOL Double-Crediting Backstop

**Date:** 2026-07-18
**Scope:** Read-only investigation. No code changes.
**No writes to `prod`.** No changes to `step5_proposal_engine.py` or any
other live module. No pipeline rerun — pure Python recomputation against
already-stored `daily_features`/`daily_prices`/`step4_analysis`/
`step5_proposals`/`setup_configs`/`risk_label_config` data, same method as
P2-F/P2-G, using the **actual production functions** imported from
`step5_proposal_engine.py` (not a reimplementation).

**Headline finding:** the coder note's premise conflates two states. The
originally-flagged "RVOL double-crediting" mechanism (`m15_double_credit_bug_finding.md`
Cause 1) **was already fixed and committed** on 2026-07-08 (`00867ae`,
`module15_score_standardization_stable`) — it is not dormant. That fix
holds up on the real 06-18/06-26 high-RVOL data (§3a). But a **second,
narrower, explicitly-deferred residual** from the same fix effort
(`ticker_best` dedup comparing un-normalized raw scores) is still live in
the code today, was never fixed, has no dedicated test, and — now that real
multi-route high-RVOL data exists — is empirically manifesting at
meaningful scale: 81/407 (19.9%) and 61/481 (12.7%) of multi-routed
tickers on the two dates would get a different dedup winner under a
family-normalized comparison (§3b).

---

## 1. The actual mechanism, with code citations

**Cause (root):** `_confirmation_score_raw()` (`step5_proposal_engine.py:873-876`)
is a pure RVOL proxy — `clamp(rvol * 50)` — and feeds into
`_proposal_score_raw()` (`:879-924`) at weight `_W_CONFIRMATION = 0.15`
(`:143`). Only `breakout` hard-gates RVOL at Step 4
(`min_rvol_breakout`, `rvol_is_hard=True`); pullback/trend_continuation are
RVOL soft-only (AD-22.23). Applying the identical absolute RVOL-derived
term to every setup type means breakout survivors — already pre-filtered
for high RVOL by the Step 4 hard gate — get credited for that same signal
a second time in Step 5's composite score, systematically inflating their
score relative to other setup types with comparable underlying quality.

**The fix, two parts, both present in current code:**

- **Part A (source normalization, `:1238-1333`)** — before the composite
  formula runs, `confirmation_score` is percentile-ranked (`_percentile_rank`,
  `:860-870`) within each `setup_type`'s own same-day distribution of
  *all* passed candidates:
  ```python
  confirmation_by_type: dict[str, list[float]] = {}
  for a in analyses:
      if a["setup_passed"]:
          ...
          confirmation_by_type.setdefault(st, []).append(_confirmation_score_raw(a, feat))
  ...
  confirmation_score = _percentile_rank(
      confirmation_by_type.get(setup_type) or [], _confirmation_score_raw(a, feat)
  )
  ```
- **Part B (defense-in-depth, global merge, `:1480-1501`)** — the
  resulting composite `proposal_score_raw` is *also* percentile-ranked per
  `setup_type`, into a transient `proposal_score_ranked` field, before the
  cross-setup-type sort/selection:
  ```python
  ranked_by_type: dict[str, list[float]] = {}
  for item in ticker_best.values():
      ranked_by_type.setdefault(item["setup_type"], []).append(item["proposal_score_raw"])
  ...
  item["proposal_score_ranked"] = _percentile_rank(
      ranked_by_type.get(item["setup_type"]) or [], item["proposal_score_raw"]
  )

  def _sort_key(x: dict) -> tuple:
      d = 0 if x["disposition"] == DISPOSITION_BUY else 1
      return (d, -x["proposal_score_ranked"], -(x["estimated_rr"] or 0.0), x["ticker"])
  ```
  `proposal_score_raw`/`proposal_score_final` (the persisted, audit-trail
  columns) keep their pre-fix meaning; `proposal_score_ranked` drives
  ranking/selection only and is not persisted.

Both blocks are present, unchanged, and match the memory record
(`m15_double_credit_bug_finding.md`) exactly — confirmed by direct
line-by-line reading of the current file, not by trusting the memory.

**The residual, explicitly deferred, still in current code
(`:1469-1478`):**

```python
# Dedupe multi-route
ticker_best: dict[str, dict[str, Any]] = {}
rejected_items: list[dict[str, Any]] = []
for item in enriched:
    if not item["rankable"]:
        rejected_items.append(item)
        continue
    t = item["ticker"]
    if t not in ticker_best or item["proposal_score_raw"] > ticker_best[t]["proposal_score_raw"]:
        ticker_best[t] = item
```

This loop runs **before** Part B (`:1489` onward). When a ticker qualifies
for two or more `setup_type`s the same `signal_date` (a real, common
scenario — M13 routes setup types independently), the winning route is
picked by comparing raw, un-normalized `proposal_score_raw` — the exact
quantity Part B exists to correct. Part B's `ranked_by_type` pre-pass then
only iterates `ticker_best.values()` (the post-dedup survivors), so the
discarded route's score never enters any family-normalized comparison at
all. This is documented in
`scratchpad/m15_ticker_best_dedup_prefamily_percentile_backlog.md` as a
backlog item split out of the 2026-07-08 fix, "not scheduled."

**Observability gap (confirmed structurally, not just per the backlog
note):** a losing-but-rankable route is neither in `all_ranked` nor
`rejected_items` — `rejected_items` only collects items where
`rankable=False` (i.e., `disposition == REJECTED`), and a dedup loser can
easily be `rankable=True` (e.g. `WATCHLIST_ONLY`) yet still lose the raw
comparison. Such a route gets **no row at all** in `step5_proposals`. Only
`step4_analysis` (which never dedupes) retains it.

---

## 2. Existing test/safeguard status

- **Part A/B (the fixed mechanism) — tested and passing.**
  `TestDoubleCreditRedistribution::test_no_single_setup_type_dominates_final_list`
  (`tests/test_step5_proposal_engine.py:2353`) seeds three setup-type
  families with equal `setup_score=70.0` but breakout's RVOL clustered high
  (matching its own hard gate) and pullback/trend_continuation's RVOL
  deliberately lower — the exact double-credit scenario — and asserts no
  single setup_type exceeds 60% of the final selected list. Ran it:

  ```
  $ python -m pytest tests/test_step5_proposal_engine.py -v -k "DoubleCreditRedistribution or MultiRouteDedupe"
  tests\test_step5_proposal_engine.py ..                                   [100%]
  2 passed, 144 deselected in 13.00s
  ```
  Passes today.

- **Dedup residual — no dedicated safeguard.**
  `TestMultiRouteDedupe::test_best_setup_type_selected_per_ticker`
  (`tests/test_step5_proposal_engine.py:1342`) only asserts that exactly
  **one** proposal row is written for a multi-routed ticker — it does not
  assert *which* route should win, so it cannot catch a raw-vs-ranked
  disagreement. The backlog note itself proposes but does not implement
  the missing test ("a `TestMultiRouteDedupe` case where the raw-best route
  and the ranked-best route disagree, asserting the ranked-best route
  wins"). No such test exists in the current suite (confirmed by reading
  the full `TestMultiRouteDedupe` class — one test only).

---

## 3. Empirical check — real 06-18/06-26 data

Pulled `step4_analysis`/`step5_proposals`/`daily_features`/`daily_prices`/
`setup_configs`/`risk_label_config` for the two dates, read-only, and
recomputed `proposal_score_raw` for **every** passed candidate using the
actual imported production functions (`_compute_stop_target`,
`_compute_estimated_rr`, `_confirmation_score_raw`, `_percentile_rank`,
`_proposal_score_raw`, `_parse_risk_label_config`), including the Fix-3
resistance-cap-on-`estimated_rr` step
(`step5_proposal_engine.py:1312-1319`) that the real code also applies.

**Fidelity check (byte-exact verification before trusting any conclusion
from this):** compared the recomputed `proposal_score_raw` against the
actual stored value for all 510 single-route (unambiguous) tickers on
2026-06-18 — **510/510 exact matches** (`max diff = 0.0`). Then compared
the recomputed *dedup winner's* score against the real stored winner for
all multi-routed tickers on both dates — **0/407 and 0/481 mismatches**.
The recomputation is a faithful, verified stand-in for the real formula,
not a parallel approximation.

### 3a. Part A/B (the fixed mechanism) — holding up on real data

```
2026-06-18: selected_flag counts = {breakout: 5, pullback: 6, trend_continuation: 8, consolidation_base: 1}  (n=20)
  max single-setup_type share: 40.0%
2026-06-26: selected_flag counts = {breakout: 11, trend_continuation: 9}  (n=20)
  max single-setup_type share: 55.0%
```

Both dates stay under the fix's own 60% acceptance bar, and multiple setup
types are represented in the final list both days. **This is the first
verification of the Part A/B fix against real high-RVOL production data**
— the original fix was verified only via a synthetic fixture
(`TestDoubleCreditRedistribution`). It holds up.

### 3b. Dedup residual — empirically manifesting, not just theoretical

Multi-routed-and-passed tickers (2+ `setup_type`s independently passing
Step 4 the same `signal_date`) are **not rare** in real high-RVOL data,
contrary to the backlog note's framing ("narrower blast radius... only
affects tickers with genuine same-day dual-qualification"):

```
2026-06-18: 407 multi-routed tickers  (breakout passed=500, pullback=305, trend_continuation=544, consolidation_base=7)
2026-06-26: 481 multi-routed tickers  (breakout passed=777, pullback=176, trend_continuation=670, consolidation_base=16)
```

For each, compared the CURRENT dedup outcome (raw `proposal_score_raw`,
matching the real code exactly — verified in the fidelity check above)
against an alternative dedup using **pre-dedup family-normalized
percentile rank** — precisely the backlog note's own proposed fix
direction (percentile rank computed across *all* passed candidates per
`setup_type`, before dedup, rather than only the post-dedup survivors):

```
2026-06-18: 81 / 407 (19.9%) multi-routed tickers get a DIFFERENT winning route
            Percentile-point gap among flips: min=0.4  median=5.5  max=27.6
2026-06-26: 61 / 481 (12.7%) multi-routed tickers get a DIFFERENT winning route
            Percentile-point gap among flips: min=0.4  median=9.4  max=20.1
```

Sample flips (06-18) — in most, the setup_type that dedup currently
discards would have looked *stronger* relative to its own family than the
one it lost to on raw terms:

```
ARWR: raw_winner=trend_continuation  ranked_winner=breakout
  breakout:            raw=43.08  ranked=33.3%ile
  trend_continuation:  raw=45.71  ranked=25.8%ile
ABCB: raw_winner=trend_continuation  ranked_winner=breakout
  breakout:            raw=50.85  ranked=81.2%ile
  trend_continuation:  raw=52.48  ranked=69.1%ile
```

In both examples, `trend_continuation` wins on raw score (higher absolute
number) but `breakout` is the stronger candidate *relative to its own
same-day breakout population* — exactly the family-comparison distortion
Part A/B was built to correct, still present at the dedup step because
dedup runs before either normalization pass sees these candidates.

**Median gaps of 5.5–9.4 percentile points (not marginal ties)** indicate
this isn't noise at the boundary — the discarded route is often
meaningfully, not marginally, more competitive within its own family than
the raw-score comparison credits it for.

Not measured in this pass (would require replicating `_compute_risk_score`/
`_assign_disposition`/diversity-capping, a larger recomputation than this
investigation's scope): how many of these 81/61 flipped tickers would
actually change the **final selected top-20 list**, versus only affecting
mid-table ranking that never reaches selection. That's the natural next
question for scoping a fix, not answered here.

---

## 4. Conclusion

**Is the originally-flagged issue (Cause 1, confirmation_score
double-credit) an active problem now that high-RVOL breakout data exists?
No — it is fixed, tested, and confirmed holding up on real 06-18/06-26
data (§3a).** The coder note's framing of this as a still-dormant backstop
was based on stale project records; `m15_double_credit_bug_finding.md`
already documents the 2026-07-08 commit, and this investigation confirms
the fix is present in the current file and behaving correctly on the
exact kind of real high-RVOL data the note anticipated.

**Is there a related, currently-live gap? Yes — the deliberately-deferred
`ticker_best` dedup residual (same root cause class, split out of the
2026-07-08 fix as an explicit backlog item) is empirically manifesting
now, at a scale (407/481 candidate population, 13-20% flip rate, 5.5-9.4
median percentile-point gaps) too large to keep calling narrow.** This
was reasonably deprioritized when framed as a rare edge case; the same
high-RVOL campaign data that motivated re-checking Cause 1 also shows the
dedup gap is not rare. Per instructions, no fix is proposed or implemented
here — this is scope/severity information for that future decision, not
a recommendation on it.

---

## 5. Anomalies (verbatim)

- Initial recomputation attempt showed high fidelity mismatches (~50%)
  against real stored `proposal_score_raw` values. Root cause, found via
  debugging: (1) `step5_proposals` has one row per `(ticker, setup_type)`,
  not per ticker — a naive `{ticker: score}` dict silently collapsed
  multiple rows per ticker (winner + any independently-rejected routes),
  picking whichever row the query happened to return last; (2) the
  recomputation was initially missing the Fix-3 resistance-cap-on-
  `estimated_rr` step (`step5_proposal_engine.py:1312-1319`), which
  produces a clean, diagnostic +25-point discrepancy whenever it flips
  `_rr_score`'s bucketed output by a full step (`_W_RR=0.25 * 100`). Both
  fixed in the investigation script; final fidelity check is exact
  (0 mismatches across 510 + 407 + 481 candidates). Neither issue reflects
  a bug in `step5_proposal_engine.py` — both were errors in this
  investigation's own recomputation, caught and corrected before drawing
  any conclusion from the data.
- Config history check: `setup_configs`/`risk_label_config` each have
  exactly one row (created 2026-07-14, the prod-rebuild date) — the same
  config was active both on the historical signal dates (06-18, 06-26)
  and at investigation time, so no config-version mismatch risk for this
  read-only recomputation.

## No writes, no code changes

Read-only investigation and read-only recomputation only
(`step5_proposal_engine.py` read via direct import of its pure functions,
not modified; `data/duckdb/prod.duckdb` opened `read_only=True`). No DB
writes, no pipeline rerun, no commit.
