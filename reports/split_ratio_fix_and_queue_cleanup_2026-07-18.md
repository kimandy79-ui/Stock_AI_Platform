# Split-Ratio Convention Fix (Frozen-Module) + Spurious Queue Cleanup

**Date:** 2026-07-18
**Scope:** Part A — code fix under a granted, narrow frozen-module
exemption (`yahoo_provider.py` only). Part B — data cleanup (`DELETE`)
against `data_repair_queue`/`feature_rebuild_log`, executed after a
dry-run count confirmed inside the same transaction as the delete.
**No commit — diff delivered, stop, per current policy.**

---

## Part A — `split_ratio` convention fix

### 1. Diff — `app/providers/yahoo_provider.py`

```diff
             dividend_amount = self._to_float(self._cell(row, _COL_DIVIDENDS, columns))
-            split_ratio = self._to_float(self._cell(row, _COL_SPLITS, columns))
+            # yfinance's "Stock Splits" column uses 0.0 as its own "no split
+            # occurred today" sentinel -- distinct from the platform schema's
+            # DEFAULT 1 convention for "no split" (SCHEMA_SPEC.md sec 3.7). A
+            # literal 0.0 ratio is not physically meaningful for a real split
+            # (a split never reduces shares-per-share to zero), so it is
+            # unambiguously the vendor's no-op sentinel, not a genuine event.
+            # Translated to None (missing) here so
+            # daily_price_ingestion.py's existing
+            # `split_ratio if split_ratio is not None else 1` default fires
+            # correctly downstream, instead of writing 0.0 verbatim and
+            # tripping MutationDetector.is_explicit_split()'s `!= 1` check on
+            # every ordinary non-split row. Narrow, ratified frozen-module
+            # exemption (2026-07-18): scoped strictly to this translation; no
+            # other provider logic changed. Forward-only -- does not
+            # retroactively correct already-stored rows.
+            _raw_split_ratio = self._to_float(self._cell(row, _COL_SPLITS, columns))
+            split_ratio = None if _raw_split_ratio == 0.0 else _raw_split_ratio
             adj_close = self._to_float(self._cell(row, _COL_ADJ_CLOSE, columns))
```

No other line in `yahoo_provider.py` touched. `daily_price_ingestion.py`
(M08) **not modified** — its existing default (`split_ratio = bar.split_ratio
if bar.split_ratio is not None else 1`, `:566`) was already correct; it
simply never fired for ordinary rows because the input wasn't `None`. It
now fires correctly because the input from the fixed provider is `None`
for non-split days.

### Scope-check: is `0.0` ever a genuine split ratio?

No. A split ratio (new shares per old share) of `0.0` would mean a
security's outstanding shares went to zero in the "split" — not a
concept that describes any real split, forward or reverse. Every
genuine-split value found in the prior investigation's data was a real,
plausible ratio (`4.0`, `0.0625`, `0.02`, `0.1`, `1.05`, `1.01`, `1.023`,
etc.) — never `0.0` itself. This matches yfinance's own documented
"Stock Splits" column convention (0 = no event that day, matching how its
"Dividends" column also uses `0.0` for no-dividend days). Treating `0.0`
as the no-split sentinel is unambiguous.

### 2. Tests — new, all passing

`tests/test_yahoo_provider.py`:
- `test_split_ratio_zero_maps_to_none` — a `Stock Splits: 0.0` input row
  maps to `PriceBar.split_ratio is None`.
- `test_split_ratio_nonzero_passes_through_unchanged` — `2.0` and `0.5`
  inputs pass through unmodified.

`tests/test_daily_price_ingestion.py`:
- `test_real_yahoo_provider_split_zero_writes_default_one` — cross-module
  integration test using a **real** `YahooProvider` (not the local
  `_FakeProvider`), fed a `Stock Splits: 0.0` frame via the same fake-
  yfinance machinery `test_yahoo_provider.py` uses (`FakeYF`,
  `_price_frame`, imported directly rather than reimplemented), run
  through the real `DailyPriceIngestionEngine().ingest()`. Asserts the
  final `daily_prices.split_ratio` written is `1.0`, proving the
  provider's `0.0`→`None` translation and the ingestion module's existing
  `None`→`1` default now compose correctly end-to-end.

**Results:**
```
$ python -m pytest tests/test_yahoo_provider.py -v -k split_ratio
2 passed

$ python -m pytest tests/test_daily_price_ingestion.py -v -k split
2 passed
```

**Full module suites:**
```
$ python -m pytest tests/test_yahoo_provider.py -v
24 passed, 1 failed  -- FAILED test_only_yahoo_provider_references_yfinance

$ python -m pytest tests/test_daily_price_ingestion.py -v
37 passed
```
The one failure is the **pre-existing, already-documented** static-scan
issue (project memory `known_preexisting_test_failures.md`, 2026-07-08):
the assertion scans all of `app/**` for the literal token `yfinance`
appearing in *executable code* outside `yahoo_provider.py`, and fails on
an unrelated function name (`..._fundamentals_from_yfinance_info`) in a
different file. Confirmed unrelated to this change: (a) my added text is
a comment (stripped by the scanner's tokenizer before comparison), (b) it
lives inside `yahoo_provider.py` itself, which the test explicitly
*expects* to contain `yfinance`. This same failure was independently
observed and documented during the P2-F gate fix work earlier today,
confirming it predates this change.

### 3. Explicit confirmation — forward-only, no retroactive data fix

This fix corrects **ingestion going forward only**. It does not, and was
not asked to, retroactively recompute `split_ratio`/`adjustment_factor`/
`mutation_flag` for the ~1.5M already-stored rows carrying the `0.0`
artifact. Those rows remain as they are; a retroactive correction (its
own backfill-style pass, re-deriving `split_ratio` for every historical
row, or a narrower re-ingestion) is a separate, larger decision, not
attempted here. `daily_prices` was not written to by any part of this
delivery — confirmed in Part B's verification (§B.4): identical row count
and identical `mutation_flag=TRUE` count before and after.

---

## Part B — Spurious queue cleanup

### B.1 — Step 1 dry-run: **actual counts differ materially from the coder note's estimate — reported, not assumed**

Ran the coder note's exact identification query (read-only, then
re-confirmed inside the delete transaction — §B.3):
```sql
SELECT drq.repair_id, drq.ticker, drq.repair_date
FROM data_repair_queue drq
JOIN daily_prices dp ON dp.ticker = drq.ticker AND dp.date = drq.repair_date
WHERE drq.repair_reason = 'mutation'
  AND dp.split_ratio NOT IN (0.0, 1.0)
```

**Result: 7 genuine rows, not ~133.** Total `reason='mutation'` population
confirmed at 7,843 in each table (matching the prior investigation).
**Genuine = 7, spurious = 7,836** — not the estimated ~133 / ~7,710.

**Why the estimate was off, traced precisely:** the coder note's ~133
figure came from the earlier investigation's count of *all* rows with a
genuine (non-0/1) `split_ratio` value across the entire backfill date
range (2024-12-01 to 2025-06-01) — i.e., "how many days across the whole
range had a real split, for any ticker." But `data_repair_queue.repair_date`
is not "a date with a real split" — per `mutation_detector.py`'s own
logic (`mutation_tickers[ticker] = row.date` only `if ticker not in
mutation_tickers`, first-row-per-ticker-in-date-order wins), it's **the
earliest flagged date for that ticker within the specific range a given
`detect()` call scanned**. Since virtually every day is flagged (the
`0.0` artifact), the "first detected date" for almost every ticker is
simply the earliest date that ticker has any data at all in that range —
which is a genuine-split day only in the rare case a real split
coincidentally fell exactly on a ticker's first day of price history
within that window. Confirmed exactly: 6 of the 7 surviving genuine
tickers have `repair_date` of `2024-12-02` or `2025-06-02` — the literal
first day of the two backfill windows this bug has fired in. This is a
narrower, different question than "how many real splits exist in the
range," and the note's own specified query (which I ran verbatim) is the
authoritative one — its result, not the preliminary estimate, is what
Step 2 acted on, exactly as instructed ("confirm the exact count... not
assumed").

Zero edge cases where the join found no matching `daily_prices` row at
all (both tables, checked explicitly) — no ambiguous rows to reason about.

Surviving genuine tickers (7): `BNBX`, `CHRN`, `FTCI`, `LARK`, `LYEL`,
`METC`, `METCB` — split_ratio values `0.067`, `0.067`, `0.1`, `1.05`,
`0.05`, `1.01`, `1.023` respectively — all physically plausible split/
reverse-split ratios, confirming correctness of the identification.

### B.2 — Safety backup taken before any delete

Copied `data/duckdb/prod.duckdb` (357,314,560 bytes) to
`data/duckdb/backups/prod_pre_mutation_cleanup_20260718_195847.duckdb`
before executing any write. Not requested by the note, but a low-cost,
reversible safety step for a destructive operation against the
production database file.

### B.3 — Step 2: actual delete executed

Single transaction, one connection, both tables, using an anti-join
(`NOT EXISTS`) mirroring the Step 1 identification query exactly
(inverted), rather than a separately-computed ID list — eliminates any
gap between what was counted and what was deleted:

```sql
BEGIN TRANSACTION;

DELETE FROM data_repair_queue
WHERE repair_reason = 'mutation'
  AND NOT EXISTS (
    SELECT 1 FROM daily_prices dp
    WHERE dp.ticker = data_repair_queue.ticker
      AND dp.date = data_repair_queue.repair_date
      AND dp.split_ratio NOT IN (0.0, 1.0)
  )
RETURNING repair_id;
-- 7836 rows

DELETE FROM feature_rebuild_log
WHERE reason = 'mutation'
  AND NOT EXISTS (
    SELECT 1 FROM daily_prices dp
    WHERE dp.ticker = feature_rebuild_log.ticker
      AND dp.date = feature_rebuild_log.affected_start_date
      AND dp.split_ratio NOT IN (0.0, 1.0)
  )
RETURNING rebuild_id;
-- 7836 rows

COMMIT;
```

Pre-delete counts were re-derived **inside this same transaction**
(matching §B.1 exactly: total=7843/7843, genuine=7/7, spurious=7836/7836
in each table) and a sanity gate (`remaining == genuine` and
`deleted_count == spurious_count` for both tables) was checked
programmatically before `COMMIT` — a failure would have triggered
`ROLLBACK` instead. Both tables' deletes: **7,836 rows removed each.**
`COMMIT` succeeded.

### B.4 — Step 3: verification

Fresh read-only connection, post-commit:

```
data_repair_queue reason='mutation':    7  (was 7,843)
feature_rebuild_log reason='mutation':  7  (was 7,843)
```

**Other repair reasons untouched** (confirms the delete was correctly
scoped, not a blanket wipe):
```
data_repair_queue: bad_ohlc=46,880 (unchanged)  missing_price=169 (unchanged)  mutation=7
feature_rebuild_log: mutation=7  (only reason ever used in this table)
```

**`daily_prices` completely untouched** (Part B never writes to it, by
design — confirms Part A's forward-only claim and Part B's scoping both
held):
```
Before: 1,596,868 rows, 1,543,254 with mutation_flag=TRUE
After:  1,596,868 rows, 1,543,254 with mutation_flag=TRUE   -- identical
```

**Dashboard "Repair queue" panel (`data_access.py::load_repair_queue`,
`ORDER BY created_at DESC LIMIT 50`) — simulated the underlying query
directly** (did not open the dashboard UI, per the note's own
instruction — just confirmed the data): the newest-50 result now mixes
`bad_ohlc` and `mutation` reasons, rather than being dominated
exclusively by today's 3,785 spurious `mutation` entries as it would have
been before the cleanup. The misleading "50 fresh mutations" appearance
noted in the prior investigation (§4 of
`mutation_flag_downstream_consumption_check_2026-07-18.md`) is resolved.

---

## Anomalies (verbatim)

- **The dry-run count (7 genuine) differs substantially from the coder
  note's estimate (~133).** Reported prominently per instructions ("not
  assumed") rather than silently reconciled — root cause traced precisely
  in §B.1 (the note's estimate measured a different, broader population
  than what `repair_date`'s first-detected-date semantics actually
  capture). The delete proceeded using the verified, data-driven count
  (7/7,836), not the estimate.
- No other anomalies. Sanity gates inside the delete transaction passed
  on the first attempt (no rollback needed); post-commit verification via
  a fresh connection matched the in-transaction counts exactly.

## Commit status

Part A: diff delivered in the working tree
(`app/providers/yahoo_provider.py`, `tests/test_yahoo_provider.py`,
`tests/test_daily_price_ingestion.py`), uncommitted, per current policy.
Part B: the `DELETE` was executed against `data/duckdb/prod.duckdb`
directly (data cleanup, not a code change — no diff to commit for this
part). A pre-cleanup backup of the database file was taken and retained
at `data/duckdb/backups/prod_pre_mutation_cleanup_20260718_195847.duckdb`.
