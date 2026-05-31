# Module 07 Coding Prompt — Benchmark / Sector ETF Loader (v1)

Use Project Instructions and Project Files as the primary source of truth.

You are a senior Python engineer implementing the next module of a local
swing-trading stock analyzer. Work silently per Project Instructions §8.

---

## FILES ATTACHED

Attach exactly one file:

1. `stock_ai_platform_module06_stable.zip`
   - Current stable codebase after Modules 01–06.
   - Modules 01–06 are **frozen and accepted**.
   - Already contains the Module 02 DuckDB manager, Module 03 schema manager
     (which created `ticker_master`, `sector_etf_map`, `daily_prices`,
     `ticker_universe_snapshot`), Module 04 provider interface (incl. `PriceBar`,
     `PriceHistoryRequest`, `TickerInfo` DTOs), Module 05 `YahooProvider`,
     and Module 06 `UniverseSnapshotEngine`.

### Required Project Files (must be present in the Claude Project, not attached)

Source of truth in priority order:

1. `M07_BENCHMARK_ETF_LOADER_SPEC.md` — *you create this in this task* (does not
   exist yet); until it exists, items 2–11 govern.
2. `01a_CORE_PRINCIPLES.md`
3. `01b_SCHEMA_AND_DATA.md`
4. `01c_FORMULAS_AND_CONFIGS.md`
5. `01d_MODULES_AND_PIPELINE.md`
6. `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
7. `02b_ARCHITECTURE_DECISIONS.md`
8. `M02_SCHEMA_SPEC.md` — `daily_prices` §3.7, `ticker_master` §3.4,
   `sector_etf_map` §3.6
9. `M04_PROVIDER_INTERFACE_SPEC.md` — `PriceBar`, `PriceHistoryRequest`,
   `MarketDataProvider` contract (consumed, never re-implemented)
10. `M05_YAHOO_PROVIDER_SPEC.md` — accepted concrete provider behavior
11. `M06_UNIVERSE_SNAPSHOT_SPEC.md` — pattern/structure to mirror

If sources conflict, **do not guess — report the conflict** and recommend the
safest interpretation.

---

## TASK

Implement ONLY **Module 07 — Benchmark / Sector ETF Loader**.

Per `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §7, Module 07 "loads SPY, QQQ, ^VIX
and sector ETFs" and **must run before the feature engine**.

Module 07 is the producer of price history for the required benchmark symbols
defined in `app.config.constants.REQUIRED_BENCHMARK_SYMBOLS`. It also seeds and
maintains `sector_etf_map` (owned **exclusively** by this module — Module 06 was
forbidden to write it; Module 07 now does).

Module 07 accepts a caller-supplied `MarketDataProvider`, fetches price bars
through it, upserts them into `daily_prices`, upserts the benchmark symbols into
`ticker_master`, seeds `sector_etf_map`, and returns a `ServiceResult`.

---

## PUBLIC API (EXACT — do not vary)

Implement exactly this class and method. No alternative shapes (no
module-level-functions variant, no extra public methods).

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

- `provider` — a `MarketDataProvider` (Module 04 contract); Module 07 calls
  `provider.get_price_history(PriceHistoryRequest(...))` per symbol. Never
  imports `yfinance` or calls Yahoo outside the provider layer.
- `start_date` / `end_date` — inclusive range passed verbatim to
  `PriceHistoryRequest`. The caller is responsible for a valid range.
- `db_role` — `"prod"` or `"debug"` only; **never** `"simulation"`. Resolved
  only through `duckdb_manager`. Reject any other role as a `failed` result with
  no writes.
- `run_id` — mint a fresh `uuid4` when `None` (mirror Module 03 / Module 06).

Constructor: parameter-free, or accept only an optional injected
`duckdb_manager`-like hook for testing; document it in the spec. No second
public method.

---

## STRICT SCOPE — ALLOWED FILES

Add or modify only:

1. `app/services/benchmarks/__init__.py`              (new package)
2. `app/services/benchmarks/benchmark_etf_loader.py`  (new — the loader)
3. `tests/test_benchmark_etf_loader.py`               (new — tests)
4. `README.md`                                        (short Module 07 note only)
5. `M07_BENCHMARK_ETF_LOADER_SPEC.md`                 (new — spec at project
   root, NOT inside `docs/`)

*(If the established service layout in the stable zip differs from
`app/services/benchmarks/`, follow the existing layout and state the chosen path
in your design notes.)*

`requirements.txt` and `pyproject.toml` must **NOT** be modified — no new
dependencies.

---

## STRICT SCOPE — FORBIDDEN

Do NOT modify any Module 01–06 file, including (non-exhaustive):
`app/providers/*`, `app/database/*`, `app/config/*`, `app/utils/*`,
`app/services/universe/*`, `conftest.py`, `requirements.txt`, `pyproject.toml`,
`.gitignore`, `.env.example`, any `docs/*.md`, and every existing test under
`tests/`.

Do NOT:

- call `yfinance` directly or `import yfinance`; all data flows through the
  Module 04/05 `MarketDataProvider` interface;
- implement daily **stock** price ingestion — that is **Module 08**
  (Module 07 covers benchmarks/ETFs only);
- implement data validation — **Module 09**;
- implement mutation detection / `adjustment_factor` computation — **Module 10**;
- implement features, screening, proposals, outcomes, simulation, AI review, or
  dashboard logic;
- open DuckDB directly, build connection strings, or `ATTACH` arbitrary paths —
  **all** DB access goes through `app.database.duckdb_manager`;
- run `ALTER TABLE`, `CREATE TABLE`, or `CREATE TYPE` — Module 03 already created
  the schema; Module 07 only `INSERT` / `UPDATE` / `DELETE` on existing tables;
- write to `simulation.duckdb`;
- write to `ticker_universe_snapshot` — that is Module 06;
- add new DTOs, metadata keys, or error kinds to the frozen provider contract;
- hardcode tunable thresholds; read symbol/sector vocabularies from
  `app.config.constants`, never inline literals.

---

## REQUIRED BEHAVIOR (LOCKED RULES)

### Symbol set

Load exactly `constants.REQUIRED_BENCHMARK_SYMBOLS` — read it from
`app.config.constants`, do not hardcode. Current value:

```text
SPY, QQQ, ^VIX,
XLK, XLF, XLV, XLY, XLP, XLC, XLI, XLE, XLB, XLU, XLRE
```

### `symbol_type` assignment

```text
SPY, QQQ          -> symbol_type = "benchmark"   (constants.SYMBOL_TYPE_BENCHMARK)
^VIX              -> symbol_type = "index"        (constants.SYMBOL_TYPE_INDEX)
sector ETFs (XL*) -> symbol_type = "etf"          (constants.SYMBOL_TYPE_ETF)
```

Use the `constants.SYMBOL_TYPE_*` constants and the `constants.SECTOR_ETFS` /
`constants.BENCHMARK_*` membership; do not hardcode strings. State the exact
SPY/QQQ vs `^VIX` vs sector-ETF classification rule in the spec.

### `^VIX` special handling

```text
close_raw = close_adj = VIX close (from PriceBar.close_raw / close_adj)
open/high/low raw and adj take provider values when available
volume_raw = NULL
volume_adj = NULL
```

### `daily_prices` write — upsert semantics

For each `PriceBar` returned by the provider, upsert on the PK `(ticker, date)`:

```text
INSERT INTO daily_prices (...) VALUES (...)
ON CONFLICT (ticker, date) DO UPDATE SET
    open_raw, high_raw, low_raw, close_raw, volume_raw,
    open_adj, high_adj, low_adj, close_adj, volume_adj,
    dividend_amount, split_ratio, source_provider,
    data_quality_status, mutation_flag,
    updated_at = now()
```

Column mapping `PriceBar` -> `daily_prices`:

```text
ticker              = PriceBar.ticker
date                = PriceBar.date
open_raw            = PriceBar.open_raw
high_raw            = PriceBar.high_raw
low_raw             = PriceBar.low_raw
close_raw           = PriceBar.close_raw
volume_raw          = PriceBar.volume_raw   (NULL for ^VIX)
open_adj            = PriceBar.open_adj     (= open_raw for ^VIX)
high_adj            = PriceBar.high_adj     (= high_raw for ^VIX)
low_adj             = PriceBar.low_adj      (= low_raw for ^VIX)
close_adj           = PriceBar.close_adj    (= close_raw for ^VIX)
volume_adj          = NULL                  (V1: reserved, always NULL)
dividend_amount     = PriceBar.dividend_amount  (default 0 if None)
split_ratio         = PriceBar.split_ratio      (default 1 if None)
adjustment_factor   = NULL                  (Module 10 derives it; never here)
source_provider     = PriceBar.source_provider  (e.g. "yahoo")
data_quality_status = "ok"                  (initial; Module 09 owns validation)
mutation_flag       = FALSE
created_at          = now()  (on first insert)
updated_at          = now()  (on conflict update; NULL on first insert)
```

### `ticker_master` upsert

For each loaded benchmark symbol upsert on PK `ticker`:

```text
INSERT INTO ticker_master (ticker, yahoo_symbol, symbol_type,
    active_flag, delisted_flag, last_updated)
VALUES (?, ?, ?, TRUE, FALSE, now())
ON CONFLICT (ticker) DO UPDATE SET
    symbol_type  = excluded.symbol_type,
    active_flag  = TRUE,
    last_updated = now()
```

- `yahoo_symbol = ticker` (V1 identity rule, mirrors Module 06).
- Do **not** overwrite `first_seen`, `last_seen`, `company_name`, `exchange`,
  `sector`, `industry`, `security_type`, or `delisted_flag` if already set by
  Module 06.

### `sector_etf_map` seed (Module 07 is the sole owner)

Seed from `app.config.constants.SECTOR_ETF_MAP` (sector-name -> ETF-ticker dict),
insert-or-ignore so repeated runs do not error:

```text
INSERT INTO sector_etf_map (sector, etf_ticker, active_flag, created_at)
VALUES (?, ?, TRUE, now())
ON CONFLICT (sector) DO NOTHING
```

`created_at` is only set on first insert; do not update existing rows.

### Per-symbol failure handling

If `provider.get_price_history()` returns a `failed` `ServiceResult` (or zero
bars) for one symbol, **skip that symbol with a warning** and continue the
others; count it in `symbols_skipped`. Do not abort the whole run. If **every**
symbol fails/returns empty, return `success_with_warnings` with
`symbols_loaded == 0` and no price rows (the DB is still available). Return
`failed` only when `db_role` is invalid or the DB itself is unavailable.

### Idempotency

`daily_prices` and `ticker_master` use `ON CONFLICT DO UPDATE`; `sector_etf_map`
uses `ON CONFLICT DO NOTHING`. Re-running the same date range is safe and
produces no duplicates. Wrap each symbol's writes (or the whole run) in a
transaction; on a write error roll back so no partial/orphaned rows survive, and
document the chosen transaction granularity in the spec.

---

## ServiceResult (EXACT metadata keys)

Return `app.utils.service_result.ServiceResult`. `status ∈ {success,
success_with_warnings, failed}`. `rows_processed` = total `daily_prices` rows
upserted across all symbols (= `price_rows_written`). `metadata` must contain
**exactly** these keys (present on every return path, including guard failure):

```text
db_role                 # "prod" | "debug" (echoes the rejected value on guard failure)
start_date              # ISO date string
end_date                # ISO date string
symbols_requested       # len(REQUIRED_BENCHMARK_SYMBOLS)
symbols_loaded          # symbols with >= 1 bar written
symbols_skipped         # symbols where provider failed or returned 0 bars
price_rows_written      # total daily_prices rows upserted
ticker_master_upserted  # rows upserted in ticker_master
sector_etf_map_seeded   # rows newly inserted in sector_etf_map (0 if already seeded)
```

Log start / end / per-symbol counts / warnings / errors via the bound-`run_id`
logger (`logging_config.get_logger(__name__, run_id)`). No `print()`. Do not
raise for expected conditions; return a `ServiceResult`.

---

## REQUIRED TESTS

Create `tests/test_benchmark_etf_loader.py`. Tests run **fully offline** — no
network, no live provider — and must **never** touch real prod / debug /
simulation DB files. Redirect DuckDB paths into pytest `tmp_path` via
`monkeypatch.setattr(settings, ...)` and apply the real Module 03 schema to that
temp DB in a fixture (mirror `tests/test_schema_manager.py`). Inject a fake
`MarketDataProvider` that returns deterministic `PriceBar` lists.

Cover at minimum:

1. **Import smoke**: `BenchmarkEtfLoader` imports; `load` present with the exact
   signature.
2. **Fresh load**: fake provider returns N bars per symbol → `success`; correct
   `daily_prices` row count; all symbols in `ticker_master`; `sector_etf_map`
   seeded; metadata counts correct.
3. **Idempotency**: `load` twice on the same range → no `daily_prices`
   duplicates; `sector_etf_map` unchanged; `sector_etf_map_seeded == 0` on the
   second run.
4. **`^VIX` handling**: `volume_raw` NULL and `volume_adj` NULL;
   `close_raw == close_adj`; `symbol_type == "index"` in `ticker_master`.
5. **`symbol_type` assignment**: SPY/QQQ → `"benchmark"`; every `XL*` → `"etf"`;
   `^VIX` → `"index"`.
6. **Per-symbol provider failure**: provider returns `failed` for one symbol →
   skipped with warning, rest loaded, status `success_with_warnings`,
   `symbols_skipped == 1`.
7. **All-symbols failure / empty**: every symbol fails or returns 0 bars →
   `success_with_warnings`, `symbols_loaded == 0`, no `daily_prices` rows.
8. **`db_role` guard**: `db_role="simulation"` (and any invalid role) →
   `failed`, no writes.
9. **`sector_etf_map` content**: exact sector→ETF pairs from
   `constants.SECTOR_ETF_MAP`; not duplicated on re-run.
10. **`ticker_master` upsert**: benchmark symbols present with correct
    `symbol_type`, `active_flag = TRUE`, `yahoo_symbol == ticker`; pre-existing
    Module-06 fields (`first_seen` etc.) not clobbered.
11. **Transaction rollback**: a forced mid-write failure leaves no partial /
    orphaned `daily_prices` rows.
12. **`adjustment_factor` NULL**: every written `daily_prices` row has
    `adjustment_factor = NULL`.
13. **`data_quality_status = "ok"`**: every written row carries the initial
    status.
14. **Static scan**: module does not `import duckdb`, does not `import
    yfinance`, has no `duckdb.connect(`, no `ATTACH`, no `ALTER TABLE` /
    `CREATE TABLE` / `CREATE TYPE`, and no `print(` (literal-vs-prose token scan
    mirroring `tests/test_schema_manager.py` / `tests/test_provider_interface.py`).
15. **ServiceResult contract**: real `ServiceResult`, valid status, and the
    **exact** metadata key set on every return path (including guard failure).
16. **`volume_adj` NULL**: every `daily_prices` row has `volume_adj = NULL`.

Existing Module 01–06 tests must continue to pass unchanged.

---

## MODULE-SPECIFIC SOURCE OF TRUTH

Create **`M07_BENCHMARK_ETF_LOADER_SPEC.md`** (separate output file, project
root, not inside `docs/`). Derive it from the Project Files above, this task, and
the accepted implementation. Do **not** invent new architecture or override
higher-priority docs; report any conflict instead.

Keep it concise and implementation-oriented. Include: purpose; scope / non-scope;
source-of-truth priority; the exact public API
(`BenchmarkEtfLoader.load` signature); the symbol set and `symbol_type`
assignment rules; the `^VIX` special-handling rule; the `PriceBar →
daily_prices` upsert mapping incl. `adjustment_factor`/`volume_adj` = NULL and
`data_quality_status = "ok"`; the `ticker_master` upsert rules incl. the
non-clobber rule for Module-06 fields; the `sector_etf_map` seed rules and sole
ownership; per-symbol failure handling; the transactional idempotency strategy;
how the DB manager is used without opening arbitrary paths; the exact
`ServiceResult` metadata keys; allowed/forbidden files; testing requirements; an
acceptance checklist; and assumptions / open questions.

---

## OUTPUT REQUIRED (per Project Instructions §8)

1. Updated project zip. Top-level folder `stock_ai_platform/`; preserve the
   Module 06 layout.
2. List of added/changed files.
3. `M07_BENCHMARK_ETF_LOADER_SPEC.md` as a separate downloadable file.
4. Short design notes (symbol-type assignment, `^VIX` handling, upsert strategy,
   `sector_etf_map` ownership, how the DB manager is used without opening
   arbitrary paths, how tests stay offline + isolated, how frozen Modules 01–06
   are protected).
5. Test command and full results:
   ```bash
   pytest -q
   ```
   If the environment cannot install `duckdb` / `polars`, say so clearly and
   list the isolated/static checks performed, plus
   `pytest -q tests/test_benchmark_etf_loader.py`.
6. Any assumptions (do not hide them; do not assume where a doc is explicit).
7. Suggested commit message:
   ```text
   module07_benchmark_etf_loader_stable
   ```

---

## STARTING STEPS

Read in this order:
1. `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §7 (Module 07), §6 (pipeline step 3
   "Load benchmarks and sector ETFs"), and the §22.10 benchmark decision.
2. `M02_SCHEMA_SPEC.md` §3.7 `daily_prices`, §3.4 `ticker_master`, §3.6
   `sector_etf_map`, and the `symbol_type` / `data_quality_status` value catalogs.
3. `app/config/constants.py` (`REQUIRED_BENCHMARK_SYMBOLS`, `SECTOR_ETFS`,
   `SECTOR_ETF_MAP`, `BENCHMARK_*`, `SYMBOL_TYPE_*`).
4. `app/database/duckdb_manager.py` (the only DB entry point) and
   `app/database/schema_manager.py` (the `run_id` + `ServiceResult` + transaction
   pattern to mirror).
5. `app/providers/provider_interface.py` (`MarketDataProvider`,
   `PriceHistoryRequest`, `PriceBar`) — consume, do not modify.
6. `app/services/universe/universe_snapshot.py` (Module 06 — the accepted
   service structure, db-role guard, and static-scan discipline to mirror).
7. `tests/test_schema_manager.py` and `tests/test_universe_snapshot.py` (the
   `tmp_path` + `monkeypatch.setattr(settings)` isolation fixture and the
   static-scan discipline).

Then implement Module 07.

Do not reopen the Module 03 schema or the Module 04 contract.
Do not implement Module 08 or later.
Do not modify any Module 01–06 file or any `docs/*.md`.
Do not call Yahoo outside the provider layer; do not open DuckDB outside the
manager.
