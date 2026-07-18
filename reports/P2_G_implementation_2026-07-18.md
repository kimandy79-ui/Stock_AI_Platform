# P2 Item G — Implementation: Multi-Reason Failure Breakdown

**Date:** 2026-07-18
**Scope:** `app/services/diagnostics/funnel_diagnostics.py` and
`tools/run_funnel_diagnostics.py` — report-layer addition only.
**No commit — diff delivered, stop, per current policy.**
`m14_setup_validators.py`, `step3_universal_eligibility.py`, and every
other live pipeline/validator module are untouched — confirmed no
frozen-module concern by the P2-G investigation
(`reports/P2_G_breakout_gate_ordering_investigation_2026-07-18.md`).

---

## 0. Background

The P2-G investigation found: (a) `validate_breakout`'s hard checks don't
short-circuit each other — every check runs and appends independently to
`hard_fails`; (b) the diagnostics report only ever surfaces the *first*
`hard_fails` entry (`setup_fail_reason`); (c) the full list is already
sitting unused in `explanation_json["hard_fails"]`, already fetched and
parsed by `build_report()`. Concretely: 119 of 3191 (3.7%) RVOL-first
breakout failures across the 5 campaign dates also fail
`stop_below_atr_floor`, invisible in the current report.

This note implements the fix: a new secondary breakdown that reads the
already-loaded `hard_fails` list and reports, per setup type, which other
reasons co-occur behind each first-reported reason.

---

## 1. Diff

### 1a. `app/services/diagnostics/funnel_diagnostics.py` — new aggregation function

Inserted immediately after `_rpt_failure_reasons` (the existing
first-reason-only breakdown it complements):

```python
def _rpt_co_occurring_failure_reasons(
    s4: list[dict], setup_type_filter: str | None
) -> list[dict]:
    """P2-G secondary failure-reason breakdown: for each setup_type's failing
    population, group by the FIRST ``hard_fails`` reason (the same value
    reported as ``setup_fail_reason`` / surfaced by ``_rpt_failure_reasons``),
    then report which OTHER reasons appear elsewhere in that row's full
    ``hard_fails`` list.

    Source: ``explanation_json["hard_fails"]`` — already fetched by
    ``_SQL_S4_REPORT`` and already parsed into a dict at the ``build_report``
    call site (``r["explanation_json"] = _parse_json_dict(...)``). No new
    query, no schema change; ``m14_setup_validators.py`` writes this same
    list into every validator's ``evidence_json["hard_fails"]``, which
    ``step4_setup_validation_engine.py`` persists verbatim as
    ``explanation_json`` (see P2-G investigation).

    Generalizes across all four setup types: whichever setup's ``hard_fails``
    happens to have more than one entry for a given row contributes to that
    setup's breakdown — nothing breakout-specific here.

    Percentages are against the first-reason cohort size (how many rows share
    that same first reason), not the setup's total failure count.
    """
    cohort_size: dict[tuple[str, str], int] = {}
    co_counts: dict[tuple[str, str, str], int] = {}

    for r in s4:
        st = r.get("setup_type") or ""
        if st not in ACTIVE_SETUP_TYPES:
            continue
        if setup_type_filter and st != setup_type_filter:
            continue
        if r["setup_passed"]:
            continue
        expl = r.get("explanation_json") or {}
        hard_fails = expl.get("hard_fails") or []
        if not hard_fails:
            continue  # soft-score-only fail (no hard_fails entries) — nothing to break down
        first_key, _ = _normalize_validation_reason(hard_fails[0])
        cohort_k = (st, first_key)
        cohort_size[cohort_k] = cohort_size.get(cohort_k, 0) + 1
        seen_others: set[str] = set()
        for hf in hard_fails[1:]:
            other_key, _ = _normalize_validation_reason(hf)
            if other_key in seen_others:
                continue  # count each co-occurring category once per row
            seen_others.add(other_key)
            k = (st, first_key, other_key)
            co_counts[k] = co_counts.get(k, 0) + 1

    result = []
    for (st, first_key, other_key), cnt in sorted(
        co_counts.items(), key=lambda x: (x[0][0], x[0][1], -x[1])
    ):
        cohort_n = cohort_size.get((st, first_key), 0)
        result.append({
            "setup_type": st,
            "first_reason": first_key,
            "co_occurring_reason": other_key,
            "count": cnt,
            "cohort_size": cohort_n,
            "pct_of_first_reason_cohort": _pct_f(cnt, cohort_n),
        })
    return result
```

Reuses `_normalize_validation_reason` and `_pct_f` — both already used by
`_rpt_failure_reasons`, no new helpers needed.

### 1b. `build_report()` — one new line (report field)

```diff
         report["failure_reasons"]        = _rpt_failure_reasons(s4, setup_type_filter)
+        report["co_occurring_failure_reasons"] = _rpt_co_occurring_failure_reasons(s4, setup_type_filter)
```

Output shape per row, exactly as specified:
`{setup_type, first_reason, co_occurring_reason, count, pct_of_first_reason_cohort}`
(plus `cohort_size`, additive — the CLI section header uses it to show
`(cohort n=…)`).

### 1c. `tools/run_funnel_diagnostics.py` — new CLI section `4b`

```python
def _print_co_occurring_failures(rpt: dict) -> None:
    # P2-G: secondary breakdown — for each first-reported failure reason,
    # which OTHER reasons also appear in that row's full hard_fails list.
    # Source: explanation_json["hard_fails"], already fetched/parsed by
    # build_report() — see _rpt_co_occurring_failure_reasons.
    _h2("4b. CO-OCCURRING FAILURE REASONS  (behind first-reported reason)")
    rows = rpt.get("co_occurring_failure_reasons", [])
    if not rows:
        print("    (no co-occurring reasons — every failing row's hard_fails "
              "list has exactly one entry)")
        return
    current_key: tuple[str, str] | None = None
    for r in rows:
        key = (r["setup_type"], r["first_reason"])
        if key != current_key:
            print()
            print(f"    {r['setup_type']} — first reason: {r['first_reason']}  "
                  f"(cohort n={r['cohort_size']})")
            current_key = key
        pct_s = _pct_str(r["pct_of_first_reason_cohort"])
        print(f"      co-occurring: {r['co_occurring_reason']:<40}  "
              f"{r['count']:>5}  ({pct_s})")
```

Wired into `_print_report()` right after section 4 (matching its `4b`
numbering):

```diff
     _print_routing_detail(rpt)
+    _print_co_occurring_failures(rpt)
     _print_evidence(rpt)
```

### Not changed

- No new SQL — `_SQL_S4_REPORT` already selects `explanation_json`
  (line 175); no other query was touched.
- No existing function bodies modified — `_rpt_failure_reasons`,
  `_rpt_borderline`, `_rpt_evidence`, `_print_failure_reasons`,
  `_print_borderline`, etc. are byte-identical to before this change (only
  two insertion points: one new report-dict line, one new CLI call).
- `m14_setup_validators.py`, `step3_universal_eligibility.py` — untouched.

---

## 2. Tests — new, all passing

`tests/test_phase6_diagnostics.py`, new section (7 tests):

- `test_co_occurring_reasons_empty_when_all_single_entry` — every failing
  row has exactly one `hard_fails` entry → `[]`, regardless of how many
  rows share a first reason.
- `test_co_occurring_reasons_detects_genuine_co_occurrence` — a two-entry
  `hard_fails` list produces exactly one breakdown row with the correct
  `(setup_type, first_reason, co_occurring_reason)` key and `count`.
- `test_co_occurring_reasons_percentage_uses_first_reason_cohort_not_total_failures`
  — 4-row RVOL-first cohort (2 co-occurring) alongside a 20-row unrelated
  `stop_below_atr_floor`-first cohort; asserts `cohort_size == 4` (not 24,
  not 20) and `pct_of_first_reason_cohort == 0.5`, not diluted by the
  larger unrelated population.
- `test_co_occurring_reasons_row_with_no_hard_fails_skipped` — a
  soft-score-only fail (`hard_fails=[]`) contributes no cohort membership.
- `test_co_occurring_reasons_generalizes_across_setup_types` — pullback and
  consolidation_base rows each produce their own breakdown row; not
  breakout-specific.
- `test_co_occurring_reasons_respects_setup_type_filter` — `setup_type_filter`
  excludes non-matching setups, matching every other `_rpt_*` function's
  convention.
- `test_co_occurring_reasons_passed_rows_excluded` — `setup_passed=True`
  rows are never included, regardless of their `hard_fails` content.

**Result:**

```
$ python -m pytest tests/test_phase6_diagnostics.py -v -k co_occurring
collected 48 items / 41 deselected / 7 selected
tests\test_phase6_diagnostics.py .......                                 [100%]
====================== 7 passed, 41 deselected in 0.42s =======================
```

---

## 3. Full diagnostics module test suite

```
$ python -m pytest tests/test_phase6_diagnostics.py tests/test_funnel_diagnostics.py tests/test_tools_runners.py -v
collected 108 items
tests\test_phase6_diagnostics.py ......................................... [ 36%]
.........                                                                 [ 44%]
tests\test_funnel_diagnostics.py ......................................... [ 80%]
.........                                                                 [ 88%]
tests\test_tools_runners.py ............                                 [100%]
============================= 108 passed in 6.22s =============================
```

108/108 pass (101 pre-existing + 7 new). No regressions — every existing
`_rpt_*`/`_print_*` function's tests pass unchanged, confirming the
existing report sections (§1b, §3a/3b, §5, §6/6b) are unaffected: only two
insertion points were made (§1), no existing function body was edited.

---

## 4. Empirical validation — reproducing the known-answer check

Ran the **actual new production function** (`_rpt_co_occurring_failure_reasons`,
not a reimplementation) against the real 5-campaign-date breakout cohort in
`data/duckdb/prod.duckdb` (read-only, no pipeline rerun — same data the
P2-G investigation used):

```
Total breakout step4_analysis rows (5 dates): 4692

_rpt_failure_reasons (unchanged, existing function):
  rvol_below_hard_threshold: 3191  (98.1%)
  stop_below_atr_floor: 47  (1.4%)
  score_below_threshold: 16  (0.5%)

_rpt_co_occurring_failure_reasons (NEW function) output:
  {'setup_type': 'breakout', 'first_reason': 'rvol_below_hard_threshold',
   'co_occurring_reason': 'stop_below_atr_floor', 'count': 119,
   'cohort_size': 3191, 'pct_of_first_reason_cohort': 0.0373}

=== Known-answer check ===
Expected (from P2-G investigation): count=119, cohort_size=3191, pct≈3.7%
Actual: count=119, cohort_size=3191, pct=3.73%
PASS: matches known answer exactly
```

Exact match — `count=119`, `cohort_size=3191`. The new report logic
reproduces the investigation's own finding precisely, using the live
production data path (`explanation_json["hard_fails"]`), not a parallel
computation.

---

## 5. Sample rendered output — new `4b` section

Ran the actual CLI (`tools/run_funnel_diagnostics.py`) against real
`prod.duckdb` data, read-only, for `--date 2026-06-11` (one of the 5
campaign dates):

**Filtered to breakout** (`--setup-type breakout`):

```
── 4. ROUTING DIAGNOSTICS  (routed vs validator coverage) ────────────
    Setup                          Routed    Step4   Coverage
  ──────────────────────────────────────────────────────────────────────
    breakout                          960      960     100.0%
    pullback                          407        0       0.0%
    trend_continuation                925        0       0.0%
    consolidation_base                630        0       0.0%

    NOTE: 1045 tickers multi-routed — breakout may absorb consolidation-like candidates if both qualify.

  ── 4b. CO-OCCURRING FAILURE REASONS  (behind first-reported reason) ──

    breakout — first reason: rvol_below_hard_threshold  (cohort n=880)
      co-occurring: stop_below_atr_floor                         42  (4.8%)
```

(880/42 is this single date's slice of the 5-date 3191/119 aggregate —
consistent, smaller sample.)

**Unfiltered** (all 4 setup types, same date) — confirms the section
generalizes and surfaces genuinely new information beyond breakout, as the
implementation note anticipated ("other setups may have their own
co-occurrence patterns worth surfacing"):

```
  ── 4b. CO-OCCURRING FAILURE REASONS  (behind first-reported reason) ──

    breakout — first reason: rvol_below_hard_threshold  (cohort n=880)
      co-occurring: stop_below_atr_floor                         42  (4.8%)

    consolidation_base — first reason: atr_too_high  (cohort n=2)
      co-occurring: price_below_base_low                          2  (100.0%)
      co-occurring: stop_below_atr_floor                          2  (100.0%)

    consolidation_base — first reason: price_below_base_low  (cohort n=59)
      co-occurring: stop_below_atr_floor                         59  (100.0%)

    consolidation_base — first reason: range_tightness_too_low  (cohort n=262)
      co-occurring: price_above_base_high                        97  (37.0%)
      co-occurring: stop_below_atr_floor                         44  (16.8%)
      co-occurring: price_below_base_low                         33  (12.6%)
```

`consolidation_base`'s `range_tightness_too_low`-first cohort (262 rows,
the setup's dominant first-reason per §3a) shows 37.0% also fail
`price_above_base_high` and 16.8% also fail `stop_below_atr_floor` —
entirely new, real visibility that didn't exist in the report before this
change, and matches the CODER NOTE's expectation that consolidation_base
would have its own worthwhile co-occurrence patterns.

---

## 6. Anomalies (verbatim)

- Running the CLI tool through a PowerShell pipe with `| Select-Object`
  produced exit code 255 even though the script completed and printed
  correctly — this is a PowerShell/console encoding interaction (the
  project's box-drawing characters and em-dashes triggering the known
  cp949-console issue, see `env_cp949_utf8_backfill` project memory),
  not a bug in this change. Redirecting to a file (`> log 2>&1`) instead
  of piping produced a clean exit code 0 with identical content. No code
  change was needed or made for this.
- Beyond the requested breakout known-answer reproduction, the unfiltered
  CLI run surfaced real, non-trivial `consolidation_base` co-occurrence
  patterns (§5) — not anticipated in exact form by the investigation
  (which only examined breakout), but consistent with the implementation
  note's own hint that other setups might have their own patterns worth
  surfacing. Not a defect — additional evidence the feature works as
  intended.

## No writes, no code changes beyond the requested scope

Read-only empirical checks against `data/duckdb/prod.duckdb`
(`read_only=True` for the standalone validation script;
`build_report()`/CLI reads are also read-only by design — `build_report`
does not write to `pipeline_run_diagnostics`). Code changes are limited to
`app/services/diagnostics/funnel_diagnostics.py` and
`tools/run_funnel_diagnostics.py`, both delivered as diffs in this report
and left uncommitted in the working tree, per current policy.
