# Module 08 Coding Prompt — Daily Price Ingestion

Use Project Instructions and Project Files.

Work silently. Do not narrate file reading, planning, or intermediate reasoning.

## Attached

1. `stock_ai_platform_module07_stable.zip`

   * Current stable codebase after Modules 01–07.
   * Modules 01–07 are accepted and frozen.
   * Use this zip as the implementation base.

## Task

Implement only **Module 08 — Daily Price Ingestion**.

Module 08 downloads and updates daily OHLCV prices for all active stock
universe tickers before the feature engine runs. It must:

* read the active ticker list from `ticker_master` (where `symbol_type = 'stock'`
  and `active_flag = TRUE`);
* fetch price bars only through the Module 04 `MarketDataProvider` interface;
* upsert bars into `daily_prices` keyed by `(ticker, date)`;
* enqueue failed or empty-result tickers into `data_repair_queue`;
* return `ServiceResult`.

Do not implement Module 09 or later.

## Source retrieval hints

Use `00_PROJECT_FILE_MAP.md` and retrieve only the smallest relevant Project
Files.

For this task, primarily use:

* `01b_SCHEMA_AND_DATA.md` — `daily_prices`, `ticker_master`,
  `data_repair_queue`, `ServiceResult`;
* `01d_MODULES_AND_PIPELINE.md` — Module 08 responsibility and pipeline
  placement; error-handling reference (`PIPELINE/72_Error_Handling_Reference.md`);
* `02_PROJECT_IMPLEMENTATION_CONTEXT.md` — coding, testing, logging,
  module-boundary rules;
* `02b_ARCHITECTURE_DECISIONS.md` — raw/adjusted, DB-boundary decisions;
* `M04_PROVIDER_INTERFACE_SPEC.md` — `MarketDataProvider`, `PriceHistoryRequest`,
  `PriceBar`;
* `M05_YAHOO_PROVIDER_SPEC.md` — accepted provider behavior;
* `M06_UNIVERSE_SNAPSHOT_SPEC.md` — service/test structure to mirror;
* `M07_BENCHMARK_ETF_LOADER_SPEC.md` — `daily_prices` upsert pattern,
  transaction strategy, and test structure to mirror (Module 08 is the
  stock-universe equivalent of Module 07).

If sources conflict, report the conflict and recommend the safest interpretation.

## Public API

Implement exactly:

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

Constructor may be parameter-free or may accept only an optional injected
DuckDB-manager-like dependency for testing.

No alternative public API.

## Scope

Allowed files:

1. `app/services/ingestion/__init__.py`
2. `app/services/ingestion/daily_price_ingestion.py`
3. `tests/test_daily_price_ingestion.py`
4. `README.md` — short Module 08 note only
5. `M08_DAILY_PRICE_INGESTION_SPEC.md` — project root

If the stable codebase uses a different established service layout, follow the
existing layout and state the chosen path in design notes.

Forbidden:

* Do not modify frozen Modules 01–07 except for a real integration blocker.
* Do not modify provider contracts, DB manager, schema manager, config
  constants, dependencies, existing tests, or `docs/*.md`.
* Do not add dependencies.
* Do not call Yahoo/`yfinance` directly or bypass the provider interface.
* Do not open DuckDB directly, bypass the DuckDB manager, or run schema DDL.
* Do not write to `simulation.duckdb`.
* Do not write to `ticker_universe_snapshot`, `sector_etf_map`,
  `ticker_master` (read-only here), or any feature/step/proposal/outcome table.
* Do not implement Module 09+ logic, including validation, mutation detection,
  features, screening, proposals, outcomes, simulation, AI review, or dashboard.

## Locked behavior

### Ticker selection

Read tickers from `ticker_master` where:

```sql
symbol_type = 'stock' AND active_flag = TRUE
```

Do not hardcode any ticker list.
Do not ingest benchmark, index, or ETF symbols (those are Module 07).

### `daily_prices` upsert

Upsert by `(ticker, date)` using each `PriceBar`.

Required defaults (mirror Module 07):

* `volume_adj = NULL`;
* `adjustment_factor = NULL` (Module 10 owns derivation);
* `data_quality_status = "ok"` (Module 09 owns real validation);
* `mutation_flag = FALSE`;
* `dividend_amount = 0` when `PriceBar.dividend_amount` is `None`;
* `split_ratio = 1` when `PriceBar.split_ratio` is `None`;
* `created_at` set on INSERT;
* `updated_at` set on conflict update only.

### `data_repair_queue`

When a ticker's provider call fails or returns zero bars, enqueue a repair
task:

```
repair_id      = new uuid4()
ticker         = ticker
repair_date    = end_date  (the ingestion range end; best known date)
repair_reason  = "missing_price"
attempts       = 0
max_attempts   = 3
status         = "pending"
created_at     = now()
updated_at     = NULL
```

Use `INSERT OR IGNORE` (or `ON CONFLICT DO NOTHING`) semantics keyed on
`(ticker, repair_date, repair_reason)` so re-runs don't duplicate queue rows.

Do not process, resolve, or delete repair queue entries. Enqueuing is the only
write. Module 08 is not the repair processor.

### `db_role` guard

Allowed DB roles: `"prod"` and `"debug"` only.

Invalid `db_role`, including `"simulation"` → return `failed`, no writes.

### Date-range guard

If `start_date > end_date` → return `failed` before any provider calls or DB
writes.

### Per-ticker failure handling

* Provider failure (failed status, exception, or missing `metadata["bars"]`
  key) → warning, enqueue repair, skip ticker, continue.
* Zero bars (key present, list empty) → warning, enqueue repair, skip ticker,
  continue.
* Provider `success_with_warnings` with valid bars → load bars, propagate
  provider warnings with ticker context, continue.

### All-ticker failure

If every ticker fails or returns empty → `success_with_warnings`,
`tickers_loaded == 0`, `price_rows_written == 0`, repair queue still written.

### DB failure

DB connect or write failure → `failed`, transaction rolled back.

### Failure handling / transactions

Use a transaction strategy that prevents orphaned or partial rows on error.
State the granularity clearly in the spec (per-ticker or single global
transaction; match the approach from Module 07 or justify a different choice).

## Critical implementation clarifications

- Before implementing `data_repair_queue` insert-or-ignore, verify from the existing schema that a unique key or constraint exists on `(ticker, repair_date, repair_reason)`. If missing, do not modify schema in Module 08; report it as a blocking schema/spec conflict.
- Use the actual schema type for `ticker_master.active_flag`. If it is BOOLEAN, use `active_flag = TRUE`. If the schema defines another representation, follow the schema and report the mismatch.
- Define `price_rows_written` as the number of valid `PriceBar` records successfully passed through the `daily_prices` upsert operation, including both inserts and conflict updates. Do not rely on DuckDB `cursor.rowcount` unless existing project code already uses it reliably.
- Define `repair_queue_enqueued` as the number of newly inserted repair queue rows, not duplicate rows ignored by insert-or-ignore.
- For each stock ticker, create `PriceHistoryRequest` using the ticker symbol from `ticker_master`, requested `start_date` / `end_date`, and `symbol_type="stock"` if the provider request supports `symbol_type`. Follow the exact M04 provider interface field names.
- Preferred transaction strategy: separate fetch phase from write phase. Fetch all provider results first without DB writes. Then perform `daily_prices` upserts and `data_repair_queue` inserts inside one DB transaction. On DB failure, roll back all Module 08 writes.
- Do not hold an open DB transaction while calling the provider.
- Unlike Module 07, Module 08 must never insert, update, or upsert `ticker_master`. It only reads active stock tickers from `ticker_master`.

## ServiceResult

Return `app.utils.service_result.ServiceResult`.

`rows_processed = price_rows_written`.

`metadata` must contain exactly these keys on every return path:

```text
db_role
start_date
end_date
tickers_requested
tickers_loaded
tickers_skipped
price_rows_written
repair_queue_enqueued
```

Use project logging with bound `run_id`. No `print()`.

Log per-ticker outcomes: successful load with bar count, provider failure,
exception, missing `metadata["bars"]`, and zero bars.

## Required tests

Create `tests/test_daily_price_ingestion.py`.

Tests must be offline and isolated with temp DuckDB paths.

Cover:

* API/import/signature and `run_id` propagation;
* ticker selection from `ticker_master`;
* fresh ingest and idempotency;
* filtering to `symbol_type = 'stock'` and `active_flag = TRUE`;
* per-ticker failures: failed status, exception, missing `metadata["bars"]`,
  zero bars, and all-ticker failure / empty data;
* repair queue insert-or-ignore and no duplicate rows on re-run;
* invalid `db_role` / `"simulation"` guard and invalid date-range guard;
* provider `success_with_warnings` propagation with ticker context;
* written-row defaults: `adjustment_factor = NULL`, `volume_adj = NULL`,
  `data_quality_status = "ok"`, `mutation_flag = FALSE`,
  `dividend_amount = 0`, and `split_ratio = 1`;
* transaction rollback / no partial rows;
* static scan for forbidden imports/SQL/direct DB/schema operations/`print`;
* exact `ServiceResult` metadata keys on every return path.

Existing Module 01–07 tests must pass unchanged.

## Module-specific source of truth

Create:

`M08_DAILY_PRICE_INGESTION_SPEC.md`

Keep it concise and implementation-oriented. Mirror the structure of
`M07_BENCHMARK_ETF_LOADER_SPEC.md`.

Include:

* purpose;
* scope and non-scope;
* source-of-truth references;
* exact public API;
* ticker selection query;
* `PriceBar → daily_prices` upsert rules;
* `data_repair_queue` enqueue rules (insert-or-ignore, reason, no processing);
* failure handling (per-ticker and global);
* transaction / idempotency strategy;
* DB-manager usage;
* exact metadata keys;
* allowed/forbidden files;
* testing requirements;
* assumptions/open questions.

Do not invent architecture or override higher-priority Project Files.

## Output

Follow Project Instructions output format.

Also include suggested commit message:

```text
module08_daily_price_ingestion_stable
```
