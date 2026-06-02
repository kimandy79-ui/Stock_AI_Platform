# ChatGPT Code Review Prompt — Module 12: Market Regime Engine

Paste this prompt into ChatGPT (GPT-4o recommended) along with the source files
listed in the **Attach** section.

---

## Prompt

You are a senior Python engineer and code reviewer specialising in financial
data pipelines, DuckDB, and Polars. Please conduct a thorough code review of
**Module 12 — Market Regime Engine** from a swing-trading stock analysis
platform.

### Context

This module:
- runs after a Feature Engine (Module 11) and before a Step-3 Screening module
  (Module 13) in a daily data pipeline;
- reads `daily_prices` rows for `SPY`, `QQQ`, and `^VIX`
  (`data_quality_status = 'ok'` only) from a local DuckDB database;
- classifies one market-wide `market_regime` value per requested **calendar**
  date using the enum `extreme_risk | high_risk | bear | bull | neutral` and a
  fixed priority order (first matching condition wins);
- updates the existing `daily_features` table — only the `market_regime` and
  `calculated_at` columns — in a single transaction; and
- returns a `ServiceResult` dataclass with a fixed set of metadata keys.

### Architecture constraints the code must respect

1. No `duckdb` import in the service module — all DB access goes through an
   injected `db_manager`.
2. No `ATTACH`, DDL (`CREATE/ALTER/DROP TABLE`), or `INSERT` into
   `daily_features`.
3. No provider/network calls, no `print()` in library code.
4. `db_role` accepts only `"prod"` or `"debug"` — `"simulation"` and any other
   value must fail **before** any DB access.
5. All EMA200 computation and as-of date alignment must use Polars (no per-date
   Python indicator loops).
6. No look-ahead: for each requested date `d`, only `daily_prices.date <= d`
   may contribute.
7. `rows_processed == metadata["dates_classified"]` on **every** return path,
   including failures.
8. On write failure the transaction must be rolled back and
   `feature_rows_updated` must be `0`.

### Classification formula

```
For each calendar date d:
  As-of align (backward): use latest eligible row with date <= d per symbol.

  Priority gates (first match wins):
    ^VIX close >= 30  →  extreme_risk
    ^VIX close >= 25  →  high_risk

  Trend (only if no VIX gate fired):
    SPY close > SPY EMA200                           →  bull
    SPY close < SPY EMA200  AND  QQQ close < QQQ EMA200  →  bear
    otherwise                                        →  neutral

Fallbacks:
  SPY absent for d               →  skip date (dates_skipped_insufficient_data)
  SPY EMA200 unavailable (<200 bars)  →  neutral + warning
  VIX unavailable                →  skip VIX gates, classify by trend only
  QQQ or QQQ EMA200 unavailable  →  bear cannot fire; neutral unless bull
```

`close_used = coalesce(close_adj, close_raw)`. EMA200 uses
`ewm_mean(span=200, adjust=False)`, masked to `NULL` for the first 199 bars.

### Required metadata keys (exact set, every return path)

```
db_role, start_date, end_date, dates_requested, dates_classified,
dates_skipped_insufficient_data, rows_read, feature_rows_updated,
regimes_by_value
```

`regimes_by_value` is a `dict[str, int]` keyed by all five regimes (zeros
included); values must sum to `dates_classified`.

---

## What to review

Please address **all** of the following areas. Be specific — quote the
relevant lines when flagging an issue.

### 1. Correctness
- Does the priority/predicate loop correctly implement the classification
  formula, including all fallbacks?
- Is the backward as-of alignment (`join_asof`) correctly set up to prevent
  look-ahead on weekends and non-trading dates?
- Is EMA200 masked properly at fewer than 200 bars?
- Does `coalesce(close_adj, close_raw)` cover the `^VIX` raw-fallback case?
- Is `rows_processed == dates_classified` guaranteed on every code path
  (success, write failure, read failure, guard failure)?

### 2. Transaction safety
- Does the write phase use a single `BEGIN / COMMIT / ROLLBACK` transaction?
- On any exception inside the transaction, is rollback guaranteed (no partial
  writes)?
- Is `feature_rows_updated` set to `0` on rollback?
- Is the connection always closed (even on exception)?

### 3. Architecture boundary compliance
- Confirm no `import duckdb` in the service module.
- Confirm no `ATTACH`, `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, or
  `INSERT INTO` in any non-docstring string literal.
- Confirm no `print()` call.
- Confirm `simulation` db_role is rejected before any DB access.

### 4. Polars usage
- Are EMA200 and as-of alignment fully vectorised (no per-date Python loops
  over indicators)?
- Is `join_asof` called with the correct `strategy="backward"` and both frames
  sorted by `date`?
- Are null/absent symbol columns handled gracefully (no crash when a symbol
  has no price rows)?
- Is `ewm_mean(span=200, adjust=False)` consistent with `adjust=False` used
  in the existing Feature Engine?

### 5. Metadata completeness and counts
- Is the exact set of nine metadata keys present on every return path
  (including every guard failure and the write-failure path)?
- Does `regimes_by_value` always include all five regime keys?
- Does `sum(regimes_by_value.values()) == dates_classified` hold?

### 6. Test coverage
- Do the tests cover VIX gates, trend classification, VIX-over-trend priority,
  and all four fallbacks?
- Is the rollback test realistic (does it actually test a mid-transaction
  failure)?
- Do the static-scan tests correctly detect forbidden patterns?
- Are there gaps in edge-case coverage (e.g. exact `SPY close == EMA200`,
  multi-date windows, date ranges spanning weekends)?

### 7. Code quality and style
- Is the code consistent with the rest of the codebase style (type hints,
  docstrings, `pathlib`, `Final`, no `print`, module-level docstring)?
- Are SQL string constants clearly named and easy to audit?
- Is the `_build_predicates` helper testable in isolation?
- Any opportunities to simplify without sacrificing clarity?

### 8. Performance / scalability
- Is there unnecessary re-computation across calendar dates (e.g. repeated
  Polars scans)?
- Could the per-date `SELECT COUNT(*) + UPDATE` loop be a bottleneck for large
  date ranges? If so, suggest a vectorised alternative (e.g. a single
  `UPDATE … WHERE feature_date IN (…)` with a bulk parameter list).
- Is warmup (`LOOKBACK_WARMUP_CALENDAR_DAYS = 320`) sufficient for EMA200 and
  appropriately documented?

### 9. Open gaps / risks
- Are there any undocumented assumptions that could silently produce wrong
  regimes?
- What would break if `MARKET_REGIME_PRIORITY` is extended with a new regime
  in a future module?
- Is the `success_with_warnings` status for the neutral-by-missing-EMA200
  fallback the right signal for downstream consumers?

---

## Files to attach

Attach all of the following when submitting this prompt:

| File | Purpose |
|------|---------|
| `app/services/regime/market_regime_engine.py` | Main implementation |
| `app/services/regime/__init__.py` | Package export |
| `tests/test_market_regime_engine.py` | Test suite |
| `M12_MARKET_REGIME_ENGINE_SPEC.md` | Module spec / contract |
| `app/config/constants.py` | Regime enum, VIX thresholds, priority order |
| `app/utils/service_result.py` | `ServiceResult` contract |
| `app/database/duckdb_manager.py` | DB manager interface |

Optional for deeper context:
- `app/services/features/feature_engine.py` (Module 11, the style reference)
- `app/database/schema_manager.py` (`daily_prices` / `daily_features` DDL)

---

## Expected output from ChatGPT

Please structure your response as:

1. **Summary verdict** — one paragraph, overall quality and most critical
   issues.
2. **Issue list** — numbered, each with: severity (`critical / major / minor /
   suggestion`), location (file + line or function name), description, and
   recommended fix.
3. **Positive observations** — what is done well (brief).
4. **Suggested diff or rewrite** — for any `critical` or `major` issues,
   provide the corrected code snippet.
5. **Test gaps** — specific additional test cases you recommend.
