# P2-F Follow-up — Stop-Distance vs. Real Outcome Correlation

**Date:** 2026-07-18
**Scope:** Read-only investigation. No code changes, no fix, no decision.
**No writes to `prod`. No pipeline rerun.**

**Headline finding — this changes the deliverable, so it's stated first:**
**none of the platform's `signal_outcomes` rows are actually matured
40-business-day outcomes yet.** `outcome_status='complete'` means "this
row's own horizon-checkpoint finished computing," not "this trade's full
40bd lifecycle has played out" — and the longest horizon that has *ever*
completed, for any candidate, platform-wide, is 10 business days (~2
calendar weeks). `stop_hit`, `target_hit`, `mfe_40bd_pct`, `mae_40bd_pct`
— the fields that would actually tell you whether a trade worked — are
**0/117 populated, platform-wide**, because those are only computed at the
horizon_bd=40 checkpoint, which has not yet fired for a single candidate.
The correlation analysis as framed in the coder note **cannot be performed
today** — the data it needs doesn't exist yet. What follows is (a) the
evidence for that finding, and (b) an explicitly-labeled, heavily-caveated
look at the 5/10-business-day interim snapshots that *do* exist, which is
not a substitute for the real analysis and should not be read as one.

---

## 1. Why the premise doesn't hold — the mechanism, with code citations

`app/services/outcomes/outcome_queue.py`:

- `outcome_id` is generated per `(proposal_id, horizon_bd)`
  (`_outcome_id_for(row["proposal_id"], horizon_bd)`, line 699) — **not**
  per proposal. A single trade candidate accumulates one `signal_outcomes`
  row *per horizon checkpoint reached so far* (5, 10, 20, 40 business
  days), each with its own `outcome_id`, rather than one row updated in
  place as horizons resolve.
- `status` (lines 691-696):
  ```python
  required = [returns[n] for n in constants.OUTCOME_HORIZONS_BD if n <= horizon_bd]
  status = (
      OUTCOME_COMPLETE if all(v is not None for v in required) else OUTCOME_PARTIAL
  )
  ```
  This is scoped to *that row's own horizon* — a row processed at
  `horizon_bd=5` only requires `returns[5]` to be non-null to be marked
  `'complete'`. It says nothing about whether 10/20/40bd have resolved.
- `mfe_40, mae_40, stop_hit, target_hit` (lines 674-682) are **only
  computed when `horizon_bd == 40`**:
  ```python
  if horizon_bd == 40:
      mfe_40, mae_40, stop_hit, target_hit = self._window_stats(...)
  ```
  For every row produced at `horizon_bd` 5, 10, or 20, these four fields
  are structurally `None` — not missing data, but fields that were never
  computed for that row by design.
- `realized_r_multiple` (line 685) is computed from `exit_close =
  eval_close_adj.get(horizon_bd)` — i.e., "the R-multiple **if you'd
  exited exactly at this horizon's close**," a different number at each
  horizon checkpoint for the same trade, not a final realized P&L.

**Empirical confirmation, platform-wide:**

```sql
SELECT horizon_bd, status, COUNT(*) FROM outcome_tracking_queue GROUP BY horizon_bd, status
```
```
(5,  'done', 70)   (5,  'pending', 46)
(10, 'done', 47)   (10, 'pending', 69)
(20, 'pending', 116)
(40, 'pending', 116)
```
**Zero** `horizon_bd=20` or `40` rows have ever reached `'done'`, for any
candidate, in any setup type. `MAX(eval_date) WHERE status='done'` =
2026-07-07 (today is 2026-07-18) — consistent with only the short 5bd/10bd
windows having had time to elapse since the 5-campaign-date entries
(mid-to-late June).

```sql
SELECT COUNT(*), COUNT(mfe_40bd_pct), COUNT(return_40bd_pct), COUNT(stop_hit) FROM signal_outcomes
```
```
(117, 0, 0, 0)
```
**0 of 117** `signal_outcomes` rows platform-wide have `mfe_40bd_pct`,
`return_40bd_pct`, or `stop_hit` populated.

**Consequence for the "117 platform-wide / 80 for pullback+trend_continuation"
counts cited in prior reports (P2-F, P2-H):** those counts measured
`signal_outcomes` *rows*, which — per the mechanism above — overcounts
distinct traded candidates, since a candidate that has reached its 10bd
checkpoint has **two** rows (one from its 5bd pass, one from its 10bd
pass), not one. For pullback+trend_continuation specifically: 80 rows
resolve to **45 distinct `proposal_id`s** (35 candidates have exactly 2
rows each — a 5bd-checkpoint row plus a later 10bd-checkpoint row for the
same trade; 10 candidates have only reached their 5bd checkpoint so far
and have 1 row). This doesn't invalidate those prior reports' own
conclusions (neither was making an outcome-quality claim), but it's
relevant context here and worth correcting going forward: **row count in
`signal_outcomes` ≠ distinct candidate count**, and neither means
"matured."

---

## 2. What this means for the requested analysis

The coder note's premise — pull matured outcomes, bucket by stop distance,
compare win rate / realized RR — assumed `outcome_status='complete'`
meant the full 40bd trade lifecycle had resolved, giving a real signal on
whether tight-but-passing stops perform differently from comfortably-clear
ones. That signal requires knowing whether the stop was actually hit
before the target, or vice versa — exactly the fields (`stop_hit`,
`target_hit`, `mfe_40bd_pct`, `mae_40bd_pct`) that don't exist yet for
any candidate. **The earliest any candidate from this cohort can reach
its true 40bd checkpoint is roughly mid-to-late August 2026** (40 business
days past entry dates of 2026-06-12 through 2026-06-23), and even then
only for the 2026-06-11/06-18 signal dates — the 07-02/07-08 dates are
further out still.

Per instructions ("do not force a conclusion the sample size can't
support"), the right conclusion here is not a weak or noisy signal — it's
that **there is currently no data capable of answering this question**,
full stop. That's a valid, useful finding for the ADR: the reject-vs-widen
decision cannot be informed by real outcome correlation yet, on any
timeline shorter than several more weeks of the platform continuing to
run and the outcome queue continuing to drain 20bd/40bd checkpoints.

---

## 3. What the interim (5bd/10bd) data shows anyway — exploratory only, not an answer

Presented for completeness and because the investigative work to build it
was already done — **not** as a preliminary answer to the reject-vs-widen
question. These are 5- or 10-business-day exit-at-horizon-close snapshots,
computed without any knowledge of whether the stop or target was actually
touched at any point (including within that short window) — a candidate
showing a "loss" here may simply not have moved yet, not have been
stopped out.

**Dedup method:** kept the latest (`calculated_at`) row per `proposal_id`
— i.e., the most-mature snapshot available for each candidate (10bd where
reached, else 5bd). 45 distinct candidates (11 pullback, 34
trend_continuation): 35 reached their 10bd checkpoint, 10 only their 5bd.

**`stop_distance_atr` distribution** (real Step 5 stop, post-P2-F-fix
formula, computed from `signal_outcomes.entry_price_raw`/`stop_price_raw`
joined to `step4_analysis.atr_pct` for the same ticker/date/setup_type):
`min=0.744  p25=1.141  median=2.334  p75=2.717  max=4.249`. **No candidate
in this matured-so-far set sits close to the 0.5 floor** — the nearest is
0.744 ATR, well clear. This is itself informative: the reject-vs-widen
question is about candidates rejected *below* 0.5, and the currently-passing
population that has any outcome data at all doesn't include anything near
that boundary — bucket boundaries below are set from the actual observed
spread (min ≈0.74), not the originally-suggested 0.5–0.7 range, per
instructions.

| Bucket | n | setup_type split | win rate (interim R>0) | mean interim R | median interim R |
|---|--:|---|--:|--:|--:|
| 0.7–1.0 ATR (nearest to floor) | 8 | pullback 6, trend_continuation 2 | 0.88 (7/8) | 2.118 | 2.468 |
| 1.0–2.0 ATR | 9 | pullback 5, trend_continuation 4 | 0.44 (4/9) | −0.050 | −0.136 |
| 2.0+ ATR (comfortably clear) | 28 | trend_continuation 28 | 0.68 (19/28) | 0.348 | 0.366 |

**By setup_type** (buckets this small per setup_type are listed for
transparency, not because they're independently meaningful):

| setup_type | 0.7–1.0 ATR | 1.0–2.0 ATR | 2.0+ ATR |
|---|---|---|---|
| pullback | n=6, win_rate=1.00, mean_R=3.068 | n=5, win_rate=0.20, mean_R=−0.709 | n=0 |
| trend_continuation | n=2, win_rate=0.50, mean_R=−0.731 | n=4, win_rate=0.75, mean_R=0.774 | n=28, win_rate=0.68, mean_R=0.348 |

---

## 4. Honest assessment — no usable pattern, for two independent reasons

1. **Sample size, even setting aside data maturity:** 45 candidates split
   3 ways by bucket and 2 ways by setup_type produces cells of n=2 to
   n=8. The `pullback` "nearest to floor" cell (n=6, 100% win rate,
   mean R=3.07) and "1.0–2.0 ATR" cell (n=5, 20% win rate, mean R=−0.71)
   look like a clean, strong signal in the *opposite* direction from what
   "widen the floor because tight stops perform worse" would predict —
   but n=5/n=6 on a 5-10-business-day snapshot is nowhere near enough to
   trust; a single or double outlier easily produces exactly this shape by
   chance (the pullback "1.0-2.0" bucket includes a −2.38 and a −1.96,
   two large losses among five, which alone drive the mean deeply
   negative). The `trend_continuation` split doesn't echo the pattern at
   all (roughly flat-to-mildly-increasing across buckets) — the two setup
   types don't agree with each other, which is itself evidence there's no
   real underlying signal here yet, just noise.
2. **Data maturity (§1-2):** these are 5/10bd snapshots, not realized
   trade outcomes. Even a "clean-looking" pattern in this data says
   nothing about whether stops were actually hit, which is the entire
   substance of the reject-vs-widen question.

**Plainly stated: no observable pattern that can be trusted.** The
headline numbers move around a lot between buckets (0.88 → 0.44 → 0.68
win rate — non-monotonic even setting aside the maturity problem), the
two setup types disagree with each other, and the underlying data isn't
even the right data yet. This should not move the reject-vs-widen ADR in
either direction.

---

## 5. Anomalies (verbatim)

- **The decisive finding of this investigation** (§1): `signal_outcomes`
  duplicates per-horizon-checkpoint by design (`outcome_id` keyed on
  `(proposal_id, horizon_bd)`), and `outcome_status='complete'` is a
  per-row, per-horizon completeness flag, not a trade-lifecycle-matured
  flag. 0/117 rows platform-wide have `mfe_40bd_pct`/`return_40bd_pct`/
  `stop_hit` populated; the longest horizon ever completed is 10 business
  days. This is not a bug introduced by this investigation and nothing was
  changed to produce it — it's the existing, currently-running design,
  simply not what "matured signal_outcomes" was assumed to mean in prior
  reports (P2-F 2026-07-15, P2-H 2026-07-18) that cited the 117/80 counts
  without checking horizon completeness. Worth an explicit note to
  whoever next reads/reports `signal_outcomes` counts: filter on which
  horizon fields are actually populated, not `outcome_status` alone, and
  dedupe on `proposal_id` (keeping the latest `calculated_at`), not raw
  row count.
- 35 of 45 distinct pullback/trend_continuation candidates have two
  `signal_outcomes` rows with **substantively different** `realized_r_multiple`
  values between their 5bd and 10bd snapshots (e.g. `TREX` 06-18: 0.319 →
  0.011; `WAL` 06-11: −0.676 → −0.045; `DYN` 06-18: 0.375 → 1.279) — large
  swings, several crossing zero. This is expected and correct given each
  row is a different horizon's exit-at-close snapshot of the same
  in-progress trade, not evidence of computational error — flagged here
  only because it looks alarming out of context and partly motivated
  digging into §1.
- No candidate in the currently-available outcome data sits within
  0.5–0.7 ATR of the floor (§3) — the bucket range originally suggested in
  the coder note doesn't have any members at all.

## No writes, no code changes

Read-only investigation only, against `data/duckdb/prod.duckdb`
(`read_only=True`). `app/services/outcomes/outcome_queue.py` was read, not
modified. No pipeline rerun, no synthetic/simulated outcomes for the
110 rejected candidates (none were attempted), no commit.
