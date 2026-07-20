# Mutation Detector Over-Flagging Investigation

**Date:** 2026-07-18
**Scope:** Read-only investigation. No code changes, no cleanup, no writes.
`mutation_detector.py` not modified; no `data_repair_queue`/
`feature_rebuild_log` rows modified or deleted; no repair/rebuild jobs run;
no pipeline rerun (one OS-level, read-only Task Scheduler query was made
for deliverable 4 — not a pipeline action).

**Headline finding — this reframes the coder note's hypothesis, not just
confirms it:** the over-flagging is real, but `MutationDetector._plan()`
is **not** the cause and behaves exactly per its spec. The suspected
conflation (adjustment_factor change vs. genuine split) does not exist in
the code. The actual defect is upstream, in the provider/ingestion layer
(`app/providers/yahoo_provider.py` + `app/services/ingestion/daily_price_ingestion.py`,
both frozen modules): yfinance's "no split today" sentinel value is
`0.0`, but the platform's schema/spec convention for "no split" is `1.0`
(or `NULL`). That `0.0` is passed straight through, untranslated, into
`daily_prices.split_ratio` — and `0.0 != 1.0`, so `is_explicit_split()`
correctly (per its own contract) fires on nearly every row, every day,
platform-wide, and has done so since the very first row was ever
ingested (2024-12-02), not just on this backfill. This is a
**platform-wide, all-time, general defect**, not a backfill-specific edge
case.

---

## 1. `_plan()` logic, in full — confirmed correct per spec

`app/services/mutation/mutation_detector.py:482-526`:

```python
for row in rows:
    if not row.is_eligible():
        rows_skipped_non_ok += 1
        continue
    rows_processed += 1

    # Adjustment-factor derivation (independent of mutation detection).
    desired = row.derived_adjustment_factor()
    if _factor_differs(desired, row.adjustment_factor):
        factor_writes.append((row.ticker, row.date, desired))

    # Explicit-split mutation detection.
    if row.is_explicit_split():
        mutation_rows_detected += 1
        if row.mutation_flag is False:
            flag_rows.append((row.ticker, row.date))
        if row.ticker not in mutation_tickers:
            mutation_tickers[row.ticker] = row.date
```

`mutation_rows_detected`, `flag_rows` (→ `mutation_flags_written`), and
`mutation_tickers` (→ `tickers_with_mutation`, drives the repair/rebuild
enqueue) are **all** gated exclusively on `row.is_explicit_split()`
(line 511) — `split_ratio is not None and split_ratio != 1.0`
(`:277-283`). `factor_writes` (→ `adjustment_factors_written`) is a
completely separate branch (lines 505-508), gated on `_factor_differs()`
(`:286-297`, comparing a freshly-derived `close_adj/close_raw` against
the stored `adjustment_factor`). **There is no union, no shared
condition, no code path where an `adjustment_factor` change sets
`mutation_flag` or enqueues anything.** The coder note's suspected
mechanism does not exist in this file.

The reason all four metrics were numerically identical in the log
(`rows_processed=462065  mutation_rows_detected=462065  adj_factor_changes=462065
mutation_flag_candidates=462065`) is **not** that they're the same check —
it's that, for this backfill's rows, both independent conditions happened
to be true for almost every row simultaneously: every fresh row has a
newly-computed `adjustment_factor` differing from its stored NULL (true
for any brand-new row, as suspected) **and**, separately, every row has
`split_ratio != 1.0` (confirmed below — not because of real splits, but
because of the `0.0` sentinel issue). Two independent, coincidentally
correlated causes producing the same near-100% rate, not one conflated
cause.

---

## 2. Real split-ratio count vs. total flagged — root cause identified

Queried `daily_prices` directly for the backfill's date range
(2024-12-01 to 2025-06-01, read-only):

```sql
SELECT COUNT(*), COUNT(CASE WHEN split_ratio IS NOT NULL AND split_ratio != 1 THEN 1 END)
FROM daily_prices WHERE date >= '2024-12-01' AND date <= '2025-06-01'
```
```
total=488802   split_ratio != 1 (non-null) = 462103
```

**But `split_ratio` value distribution reveals the real story:**

```sql
SELECT split_ratio, COUNT(*) FROM daily_prices
WHERE date BETWEEN '2024-12-01' AND '2025-06-01' GROUP BY split_ratio ORDER BY COUNT(*) DESC
```
```
0.0    461,970   <- not a real split; see below
1.0     26,699   <- baseline "no split"
0.1         12
2.0         10
0.05         9
... (many more small counts, fractional/whole ratios consistent with real splits)
```

**461,970 of the 462,103 "flagged" rows have `split_ratio` literally equal
to `0.0`.** Only **133 rows** have `split_ratio` outside `{0.0, 1.0}` —
values like `4.0` (`ANET`, 2024-12-04 — a plausible real forward split),
`0.0625`/`0.02`/`0.1` etc. (plausible reverse splits for various small
tickers) — a population two orders of magnitude smaller, and structurally
distinct (varied, plausible corporate-action ratios vs. one dominant
constant). **133 of 462,103 (0.03%) look like genuine explicit splits;
99.97% are the `0.0` artifact.**

**Root cause, traced to source:**

- `app/providers/yahoo_provider.py:622`:
  ```python
  split_ratio = self._to_float(self._cell(row, _COL_SPLITS, columns))
  ```
  Reads yfinance's "Stock Splits" column verbatim. **In yfinance's own
  convention, `0.0` means "no split occurred that day"** — it is yfinance's
  sentinel/baseline, not a missing value and not equivalent to `1.0`. No
  translation happens here.
- `app/services/ingestion/daily_price_ingestion.py:566`:
  ```python
  split_ratio = bar.split_ratio if bar.split_ratio is not None else 1
  ```
  The module's own docstring (`:562`) says this "applies the missing-value
  default `split_ratio = 1`" — but the guard is `is not None`, and
  yfinance's no-split value is `0.0`, a real float, never `None`. The
  intended default (baseline `1.0` for "no split") **never fires** for the
  overwhelming majority of ordinary, split-free rows, because the input
  isn't missing — it's present and wrong relative to the platform's own
  convention.
- `specs/M10_MUTATION_DETECTOR_SPEC.md:122` documents the check as keyed
  on `SCHEMA §3.7 split_ratio DEFAULT 1` — i.e., the spec's own baseline
  assumption is `1.0` for "no split," matching the schema's `DEFAULT 1`
  column definition. Nothing in the frozen M10 spec or M10's
  implementation is inconsistent with this. The mismatch is entirely
  between yfinance's `0.0` convention and the schema/spec's `1.0`
  convention, and no code anywhere in the M05→M08 chain reconciles the
  two.

**`MutationDetector` is doing exactly what its spec says, given
consistently-wrong input.** This is a provider/ingestion mapping defect
(M05 + M08), not an M10 defect.

---

## 3. Backfill-specific or general? — General, confirmed platform-wide

Checked a normal, non-backfill, recently-ingested date range (ordinary
daily pipeline runs, 2026-06-01 to 2026-06-10):
```sql
SELECT COUNT(*), COUNT(CASE WHEN split_ratio IS NOT NULL AND split_ratio != 1 THEN 1 END)
FROM daily_prices WHERE date >= '2026-06-01' AND date <= '2026-06-10'
```
```
total=31,777   split_ratio != 1 = 31,747  (99.9%)
```

Same pattern, same rate, on ordinary daily-ingested rows that were never
part of any backfill. Checked the **entire table**, all-time:
```sql
SELECT COUNT(*), COUNT(CASE WHEN split_ratio IS NOT NULL AND split_ratio != 1 THEN 1 END),
       MIN(date), MAX(date) FROM daily_prices
```
```
total=1,596,868   split_ratio != 1 = 1,550,041 (97.1%)   min_date=2024-12-02   max_date=2026-07-13
```
```sql
SELECT COUNT(*) FROM daily_prices WHERE mutation_flag = TRUE
```
```
1,543,254 / 1,596,868 rows (96.6%) have mutation_flag = TRUE
```

**This has been happening since the earliest row in the table
(2024-12-02) — the entire lifetime of the platform's data — and affects
essentially every daily-ingested row, not just backfill-inserted ones.**
It is **not** specific to "brand-new historical rows with no prior stored
`adjustment_factor`" as hypothesized; it is a property of every row
`YahooProvider` has ever produced, because the defect lives in how
`split_ratio` is read from the provider, independent of whether the row
is new or being re-ingested.

---

## 4. Confirmed: no active process consumes these queues right now

- Repo-wide search for `UPDATE data_repair_queue` / `UPDATE
  feature_rebuild_log` (the only way a "pending" row could ever be
  consumed/resolved) returns **zero matches** anywhere in `app/` or
  `tools/`. `data_validator.py` (M09, the other writer) documents itself
  explicitly: *"process / resolve / update / delete existing
  `data_repair_queue` rows (it only inserts — it is not the repair
  processor)"* (`:36-38`) — no repair-processor module exists in this
  codebase at all.
- `Get-ScheduledTask` (Windows Task Scheduler, queried directly, read-only)
  returns **no registered tasks** matching this project — confirms the
  project-memory note ("Task Scheduler artifact delivered 2026-07-05 but
  NOT yet registered on machine") is still current, checked fresh rather
  than assumed.

**No active harm is occurring.** Every one of these rows — 7,843
platform-wide as of now (see §5) — sits inert as a `pending`
insert-or-ignore row. Nothing reads or acts on `status='pending'` today.

---

## 5. Precise scope and identification, for a future cleanup decision

**Platform-wide cumulative total (all-time, not just this run):**
```sql
SELECT status, COUNT(*) FROM data_repair_queue WHERE repair_reason='mutation' GROUP BY status
-- ('pending', 7843)
SELECT status, COUNT(*) FROM feature_rebuild_log WHERE reason='mutation' GROUP BY status
-- ('pending', 7843)
```
7,843 rows in each table (1:1 paired, as expected — one of each written
per mutation-flagged ticker), `repair_date`/`affected_start_date` spanning
2024-12-02 through 2026-07-09, **not** just from today's run.

**Cleanly separable by `created_at`, into exactly 3 clusters (by minute),
each corresponding to a distinct historical ingestion/backfill event:**
```
2026-07-14 19:02   ->    110 rows
2026-07-14 20:22   ->  3,948 rows
2026-07-18 17:31   ->  3,785 rows   <- today's backfill run
```
The `2026-07-14` clusters (4,058 rows combined) correlate with the
earlier prod-rebuild backfill event
(`repair_date`/`affected_start_date` 2025-06-02 onward — matching the
"backfilled 2025-06-02→7-13" prod-rebuild memory) — i.e., **this same
defect has already fired at least twice before today**, on a separate
occasion, and those spurious entries have been sitting in the queue
unnoticed since 2026-07-14.

**Today's 3,785 rows are identifiable two independent, exactly-agreeing
ways** (either is sufficient on its own):
```sql
-- by created_at (exact minute)
WHERE repair_reason='mutation' AND created_at BETWEEN '2026-07-18 17:31:00' AND '2026-07-18 17:31:59'
-- by repair_date / affected_start_date range (the backfill's own date window)
WHERE repair_reason='mutation' AND repair_date >= '2024-12-01' AND repair_date <= '2025-06-01'
```
Both return exactly 3,785 — a clean, unambiguous 1:1 match, confirming
either identification method alone would correctly scope a future
targeted cleanup of just today's run.

**Important caveat for any future cleanup, whatever its scope:** the ~133
genuine explicit-split rows found in §2 (and however many more exist
across the full 2024-12-02–2026-07-13 range) are mixed into these same
`repair_reason='mutation'` / `reason='mutation'` populations, and are
legitimate — not spurious. A blanket delete-by-reason would also discard
real corporate-action repair/rebuild tasks. Any future cleanup would need
to cross-reference against the actual `split_ratio` value for the
ticker/date in question (`split_ratio NOT IN (0.0, 1.0)` ≈ genuine;
`= 0.0` ≈ spurious) to distinguish the two populations — not something
attempted or needed in this read-only pass, but worth flagging now so a
future cleanup doesn't accidentally erase real signal.

---

## 6. Anomalies (verbatim)

- The investigation's original hypothesis (adjustment_factor-derivation
  conflated with split detection inside `_plan()`) is **not what's
  happening** — confirmed by direct code reading, not assumed away. The
  real mechanism is a provider-convention mismatch (yfinance `0.0` =
  "no split" vs. platform schema `1.0` = "no split") that was never
  translated anywhere in the M05→M08 ingestion chain. Both `_COL_SPLITS`
  in `yahoo_provider.py` and the `is not None`-only default in
  `daily_price_ingestion.py` are candidates for where a fix would need to
  land, but **both are frozen modules** (M05, M08 — per `CLAUDE.md`'s
  frozen-module list) — any fix would need the same
  ratified-frozen-module-delta process already used once for M04's
  `shares_outstanding` addition, not a casual patch. Per instructions, no
  conclusion is drawn here about whether/how to fix it — flagged for the
  architect.
- This defect's true blast radius (97.1% of all 1,596,868
  `daily_prices` rows ever ingested, 96.6% with `mutation_flag=TRUE`,
  since the platform's very first ingested row) is dramatically larger
  than the single backfill run the coder note asked about. This wasn't
  anticipated going in and is the most important scoping correction this
  investigation produced.
- The queue accumulation is not a one-time event from today: 4,058 of the
  7,843 cumulative pending `mutation` entries predate today's backfill by
  4 days (2026-07-14), from the earlier prod-rebuild backfill — meaning
  this has silently been accumulating queue entries across at least two
  separate historical operations, unnoticed until this investigation.

## No writes, no code changes

Read-only investigation only: `mutation_detector.py`, `yahoo_provider.py`,
`daily_price_ingestion.py`, `data_validator.py`, and
`M10_MUTATION_DETECTOR_SPEC.md` were read, not modified. All DB queries
against `data/duckdb/prod.duckdb` were `read_only=True`. One `Get-ScheduledTask`
OS query (read-only, not a pipeline action). No `data_repair_queue`/
`feature_rebuild_log` rows modified or deleted. No repair/rebuild jobs
run. No commit.
