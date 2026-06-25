# M11_FEATURE_ENGINE_SPEC

Module 11 — Feature Engine. Contract spec. Concise and implementation-oriented.
Derived from the frozen split Project Files; where they are silent, gaps are
marked, not invented.

## 1. Purpose and non-scope

Purpose: read eligible `daily_prices` rows, compute `daily_features` strictly
from the frozen formulas, and upsert exactly one feature row per processed
ticker (anchored on that ticker's `feature_cutoff_date`) into `daily_features`.

Runs after Module 10 (Mutation Detector), before Module 12 (Market Regime
Engine).

Non-scope: no Module 12 standalone market-regime computation/tables; no
provider/network calls; no writes to any table other than `daily_features`; no
`duckdb` import, `ATTACH`, DDL, or schema change; no `print()` in library code.

## 2. Source references

- Formulas: `01c_FORMULAS_AND_CONFIGS.md` → `FORMULAS/60_Feature_Formulas_Complete.md`.
- `daily_features` schema: `01b_SCHEMA_AND_DATA.md` (mirrored in `schema_manager.py`).
- Pipeline position / boundary: `01d_MODULES_AND_PIPELINE.md` (Module 11/12).
- Decisions: `02b_ARCHITECTURE_DECISIONS.md` 22.2 (Polars-first), 22.6 (raw+adjusted),
  22.7 (feature_cutoff_date / no look-ahead), 22.8 (zero-padded schema version),
  22.10 (regime uses SPY/QQQ/VIX — Module 12).
- Constants: `app/config/constants.py` (`FEATURE_SCHEMA_VERSION = "features_v01"`,
  `SECTOR_ETF_MAP`, VIX thresholds, `MARKET_REGIME_PRIORITY`).

## 3. Exact public API

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
- `tickers=None` → all distinct tickers with eligible (`data_quality_status='ok'`)
  `daily_prices` rows in `[start_date, end_date]`.
- explicit `tickers` → only requested; requested tickers with no eligible
  in-range rows counted in `tickers_skipped_no_data`.
- `db_role` accepts only `prod` / `debug`; any other value (incl. `simulation`)
  fails before any DB read/write.
- `start_date > end_date` fails before any DB read/write.
- `run_id is None` → fresh `uuid4`.
- `ServiceResult.rows_processed == metadata["tickers_processed"]` on every return.

## 4. Exact metadata keys

`db_role, start_date, end_date, tickers_requested, tickers_processed,
tickers_skipped_no_data, rows_read, feature_rows_written, feature_rows_updated,
feature_ready_count, feature_not_ready_count`.

Definitions:
- `tickers_requested`: `len(tickers)` (deduplicated input is still counted by
  the unique requested set); `0` when `tickers=None`.
- `tickers_processed`: distinct tickers for which a feature row was attempted
  (one row per ticker at its cutoff).
- `tickers_skipped_no_data`: requested tickers with no eligible in-range rows;
  `0` when `tickers=None`.
- `rows_read`: eligible source rows loaded into Polars, including warmup and the
  sector ETF rows needed for sector relative strength.
- `feature_rows_written`: net-new `daily_features` rows inserted.
- `feature_rows_updated`: existing rows updated via conflict.
- `feature_ready_count` / `feature_not_ready_count`: rows written/updated this
  run split by readiness.

On guard/read failure: all durable write counts `0`. On write failure: rollback
mandatory; read/compute counts may remain accurate, durable write counts `0`.

## 5. Formula-to-column mapping

All formulas use rows with `date <= feature_cutoff_date` (no look-ahead). Price
indicators use **adjusted** prices; volume features use **raw** volume.

| column | type | formula / source | inputs | lookback | null/default | status |
|---|---|---|---|---|---|---|
| ticker | VARCHAR | key | selection | — | n/a | implemented |
| feature_date | DATE | = feature_cutoff_date | daily_prices.date | — | n/a | implemented |
| feature_cutoff_date | DATE | latest eligible date in `[start,end]` per ticker | daily_prices.date | — | n/a | implemented |
| feature_schema_version | VARCHAR | `constants.FEATURE_SCHEMA_VERSION` | constant | — | `features_v01` | implemented |
| feature_ready | BOOLEAN | TRUE iff all required cols non-null | required cols | — | FALSE | implemented |
| ema20/50/200 | DOUBLE | EMA(span) on close_adj (recursive EWM, adjust=False) | close_adj | 20/50/200 | NULL if < span bars | implemented |
| ema_alignment_score | DOUBLE | 100 if EMA20>EMA50>EMA200; 50 if close_adj>EMA200; else 0 | emas, close_adj | 200 | NULL if any EMA null | implemented |
| distance_to_ema20/50/200_pct | DOUBLE | close_adj/EMA − 1 (optional) | close_adj, ema | per ema | NULL if ema null | implemented |
| rsi14 | DOUBLE | Wilder RSI14 on close_adj (EWM α=1/14); RSI=100 if avg_loss=0 | close_adj deltas | 15 | NULL if < 15 bars | implemented |
| roc20 | DOUBLE | close_adj_t / close_adj_{t-20} − 1 | close_adj | 20 | NULL if < 21 bars | implemented |
| atr14 | DOUBLE | Wilder ATR14 on adjusted OHLC (EWM α=1/14) | high_adj,low_adj,close_adj | 15 | NULL if < 15 bars | implemented |
| atr_pct | DOUBLE | atr14 / close_adj_t | atr14, close_adj | 15 | NULL if atr14 null | implemented |
| rvol20 | DOUBLE | volume_raw_t / mean(volume_raw, t-20..t-1) | volume_raw | 20 | NULL if < 21 bars | implemented |
| avg_volume_20d | DOUBLE | mean(volume_raw, prior 20 td) | volume_raw | 20 (t-20..t-1) | NULL if < 21 bars | implemented |
| avg_dollar_volume_20d | DOUBLE | mean(close_raw·volume_raw, prior 20 td) | close_raw, volume_raw | 20 (t-20..t-1) | NULL if < 21 bars | implemented |
| distance_from_52w_high_pct | DOUBLE | close_adj_t / max(close_adj, 252 td ending t) − 1 | close_adj | 252 | NULL if < 252 bars | implemented |
| pullback_from_recent_high_pct | DOUBLE | close_adj_t / max(close_adj, 20 td ending t) − 1 | close_adj | 20 | NULL if < 20 bars | implemented |
| breakout_proximity | DOUBLE | (close_adj_t − rolling_20d_high) / atr14_t | close_adj, atr14 | 20 | NULL if either null | implemented |
| consolidation_score | DOUBLE | 100·(0.4·atr_contr + 0.4·range_contr + 0.2·vol_contr), clip 0–100 | atr14, high_adj−low_adj, volume_raw | 60 | NULL if 60-window not filled | implemented |
| sector_relative_strength | DOUBLE | ticker_20d_return_adj − sector_etf_20d_return_adj | roc20 (ticker & mapped ETF) | 20 | NULL if unmapped/missing | implemented |
| market_regime | VARCHAR | inline regime classification | SPY/QQQ/VIX | — | **NULL** | **open gap G-REGIME** |
| days_to_earnings_bd | INTEGER | business days to next earnings | earnings_calendar | — | **NULL** | **open gap G-EARN** |
| earnings_confidence | VARCHAR | confidence of next earnings | earnings_calendar | — | **NULL** | **open gap G-EARN** |
| macro_event_risk_flag | BOOLEAN | macro event in window | macro_events_calendar | — | **FALSE** | **open gap G-MACRO** |
| calculated_at | TIMESTAMP | DB `now()` on every upsert | clock | — | set on write | implemented |

Contraction terms (all clipped so each ≤ 1):
`atr_contraction = 1 − min(ATR14 / mean(ATR14, prior 60 td), 1)`;
`range_contraction = 1 − min(mean(high_adj−low_adj, prior 10 td) / mean(…, prior 60 td), 1)`;
`volume_contraction = 1 − min(mean(volume_raw, prior 10 td) / mean(…, prior 60 td), 1)`.

NaN/±inf for any computed value is mapped to NULL (an infinite indicator is not
"available" and must not count toward readiness).

## 6. Date range, warmup, feature_cutoff_date

- `feature_cutoff_date` = latest eligible `daily_prices.date` within
  `[start_date, end_date]` per ticker; `feature_date = feature_cutoff_date`.
- Read window: `[start_date − LOOKBACK_WARMUP_CALENDAR_DAYS, end_date]` with
  `LOOKBACK_WARMUP_CALENDAR_DAYS = 420` calendar days (assumption A-WARMUP),
  which covers the longest required lookback (252 trading days ≈ 353 calendar
  days) with a holiday/closure buffer.
- Rows after a ticker's cutoff are never loaded (`date <= end_date`) and never
  used — no look-ahead.
- Exactly one `daily_features` row is written per processed ticker, at its
  cutoff (never outside the requested range).

## 7. Required vs optional readiness

Required (all must be non-null for `feature_ready = TRUE`): `ema20, ema50,
ema200, ema_alignment_score, rsi14, roc20, atr14, atr_pct, rvol20,
avg_volume_20d, avg_dollar_volume_20d, distance_from_52w_high_pct,
pullback_from_recent_high_pct, breakout_proximity, consolidation_score`.

Optional (never block readiness): `distance_to_ema20/50/200_pct,
sector_relative_strength, market_regime, days_to_earnings_bd,
earnings_confidence, macro_event_risk_flag`.

This matches the frozen Module 11 readiness list.

## 8. Price / volume rules

Adjusted prices (`close_adj`, `high_adj`, `low_adj`) drive all price indicators;
volume features use `volume_raw`; `avg_dollar_volume_20d = mean(close_raw ·
volume_raw)`; `volume_adj` is reserved and unused in V1.

## 9. Sector relative strength

Uses `constants.SECTOR_ETF_MAP`. The ticker's sector comes from
`ticker_master`; the mapped ETF's 20-day adjusted return is read from eligible
`close_adj` rows (the ETF is loaded alongside the targets). If the sector is
absent/unmapped, or the ETF lacks a value at the cutoff date, or the ticker's
own `roc20` is null → `sector_relative_strength = NULL`. `sector_etf_map` is not
created or modified.

## 10. Inline market-regime note + Module 12 boundary

The frozen sources define VIX thresholds (25 / 30) and a regime priority order
but **no** explicit inline bull/bear/neutral classification formula, and the
market regime is owned by Module 12. Per the prompt, this is recorded as open
gap **G-REGIME** and `market_regime` is written `NULL`. No standalone regime
computation or regime tables are implemented here.

## 11. Earnings / macro fallback

No feature-engine population rule for `days_to_earnings_bd` /
`earnings_confidence` / `macro_event_risk_flag` is defined in the frozen
sources, and no strategy config is passed to this module's API (the macro/
earnings windows live in strategy config, used by later scoring modules).
Defaults applied: `days_to_earnings_bd = NULL`, `earnings_confidence = NULL`,
`macro_event_risk_flag = FALSE`. Open gaps **G-EARN** / **G-MACRO**.

## 12. Polars strategy

A single per-(ticker, date) frame is computed with vectorised window
(`.over("ticker")`) and rolling expressions — EWM for EMAs and Wilder RSI/ATR,
`rolling_max`/`rolling_mean` (with `min_samples`) for the windowed stats, and
`shift`/`diff` for lagged terms. No ticker-by-ticker Python indicator loops; the
only Python loops are small orchestration loops (row assembly and the upsert
batch).

## 13. Upsert / idempotency / transaction model

Three phases: (1) read source rows through the DB manager and close the read
connection; (2) compute in Polars with no DB writes; (3) upsert all rows in one
transaction. Upsert:
`INSERT … ON CONFLICT (ticker, feature_date, feature_schema_version) DO UPDATE SET …`.
`calculated_at` is refreshed via DB `now()` on insert and on conflict. Reruns
are stable (no duplicates). Inserts vs conflict-updates are counted by probing
existing keys for the run's tickers at the start of the transaction. On any
write error: `ROLLBACK` and return `failed` with no partial Module 11 writes.

Conflict **R-CREATED_AT**: the frozen `daily_features` schema has **no**
`created_at` column (only `calculated_at`), so there is nothing to "preserve on
conflict"; the prompt's `created_at` preservation note is satisfied vacuously.

## 14. DB-manager usage

All access goes through `app.database.duckdb_manager` (read-only connection for
reads; read-write for the single upsert transaction). The optional
`db_manager=` constructor arg exists only for test injection. No `duckdb`
import, no `ATTACH`, no DDL.

## 15. Tests

`tests/test_feature_engine.py`, fully offline, temp DuckDB paths via
`monkeypatch`. Covers: signature/`run_id`/exact metadata keys/`rows_processed ==
tickers_processed`; guards (invalid & `simulation` `db_role`, invalid date
range, no DB access before guard via exploding stub); ticker selection
(`None`/explicit/skip-no-data); data-quality filter; warmup read; no-lookahead
cutoff; readiness (sufficient vs insufficient history); deterministic EMA /
RSI14 / ATR14 / ROC20 / RVOL20 / 52w-high / pullback / breakout / consolidation
clamp / EMA alignment; sector RS present and NULL fallback; market-regime NULL
(open gap); earnings/macro defaults; upsert idempotency + `calculated_at`
refresh; write ownership (only `daily_features` changes; forbidden tables
untouched); rollback leaves no partial rows; static scans (no direct `duckdb`,
`ATTACH`, DDL, provider imports, `print()`).

## 16. Assumptions, open gaps, blockers

Assumptions:
- **A-WARMUP**: 420-calendar-day warmup window covers the 252-trading-day
  lookback with buffer.
- **A-20D-WINDOW**: `avg_volume_20d` / `avg_dollar_volume_20d` use the prior 20
  trading days (t-20..t-1), consistent with the RVOL20 denominator definition.
- **A-NAN-NULL**: NaN/±inf computed values are stored as NULL.

Open gaps (implemented as documented defaults, not invented):
- **G-REGIME**: no inline market-regime formula → `market_regime = NULL`.
- **G-EARN**: no feature-engine earnings-population rule → `days_to_earnings_bd`
  / `earnings_confidence = NULL`.
- **G-MACRO**: no feature-engine macro rule → `macro_event_risk_flag = FALSE`.

Blockers: none for implementation. Local test execution in the build sandbox was
blocked only by the absence of `duckdb`/`polars` wheels and disabled network;
the suite is written to run with the pinned `requirements.txt` versions.
