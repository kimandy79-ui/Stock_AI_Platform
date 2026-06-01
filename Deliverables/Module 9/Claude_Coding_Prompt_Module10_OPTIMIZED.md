# Module 10 Coding Prompt — Mutation Detector

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not repeat or summarize global rules.
Work silently and return only actionable results.

## Attached

1. `stock_ai_platform_module09_stable.zip`
2. `M10_MUTATION_DETECTOR_SPEC.md`, if already available

Current codebase:
Modules 01–09 are accepted and frozen. Use `stock_ai_platform_module09_stable.zip` as the implementation base.

## Task

Implement only **Module 10 — Mutation Detector**.

Module 10 runs after Module 09 and before Module 11. It must:

- read eligible `daily_prices` rows for the requested date range;
- skip rows where `data_quality_status != 'ok'`;
- detect explicit splits and supported historical mutations;
- set `daily_prices.mutation_flag = TRUE` only on detected rows;
- derive/write `daily_prices.adjustment_factor` where computable;
- enqueue mutation-related repair rows in `data_repair_queue` using `repair_reason = 'mutation'` when supported by the frozen schema/spec;
- insert `feature_rebuild_log` entries for affected tickers when supported by the frozen schema/spec;
- return `ServiceResult`.

Do not implement Module 11 or later.

## Source retrieval hints

Use the smallest relevant sources only:

- schema / columns / enums → `01b_SCHEMA_AND_DATA.md` or schema docs in the zip;
- mutation rules / thresholds, if defined → `01c_FORMULAS_AND_CONFIGS.md`;
- Module 10 responsibility and pipeline placement → `01d_MODULES_AND_PIPELINE.md`;
- coding / testing / logging / module boundaries → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`;
- raw-vs-adjusted and DB-boundary decisions → `02b_ARCHITECTURE_DECISIONS.md`;
- deterministic repair-id pattern / transaction model → `M08_DAILY_PRICE_INGESTION_SPEC.md`;
- validation status ownership and no-downgrade discipline → `M09_DATA_VALIDATOR_SPEC.md`.

If Project Files do not define a mutation rule, threshold, enum, or column derivation formula, do not guess. Mark it as an open specification gap in `M10_MUTATION_DETECTOR_SPEC.md` and implement only the safest explicitly supported subset.

## Required pre-coding analysis

Before coding, create a concise rule matrix inside `M10_MUTATION_DETECTOR_SPEC.md`.

For each candidate detection rule, include:

- check name;
- source file / section;
- condition;
- columns written;
- whether `data_repair_queue` is written and exact `repair_reason` value;
- whether `feature_rebuild_log` is written;
- implementation status: implemented / open spec gap / blocker.

Investigate at minimum:

- `split_ratio != 1` explicit split event;
- `close_raw / close_adj` ratio discontinuity across consecutive rows;
- `adjustment_factor` derivation formula;
- threshold for significant ratio change vs noise;
- which rows receive `mutation_flag = TRUE` and `adjustment_factor` updates;
- which tickers trigger repair queue and rebuild log entries.

Do not invent thresholds. If no discontinuity threshold is defined, document open gap `G1` and implement only the explicit `split_ratio != 1` path plus `adjustment_factor` derivation for eligible rows.

## Public API

Mirror the Module 09 service style unless Project Files define a different interface.

Document and test the exact public API in `M10_MUTATION_DETECTOR_SPEC.md`. It must include:

- `db_role` guard: `prod` / `debug` only; `simulation` fails before any DB read/write;
- `start_date` / `end_date` guard: `start_date > end_date` fails before any DB read/write;
- optional `run_id` propagation;
- `ServiceResult` with exact metadata keys on every return path.

Do not call providers or fetch data. Module 10 operates only on already-ingested `daily_prices` rows.

### Exact ServiceResult metadata keys

`ServiceResult.metadata` must contain exactly these keys on every return path:

```text
db_role
start_date
end_date
rows_read
rows_processed
rows_skipped_non_ok
adjustment_factors_written
mutation_rows_detected
mutation_flags_written
tickers_with_mutation
repair_queue_enqueued
rebuild_logs_enqueued
```

Definitions:

- `rows_read`: all `daily_prices` rows read in the requested date range.
- `rows_processed`: eligible rows after `data_quality_status = 'ok'` filter.
- `rows_skipped_non_ok`: skipped rows where `data_quality_status != 'ok'`.
- `adjustment_factors_written`: rows where stored `adjustment_factor` changed, was inserted, or was cleared to `NULL`.
- `mutation_rows_detected`: eligible rows passing a mutation detection rule.
- `mutation_flags_written`: rows whose `mutation_flag` changed from `FALSE` to `TRUE`.
- `tickers_with_mutation`: distinct eligible tickers with at least one detected mutation.
- `repair_queue_enqueued`: newly inserted repair rows; ignored duplicates not counted.
- `rebuild_logs_enqueued`: newly inserted rebuild-log rows; ignored duplicates not counted.

On guard failure, numeric counts are `0`. On write failure, rollback is mandatory, so durable write counts should be `0`.

## Scope

Expected additions / changes:

```text
app/services/mutation/__init__.py
app/services/mutation/mutation_detector.py
tests/test_mutation_detector.py
M10_MUTATION_DETECTOR_SPEC.md
README.md                          # short Module 10 note only
```

Do not modify frozen Modules 01–09 unless required by a failing test or real integration blocker. If required, explain the blocker and keep the change minimal.

Module 10 may write only:

- `daily_prices.mutation_flag`;
- `daily_prices.adjustment_factor`;
- `daily_prices.updated_at` when those fields are written;
- `data_repair_queue` insert-only mutation repairs;
- `feature_rebuild_log` insert-only rebuild entries.

Module 10 must not modify price values, dividend/split fields, `data_quality_status`, `created_at`, `ticker_master`, `ticker_universe_snapshot`, `sector_etf_map`, simulation DB, or feature/step/proposal/outcome/AI/execution tables. It must not call providers, import `duckdb`, use `ATTACH`, run DDL, modify schema, bypass the DuckDB manager, or process/update/delete existing repair/rebuild rows.

## Locked behavior

### Data-quality boundary

Process only rows where `daily_prices.data_quality_status = 'ok'`.

Rows with any other status are counted as skipped and must not cause writes to `mutation_flag`, `adjustment_factor`, `data_repair_queue`, or `feature_rebuild_log`.

### Mutation detection and adjustment factor

For each eligible ticker in the requested date range:

1. **Explicit split detection**: flag rows where `split_ratio != 1` and non-null.
2. **Adjustment factor derivation**: where `close_raw` is non-null, non-zero, and `close_adj` is non-null, set `adjustment_factor = close_adj / close_raw`; otherwise set/leave it `NULL`.
3. **Historical mutation / retroactive adjustment detection**: implement only if Project Files define a threshold for `close_raw / close_adj` ratio discontinuity. If implemented, read one immediately previous eligible row per ticker before `start_date` for comparison only. Never write outside the requested date range. If no threshold is defined, record open gap `G1` and skip this check.

### Repair queue and rebuild log

For each ticker with at least one detected mutation:

- insert one `data_repair_queue` row with `repair_reason = 'mutation'`, if that enum/value is supported by the frozen schema/spec;
- insert one `feature_rebuild_log` row, if the table/fields/defaults are supported by the frozen schema/spec;
- use deterministic `uuid5` IDs and insert-or-ignore semantics so re-runs do not duplicate rows.

Use the first detected mutation date in the requested range as the repair date / affected start date unless a higher-priority source defines different behavior. Use `constants.FEATURE_SCHEMA_VERSION` for rebuild entries when required by schema/spec.

If `repair_reason = 'mutation'`, `feature_rebuild_log`, or required constraints are missing, do not change schema or invent values; document the gap/blocker in the spec and final output.

### Status and idempotency discipline

- Do not downgrade `mutation_flag` from `TRUE` to `FALSE`.
- Update `adjustment_factor` only when the computed value differs from the stored value.
- Re-running over the same date range and input data must produce stable values and no duplicate repair/rebuild rows.

### Transaction strategy

Separate phases:

1. Read eligible source rows.
2. Compute mutation decisions and payloads in Python without DB writes.
3. Write all `daily_prices`, repair queue, and rebuild-log changes inside one transaction.

On any write error, rollback all Module 10 writes and return `failed`.

## Required tests

Create `tests/test_mutation_detector.py`. Tests must be offline and isolated with temp DuckDB paths.

Cover:

- public API, guards before DB read/write, exact metadata keys, and `run_id` propagation;
- `adjustment_factor` derivation and underivable `NULL` behavior;
- non-ok rows skipped and not modified;
- explicit split detection → `mutation_flag`, repair, and rebuild behavior;
- clean rows unchanged; no downgrade from `TRUE` to `FALSE` on re-run;
- deterministic repair/rebuild IDs and no duplicates on re-run;
- write ownership: only `mutation_flag`, `adjustment_factor`, and relevant `updated_at` may change in `daily_prices`;
- no writes to forbidden tables or simulation DB;
- invalid `db_role` / `simulation` and invalid date range fail before DB access;
- transaction rollback leaves no partial status, repair, or rebuild writes;
- no provider/vendor/network usage;
- ratio-discontinuity detection behavior if implemented; otherwise documented open gap `G1`;
- static scan: no direct `duckdb`, `ATTACH`, DDL/schema changes, provider/vendor imports, or `print()`.

Existing Module 01–09 tests must pass unchanged.

## Module-specific source of truth

Create or update:

`M10_MUTATION_DETECTOR_SPEC.md`

Keep it concise and implementation-oriented. Include:

- purpose and non-scope;
- source references;
- exact public API;
- exact metadata keys;
- data-quality boundary;
- rule matrix;
- `adjustment_factor` formula;
- `mutation_flag` no-downgrade rule;
- repair queue and rebuild-log rules;
- transaction/idempotency strategy;
- DB-manager usage;
- tests;
- assumptions, open gaps, and blockers, especially `G1` if ratio-discontinuity threshold is undefined.

Do not invent architecture or override higher-priority Project Files.

## Output

Follow Project Instructions. Return only:

- updated zip;
- added / changed files;
- `M10_MUTATION_DETECTOR_SPEC.md`;
- short design notes;
- test command / results;
- assumptions, open gaps, and blockers;
- suggested commit message: `module10_mutation_detector_stable`.
