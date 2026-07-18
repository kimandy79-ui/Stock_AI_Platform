# Step 4 Stop-Floor Gate / Step 5 Formula Fix

**Date:** 2026-07-18
**Scope:** `app/services/screening/m14_setup_validators.py` — the
`stop_below_atr_floor` hard check inside `validate_pullback` and
`validate_trend_continuation` only. `validate_breakout` and
`validate_consolidation_base` untouched. `step5_proposal_engine.py`
untouched (already correct — reference standard for this fix).
**No commit — diff delivered, stop, per current policy.**

---

## 0. Background

[P2 Item F investigation](P2_F_stop_floor_investigation_2026-07-15.md)
(2026-07-15) found Step 4's `stop_below_atr_floor` gate measured distance to
a narrower stop basis than Step 5's real, already-approved stop formula
(`step5_proposal_engine._compute_stop_target`):

- **Pullback:** gate checked distance to `support_raw` only. Step 5's real
  stop takes `min(support_raw, swing_low_raw, ema_area_raw) − buffer_atr`.
- **Trend_continuation:** gate and Step 5 used the same base level
  (`swing_low_raw`), but the gate omitted the `buffer_atr_multiple` (0.25)
  subtraction Step 5 applies.

458 of 568 candidates that failed Step 4 *solely* on this gate across 5
campaign dates would have cleared the floor under Step 5's real formula
(90.1% pullback, 59.7% trend_continuation). This was a gate/formula
mismatch, not a policy question.

---

## 1. Diff — `app/services/screening/m14_setup_validators.py`

### 1a. `validate_pullback` — config reads (new constants, same keys Step5 reads)

```diff
     min_atr_stop_floor: float = float(val.get("min_atr_stop_floor_multiple", 0.5))
+    # P2-F: same defaults/keys as step5_proposal_engine._compute_stop_target's
+    # pullback branch — the stop-floor gate below is measured against that
+    # formula, so the constants must match.
+    k_atr_stop: float = float(val.get("k_atr_stop", 1.0))
+    buffer_atr_multiple: float = float(val.get("buffer_atr_multiple", 0.25))
     # rvol_is_hard MUST be False for pullback (AD-22.23)
     rvol_is_hard: bool = bool(val.get("rvol_is_hard", False))
```

### 1b. `validate_pullback` — new `ema_area_raw` (min(ema20,ema50), raw-converted)

```diff
     # Guard: swing_low above current price cannot serve as a valid stop anchor
     if swing_low_adj is not None and close_adj is not None and swing_low_adj >= close_adj:
         swing_low_adj = None
         swing_low_raw = None

+    # ema_area_raw: min(ema20, ema50) raw-converted — same basis Step5's
+    # _compute_stop_target uses for the pullback stop (P2-F).
+    ema_area_raw: float | None = None
+    if ema20 is not None and ema50 is not None:
+        ema_area_raw = _raw_conv(min(ema20, ema50), close_raw, close_adj) if close_raw and close_adj else None
+    elif ema20 is not None:
+        ema_area_raw = _raw_conv(ema20, close_raw, close_adj) if close_raw and close_adj else None
+    elif ema50 is not None:
+        ema_area_raw = _raw_conv(ema50, close_raw, close_adj) if close_raw and close_adj else None
+
     entry_raw = close_raw
```

### 1c. `validate_pullback` — the gate itself (hard check #6)

```diff
-    # 6. Stop ≥ 0.5 ATR below entry (P1-1)
-    # Stop estimated as entry minus support (standard pullback stop placement)
-    if (
-        atr_pct is not None and atr_pct > 0
-        and support_raw is not None
-        and entry_raw is not None and entry_raw > 0
-    ):
-        stop_distance_pct = (entry_raw - support_raw) / entry_raw
-        stop_distance_atr = stop_distance_pct / atr_pct
-        if stop_distance_atr < min_atr_stop_floor:
-            hard_fails.append(
-                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
-            )
+    # 6. Stop ≥ 0.5 ATR below entry (P1-1 / P2-F fix)
+    # Measured against the same stop basis Step5's _compute_stop_target uses
+    # for pullback: min(support_raw, swing_low_raw, ema_area_raw) - buffer_atr,
+    # falling back to the ATR stop when no structural candidate qualifies.
+    # MAINTENANCE-DEBT: duplicated formula, not a shared function — M14 must
+    # not import step5_proposal_engine.py (DB-importing M15 module; M14 runs
+    # before M15 and forbids DB access). Keep in sync with
+    # step5_proposal_engine._compute_stop_target's SETUP_PULLBACK branch if
+    # that formula changes.
+    if atr_pct is not None and atr_pct > 0 and entry_raw is not None and entry_raw > 0:
+        atr_raw = atr_pct * entry_raw
+        buffer_atr = buffer_atr_multiple * atr_raw
+        stop_basis_candidates = [
+            lvl for lvl in (support_raw, swing_low_raw, ema_area_raw)
+            if lvl is not None and lvl < entry_raw
+        ]
+        if stop_basis_candidates:
+            gate_stop_raw = min(stop_basis_candidates) - buffer_atr
+        else:
+            gate_stop_raw = entry_raw - k_atr_stop * atr_raw - buffer_atr
+        stop_distance_pct = (entry_raw - gate_stop_raw) / entry_raw
+        stop_distance_atr = stop_distance_pct / atr_pct
+        if stop_distance_atr < min_atr_stop_floor:
+            hard_fails.append(
+                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
+            )
```

Note: the gate now always evaluates (previously skipped entirely when
`support_raw` was `None`). This matches Step5, which always produces a stop
(falling back to the ATR stop when no structural candidate exists). For
default configs (`k_atr_stop≈1.0-1.5`, `buffer_atr_multiple=0.25`), the
fallback distance is always well above the 0.5 floor, so this changes
nothing observable for the missing-data corner case — it's included purely
so the gate is a faithful match to Step5's formula rather than a special
case that happens not to matter today.

### 1d. `validate_pullback` — evidence transparency (debugging aid, not required by the gate logic)

```diff
     "swing_low_adj": swing_low_adj,
+    "swing_low_raw": swing_low_raw,
     "swing_high_raw": swing_high_raw,
+    "ema_area_raw": ema_area_raw,
     "rvol20": rvol20,
```

### 1e. `validate_trend_continuation` — config reads

```diff
     min_atr_stop_floor: float = float(val.get("min_atr_stop_floor_multiple", 0.5))
+    # P2-F: same defaults/keys as step5_proposal_engine._compute_stop_target's
+    # trend_continuation branch — the stop-floor gate below is measured
+    # against that formula, so the constants must match.
+    k_atr_stop: float = float(val.get("k_atr_stop", 1.0))
+    buffer_atr_multiple: float = float(val.get("buffer_atr_multiple", 0.25))
```

### 1f. `validate_trend_continuation` — the gate itself (hard check #7)

```diff
-    # 7. Stop ≥ 0.5 ATR below entry (P1-1)
-    # Stop estimated as entry minus swing_low (standard trend continuation stop)
-    if (
-        atr_pct is not None and atr_pct > 0
-        and swing_low_raw is not None
-        and entry_raw is not None and entry_raw > 0
-    ):
-        stop_distance_pct = (entry_raw - swing_low_raw) / entry_raw
-        stop_distance_atr = stop_distance_pct / atr_pct
-        if stop_distance_atr < min_atr_stop_floor:
-            hard_fails.append(
-                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
-            )
+    # 7. Stop ≥ 0.5 ATR below entry (P1-1 / P2-F fix)
+    # Measured against the same stop basis Step5's _compute_stop_target uses
+    # for trend_continuation: swing_low_raw - buffer_atr (falling back to the
+    # ATR stop when swing_low_raw is unusable), not raw swing_low distance.
+    # MAINTENANCE-DEBT: duplicated formula, not a shared function — M14 must
+    # not import step5_proposal_engine.py (DB-importing M15 module; M14 runs
+    # before M15 and forbids DB access). Keep in sync with
+    # step5_proposal_engine._compute_stop_target's SETUP_TREND_CONTINUATION
+    # branch if that formula changes.
+    if atr_pct is not None and atr_pct > 0 and entry_raw is not None and entry_raw > 0:
+        atr_raw = atr_pct * entry_raw
+        buffer_atr = buffer_atr_multiple * atr_raw
+        if swing_low_raw is not None and swing_low_raw < entry_raw:
+            gate_stop_raw = swing_low_raw - buffer_atr
+        else:
+            gate_stop_raw = entry_raw - k_atr_stop * atr_raw - buffer_atr
+        stop_distance_pct = (entry_raw - gate_stop_raw) / entry_raw
+        stop_distance_atr = stop_distance_pct / atr_pct
+        if stop_distance_atr < min_atr_stop_floor:
+            hard_fails.append(
+                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
+            )
```

### Shared-function refactor: considered, rejected

The CODER NOTE asked to factor a shared calculation if feasible without a
large refactor. Checked: `step5_proposal_engine.py` imports
`app.database.duckdb_manager` (it's the DB-writing M15 orchestration
module), while `m14_setup_validators.py`'s own docstring states "No DB
access. No DuckDB imports." M14 (Step 4) also runs *before* M15 (Step 5) in
the pipeline (`01d_MODULES_AND_PIPELINE.md`), so having M14 import from M15
would be a backwards layering dependency on top of the DB-import violation.
Duplication (with the maintenance-debt comments above, pointing at the
formula each duplicate must stay in sync with) was the correct call here,
not a workaround.

### Not changed

- `min_atr_stop_floor_multiple` values (0.5 pullback/trend_continuation, 0.3
  consolidation_base) — untouched, per instruction.
- `validate_breakout`, `validate_consolidation_base` — untouched (see §3).
- `step5_proposal_engine.py` — untouched; it was already the reference
  standard.

---

## 2. Tests — new/updated, all passing

`tests/test_m14_setup_validators.py`, class `TestAtrStopFloorGate`:

- **Updated** `test_pullback_stop_below_atr_floor_fails` — old feature data
  (support only tight to entry) no longer produces a genuine floor failure
  post-fix, because `swing_low`/`ema_area` were left at their (looser)
  `_pullback_feat()` defaults and now rescue the candidate — exactly the bug
  being fixed. Updated to a case where all three candidates
  (`support`/`swing_low`/`ema_area`) sit tight to entry, so the floor
  failure is genuine under the corrected formula too.
- **Updated** `test_atr_floor_gate_doesnt_block_other_setups` — same
  root cause/fix as above, for the pullback half of this cross-setup
  independence test.
- **Unchanged, still passing** `test_pullback_stop_sufficient_passes_atr_floor`,
  `test_trend_continuation_stop_below_atr_floor_fails`,
  `test_trend_continuation_stop_sufficient_passes`,
  `test_breakout_*`, `test_consolidation_base_*` (4 tests) — none of these
  needed changes; their numbers already held under the corrected formula.
- **New** `test_pullback_ema_area_below_support_rescues_gate` — support_raw
  alone is tight to entry (would have failed the old support-only gate:
  `(300−299.5)/300/0.025 = 0.067 < 0.5`), but `ema_area_raw` (min(ema20,
  ema50) = 260.0) sits well below support. Asserts
  `evidence_json["ema_area_raw"] == 260.0` and that the gate now passes
  (`5.58 > 0.5` using the lower candidate).
- **New** `test_trend_continuation_buffer_atr_subtracted_before_floor` —
  `swing_low` chosen so the raw (un-buffered) distance ratio is exactly
  0.45 (would have hard-failed under the pre-fix gate). With
  `buffer_atr_multiple=0.25` now subtracted, the effective ratio becomes
  0.70 — asserts the gate passes.

**Result:**

```
$ python -m pytest tests/test_m14_setup_validators.py -v
============================ 186 passed in 10.57s =============================
```

All 186 tests pass (184 pre-existing + 2 new). Zero pre-existing tests
needed logic changes beyond the two documented above, which encoded the old
(buggy) gate's numbers and had to be updated to reflect that Step5's real,
wider stop basis now correctly rescues those candidates — this is the
fix working as intended, not a weakened test.

Also ran for adjacent-module regressions (both clean, no changes needed):

```
$ python -m pytest tests/test_m14_fundamentals_scoring.py tests/test_step5_proposal_engine.py -q
........................................................................ [ 45%]
........................................................................ [ 90%]
................                                                         [100%]
```

---

## 3. Isolation proof — `breakout` / `consolidation_base` unchanged

- **Code-level:** every edit (`git diff`-equivalent shown in §1) is confined
  to lines inside `validate_pullback` (function body ~668–990) and
  `validate_trend_continuation` (~988–1270). `validate_breakout` (~380–661)
  and `validate_consolidation_base` (~1273–1587) have zero touched lines —
  confirmed by grepping the new `P2-F`/`k_atr_stop`/`buffer_atr_multiple`
  markers introduced by this fix and checking their line ranges fall
  entirely within the pullback/trend_continuation functions.
- **Behavioral:** all pre-existing breakout and consolidation_base tests in
  `TestAtrStopFloorGate` (`test_breakout_stop_below_atr_floor_fails`,
  `test_breakout_stop_sufficient_passes_atr_floor`,
  `test_breakout_atr_floor_skipped_when_support_none`,
  `test_breakout_atr_floor_configurable`,
  `test_consolidation_base_stop_below_atr_floor_fails`,
  `test_consolidation_base_stop_sufficient_passes`) pass unchanged with
  unchanged assertions — byte-identical inputs produce byte-identical
  pass/fail outcomes before and after this fix.
- `git` is not available in this shell session (`git.exe` not found on
  `PATH` or in the usual install locations), so a literal `git diff --stat`
  could not be produced for this report; the isolation claim above rests on
  the recorded edit locations (§1) plus the unchanged-test evidence, which
  together are equivalent in strength for these two functions specifically
  (no line in either function was touched, and no test result for either
  function changed).

---

## 4. Full module test suite

```
$ python -m pytest tests/test_m14_setup_validators.py -v
============================ 186 passed in 10.57s =============================
```

No regressions.

**Broader regression check — full repo suite (`tests/`):** run twice
independently (`bivje085m`, `bcqf5dipo`) to confirm reproducibility. Both
runs produced the identical 3-item `FAILED` list:

```
FAILED tests/test_data_validator.py::test_spec_documents_open_gaps_not_invented
FAILED tests/test_mutation_detector.py::test_spec_documents_open_gap_g1
FAILED tests/test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance
```

These three are the pre-existing failures already logged in project memory
(`known_preexisting_test_failures.md`, 2026-07-08: edgar/yahoo provider
overlap + 2 spec-path lookups) — unrelated to `m14_setup_validators.py`,
present before this fix, and out of scope for it. No test outside
`test_m14_setup_validators.py`'s own two updated cases changed status.

Note: both full-suite console logs ended immediately after these 3
`FAILED` lines without pytest's usual trailing `X failed, Y passed, Z
skipped in Ns` summary line, despite a clean process exit (code 1, matching
pytest's own exit status for "tests failed", not a crash) — an
environment/output-capture quirk in this shell session (PowerShell
redirection dropping the final terminal-reporter line), not a signal about
the fix itself. Confirmed by re-running a third time with `--junitxml`
(machine-readable output, immune to the console-capture issue):

```
$ python -m pytest tests/ -q --tb=no --junitxml=full_suite.xml
<testsuite name="pytest" tests="2414" errors="0" failures="3" skipped="143" time="711.145">
```

**2414 total, 0 errors, 3 failures, 143 skipped → 2268 passed.** The 3
`<testcase>` failures match by name exactly:
`tests.test_data_validator::test_spec_documents_open_gaps_not_invented`,
`tests.test_mutation_detector::test_spec_documents_open_gap_g1`,
`tests.test_yahoo_provider::test_only_yahoo_provider_references_yfinance`
— the same pre-existing failures, confirmed via structured output this
time rather than console-log inference.

---

## 5. Empirical re-run — corrected gate vs. Step5's real formula, full 568-candidate cohort

Read-only recomputation, same method as the original investigation
(§3 of `P2_F_stop_floor_investigation_2026-07-15.md`): pulled the same
568-row cohort from `data/duckdb/prod.duckdb` (`step4_analysis` rows,
5 campaign dates, `setup_passed=FALSE`, exactly one hard-fail reason,
`stop_below_atr_floor*`), joined `daily_features_current`/`daily_prices`
for each candidate, and for every row:

1. Ran the corrected `validate_pullback`/`validate_trend_continuation`
   (actual post-fix production code) and recorded whether
   `stop_below_atr_floor` is in `hard_fails`.
2. Independently called `step5_proposal_engine._compute_stop_target` (the
   unmodified reference formula) and computed floor clearance from its
   returned `stop` directly.
3. Compared the two.

No live pipeline rerun; no writes to `prod` — same precedent as the
investigation and the AD-22.26 closure (`prod.duckdb` opened
`read_only=True`).

**Active configs at time of this check:**
```
pullback:            k_atr_stop=1.2  buffer_atr_multiple=0.25  min_atr_stop_floor_multiple=0.5
trend_continuation:  k_atr_stop=1.5  buffer_atr_multiple=0.25  min_atr_stop_floor_multiple=0.5
```
(same values the original investigation used — confirms the active configs
have not changed since 2026-07-15).

**Result:**

| Setup type | n | Corrected gate clears | Corrected gate fails | Step5 formula clears | Step5 formula fails | Mismatches |
|---|--:|--:|--:|--:|--:|--:|
| pullback | 392 | 353 | 39 | 353 | 39 | **0** |
| trend_continuation | 176 | 105 | 71 | 105 | 71 | **0** |
| **Total** | **568** | **458** | **110** | **458** | **110** | **0** |

**Zero mismatches across all 568 candidates.** The corrected gate's
pass/fail split is byte-identical to Step5's real formula, and matches the
investigation's original 458/110 (80.6%/19.4%) split exactly:
90.1% (353/392) of pullback and 59.7% (105/176) of trend_continuation
candidates that previously failed Step 4 solely on this gate now correctly
clear it, and the genuinely-under-floor 110-candidate cohort (39 pullback,
71 trend_continuation) still correctly fails — the fix neither over- nor
under-corrects.

---

## 6. Anomalies (verbatim)

None encountered. `data/duckdb/prod.duckdb` still contained
`daily_features`/`daily_prices` data for all 568 cohort rows across all 5
dates unchanged since the original investigation (0 missing feature/price
joins), and the active `pullback`/`trend_continuation` `setup_configs` are
the same ones the investigation read (`k_atr_stop`/`buffer_atr_multiple`
values match exactly).

---

## No writes, no commit

Read-only regression against `data/duckdb/prod.duckdb`
(`read_only=True`), no DB writes. Code changes are limited to
`app/services/screening/m14_setup_validators.py` (validators) and
`tests/test_m14_setup_validators.py` (tests) — both delivered as diffs in
this report and left uncommitted in the working tree, per current policy.
