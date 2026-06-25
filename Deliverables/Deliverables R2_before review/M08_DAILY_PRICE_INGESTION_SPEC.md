# M08 — Daily Price Ingestion — Module Spec

Status: accepted (Module 08). Concise, implementation-oriented source of truth
for the daily price ingestion engine. Derived from the Module 08 task,
`01a_CORE_PRINCIPLES.md`, `01b_SCHEMA_AND_DATA.md`, `01c_FORMULAS_AND_CONFIGS.md`,
`01d_MODULES_AND_PIPELINE.md`, `02_PROJECT_IMPLEMENTATION_CONTEXT.md`,
`02b_ARCHITECTURE_DECISIONS.md`, `M02_SCHEMA_SPEC.md`,
`M04_PROVIDER_INTERFACE_SPEC.md`, `M05_YAHOO_PROVIDER_SPEC.md`,
`M06_UNIVERSE_SNAPSHOT_SPEC.md`, and `M07_BENCHMARK_ETF_LOADER_SPEC.md`
(structure mirrored — Module 08 is the stock-universe equivalent of Module 07).
This spec introduces no new architecture and overrides no higher-priority
document.

## 1. Purpose

Download and update daily OHLCV prices for **all active stock-universe tickers**
before the feature engine runs. Module 08 produces the `stock` rows in
`daily_prices` and enqueues failed / empty-result tickers into
`data_repair_queue` for later repair. It is the stock-universe counterpart to
Module 07 (benchmark / index / sector-ETF loader).

## 2. Scope / non-scope

In scope: read active stock tickers from `ticker_master`
(`symbol_type = 'stock' AND active_flag = TRUE`, never hardcoded); fetch bars
only through the Module 04 `MarketDataProvider` interface; upsert bars into
`daily_prices` keyed by `(ticker, date)`; enqueue failed / empty-result tickers
into `data_repair_queue` (insert-or-ignore); return a `ServiceResult`.

Out of scope (owned elsewhere): calling Yahoo/`yfinance` or any vendor directly
(Modules 04/05); benchmark/index/ETF ingestion or `sector_etf_map` (Module 07);
universe construction or any write to `ticker_universe_snapshot` (Module 06);
**any write to `ticker_master`** (read-only here); validation / mutation
detection (Module 09); `adjustment_factor` derivation (Module 10);
**processing / resolving / deleting `data_repair_queue` entries** (Module 08 only
enqueues — it is not the repair processor); features, screening, proposals,
outcomes, simulation, AI review, dashboard. Module 08 never opens DuckDB
directly or `ATTACH`es, never runs DDL, and never writes to `simulation.duckdb`.

## 3. Source-of-truth priority

1. This file (`M08_DAILY_PRICE_INGESTION_SPEC.md`).
2. `01a_CORE_PRINCIPLES.md` / `01b_SCHEMA_AND_DATA.md` / `01c_FORMULAS_AND_CONFIGS.md` / `01d_MODULES_AND_PIPELINE.md`.
3. `02_PROJECT_IMPLEMENTATION_CONTEXT.md` / `02b_ARCHITECTURE_DECISIONS.md`.
4. `M02_SCHEMA_SPEC.md` (`daily_prices`, `ticker_master`, `data_repair_queue`).
5. `M04_PROVIDER_INTERFACE_SPEC.md` (`MarketDataProvider`, `PriceHistoryRequest`,
   `PriceBar` — consumed, never re-implemented).
6. `M05_YAHOO_PROVIDER_SPEC.md` / `M06_UNIVERSE_SNAPSHOT_SPEC.md` /
   `M07_BENCHMARK_ETF_LOADER_SPEC.md`.

On conflict: do not guess — report and recommend the safest interpretation. See
§16 for the one schema-level finding (no unique constraint on the repair-queue
dedup key) and its safe, no-DDL resolution.

## 4. Public API (exact)

```python
class DailyPriceIngestionEngine:
    def ingest(
        self,
        provider: MarketDataProvider,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        ...
```

Exactly one public method. Constructor is parameter-free except an optional
`db_manager` hook used only for test injection
(`DailyPriceIngestionEngine(db_manager=...)`); when omitted, the real
`app.database.duckdb_manager` is used. No module-level function variant, no
extra public methods.

- `provider` — Module 04 `MarketDataProvider`; bars fetched only via
  `provider.get_price_history(PriceHistoryRequest(...))`.
- `start_date` / `end_date` — inclusive `[start_date, end_date]` range per
  request.
- `db_role` — `"prod"` or `"debug"` only, resolved only through `duckdb_manager`;
  `"simulation"` and any other value yield a `failed` result with no writes.
- `run_id` — a fresh `uuid4` is minted when `None`.

## 5. Ticker selection

Active stock tickers are read from `ticker_master` (never hardcoded):

```sql
SELECT ticker FROM ticker_master
WHERE symbol_type = 'stock' AND active_flag = TRUE
ORDER BY ticker;
```

`active_flag` is `BOOLEAN` in the frozen Module 03 schema
(`schema_manager.py` / `M02_SCHEMA_SPEC.md` §3.4), so the predicate uses
`active_flag = TRUE`. Benchmark / index / ETF symbols are excluded by the
`symbol_type = 'stock'` filter (those are Module 07). The selection runs in a
short **read-only** connection that is closed before the fetch phase, so no
transaction is held open across provider calls. Each request is built with
`symbol_type = constants.SYMBOL_TYPE_STOCK`, the requested `start_date` /
`end_date`, and the ticker from `ticker_master`, using the exact M04
`PriceHistoryRequest` field names.

## 6. `PriceBar` → `daily_prices` (upsert by `(ticker, date)`)

```
ticker               = loaded ticker
date                 = bar.date
open_raw/high_raw/low_raw = bar.* (raw)
close_raw            = bar.close_raw          # stocks use raw verbatim (no ^VIX rule)
volume_raw           = bar.volume_raw
open_adj/high_adj/low_adj/close_adj = bar.* (adjusted)
volume_adj           = NULL
dividend_amount      = bar.dividend_amount if not None else 0
split_ratio          = bar.split_ratio if not None else 1
adjustment_factor    = NULL                   # Module 10 owns derivation
source_provider      = bar.source_provider
data_quality_status  = "ok"                   # Module 09 owns real validation
mutation_flag        = FALSE
created_at           = now()                  # on INSERT only
updated_at           = NULL on INSERT; now() on CONFLICT update
```

Upsert is `INSERT ... ON CONFLICT (ticker, date) DO UPDATE SET ...`; `created_at`
is preserved on conflict, `updated_at` refreshed. `price_rows_written` is the
number of valid `PriceBar` records successfully passed through this upsert
(inserts + conflict updates), counted in application code — not via
`cursor.rowcount`.

## 7. `data_repair_queue` enqueue (insert-or-ignore, reason, no processing)

When a ticker's provider call fails, raises, returns a contract-violating
result (missing/`None` `metadata['bars']`), or returns zero bars, a repair task
is enqueued:

```
repair_id      = uuid5(NAMESPACE_URL, "data_repair_queue:<ticker>:<repair_date>:<repair_reason>")
ticker         = ticker
repair_date    = end_date          # ingestion range end; best known date
repair_reason  = "missing_price"
attempts       = 0
max_attempts   = 3
last_attempt   = NULL
status         = "pending"
created_at     = now()
updated_at     = NULL
```

Insert-or-ignore is keyed on the logical triple
`(ticker, repair_date, repair_reason)`. Because the frozen schema defines **no
unique constraint** on that triple (only `repair_id` PRIMARY KEY and a
non-unique `idx_repair_status(status, repair_date)` index — see §16), the
`repair_id` is derived **deterministically** (`uuid5`) from the logical key and
the insert uses the existing PRIMARY KEY as the conflict target:

```sql
INSERT INTO data_repair_queue (...)
VALUES (..., 'pending', CAST(now() AS TIMESTAMP), NULL)
ON CONFLICT (repair_id) DO NOTHING
RETURNING repair_id;
```

This gives **DB-enforced** insert-or-ignore (atomic at the DB layer, no
read-then-write race window) without any DDL: the same logical task always maps
to the same `repair_id`, so a second insert is a no-op even under concurrent
Module 08 runs. `RETURNING repair_id` yields one row per actual insert and zero
per conflict, and `repair_queue_enqueued` counts only those **newly inserted**
rows. A within-run guard skips redundant inserts for the same logical key.

Compatibility guard: a repair row created by a prior version with a random
`uuid4` for the same logical key would not collide on the deterministic
`repair_id`. To avoid a logical duplicate in that case, existing
`(ticker, repair_date)` pairs for `repair_reason = 'missing_price'` are read at
the start of the write phase and used to skip the deterministic insert when a
legacy row already covers the key.

Module 08 only **enqueues**. It never reads queue rows for processing, never
updates `attempts` / `status`, and never deletes entries — that is the repair
processor's job, not Module 08's.

## 8. `ticker_master` is read-only

Unlike Module 07, Module 08 **never** inserts, updates, or upserts
`ticker_master`. It only reads the active stock list (§5). No SQL literal in the
module writes `ticker_master`.

## 9. Failure handling

```
Invalid db_role (incl. "simulation")  -> failed, no reads, no writes,
                                         no provider calls
Invalid date range (start_date >       -> failed, no reads, no writes,
  end_date)                              no provider calls
Allowed db_role                        -> "prod" / "debug" only
Ticker-selection (DB read) failure     -> failed, no writes
Per-ticker provider raised exception   -> warning, enqueue repair, skip, continue
Per-ticker provider status == failed   -> warning, enqueue repair, skip, continue
Per-ticker missing metadata['bars']    -> warning (contract violation),
  (or value is None)                     enqueue repair, skip, continue
Per-ticker zero bars (key present)     -> warning, enqueue repair, skip, continue
Per-ticker success_with_warnings +     -> bars loaded; each provider warning
  valid bars                             propagated with ticker context
All tickers fail / empty / contract-   -> success_with_warnings,
  violating                              tickers_loaded == 0,
                                         price_rows_written == 0,
                                         repair queue still written
DB connect / write failure             -> failed (transaction rolled back)
```

Status: `failed` for invalid `db_role`, invalid range, ticker-selection read
failure, or any DB write error; `success_with_warnings` when any ticker is
skipped/enqueued **or** any provider warning was propagated; `success` only when
every requested ticker loads with no warnings (provider-side or engine-side),
including the degenerate "no active stocks" case (clean `success`, zero rows).

Per-ticker outcomes (load with bar count, provider failure, exception, missing
`metadata['bars']`, zero bars) are logged through the bound
`RunIdLoggerAdapter` so production runs can be diagnosed without parsing the
returned `warnings` list.

## 10. Transaction / idempotency strategy

Fetch and write phases are separated. **All provider fetches happen first, with
no DB writes and no open transaction.** Then a **single** transaction wraps the
entire DB write phase:

```
BEGIN TRANSACTION;
  -- (1) daily_prices upsert for each loaded bar
  -- (2) data_repair_queue insert-or-ignore (deterministic repair_id +
  --     ON CONFLICT (repair_id) DO NOTHING) for failed tickers
COMMIT;
```

On any error inside the transaction the engine issues `ROLLBACK` and returns a
`failed` `ServiceResult` with no partial / orphaned rows (no half-written
prices, no stray repair rows). This single-global-transaction granularity
mirrors Module 07. Re-running the same range is idempotent: `daily_prices`
upserts by `(ticker, date)` (no duplicates; `updated_at` advances) and
`data_repair_queue` re-enqueue for the same `(ticker, end_date, 'missing_price')`
is a no-op.

## 11. DB-manager usage

All DB access is via `app.database.duckdb_manager.connect(db_role)` (read-only
for selection, read-write for the transaction) or an injected manager-like
object in tests. Module 08 never imports `duckdb` directly, never opens a path,
never `ATTACH`es, and runs no DDL.

## 12. `ServiceResult` (exact metadata keys)

`rows_processed = price_rows_written`. `metadata` carries exactly these keys on
**every** return path (guard failure, selection failure, write failure,
success):

```
db_role
start_date              # ISO string
end_date                # ISO string
tickers_requested
tickers_loaded
tickers_skipped
price_rows_written
repair_queue_enqueued
```

Definitions: `tickers_requested` = active stocks selected; `tickers_loaded` =
tickers with ≥1 bar successfully upserted; `tickers_skipped` =
`tickers_requested − tickers_loaded`; `price_rows_written` = valid `PriceBar`
records passed through the upsert (inserts + conflict updates);
`repair_queue_enqueued` = newly inserted repair rows (excludes ignored
duplicates). On the `db_role` and date-range guards (before any read),
`tickers_requested` is `0`.

Logging uses the project `RunIdLoggerAdapter` with the bound `run_id`; no
`print()`.

## 13. Allowed / forbidden files

Allowed (created/changed by Module 08):

```
app/services/ingestion/__init__.py
app/services/ingestion/daily_price_ingestion.py
tests/test_daily_price_ingestion.py
README.md                              # short Module 08 note only
M08_DAILY_PRICE_INGESTION_SPEC.md      # project root
```

Forbidden: modifying frozen Modules 01–07 (except a real integration blocker —
none required); provider contracts, DB manager, schema manager, config
constants, dependencies, existing tests, or `docs/*.md`; adding dependencies;
calling Yahoo/`yfinance`; bypassing the provider interface; opening DuckDB
directly or bypassing the manager; running schema DDL; writing to
`simulation.duckdb`, `ticker_universe_snapshot`, `sector_etf_map`, or
`ticker_master` (read-only); processing/resolving repair-queue rows;
implementing Module 09+ logic.

## 14. Testing requirements

`tests/test_daily_price_ingestion.py`, fully offline and isolated (temp DuckDB
paths via the `tmp_db_paths` fixture; an in-test `MarketDataProvider` fake; the
active universe seeded directly into `ticker_master`). Covers: import + exact
signature and `run_id` propagation; fresh ingest and idempotency; ticker
selection filtered to `symbol_type = 'stock'` and `active_flag = TRUE`
(plus a no-active-stocks clean-success case); per-ticker failures (failed /
empty / raised / missing `metadata['bars']`) each enqueuing one repair;
all-ticker failure / empty (repair queue still written, no price rows); repair
insert-or-ignore with no duplicate rows on re-run and within a single run, plus
**deterministic `repair_id`** (same logical key → same id, re-run enqueues 0,
different logical keys → different ids) and the `ON CONFLICT (repair_id) DO
NOTHING RETURNING` insert contract;
invalid `db_role` / `"simulation"` guard (no provider calls, no writes) and
invalid date-range guard; provider `success_with_warnings` propagation with
ticker context (bars still loaded, no repair); written-row defaults
(`adjustment_factor = NULL`, `volume_adj = NULL`, `data_quality_status = "ok"`,
`mutation_flag = FALSE`, `dividend_amount = 0`, `split_ratio = 1`, stock
`close_raw` / `volume_raw` verbatim); `ticker_master` not mutated; transaction
rollback / no partial rows (prices and repairs both roll back); static scan for
forbidden imports / direct DB / DDL / forbidden-table writes / `print`; exact
`ServiceResult` metadata keys on success, guard-failure, and write-failure
paths; request symbol-type is always `stock`. Module 01–07 tests pass unchanged.

## 15. Assumptions / open questions

- **A1.** `repair_date` is set to `end_date` (the ingestion range end, the best
  known date) per the locked behavior, for every failure mode.
- **A2.** The `db_role` and date-range guards run before any DB read, so on
  those paths `tickers_requested` is unknown and reported as `0` (mirrors
  Module 07's `symbols_requested` handling on its guard paths, adapted to the
  fact that Module 08's request count depends on a DB read).
- **A3.** A provider that returns `status == success` without a
  `metadata['bars']` key (or with `metadata['bars'] is None`) is treated as a
  provider-contract violation: a distinct warning is emitted, the ticker is
  enqueued for repair and skipped, and the run continues — distinct from a
  legitimate empty range (key present, list empty), which is also enqueued but
  warned differently.
- **A4.** Provider `success_with_warnings` results with valid bars still load
  the bars and are **not** enqueued for repair; every provider-side warning is
  propagated into the engine's `warnings` list prefixed with the ticker.
- **A5.** `start_date` / `end_date` are echoed in metadata as ISO strings
  (mirrors Module 07).

## 16. Schema finding (reported, not modified)

The locked behavior requests `INSERT OR IGNORE` / `ON CONFLICT DO NOTHING`
semantics keyed on `(ticker, repair_date, repair_reason)`. The frozen Module 03
schema (`schema_manager.py`, matching `M02_SCHEMA_SPEC.md`) defines
`data_repair_queue` with **`repair_id` as the only PRIMARY KEY** and a
**non-unique** `idx_repair_status(status, repair_date)` index. There is **no
unique key or constraint** on `(ticker, repair_date, repair_reason)`, so a
DB-level `ON CONFLICT (ticker, repair_date, repair_reason) DO NOTHING` cannot be
expressed without DDL.

Per the task's critical clarification ("if missing, do not modify schema in
Module 08; report it as a blocking schema/spec conflict"), Module 08 does **not**
add a constraint or alter the schema.

Resolution adopted: the frozen schema does not define
`UNIQUE (ticker, repair_date, repair_reason)` for `data_repair_queue`, so
Module 08 cannot rely on `ON CONFLICT` over the logical repair key. To preserve
idempotency without schema changes, Module 08 generates **deterministic**
`repair_id` values from `(ticker, repair_date, repair_reason)`
(`uuid5(NAMESPACE_URL, "data_repair_queue:<ticker>:<repair_date>:<repair_reason>")`)
and inserts with `ON CONFLICT (repair_id) DO NOTHING RETURNING repair_id`. This
provides **DB-enforced** deduplication for Module-08-created rows — atomic at the
DB layer, with no read-then-write race window — so duplicates are avoided across
sequential re-runs *and* concurrent runs, while avoiding any DDL change. A
future schema migration may add the natural unique key directly.

A small application-side pre-check is retained only as a **compatibility guard**:
a legacy repair row written by a prior version with a random `uuid4` for the same
logical key would not collide on the deterministic `repair_id`, so existing
`(ticker, repair_date)` pairs for `repair_reason = 'missing_price'` are read at
the start of the write phase and used to skip a redundant deterministic insert
for keys a legacy row already covers. This pre-check is a convenience for mixed
old/new data, not the dedup mechanism; the DB-level `ON CONFLICT (repair_id)` is.

Concurrency note: the deterministic `repair_id` + `ON CONFLICT (repair_id)`
provides true concurrent insert-or-ignore for Module-08-created rows. (The
legacy-compatibility pre-check, like any read-then-write step, is not itself
concurrency-safe, but it only ever *suppresses* an insert that `ON CONFLICT`
would otherwise also suppress for deterministic rows — it never creates a
duplicate.)

Recommended follow-up (out of scope for Module 08, for the schema owner to
decide): add a `UNIQUE (ticker, repair_date, repair_reason)` constraint to
`data_repair_queue` in a future schema revision so the dedup can target the
natural key directly and the compatibility pre-check can be dropped.
