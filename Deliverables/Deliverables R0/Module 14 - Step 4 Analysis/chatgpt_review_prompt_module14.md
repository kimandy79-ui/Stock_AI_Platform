# ChatGPT Review Prompt — Module 14: Step 4 Setup Analysis

Paste this prompt into ChatGPT (with Project Instructions and Project Files attached).

---

```
Review the attached implementation for Module 14 — Step 4 Setup Analysis.

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not rely on archived monolithic source files or old FULL/PATCH/MINI PATCH files
if split Project Files are available.

Attached:
1. `stock_ai_platform_module14_stable.zip`
2. `M14_STEP4_ANALYSIS_SPEC.md`
3. pytest output or test notes (if available)

Context:
- Modules 01–13 were previously accepted and should remain frozen.
- Claude was instructed to implement only Module 14.
- Module 15 (Step 5 Proposal Engine) and later are out of scope.
- The `step4_analysis` table already exists in the frozen schema; no DDL
  should have been added.
- The engine reads `step3_candidates` (passed_hard_filters = TRUE) for the
  given signal_date and strategy_config_id, joins `daily_features_current`
  and `daily_prices`, computes setup classification + scores + RR, and
  inserts one row per analyzable candidate into `step4_analysis`.
- Tests must be fully offline (tmp_path, fakes, monkeypatching — no real DB
  files, no network).

Review goals:

1. **Scope compliance and frozen-module protection.**
   Confirm only additive changes: new `app/services/analysis/` package,
   `tests/test_step4_analysis.py`, `M14_STEP4_ANALYSIS_SPEC.md`, and an
   additive README note. No Module 01–13 files should be modified except
   README (append only).

2. **Correctness against `M14_STEP4_ANALYSIS_SPEC.md` and Project Files.**
   - db_role guard: only prod/debug allowed; simulation rejected before any
     DB access.
   - Config validation before DB access: step4.target_R (> 0),
     earnings.avoid_within_bd (int >= 0), earnings.penalty_points_max (<= 0),
     macro_event_risk.enabled (bool), macro_event_risk.penalty_points (<= 0),
     screening.min_rvol (> 0).
   - Entry proxy: close_raw on signal_date (no look-ahead).
   - ATR raw-equivalent: atr14 * (close_raw / close_adj) when both present
     and close_adj != 0; fallback to atr14; fallback to None.
   - Stop: min(recent_20d_low_raw, entry - 1.5 * atr14_raw_equivalent) over
     available candidates; clamp to entry * 0.95 (stop_clamped = True) when
     inputs are missing/invalid or result >= entry.
   - Target / RR: entry + target_R * (entry - stop); RR = None when denom <= 0.
   - Setup classification priority (first match wins):
     high_tight_flag → breakout → volatility_squeeze →
     trend_pullback → trend_resume → unknown.
     NULL required inputs must make a clause false (no crashes).
     trend_resume requires >= 3 of the prior 10 rows with close_adj < ema20;
     skipped (falls through) when prior history is unavailable.
   - Scoring: breakout_quality (0.5 * position_sub + 0.5 * rvol_sub),
     squeeze (consolidation_score or 0), timing (0.4 * rsi + 0.3 * ema_align
     + 0.3 * sector_rs; sector_rs NULL → neutral 50), confirmation
     (50 * I(close_adj > ema200) + 50 * I(ema20 > ema50)). All 0–100 clamped.
   - setup_score: clamp(equal-weight mean of 4 components + earnings_penalty
     + macro_penalty, 0, 100).
   - Earnings penalty: 0 when days_to_earnings_bd is None; linear decay within
     avoid_within_bd; boundary semantics when avoid_within_bd == 0.
   - Macro penalty: flat penalty_points when enabled = True and
     macro_event_risk_flag = True; else 0.0.
   - explanation_json: sorted-key JSON with all required fields.
   - Metadata keys present on every return path: db_role, signal_date,
     strategy_config_id, run_id, candidates_evaluated, analyses_written,
     estimated_rr_min, estimated_rr_max, estimated_rr_mean (None when zero
     written), setup_type_counts ({} when none). rows_processed ==
     analyses_written always.

3. **Architecture boundaries.**
   - No `import duckdb` in the engine.
   - No provider imports or network access.
   - No `print()` calls.
   - No DDL, no `ATTACH`, no `UPDATE`/`DELETE`.
   - Only one INSERT target: `step4_analysis`.
   - All DB access via the shared duckdb_manager.

4. **Not-analyzable candidate handling.**
   Candidates with no current feature row or no usable close_raw (entry <= 0
   or None) must be counted in candidates_evaluated but not written. No
   partial rows or crashes.

5. **Transaction model.**
   Single BEGIN TRANSACTION / COMMIT for the write phase. Per-row execute().
   ROLLBACK on any write error with zero analyses_written and failed result.

6. **Test quality.**
   Check coverage for: role guards, all config validation branches,
   empty qualifying candidates, signal-date/config isolation, debug role,
   not-analyzable skip, stop/target/RR + clamp, ATR ratio + fallback,
   recent-20d-low with < 20 rows, RR = None when denom <= 0, all six
   classifications + priority + NULL handling + trend-resume history,
   component score boundaries, earnings + macro penalty edge cases (NULL /
   avoid_within_bd = 0 / day 0 / boundary / outside), sector-RS NULL → 50,
   score clamp, single-transaction rollback, only step4_analysis written,
   unique uuid4 ids, candidate_id preserved, RR stats + setup_type_counts,
   static scans (imports, print, DDL/ATTACH, INSERT target).
   Tests must be fully offline with no real DB files.

7. **Spec accuracy.**
   Check that `M14_STEP4_ANALYSIS_SPEC.md` accurately reflects the
   implementation: public API, metadata keys, input joins, missing-data
   behavior, formulas, classification priority, scoring weights, penalties,
   explanation_json fields, config validation, transaction model, and
   documented open assumptions (G-ATR-CONTRACTION, G-TREND-RESUME-HISTORY,
   G-SCORING-SUBCOMPONENT-WEIGHTS, G-MISSING-ATR-OR-PRICE).

8. **Verdict.**
   Recommend one of: ACCEPT / ACCEPT WITH MINOR FIXES / REJECT.
   Provide only concrete findings and fixes. Do not rewrite the whole module
   unless necessary.
```
