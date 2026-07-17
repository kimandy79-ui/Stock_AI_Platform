# AD-22.26 Option 1 Implementation — consolidation_base Score Rescale

**Date:** 2026-07-15
**Status:** Implemented per architect approval (2026-07-15). Diffs delivered below — no commit made, per current policy.
**Scope:** `validate_consolidation_base()` in `app/services/screening/m14_setup_validators.py` only, plus the ADR text/header fixes. No other setup's scoring, no gate logic, no config, no schema.

---

## 1. ADR header fix

`specs/02b_ARCHITECTURE_DECISIONS.md` §22.26 header changed:

```diff
-## 22.26 consolidation_base score recalibration (pending sign-off)
+## 22.26 consolidation_base score recalibration
```

Confirmed: single-line change, the Status line below it already read `DECIDED` from the prior coder-note pass, so the header now matches.

---

## 2. Implementation diff

`app/services/screening/m14_setup_validators.py`:

```diff
@@ -65,6 +65,20 @@ CONFIDENCE_LOW: Final[str] = "low"
 _CONFIDENCE_HIGH_THRESHOLD: Final[float] = 75.0
 _CONFIDENCE_MEDIUM_THRESHOLD: Final[float] = 50.0

+# AD-22.26 (Option 1, implemented 2026-07-15): consolidation_base's raw
+# penalized_score sits systematically below the shared 55.0 pass threshold
+# (campaign median 41.5-44.9 vs. pullback/trend_continuation's 66-75) because
+# its component formula has no floor tied to its own hard-check conditions,
+# unlike the other three setups. This affine rescale corrects the output
+# scale only — component weights and hard gates are untouched. Anchors:
+# f(100)=100 (ceiling maps to itself, which is what keeps the transform
+# clamp-free over the full [0,100] domain) and f(43.27)=~70.5 (campaign mean
+# p50 -> mean of pullback/trend_continuation p50 from the same campaign).
+# Strictly increasing (slope > 0) => preserves relative ordering among
+# consolidation_base candidates exactly; see tests/test_m14_setup_validators.py.
+_CONSOLIDATION_BASE_RESCALE_A: Final[float] = 0.52
+_CONSOLIDATION_BASE_RESCALE_B: Final[float] = 48.0
+

 def _derive_confidence(
         setup_score: float,
@@ -1471,6 +1485,11 @@ def validate_consolidation_base(

         raw_score = _apply_weights(components, weights)
         penalized_score = _clamp(raw_score + earnings_pen + macro_pen + fundamentals_adj)
+        # AD-22.26: output-scale rescale, applied last and only here (see constant
+        # docstring above) — not to raw_score or any individual component.
+        penalized_score = _clamp(
+            _CONSOLIDATION_BASE_RESCALE_A * penalized_score + _CONSOLIDATION_BASE_RESCALE_B
+        )

         setup_passed = setup_passed_hard and penalized_score >= min_setup_score
```

**Insertion point confirmed**: immediately after the existing `penalized_score = _clamp(raw_score + earnings_pen + macro_pen + fundamentals_adj)` line (previously line 1473), and this is the *only* call site of the transform in the file — grepped for `_CONSOLIDATION_BASE_RESCALE_A`/`_B`: two hits total, the constant definitions and this one application. `raw_score` (stored separately in `evidence_json["raw_score"]`) and every individual `components[...]` value are untouched, so the pre-penalty, pre-rescale component breakdown remains fully auditable in evidence.

---

## 3. Regression tests

Added `TestConsolidationBaseRescaleAD2226` (11 tests) to `tests/test_m14_setup_validators.py`, plus the two new constants added to the module's import block. All four requested categories, plus two extra checks (constant-value pin, min_setup_score-untouched):

| # | Test | What it proves |
|---|---|---|
| 1 | `test_rescale_preserves_ordering_full_integer_domain` | Exhaustive 101×101 pairwise check across every integer old_score in [0,100]: `a>b ⟹ new_a>new_b`, `a==b ⟹ new_a==new_b`. Not a handful of examples. |
| 2 | `test_rescale_preserves_ordering_fractional_domain` | Same property at 0.1 resolution (1001-point strictly-increasing sequence) — catches float/boundary issues the integer grid could miss. |
| 3 | `test_clamp_lower_boundary_matches_proof` | `f(0) == 48.0` exactly. |
| 4 | `test_clamp_upper_boundary_matches_proof` | `f(100) == 100.0` exactly. |
| 5 | `test_clamp_never_fires_across_full_domain` | Clamped and unclamped forms agree at every integer point in [0,100] — the outer `_clamp` is provably a no-op for every legal input. |
| 6 | `test_other_setups_byte_identical_to_pre_ad2226` | Pins `breakout`/`pullback`/`trend_continuation` scores to the exact pre-change values (verified via `git stash`, §4 below). |
| 7 | `test_consolidation_base_component_scores_unaffected` | `evidence_json["raw_score"]` (pre-penalty, pre-rescale) is unchanged and provably ≠ the new `setup_score`. |
| 8 | `test_confidence_shifts_from_medium_to_high_for_the_canonical_good_case` | The module's own canonical good-path fixture: 74.96 (medium) → 86.98 (high), asserts the new behavior. |
| 9 | `test_confidence_shifts_from_low_to_medium_for_a_below_median_case` | A depressed-component fixture landing old-score-equivalent in [30,50] (previously "low") now reads medium/high, never low. |
| 10 | `test_rescale_constants_match_approved_proposal` | Pins A=0.52, B=48.0 against drift. |
| 11 | `test_min_setup_score_threshold_unchanged` | Confirms the shared 55.0 pass threshold itself was not touched — only the score's scale moved. |

**Result:** `python -m pytest tests/test_m14_setup_validators.py -k TestConsolidationBaseRescaleAD2226 -v` → **11 passed**.

---

## 4. Full test-suite results

### 4.1 `tests/test_m14_setup_validators.py` (the module)

```
184 passed in 10.21s
```

All pre-existing tests in the file continue to pass unmodified — none of them assert a literal `consolidation_base` score value (they check pass/fail booleans, hard-fail-reason strings, and self-derived confidence/score-range invariants), so the rescale didn't require touching any existing test.

### 4.2 Isolation proof (byte-identical for the other three setups)

Verified two ways:

**a) `git stash` before/after comparison.** Ran a script computing `setup_score` for the four validators' canonical good-path fixtures (from `tests/test_m14_setup_validators.py`), once against the working tree with this change, once with the change stashed out:

| Setup | Pre-change | Post-change | Match |
|---|---:|---:|:---:|
| breakout | 76.44444444444444 | 76.44444444444444 | ✓ |
| pullback | 75.63470319634703 | 75.63470319634703 | ✓ |
| trend_continuation | 78.34649122807018 | 78.34649122807018 | ✓ |
| consolidation_base | 74.95614961961115 | 86.9771978021978 | changed (expected) |

**b) Pinned regression test** (`test_other_setups_byte_identical_to_pre_ad2226`) bakes the first three values above into the permanent test suite, so any future drift in those three validators is caught automatically, not just at this one point in time.

### 4.3 Broader suite

`tests/test_step5_proposal_engine.py` (Step 5, the largest downstream consumer of `setup_score`) — **all passed** (exit code 0). `tests/test_config_service.py` — **all passed** (exit code 0).

A full `pytest tests/` run was attempted twice and both times failed partway through with `There is not enough space on the disk.` (C:\ was at 0 bytes free — a pre-existing environment condition, already logged in project memory, unrelated to this change: each pytest run accumulates `tmp_path`-fixture DuckDB files under `%TEMP%\pytest-of-kiman`, which had grown to ~1.5GB and exhausted the drive). Cleared that directory twice during this session to unblock targeted runs. **The complete, uninterrupted 40+-file suite was not obtained** — this is an environment limitation, not a code issue; the module suite, the isolation proof, and the two heaviest downstream consumers (Step 5, config service) all passed cleanly. Recommend running the full suite once disk headroom is sorted, ideally from a session that isn't also generating scratch DuckDB files concurrently.

---

## 5. Empirical before/after (existing 5-date campaign data, no new pipeline run)

Recomputed directly from the already-stored `step4_analysis` rows for `consolidation_base` across the 5 campaign dates (2026-06-11, 06-18, 06-26, 07-02, 07-08) — no backfill, no re-run against fresh dates. Hard-fail outcomes (`range_tightness_too_low`, `price_above_base_high`, `price_below_base_low`, `stop_below_atr_floor`, `atr_too_high`, `earnings_too_close`) are untouched by the rescale and were carried forward unchanged; only rows that were previously `score_below_threshold`-only or already-passing were recomputed against the new threshold comparison.

### 5.1 Pass rate per date

| Date | n | Old pass | Old % | New pass | New % |
|---|--:|--:|--:|--:|--:|
| 2026-06-11 | 630 | 16 | 2.54% | 164 | 26.03% |
| 2026-06-18 | 598 | 7 | 1.17% | 145 | 24.25% |
| 2026-06-26 | 612 | 16 | 2.61% | 121 | 19.77% |
| 2026-07-02 | 642 | 8 | 1.25% | 115 | 17.91% |
| 2026-07-08 | 633 | 11 | 1.74% | 127 | 20.06% |
| **Total** | **3115** | **58** | **1.86%** | **672** | **21.57%** |

Sanity check requested by the coder note: pass rate rises meaningfully (1.86% → 21.57% aggregate) but stays well short of 100% — `range_tightness_too_low` and `price_above_base_high` still gate independently and remain the two largest rejection reasons by a wide margin. This is the expected shape, not a runaway pass-everything outcome.

### 5.2 Failure-reason breakdown (aggregate across all 5 dates)

| Reason | Old count | Old % | New count | New % |
|---|--:|--:|--:|--:|
| range_tightness_too_low | 1206 | 38.7% | 1206 | 38.7% |
| price_above_base_high | 842 | 27.0% | 842 | 27.0% |
| **score_below_threshold** | **614** | **19.7%** | **0** | **0.0%** |
| price_below_base_low | 322 | 10.3% | 322 | 10.3% |
| **passed** | **58** | **1.9%** | **672** | **21.6%** |
| stop_below_atr_floor | 61 | 2.0% | 61 | 2.0% |
| atr_too_high | 9 | 0.3% | 9 | 0.3% |
| earnings_too_close | 3 | 0.1% | 3 | 0.1% |

Exactly as predicted in the approved proposal (§B.5): `score_below_threshold` — previously the #2/#3 failure reason at 19.7% of the population — drops to 0%, and `range_tightness_too_low` / `price_above_base_high` become the sole effective gates, converging `consolidation_base`'s behavior toward how the ADR describes the other three setups already working (structural gates do the filtering; score is rarely the actual blocker).

### 5.3 Live Step 5 before/after — concrete candidates

Recomputed exactly (not approximated) from the *already-stored* `step5_proposals` rows: since `_proposal_score_raw`'s only setup-score-dependent term is `_W_SETUP * setup_score` (W_SETUP=0.40) and `_compute_risk_score`'s only setup-score-dependent term is `setup_confirmation = 100 − setup_score` (weight 0.10, from the active `risk_label_config_v1.factor_weights`), every other input (RR, market regime, stop quality, liquidity, etc.) is unchanged by this AD, so the delta is exact algebra, not simulation — confirmed none of the three examples below sit at a clamp boundary (0 or 100) either before or after.

| Ticker (date) | setup_score | proposal_score_raw | risk_score | risk_label | disposition (old) |
|---|---|---|---|---|---|
| **HTGC** (2026-07-02) | 59.29 → **78.83** (+19.54) | 59.09 → **66.91** (+7.82) | 30.33 → **28.37** (−1.95) | low → low | **BUY**, raw_rank 3, diversified_rank 2 |
| AVNS (2026-06-11) | 66.87 → 82.77 (+15.90) | 51.94 → 58.30 (+6.36) | 33.58 → 31.99 (−1.59) | **medium → low** | WATCHLIST_ONLY, raw_rank 63, diversified_rank 33 |
| OGN (2026-07-02) | 85.39 → 92.40 (+7.01) | 50.89 → 53.69 (+2.81) | 38.48 → 37.78 (−0.70) | medium → medium | WATCHLIST_ONLY, raw_rank 369, diversified_rank 57 |

**HTGC** is the only `consolidation_base` candidate with `disposition = BUY` anywhere in the 5-date sample and was already ranked #3 raw / #2 diversified — its `proposal_score_raw` climbs another ~7.8 points post-rescale, which (holding the rest of that date's candidate pool fixed) would very likely improve its rank further. **AVNS** crosses a `risk_label` boundary outright (medium → low, `low_max=33`), a discrete label change directly attributable to the rescale, not just a score wobble.

### 5.4 Known gap — newly-passing candidates' trade plans not simulated

The 614 rows that flip `score_below_threshold` → `passed` (§5.2) were previously stored with `disposition = REJECTED` and **no stop/target/RR/risk_score at all** — per the module's own comment, "Only setup-valid rows (setup_passed = TRUE) get stop/target/risk; failed → REJECTED." Determining their actual post-rescale disposition (BUY vs. WATCHLIST_ONLY vs. still-REJECTED-on-other-grounds) requires running M15's stop/target/RR computation for the first time on each of them — that's a live Step 4 → Step 5 re-run against the existing 5 dates, not something derivable from already-stored data by algebra. **Flagging this as the natural next validation step**, not completing it here, given the disk-space constraints encountered in §4.3 and the scope of this note (implementation + proof, not a full pipeline re-run). The 3 candidates in §5.3 were deliberately chosen from the *already-passing* population specifically so their before/after could be computed exactly rather than estimated.

### 5.5 Ranking-pool / diversity-cap interaction

Not independently verified. Since `consolidation_base`'s routed-and-passing population grows roughly 10x on every date (§5.1), and Step 5 ranks across all four setup types in one shared pool (per CLAUDE.md), the composition of `raw_rank`/`diversified_rank`/`top_n` and any sector/industry diversification caps could plausibly shift for *other* setups' candidates too, not just `consolidation_base`'s own. Confirming this requires the same live re-run named in §5.4 (recomputing the full candidate pool's ranking, not just three isolated tickers). Flagged, not verified — consistent with declining the same live-re-run cost noted above for the same reason.

---

## 6. Anomalies / unexpected findings

1. **`_apply_weights`'s inactive normalization branch.** While reading the formula, found that `_apply_weights()` has a branch (`total_score / total_w * 100.0 if total_w != 1.0`) that would produce values far outside [0,100] if any setup's `scoring_weights` didn't sum to exactly 1.0 — none currently do (breakout/pullback/trend_continuation/consolidation_base all sum to 1.0 exactly), so this is dormant, not a live bug, and out of scope for this AD. Noting it for the record since it's adjacent code encountered during the investigation, not something to act on now.
2. **Environment: C:\ drive space.** Confirmed the pre-existing "C drive full" issue from project memory recurs readily — a single `pytest tests/test_step5_proposal_engine.py` run alone regenerated enough `tmp_path` DuckDB scratch data to refill the ~1.5GB just freed. This blocked obtaining a full-suite result (§4.3) and will keep blocking any full-suite run on this machine until addressed at the environment level (e.g., `pytest --basetemp` pointed at a drive with headroom, or a CI-side pytest tmp-retention policy). Not caused by this change, but worth escalating since it's now blocked two separate coder-note deliverables.
3. **No double-counting risk found.** Checked `simulation_engine.py`, `dashboard/data_access.py`, and `dashboard/ticker_report.py` for hardcoded `setup_score` range assumptions beyond the two already flagged in the investigation note (`_derive_confidence`'s 75/50 thresholds, Step 5's `_W_SETUP`/`setup_confirmation` terms) — none found; those three files only pass `setup_score` through to storage/display.

---

## Deliverables checklist

1. ✅ ADR header fix — confirmed, single-line diff.
2. ✅ Diff for `m14_setup_validators.py` — §2, insertion point and isolation confirmed.
3. ✅ Regression tests — 11 new tests, all 4 requested categories, all passing.
4. ✅ Module test-suite result — 184/184 passed; isolation proof for the other 3 setups (stash-verified + pinned test); broader suite partially blocked by a pre-existing environment issue (§4.3, §6.2).
5. ✅ Empirical before/after — pass-rate and failure-reason shift (§5.1–5.2), 3 concrete Step 5 candidates (§5.3), one known gap flagged rather than glossed over (§5.4), diversity-cap interaction flagged as unverified (§5.5).
6. ✅ Anomalies — §6.

No commit made. Diffs above are ready for review/commit at the architect's discretion.
