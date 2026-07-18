# P1 — Pre-Dedup Family-Normalized Ranking Fix (`ticker_best` Dedup)

**Date:** 2026-07-18
**Scope:** `app/services/proposal/step5_proposal_engine.py` — the
`ticker_best` dedup comparison key only. No schema change.
`proposal_score_raw`/`proposal_score_final` (persisted, audit-trail
columns) keep their existing meaning — confirmed unchanged (§3, UPS/UA
detail dumps show the same raw scores as before the fix, just attached to
a different winning route).
**No commit — diff delivered, stop, per current policy.**

---

## 0. Background

Confirmed by the P2-H investigation + impact-measurement follow-up
(2026-07-18; backlog raised to P1,
`scratchpad/m15_ticker_best_dedup_prefamily_percentile_backlog.md`): the
`ticker_best` multi-route dedup loop picked a winner by raw
`proposal_score_raw`, computed *before* Part B's post-dedup
family-normalized percentile ranking — letting the dedup step be swayed by
absolute-score differences between setup types, the same distortion class
Part A/B were built to correct downstream. 142 of 2,048 multi-routed
candidates across 2 real high-RVOL dates got a different dedup winner
under a fair comparison; 2 of those changed the actual final selected
list (`UPS` — thesis/setup_type change; `UA` — silent drop).

---

## 1. Diff

Inserted immediately before the existing "Dedupe multi-route" block
(`step5_proposal_engine.py`, was line ~1469):

```diff
+        # P1 (ticker_best dedup fix,
+        # m15_ticker_best_dedup_prefamily_percentile_backlog.md): percentile-
+        # rank proposal_score_raw within each setup_type's full rankable
+        # population BEFORE dedup runs, so the ticker_best comparison below
+        # is itself family-normalized rather than comparing absolute raw
+        # scores across setup types with different score distributions --
+        # the same distortion class Part A/B correct downstream of dedup,
+        # left uncorrected at the dedup step itself until now (P2-H
+        # investigation, 2026-07-18: 142/2048 multi-routed candidates across
+        # 2 real high-RVOL dates got a different dedup winner under this
+        # comparison; 2 changed the final selected list).
+        # Distinct from Part A (confirmation_score, a per-candidate input
+        # feature, normalized before the composite formula) and Part B
+        # (proposal_score_raw re-ranked over the POST-dedup ticker_best
+        # survivors, drives the final cross-setup-type sort/selection) --
+        # this is a third, separate percentile-rank pass, over the PRE-dedup
+        # population (every rankable route, multiple per ticker allowed).
+        # Transient/comparison-only, like proposal_score_ranked below;
+        # proposal_score_raw/_final keep their existing stored (audit-trail)
+        # meaning unchanged -- this only changes which route's row is kept.
+        predup_ranked_by_type: dict[str, list[float]] = {}
+        for item in enriched:
+            if item["rankable"]:
+                predup_ranked_by_type.setdefault(item["setup_type"], []).append(item["proposal_score_raw"])
+        for _scores in predup_ranked_by_type.values():
+            _scores.sort()
+        for item in enriched:
+            if item["rankable"]:
+                item["proposal_score_predup_ranked"] = _percentile_rank(
+                    predup_ranked_by_type.get(item["setup_type"]) or [], item["proposal_score_raw"]
+                )
+
         # Dedupe multi-route
         ticker_best: dict[str, dict[str, Any]] = {}
         rejected_items: list[dict[str, Any]] = []
         for item in enriched:
             if not item["rankable"]:
                 rejected_items.append(item)
                 continue
             t = item["ticker"]
-            if t not in ticker_best or item["proposal_score_raw"] > ticker_best[t]["proposal_score_raw"]:
+            if (
+                t not in ticker_best
+                or item["proposal_score_predup_ranked"] > ticker_best[t]["proposal_score_predup_ranked"]
+            ):
                 ticker_best[t] = item
```

**Untouched, per instructions:**
- Part A (`confirmation_by_type`/`confirmation_score` normalization,
  earlier in `_build_rows`) — not modified.
- Part B (`ranked_by_type`/`proposal_score_ranked` post-dedup pass and
  `_sort_key`) — not modified; still runs exactly as before, now over the
  corrected `ticker_best` survivors.
- `min_atr_stop_floor`, `_compute_risk_score`, `_assign_disposition`,
  diversity-cap thresholds (`_apply_hard_cap`/`_apply_soft_penalty`) —
  not modified.
- No new field is written to any persisted column;
  `proposal_score_predup_ranked` lives only on the in-memory `item` dict
  used during `_build_rows`, exactly like `proposal_score_ranked` — the
  final `rows.append({...})` block builds its dict by explicit field
  name, so neither transient field can leak into `step5_proposals`.

---

## 2. Tests — new, all passing

`tests/test_step5_proposal_engine.py`, `TestMultiRouteDedupe` — new test
`test_family_normalized_route_wins_not_raw` (the exact case the backlog
note called out as missing): a ticker `MULTI` qualifying for both
`breakout` (`setup_score=55`) and `trend_continuation` (`setup_score=60`,
higher raw score) the same day. `breakout`'s family that day is a
low-scoring cluster (30–36), so `MULTI`'s 55 sits at the top of it;
`trend_continuation`'s family is a high-scoring cluster (80–86), so
`MULTI`'s 60 sits at the bottom of it — structurally identical to the real
`ARWR`/`ABCB` examples from the investigation. Asserts the winning route
is `breakout` (family-top) despite its lower raw score, not
`trend_continuation` (raw-higher but family-bottom).

```
$ python -m pytest tests/test_step5_proposal_engine.py -v -k "MultiRouteDedupe or DoubleCreditRedistribution"
tests\test_step5_proposal_engine.py ...                                  [100%]
3 passed, 144 deselected in 14.52s
```

All 3 pass: the new test, plus `test_best_setup_type_selected_per_ticker`
(pre-existing dedup structural test) and
`test_no_single_setup_type_dominates_final_list` (Part A/B's own
regression guard) — confirming the fix operates upstream of Part B
without disturbing it, not just assuming so.

---

## 3. Known-answer reproduction — UPS / UA, against the real fixed code

Called `Step5ProposalEngine()._build_rows(...)` **directly** — the actual,
now-fixed production method, not a reimplementation — with real historical
`analyses`/`features_map` read from `data/duckdb/prod.duckdb` (read-only,
via the module's own `_SQL_READ_ANALYSES`/`_ANALYSIS_COLS`/
`_SQL_READ_FEATURES`/`_FEATURE_COLS`) for 2026-06-18 and 2026-06-26. No
pipeline rerun: `_build_rows` is pure compute — `propose()`'s `_write()`
step (the only part that touches the DB for writes) was never called.

**`UPS` (06-18)** — now:
```
setup_type=trend_continuation  disposition=WATCHLIST_ONLY
proposal_score_raw=73.21  (same value as before the fix — persisted
  columns unchanged, only which route's row this is changed)
raw_rank=13  diversified_rank=12  selected_flag=True
```
Matches the impact-measurement report's predicted counterfactual exactly:
`trend_continuation`, `WATCHLIST_ONLY`, selected at rank 12 — not today's
pre-fix actual (`pullback`, `BUY`, rank 3).

**`UA` (06-26)** — now:
```
setup_type=trend_continuation  disposition=BUY
proposal_score_raw=71.53
raw_rank=6  diversified_rank=6  selected_flag=True
```
Matches the predicted counterfactual: `UA` is now selected via
`trend_continuation` (clearing the diversity cap that blocked its
`breakout` route), rather than being silently dropped as it was before
the fix.

---

## 4. Full 142-flip re-validation (not just the 2 known cases)

Loaded the complete list of 142 previously-identified flips (81 from
06-18, 61 from 06-26) from the prior investigation's saved detail, and for
each, compared the fixed `_build_rows()`'s actual winning `setup_type`
against the `ranked_winner` value already computed and verified in that
investigation:

```
2026-06-18: Mismatches vs predicted ranked_winner: 0 / 81
2026-06-26: Mismatches vs predicted ranked_winner: 0 / 61

ALL 142 FLIPS MATCH PREDICTED RANKED WINNER
```

**Zero mismatches across all 142.** The fix is complete, not just correct
for the two consequential examples already known — every previously-flipped
candidate now resolves to the family-normalized winner.

---

## 5. Full module test suite

```
$ python -m pytest tests/test_step5_proposal_engine.py -v
147 passed in 92.99s (0:01:32)
```

147/147 pass (146 pre-existing + 1 new). No regressions.

**Broader regression check** (files that import/exercise
`Step5ProposalEngine`, for extra confidence on a scheduled-priority
production fix beyond what was strictly requested):

```
$ python -m pytest tests/test_step5_proposal_engine.py tests/test_p2_5_orchestration_wiring.py \
    tests/test_config_read_coverage.py tests/test_phase6_orchestrator.py \
    tests/test_simulation_engine.py tests/test_outcome_queue.py -q
```
No `FAILED`/`ERROR` lines anywhere in the output (grepped explicitly);
all non-passing entries are pre-existing `SKIPPED` (M16/M17 integration
tests pending full-schema fixtures, unrelated to this change).

---

## 6. Anomalies (verbatim)

- The pytest console output in this shell session again omitted its
  trailing `X passed in Ns` summary line for the broader multi-file
  regression run (same PowerShell-redirection quirk documented in earlier
  reports, e.g. `step4_stopfloor_gate_fix_2026-07-18.md` §4) — confirmed
  a non-issue by explicitly grepping the full captured output for
  `FAILED`/`ERROR` (zero matches) rather than relying on the summary line.
  The single-file run (`test_step5_proposal_engine.py -v`) printed its
  summary normally (`147 passed`).
- None otherwise. The fix behaved exactly as designed on the first fully
  assembled run — no fidelity surprises this time, likely because both
  the fix itself and its validation reused the same real, already-verified
  production functions and data pipeline from the prior two investigations
  rather than introducing new recomputation surface area.

## No commit

Diff is in `app/services/proposal/step5_proposal_engine.py` and
`tests/test_step5_proposal_engine.py`, left uncommitted in the working
tree, per current policy.
