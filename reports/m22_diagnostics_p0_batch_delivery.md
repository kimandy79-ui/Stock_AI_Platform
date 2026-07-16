# M22 Funnel Diagnostics — P0 Batch Delivery

**Date:** 2026-07-13
**Commit:** `fb5a36d` — `module22_diagnostics_p0_batch_stable`
**Scope:** Diagnostics-only (display / report-builder layer). No schema, scoring, or gate logic touched.
**Spec reference:** `M22_FUNNEL_DIAGNOSTICS_SPEC.md` (updated in this batch).

---

## Summary of items

| Item | Description | Status |
|------|-------------|--------|
| A | Merge `breakout_proximity` instrumentation | Already committed (`0015a85`); 3 tests reshaped to nested evidence dict |
| B | Fix `_rpt_evidence` routed/validated population mismatch + report labeling (industry_cap rename, 6b proximity sort) | Done |
| C | Add eligibility rejection-reason breakdown to report (CLI `1b`) | Done |
| D | Confirm ATR stop-floor 0.3/0.5 split is intentional | Verified — intentional, no change |

---

## Item B — investigation finding (root cause)

**Confirmed by reading `_rpt_evidence()`.** The function looped over step4 rows and collected
`setup_score` **before** the pass/fail guard, while every other field was collected **inside**
`if r["setup_passed"]:`

```python
for r in s4:
    _collect(d, "setup_score", r.get("setup_score"))   # ← ALL routed rows (pass + fail)
    if r["setup_passed"]:
        _collect(d, "rvol", ...)                        # ← validated rows only
        _collect(d, "atr_pct", ...)                     # ← validated rows only
        ...
```

So `setup_score` reflected the **routed** population (e.g. n=713/749/1075/595) while
`rvol`/`atr_pct`/etc reflected the **validator-passed** population (e.g. n=14/464/662/13) — all
emitted under a single "step4-passed rows" header. That is exactly why the 2026-07-10 report was
internally inconsistent.

It was **not** the case that some other field was accidentally left ungated. The 2026-07-08
scratchpad claim that "`_rpt_evidence` gates every field on `setup_passed=True`" was simply wrong
for the one field `setup_score`.

**Fix:** split each setup_type into two explicit populations, each carrying its own row count:

```
evidence_summaries[setup_type] = {
    "routed_n":    <count of all step4 rows for setup_type (pass + fail)>,
    "validated_n": <count of setup_passed == True step4 rows>,
    "routed":    { "setup_score": {stats} },
    "validated": { "rvol": {stats}, "atr_pct": {stats}, ..., "breakout_proximity": {stats},
                   "estimated_rr_s5": {stats}, "stop_distance_pct_s5": {stats} },
}
```

- **routed** — fields defined for every routed candidate regardless of pass/fail (`setup_score`).
- **validated** — gate-input / evidence fields that only exist for candidates that cleared the
  validator, plus the step5-derived `*_s5` fields (a step5 proposal only exists for a validated
  candidate). No field appears in both series.

---

## Item D — investigation finding (ATR stop-floor split)

**Intentional per-config, not drift.** `min_atr_stop_floor_multiple` is **0.3** for
`consolidation_base` and **0.5** for breakout / pullback / trend_continuation, and this is
consistent in three independent places:

1. **Seed configs** — `app/services/config/default_configs.py` (verified across every preset
   variant): `consolidation_base` = 0.3 at lines 191 and 306; all others = 0.5 at lines
   93 / 127 / 158 / 251 / 278 / 343 / 378 / 406.
2. **Code-default fallbacks** — `app/services/screening/m14_setup_validators.py`:
   breakout / pullback / trend_continuation default to 0.5 (lines 399 / 688 / 1011);
   consolidation_base defaults to 0.3 (line 1296).
3. **Explicit code comment** — `m14_setup_validators.py:1295`:
   *"Consolidation uses a tighter ATR floor because base stops are naturally compressed"*, and
   line 1394 *"Stop ≥ 0.3 ATR below entry (P1-1; tighter floor for base setups)"*.

**No config or code change made.**

> Caveat: prod/debug `.duckdb` files do not currently exist (the authorized wipe is pending), so
> this was verified against the seed sources that *populate* the active configs rather than a live
> `setup_configs` table. Re-confirm against the live table after the backfill if desired.

---

## Report labeling changes (Item B, part 2)

- **`industry_cap` → `diversity_trim_industry_cap`** in the human-readable report **display only**
  (`_rpt_s5_rejection_reasons` output). The raw DB `rejection_reason` value, the
  `_rpt_layers` matching, and the step5 engine are all unchanged. Implemented via a
  display-surface mapping `_REJECTION_DISPLAY_LABELS`.
- **`6b. BORDERLINE — nearest to threshold`** — a second borderline view sorted ascending by
  direction-aware normalized distance to the failed threshold:
  - below-min rules (`<`): `(threshold - actual) / threshold`
  - above-max rules (`>`): `(actual - threshold) / threshold`
  - unparseable rows sort last. The existing score-sorted section `6` is retained as primary.

## Report additions (Item C)

- **`eligibility_rejection_reasons`** report key: list of `{reason, count, pct_of_ineligible}`
  over step3 ineligible candidates, sorted by count desc. Surfaces M13 gates such as
  `merger_pending`. Rendered as CLI section **`1b`**. Computed from the already-loaded s3 rows
  (`eligibility_fail_reasons`) — no additional DB query.

---

## Changed files

```
app/services/diagnostics/funnel_diagnostics.py | 131 +++++++++++++++++++++---
specs/M22_FUNNEL_DIAGNOSTICS_SPEC.md           |  51 ++++++++++
tests/test_phase6_diagnostics.py               |  85 ++++++++++++++--
tools/run_funnel_diagnostics.py                | 132 +++++++++++++++++++------
4 files changed, 349 insertions(+), 50 deletions(-)
```

### `app/services/diagnostics/funnel_diagnostics.py`
- `_rpt_evidence()` — routed/validated population split with `routed_n` / `validated_n`.
- `_rpt_eligibility_rejection_reasons()` — new (Item C).
- `_borderline_proximity()` / `_sort_borderline_by_proximity()` — new (Item B, 6b).
- `_REJECTION_DISPLAY_LABELS` / `_display_rejection_label()` — new (industry_cap relabel).
- `build_report()` — adds `eligibility_rejection_reasons` to the report dict.

### `tools/run_funnel_diagnostics.py`
- `_print_eligibility_rejections()` — new CLI section `1b`.
- `_print_evidence()` rewritten to print `routed (n=…)` and `validated (n=…)` sub-headers.
- `_print_borderline()` — adds section `6b` proximity sort.
- Imports `_borderline_proximity`, `_sort_borderline_by_proximity`.

### `specs/M22_FUNNEL_DIAGNOSTICS_SPEC.md`
- New "Report surface (`build_report`)" section documenting the routed/validated split, the
  `eligibility_rejection_reasons` (1b), the `industry_cap` relabel, and the `6/6b` borderline
  views.

### `tests/test_phase6_diagnostics.py`
- 3 Item A tests updated to the nested evidence shape (`["validated"]["breakout_proximity"]`).
- New: `test_evidence_summaries_setup_score_uses_routed_population`,
  `test_evidence_summaries_rvol_uses_validated_population`,
  `test_borderline_proximity_sort_orders_correctly`,
  `test_report_includes_eligibility_rejection_reasons`.

---

## Test results

Command:

```
pytest tests/test_phase6_diagnostics.py tests/test_funnel_diagnostics.py -v
```

Result:

```
platform win32 -- Python 3.14.5, pytest-9.0.3, pluggy-1.6.0
collected 80 items

tests\test_phase6_diagnostics.py ....................................... [ 48%]
.                                                                        [ 50%]
tests\test_funnel_diagnostics.py ....................................... [ 98%]
.                                                                        [100%]

============================= 80 passed in 4.75s ==============================
```

New / updated tests (all PASSED):

```
test_evidence_summaries_collects_breakout_proximity_for_passed_breakout   PASSED
test_evidence_summaries_breakout_proximity_absent_when_no_passed_rows      PASSED
test_evidence_summaries_other_setup_types_have_no_breakout_proximity_key   PASSED
test_evidence_summaries_setup_score_uses_routed_population                 PASSED
test_evidence_summaries_rvol_uses_validated_population                     PASSED
test_borderline_proximity_sort_orders_correctly                           PASSED
test_report_includes_eligibility_rejection_reasons                        PASSED
```

Also ran `pytest tests/test_tools_runners.py` → 12 passed (CLI was modified).

---

## Sample report render (fixture)

Rendered through the real `tools/run_funnel_diagnostics.py` print path with a synthetic report dict:

```
  ── 1b. ELIGIBILITY REJECTION REASONS  (step3, over ineligible candidates)
    Reason                                         Count   % inelig
  ──────────────────────────────────────────────────────────────────────
    merger_pending                                     2     100.0%
    price_below_min                                    1      50.0%

  ── 3b. STEP5 REJECTION REASONS  (grouped, all dispositions) ──────────
    Reason                                         Count   % total  Example (actual vs threshold)
  ──────────────────────────────────────────────────────────────────────
    diversity_trim_industry_cap                        1    100.0%    [ZZZ]

  ── 5. EVIDENCE SUMMARIES  (routed vs validated populations) ──────────

    breakout — routed (n=2)
      Field                        n      min      p25      p50      p75      max     mean
      ────────────────────────────────────────────────────────────────
      setup_score                  2    40.00    40.00    80.00    80.00    80.00    60.00

    breakout — validated (n=1)
      Field                        n      min      p25      p50      p75      max     mean
      ────────────────────────────────────────────────────────────────
      rvol                         1     2.00     2.00     2.00     2.00     2.00     2.00

  ── 6. BORDERLINE FAILURES  (highest-scoring fails per setup) ─────────

    breakout:
      Ticker     Score  Failed Rule                               Comparison
      ──────────────────────────────────────────────────────────────────────────
      NEAR      69.0000  rvol_low                                  actual 1.4 < min 1.5
      FAR       70.0000  rvol_low                                  actual 0.5 < min 1.5
      WIDE      68.0000  stop_wide                                 actual 0.12 > max 0.10

  ── 6b. BORDERLINE — nearest to threshold  (direction-aware normalized distance)

    breakout:
      Ticker       Dist  Failed Rule                               Comparison
      ──────────────────────────────────────────────────────────────────────────
      NEAR         6.7%  rvol_low                                  actual 1.4 < min 1.5
      WIDE        20.0%  stop_wide                                 actual 0.12 > max 0.10
      FAR         66.7%  rvol_low                                  actual 0.5 < min 1.5
```

Note how section `6` orders by `setup_score` (NEAR 69 → FAR 70 → WIDE 68 stays score-ordered),
while `6b` orders by proximity (NEAR 6.7% → WIDE 20.0% → FAR 66.7%), handling both `<` and `>`
directions.

---

## Assumptions

1. **`industry_cap` only** was relabelled, per the note's literal wording. `sector_cap` is the
   analogous post-ranking diversity trim and is currently left showing its raw label — flagged for
   confirmation (a one-line addition to `_REJECTION_DISPLAY_LABELS` if you want parity).
2. The step5-derived `estimated_rr_s5` / `stop_distance_pct_s5` evidence fields were placed in the
   **validated** bucket (a step5 proposal only exists for a validated candidate). They carry their
   own per-field `n`, so they do not mix with the step4 validated row count.
3. Item C sources the eligibility reasons from the already-loaded `s3` rows inside `build_report`,
   rather than re-running `_SQL_S3_ELIGIBILITY_REASONS` — equivalent data, one fewer DB round-trip,
   consistent with how `build_report` already works.
4. The 3 Item A tests were updated to the new nested shape (`["validated"]["breakout_proximity"]`).
   The instrumentation logic (gated on breakout + setup_passed) is unchanged; only the container
   moved. This was necessary reconciliation between Item A and Item B.

---

## Out of scope (require separate architect sign-off)

Items E–H (consolidation recalibration, ATR floor policy, breakout gate-ordering, RVOL backstop)
are not part of this batch and require sign-off after the multi-day diagnostics campaign.
No changes were made to step3 / step4 / step5 gate logic, thresholds, or scoring.

---

## Commit

```
fb5a36d  module22_diagnostics_p0_batch_stable
```

Committed directly to `main` following this repo's established convention. Only the four batch
files were staged; unrelated pre-existing working-tree changes were left untouched.
