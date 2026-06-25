# Claude Coding Prompt — Module 13: Step 3 Screening

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not repeat or summarize global rules.
Work silently and return only actionable results.

## Attached

1. `stock_ai_platform_module12_stable.zip` — implementation base.
2. `M12_MARKET_REGIME_ENGINE_SPEC.md` — frozen Module 12 reference only.

Current codebase: Modules 01–12 are accepted and frozen.

## Task

Implement only **Module 13 — Step 3 Screening**.

Module 13 reads `daily_features_current`, joins `ticker_master` and `daily_prices`, applies Step 3 hard filters and soft scoring, then appends all evaluated rows to `step3_candidates`.

Do not implement Module 14 or later.

## Source retrieval hints

Retrieve only what is needed:

- Step 3 hard filters, sub-score thresholds, weights, and market-score mapping → `01c_FORMULAS_AND_CONFIGS.md`
- `step3_candidates`, `daily_features_current`, `daily_features`, `daily_prices`, `ticker_master` schema → `01b_SCHEMA_AND_DATA.md`
- Module 13 responsibility and pipeline position → `01d_MODULES_AND_PIPELINE.md`
- coding, logging, testing, Polars-first, and boundary rules → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- current `db_role` / service style patterns → `M12_MARKET_REGIME_ENGINE_SPEC.md`

If a scoring threshold, enum mapping, schema column, or config path is missing or contradictory, do not guess. Report the gap in `M13_STEP3_SCREENING_SPEC.md` and implement only the explicitly supported subset.

## Public API

Implement exactly:

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

Rules:

- `strategy_config` is the parsed strategy config JSON.
- Validate required config keys before DB access where possible.
- `db_role` accepts only `prod` and `debug`; reject `simulation` and all other values before DB access.
- Mint `run_id` when `None`; preserve caller-supplied `run_id`.
- `ServiceResult.rows_processed == metadata["candidates_written"]` on every return path.

Required config paths:

```text
universe.min_price
universe.min_avg_dollar_volume_20d
screening.min_rvol
screening.scoring_weights
```

If Project Files define `scoring_weights` under a different path, follow Project Files and document the difference.

## Exact metadata keys

`ServiceResult.metadata` must contain exactly:

```text
db_role
signal_date
strategy_config_id
run_id
tickers_evaluated
passed_hard_filters
failed_hard_filters
candidates_written
screening_score_min
screening_score_max
screening_score_mean
```

Score min/max/mean are computed over passed candidates only. If no candidates pass hard filters, use `None`.

## Scope and ownership

Expected files:

```text
app/services/screening/__init__.py
app/services/screening/step3_screening.py
tests/test_step3_screening.py
M13_STEP3_SCREENING_SPEC.md
README.md                         # short Module 13 note only
```

Do not modify frozen Modules 01–12 unless a failing test or real integration blocker requires it. Explain any such change and keep it minimal.

Module 13 may only insert into `step3_candidates`.

Module 13 must not update/delete existing `step3_candidates` rows, write other tables, run DDL, call providers, import `duckdb`, use `ATTACH`, bypass the DuckDB manager, or use `print()` in library code.

## Locked behavior

### Input rows

Read `daily_features_current` for `signal_date`, join:

- `ticker_master` by `ticker`;
- `daily_prices` by `(ticker, date = feature_date)` to get `data_quality_status`.

If no `daily_features_current` rows exist for `signal_date`, return `success` with zero counts and no insert.

Use Polars for in-memory scoring and filtering. Do not use ticker-by-ticker Python loops for feature reads or scoring. Query only needed columns.

### Hard filters

Evaluate all filters and collect all failing labels in `hard_filter_fail_reasons` as a deterministic JSON array.

Required filters and labels:

```text
feature_ready = TRUE                                      -> feature_not_ready
ticker_master.symbol_type = 'stock'                       -> not_stock
close_raw >= config.universe.min_price                    -> price_below_min
avg_dollar_volume_20d >= config.universe.min_avg_dollar_volume_20d -> avg_dollar_volume_below_min
rvol20 >= config.screening.min_rvol                       -> rvol_below_min
daily_prices.data_quality_status = 'ok'                   -> data_quality_not_ok
```

NULL or missing values fail the related filter. Missing/non-ok `daily_prices.data_quality_status` fails `data_quality_not_ok`.

Passed rows get:

```text
passed_hard_filters = TRUE
hard_filter_fail_reasons = []
screening_score = non-null
```

Failed rows get:

```text
passed_hard_filters = FALSE
hard_filter_fail_reasons = populated JSON array
screening_score = NULL
```

Both passed and failed candidates are written.

### Soft scoring

Only passed rows are scored.

Use Step 3 formulas from `FORMULAS/61_Scoring_Formulas_Step3.md`:

```text
screening_score =
  scoring_weights.trend  * trend_score +
  scoring_weights.momentum * momentum_score +
  scoring_weights.setup * setup_score +
  scoring_weights.volume * volume_score +
  scoring_weights.market * market_score
```

All sub-scores are clamped to 0–100.

Components:

- `trend_score`
- `momentum_score`
- `setup_score`
- `volume_score`
- `market_score`

Use exact threshold/sub-score rules from Project Files. Missing `sector_relative_strength` is neutral score `50` and is not a warning. Unknown `market_regime` receives neutral score `50` and is documented in `soft_score_components`.

### Candidate payload

For every evaluated ticker/date insert one row into `step3_candidates`.

Rules:

- `candidate_id`: fresh `uuid4` string per row.
- `run_id`: caller-supplied or minted run id.
- `strategy_config_id`: caller value.
- `signal_date`: `daily_features_current.feature_date`.
- `soft_score_components`: deterministic JSON object with sub-scores and relevant component inputs.
- `feature_snapshot_json`: deterministic JSON object with key features used for scoring.

Minimum `feature_snapshot_json` fields:

```text
ema20, ema50, ema200, ema_alignment_score, rsi14, roc20, rvol20,
avg_dollar_volume_20d, breakout_proximity, pullback_from_recent_high_pct,
consolidation_score, sector_relative_strength, market_regime, close_raw
```

Use `json.dumps(..., sort_keys=True)` for JSON fields.

### Write and transaction

Append candidates in one INSERT batch inside one transaction.

On read/config/guard/write failure, return `failed` with exact metadata keys, `rows_processed = 0`, and `candidates_written = 0`. On write failure, rollback so no partial candidate rows remain.

## Required tests

Create `tests/test_step3_screening.py`. Tests must be offline and use temporary DuckDB paths.

Cover:

- exact API signature, run_id minted/preserved, metadata keys, and `rows_processed == candidates_written`;
- guard/config/read/write failure paths, including `simulation` rejected before DB access;
- empty input success with zero counts and no insert;
- hard-filter pass and each failure label, including NULL/missing-value behavior;
- all failures collected, not only first;
- passed vs failed row semantics for `screening_score`, `passed_hard_filters`, and `hard_filter_fail_reasons`;
- Step 3 score reproducibility and market-regime mapping for all five enum values;
- missing `sector_relative_strength` neutral 50, no warning;
- unknown `market_regime` neutral 50 and audit JSON records it;
- `soft_score_components` and `feature_snapshot_json` are deterministic and parseable;
- all candidates inserted in one transaction; rollback leaves none;
- write ownership: no table except `step3_candidates` changes;
- debug role supported;
- static scans: no direct `duckdb`, `ATTACH`, DDL, provider imports, or `print()`;
- existing Module 01–12 tests pass unchanged.

## Module-specific source of truth

Create `M13_STEP3_SCREENING_SPEC.md`.

Keep it concise and implementation-oriented. Include:

- purpose and non-scope;
- source references;
- exact public API and metadata keys;
- input joins and signal-date behavior;
- hard filters and exact reason labels;
- scoring formula, config weight usage, sub-score source references, and neutral fallbacks;
- `soft_score_components` and `feature_snapshot_json` rules;
- write ownership, transaction model, and idempotency/append behavior;
- tests;
- assumptions, open gaps, and blockers.

Do not invent architecture or override higher-priority Project Files.

## Output

Follow Project Instructions.

Return only:

- updated zip;
- added / changed files;
- `M13_STEP3_SCREENING_SPEC.md`;
- short design notes;
- test commands and results;
- assumptions, gaps, or blockers;
- suggested commit message: `module13_step3_screening_stable`.
