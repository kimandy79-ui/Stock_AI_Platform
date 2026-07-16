# Campaign Date Selection + Stage 1 Wiring — Coder Report

**To:** Architect
**From:** Coder (read-only investigation)
**Date:** 2026-07-14
**Re:** CODER NOTE — Campaign Date Selection + Stage 1 Wiring Validation
**Scope executed:** Read-only queries against `prod` only. No replay run. No schema/gate/scoring/config changes. No writes to any DB.
**Status:** ⛔ **BLOCKED** — stated prerequisite is only half-true. Neither Part 1 (usable ranking) nor Part 2 (replay/diagnostics) can proceed. Details below.

---

## TL;DR

1. **Prerequisite is half-true.** Raw prices are fully backfilled (1.1M rows, Jun 2025 → Jul 2026). **Features are not.** `daily_features` is universe-wide on **one date only (2026-07-13)**; every candidate-window date has 1–14 tickers.
2. **The Part 1 query has two wrong column names** — `rvol` and `as_of_date` do not exist in `daily_features`. Correct source is **`daily_features.rvol20`** keyed on **`feature_date`**.
3. **Even corrected, the Part 1 ranking is meaningless** — medians are computed over 1–14 tickers/date, not the ~3,900-ticker universe. Not usable for date selection.
4. **Part 2 not run** — blocked on the same feature gap, *and* the note's "replay via M17" targets the wrong tables: the funnel diagnostics reader consumes the **step** tables, which M17 does not write.
5. **Unblock path:** compute features across the universe for 2026-02-01 → 2026-07-10 (M11 / feature rebuild) before any RVOL ranking or replay. That is a prod-writing operation outside this note's scope — architect's call.

---

## 1. Prerequisite check: raw prices ✅, features ❌

The note states: *"prod DB is populated June 2025 → July 2026. No backfill required."* This is true for **raw prices** but **not** for the feature layer the RVOL ranking depends on.

| Table | Coverage (as of 2026-07-14) | Verdict |
|---|---|---|
| `daily_prices` (raw OHLCV) | 2025-06-02 → 2026-07-13 · **1,108,066 rows · 3,974 tickers** | ✅ fully populated |
| `daily_features` (RVOL source) | **11 distinct dates only**; universe-wide on **2026-07-13 alone** | ❌ not populated over history |
| `daily_features_current` | identical to `daily_features` (same 11 sparse dates) | ❌ not populated over history |

### `daily_features` per-date breadth (full table)

```
feature_date   n_tickers   feature_ready
2026-06-05          1            0
2026-06-08          3            3
2026-06-12          2            2
2026-06-15          1            1
2026-06-22          1            1
2026-06-29          1            1
2026-06-30          4            4
2026-07-01          1            1
2026-07-02          3            3
2026-07-10         14           14
2026-07-13       3943         3804     <-- only universe-wide date
```

**Root cause:** the "Prod Rebuild 2026-07-14" backfilled *raw prices* only; the pipeline / M11 feature engine has not been run across history ("no proposals generated yet"). Features exist for 2026-07-13 because the reconciliation run touched that date. Historical feature rows must be generated before any per-date volume-regime metric is computable.

---

## 2. Part 1 — median-RVOL ranking

### 2a. Schema corrections (confirmed against `01b_SCHEMA_AND_DATA.md` and on-disk PRAGMA)

The note's query references two columns that do **not** exist in `daily_features`:

| Note's column | Actual column in `daily_features` | Notes |
|---|---|---|
| `rvol` | **`rvol20`** | `daily_features` has no `rvol`. A bare `rvol` exists only in `step4_analysis` (per-validated-candidate, wrong population for a market-wide ranking). |
| `as_of_date` | **`feature_date`** | `daily_features` has no `as_of_date`. `as_of_date` exists only in `fundamentals_snapshot`. |

**Confirmed RVOL column path for date ranking:** `daily_features.rvol20`, grouped by `feature_date`.

### 2b. Corrected query

```sql
SELECT
    feature_date AS as_of_date,
    MEDIAN(rvol20) AS median_rvol,
    COUNT(*)       AS ticker_count
FROM daily_features
WHERE feature_date BETWEEN '2026-02-01' AND '2026-07-10'
GROUP BY feature_date
ORDER BY feature_date;
```

### 2c. Raw, unfiltered result

```
as_of_date    median_rvol   ticker_count
2026-06-05        0.1153            1
2026-06-08        0.0000            3
2026-06-12       26.1957            2
2026-06-15        8.6161            1
2026-06-22        0.0000            1
2026-06-29        0.0000            1
2026-06-30        7.1459            4
2026-07-01        1.0099            1
2026-07-02        0.0000            3
2026-07-10        0.3812           14
```

### 2d. Why this is NOT usable for date selection

- Every `median_rvol` is computed over **1–14 tickers**, not the ~3,900-ticker universe.
- The values (0.0000, 26.19, 8.61, …) are noise from a handful of stray feature rows, not a market volume regime.
- **No candidate-window date has universe-wide features.** The only universe-wide date (2026-07-13) is outside the window and already covered by prior diagnostics.
- Applying any selection criteria (low/high/medium vol split) to this table would produce a spurious campaign schedule.

---

## 3. Part 2 — Stage 1 wiring validation: NOT RUN

Two independent reasons; either is sufficient to block:

### 3a. Same feature gap
A replay for any candidate-window date requires universe features for that date. None exist. The one date with full features (2026-07-13) is not in the Part 1 set and is already diagnosed.

### 3b. Entry-point discrepancy (the item the note flagged as unconfirmed)
- `tools/run_funnel_diagnostics.py` reads **`step3_candidates` / `step4_analysis` / `step5_proposals`** (the prod/debug **step** tables).
- **M17** `SimulationEngine._replay_date` writes to **`sim_step4_analysis` / `sim_step5_proposals`** — which the diagnostics reader does **not** read.
- There is **no `--replay-date` CLI** anywhere in `tools/`.
- **Correct populate-then-diagnose path** for debug-role step tables:
  ```
  python tools/run_debug_pipeline.py --date <date> [--setups breakout pullback ...]
  python tools/run_funnel_diagnostics.py --date <date> --db-role debug --json-out funnel_stage1_check.json
  ```
  `run_debug_pipeline.py` drives the M22 `DebugModeController` (hard-wired to `debug`, cannot touch prod) and writes the step tables the diagnostics reader consumes.

**Actual M17 invocation used:** none. M17 is the wrong entry point for this diagnostics reader.

---

## 4. Deliverables recap (against the note)

| # | Requested | Delivered |
|---|---|---|
| 1 | Full median-RVOL ranking (raw, unfiltered) | §2c — provided, but statistically meaningless (1–14 tickers/date) |
| 2 | RVOL column source if different | §2a — **`daily_features.rvol20`** on **`feature_date`** (not `rvol` / `as_of_date`) |
| 3 | Stage 1 replay + diagnostics output | **Not produced** — blocked (§3) |
| 4 | Exceptions/errors verbatim | None at runtime; blockers are data-coverage/schema (§1, §2a) |
| 5 | Actual M17 replay invocation | **None** — M17 ≠ diagnostics reader (§3b) |

---

## 5. Recommended next step (architect decision — not started)

Before any RVOL date-ranking or Stage 1 replay is possible:

1. **Compute features across the universe** for the candidate window (2026-02-01 → 2026-07-10) — via the pipeline / M11 feature engine or a `feature_rebuild` over those dates. This is a **prod-writing** operation, outside this read-only/smoke-test note's scope.
2. Once features exist window-wide:
   - The corrected §2b query returns a meaningful ~3,900-ticker-per-date ranking.
   - Part 2 can run via the **debug-pipeline path** in §3b (not M17).

I can draft a scoped note for the feature-backfill step on request. No commit was made; nothing was written to any DB.
