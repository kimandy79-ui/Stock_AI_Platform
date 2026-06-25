# M14 — Step 4 Setup Analysis Specification

## Purpose and non-scope

Module 14 implements **Step 4 Setup Analysis**. For a given `signal_date` and
`strategy_config_id`, it reads the qualifying Step 3 candidates (those that
passed the hard filters), computes a setup classification, quality/timing/
confirmation scores, a risk-based stop/target/RR, and earnings/macro penalties,
then writes exactly one `step4_analysis` row per *analyzable* candidate.

Non-scope:
- No screening, ranking, sizing, ordering, or portfolio logic (Modules 13 / 15+).
- No DDL: the `step4_analysis` table already exists in the frozen schema; the
  engine only `INSERT`s into it.
- No provider / network / file-system access.
- Modules 01–13 are frozen and untouched (additive README note only).

## Public API

```python
class Step4AnalysisEngine:
    def __init__(self, db_manager=None) -> None: ...

    def analyze(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

- `db_manager` defaults to the shared `duckdb_manager` instance when `None`.
- `db_role`: only `prod` and `debug` are allowed. `simulation` and any other
  value are rejected **before** any DB access (returns `failed`).
- `run_id`: a `uuid4` string is minted when `None`; a supplied value is
  preserved verbatim.
- Config is validated **before** any DB access.

### Exact metadata keys (present on every return path)

```text
db_role, signal_date, strategy_config_id, run_id,
candidates_evaluated, analyses_written,
estimated_rr_min, estimated_rr_max, estimated_rr_mean,
setup_type_counts
```

- `signal_date` is the ISO date string.
- `candidates_evaluated` counts qualifying Step 3 candidates inspected
  (including those skipped as not analyzable).
- `analyses_written` counts rows actually inserted.
- `estimated_rr_{min,max,mean}` are computed over **written rows only** and are
  `None` when no rows were written.
- `setup_type_counts` is a `dict[str, int]` over written rows (`{}` when none).
- `ServiceResult.rows_processed == metadata["analyses_written"]` on every path.

## Inputs, joins, and missing-data behavior

Qualifying Step 3 candidates are those in `step3_candidates` with
`signal_date = analyze.signal_date`, `strategy_config_id =
analyze.strategy_config_id`, and `passed_hard_filters = TRUE`.

Per candidate, reads (read-only connection):
- `daily_features_current` joined to `daily_prices` on
  `(ticker, feature_date = signal_date)` / `(ticker, date = signal_date)` for
  the current feature row plus `close_raw, close_adj, open_raw, high_raw,
  low_raw`.
- `recent_20d_low_raw = MIN(low_raw)` over the last ≤20 `daily_prices` rows for
  the ticker with `date <= signal_date` (uses fewer rows if <20 exist).
- For `trend_resume`: the prior ≤10 feature rows (`close_adj`, `ema20`) with
  `date < signal_date`.

Missing-data rules:
- **No qualifying candidates** → `success`, all counts zero, RR stats `None`,
  `setup_type_counts = {}`, no insert.
- **Candidate lacks a current feature row, or has no usable `close_raw`**
  (entry `<= 0` / `None`) → treated as *not analyzable*: counted in
  `candidates_evaluated`, **not** written, no partial row. (`G-MISSING-ATR-OR-PRICE`.)

## Core formulas

- **Entry:** `entry_proxy_raw = close_raw` on `signal_date` (no look-ahead).
- **ATR raw-equivalent:**
  `atr14_raw_equivalent = atr14 * (close_raw / close_adj)` when `atr14`,
  `close_raw`, and non-zero `close_adj` are all present; otherwise `atr14`
  directly; otherwise `None` (forces the stop clamp). (`G-MISSING-ATR-OR-PRICE`.)
- **Stop:** `min(recent_20d_low_raw, entry - 1.5 * atr14_raw_equivalent)` over
  available candidates. If the result is missing/invalid or `>= entry`, clamp to
  `entry * 0.95` and set `stop_clamped = true`; otherwise `stop_clamped = false`.
- **Target / RR:**
  `target = entry + target_R * (entry - stop)`;
  `estimated_rr = (target - entry) / (entry - stop)`; `None` if denominator `<= 0`.

## Setup classification (priority order, first match wins)

NULL required inputs make a condition false.

| # | setup_type | Condition |
|---|---|---|
| 1 | `high_tight_flag` | `roc20 > 0.15` and `consolidation_score >= 60` |
| 2 | `breakout` | `-0.5 <= breakout_proximity <= 0.5` and `rvol20 >= min_rvol` |
| 3 | `volatility_squeeze` | `consolidation_score >= 70` and `atr_contraction_proxy` |
| 4 | `trend_pullback` | `close_adj > ema200` and `-0.12 <= pullback_from_recent_high_pct <= -0.03` and `ema20 > ema50` |
| 5 | `trend_resume` | AD-22.16 (below) |
| 6 | `unknown` | fallback |

- `atr_contraction_proxy` is `True` when `consolidation_score >= 70`
  (`G-ATR-CONTRACTION`).
- **AD-22.16 `trend_resume`:** `close_adj > ema20`, and
  `-0.20 <= pullback_from_recent_high_pct <= -0.03`, and in the prior 10 rows
  `close_adj < ema20` for at least 3 rows. If prior-10 history is unavailable,
  `trend_resume` is skipped and classification falls through
  (`G-TREND-RESUME-HISTORY`).

## Scores (all clamped 0–100)

- **breakout_quality_score** `= 0.5 * breakout_position_sub + 0.5 * rvol_sub`.
  `breakout_position_sub = 100` if `-0.5 <= breakout_proximity <= 0.5`, else
  `max(0, 100 * (1 - abs(breakout_proximity) / 2))`, `0` if NULL.
  `rvol_sub`: 100 (`>=2.0`), 70 (`1.5–2.0`), 40 (`1.2–1.5`), else/NULL 0.
- **squeeze_score** `= consolidation_score` (NULL → 0).
- **timing_score** `= 0.4 * rsi_timing_sub + 0.3 * ema_alignment_sub + 0.3 * sector_rs_sub`.
  `rsi_timing_sub`: 100 (`50–65`), 70 (`45–50` or `65–70`), 30 otherwise, 0 NULL.
  `ema_alignment_sub = ema_alignment_score` (0/50/100; NULL → 0).
  `sector_rs_sub`: 100 (`>0.05`), 70 (`0–0.05`), 30 (`-0.05–0`), 0 (`<-0.05`),
  **50 if NULL** (neutral).
- **confirmation_score** `= 50 * I(close_adj > ema200) + 50 * I(ema20 > ema50)`
  (NULL inputs → indicator 0).
- **setup_score** `= clamp(mean(breakout_quality, squeeze, timing,
  confirmation) + earnings_penalty + macro_penalty, 0, 100)`. Equal-weight mean
  is required (`G-SCORING-SUBCOMPONENT-WEIGHTS`).

## Penalties (both ensured `<= 0`)

- **Earnings:** `0.0` if `days_to_earnings_bd is None`. If `avoid_within_bd == 0`:
  `penalty_points_max` when `days_to_earnings_bd == 0`, else `0.0`. Else if
  `days_to_earnings_bd <= avoid_within_bd`:
  `penalty_points_max * (1 - days_to_earnings_bd / avoid_within_bd)`; else `0.0`.
- **Macro:** `penalty_points` when `macro_event_risk.enabled is True` **and**
  `macro_event_risk_flag is True`; otherwise `0.0`.

## explanation_json

Sorted-key JSON containing at least: `setup_type`, `entry_proxy_raw`,
`stop_price_raw`, `target_price_raw`, `target_R`, `atr14_raw_equivalent`,
`recent_20d_low_raw`, `stop_clamped`, `earnings_penalty`, `macro_penalty`,
`days_to_earnings_bd`, `macro_event_risk_flag`.

## Config validation (before DB access)

| Path | Type / range |
|---|---|
| `step4.target_R` | number `> 0` |
| `earnings.avoid_within_bd` | int `>= 0` |
| `earnings.penalty_points_max` | number `<= 0` |
| `macro_event_risk.enabled` | bool |
| `macro_event_risk.penalty_points` | number `<= 0` |
| `screening.min_rvol` | number `> 0` |

Any missing path, wrong type, or out-of-range value → `failed`, zero counts,
before DB access. `step4.target_R` must be present in the concrete config; the
engine does not infer risk-profile defaults.

## Transaction model

- Reads use a read-only connection.
- Writes are limited to `INSERT INTO step4_analysis`, one per row via
  `execute()`, inside a single `BEGIN TRANSACTION` / `COMMIT`.
- On any write error: `ROLLBACK`, no partial rows, `failed` with zero
  `analyses_written`.
- No `UPDATE`/`DELETE`, no writes to any other table, no DDL/`ATTACH`.
- `analysis_id` is a fresh `uuid4` per row; `candidate_id` is preserved from
  Step 3; `created_at` is the DB `now()`.

## Tests summary (`tests/test_step4_analysis.py`)

Offline only, using the `tmp_db_paths` schema-setup pattern from
`tests/test_step3_screening.py` (no real prod/debug/simulation DB files).
Coverage: API/signature/`run_id` mint+preserve, exact metadata keys and
`rows_processed == analyses_written` on success and failure, role guards
(`simulation`/invalid rejected pre-DB), parametrized config validation,
candidate filtering (passed only), empty input, signal-date/config isolation,
`debug` role, not-analyzable skip, stop/target/RR + clamp, ATR raw-equivalent
ratio and fallback, recent-20d-low with <20 rows, all six classifications +
priority + NULL + trend-resume history present/absent, component-score and
setup_score boundaries, earnings penalty (NULL / `avoid_within_bd=0` / day 0 /
boundary / outside), macro penalty (enabled/disabled, flag True/False/NULL),
sector-RS NULL → 50, score clamps, single-transaction rollback, only
`step4_analysis` written, unique uuid4 ids, `candidate_id` preserved,
`setup_type_counts` and RR stats, and ast-based static scans (no `duckdb`,
providers, `print`, `ATTACH`, DDL; only `INSERT INTO step4_analysis`).

## Open assumptions (documented, not blockers)

- **G-ATR-CONTRACTION:** no stored `atr_contraction`; `consolidation_score >= 70`
  is used as the proxy for `volatility_squeeze`.
- **G-TREND-RESUME-HISTORY:** `trend_resume` requires prior-10 feature history;
  it is skipped (falls through) when unavailable.
- **G-SCORING-SUBCOMPONENT-WEIGHTS:** equal-weight mean across the four setup
  components for `setup_score`.
- **G-MISSING-ATR-OR-PRICE:** if ATR/price stop inputs are incomplete, the stop
  uses the documented `entry * 0.95` safe clamp; a candidate with no current
  feature row or no usable `close_raw` is skipped as not analyzable, kept
  consistent across counts.
