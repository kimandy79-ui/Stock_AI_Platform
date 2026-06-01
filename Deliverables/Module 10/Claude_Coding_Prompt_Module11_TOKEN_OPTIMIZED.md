# Module 11 Coding Prompt — Feature Engine (Token-Optimized)

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not rely on archived monolithic source files or old FULL/PATCH/MINI PATCH files if split Project Files are available.
Do not repeat or summarize global rules.
Work silently and return only actionable results.

## Attached

1. `stock_ai_platform_module10_accepted.zip` or latest accepted Module 10 baseline
2. `M11_FEATURE_ENGINE_SPEC.md`, if already available

Current codebase:
Modules 01–10 are accepted and frozen. Use the accepted Module 10 zip as the implementation base.

## Task

Implement only **Module 11 — Feature Engine**.

Module 11 runs after Module 10 and before Module 12. It must:

- read eligible `daily_prices` rows where `data_quality_status = 'ok'`;
- compute `daily_features` strictly from formulas and rules in Project Files;
- upsert into `daily_features` on `(ticker, feature_date, feature_schema_version)`;
- set `feature_ready = TRUE` only when all required indicators are available;
- return `ServiceResult`.

Do not implement Module 12 or later.

## Source retrieval hints

Retrieve only what is needed:

- formulas/configs → `01c_FORMULAS_AND_CONFIGS.md`
- `daily_features` schema → `01b_SCHEMA_AND_DATA.md`
- Module 11 boundary / pipeline position → `01d_MODULES_AND_PIPELINE.md`
- coding, logging, testing, performance → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- Polars-first, no-lookahead, feature cutoff, raw/adjusted, schema-version decisions → `02b_ARCHITECTURE_DECISIONS.md`
- constants → `app/config/constants.py` (`FEATURE_SCHEMA_VERSION`, `SECTOR_ETF_MAP`, VIX thresholds, `MARKET_REGIME_PRIORITY`)

If any formula, threshold, source table, enum, or input column is missing from frozen Project Files, do not invent it. Mark it as an open gap or blocker in `M11_FEATURE_ENGINE_SPEC.md` and implement only the explicitly supported subset.

## Required pre-coding spec update

Create or update `M11_FEATURE_ENGINE_SPEC.md` before coding with a concise formula-to-column mapping table.

For each `daily_features` column, record:

- schema column and type;
- formula/source reference;
- input tables/columns;
- lookback window, if any;
- null/default behavior for missing inputs or insufficient history;
- status: implemented / open gap / blocker.

For technical columns such as keys, schema version, timestamps, and flags, document the source/default instead of inventing a formula.

## Public API

Mirror the Module 10 service style unless higher-priority Project Files define otherwise.

```text
FeatureEngine(db_manager=None)
    .calculate(
        start_date: date,
        end_date: date,
        tickers: list[str] | None = None,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult
```

Rules:

- `tickers=None` processes all distinct tickers with eligible `daily_prices` rows in `[start_date, end_date]`.
- Explicit `tickers` processes only requested tickers; requested tickers with no eligible rows are counted as skipped.
- `db_role` accepts only `prod` and `debug`; `simulation` and other values fail before any DB read/write.
- `start_date > end_date` fails before any DB read/write.
- Mint a fresh `uuid4` when `run_id is None`.
- `ServiceResult.rows_processed == metadata["tickers_processed"]` on every return path.

## Exact `ServiceResult.metadata` keys

`metadata` must contain exactly these keys on every return path:

```text
db_role
start_date
end_date
tickers_requested
tickers_processed
tickers_skipped_no_data
rows_read
feature_rows_written
feature_rows_updated
feature_ready_count
feature_not_ready_count
```

Definitions:

- `tickers_requested`: `len(tickers)`, or `0` when `tickers=None` means all eligible tickers.
- `tickers_processed`: distinct tickers for which at least one feature row was attempted.
- `tickers_skipped_no_data`: requested tickers with no eligible rows; `0` when `tickers=None`.
- `rows_read`: eligible source rows read, including warmup and benchmark/ETF rows needed for calculations.
- `feature_rows_written`: net-new `daily_features` rows inserted.
- `feature_rows_updated`: existing `daily_features` rows updated through upsert.
- `feature_ready_count` / `feature_not_ready_count`: rows written/updated by this run by readiness status.

On guard/read failure, durable write counts are `0`. On write failure, rollback is mandatory; read/compute counts may remain accurate, but durable write counts must be `0`.

## Scope and ownership

Expected additions / changes only:

```text
app/services/features/__init__.py
app/services/features/feature_engine.py
tests/test_feature_engine.py
M11_FEATURE_ENGINE_SPEC.md
README.md                          # short Module 11 note only
```

Do not modify frozen Modules 01–10 unless required by a real integration blocker. Explain and keep any such change minimal.

Module 11 may write only `daily_features` via upsert, including `calculated_at` refresh.

Module 11 must not write `daily_prices`, ticker/universe/sector tables, repair/rebuild tables, simulation tables, or any step/proposal/outcome/AI/execution table.

Do not call providers, import `duckdb`, use `ATTACH`, run DDL, modify schema, bypass the DuckDB manager, or use `print()` in library code.

## Locked behavior

### Data-quality boundary

Only `daily_prices` rows with `data_quality_status = 'ok'` may contribute to features, benchmark/ETF calculations, market regime, earnings, or macro context.

### Polars-first

Use Polars for rolling, grouped, join, and vectorized calculations. Avoid ticker-by-ticker Python loops for indicators. Small orchestration/upsert batching loops are acceptable. Pandas only when unavoidable.

### Date range, warmup, and no look-ahead

- `feature_cutoff_date` is the latest eligible `daily_prices.date` for each ticker within `[start_date, end_date]`.
- `feature_date = feature_cutoff_date`.
- Read enough warmup before `start_date` for the longest required lookback, including 252-trading-day windows.
- Never use any row after that ticker's `feature_cutoff_date`.
- Never write a `daily_features` row outside the requested range.

### Price and volume rules

- Adjusted prices (`close_adj`, `high_adj`, `low_adj`) drive price indicators.
- Volume features use `volume_raw`.
- `avg_dollar_volume_20d = mean(close_raw * volume_raw)` unless Project Files explicitly say otherwise.
- `volume_adj` is reserved and unused in V1.

### Required vs optional readiness

`feature_ready = TRUE` only when all required indicators are non-null:

```text
ema20, ema50, ema200, ema_alignment_score,
rsi14, roc20, atr14, atr_pct,
rvol20, avg_volume_20d, avg_dollar_volume_20d,
distance_from_52w_high_pct, pullback_from_recent_high_pct,
breakout_proximity, consolidation_score
```

Optional/context columns do not block readiness:

```text
sector_relative_strength, market_regime,
days_to_earnings_bd, earnings_confidence, macro_event_risk_flag
```

If Project Files define a different list, follow them and document the difference.

### Sector relative strength

Use `SECTOR_ETF_MAP`. Compute `ticker_20d_return_adj - sector_etf_20d_return_adj` from eligible `close_adj` rows. If sector/ETF data is unavailable or insufficient, set `sector_relative_strength = NULL` and continue. Do not create or modify `sector_etf_map`.

### Market regime boundary

Only compute the inline `daily_features.market_regime` value if the formula is explicitly defined. Use already-ingested SPY, QQQ, and `^VIX` rows plus constants. Do not implement standalone Module 12 or write market-regime tables.

If the exact formula is missing, mark an open gap instead of inventing behavior.

### Earnings and macro fallback

Read `earnings_calendar` and `macro_events_calendar` only if they exist in the frozen schema. If absent/empty/insufficient, default to:

```text
days_to_earnings_bd = NULL
earnings_confidence = NULL
macro_event_risk_flag = FALSE
```

### Upsert / idempotency

Use `constants.FEATURE_SCHEMA_VERSION`. Upsert with:

```sql
INSERT ... ON CONFLICT (ticker, feature_date, feature_schema_version) DO UPDATE SET ...
```

Preserve `created_at` on conflict, refresh `calculated_at` on every upsert, and keep reruns stable with no duplicates. Count inserts vs conflict updates separately.

### Transaction model

Use separate phases:

1. read source rows through the DB manager and close the read connection;
2. compute features in Polars with no DB writes;
3. upsert all `daily_features` rows in one transaction.

On write error, rollback and return `failed` with no partial Module 11 writes.

## Required tests

Create `tests/test_feature_engine.py`. Tests must be offline and use temporary DuckDB paths.

Cover:

- public API/signature, `run_id`, exact metadata keys, and `rows_processed == tickers_processed`;
- guards: invalid/`simulation` `db_role`, invalid date range, no DB access before guard failure;
- ticker selection: `tickers=None`, explicit list, and skipped no-data tickers;
- data-quality filter, warmup read, no-lookahead, and requested-range write boundary;
- feature readiness for sufficient vs insufficient history;
- deterministic formula checks for EMA, RSI14, ATR14, RVOL20, ROC20, 52-week/longest-lookback behavior, and `consolidation_score` clamp;
- sector relative strength when data exists and NULL fallback when unavailable;
- market-regime calculation only where formula is defined;
- earnings/macro fallback defaults;
- upsert idempotency, `created_at` preservation, and `calculated_at` refresh;
- write ownership: only `daily_features` changes; forbidden tables untouched;
- rollback leaves no partial feature rows;
- no provider/vendor/network usage;
- static scans: no direct `duckdb`, `ATTACH`, DDL/schema changes, provider imports, or `print()`;
- existing Module 01–10 tests pass unchanged.

## Module-specific source of truth

Create or update `M11_FEATURE_ENGINE_SPEC.md`. Keep it concise and implementation-oriented. Include:

- purpose and non-scope;
- source references;
- exact public API;
- exact metadata keys and definitions;
- formula-to-column mapping table;
- date range, warmup, and `feature_cutoff_date` rules;
- required vs optional readiness columns;
- price/volume rules;
- sector relative strength;
- inline market-regime note and Module 12 boundary;
- earnings/macro fallback;
- Polars strategy;
- upsert/idempotency and transaction model;
- DB-manager usage;
- tests;
- assumptions, open gaps, and blockers.

Do not invent architecture or override higher-priority Project Files.

## Output

Return only:

- updated zip;
- added / changed files;
- `M11_FEATURE_ENGINE_SPEC.md`;
- short design notes;
- test commands and results;
- open gaps and blockers;
- suggested commit message: `module11_feature_engine_stable`.
