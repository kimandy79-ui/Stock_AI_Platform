# Feature Backfill (Part A) — Findings & Stop-for-Decision

**To:** Architect
**From:** Coder
**Date:** 2026-07-14
**Re:** CODER NOTE — Feature Backfill (Candidate Window) + Corrected Date Selection + Stage 1
**Status:** ⏸️ **STOPPED after Part A smoke tests** — two unexpected findings that change the campaign's date-selection premise. Per the note ("no further sign-off needed *unless Part A surfaces something unexpected*"), reporting before scaling up. Parts B and C deferred pending your decision.
**Prod writes made:** feature rows for **3 dates only** (2026-02-02, 2026-07-01, 2026-07-10) — legitimate as-of snapshots, harmless. No schema/gate/scoring/config change. No commit. No network re-fetch.

---

## TL;DR

1. ✅ **Wiring works.** M11 `FeatureEngine.calculate()` writes universe-wide features for a modern date (2026-07-01 → 3,947 rows, **3,805 feature_ready**).
2. ⛔ **252-bar `feature_ready` cliff.** Prices start 2025-06-02; `feature_ready` needs a 252-bar (52-week-high) lookback. **First possible `feature_ready` date = 2026-06-02.** Everything in the candidate window before that is `feature_ready = False`. Empirically: 2026-02-02 → **0 of 3,915** ready.
3. ⚠️ **`calculate(start,end)` is not a per-date backfill.** It writes a single as-of-`end_date` snapshot (one row/ticker at the cutoff), not one row per trading day. No existing batch path backfills per-date history across a range.
4. **Net effect:** only **2026-06-02 → 2026-07-10** (~28 trading days, all one month) is usable for the campaign — the "2 low-vol + 2 high-vol + 1 medium, spread across months" selection is **infeasible on current price history**.
5. **Decision needed** (§6): extend price history back to ~2024-07, **or** accept the compressed Jun–Jul usable window. Either way Part A must be re-run as a **per-date loop**, not a single range call.

---

## 1. Smoke tests (Part A step "run one date first")

Invoked the M11 engine directly (`FeatureEngine.calculate(d, d, db_role="prod")`) — see §5 for why not `backfill_prod_history.py`.

| feature_date | rows written | feature_ready | Interpretation |
|---|---:|---:|---|
| **2026-07-01** (late) | 3,947 | **3,805** | ✅ wiring good; meets ~3,900-ready acceptance criterion |
| **2026-02-02** (early) | 3,915 | **0** | ⛔ cliff — universe computes but nothing is `feature_ready` |

`rvol20` is populated on **both** dates (it needs only 20 bars), so the Part B RVOL ranking would be computable window-wide — but the dates themselves are unusable for screening when `feature_ready = False`.

---

## 2. Finding — 252-bar `feature_ready` cliff (blocks Feb–May)

`feature_ready` requires **all** of `REQUIRED_FEATURE_COLUMNS` to be non-null (`feature_engine.py:1410`). Two of them have long lookbacks:

| Required column | Source | Min bars | Code |
|---|---|---:|---|
| `ema200` | EWM span 200 | 200 | `bar_index >= _MIN_BARS_EMA200` (`:1644`) |
| `distance_from_52w_high_pct` | `_high252` = `rolling_max(252, min_samples=252)` | **252** | `:1616`, `:1670` |

`daily_prices` starts **2025-06-02** for every ticker (min date confirmed; no legacy history earlier). Mapping bar counts to calendar dates off the actual trading calendar:

| Milestone | Trading-date index | Date | Effect |
|---|---:|---|---|
| EMA200 clears | 200th | **2026-03-18** | `ema200` non-null from here |
| 52-week high clears | 252nd | **2026-06-02** | `distance_from_52w_high_pct` non-null → `feature_ready` **possible** from here |

**Consequence for the candidate window (2026-02-01 → 2026-07-10):**

```
2026-02-01 ┬──────────────── feature_ready = FALSE ────────────────┬ 2026-06-01
           │  (features compute, but 52w-high lookback incomplete) │
2026-06-02 ┼──────────────── feature_ready = TRUE ─────────────────┼ 2026-07-10
           │              usable campaign sub-range                │
```

The architect note's premise — *"~8 months of trailing history … for 200MA/EMA/ATR lookback validity"* — is short for the **52-week high**, which needs ~12 months (252 bars). 8 months ≈ 168 bars.

---

## 3. Finding — `calculate(start,end)` writes only `end_date`, not per-date history

`FeatureEngine._build_feature_rows` docstring (`:1154`): *"Return one feature-row dict per processed ticker **at its cutoff**"*; the cutoff is `MAX(date)` within `[start,end]` per ticker (`:1161–1168`). So:

- `calculate(2026-02-01, 2026-07-10)` wrote features for **2026-07-10 only** (verified in DB: the range call added exactly one universe-wide date).
- `backfill_prod_history.py`'s feature step calls `calculate(start,end)` the same single way (`:1024`) → it too only produces `end_date` features.
- **There is no existing batch entry point that backfills per-date features across a range.** The daily pipeline computes features one `signal_date` at a time (`start == end`).

**To populate the window for per-date median-RVOL selection, the engine must be called once per trading day** (`calculate(d, d)` for each `d`). ~28 calls for the usable sub-range; ~110 for the full window.

### Current `daily_features` state in-window (after my 3 writes)

```
feature_date   rows   feature_ready
2026-02-02     3915        0     <- my early smoke test
2026-07-01     3947     3805     <- my late smoke test
2026-07-10     3957     3815     <- end_date of the range call
(plus 8 pre-existing stray dates with 1–4 rows each)
```

---

## 4. Side finding — price completeness is not 100%

Part A assumed *"existing daily_prices (already fully backfilled — no re-fetch needed)."* The completeness planner (dry-run) reports, for the window:

```
active stock tickers = 3911
complete = 3823   need_repair = 88   missing_dates = 334   bad_rows = 2708
```

88 tickers have gaps/bad rows in-window (delistings, mid-window IPOs, or data-quality flags). This is a **pre-existing** price issue, separate from feature computation. I did **not** repair it (would require network re-fetch, out of Part A scope).

---

## 5. Why the M11 engine directly, not `backfill_prod_history.py`

`backfill_prod_history.py` (the documented batch tool) bundles a **price-repair step** ahead of features. With `--resume` it would have fired a **network re-fetch for the 88 incomplete tickers** — contradicting Part A's explicit "feature computation only … no re-fetch needed," and risking the full-universe network-loop kills noted in prior sessions. `calculate()` is the same public M11 method that tool calls internally (`:1024`), so invoking it directly is the standard engine call with **zero network** — faithful to Part A's scope. (It also revealed Finding §3, which the tool's single range call would have hidden.)

---

## 6. Decision needed before Parts B/C

The campaign wants mixed-volume dates spread across months. On current data only ~28 usable dates exist, all in one ~5.5-week span. Options:

| | Option A — extend price history | Option B — accept compressed window |
|---|---|---|
| **Action** | Backfill `daily_prices` back to ~**2024-07** (network, full universe), then per-date feature backfill over 2026-02-01 → 2026-07-10 | Per-date feature backfill over **2026-06-02 → 2026-07-10** only |
| **Enables** | Full Feb–Jul mixed-month selection | Jun–Jul selection only (relaxes "spread across months") |
| **Cost** | Large price backfill (hours, network-heavy) + ~110 per-date feature calls | ~28 per-date feature calls, no network |
| **Risk** | Network-loop kills (per prior sessions) | Limited volume-regime variance across so few dates |

**Also confirm the mechanism:** Part A as written ("single range call") cannot do a per-date backfill (§3). Should I add a thin per-date loop over the existing `FeatureEngine.calculate(d, d)` (no new pipeline logic — just iterate the existing engine over trading days), or do you have a preferred entry point?

### Parts B & C — deferred
- **Part B** (per-date RVOL ranking) needs the per-date backfill first; a single-date snapshot can't be ranked.
- **Part C** (Stage 1 smoke test) must pick a date **≥ 2026-06-02** to have `feature_ready` data; the debug pipeline also recomputes features independently in `debug.duckdb` for `_reduced_50` — I'll confirm how it sources them when Part C proceeds.

**Recommendation:** if the diagnostics campaign can tolerate a Jun–Jul-only date set, **Option B** is fast and network-free; I can run the ~28-date per-date backfill and deliver Parts B & C on your go-ahead. If mixed-month spread is essential, **Option A** first.
