# M12 — Market Regime Engine Spec

Module-specific source of truth for **Module 12 — Market Regime Engine**.
Concise and implementation-oriented. Higher-priority Project Files win on any
conflict (`01a`, `01b`, `01c`, `01d`, `02b`); this spec only fills the gap they
leave for the bull/bear/neutral trend rule (open gap **G-REGIME** in
`M11_FEATURE_ENGINE_SPEC.md`).

## 1. Purpose and non-scope

Runs after Module 11 (Feature Engine) and before Module 13 (Step 3 Screening).
It classifies exactly one `market_regime` value per requested calendar date from
`SPY`, `QQQ`, `^VIX` price history and writes that value back onto the existing
`daily_features` rows for the date / current feature schema version.

Non-scope: no new tables, no DDL, no `daily_features` inserts, no `daily_prices`
writes, no provider/network calls, no Module 13+ logic, no per-ticker regime
(regime is market-wide, written to every existing `daily_features` row for the
date). It does not recompute any Module 11 indicator other than the EMA200 it
needs for SPY/QQQ.

## 2. Source references

- regime enum / VIX thresholds / priority / guardrails → `01a_CORE_PRINCIPLES.md`,
  `app/config/constants.py` (`REGIME_*`, `MARKET_REGIME_PRIORITY`,
  `VIX_EXTREME_RISK_THRESHOLD = 30.0`, `VIX_HIGH_RISK_THRESHOLD = 25.0`,
  `BENCHMARK_SPY/QQQ/VIX`, `FEATURE_SCHEMA_VERSION = "features_v01"`).
- `daily_prices` / `daily_features` schema → `01b_SCHEMA_AND_DATA.md`,
  `app/database/schema_manager.py`.
- VIX threshold config (25 / 30) → `01c_FORMULAS_AND_CONFIGS.md` (`market_regime`).
- pipeline position / no-look-ahead / config-driven → `01d_MODULES_AND_PIPELINE.md`.
- Polars-first / market-regime uses SPY/QQQ/VIX / look-ahead via cutoff →
  `02b_ARCHITECTURE_DECISIONS.md` §22.2, §22.7, §22.10.
- service style / EMA behavior mirror → `M11_FEATURE_ENGINE_SPEC.md`,
  `app/services/features/feature_engine.py`.

## 3. Formula derivation decision (G-REGIME closure)

Project Files freeze the enum, the VIX thresholds (25 / 30) and the priority
order (`extreme_risk > high_risk > bear > bull > neutral`) but define **no**
explicit SPY/QQQ rule for `bear` / `bull` / `neutral`. None of the
higher-priority files contradict the minimal rule below, so it is adopted:

```text
For each requested calendar date d:
  Use the latest eligible row (data_quality_status = 'ok') for SPY, QQQ and ^VIX
  with daily_prices.date <= d  (as-of / no look-ahead).

Priority gates (consume MARKET_REGIME_PRIORITY top-down, first match wins):
  if ^VIX close >= VIX_EXTREME_RISK_THRESHOLD (30) -> extreme_risk
  if ^VIX close >= VIX_HIGH_RISK_THRESHOLD    (25) -> high_risk

Trend classification (only if no VIX gate fired):
  if SPY close > SPY EMA200                                  -> bull
  if SPY close < SPY EMA200 and QQQ close < QQQ EMA200       -> bear
  otherwise                                                  -> neutral
```

`bull` and `bear` are mutually exclusive by construction (strict `>` vs strict
`<`), so the priority order between them is never actually contested; an exact
`SPY close == SPY EMA200` falls through to `neutral`.

If a future, higher-priority Project File defines a contradictory rule, follow
that file and record the deviation here.

## 4. Price selection and EMA200

- `close_used = coalesce(close_adj, close_raw)`. For `SPY` / `QQQ` this is
  `close_adj`; for `^VIX` `close_adj` is normally `NULL`, so it falls back to
  `close_raw` (documented; assumption **A-VIX-RAW**).
- EMA200 is the standard recursive EMA, `ewm_mean(span=200, adjust=False)` per
  symbol over `close_used`, identical to Module 11 EMA behavior, masked to
  `NULL` until a symbol has `>= 200` eligible bars (`_MIN_BARS_EMA200 = 200`).
- Computed once with Polars window expressions; no per-date indicator loops.

## 5. Public API

```text
MarketRegimeEngine(db_manager=None)
    .classify(
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult
```

- `db_manager=None` uses the approved `app.database.duckdb_manager`; the argument
  exists only for test injection. No arbitrary paths, no `ATTACH`, no `duckdb`
  import.
- Guards, evaluated **before any DB access**:
  - `db_role` must be `"prod"` or `"debug"`; `"simulation"` and any other value
    fail. (`simulation` is a valid manager role but forbidden here.)
  - `start_date > end_date` fails.
  - every value in `MARKET_REGIME_PRIORITY` must be a known regime with a
    classifier predicate; an unknown/unsupported value fails clearly.
- `run_id` is a fresh `uuid4` when `None`.
- `rows_processed == metadata["dates_classified"]` on **every** return path.

## 6. Exact metadata keys

`ServiceResult.metadata` carries exactly these keys on every return path:

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

- `dates_requested`: count of calendar days in `[start_date, end_date]`.
- `dates_classified`: dates with a computed regime, even if no `daily_features`
  row exists for them. `== dates_requested - dates_skipped_insufficient_data`.
- `dates_skipped_insufficient_data`: dates skipped because SPY had no eligible
  row on or before that date.
- `rows_read`: eligible SPY/QQQ/^VIX `daily_prices` rows read, including warmup.
- `feature_rows_updated`: actual existing `daily_features` rows updated.
- `regimes_by_value`: `dict[str, int]` keyed by **every** regime in
  `MARKET_REGIME_PRIORITY` (zeros included); values sum to `dates_classified`.

On guard/read failure all durable counts are `0` (and `dates_classified = 0`).
On write failure the transaction is rolled back and `feature_rows_updated = 0`
while `dates_classified` / `rows_processed` keep their computed values.

## 7. Classification algorithm and priority handling

1. Compute the per-(symbol, date) EMA200 frame once (Polars).
2. As-of align (Polars `join_asof`, backward) each symbol onto the full list of
   requested calendar dates, yielding per date: `spy_close`, `spy_ema200`,
   `qqq_close`, `qqq_ema200`, `vix_close`. Backward as-of gives the latest row
   with `date <= d`, which correctly resolves weekends / non-trading dates and
   guarantees no look-ahead.
3. For each requested date, build a predicate map and iterate
   `MARKET_REGIME_PRIORITY` top-down, returning the first regime whose predicate
   holds; `neutral` is the guaranteed fallback:
   - `extreme_risk`: VIX available and `vix_close >= 30`.
   - `high_risk`: VIX available and `vix_close >= 25`.
   - `bear`: SPY EMA200 available, `spy_close < spy_ema200`, and QQQ + QQQ EMA200
     available with `qqq_close < qqq_ema200`.
   - `bull`: SPY EMA200 available and `spy_close > spy_ema200`.
   - `neutral`: fallback (also when SPY EMA200 is unavailable).

## 8. Warmup, no-look-ahead, fallbacks

- **Warmup** (`LOOKBACK_WARMUP_CALENDAR_DAYS = 320`, assumption **A-WARMUP**):
  read from `start_date - 320 calendar days` through `end_date` so EMA200 has
  `>= 200` eligible bars by `start_date` (≈228 trading days of buffer).
- **No look-ahead**: only `daily_prices.date <= d` contributes to date `d`
  (`02b §22.7`). Implemented by backward as-of alignment.
- **Fallbacks** (mirror the prompt):
  - SPY has no eligible row on/before `d` → date **skipped**
    (`dates_skipped_insufficient_data`), no regime, no update.
  - SPY present but EMA200 unavailable (`< 200` bars) → `neutral` + a warning
    (aggregated; status becomes `success_with_warnings`).
  - VIX unavailable for `d` → skip VIX gates, classify by trend only (expected;
    info-logged, not a warning).
  - QQQ or QQQ EMA200 unavailable → `bear` cannot fire → `neutral` unless `bull`.

## 9. `daily_features` UPDATE ownership, no INSERT

Module 12 may update only:

```text
daily_features.market_regime
daily_features.calculated_at
```

for rows where `feature_date in [start_date, end_date]` and
`feature_schema_version = constants.FEATURE_SCHEMA_VERSION`. It never inserts
`daily_features` rows and never filters by ticker (regime is market-wide), and
never writes any other table.

Required SQL shape:

```sql
UPDATE daily_features
SET market_regime = ?, calculated_at = CAST(now() AS TIMESTAMP)
WHERE feature_date = ? AND feature_schema_version = ?
```

`feature_rows_updated` is counted with a `SELECT COUNT(*)` over the identical
`WHERE` immediately before each per-date `UPDATE`, inside the write transaction
(a method supported by the DuckDB manager connection); the per-date counts are
summed. Dates with no matching `daily_features` row contribute `0` but still
count as classified.

## 10. Transaction model and DB-manager usage

Read → compute → single write transaction. The read uses a read-only connection
(closed before compute). The write opens one connection, `BEGIN TRANSACTION`,
runs the per-date count + UPDATE pairs, then `COMMIT`. Any write error triggers
`ROLLBACK`, leaving no partial updates, and returns a `failed` result with
`feature_rows_updated = 0`. All connections come from
`app.database.duckdb_manager` (injectable for tests); no `duckdb` import, no
`ATTACH`, no DDL/schema change, no `print()`.

## 11. Tests

`tests/test_market_regime_engine.py`, fully offline, temporary DuckDB paths:
exact signature / `run_id` mint+preserve / exact metadata keys /
`rows_processed == dates_classified`; guards before DB access (`simulation` and
other invalid `db_role`, inverted date range); VIX extreme/high gates, trend
bull/bear/neutral, and VIX-over-trend priority; fallbacks (insufficient SPY
EMA200 → neutral+warn, SPY absent → skipped, VIX missing → trend-only, QQQ
missing/insufficient → no bear); no-look-ahead and weekend/non-trading as-of;
accurate `feature_rows_updated` across all existing rows for a date/schema
version; rollback leaves no partial updates; write ownership (only
`market_regime` + `calculated_at` change); `regimes_by_value` correctness; and
static scans (no direct `duckdb`, `ATTACH`, DDL, provider import, or `print()`).

## 12. Assumptions and open gaps

- **A-WARMUP**: `LOOKBACK_WARMUP_CALENDAR_DAYS = 320` (prompt-suggested), defined
  inside the Module 12 module so frozen `constants.py` (Module 01) is untouched.
- **A-VIX-RAW**: `^VIX` uses `close_raw` via `coalesce(close_adj, close_raw)`
  because adjusted VIX closes are normally `NULL`.
- **A-REGIME-ALL-KEYS**: `regimes_by_value` always lists all five regimes
  (zeros included) for stable, summable counts. The dict is built from
  `CANONICAL_REGIMES` — a fixed module-level tuple — rather than from
  `MARKET_REGIME_PRIORITY`, so the metadata shape is stable even if the
  priority ordering changes (the guard enforces the two sets stay equal).
- **A-WARN-STATUS**: any neutral-by-missing-SPY-EMA200 fallback makes the result
  `success_with_warnings` (one aggregated warning), otherwise `success`.
- **G-REGIME** is closed by §3; no remaining open gaps for Module 12.

## 13. Priority guard (post-freeze upgrade)

The priority guard validates `MARKET_REGIME_PRIORITY` in three steps before any
DB access, each with a clear error message:

1. **Unknown values** — any entry not in `SUPPORTED_REGIMES` fails.
2. **Duplicates** — `len(priority) != len(set(priority))` fails.
3. **Set mismatch** — `set(priority) != set(CANONICAL_REGIMES)` fails (catches
   missing regimes not caught by the unknown-values step).

`CANONICAL_REGIMES` is the authoritative fixed tuple; `MARKET_REGIME_PRIORITY`
controls classification order only.  The guard enforces they cover the same set.
