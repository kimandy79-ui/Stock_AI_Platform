# Module 07 Coding Prompt — Benchmark / Sector ETF Loader

Use Project Instructions and Project Files.

Work silently. Do not narrate file reading, planning, or intermediate reasoning.

## Attached

1. `stock_ai_platform_module06_stable.zip`
   - Current stable codebase after Modules 01–06.
   - Modules 01–06 are accepted and frozen.
   - Use this zip as the implementation base.

## Task

Implement only **Module 07 — Benchmark / Sector ETF Loader**.

Module 07 loads benchmark and sector ETF price history before the feature engine.

It must:

- load required benchmark symbols from `app.config.constants.REQUIRED_BENCHMARK_SYMBOLS`;
- fetch price bars only through the Module 04 `MarketDataProvider` interface;
- write benchmark / ETF / index prices to `daily_prices`;
- upsert loaded benchmark / ETF / index symbols into `ticker_master`;
- seed and maintain `sector_etf_map`;
- return `ServiceResult`.

Do not implement Module 08 or later.

## Source retrieval hints

Use `00_PROJECT_FILE_MAP.md` and retrieve only the smallest relevant Project Files.

For this task, primarily use:

- `01b_SCHEMA_AND_DATA.md` — `daily_prices`, `ticker_master`, `sector_etf_map`, `ServiceResult`;
- `01d_MODULES_AND_PIPELINE.md` — Module 07 responsibility and pipeline placement;
- `02_PROJECT_IMPLEMENTATION_CONTEXT.md` — coding, testing, logging, module-boundary rules;
- `02b_ARCHITECTURE_DECISIONS.md` — benchmark/ETF, raw/adjusted, DB-boundary decisions;
- `M04_PROVIDER_INTERFACE_SPEC.md` — `MarketDataProvider`, `PriceHistoryRequest`, `PriceBar`;
- `M05_YAHOO_PROVIDER_SPEC.md` — accepted provider behavior;
- `M06_UNIVERSE_SNAPSHOT_SPEC.md` — service/test structure to mirror.

If sources conflict, report the conflict and recommend the safest interpretation.

## Public API

Implement exactly:

```python
class BenchmarkEtfLoader:
    def load(
        self,
        provider: MarketDataProvider,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        ...
```

Constructor may be parameter-free or may accept only an optional injected DuckDB-manager-like dependency for testing.

No alternative public API.

`app/services/benchmarks/__init__.py` should re-export:

```python
from app.services.benchmarks.benchmark_etf_loader import BenchmarkEtfLoader

__all__ = ["BenchmarkEtfLoader"]
```

## Scope

Allowed files:

1. `app/services/benchmarks/__init__.py`
2. `app/services/benchmarks/benchmark_etf_loader.py`
3. `tests/test_benchmark_etf_loader.py`
4. `README.md` — short Module 07 note only
5. `M07_BENCHMARK_ETF_LOADER_SPEC.md` — project root

If the stable codebase uses a different established service layout, follow the existing layout and state the chosen path in design notes.

Forbidden:

- Do not modify frozen Modules 01–06 except for a real integration blocker.
- Do not modify provider contracts, DB manager, schema manager, config constants, dependencies, existing tests, or `docs/*.md`.
- Do not add dependencies.
- Do not call Yahoo/yfinance directly.
- Do not bypass the provider interface.
- Do not open DuckDB directly or bypass the DuckDB manager.
- Do not run schema DDL.
- Do not write to `simulation.duckdb`.
- Do not write to `ticker_universe_snapshot`.
- Do not implement validation, mutation detection, feature calculation, screening, proposals, outcomes, simulation, AI review, dashboard, or Module 08+ logic.

## Locked behavior

### Guard rules

Allowed DB roles: `"prod"` and `"debug"` only.

Invalid `db_role`, including `"simulation"`, returns `failed` with no provider calls and no DB writes.

If `start_date > end_date`, return `failed` with no provider calls and no DB writes. Metadata must still contain the exact key set with zero counts.

### Symbols

Load exactly `constants.REQUIRED_BENCHMARK_SYMBOLS`.

Do not hardcode the symbol list when implementing.

Required classification:

- SPY, QQQ → `SYMBOL_TYPE_BENCHMARK`
- `^VIX` → `SYMBOL_TYPE_INDEX`
- sector ETFs → `SYMBOL_TYPE_ETF`

Use constants for symbol-type values and benchmark/ETF membership.

### Provider result handling

For each symbol, call:

```python
provider.get_price_history(PriceHistoryRequest(ticker=symbol, start_date=start_date, end_date=end_date))
```

Read returned bars only from `result.metadata["bars"]`.

Treat missing `"bars"`, `None`, non-list values, or an empty list as zero bars for that symbol and skip it with a warning.

If provider result is `success_with_warnings` and contains at least one `PriceBar`, load the symbol and propagate provider warnings into the loader warnings. Do not count it as skipped unless there are zero accepted bars.

A **loaded symbol** means a required symbol for which the provider returned at least one accepted `PriceBar` that is written to `daily_prices`.

### `^VIX`

For `^VIX`:

- `close_raw = close_adj`;
- raw/adjusted open/high/low use provider values when available;
- `volume_raw = NULL`;
- `volume_adj = NULL`.

### `daily_prices`

Upsert by `(ticker, date)` using `PriceBar`.

Required defaults:

- `volume_adj = NULL`;
- `adjustment_factor = NULL`;
- `data_quality_status = "ok"`;
- `mutation_flag = FALSE`;
- `dividend_amount = 0` when missing;
- `split_ratio = 1` when missing;
- `created_at` on insert;
- `updated_at` on conflict update.

Module 10 owns adjustment-factor derivation. Module 09 owns validation.

### `ticker_master`

Upsert only loaded benchmark/index/ETF symbols.

Rules:

- `yahoo_symbol = ticker`;
- set correct `symbol_type`;
- set `active_flag = TRUE`;
- set `last_updated = now()`;
- do not clobber existing Module-06 fields such as `first_seen`, `last_seen`, `company_name`, `exchange`, `sector`, `industry`, `security_type`, or `delisted_flag`.

### `sector_etf_map`

Seed from `constants.SECTOR_ETF_MAP`.

Use insert-or-ignore semantics.

Do not update existing rows.

Module 07 is the sole owner of `sector_etf_map`.

`sector_etf_map_seeded` must be counted deterministically. Do not rely on `cursor.rowcount` unless verified. Prefer reading existing sectors before insert and counting absent keys, or use a tested DuckDB-supported `RETURNING` pattern.

### Failure handling

- Per-symbol provider failure or zero bars → warning, skip symbol, continue.
- If all symbols fail or return empty → `success_with_warnings`, `symbols_loaded == 0`, no `daily_prices` rows and no `ticker_master` upserts.
- Even if all symbols fail/empty, still seed `sector_etf_map`, because it is static config owned by Module 07 and does not depend on provider success.
- DB unavailable or write failure → `failed`.

### Transaction and idempotency

Provider calls are made before DB writes. Collect all successful per-symbol bars first.

After provider collection, perform all database writes in **one whole-run transaction**, not per-symbol commits:

```text
BEGIN;
  upsert ticker_master for loaded symbols;
  upsert daily_prices for all accepted bars;
  seed sector_etf_map;
COMMIT;
```

On any database write error, `ROLLBACK` and return `failed` with no partial/orphaned DB writes.

`daily_prices` and `ticker_master` use `ON CONFLICT DO UPDATE`; `sector_etf_map` uses `ON CONFLICT DO NOTHING`. Re-running the same date range is safe and produces no duplicates.

## ServiceResult

Return `app.utils.service_result.ServiceResult`.

`rows_processed = price_rows_written`.

`metadata` must contain exactly these keys on every return path:

```text
db_role
start_date
end_date
symbols_requested
symbols_loaded
symbols_skipped
price_rows_written
ticker_master_upserted
sector_etf_map_seeded
```

Use project logging with bound `run_id`. No `print()`.

## Required tests

Create `tests/test_benchmark_etf_loader.py`.

Tests must be offline and isolated with temp DuckDB paths.

Cover:

- import and exact public signature;
- fresh load;
- idempotency;
- `^VIX` handling;
- symbol-type assignment;
- provider `success_with_warnings` with bars;
- per-symbol failure;
- all-symbol failure / empty data, including the locked `sector_etf_map` behavior;
- invalid `db_role` / `"simulation"` guard;
- invalid date range guard;
- `sector_etf_map` content and idempotency;
- deterministic `sector_etf_map_seeded` count;
- `ticker_master` non-clobbering;
- transaction rollback for DB write failure;
- `adjustment_factor = NULL`;
- `volume_adj = NULL`;
- `data_quality_status = "ok"`;
- static scan for forbidden imports/direct DB/schema operations/`print`;
- exact `ServiceResult` metadata keys.

Existing Module 01–06 tests must pass unchanged.

## Module-specific source of truth

Create:

`M07_BENCHMARK_ETF_LOADER_SPEC.md`

Keep it concise and implementation-oriented.

Include:

- purpose;
- scope and non-scope;
- source-of-truth references;
- exact public API;
- symbol set and classification;
- provider result handling;
- `^VIX` handling;
- `PriceBar → daily_prices` rules;
- `ticker_master` upsert rules;
- `sector_etf_map` ownership and seeding;
- failure handling;
- whole-run transaction/idempotency strategy;
- DB-manager usage;
- exact metadata keys;
- allowed/forbidden files;
- testing requirements;
- assumptions/open questions.

Do not invent architecture or override higher-priority Project Files.

## Output

Follow Project Instructions output format.

Also include suggested commit message:

```text
module07_benchmark_etf_loader_stable
```
