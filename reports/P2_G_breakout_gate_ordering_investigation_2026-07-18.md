# P2 Item G — Breakout Gate-Ordering Visibility Investigation

**Date:** 2026-07-18
**Scope:** Read-only investigation. No code changes.
**No writes to `prod`.** No changes to `m14_setup_validators.py`,
`funnel_diagnostics.py`, or any other live module.

**Headline finding:** the gates ARE wired and firing — confirmed both by
code reading and by 5-date empirical data. But the original premise about
*which* gate masks *which* other gate turns out to be backwards: RVOL is
checked **third**, not first, so it structurally cannot mask
resistance/proximity/duration failures — those are always evaluated and
reported *before* RVOL gets a chance to run, by construction of the check
order. What RVOL's first-reason dominance actually masks is
`stop_below_atr_floor` (checked *after* RVOL) — confirmed for 119 real
candidates across the 5 campaign dates. The fix is exactly the report-layer
enhancement the note anticipated as the likely outcome (§(a) below); it
just targets a different downstream gate than originally hypothesized.

---

## 1. Confirmation: no short-circuit in `validate_breakout` (with one caveat)

Read `app/services/screening/m14_setup_validators.py:380-533` in full.

**Checks 1–5 run unconditionally, independently, no short-circuit between
them:**

| # | Check | Lines | Appends to |
|---|---|---|---|
| 1 | `breakout_proximity` in `[prox_min, prox_max]` | 487–494 | `hard_fails` |
| 2 | `range_duration >= min_base_duration` | 496–500 | `hard_fails` |
| 3 | RVOL hard gate (`rvol20 >= min_rvol`) | 502–507 | `hard_fails` |
| 4 | Stop ≥ `min_atr_stop_floor` ATR below entry | 509–521 | `hard_fails` |
| 5 | Optional earnings hard-block (opt-in, default off) | 523–531 | `hard_fails` |

`setup_passed_hard = len(hard_fails) == 0` at line 533 — evaluated only
after all five checks have run. No `return`, `continue`, or `elif`-chain
gates checks 2–5 on check 1–4's outcome. This matches the non-short-circuit
pattern the P2-F investigation found in `validate_pullback`,
`validate_trend_continuation`, and `validate_consolidation_base`.

**Caveat — a genuinely separate short-circuit exists, but it's not RVOL:**
lines 443–471 (`P0-1`) return immediately, **before any of the 5 hard
checks run**, when `resistance_adj is None or resistance_adj <= 0`:

```python
if resistance_adj is None or resistance_adj <= 0:
    return SetupValidationResult(
        ...
        setup_fail_reason="no_resistance_level",
        evidence_json={"hard_fails": ["no_resistance_level"], ...},
        ...
    )
```

This bypasses RVOL, proximity, duration, stop-floor, and earnings entirely
for candidates with no resistance level. It's a real short-circuit — just
in the opposite direction of the original concern (missing resistance
blocks RVOL from ever running, not the other way around).

**Correction to the original premise — check order matters:**
`breakout_proximity` (#1) and `range_duration` (#2) are evaluated *before*
RVOL (#3). Since `hard_fails` accumulates in check order and
`setup_fail_reason = hard_fails[0]`, **a candidate can only reach RVOL as
its first-reported reason if proximity and duration already passed.**
RVOL failing can never mask a proximity or duration failure in the
first-reason report — that's structurally impossible, not just empirically
rare. The only checks that *can* be masked behind an RVOL-first label are
the ones that run **after** RVOL: `stop_below_atr_floor` (#4) and
`earnings_too_close` (#5, normally inert — opt-in and off by default).
Resistance is handled separately again: it's checked *before* any of the 5
(via the `P0-1` early return), so it can never be masked by RVOL either —
if resistance is missing, `"no_resistance_level"` is *always* the reported
reason, and RVOL is never evaluated for that row at all.

---

## 2. Confirmation: diagnostics report is first-reason-only

`app/services/diagnostics/funnel_diagnostics.py`:

- **The SQL query itself doesn't select `setup_reasons`** — only the
  single-value `setup_fail_reason` column (plus `explanation_json`):
  ```sql
  -- _SQL_S4_REPORT, lines 172-177
  SELECT ticker, setup_type, setup_score, setup_passed, setup_fail_reason,
         rvol, atr_pct, distance_to_ema20_pct, distance_to_ema50_pct,
         explanation_json
  FROM step4_analysis WHERE run_id = ? AND signal_date = ?
  ```
- **`_rpt_failure_reasons`** (line 981, the failure-breakdown table) —
  line 993: `key, example = _normalize_validation_reason(r.get("setup_fail_reason"))`.
  First-reason only.
- **`_rpt_borderline`** (line 1097, the "nearest to threshold" table) —
  line 1117: same call, same single-value source. First-reason only.
- A repo-wide grep for `setup_reasons` inside `funnel_diagnostics.py`
  returns zero matches — the full multi-reason list is never read by name
  anywhere in this module.

**But the full list is already sitting in memory, unused.** Every
validator writes the complete `hard_fails` list into its own
`evidence_json["hard_fails"]`
(`m14_setup_validators.py` — e.g. line 623 for breakout: `"hard_fails": hard_fails`),
and `step4_setup_validation_engine.py:674` writes that same `evidence_json`
into the `explanation_json` column verbatim
(`json.dumps(result.evidence_json, default=str)`). `funnel_diagnostics.py`
already fetches `explanation_json` in `_SQL_S4_REPORT` and already parses
it into a dict before the failure-reason functions run
(`funnel_diagnostics.py:713`: `r["explanation_json"] = _parse_json_dict(...)`).

**Practical consequence: a fix needs no new SQL column and no live pipeline
rerun.** `r["explanation_json"]["hard_fails"]` is the full ordered list,
already present in every already-loaded row, for every date already in
`prod.duckdb`.

---

## 3. Secondary breakdown — actual 5-date data (read-only, same method as P2-F)

Pulled all `step4_analysis` rows for `setup_type = 'breakout'` across the
same 5 campaign dates P2-F used (2026-06-11, 06-18, 06-26, 07-02, 07-08),
read-only against `data/duckdb/prod.duckdb`.

```
Total breakout step4_analysis rows: 4692   (pass=1438, fail=3254)
```

**First-reason distribution (what the report currently shows):**

| First reason | Count | % of fails |
|---|--:|--:|
| `rvol_below_hard_threshold` | 3191 | 98.1% |
| `stop_below_atr_floor` | 47 | 1.4% |
| `score_below_threshold` (soft-score fail, 0 hard_fails) | 16 | 0.5% |

**Full `hard_fails` distribution (every entry, any position in the list) —
across all 3254 failing rows:**

| Reason (any position) | Count |
|---|--:|
| `rvol_below_hard_threshold` | 3191 |
| `stop_below_atr_floor` | 166 |
| `breakout_proximity_out_of_range` / `missing_breakout_proximity` | **0** |
| `range_duration_too_short` / `missing_range_duration` | **0** |
| `no_resistance_level` | **0** |

**RVOL-first cohort (n=3191) — masking analysis:**

- 119 of 3191 (3.7%) RVOL-first-failing candidates **also** have
  `stop_below_atr_floor` elsewhere in `hard_fails` — invisible in the
  current report, which only shows "RVOL" as their blocker. Spot-checked
  5 rows directly (ACA, ADC, ADMA, AES, AFL, all 2026-06-11):
  ```
  ACA:  hard_fails=['rvol_below_hard_threshold(1.07<1.5)', 'stop_below_atr_floor(stop_atr=0.46<0.5)']
  ADC:  hard_fails=['rvol_below_hard_threshold(1.04<1.5)', 'stop_below_atr_floor(stop_atr=0.34<0.5)']
  ```
- **Zero** RVOL-first rows have a proximity/duration/resistance reason
  co-occurring — confirming §1's structural claim with real data, not just
  code reading: it's not just rare, it never happens in this cohort, which
  is exactly what the check-order argument predicts (impossible by
  construction, since proximity/duration/resistance are all evaluated
  strictly before RVOL can become the reported reason).

**Why proximity/duration/resistance never fail at all for routed breakout
candidates (0 occurrences even outside the RVOL-first cohort) — this is
new information beyond what the note asked for, worth flagging:** Step 3's
own routing gate (`app/services/screening/step3_universal_eligibility.py`,
`_route_breakout`, lines 417–423) already requires
`breakout_proximity >= -1.0` and `range_duration >= 10` before a candidate
is ever routed to `'breakout'` — the same lower-bound/floor M14 re-checks
as hard checks 1–2. `breakout_proximity` also can't be non-null without
resistance-derived feature data existing in the first place (it's computed
from `high20`/resistance in `feature_engine.py`), which is consistent with
`no_resistance_level` never appearing for routed candidates either. M14's
one check that Step 3 does *not* pre-filter on is the upper bound
(`prox_max=0.5` — Step 3 only checks `>= -1.0`, not `<= 0.5`), so an
over-extended breakout could in principle still reach M14 and fail
`breakout_proximity_out_of_range` there — it simply didn't happen to occur
in this particular 5-date sample. Not a bug: M14 re-checking conditions
Step 3 already substantially filters on is redundant-by-design defense in
depth, not dead code (see §1 — the checks do run and would fire given the
right input).

---

## 4. Conclusion

**(a) Report-layer fix, no frozen-module concern.** The gates are
confirmed wired and firing — no short-circuit exists among breakout's 5
hard checks, and the resistance early-return is a real but separate,
inert-in-this-data short-circuit. `m14_setup_validators.py` does not need
touching. What's actually masked by first-reason-only reporting is
`stop_below_atr_floor` behind `rvol_below_hard_threshold` (119 real
candidates across 5 dates, 3.7% of the RVOL-first cohort) — not
resistance/proximity as originally framed, because RVOL's position as
check #3 (after proximity #1 and duration #2) makes masking those two
structurally impossible. The fix belongs in `funnel_diagnostics.py`'s
failure-reason reporting layer — a secondary breakdown reading
`explanation_json["hard_fails"]` (already fetched, already parsed, no new
query) instead of/alongside `setup_fail_reason`, in the same spirit as the
M22 P0 batch's `6b` proximity-sort addition
(`reports/m22_diagnostics_p0_batch_delivery.md`). No live pipeline rerun
needed — the data to build and validate this already exists in
`prod.duckdb` for all 5 campaign dates.

**(b) Not applicable** — breakout does not short-circuit in a way that
differs from the other three validators for the checks in question; no
core-logic question requiring frozen-module consideration was found.

---

## Anomalies (verbatim)

- The original note's framing ("RVOL, being checked first and failing
  99%+ of the time... could be masking visibility into resistance/
  proximity gate behavior") has the check order backwards: RVOL is check
  #3 in the code, after proximity (#1) and duration (#2). This doesn't
  change the recommended next step (a report-layer multi-reason fix is
  still correct), but it changes *what* that fix needs to surface —
  `stop_below_atr_floor`, not resistance/proximity, is the reason
  category actually hidden behind RVOL's dominance in the current report.
- Proximity, duration, and resistance hard-fail reasons have **zero**
  occurrences anywhere in the 5-date breakout cohort (4692 rows, pass and
  fail). This is consistent with Step 3's routing gate substantially
  pre-filtering on the same conditions before a candidate ever reaches
  M14 — not evidence that M14's checks are unreachable in general (they
  are ordinary code that will fire given the right input; `prox_max` in
  particular is not pre-filtered by Step 3 and remains reachable).

## No writes, no code changes

Read-only investigation only: `m14_setup_validators.py`,
`funnel_diagnostics.py`, and `step3_universal_eligibility.py` were read,
not modified. Data pull was a standalone read-only script against
`data/duckdb/prod.duckdb` (`read_only=True`), same 5-date cohort and
method as the P2-F investigation. No DB writes, no pipeline rerun, no
commit.
