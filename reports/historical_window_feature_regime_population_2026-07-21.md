# Historical Window Feature/Regime Population — Stage 1 & 2

**Note date:** 2026-07-21 (original) / recovery run 2026-07-23
**Scope:** Crash recovery + Stage 1 and Stage 2 only. Stage 3+ NOT run.
**Commit:** none (investigation + measurement only)

> **Follow-up:** the two blockers found here (§4.1 `ema150`, §4.2 `features_v05`)
> were addressed on 2026-07-23 — see
> `reports/schema_migration_and_batched_writes_2026-07-23.md`. Prod is now
> migrated and the write loop is batched, cutting the Stage 3 estimate from
> ~11.8 h to ~65 min. Note that report also corrects the date count in §3.1
> below from 152 to **151**.

---

## 1. Crash recovery findings

### 1.1 What was actually running

The crashed work was **not** on 2026-07-21. Reconstructed from the Claude session
transcripts in `~/.claude/projects/D--Python-Stock-AI-Platform/`:

| Session | Ended | What it was |
|---|---|---|
| `5b9e06f9…` | 2026-07-23 22:03 | First attempt at the status-check note |
| `39948c0d…` | 2026-07-23 22:14 | Second attempt — **this is the one that died** |

Both ran the *same* note (`CODER NOTE — Status Check: Historical Window
Feature/Regime Population`). The original staged note file
`CODER_NOTE_2026-07-21_historical_window_population.md` **does not exist on disk**
anywhere in the repo — it was only ever pasted into the chat.

The crash point is exact: session `39948c0d…`'s final tool call was
`stage2_probe.py`, and it has **no tool result** — the process was killed
mid-run at ~22:14.

### 1.2 Lock file status

**No lock cleanup was needed. Nothing was deleted.**

- `data/locks/` — exists but is **empty**. No `prod_backfill_history.lock`, no
  stale lock of any kind.
- `pipeline_locks` table (prod) — the real lock mechanism for this project:
  ```
  ('daily_pipeline', locked=False, holder=None,
   acquired 2026-07-16 20:46:21, released 2026-07-16 20:46:41)
  ```
  Not held, cleanly released 7 days before the crash.
- Running processes — only `pycharm64` (PID 8316, started 2026-07-23 10:18, i.e.
  the *restarted* IDE). **No orphaned Python process** from the crashed run.

### 1.3 Was prod written to before the crash? — No.

`prod.duckdb` mtime is **2026-07-18 20:35**, five days before the crash. No
`.wal` and no `.tmp` files → clean shutdown, no torn transaction.

Confirmed at row level — newest `daily_features.calculated_at` is
**2026-07-16 20:46**, and `daily_features` still holds exactly the same 29 dates
as before (2026-02-02, then 2026-06-02 … 2026-07-13). Nothing from 07-21/22/23.

**Reason prod is untouched is important:** the pre-crash run did attempt three
`FeatureEngine.calculate()` calls against prod, and **all three failed at the
write step and rolled back** (see §1.4). The crash itself happened later, during
a follow-up probe that ran against a *copy*, never against prod.

### 1.4 The real blocker found pre-crash — `ema150` schema drift

All three Stage 2 sample dates failed identically, verbatim:

```
calculate failed during write (rolled back): BinderException: Binder Error:
Table "daily_features" does not have a column with name "ema150"

Did you mean: "ema50", "ema50_slope", "ema20", "ema200", "ema20_slope"
```

Column diff (prod table vs. the DDL in code):

```
prod table cols : 50
code DDL   cols : 51

In code DDL but MISSING from prod table:
   - ema150
In prod table but not in code DDL:
   (none)
```

`ema150` was added to the code DDL on 2026-07-20 (commit `536a833`), but the
existing `prod.duckdb` was never migrated — `schema_manager` creates the column
for a *fresh* DB and there is no ALTER-based migration for an existing one.

**Consequence: `FeatureEngine.calculate()` cannot write to prod at all right
now** — not for the historical window, and not for a normal daily run either.
This blocks Stage 3 regardless of cost.

Per the note's instruction ("report before fixing, don't fix silently"), **prod
was not altered.** The measurement below was taken on a throwaway copy.

### 1.5 Partial-write check — clean, nothing to delete

The leftover copy `probe.duckdb` (356 MB, created 22:12) was verified intact:
51 columns with `ema150` already added, and **zero rows on all three sample
dates**. The crash landed *after* the `ALTER TABLE` but *before* any date
completed → clean-none state, no partial rows anywhere.

### 1.6 `FeatureEngine` write semantics — verified, not assumed

`feature_engine.py:1485` `_write()` is an **upsert inside a single
transaction**:

```sql
INSERT INTO daily_features (…, calculated_at) VALUES (…)
ON CONFLICT (ticker, feature_date, feature_schema_version)
DO UPDATE SET …, calculated_at = CAST(now() AS TIMESTAMP)
```

wrapped in `BEGIN TRANSACTION` … `COMMIT`, with `ROLLBACK` on any exception.

Two consequences:
1. **Re-running any date is safe and idempotent** — no need to delete rows
   first, ever. A crashed date leaves all-or-nothing.
2. Rows are upserted **one at a time in a Python loop** (~3,900 individual
   `execute()` calls per date). This is the dominant per-date cost — see §3.

---

## 2. Stage 1 — exact `feature_ready` boundary

Independently recomputed via the project trading calendar (not estimated):

```
REQUIRED_MIN_BARS = 252
anchor 2024-12-02  is_trading_day = True

[A] 252nd session INCLUSIVE of anchor = 2025-12-03   (sessions in [anchor,x] = 252)
[B] 252 sessions AFTER anchor         = 2025-12-04   (sessions in [anchor,x] = 253)
```

**Boundary date = `2025-12-03`.** Semantics A is correct: the anchor bar itself
counts toward the 252-bar lookback, so 2025-12-03 is the first date with a full
252 sessions of history available.

**Candidate new window: `2025-12-03` → `2026-06-01` = 123 trading sessions**
(the day before the already-covered 2026-06-02 range begins).

Price coverage backing this (prod `daily_prices`):

```
min date   2024-12-02      max date  2026-07-13
rows       1,596,868       tickers   3,974        distinct dates  402
```

All 3,974 tickers start on 2024-12-02 (uniform), and coverage is contiguous —
402/402 NYSE sessions present, zero gaps.

---

## 3. Stage 2 — real cost probe

Resumed 2026-07-23 22:30, completed 22:56. Run against the **copy**
(`probe.duckdb`) with `ema150` added to the copy only — prod untouched. All
three dates were empty beforehand, so every row is an insert (`updated = 0`),
which is what a real Stage 3 run against these dates would also do.

| sample | date | wall clock | rows written | `feature_ready` | not ready | rows read | status |
|---|---|---|---|---|---|---|---|
| start-boundary | 2025-12-03 | **223.6 s** | 3,897 | **3,696** (94.8%) | 201 | 959,647 | success |
| middle | 2026-03-04 | **293.3 s** | 3,932 | **3,760** (95.6%) | 172 | 1,105,025 | success |
| near-join | 2026-06-01 | **329.1 s** | 3,968 | **3,784** (95.4%) | 184 | 1,120,578 | success |

Mean **282.0 s/date** (4.7 min), worst **329.1 s/date** (5.5 min).

### 3.1 Extrapolated runtime

| scope | N dates | at mean 282 s | at worst 329 s |
|---|---|---|---|
| new window only (2025-12-03 → 2026-06-01) | 123 | **578 min ≈ 9.6 h** | 675 min ≈ 11.2 h |
| full recompute (2025-12-03 → 2026-07-13) | 152 | **714 min ≈ 11.9 h** | 834 min ≈ 13.9 h |

**The 152-date row is the one that matters** — see §4.2, the schema-version
finding forces a full recompute rather than a 123-date top-up.

Cost is dominated by the row-by-row upsert (§1.6), not by compute: the
pre-crash runs that failed *at the write step* had already finished reading and
computing in 16–20 s. So ~93% of each date's 282 s is the ~3,900-statement
write loop. **If Stage 3 runtime needs to come down, batching that write is the
single lever** — it would plausibly cut ~10 h to well under 1 h. That is a code
change and is out of scope here; flagging it as an option, not doing it.

### 3.2 `feature_ready` counts — mildly below the June/July band, and explainable

The note asked to flag counts "noticeably below the ~3,800–3,900 range". Result:
**3,696 / 3,760 / 3,784** vs 3,803–3,824 on the existing June/July dates.

This is slightly below, but it is a smooth monotonic ramp, not a cliff:

```
2025-12-03  3696   (252 bars available — exactly the minimum)
2026-03-04  3760
2026-06-01  3784
2026-06-02  3805   ← existing data picks up seamlessly
```

The gradient is the expected consequence of lookback depth: at the boundary
every ticker has exactly `REQUIRED_MIN_BARS = 252` bars and nothing to spare, so
marginally more tickers fail a per-indicator sufficiency check. It converges on
the existing range as history deepens. **No anomaly — the window is usable
end-to-end**, with the caveat that the earliest ~3% of dates carry a slightly
thinner ready-set.

`ema150` non-null counts were 3,775 / 3,843 / 3,877 — i.e. the new column
computes correctly across the window.

---

## 4. Anomalies

### 4.1 `ema150` missing from prod — blocks all feature writes
See §1.4. **Blocks Stage 3, and blocks normal daily runs too.** Needs an explicit
decision on how to migrate the existing prod DB. Do not fix this in isolation —
adding the column alone un-blocks the write path and immediately exposes §4.2.

### 4.2 `features_v05` silently hides all existing history — the dangerous one

`constants.FEATURE_SCHEMA_VERSION` is now **`features_v05`**, bumped in the same
commit `536a833` (2026-07-20) that added `ema150`. Every row currently in prod is
**`features_v04`** (114,816 rows / 29 dates, one single version).

The upsert key is `(ticker, feature_date, feature_schema_version)`, so v05 rows
do **not** replace v04 rows — the two versions coexist. And the view every
downstream consumer reads is:

```sql
CREATE VIEW daily_features_current AS
SELECT * FROM daily_features
WHERE feature_schema_version = (SELECT max(feature_schema_version) FROM daily_features);
```

`max()` on a VARCHAR is a lexicographic string compare, and
`'features_v05' > 'features_v04'`. **The moment a single v05 row is written, the
view stops returning every v04 row.**

This is demonstrated, not theoretical — measured on the probe copy after the
three sample dates landed:

```
before:  daily_features_current = 114,816 rows / 29 dates
after :  daily_features_current =  11,797 rows /  3 dates   ← all 29 existing dates gone
```

Consumers of that view: `step4_setup_validation_engine.py:153`,
`step5_proposal_engine.py:997`, `step4_analysis_engine.py:132,150`,
`export_package_engine.py:599`.

**Consequence: writing even one date of the historical window into prod would
blind Step 4, Step 5 and the export engine to all existing June/July feature
history, and break the normal daily run — silently, with no error.** The
`ema150` failure in §4.1 is loud and safe; this one is quiet and destructive,
and it is exactly what Stage 3 would have hit immediately after anyone "fixed"
§4.1 by adding the missing column.

Implication for Stage 3: it must be a **full recompute of all 152 dates to v05**
(2025-12-03 → 2026-07-13), not a 123-date top-up, so that one consistent version
covers the whole range. Then the stale v04 rows should be dropped deliberately.
Both of those are decisions for review, not something to do unprompted.

Related latent bug (not triggered today, worth noting once): the lexicographic
`max()` breaks at the v09 → v10 rollover, since `'features_v10' < 'features_v09'`.

### 4.3 `market_regime` is populated on only 6 of 29 feature dates

```
2026-06-11, 06-18, 06-26, 07-02, 07-08, 07-13   → market_regime = 'bull' (fully populated)
all other 23 dates (incl. all of the Jul-14 backfill) → market_regime NULL
```

The populated dates are exactly the six that have a `pipeline_runs` row — i.e.
regime is written by the **orchestrator's `market_regime_classification` step**,
not by `FeatureEngine.calculate()`. A bulk feature backfill therefore leaves
`market_regime` NULL for every date it touches.

This matters because of the CLAUDE.md rule:
> `market_regime` NULL never defaulted to neutral; NULL blocks BUY
> (WATCHLIST_ONLY at most)

So populating features for the 123-date window **without** a corresponding
regime pass would make every one of those dates incapable of producing a BUY —
which would silently bias any diagnostics run over that window. This is the
"/regime" half of the note's title and needs its own plan in Stage 3.

### 4.4 `2026-02-02` — 3,915 rows, 0 `feature_ready`
Pre-existing, unrelated to this crash (written 2026-07-14 21:31). It is the
known probe date from the feature-backfill-cliff investigation; it predates the
2024-12-02 backfill, so it had insufficient lookback. Harmless, but it is the
one date in `daily_features` that is entirely not-ready.

---

## 5. State left behind

- `prod.duckdb` — **untouched** (mtime still 2026-07-18 20:35).
- No locks acquired or released; nothing deleted.
- Scratch copy `probe.duckdb` + probe scripts remain under
  `D:\Temp\claude\D--Python-Stock-AI-Platform\39948c0d-…\scratchpad\`
  (safe to delete; ~356 MB).
- No commit.

## 6. Stop point and what Stage 3 needs decided first

Stopping here for review as instructed. **Stage 3+ not started.** Before it can
run, three things need an explicit decision — none were made here:

1. **How to migrate prod to the `ema150` column** (§4.1) — `ALTER TABLE ADD
   COLUMN` on prod, or a rebuild. There is currently no migration path in
   `schema_manager` for an existing DB.
2. **How to handle the v04 → v05 transition** (§4.2) — recommended: recompute
   all 152 dates to v05, then drop the stale v04 rows in the same maintenance
   window, so `daily_features_current` is never left pointing at a partial
   version. Doing (1) without (2) breaks production silently.
3. **Whether regime gets populated for the window** (§4.3) — a feature-only
   backfill leaves all 123 dates regime-NULL, which per CLAUDE.md blocks BUY on
   every one of them.

Budget for the run itself: **~12 h at mean, ~14 h worst case** for the 152-date
full recompute, unless the write loop is batched first (§3.1).
