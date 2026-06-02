# Module 12 Coding Prompt — Market Regime Engine

Use Project Instructions and Project Files.  
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.  
Do not repeat or summarize global rules.  
Work silently and return only actionable results.

## Attached

1. `stock_ai_platform_module11_stable.zip` — accepted Module 11 baseline and implementation base.
2. `M12_MARKET_REGIME_ENGINE_SPEC.md`, if already available.

Current codebase: Modules 01–11 are accepted and frozen.

## Task

Implement only **Module 12 — Market Regime Engine**.

Module 12 runs after Module 11 Feature Engine and before Module 13 Step 3 Screening. It must:

- read eligible `daily_prices` rows for `SPY`, `QQQ`, and `^VIX` where `data_quality_status = 'ok'`;
- classify one `market_regime` value per requested calendar date using the frozen enum: `extreme_risk`, `high_risk`, `bear`, `bull`, `neutral`;
- apply `constants.MARKET_REGIME_PRIORITY` top-down; first matching condition wins;
- update existing `daily_features.market_regime` rows for the requested date range and `constants.FEATURE_SCHEMA_VERSION` only;
- return `ServiceResult`.

Do not implement Module 13 or later.

## Source retrieval hints

Retrieve only what is needed:

- regime enum / guardrails → `01a_CORE_PRINCIPLES.md`
- `daily_prices`, `daily_features` schema → `01b_SCHEMA_AND_DATA.md`
- Module 12 pipeline position → `01d_MODULES_AND_PIPELINE.md`
- coding/testing/logging/performance → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- market-regime / Polars / no-lookahead decisions → `02b_ARCHITECTURE_DECISIONS.md`
- constants → `app/config/constants.py` (`VIX_*`, `MARKET_REGIME_PRIORITY`, `FEATURE_SCHEMA_VERSION`)
- Module 11 market-regime gap, if present → `M11_FEATURE_ENGINE_SPEC.md`

## Mandatory formula decision

Project Files define VIX thresholds, priority order, symbols, and enum values, but the exact SPY/QQQ rule for `bear` / `bull` / `neutral` may be incomplete.

Unless higher-priority Project Files contradict it, document and implement this minimal architectural decision in `M12_MARKET_REGIME_ENGINE_SPEC.md`:

```text
For each requested calendar date d:
  Use latest eligible rows for SPY, QQQ, and ^VIX with daily_prices.date <= d.

Priority gates:
  if ^VIX close >= VIX_EXTREME_RISK_THRESHOLD -> extreme_risk
  if ^VIX close >= VIX_HIGH_RISK_THRESHOLD    -> high_risk

Trend classification, only if no VIX gate fires:
  if SPY close_adj > SPY EMA200                                  -> bull
  if SPY close_adj < SPY EMA200 and QQQ close_adj < QQQ EMA200   -> bear
  otherwise                                                       -> neutral
```

Rules:

- Use `close_adj` when available; for `^VIX`, fall back to `close_raw` if needed and document it.
- EMA200 is the standard recursive EMA with `span=200`, `adjust=False`, matching Module 11 EMA behavior.
- Minimum EMA200 history: 200 eligible rows per symbol.
- If VIX is unavailable for date `d`, skip VIX gates and classify by trend only.
- If SPY has no eligible row on or before `d`, skip the date.
- If SPY exists but EMA200 is unavailable, classify as `neutral` and warn.
- If QQQ or QQQ EMA200 is unavailable, `bear` cannot fire; classify as `neutral` unless `bull` fires.
- If Project Files define a contradictory rule, follow Project Files and document the deviation.

## Public API

Mirror the Module 10 / Module 11 service style:

```text
MarketRegimeEngine(db_manager=None)
    .classify(
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult
```

Rules:

- `db_role` accepts only `prod` and `debug`; `simulation` and all other values fail before DB access.
- `start_date > end_date` fails before DB access.
- Mint `uuid4` when `run_id is None`.
- `ServiceResult.rows_processed == metadata["dates_classified"]` on every return path.

## Exact metadata keys

`ServiceResult.metadata` must contain exactly these keys on every return path:

```text
db_role
start_date
end_date
dates_requested
dates_classified
dates_skipped_insufficient_data
rows_read
feature_rows_updated
regimes_by_value
```

Definitions:

- `dates_requested`: calendar days in `[start_date, end_date]`.
- `dates_classified`: dates with a computed regime, even if no matching `daily_features` row exists.
- `dates_skipped_insufficient_data`: dates skipped because SPY had no eligible row on or before that date.
- `rows_read`: eligible SPY/QQQ/^VIX `daily_prices` rows read, including warmup.
- `feature_rows_updated`: actual existing `daily_features` rows updated.
- `regimes_by_value`: `dict[str, int]` count of classified dates by regime.

On guard/read failure, durable write counts are `0`. On write failure, rollback is mandatory and `feature_rows_updated = 0`.

## Scope and ownership

Expected additions / changes only:

```text
app/services/regime/__init__.py
app/services/regime/market_regime_engine.py
tests/test_market_regime_engine.py
M12_MARKET_REGIME_ENGINE_SPEC.md
README.md                            # short Module 12 note only
```

Do not modify frozen Modules 01–11 unless required by a real integration blocker.

Module 12 may only update:

```text
daily_features.market_regime
daily_features.calculated_at
```

Only update rows where:

```text
feature_date in [start_date, end_date]
feature_schema_version = constants.FEATURE_SCHEMA_VERSION
```

Do not insert `daily_features` rows. Do not write `daily_prices`, ticker/universe/sector/repair/rebuild/simulation/step/proposal/outcome/AI/execution tables. Do not call providers, import `duckdb`, use `ATTACH`, run DDL/schema changes, bypass DuckDB manager, or use `print()` in library code.

## Locked behavior

- **Data-quality boundary:** only `daily_prices.data_quality_status = 'ok'` rows may contribute.
- **Polars-first:** use Polars for EMA200 and as-of alignment; avoid per-date Python indicator loops.
- **Warmup:** read enough history before `start_date` for EMA200. Define `LOOKBACK_WARMUP_CALENDAR_DAYS`; suggested value: `320`.
- **No look-ahead:** for each date `d`, use only rows with `daily_prices.date <= d`.
- **Priority:** consume `constants.MARKET_REGIME_PRIORITY`; fail clearly for unsupported/unknown values.
- **Feature updates:** update all existing `daily_features` rows for each classified date/current schema version; do not filter by ticker unless a future spec requires it.
- **Transaction:** read → compute → single write transaction. On write error, rollback and return `failed` with no partial updates.

Required SQL shape:

```sql
UPDATE daily_features
SET market_regime = ?, calculated_at = CAST(now() AS TIMESTAMP)
WHERE feature_date = ? AND feature_schema_version = ?
```

Count updated rows using a method supported by the current DB manager/connection.

## Required tests

Create `tests/test_market_regime_engine.py`. Tests must be offline and use temporary DuckDB paths.

Cover:

- public API/signature, `run_id`, exact metadata keys, and `rows_processed == dates_classified`;
- guards before DB access: invalid/`simulation` `db_role`, invalid date range;
- VIX gates, trend classification, and priority override;
- fallbacks: insufficient SPY EMA200 → `neutral`; SPY absent → skipped; VIX missing → trend-only; QQQ missing/insufficient → no `bear`;
- no-lookahead and calendar-date as-of behavior, including weekends/non-trading dates;
- accurate `feature_rows_updated`; update all existing `daily_features` rows for date/schema version;
- rollback leaves no partial updates;
- write ownership: only `market_regime` and `calculated_at` change;
- `regimes_by_value` correctness;
- static scans: no direct `duckdb`, `ATTACH`, DDL/schema changes, provider imports, or `print()`;
- existing Module 01–11 tests pass unchanged.

## Module-specific source of truth

Create or update `M12_MARKET_REGIME_ENGINE_SPEC.md` before coding. Keep it concise and implementation-oriented. Include:

- purpose and non-scope;
- source references;
- formula derivation decision for bear/bull/neutral;
- exact public API and metadata keys;
- classification algorithm and priority handling;
- warmup, no-lookahead, and fallback rules;
- `daily_features` UPDATE ownership, no INSERT;
- transaction model and DB-manager usage;
- tests;
- assumptions and open gaps.

Do not invent architecture or override higher-priority Project Files.

## Output

Follow Project Instructions. Return only:

- updated zip;
- added / changed files;
- `M12_MARKET_REGIME_ENGINE_SPEC.md`;
- short design notes;
- test commands and results;
- open gaps or blockers;
- suggested commit message: `module12_market_regime_engine_stable`.
