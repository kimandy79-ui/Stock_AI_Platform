# Part 1 + Part 2 — prod `ema150` migration and batched feature writes

**Date:** 2026-07-23
**Scope:** Parts 1 and 2 only. **Part 3 (152/151-date recompute + regime pass) NOT executed** — plan proposed below for authorization.
**Predecessor:** `reports/historical_window_feature_regime_population_2026-07-21.md` (Stage 1/2 recovery)
**Commit:** none. Part 1 is a prod DDL change (no code); Part 2 is an uncommitted code diff.

---

## Part 1 — prod schema migration

### 1.1 Backup taken first

```
data/duckdb/backups/prod_pre_ema150_20260723.duckdb   (357 MB, copied in 2.5 s)
```

Not requested by the note, but this is a DDL change against prod; the copy makes
it trivially reversible.

### 1.2 Exact statement executed

```sql
ALTER TABLE daily_features ADD COLUMN ema150 DOUBLE
```

Run once against `data/duckdb/prod.duckdb`. Result, verbatim:

```
before: n cols = 50 | ema150 present: False
executing: ALTER TABLE daily_features ADD COLUMN ema150 DOUBLE
after : n cols = 51 | ema150 present: True
added: ['ema150'] | removed: []
ema150 ordinal (1-based): 51
rows: 114816
ema150 non-null: 0
versions: [('features_v04', 114816)]
daily_features_current rows: 114816
```

Clean: exactly one column added, nothing removed, all 114,816 rows intact, all
`ema150` NULL (correct — no v05 row has been computed against prod), and
`daily_features_current` still returns the full 114,816 because no v05 row
exists yet.

### 1.3 Column set now matches the code DDL exactly

```
code DDL cols : 51
prod table cols: 51
MISSING from prod : none
EXTRA in prod     : none
SET MATCH: True
order identical: False  (ema150 appended at position 51; code DDL declares it at 9)
```

**The ordinal difference is cosmetic and safe.** Every read and write uses an
explicit column list, and the one `SELECT *` consumer
(`export_package_engine.py:588-606`) discovers column order dynamically from
`cursor.description` and emits header+rows together. Worth recording only
because a *freshly initialised* DB will have `ema150` at position 9 while
migrated prod has it at 51 — the two are set-equivalent, not order-equivalent.

### 1.4 Smoke test — on a throwaway copy, not prod

Per the note, the write path was confirmed against a fresh copy of the
*now-migrated* prod, so that "just confirming the column works" could not itself
trigger the v04/v05 blinding bug.

```
copy schema: 51 cols | ema150 present: True
calculate(2026-06-01, 2026-06-01, tickers=['AAPL'])
  status       : success
  rows written : 1
  feature_ready: 1
  errors       : []

  ticker feature_date feature_schema_version  feature_ready    ema20     ema50     ema150     ema200
0   AAPL   2026-06-01           features_v05           True  298.633   283.991    266.410    259.524

BinderException present: False
SMOKE TEST: PASS
```

`ema150` computes and persists. **prod itself still holds zero v05 rows** —
re-verified after the smoke test:

```
prod versions: [('features_v04', 114816, 29)]
prod daily_features_current rows: 114816
prod n cols: 51
```

---

## Part 2 — batching the write loop

### 2.1 Investigation — confirming the diagnosis

`_write()` was indeed one `execute()` per row: a `for row in feature_rows`
loop issuing `_UPSERT_FEATURE_ROW` individually, ~3,900–3,970 statements per
date. DuckDB re-parses and re-plans a 49-column `INSERT … ON CONFLICT` on every
call, which works out to ~60 ms per statement — that, not computation, was ~90%
of each date's runtime.

### 2.2 Candidate approaches, measured — not assumed

Benchmarked on the real `daily_features` DDL with a realistic 3,900-row batch,
covering both the insert path (empty table) and the update path (keys already
present), verifying stored values in each case:

| approach | insert | update | vs A | values identical to A |
|---|---|---|---|---|
| **A** per-row `execute()` loop (current) | 235.29 s | 53.04 s | 1.0x | — |
| **B** `executemany()` | 279.19 s | 64.83 s | **0.8x — slower** | yes |
| **C** `register(frame)` + `INSERT … SELECT … ON CONFLICT DO UPDATE` | **0.59 s** | **0.07 s** | **398x / 751x** | yes |

**`executemany` is a trap here.** DuckDB's Python `executemany` is a loop over
the same prepared statement with additional parameter marshalling — it does not
vectorise the insert, so it is *slower* than the plain loop. This is exactly the
case the note warned against defaulting to.

**Chosen: C.** It is the only one of the three that is actually set-based, it is
DuckDB's documented bulk pattern (a registered Arrow/Polars frame is scanned as
a table), and `ON CONFLICT … DO UPDATE` is fully supported on `INSERT … SELECT`,
so the upsert key and conflict clause carry over unchanged rather than being
re-expressed.

### 2.3 Implementation

`app/services/features/feature_engine.py`, +94/-4, no other file touched.

- `_build_batch_upsert_sql()` / `_UPSERT_FEATURE_BATCH` — set-based twin of the
  existing `_build_upsert_sql()`. Same target column list, **same conflict key
  `(ticker, feature_date, feature_schema_version)`, same `DO UPDATE SET` clause**,
  same `calculated_at = CAST(now() AS TIMESTAMP)` on both paths. The single
  difference is `SELECT … FROM _feature_batch` in place of `VALUES (?, …)`.
- `_build_batch_frame()` — builds the batch as a Polars frame whose dtypes are
  read from the **live table** via `DESCRIBE daily_features`, not assumed.
  Without pinned dtypes an all-NULL column in a given batch would infer as
  `pl.Null` and be rejected on insert.
- `_dedupe_feature_rows()` — last-wins collapse on the upsert key. This is a
  no-op for real input (`calculate()` emits one row per ticker/date), but the
  two mechanisms genuinely disagree on in-batch duplicates: the old loop applied
  them sequentially with last-write-wins, whereas set-based `ON CONFLICT DO
  UPDATE` raises *"can not update the same row twice"*. Last-wins preserves the
  previous behaviour rather than introducing a new failure mode.
- `_write()` — unchanged in shape: same `BEGIN TRANSACTION` / `COMMIT` /
  `ROLLBACK`-on-exception, same `_existing_keys()` call driving the
  `written`/`updated` counters. `register` is paired with `unregister` in a
  `finally` so the relation cannot leak onto a reused handle.

Nothing about *what* is computed changed — this is strictly a write-mechanism
change.

### 2.4 Test — byte-for-byte equality on real prod data

Full universe, one real date, on throwaway copies of migrated prod. Feature rows
were computed **once** and then written by both mechanisms, so the input is
identical by construction:

```
computing features for 2026-06-01 (full universe) ...
  compute took 34.9s | status=success | rows captured=3968

writing 3968 rows via OLD per-row loop ...
  OLD write: 255.48s
writing 3968 rows via NEW batched write ...
  NEW write: 3.61s  (written=3968, updated=0)

rows  old=3968  new=3968
row counts equal : True
VALUES IDENTICAL : True   (all 49 columns; calculated_at excluded — it is now() in both paths)

speedup: 71x  (255.48s -> 3.61s)
versions after new write: [('features_v04', 114816), ('features_v05', 3968)]
```

**All 49 columns × 3,968 rows identical.** `calculated_at` is the only excluded
column and is `now()` in both implementations by design.

The 71x here is lower than the 398x in §2.2 because this runs against a real
356 MB database with 114,816 existing rows — genuine index and I/O work rather
than a synthetic empty table. **71x is the honest write-path number.**

### 2.5 Test — transaction / rollback semantics preserved

A row mid-batch was poisoned with `feature_cutoff_date = None`, violating the
table's NOT NULL:

```
before: total=114816 versions=[('features_v04', 114816)] rows_on_2026-06-01=0
poisoned row index 1984 (ticker=KLAC): feature_cutoff_date -> None (violates NOT NULL)
exception raised : True
  ConstraintException: Constraint Error: NOT NULL constraint failed: daily_features.feature_cutoff_date
after : total=114816 versions=[('features_v04', 114816)] rows_on_2026-06-01=0
TABLE UNCHANGED  : True
NO PARTIAL WRITE : True
```

The exception propagates, the transaction rolls back whole, and the table is
byte-identical to its pre-write state. Atomicity guarantee holds.

### 2.6 Test — suites

```
tests/test_feature_engine.py, tests/test_feature_engine_v02.py      99 passed
tests/test_p2_3_vcp_sequencing.py, tests/test_p2_4_shares_market_cap.py,
tests/test_schema_manager.py, tests/test_step3_universal_eligibility.py   245 passed
```

All green, no new failures, no skips introduced.

### 2.7 End-to-end re-run of the exact Stage 2 probe

Same three dates, same shape of copy (migrated prod, the three dates empty), so
this is directly comparable to the Stage 2 baseline:

| sample | date | before | after | speedup | rows written | `feature_ready` |
|---|---|---|---|---|---|---|
| start-boundary | 2025-12-03 | 223.6 s | **24.7 s** | 9.1x | 3,897 | 3,696 |
| middle | 2026-03-04 | 293.3 s | **30.5 s** | 9.6x | 3,932 | 3,760 |
| near-join | 2026-06-01 | 329.1 s | **22.7 s** | 14.5x | 3,968 | 3,784 |

**Mean 282.0 s → 26.0 s per date = 10.9x end-to-end.**

Critically, `rows written` and `feature_ready` are **identical to Stage 2 on all
three dates** (3,897/3,932/3,968 and 3,696/3,760/3,784). The mechanism change
moved no numbers.

End-to-end speedup (10.9x) is lower than write-path speedup (71x) because the
~25 s of read+compute per date is now the dominant cost. **Further write
optimisation would be pointless; compute is the floor.**

---

## Part 3 — proposed plan, NOT executed

### 3.1 Corrected date count

The Stage 2 probe extrapolated with `N=152` for the full range. The actual count
is **151**:

```
full recompute range 2025-12-03..2026-07-13 : 151 sessions
new window only      2025-12-03..2026-06-01 : 123 sessions
remainder (already covered by v04)          :  28 sessions
```

This was an off-by-one in the estimate constant only — no data was affected.
All figures below use 151.

### 3.2 Revised runtime

| scope | N | before (per-row write) | **after (batched)** |
|---|---|---|---|
| full recompute 2025-12-03 → 2026-07-13 | 151 | ~11.8 h | **~65 min** (worst ~77 min) |

The maintenance window drops from an overnight job to roughly one hour.

### 3.3 Dropping v04 loses nothing — verified

```
existing v04 dates: 29
v04 dates INSIDE recompute range : 29
v04 dates OUTSIDE recompute range: none
=> dropping v04 after a full-range v05 recompute loses nothing: True
```

Every existing v04 date (including the orphan 2026-02-02) falls inside
2025-12-03 → 2026-07-13, so a full-range v05 recompute is a strict superset.

### 3.4 Regime pass is feasible across the whole window — verified

```
benchmark coverage in daily_prices:
   ('QQQ', 2024-12-02, 2026-07-13, 402 bars)
   ('SPY', 2024-12-02, 2026-07-13, 402 bars)
   ('^VIX', 2024-12-02, 2026-07-13, 402 bars)

bars available before 2025-12-03 (EMA200 warmup needs 200):
   QQQ 251 | SPY 251 | ^VIX 251
```

All three benchmarks clear the 200-bar EMA200 warmup at the boundary, so no date
in the window would be classified `neutral` merely for missing SPY EMA200.

**Ordering constraint:** `market_regime` is one of the columns the feature write
sets (to NULL), so the regime pass **must run after** the feature recompute for
a given date — running it first would have the recompute wipe it. This also
confirms the note's point that the 6 dates already carrying regime under v04
must be re-classified: their v05 rows are new rows with `market_regime` NULL.

### 3.5 Proposed sequence

1. **Back up prod** (as in §1.1) — the drop in step 5 is the irreversible step.
2. **Recompute 151 dates to `features_v05`** against real prod with the batched
   write. ~65 min. Checkpoint: 151 distinct v05 dates present; per-date row
   counts in the 3,890–3,970 band; `feature_ready` in the 3,690–3,830 band.
3. **Run `market_regime_classification` for all 151 dates**, including the 6 that
   had regime under v04. Checkpoint: `COUNT(market_regime)` equals row count on
   every one of the 151 dates — zero NULLs.
4. **Verify before dropping anything**: v05 covers all 151 dates and is a strict
   superset of the 29 v04 dates.
5. **Only then drop the stale v04 rows** —
   `DELETE FROM daily_features WHERE feature_schema_version = 'features_v04'`
   (114,816 rows). Checkpoint: `daily_features_current` returns 151 dates and
   exactly the v05 row count.

### 3.6 Conflict to flag: the view *cannot* stay consistent mid-transition

The note's checkpoint 4 asks that `daily_features_current` "never show fewer
dates than either version alone provided, mid-transition". **With the current
view definition that is not achievable, and I want that on the record rather
than planned around.**

`daily_features_current` resolves to a single `MAX(feature_schema_version)`. The
instant the first v05 row lands (step 2, date 1), the view stops returning all
29 v04 dates and returns only the v05 dates written so far — 1 date, then 2, and
so on. There is no ordering of the recompute that avoids this, because the view
can only ever expose one version at a time. Fixing that would mean changing the
view's version-comparison logic, which this note explicitly places out of scope.

Practical consequence: **for the ~65 minutes of step 2, `daily_features_current`
is partial, and Step 4, Step 5, the export engine and the dashboard must not be
run.** The recompute must be treated as an exclusive maintenance window. It is
recoverable (the v04 rows are still there and the backup exists), but it is not
concurrent-safe. Recommend confirming nothing is scheduled against prod for the
window before starting.

---

## 4. Anomalies

**4.1 `market_breadth_pct` is never written by the feature engine.** The table
has 51 columns; the engine writes 50 (`_FEATURE_PARAM_COLUMNS` 49 +
`calculated_at`). The omitted column is `market_breadth_pct` — it is in the DDL
and is listed in CLAUDE.md as a dormant field read by no validator, but it is
also never *populated*, so it is NULL for all 114,816 rows. Not touched here;
recording it because "dormant" implies landed-but-unread, and this one is
landed-but-unwritten.

**4.2 `N=152` in the Stage 2 extrapolation was off by one** — the real count is
151 (§3.1). Estimate-only; no data implication.

**4.3 `executemany` is slower than the per-row loop** (§2.2, 0.8x). Recorded so
it is not revisited as an optimisation later.

**4.4 Git reports a line-ending warning** on the modified file:
`warning: in the working copy of 'app/services/features/feature_engine.py', LF
will be replaced by CRLF the next time Git touches it`. Pre-existing repo-wide
CRLF behaviour on Windows, not introduced by this change.

---

## 5. State left behind

- `prod.duckdb` — **schema migrated (51 cols), zero v05 rows, `daily_features_current`
  still returns all 114,816 v04 rows.** No feature data written.
- Backup: `data/duckdb/backups/prod_pre_ema150_20260723.duckdb`.
- Code: `app/services/features/feature_engine.py` modified (+94/-4), **uncommitted**.
- Scratch DB copies deleted; probe scripts retained under the session scratchpad.
- Part 3 not started. No regime run against prod. No v04 row dropped.

## 6. Awaiting authorization

Part 3 per §3.5, with the §3.6 maintenance-window caveat acknowledged.
