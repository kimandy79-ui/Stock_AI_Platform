# Module 07 Coding Prompt — Benchmark / Sector ETF Loader

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not repeat or summarize global rules.
Work silently and provide only actionable results.

---

## Attached

1. `stock_ai_platform_module06_stable.zip`
   - Current stable codebase after Modules 01–06.
   - Modules 01–06 are accepted and frozen.
   - Use this zip as the implementation base.

---

## Task

Implement only **Module 07 — Benchmark / Sector ETF Loader**.

Module 07 loads benchmark and sector ETF price history before the feature engine.

It must:

- load required benchmark symbols from `app.config.constants.REQUIRED_BENCHMARK_SYMBOLS`;
- fetch price bars only through the Module 04 `MarketDataProvider` interface;
- write benchmark / ETF / index prices to `daily_prices`;
- upsert benchmark symbols into `ticker_master`;
- seed and maintain `sector_etf_map`;
- return `ServiceResult`.

Do not implement Module 08 or later.

---

## Source retrieval hints

Use `00_PROJECT_FILE_MAP.md` and retrieve only the smallest relevant Project Files.

For this task, primarily use:

- `01b_SCHEMA_AND_DATA.md` — `daily_prices`, `ticker_master`, `sector_etf_map`, `ServiceResult`;
- `01d_MODULES_AND_PIPELINE.md` — Module 07 responsibility and pipeline placement;
- `02_PROJECT_IMPLEMENTATION_CONTEXT.md` — coding, testing, logging, module-boundary rules;
- `02b_ARCHITECTURE_DECISIONS.md` — benchmark / ETF, raw / adjusted, DB-boundary decisions;
- `M04_PROVIDER_INTERFACE_SPEC.md` — `MarketDataProvider`, `PriceHistoryRequest`, `PriceBar`;
- `M05_YAHOO_PROVIDER_SPEC.md` — accepted provider behavior;
- `M06_UNIVERSE_SNAPSHOT_SPEC.md` — service / test structure to mirror.

If sources conflict, report the conflict and recommend the safest interpretation.

---

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

---

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
- Do not call Yahoo / yfinance directly.
- Do not bypass the provider interface.
- Do not open DuckDB directly or bypass the DuckDB manager.
- Do not run schema DDL.
- Do not write to `simulation.duckdb`.
- Do not write to `ticker_universe_snapshot`.
- Do not implement validation, mutation detection, feature calculation, screening, proposals, outcomes, simulation, AI review, dashboard, or Module 08+ logic.

---

## Locked behavior

### Symbols

Load exactly `constants.REQUIRED_BENCHMARK_SYMBOLS`.

Do not hardcode the symbol list when implementing.

Required classification:

- SPY, QQQ → `SYMBOL_TYPE_BENCHMARK`
- `^VIX` → `SYMBOL_TYPE_INDEX`
- sector ETFs → `SYMBOL_TYPE_ETF`

Use constants for symbol-type values and benchmark / ETF membership.

### `^VIX`

For `^VIX`:

- `close_raw = close_adj`;
- raw / adjusted open, high, and low use provider values when available;
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

Upsert every loaded benchmark / index / ETF symbol.

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

### Failure handling

- Invalid `db_role`, including `"simulation"` → `failed`, no writes.
- Allowed DB roles: `"prod"` and `"debug"` only.
- Per-symbol provider failure or zero bars → warning, skip symbol, continue.
- If all symbols fail or return empty → `success_with_warnings`, `symbols_loaded == 0`, no price rows.
- DB unavailable or write failure → `failed`.

Use transactions so failed writes do not leave partial / orphaned rows. State transaction granularity in the spec.

---

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

---

## Required tests

Create `tests/test_benchmark_etf_loader.py`.

Tests must be offline and isolated with temp DuckDB paths.

Cover:

- import and exact public signature;
- fresh load;
- idempotency;
- `^VIX` handling;
- symbol-type assignment;
- per-symbol failure;
- all-symbol failure / empty data;
- invalid `db_role` / `"simulation"` guard;
- `sector_etf_map` content and idempotency;
- `ticker_master` non-clobbering;
- transaction rollback;
- `adjustment_factor = NULL`;
- `volume_adj = NULL`;
- `data_quality_status = "ok"`;
- static scan for forbidden imports / direct DB / schema operations / `print`;
- exact `ServiceResult` metadata keys.

Existing Module 01–06 tests must pass unchanged.

---

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
- `^VIX` handling;
- `PriceBar → daily_prices` rules;
- `ticker_master` upsert rules;
- `sector_etf_map` ownership and seeding;
- failure handling;
- transaction / idempotency strategy;
- DB-manager usage;
- exact metadata keys;
- allowed / forbidden files;
- testing requirements;
- assumptions / open questions.

Do not invent architecture or override higher-priority Project Files.

---

## Output

Follow Project Instructions output format.

Also include suggested commit message:

```text
module07_benchmark_etf_loader_stable
```
