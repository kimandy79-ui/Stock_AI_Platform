# M07 — Benchmark / Sector ETF Loader — Module Spec

Status: accepted (Module 07). Concise, implementation-oriented source of truth
for the benchmark / sector-ETF loader. Derived from the Module 07 task,
`01a_CORE_PRINCIPLES.md`, `01b_SCHEMA_AND_DATA.md`, `01c_FORMULAS_AND_CONFIGS.md`,
`01d_MODULES_AND_PIPELINE.md`, `02_PROJECT_IMPLEMENTATION_CONTEXT.md`,
`02b_ARCHITECTURE_DECISIONS.md`, `M02_SCHEMA_SPEC.md`,
`M04_PROVIDER_INTERFACE_SPEC.md`, `M05_YAHOO_PROVIDER_SPEC.md`, and
`M06_UNIVERSE_SNAPSHOT_SPEC.md` (structure mirrored). This spec introduces no
new architecture and overrides no higher-priority document.

## 1. Purpose

Load benchmark, index, and sector-ETF daily price history **before** the feature
engine so later modules can compute market regime (SPY/QQQ/`^VIX`) and sector
relative strength (sector SPDRs). Module 07 produces the benchmark/index/ETF rows
in `daily_prices`, registers those symbols in `ticker_master`, and is the **sole
owner** of `sector_etf_map`.

## 2. Scope / non-scope

In scope: load exactly `constants.REQUIRED_BENCHMARK_SYMBOLS`; fetch bars only
through the Module 04 `MarketDataProvider` interface; upsert bars into
`daily_prices`; upsert each loaded symbol into `ticker_master` without clobbering
Module-06-owned fields; seed `sector_etf_map` (insert-or-ignore); return a
`ServiceResult`.

Out of scope (owned elsewhere): calling Yahoo/`yfinance` or any vendor directly
(Modules 04/05); universe construction or any write to `ticker_universe_snapshot`
(Module 06); stock price ingestion (Module 08); validation / mutation detection
(Module 09); `adjustment_factor` derivation (Module 10); features, screening,
proposals, outcomes, simulation, AI review, dashboard. Module 07 never opens
DuckDB directly or `ATTACH`es, never runs DDL, and never writes to
`simulation.duckdb`.

## 3. Source-of-truth priority

1. This file (`M07_BENCHMARK_ETF_LOADER_SPEC.md`).
2. `01a_CORE_PRINCIPLES.md` / `01b_SCHEMA_AND_DATA.md` / `01c_FORMULAS_AND_CONFIGS.md` / `01d_MODULES_AND_PIPELINE.md`.
3. `02_PROJECT_IMPLEMENTATION_CONTEXT.md` / `02b_ARCHITECTURE_DECISIONS.md`.
4. `M02_SCHEMA_SPEC.md` (`daily_prices`, `ticker_master`, `sector_etf_map`).
5. `M04_PROVIDER_INTERFACE_SPEC.md` (`MarketDataProvider`, `PriceHistoryRequest`,
   `PriceBar` — consumed, never re-implemented).
6. `M05_YAHOO_PROVIDER_SPEC.md` / `M06_UNIVERSE_SNAPSHOT_SPEC.md`.

On conflict: do not guess — report and recommend the safest interpretation. Two
wording differences were reconciled in favor of the task's locked behavior (more
specific): `01d` says benchmark ETFs may be `benchmark or etf` and `^VIX` volume
`NULL or 0` — Module 07 uses SPY/QQQ → `benchmark`, sector SPDRs → `etf`, `^VIX`
volume → `NULL`. No schema conflicts: the frozen `schema_manager.py` DDL matches
`M02_SCHEMA_SPEC.md`.

## 4. Public API (exact)

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

Exactly one public method. Constructor is parameter-free except an optional
`db_manager` hook used only for test injection (`BenchmarkEtfLoader(db_manager=...)`);
when omitted, the real `app.database.duckdb_manager` is used. No module-level
function variant, no extra public methods.

- `provider` — Module 04 `MarketDataProvider`; bars fetched only via
  `provider.get_price_history(PriceHistoryRequest(...))`.
- `start_date` / `end_date` — inclusive `[start_date, end_date]` range per
  request.
- `db_role` — `"prod"` or `"debug"` only, resolved only through `duckdb_manager`;
  `"simulation"` and any other value yield a `failed` result with no writes.
- `run_id` — a fresh `uuid4` is minted when `None`.

## 5. Symbol set and classification

Symbols are read from `constants.REQUIRED_BENCHMARK_SYMBOLS` (never hardcoded).
Classification uses constants for membership:

```
^VIX (constants.BENCHMARK_VIX)              -> SYMBOL_TYPE_INDEX
SPY, QQQ (BENCHMARK_SPY, BENCHMARK_QQQ)     -> SYMBOL_TYPE_BENCHMARK
sector SPDRs (constants.SECTOR_ETFS)        -> SYMBOL_TYPE_ETF
```

The classified `symbol_type` is passed both to the `PriceHistoryRequest` and to
the `ticker_master` upsert. Benchmarks/index/ETFs are excluded from screening
downstream (they are not `stock`).

## 6. `^VIX` handling (locked)

When writing a `^VIX` bar to `daily_prices`:

```
close_raw            = bar.close_adj      # raw mirrors adjusted for the index
open_raw/high_raw/low_raw  = provider raw values (when available)
open_adj/high_adj/low_adj/close_adj = provider adjusted values
volume_raw           = NULL
volume_adj           = NULL
```

All other symbols use the provider's `close_raw` / `volume_raw` verbatim.

## 7. `PriceBar` → `daily_prices` (upsert by `(ticker, date)`)

```
ticker               = loaded ticker
date                 = bar.date
open_raw/high_raw/low_raw = bar.* (raw)
close_raw            = bar.close_raw   (^VIX: bar.close_adj)
volume_raw           = bar.volume_raw  (^VIX: NULL)
open_adj/high_adj/low_adj/close_adj = bar.* (adjusted)
volume_adj           = NULL
dividend_amount      = bar.dividend_amount if not None else 0
split_ratio          = bar.split_ratio if not None else 1
adjustment_factor    = NULL            # Module 10 owns derivation
source_provider      = bar.source_provider
data_quality_status  = "ok"            # Module 09 owns real validation
mutation_flag        = FALSE
created_at           = now()           # on INSERT only
updated_at           = NULL on INSERT; now() on CONFLICT update
```

Upsert is `INSERT ... ON CONFLICT (ticker, date) DO UPDATE SET ...`; `created_at`
is preserved on conflict, `updated_at` refreshed.

## 8. `ticker_master` upsert (non-clobbering)

For every **loaded** symbol (≥1 bar):

```
New row (ticker absent):
    yahoo_symbol  = ticker
    symbol_type   = classified type
    active_flag   = TRUE
    delisted_flag = FALSE
    last_updated  = now()
    company_name / exchange / sector / industry / security_type /
        first_seen / last_seen = left NULL (Module 06 authors these)

Existing row:
    yahoo_symbol  = ticker
    symbol_type   = classified type
    active_flag   = TRUE
    last_updated  = now()
    first_seen / last_seen / company_name / exchange / sector / industry /
        security_type / delisted_flag = UNCHANGED (not clobbered)
```

A symbol that returns zero bars or a provider failure is skipped and is **not**
upserted into `ticker_master`.

## 9. `sector_etf_map` ownership and seeding

Module 07 is the sole owner. Seeded from `constants.SECTOR_ETF_MAP` using
**SQL-level** insert-or-ignore (no application-side read-then-write window):

```sql
INSERT INTO sector_etf_map (sector, etf_ticker, active_flag, created_at)
VALUES (?, ?, TRUE, CAST(now() AS TIMESTAMP))
ON CONFLICT (sector) DO NOTHING
RETURNING sector;
```

`ON CONFLICT (sector) DO NOTHING` is atomic at the DB layer, so concurrent or
repeated execution cannot race; existing rows are never updated. `RETURNING
sector` yields one row per actual insert and zero rows per conflict, and
`sector_etf_map_seeded` counts those returned rows. Seeding is constant-driven
and runs even when all provider fetches fail (it does not depend on price data),
but only when `db_role` is valid **and** the date range is valid.

## 10. Failure handling

```
Invalid db_role (incl. "simulation")  -> failed, no writes, no seeding,
                                         no provider calls
Invalid date range (start_date >       -> failed, no writes, no seeding,
  end_date)                              no provider calls
Allowed db_role                        -> "prod" / "debug" only
Per-symbol provider raised exception   -> warning, skip symbol, continue
Per-symbol provider status == failed   -> warning, skip symbol, continue
Per-symbol missing metadata['bars']    -> warning (contract violation), skip,
  (or value is None)                     continue
Per-symbol zero bars (key present)     -> warning, skip symbol, continue
Per-symbol success_with_warnings +     -> bars loaded; each provider warning
  valid bars                             propagated with ticker context
All symbols fail / empty / contract-   -> success_with_warnings,
  violating                              symbols_loaded == 0, no price rows
                                         (sector_etf_map still seeded)
DB connect / write failure             -> failed (transaction rolled back)
```

Status: `failed` for invalid `db_role`, invalid range, or any DB error;
`success_with_warnings` when any symbol is skipped **or** any provider warning
was propagated; `success` only when every requested symbol loads with no
warnings (provider-side or loader-side).

Per-symbol outcomes (load, skip, contract violation, exception) are also logged
through the bound `RunIdLoggerAdapter` so production runs can be diagnosed
without parsing the returned `warnings` list.

## 11. Transaction / idempotency strategy

A single transaction wraps the **entire** DB write phase (all provider fetches
happen first, with no DB writes):

```
BEGIN TRANSACTION;
  -- (1) sector_etf_map insert-or-ignore (constant-driven)
  -- (2) ticker_master upsert for each loaded symbol
  -- (3) daily_prices upsert for each loaded bar
COMMIT;
```

On any error inside the transaction the loader issues `ROLLBACK` and returns a
`failed` `ServiceResult` with no partial / orphaned rows (no half-seeded sector
map, no half-written prices, no stray master rows). Re-running the same range is
idempotent: `daily_prices` upserts by `(ticker, date)` (no duplicates;
`updated_at` advances), `ticker_master` re-upserts safely, and `sector_etf_map`
re-seeding is a no-op.

## 12. DB-manager usage

All DB access is via `app.database.duckdb_manager.connect(db_role)` (or an
injected manager-like object in tests). Module 07 never imports `duckdb`
directly, never opens a path, never `ATTACH`es, and runs no DDL.

## 13. `ServiceResult` (exact metadata keys)

`rows_processed = price_rows_written`. `metadata` carries exactly these keys on
**every** return path (guard failure, write failure, success):

```
db_role
start_date              # ISO string
end_date                # ISO string
symbols_requested
symbols_loaded
symbols_skipped
price_rows_written
ticker_master_upserted
sector_etf_map_seeded
```

Logging uses the project `RunIdLoggerAdapter` with the bound `run_id`; no
`print()`.

## 14. Allowed / forbidden files

Allowed (created/changed by Module 07):

```
app/services/benchmarks/__init__.py
app/services/benchmarks/benchmark_etf_loader.py
tests/test_benchmark_etf_loader.py
README.md                              # short Module 07 note only
M07_BENCHMARK_ETF_LOADER_SPEC.md       # project root
```

Forbidden: modifying frozen Modules 01–06 (except a real integration blocker —
none required); provider contracts, DB manager, schema manager, config
constants, dependencies, existing tests, or `docs/*.md`; adding dependencies;
calling Yahoo/`yfinance`; bypassing the provider interface; opening DuckDB
directly or bypassing the manager; running schema DDL; writing to
`simulation.duckdb` or `ticker_universe_snapshot`; implementing Module 08+ logic.

## 15. Testing requirements

`tests/test_benchmark_etf_loader.py`, fully offline and isolated (temp DuckDB
paths via the `tmp_db_paths` fixture; an in-test `MarketDataProvider` fake).
Covers: import + exact signature; fresh load; idempotency; `^VIX` handling;
symbol-type assignment; per-symbol failure (failed / empty / raised);
all-symbol failure / empty; invalid `db_role` / `"simulation"` guard;
**invalid date range fails early (no provider calls, no DB writes, no
seeding)**; `sector_etf_map` content, idempotency, **SQL-level
`ON CONFLICT DO NOTHING RETURNING` clause**, and no-update-existing under SQL
conflict; `ticker_master` non-clobbering; transaction rollback;
`adjustment_factor` NULL; `volume_adj` NULL; `data_quality_status == "ok"`;
dividend/split defaults; **provider `success_with_warnings` warnings propagated
with ticker context and bars still loaded**; **missing `metadata['bars']` is
treated as a contract violation (distinct warning, symbol skipped)**; static
scan for forbidden imports / direct DB / DDL / snapshot write / `print`; exact
`ServiceResult` metadata keys on success, guard-failure, range-failure, and
write-failure paths; request symbol-type classification. Module 01–06 tests
pass unchanged.

## 16. Assumptions / open questions

- **A1.** `sector_etf_map` seeding is constant-driven and therefore runs even
  when all provider fetches fail (it has no dependency on price data), as long
  as `db_role` **and** the date range are valid. The "no price rows" rule for
  all-fail applies to `daily_prices`, not to the constant sector map.
- **A2.** Provider-construction or call exceptions for a single symbol degrade
  to a per-symbol skip + warning. An inverted date range
  (`start_date > end_date`) is treated separately as a programmer error and
  fails the whole call early — before any provider call or DB write.
- **A3.** On a fresh `ticker_master` insert for a benchmark symbol, `first_seen`
  / `last_seen` and descriptive fields are left NULL; Module 07 does not author
  universe lifecycle dating (Module 06 territory). Benchmarks are not part of the
  stock universe, so Module 06 is not expected to populate them.
- **A4.** `start_date` / `end_date` are echoed in metadata as ISO strings
  (mirrors Module 06's `snapshot_month`).
- **A5.** `ticker_master_upserted` counts inserts + updates for loaded symbols;
  `sector_etf_map_seeded` counts only newly inserted sector rows (the count of
  rows returned by `INSERT ... ON CONFLICT DO NOTHING RETURNING sector`).
- **A6.** A provider that returns `status == success` without a `metadata['bars']`
  key (or with `metadata['bars'] is None`) is treated as a provider-contract
  violation: a distinct warning is emitted, the symbol is skipped, and the run
  continues. This differs from a legitimate empty range (key present, list
  empty), which is also skipped but warned with a different message.
- **A7.** Provider `success_with_warnings` results with valid bars still load
  the bars; every provider-side warning is propagated into the loader's
  `warnings` list prefixed with the ticker.
