# M11_FEATURE_ENGINE_SPEC

Module 11 — Feature Engine. Contract spec. Phase 2 (features_v02).
Derived from the frozen split Project Files; gaps are marked, not invented.

## 1. Purpose and non-scope

Purpose: read eligible `daily_prices` rows, compute `daily_features`
(schema version `features_v02`) strictly from the frozen formulas, and upsert
exactly one feature row per processed ticker (anchored on that ticker's
`feature_cutoff_date`) into `daily_features`.

Runs after Module 10 (Mutation Detector), before Module 12 (Market Regime Engine).

Non-scope: no Module 12 regime computation; no provider/network calls; no
writes to any table other than `daily_features`; no direct `duckdb` import,
`ATTACH`, DDL, or schema change; no `print()` in library code.

## 2. Source references

- Formulas: `01c_FORMULAS_AND_CONFIGS.md` → `FORMULAS/60_Feature_Formulas_Complete.md`.
- `daily_features` schema: `01b_SCHEMA_AND_DATA.md` (mirrored in `schema_manager.py`).
- Pipeline position: `01d_MODULES_AND_PIPELINE.md` (Module 11/12).
- Decisions: `02b_ARCHITECTURE_DECISIONS.md` 22.2, 22.6, 22.7, 22.8, 22.10,
  22.19–22.24.
- Constants: `app/config/constants.py` (`FEATURE_SCHEMA_VERSION = "features_v02"`,
  `SECTOR_ETF_MAP`, `BENCHMARK_SPY`, VIX thresholds, `MARKET_REGIME_PRIORITY`).

## 3. Public API (unchanged)

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

Guards (fire before any DB I/O):
- `db_role` not in `{"prod", "debug"}` → `STATUS_FAILED`.
- `start_date > end_date` → `STATUS_FAILED`.

`ServiceResult.rows_processed == metadata["tickers_processed"]` on every return.

## 4. Metadata keys (unchanged)

`db_role, start_date, end_date, tickers_requested, tickers_processed,
tickers_skipped_no_data, rows_read, feature_rows_written, feature_rows_updated,
feature_ready_count, feature_not_ready_count`.

All keys present on every return path including guard failures and write rollback.

## 5. Price columns read

```sql
SELECT ticker, date, close_raw, high_raw, low_raw,
       close_adj, high_adj, low_adj, volume_raw
FROM daily_prices
```

`high_raw` / `low_raw` are fetched (available for future use); structural
level computation uses adjusted columns only; raw conversion is deferred to
Step 4.

## 6. SPY in the load set

`constants.BENCHMARK_SPY` ("SPY") is unconditionally added to the price-read
load set to enable `relative_strength_vs_spy`.  If SPY has no eligible rows at
the cutoff date, `relative_strength_vs_spy` is NULL.  No error is raised.

## 7. Upsert key

`PRIMARY KEY (ticker, feature_date, feature_schema_version)` — enforced by
`ON CONFLICT DO UPDATE`.  `calculated_at` is refreshed on every write (including
conflict-updates).

## 8. v01 formula-to-column mapping (unchanged; all implemented)

| column | formula | inputs | min_bars | null condition |
|---|---|---|---|---|
| ema20/50/200 | EWM(span), close_adj | close_adj | 20/50/200 | < span bars |
| ema_alignment_score | 100/50/0 tiers | emas, close_adj | 200 | any EMA null |
| distance_to_ema{20,50,200}_pct | close_adj/EMA−1 | close_adj, ema | per ema | ema null |
| rsi14 | Wilder RSI14, EWM α=1/14 | close_adj deltas | 15 | < 15 bars |
| roc20 | close_adj_t/close_adj_{t-20}−1 | close_adj | 20 | < 21 bars |
| atr14 | Wilder ATR14, EWM α=1/14 | high/low/close_adj | 15 | < 15 bars |
| atr_pct | atr14/close_adj_t | atr14, close_adj | 15 | atr14 null |
| rvol20 | vol_raw_t/mean(vol, t-20..t-1) | volume_raw | 20 | < 21 bars |
| avg_volume_20d | mean(vol_raw, prior 20 td) | volume_raw | 20 | < 21 bars |
| avg_dollar_volume_20d | mean(close_raw·vol_raw, prior 20 td) | close_raw, volume_raw | 20 | < 21 bars |
| distance_from_52w_high_pct | close_adj/max(close_adj,252td)−1 | close_adj | 252 | < 252 bars |
| pullback_from_recent_high_pct | close_adj/max(close_adj,20td)−1 | close_adj | 20 | < 20 bars |
| breakout_proximity | (close_adj−high20)/atr14 | close_adj, atr14 | 20 | either null |
| consolidation_score | 100·(0.4·atr_contr+0.4·range_contr+0.2·vol_contr) | atr14, hl range, volume | 60 | < 60 bars |
| sector_relative_strength | ticker_roc20−sector_etf_roc20 | roc20 | 20 | unmapped/missing |

## 9. v02 new columns (all optional — NULL when insufficient data)

### Vectorised (Polars, computed for all tickers in one pass)

| column | formula | inputs | min_bars | null condition | setup usage |
|---|---|---|---|---|---|
| ema20_slope | ema20_t / ema20_{t-5} − 1 | ema20 | 25 | bar < 25 | pullback, trend_continuation |
| ema50_slope | ema50_t / ema50_{t-10} − 1 | ema50 | 60 | bar < 60 | trend_continuation |
| atr_compression_score | 100·(1−min(atr14/mean(atr14,60td),1)), clip 0–100. >0 when current ATR < mean | atr14 | 75 | atr14 or atr_mean60 null |consolidation_base |
| pullback_depth_pct | (max(high_adj,20td)−close_adj)/max(high_adj,20td) | high_adj,close_adj | 20 | < 20 bars or max≤0 | pullback |
| volume_dry_up_score | 100·(1−min(vol_mean10/vol_mean60,1)), clip 0–100. >0 when recent vol < 60d mean | volume_raw | 60 | either mean null or 60d mean=0 | consolidation_base, pullback |
| volume_expansion_score | 100·min(max(rvol20−1,0)/1,1), clip 0–100 | rvol20 | 20 | rvol20 null | breakout |

### Per-ticker Python helpers (operate on full history slice per ticker)

#### True range
`TR_i = max(high_i−low_i, |high_i−close_{i-1}|, |low_i−close_{i-1}|)`.
Bar 0 uses `high−low` only (no prior close).  Uses **adjusted** prices.

#### Swing pivots — `_compute_swing_pivots(ticker_df, k=2, lookback=20)`

Returns `(swing_highs: list[float], swing_lows: list[float])` ordered
most-recent first.  A bar at index `i` is a confirmed pivot high if its
`high_adj` is strictly greater than each of the `k` bars before AND after it.
Swing lows use `low_adj` with strictly less-than.

Scans bars from `last_confirmable = n−k−1` back to
`search_start = max(k, n−lookback−k)`.  Collects ALL confirmed pivots in
the window (not just the first).  Returns empty lists if fewer than `2k+1`
bars.

`swing_high` / `swing_low` stored in `daily_features` are the most-recent
confirmed pivot high / low values (lists[0]).

#### Support / resistance — `_compute_support_resistance(...)`

Uses the full `swing_highs` and `swing_lows` lists:

- `support_level`: largest `swing_low < close_adj`.  Fallback: `ema50`.
- `resistance_level`: smallest `swing_high > close_adj`.  Fallback: `max(high_adj, 20td)`.
- `next_resistance_level`: smallest `swing_high > resistance_level`.
  Fallback: `52w high` if > `resistance_level`.

All values on adjusted basis.  All None when `close_adj` is None.

#### Base detection — `_compute_base(ticker_df)`

1. Compute per-bar true range (full ATR formula) on adjusted prices.
2. Reference median TR: sorted median of `trs[n-61..n-2]` (60 bars before cutoff).
3. Threshold = `1.5 × median_TR`.
4. Qualifying mask: `TR_i ≤ threshold` for each of the last 60 bars.
5. Find the **longest** contiguous run of qualifying bars (any position in
   the last 60 bars; not required to end at the cutoff bar — supports
   detection of a base the price has broken out of).
6. If longest run ≥ 2: compute `base_high`, `base_low`, `range_duration`,
   `range_width_pct = (base_high−base_low)/base_low`,
   `range_tightness_score = 100·(1−min(range_width_pct/0.20,1))`.

Requires ≥ 60 bars.  Returns all-None otherwise.

| column | null condition | setup usage |
|---|---|---|
| swing_high | < 5 bars or no confirmed pivot high in window | breakout, pullback, trend_continuation |
| swing_low | < 5 bars or no confirmed pivot low in window | pullback, consolidation_base |
| support_level | close_adj null | all setups |
| resistance_level | close_adj null | breakout, consolidation_base |
| next_resistance_level | resistance null or no value above | breakout, pullback |
| base_high | < 60 bars or longest qualifying run < 2 | consolidation_base, breakout |
| base_low | base_high null | consolidation_base, breakout |
| range_width_pct | base_low null or ≤ 0 | consolidation_base |
| range_duration | < 60 bars or run < 2 | consolidation_base |
| range_tightness_score | range_width_pct null | consolidation_base |
| relative_strength_vs_spy | SPY roc20 missing | trend_continuation |

## 10. Readiness (unchanged)

Required (must all be non-null for `feature_ready = TRUE`): `ema20, ema50,
ema200, ema_alignment_score, rsi14, roc20, atr14, atr_pct, rvol20,
avg_volume_20d, avg_dollar_volume_20d, distance_from_52w_high_pct,
pullback_from_recent_high_pct, breakout_proximity, consolidation_score`.

All v02 columns are **optional** — never block `feature_ready`.

## 11. Write / rollback

Single `BEGIN / COMMIT` across all tickers per run.  Any exception triggers
`ROLLBACK` before re-raising.  On rollback: `feature_rows_written = 0`,
`feature_rows_updated = 0`.  All metadata keys present.

## 12. Module boundary rules

- No direct `duckdb` import.
- No `ATTACH`, DDL, schema change.
- No `print()`.
- No provider calls.
- Writes only `daily_features`.
- Reads only `daily_prices` and `ticker_master`.
- `db_role` and `start_date > end_date` guards fire before any DB I/O.

## 13. Open gaps (unchanged)

- **G-REGIME**: `market_regime = NULL` (owned by Module 12).
- **G-EARN**: `days_to_earnings_bd = NULL`, `earnings_confidence = NULL`.
- **G-MACRO**: `macro_event_risk_flag = FALSE`.

## 14. Feature mapping table

| feature_name | setup_usage | null_behavior | tests |
|---|---|---|---|
| ema20_slope | pullback, trend_continuation | NULL if < 25 bars | test_ema20_slope_formula, test_trend_continuation_positive_ema_slopes |
| ema50_slope | trend_continuation | NULL if < 60 bars | test_ema50_slope_null_with_too_few_bars, test_trend_continuation_positive_ema_slopes |
| atr_compression_score | consolidation_base | NULL if missing; 0 if current≥mean | test_atr_compression_detected_after_volatile_period |
| pullback_depth_pct | pullback | NULL if < 20 bars | test_pullback_depth_pct_exact |
| swing_high | breakout, pullback, trend_continuation | NULL if < 5 bars or no pivot | TestSwingPivots, test_swing_null_with_fewer_than_5_bars |
| swing_low | pullback, consolidation_base | NULL if < 5 bars or no pivot | TestSwingPivots, test_swing_null_with_fewer_than_5_bars |
| support_level | all setups | NULL if close_adj null | TestSupportResistance, test_support_resistance_exact |
| resistance_level | breakout, consolidation_base | NULL if close_adj null | TestSupportResistance, test_support_resistance_exact |
| next_resistance_level | breakout, pullback | NULL if nothing above resistance | TestSupportResistance, test_next_resistance_above_resistance |
| base_high | consolidation_base, breakout | NULL if < 60 bars or run < 2 | TestComputeBase, test_base_detected_before_breakout |
| base_low | consolidation_base, breakout | same as base_high | TestComputeBase |
| range_width_pct | consolidation_base | NULL if base_low null | test_range_width_pct_formula |
| range_duration | consolidation_base | NULL if run < 2 | test_tight_range_at_end_detected |
| range_tightness_score | consolidation_base | NULL if range_width_pct null | test_range_tightness_score_formula, test_consolidation_high_tightness |
| volume_dry_up_score | consolidation_base, pullback | NULL if means missing; 0 if recent≥60d mean | test_consolidation_high_tightness |
| volume_expansion_score | breakout | NULL if rvol20 null | test_volume_expansion_score_exact, test_volume_expansion_at_1x_is_zero |
| relative_strength_vs_spy | trend_continuation | NULL if SPY data missing | test_relative_strength_vs_spy_exact, test_rs_vs_spy_null_when_spy_absent |
