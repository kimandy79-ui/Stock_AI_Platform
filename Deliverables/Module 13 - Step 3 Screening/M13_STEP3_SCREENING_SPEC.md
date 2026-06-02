# M13 — Step 3 Screening Spec

Module-specific source of truth for **Module 13 — Step 3 Screening**
(`app/services/screening/step3_screening.py`). Subordinate to the split Project
Files; where this spec closes an open gap it says so explicitly and never
overrides a higher-priority document.

## 1. Purpose and non-scope

**Purpose.** For one `signal_date`, read every `daily_features_current` row,
join `ticker_master` (symbol type) and `daily_prices` (close + data-quality),
apply the Step 3 hard filters and — for the passing rows — the Step 3 soft
score, then append **one `step3_candidates` row per evaluated ticker** (passed
*and* failed) in a single transaction.

**Non-scope.** No Step 4+ logic (setup analysis, proposals, outcomes,
simulation, AI review, dashboard). Module 13 only ever *inserts* into
`step3_candidates`: it never updates/deletes existing candidate rows, writes any
other table, runs DDL, calls providers, imports `duckdb`, uses `ATTACH`,
bypasses the DuckDB manager, or uses `print()`.

## 2. Source references

- Hard filters, sub-scores, weights, market mapping → `01c_FORMULAS_AND_CONFIGS.md`
  §`FORMULAS/61_Scoring_Formulas_Step3.md`; config shape §`CONFIG/20_Config_Base_Normal.json`.
- `step3_candidates`, `daily_features`, `daily_features_current` view,
  `daily_prices`, `ticker_master` → `01b_SCHEMA_AND_DATA.md`.
- Pipeline position (after M12 Market Regime, before M14 Step 4) → `01d_MODULES_AND_PIPELINE.md`.
- Coding / logging / Polars-first / DB-boundary rules → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`.
- Regime enum / guardrails → `01a_CORE_PRINCIPLES.md`; `db_role` / service style → `M12_MARKET_REGIME_ENGINE_SPEC.md`.

## 3. Public API

```python
class Step3ScreeningEngine:
    def __init__(self, db_manager=None) -> None: ...

    def screen(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

- `db_role` accepts only `prod` / `debug`; `simulation` and any other value are
  rejected **before** any DB access (`failed`, zero counts).
- `run_id` is minted (`uuid4`) when `None`; a supplied value is preserved.
- Required config keys are validated before DB access (see §6).
- `ServiceResult.rows_processed == metadata["candidates_written"]` on **every**
  return path (it equals the durably-written count; `0` on any failure).

### Exact metadata keys

`metadata` contains exactly:

```
db_role, signal_date, strategy_config_id, run_id,
tickers_evaluated, passed_hard_filters, failed_hard_filters,
candidates_written, screening_score_min, screening_score_max, screening_score_mean
```

`screening_score_{min,max,mean}` are computed over **passed candidates only**;
all three are `None` when no candidate passes the hard filters.

## 4. Input joins and signal-date behavior

Read from the `daily_features_current` view (current feature schema version only)
for `feature_date == signal_date`, with:

- `LEFT JOIN ticker_master tm ON tm.ticker = f.ticker` → `symbol_type`;
- `LEFT JOIN daily_prices p ON p.ticker = f.ticker AND p.date = f.feature_date`
  → `close_raw`, `close_adj`, `data_quality_status`.

Left joins ensure rows with a missing `ticker_master` / `daily_prices` match
still appear and **fail** the relevant hard filter rather than disappearing.

If no `daily_features_current` rows exist for `signal_date`: return `success`
with all counts `0`, score stats `None`, and **no insert**.

Reads use a read-only connection and only the columns needed for filtering,
scoring, and the snapshot. Scoring/filtering is **Polars-vectorized** (no
ticker-by-ticker Python loops); per-row Python iteration is used only to assemble
JSON / UUID insert payloads from the already-scored frame.

## 5. Hard filters and exact reason labels

All six filters are evaluated for every row; *all* failing labels are collected
into `hard_filter_fail_reasons` (deterministic order = the order below). NULL /
missing inputs fail the related filter.

| Condition (pass) | Fail label |
|---|---|
| `feature_ready = TRUE` | `feature_not_ready` |
| `ticker_master.symbol_type = 'stock'` | `not_stock` |
| `close_raw >= universe.min_price` | `price_below_min` |
| `avg_dollar_volume_20d >= universe.min_avg_dollar_volume_20d` | `avg_dollar_volume_below_min` |
| `rvol20 >= screening.min_rvol` | `rvol_below_min` |
| `daily_prices.data_quality_status = 'ok'` | `data_quality_not_ok` |

- **Passed** rows: `passed_hard_filters = TRUE`, `hard_filter_fail_reasons = []`,
  `screening_score` non-null.
- **Failed** rows: `passed_hard_filters = FALSE`, `hard_filter_fail_reasons`
  populated, `screening_score = NULL`.
- Both passed and failed rows are written.

## 6. Scoring formula, weights, sub-score sources, neutral fallbacks

Only passed rows are scored. Sub-scores are clamped to 0–100 and combined with
the caller's weights:

```
screening_score = w.trend*trend + w.momentum*momentum
                + w.setup*setup + w.volume*volume + w.market*market   (clamped 0–100)
```

Sub-score rules are taken verbatim from `FORMULAS/61_Scoring_Formulas_Step3.md`:

- **trend** = 50%·`ema_alignment_score` + 25%·`distance_to_ema50_pct` band
  + 25%·(close above EMA200 ? 100 : 0).
- **momentum** = 40%·RSI14 band + 30%·ROC20 band + 30%·sector-RS band
  (missing `sector_relative_strength` → neutral **50**, *not* a warning).
- **setup** = 40%·`consolidation_score` + 30%·`breakout_proximity` band
  + 30%·`pullback_from_recent_high_pct` band.
- **volume** = `rvol20` band (see gap G-VOL-ADV).
- **market** = regime map `bull=100, neutral=60, bear=20, high_risk=0,
  extreme_risk=0`; unknown / NULL regime → neutral **50**, recorded in
  `soft_score_components` (`market_regime_known=false`).

### Config weight usage (gap G-WEIGHTS-PATH)

The prompt lists `screening.scoring_weights`, but Project Files
(`CONFIG/20_Config_Base_Normal.json`) place `scoring_weights` at the **config top
level** with sub-keys `trend / momentum / setup / volume / market`. Per the
precedence rule the implementation follows Project Files: weights are read from
`config.scoring_weights.*`. Required validated keys:
`universe.min_price`, `universe.min_avg_dollar_volume_20d`, `screening.min_rvol`,
and `scoring_weights.{trend,momentum,setup,volume,market}`. The default-config
weights (`0.30/0.25/0.20/0.15/0.10`) reproduce the documented closed-form formula
in FORMULAS/61.

## 7. `soft_score_components` and `feature_snapshot_json`

Both are `json.dumps(..., sort_keys=True)` (deterministic, parseable).

- `soft_score_components`: `trend_score, momentum_score, setup_score,
  volume_score, market_score, market_regime, market_regime_known,
  sector_relative_strength`.
- `feature_snapshot_json` (minimum required fields): `ema20, ema50, ema200,
  ema_alignment_score, rsi14, roc20, rvol20, avg_dollar_volume_20d,
  breakout_proximity, pullback_from_recent_high_pct, consolidation_score,
  sector_relative_strength, market_regime, close_raw`.

## 8. Write ownership, transaction model, idempotency

- `candidate_id`: fresh `uuid4` per row. `run_id`: caller/minted. `signal_date`:
  `daily_features_current.feature_date`. `created_at`: DB `now()`.
- All candidate rows are written using per-row `execute()` calls inside a single
  `BEGIN TRANSACTION / COMMIT` (not `executemany`). On any read/config/guard/write
  failure the result is `failed`
  with `rows_processed = 0` and `candidates_written = 0`; a write failure
  triggers `ROLLBACK` so **no partial candidate rows remain**.
- **Append-only.** Module 13 never updates/deletes existing `step3_candidates`
  rows; re-running for the same `signal_date` appends a new evaluated batch
  (distinguished by `run_id`). De-duplication, if ever needed, belongs to a
  downstream/orchestration concern, not Module 13.

## 9. Tests

`tests/test_step3_screening.py` (offline; temp DuckDB via `tmp_path` +
`monkeypatch`, real Module 03 schema). Covers: exact signature, run_id
mint/preserve, exact metadata keys, `rows_processed == candidates_written`;
guard/config/read/write failure paths incl. `simulation` rejected before DB
access; empty-input success with no insert; hard-filter pass and each failure
label incl. NULL/missing-row behavior; **explicit NULL tests for `rvol20` and
`avg_dollar_volume_20d`** triggering `rvol_below_min` / `avg_dollar_volume_below_min`
respectively; all-failures-collected and deterministic reason order; passed vs
failed `screening_score` / `passed_hard_filters` / `hard_filter_fail_reasons`
semantics; score reproducibility; **`breakout_proximity` boundary tests** covering
the ideal-band edges (±1, 0.5), mid-band gap value (1.0 → 30), and above-1.5 (20);
**`distance_to_ema50_pct` taper boundary tests** at in-band edges, mid-taper (50),
taper-edge (0), and beyond-taper (0); market-regime mapping for all five enums;
unknown/NULL regime neutral-50 + audit; missing sector-RS neutral-50 with no
warning; custom weights; score stats over passed only (and `None` when none pass);
deterministic parseable JSON; one-transaction write and rollback-leaves-none; write
ownership (only `step3_candidates` changes); unique `candidate_id`s; signal-date
isolation; debug role; static scans (no `duckdb` import, `ATTACH`, DDL,
non-`step3_candidates` writes, provider imports, or `print()`).

## 10. Assumptions, open gaps, and blockers

- **G-WEIGHTS-PATH** (closed): `scoring_weights` lives at config top level per
  Project Files, not under `screening`. Followed Project Files; documented above.
- **G-VOL-ADV** (open, conservatively closed): FORMULAS/61 weights volume 60%
  RVOL + 40% `avg_dollar_volume_20d` but gives **no** dollar-volume sub-score
  mapping. Per "implement only the explicitly supported subset", the
  dollar-volume sub-component is omitted and `volume_score` = the RVOL sub-score
  alone. Revisit if a mapping is added to Project Files.
- **G-EMA50-TAPER** (gap, closed by assumption A-EMA50-TAPER): FORMULAS/61 says
  the `distance_to_ema50` sub-score "declines linearly" outside `[-3%, +8%]` but
  gives no slope. Implemented as a linear taper from 100→0 across a `0.10`
  fractional window beyond each band edge (clamped 0–100). Deterministic and
  reversible if a slope is specified.
- **G-BP-MIDBAND** (gap, closed): FORMULAS/61 leaves `breakout_proximity` in
  `(0.5, 1.5]` unspecified (it defines `≤0.5` → 100 and `>1.5` → 20). Scored
  **30** for that mid-band; documented and reversible.
- **A-CLOSE-EMA200**: the trend "close above EMA200" comparison uses
  `coalesce(close_adj, close_raw)` to match the adjusted basis of the EMAs; the
  `price_below_min` hard filter and the snapshot use `close_raw` as specified.
- **A-NULL-COMPONENT**: because `feature_ready = TRUE` gates passed rows, scoring
  inputs are expected non-null. Defensively, a NULL input other than
  `sector_relative_strength` (→50) and `market_regime` (→50) drives its component
  to 0.
- **Blocker (environment only, not code):** the offline sandbox used to author
  this module has no `polars`, `duckdb`, or `pytest` and no network to install
  them, so the suite could not be executed here. Static scans, compile, API
  signature, scoring math (scalar mirror), and SQL/column correctness against the
  schema were verified. Run `pytest tests/test_step3_screening.py` in the project
  environment (which has these deps) to execute the full suite.
