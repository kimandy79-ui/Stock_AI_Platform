# Per-Date Feature Backfill (Option B) — Parts A / B / C

**To:** Architect
**From:** Coder
**Date:** 2026-07-14
**Re:** CODER NOTE — Per-Date Feature Backfill (Compressed Window) + Parts B/C
**Status:** Part A ✅ done · Part B ✅ done · **Date selection ⚠️ criteria don't fit — reporting distribution per your escape clause** · Part C ⏸️ mechanism confirmed; empirical run blocked on a debug-seeding prerequisite (needs your call).
**Prod writes:** per-date `daily_features` for all 27 sessions in 2026-06-02 → 2026-07-10. No schema/gate/scoring/config change. No commit. No network re-fetch (used the M11 engine directly, not the price-repair path).

---

## Part A — per-date backfill complete (2026-06-02 → 2026-07-10)

Thin loop over the existing `FeatureEngine.calculate(d, d, db_role="prod")`, one NYSE session at a time (`trading_days_between`, NYSE). 07-01 and 07-10 were left as-is (already universe-wide from prior smoke tests). **All 27 sessions now universe-wide; `feature_ready` stable 3,803–3,824; no anomalous dates.**

> **Ops note:** the backfill loop kept getting killed by a background-task wall-clock limit (~1–4 dates per run). Completed dates commit per-date, so it was resumable; the **PowerShell tool runs synchronously and was not killed** — that's how the tail dates landed. No data impact, but flagging for future long prod loops on this machine (consistent with the "long tasks get killed" env note).

Per-date result (compact):

```
feature_date  rows  ready      feature_date  rows  ready
2026-06-02   3968  3805        2026-06-23*  3966  3821
2026-06-03   3968  3813        2026-06-24   3966  3821
2026-06-04   3970  3813        2026-06-25   3966  3822
2026-06-05   3970  3816        2026-06-26*  3966  3823
2026-06-08   3969  3818        2026-06-29   3966  3824
2026-06-09   3966  3816        2026-06-30*  3950  3808
2026-06-10   3967  3816        2026-07-01   3947  3805
2026-06-11   3967  3817        2026-07-02   3948  3807
2026-06-12   3968  3819        2026-07-06   3945  3804
2026-06-15   3967  3818        2026-07-07   3945  3804
2026-06-16   3966  3817        2026-07-08   3945  3804
2026-06-17   3966  3817        2026-07-09   3945  3803
2026-06-18   3967  3819        2026-07-10   3957  3815
2026-06-22   3967  3819
                              (* = already-run / excluded date)
```
No date has an anomalously low `ready_count` relative to neighbours — the ~15-ticker spread is normal (the 88 tickers with pre-existing price gaps drift in/out per session).

---

## Part B — median-RVOL ranking (raw, unfiltered, with `ready_count`)

```
as_of_date   median_rvol  ticker_count  ready_count
2026-06-02      0.8762        3968         3805
2026-06-03      0.8857        3968         3813
2026-06-04      0.8158        3970         3813
2026-06-05      0.8559        3970         3816
2026-06-08      0.8071        3969         3818
2026-06-09      0.9289        3966         3816
2026-06-10      0.8691        3967         3816
2026-06-11      0.8726        3967         3817
2026-06-12      0.7958        3968         3819
2026-06-15      0.9223        3967         3818
2026-06-16      0.8904        3966         3817
2026-06-17      0.9750        3966         3817
2026-06-18      1.5585        3967         3819   <- only >1.2
2026-06-22      0.9357        3967         3819
2026-06-23      0.8690        3966         3821   (already-run)
2026-06-24      0.9380        3966         3821
2026-06-25      0.8529        3966         3822
2026-06-26      1.8269        3966         3823   (already-run) <- only other >1.2
2026-06-29      0.8987        3966         3824
2026-06-30      0.8450        3950         3808   (already-run)
2026-07-01      0.8342        3947         3805
2026-07-02      0.7714        3948         3807
2026-07-06      0.7242        3945         3804
2026-07-07      0.7518        3945         3804
2026-07-08      0.7607        3945         3804
2026-07-09      0.6606        3945         3803   (last-2, avoid)
2026-07-10      0.5880        3957         3815   (already-run / last day)
```
Every date is genuinely usable for screening (`ready_count` ≈ full universe).

---

## Date selection — criteria do NOT cleanly fit (not forcing a set)

Applying: exclude already-run (06-23/06-26/06-30/07-10) and edge dates (06-02/03, 07-09/10). Among the remaining **usable** dates:

| Bucket | Threshold | Qualifying usable dates | Verdict |
|---|---|---|---|
| **Low-vol** | median RVOL < 0.7 | **none** (lowest usable = 07-06 @ 0.724) | ✗ cannot form a low pair |
| **High-vol** | median RVOL > 1.2 | **06-18 only** (1.559); the only other spike 06-26 @ 1.83 is already-run | ✗ cannot form a non-adjacent high pair |
| **Medium** | 0.8 – 1.1 | abundant (most dates) | ✓ |

**Root cause:** the compressed one-month window has **low volume dispersion** — 24 of 27 sessions sit in a narrow 0.72–0.98 median-RVOL band. Only two genuine high-volume spikes exist (06-18, 06-26) and no sub-0.7 low day except the final two sessions (07-09/10, both at the edge). This is the volume-variance limitation predicted when we compressed to Jun–Jul (Option B).

### Options for you to pick thresholds (I will not force a fit)
- **High-vol pair:** the only clean pair is **06-18 (1.56) + 06-26 (1.83)** — 6 trading days apart, non-adjacent weeks. Requires **un-excluding 06-26** (re-running diagnostics on an already-run date is harmless). Without 06-26, there is no second high-vol day.
- **Low-vol pair (relaxed to < 0.78):** candidates cluster in early July — 07-06 (0.724), 07-07 (0.752), 07-08 (0.761), plus 07-02 (0.771). Non-adjacent pair e.g. **07-02 + 07-07**. (These are "lowest available," not truly low.)
- **Medium:** e.g. **06-15 (0.922)** or **06-09 (0.929)**.
- A possible 5-date set *if* you accept the two relaxations above: `06-18, 06-26, 07-02, 07-07, 06-15`. Flagging it as a candidate only — your call on thresholds/exclusions.

---

## Part C — mechanism confirmed; empirical smoke test blocked on a prerequisite

**Feature-sourcing (the open question), traced in code — not assumed:**
`run_debug_pipeline.py` → M22 `DebugModeController` → `PipelineOrchestrator.run()` against **`debug.duckdb` only**. It **recomputes features independently** (its own universe → price → `FeatureEngine.calculate` steps) and **does not read prod `daily_features`**.

**The catch:** the orchestrator's price step ingests **only `run_date`'s single bar** (`pipeline_orchestrator.py:1279` → `ingest(start=run_date, end=run_date)`), the incremental daily model. The presets' `trading_days` value (5/30/90/126) is **vestigial — not wired to ingestion**. So a debug run yields `feature_ready` features **only if `debug.duckdb` already contains ~252 bars of history** up to `run_date` for the sampled tickers.

**Consequence:** `debug.duckdb` does not currently exist. A fresh one (or one only `init`-ed with `_reduced_50`) → 1 bar per ticker → `feature_ready = False` → step3 finds nothing eligible → diagnostics sections **1b / 5 / 6b render empty**. Running the two commands as-is would just confirm that empty outcome.

**To get a real Stage-1 smoke test, `debug.duckdb` must be seeded with lookback history first.** Options (need your pick):
1. **Copy `prod.duckdb` → `debug.duckdb`** (prod now has the backfilled June features + 252-bar price history), then run the debug pipeline for a date ≥ 2026-06-02. Cleanest real data; ~180 MB copy; the debug run still network-fetches benchmarks + sampled-ticker bars.
2. **Targeted debug backfill** — ingest ~252 bars for the ~50 sampled tickers into `debug.duckdb` (network, small), then run.
3. **Accept the empty-diagnostics outcome** as the documented Part-C result (confirms the prerequisite, no seeding).

Also note a tooling mismatch: `run_debug_pipeline.py` selects tickers by **`--sample-count`** (preset-driven), not a `_reduced_50` universe name — there is no `--universe` arg on that runner.

---

## Deliverables recap
1. **Part A** — ✅ all 27 sessions backfilled per-date; table above; no anomalies.
2. **Part B** — ✅ raw ranking with `ready_count` above.
3. **5 selected dates** — ⚠️ **not selected**: criteria don't fit (no low-vol pair; only one high-vol day after exclusions). Distribution + relaxation options above, per your "report, don't force" instruction.
4. **Part C** — mechanism confirmed (recompute-independently; `run_date`-only ingestion; needs pre-seeded ≥252-bar history). Empirical run pending your seeding choice.
5. **Errors** — none in the feature computation. The only operational issue was the background-task killer (worked around via synchronous PowerShell; no data impact).

**Two decisions needed:** (a) volume thresholds / whether to un-exclude 06-26 for the high-vol pair; (b) how to seed `debug.duckdb` for Part C (options 1–3).
