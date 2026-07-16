# Part C — Stage-1 Wiring Smoke Test: COMPLETE (Option A)

**To:** Architect
**From:** Coder
**Date:** 2026-07-14
**Re:** CODER NOTE — Finalize Date Selection + Targeted Debug Seeding (Part C), Option A
**Status:** ✅ **Done.** Diagnostics ran clean against a real replay; sections 1b / 5 / 6b all render without exceptions and are non-empty. **One pre-existing platform bug found and routed around (not fixed) — needs its own scoped note (§4).**
**Artifact:** `funnel_stage1_check.json` (47 KB, repo root), `run_id=fd593ae3-fd41-4865-af22-b26cc79d4b83`.
**Writes:** `debug.duckdb` only (seeded 50 tickers + ran step3→step5). No prod writes. No commit.

---

## 1. Result — all target sections populated

Smoke date **2026-07-08**, `db_role=debug`, 50 tickers (first-50-alphabetical active). Real pipeline output:

```
step3 candidates = 50   (routed 23 · ineligible 23 · no_route 4)
step4 analyses   = 46   (breakout 11 · pullback 10 · trend_continuation 17 · consolidation_base 8; 15 multi-routed)
step5 proposals  = 42   (WATCHLIST 10 · REJECTED 32 · BUY 0)
daily_features   = 50 / 50 feature_ready
```

| Section | Renders | Non-empty | Content |
|---|---|---|---|
| **1b** eligibility rejections | ✅ | ✅ | `liquidity_below_min` 20 (87%), `price_below_min` 10 (43.5%) |
| **5** evidence (routed vs validated) | ✅ | ✅ | routed setup_score stats for all 4 setups; validated stats (rvol/atr_pct/ema20-dist/ema50-dist) for pullback (n=5) and trend_continuation (n=9) |
| **6b** borderline nearest-to-threshold | ✅ | ✅ | direction-aware distances for all 4 setups (e.g. pullback AAL 0.8%, trend ACMR 0.0%, consolidation ACN 2.7%, breakout AAT 8.7%) |

All other sections (1, 2, 3a, 3b, 4, 6, 7, 8, 9) also render correctly. The M22 P0 diagnostics batch (`fb5a36d`) is confirmed working against a real replay, not just the synthetic fixture.

---

## 2. Deliverable 2 & 3 (from prior note) — confirmed empirically

- **Sample logic:** `sorted(active_tickers)[:N]` = first-N alphabetical, deterministic — but the base provider must be given a symbol source (bare `YahooProvider()` returns empty). Here I injected a 50-`TickerInfo` source.
- **Feature recompute:** the debug controller **always resumes from `universe_ingestion`**, and the orchestrator's cleanup **deletes `daily_features` for the signal_date whenever `resume_idx ≤ feature_calculation`** (`pipeline_orchestrator.py:832-834`). So **seeding `daily_features` is futile** — it gets deleted and the feature step is expected to recompute. Confirmed: my first attempt seeded features → they were wiped → all 50 ineligible (`feature_not_ready`).

---

## 3. What I actually did (Option A, adjusted to the two facts in §2)

The specced "seed daily_prices + daily_features, run `config_tuning_test`" could not work as written (features get deleted; `config_tuning_test` scopes step3 into the broken proxy in §4). The working bounded path — **existing components only, no new pipeline logic, zero network:**

1. **Seed `debug.duckdb`** (read prod → write debug): 50 tickers' `ticker_master` + **full `daily_prices` history** 2025-06-02 → 2026-07-08 (13,800 rows) so the recompute has ≥252 bars. **No `daily_features` seed** (would be deleted).
2. **Inject** `YahooProvider(symbol_source=<50 TickerInfo>)` so the run scopes to exactly those 50.
3. **Run a custom `DebugRunPlan`** `start_step=feature_calculation … end_step=step5_proposals` via `DebugModeController.run(plan)`:
   - benchmark / price / validation / mutation ingestion = **noop → no network**;
   - feature step **active + scoped** to the 50 → recomputes `feature_ready=True` from the seed;
   - step3 runs **unscoped** (real engine) — which also sidesteps the §4 bug entirely.
4. `run_funnel_diagnostics.py --date 2026-07-08 --db-role debug --json-out funnel_stage1_check.json`.

This deviates from the literal CLI (`run_debug_pipeline.py` exposes no provider injection, no custom step range, no watchlist), but stays within Option A's "bounded driver via the controller."

---

## 4. ⚠️ Pre-existing bug found (routed around, NOT fixed) — needs its own note

**`_ScopedStep3UniversalProxy.run()` in `debug_mode.py` has a stale strategy-mode signature.** It requires `setup_config_id` / `setup_config` and delegates to `self._real.run(setup_config_id=…, setup_config=…)`, but setup-mode's `Step3UniversalEligibilityEngine.run(signal_date, db_role, run_id, setup_configs=None)` takes none of those, and the orchestrator calls step3 as `run(signal_date, db_role, run_id)` (`pipeline_orchestrator.py:1323-1329`).

**Impact:** any debug run that triggers step3 scoping — i.e. **the shipped `config_tuning_test` preset**, or any preset where the feature step is a noop and screening runs — crashes with:
```
_ScopedStep3UniversalProxy.run() missing 2 required positional arguments: 'setup_config_id' and 'setup_config'
```
I hit this on the first attempt and confirmed the root cause. My final run avoids the scoped-step3 path (step3 unscoped), so **I did not modify any platform code** — per the guardrail, a fix belongs in its own scoped note. The proxy also needs the `_read_features` ticker-filter re-expressed against the setup-mode step3, since its scoping intent is otherwise dead.

---

## 5. Observations from the diagnostics (real signal, not wiring)

Two things surfaced that are **expected for this date/sample**, not defects — flagging so they aren't mistaken for wiring issues, and **not** to be tuned from one date (per prior guidance):
- **breakout: 0/11 passed — all failed `rvol_below_hard_threshold`** (RVOL < 1.5). 2026-07-08 is a low-vol day (median RVOL 0.76); the breakout hard RVOL gate rejects everything. Consistent with the earlier "breakout RVOL-starved" datapoint.
- **consolidation_base: 0/8 passed → `consolidation_validator_too_strict_or_misconfigured` warning.** Mostly `range_tightness_too_low` (57 < 60). Consistent with the earlier "consolidation_base too strict" observation.

These are single-date, 50-alphabetical-ticker artifacts — informational only.

---

## Deliverables recap
1. **Sample logic** — first-N alphabetical (needs injected symbol source). ✅
2. **Recompute behavior** — controller always deletes `daily_features` for the date and recomputes; seed prices, not features. ✅
3. **Part C diagnostics** — `funnel_stage1_check.json` written; sections 1b / 5 / 6b render + non-empty. ✅
4. **Errors** — one pre-existing bug (§4), routed around without platform edits; needs its own scoped note.

**No commit.** `debug.duckdb` retains the 50-ticker smoke-test state (isolated, untracked). One follow-up for you: whether to open a scoped note to fix the `_ScopedStep3UniversalProxy` signature (§4) so `config_tuning_test` and other step3-scoping debug runs work.
