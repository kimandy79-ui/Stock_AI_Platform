# Module 14 — Step 4 Setup Analysis: Coding Prompt

## Task

Implement **Module 14 — Step 4 Setup Analysis** against frozen `stock_ai_platform_module13_stable.zip`.

Attach to Claude:
1. `stock_ai_platform_module13_stable.zip` — accepted/frozen Modules 01–13.
2. All current Project Files in the Claude Project.

Use `00_PROJECT_FILE_MAP.md` only for targeted retrieval. Do not read archived monolithic FULL/PATCH/MINI PATCH files when split Project Files exist.

**Scope:** implement only Module 14. Do not implement Module 15+.

---

## Token-saving communication policy

Be concise. Do not narrate reasoning. Do not emit chain-of-thought, exploratory logs, or broad summaries.

Allowed output only:
- blocking issues,
- missing dependencies,
- implementation summary,
- added/changed files,
- test results,
- assumptions/gaps,
- final delivery note.

---

## Files

| Action | Path |
|---|---|
| New | `app/services/analysis/__init__.py` |
| New | `app/services/analysis/step4_analysis_engine.py` |
| New | `tests/test_step4_analysis.py` |
| New | `M14_STEP4_ANALYSIS_SPEC.md` |
| Update | `README.md` additive note only |

Do not modify frozen Modules 01–13 except additive README note if required.

---

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

Rules:
- `db_role`: allow only `prod` and `debug`; reject `simulation` and anything else before DB access.
- `run_id`: generate `uuid4` when `None`; preserve supplied value.
- Validate config before DB access.
- `ServiceResult.rows_processed == metadata["analyses_written"]` on every path.

Exact metadata keys on every return:

```text
db_role, signal_date, strategy_config_id, run_id,
candidates_evaluated, analyses_written,
estimated_rr_min, estimated_rr_max, estimated_rr_mean,
setup_type_counts
```

Stats:
- `estimated_rr_{min,max,mean}` over written rows only; `None` if no rows written.
- `setup_type_counts`: `dict[str, int]` over written rows.

---

## Inputs and reads

Process only rows from `step3_candidates` where:
- `signal_date = analyze.signal_date`
- `strategy_config_id = analyze.strategy_config_id`
- `passed_hard_filters = TRUE`

Required data:
- `daily_features_current` on `(ticker, feature_date = signal_date)`.
- `daily_prices` on `(ticker, date = signal_date)` for `close_raw`, `close_adj`, `open_raw`, `high_raw`, `low_raw`.
- Last up to 20 `daily_prices.low_raw` rows where `ticker = candidate.ticker` and `date <= signal_date`; use `MIN(low_raw)` as `recent_20d_low_raw`.
- For `trend_resume`, read prior 10 trading rows from `daily_features` / available feature history with `close_adj` and `ema20` before `signal_date`.

Use read-only connection for reads.

If there are no qualifying Step 3 candidates: return `success`, zero counts, RR stats `None`, `setup_type_counts = {}`, and no insert.

If a candidate lacks required current feature/price data needed to calculate entry/stop/output, do not write a partial row. Treat it as not analyzable; document this in the spec and keep counts consistent.

---

## Output table

Write one row per analyzable qualifying Step 3 candidate to `step4_analysis`:

```sql
analysis_id VARCHAR PRIMARY KEY,      -- fresh uuid4
candidate_id VARCHAR NOT NULL,        -- from step3_candidates
run_id VARCHAR NOT NULL,
strategy_config_id VARCHAR NOT NULL,
ticker VARCHAR NOT NULL,
signal_date DATE NOT NULL,
setup_type VARCHAR,
setup_score DOUBLE,                   -- 0–100
breakout_quality_score DOUBLE,         -- 0–100
squeeze_score DOUBLE,                  -- 0–100
timing_score DOUBLE,                   -- 0–100
confirmation_score DOUBLE,             -- 0–100
estimated_rr DOUBLE,
stop_price_raw DOUBLE,
target_price_raw DOUBLE,
earnings_penalty DOUBLE,               -- <= 0
macro_penalty DOUBLE,                  -- <= 0
explanation_json JSON,
created_at TIMESTAMP NOT NULL          -- DB now()
```

---

## Config validation before DB access

Required paths and types:

```text
step4.target_R                         number > 0
earnings.avoid_within_bd               int >= 0
earnings.penalty_points_max            number <= 0
macro_event_risk.enabled               bool
macro_event_risk.penalty_points         number <= 0
screening.min_rvol                     number > 0
```

Missing/wrong type/range → `failed`, zero counts, before DB access.

`step4.target_R` must be present in the concrete strategy config. Do not infer risk-profile defaults inside the engine unless the existing Project Files already provide that convention.

---

## Core formulas

### Entry

```text
entry_proxy_raw = close_raw on signal_date
```

No look-ahead. Do not use next-day open.

### ATR raw-equivalent

```text
atr14_raw_equivalent = atr14 * (close_raw / close_adj)
```

Use adjusted ratio only when `atr14`, `close_raw`, and non-zero `close_adj` are present; otherwise use `atr14` directly. If `atr14` is missing, use a safe fallback that prevents invalid stop calculation and document it.

### Stop

```text
stop_price_raw = min(recent_20d_low_raw, entry_proxy_raw - 1.5 * atr14_raw_equivalent)
```

Use available rows if fewer than 20 exist.

Guard: if stop is missing, invalid, or `stop_price_raw >= entry_proxy_raw`, set:

```text
stop_price_raw = entry_proxy_raw * 0.95
stop_clamped = true
```

Otherwise `stop_clamped = false`.

### Target and RR

```text
target_price_raw = entry_proxy_raw + target_R * (entry_proxy_raw - stop_price_raw)
estimated_rr = (target_price_raw - entry_proxy_raw) / (entry_proxy_raw - stop_price_raw)
```

If RR denominator `<= 0`, set `estimated_rr = None`.

---

## Setup type classification

Evaluate in priority order. First match wins. NULL required inputs make that condition false.

| Priority | setup_type | Condition |
|---|---|---|
| 1 | `high_tight_flag` | `roc20 > 0.15` and `consolidation_score >= 60` |
| 2 | `breakout` | `-0.5 <= breakout_proximity <= 0.5` and `rvol20 >= config["screening"]["min_rvol"]` |
| 3 | `volatility_squeeze` | `consolidation_score >= 70` and `atr_contraction_proxy is True` |
| 4 | `trend_pullback` | `close_adj > ema200` and `-0.12 <= pullback_from_recent_high_pct <= -0.03` and `ema20 > ema50` |
| 5 | `trend_resume` | AD-22.16 rule below |
| 6 | `unknown` | fallback |

`atr_contraction_proxy`: because `atr_contraction` is not stored, set true when `consolidation_score >= 70`. Document as open assumption `G-ATR-CONTRACTION`.

`trend_resume` AD-22.16:
- current `close_adj > ema20`, and
- `pullback_from_recent_high_pct` is between `-0.20` and `-0.03`, and
- in the prior 10 trading rows before `signal_date`, `close_adj < ema20` for at least 3 rows.

If prior-10 history is unavailable, skip `trend_resume` and continue to fallback.

---

## Scores, all clamped 0–100

Use points consistently. Sub-scores are 0–100. Weighted components use decimal weights.

### breakout_quality_score

```text
breakout_position_sub = 100 if -0.5 <= breakout_proximity <= 0.5
                        else max(0, 100 * (1 - abs(breakout_proximity) / 2))
```

`breakout_proximity = NULL` → `breakout_position_sub = 0`.

```text
rvol_sub = 100 if rvol20 >= 2.0
           70  if 1.5 <= rvol20 < 2.0
           40  if 1.2 <= rvol20 < 1.5
           0   otherwise or NULL

breakout_quality_score = 0.5 * breakout_position_sub + 0.5 * rvol_sub
```

### squeeze_score

```text
squeeze_score = consolidation_score
```

NULL → 0.

### timing_score

```text
rsi_timing_sub = 100 if 50 <= rsi14 <= 65
                 70  if 45 <= rsi14 < 50 or 65 < rsi14 <= 70
                 30  otherwise
                 0   if NULL

ema_alignment_sub = ema_alignment_score      # already 0/50/100; NULL -> 0

sector_rs_sub = 100 if sector_relative_strength > 0.05
                70  if 0 <= sector_relative_strength <= 0.05
                30  if -0.05 <= sector_relative_strength < 0
                0   if sector_relative_strength < -0.05
                50  if NULL

timing_score = 0.4 * rsi_timing_sub + 0.3 * ema_alignment_sub + 0.3 * sector_rs_sub
```

### confirmation_score

```text
confirmation_score =
    50 * indicator(close_adj > ema200) +
    50 * indicator(ema20 > ema50)
```

NULL condition inputs → indicator `0`.

### setup_score

```text
setup_quality = mean(breakout_quality_score, squeeze_score, timing_score, confirmation_score)
setup_score = clamp(setup_quality + earnings_penalty + macro_penalty, 0, 100)
```

Equal-weight mean is required. Document as `G-SCORING-SUBCOMPONENT-WEIGHTS`.

---

## Penalties

### Earnings penalty

Config:
- `earnings.avoid_within_bd`
- `earnings.penalty_points_max` where value is negative or zero.

```python
if days_to_earnings_bd is None:
    earnings_penalty = 0.0
elif avoid_within_bd == 0:
    earnings_penalty = penalty_points_max if days_to_earnings_bd == 0 else 0.0
elif days_to_earnings_bd <= avoid_within_bd:
    earnings_penalty = penalty_points_max * (1 - days_to_earnings_bd / avoid_within_bd)
else:
    earnings_penalty = 0.0
```

Always clamp/ensure `earnings_penalty <= 0`.

### Macro penalty

```python
if config["macro_event_risk"]["enabled"] is True and macro_event_risk_flag is True:
    macro_penalty = config["macro_event_risk"]["penalty_points"]
else:
    macro_penalty = 0.0
```

Always clamp/ensure `macro_penalty <= 0`.

---

## explanation_json

Store sorted-key JSON with at least:

```json
{
  "setup_type": "<classified>",
  "entry_proxy_raw": 0.0,
  "stop_price_raw": 0.0,
  "target_price_raw": 0.0,
  "target_R": 0.0,
  "atr14_raw_equivalent": null,
  "recent_20d_low_raw": null,
  "stop_clamped": false,
  "earnings_penalty": 0.0,
  "macro_penalty": 0.0,
  "days_to_earnings_bd": null,
  "macro_event_risk_flag": null
}
```

---

## Architecture rules

- No `duckdb` import in the engine module.
- No provider imports.
- No `print()` in library modules.
- No SQL `ATTACH`, `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`.
- All DB access through approved `db_manager` layer.
- Reads use read-only connection.
- Writes: only `INSERT INTO step4_analysis`.
- No update/delete against `step4_analysis`; no writes to other tables.
- Write rows with per-row `execute()` inside one `BEGIN TRANSACTION / COMMIT`.
- On write failure: `ROLLBACK`; no partial rows.

---

## Tests: `tests/test_step4_analysis.py`

Offline only. Use `tmp_path`, monkeypatch/fake `db_manager`, and the real schema setup pattern from `tests/test_step3_screening.py`. No real prod/debug/simulation DB files.

Required coverage:

### API / guards
- Exact `analyze` signature.
- `run_id` minted/preserved.
- Exact metadata keys on success/failure.
- `rows_processed == analyses_written` on every path.
- Invalid roles and `simulation` rejected before DB access.
- Bad/missing config rejected before DB access.
- Read failure returns `failed`, zero counts.

### Inputs
- Only passed Step 3 candidates processed.
- Failed Step 3 candidates ignored.
- Empty qualifying input → success, zero counts, no insert, RR stats `None`, counts `{}`.
- Signal-date and strategy-config isolation.
- `debug` role supported.

### Stop / target / RR
- Stop always below entry after guard.
- Target above entry.
- RR positive/finite when denominator valid.
- Stop clamp writes `stop_clamped = true`.
- ATR raw-equivalent uses adjusted ratio and fallback path.
- Recent 20-day low uses available rows when fewer than 20.

### Classification
- Each setup type assigned correctly: `high_tight_flag`, `breakout`, `volatility_squeeze`, `trend_pullback`, `trend_resume`, `unknown`.
- Priority order respected.
- NULL inputs make condition false.
- `trend_resume` assigned with valid prior-10 history.
- Missing trend-resume history falls through.

### Scoring / penalties
- Component scores and setup_score formulas verified.
- Boundaries for RSI, RVOL, breakout proximity, sector RS, pullback, and confirmation.
- Earnings penalty: NULL, `avoid_within_bd = 0`, day 0, boundary day, and outside window.
- Macro penalty: enabled/disabled and flag True/False/NULL.
- Sector RS NULL → neutral 50.
- Score clamps to 0–100.

### Writes / metadata / static scans
- Single transaction rollback leaves zero rows.
- Only `step4_analysis` written.
- Unique valid uuid4 `analysis_id` per row.
- `candidate_id` preserved from Step 3.
- `setup_type_counts` correct.
- RR min/max/mean over written rows; `None` when zero.
- `analyses_written == len(step4 rows)`.
- Static scans: no `duckdb`, providers, `print`, `ATTACH`, DDL; only `INSERT INTO step4_analysis`.

---

## Spec file

Create `M14_STEP4_ANALYSIS_SPEC.md` documenting:
- purpose and non-scope;
- public API and exact metadata keys;
- input joins and missing-data behavior;
- formulas and setup classification priority;
- scoring and penalties;
- `explanation_json` fields;
- config validation;
- transaction model;
- tests summary;
- open assumptions below.

Open assumptions to document, not blockers:
- `G-ATR-CONTRACTION`: no stored `atr_contraction`; use `consolidation_score >= 70` proxy.
- `G-TREND-RESUME-HISTORY`: requires prior-10 feature history; skip if unavailable.
- `G-SCORING-SUBCOMPONENT-WEIGHTS`: equal-weight mean across four setup components.
- `G-MISSING-ATR-OR-PRICE`: if stop inputs are incomplete, use documented safe clamp/fallback or skip unanalyzable candidate consistently.

---

## Output required

1. Updated zip: `stock_ai_platform_module14_stable.zip`.
2. Added/changed files list.
3. `M14_STEP4_ANALYSIS_SPEC.md`.
4. Short design notes.
5. Test commands and results:
   ```bash
   pytest -q tests/test_step4_analysis.py
   pytest -q
   ```
6. Assumptions/gaps/blockers.
7. Suggested commit message: `module14_step4_analysis_stable`.
