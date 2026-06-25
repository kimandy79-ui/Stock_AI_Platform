# Module 09 Coding Prompt — Data Validator

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not repeat or summarize global rules.
Work silently and return only actionable results.

## Attached

1. `stock_ai_platform_module08_stable.zip`
2. `M09_DATA_VALIDATOR_SPEC.md`, if already available

Current codebase:
Modules 01–08 are accepted and frozen. Use `stock_ai_platform_module08_stable.zip` as the implementation base.

## Task

Implement only **Module 09 — Data Validator**.

Module 09 validates existing `daily_prices` rows after ingestion and before mutation detection / feature calculation. It must:

- read price rows from `daily_prices` for the requested date range;
- set real `daily_prices.data_quality_status` values;
- optionally enqueue validation-related repair rows into `data_repair_queue` only when supported by the frozen schema/spec;
- return `ServiceResult`.

Do not implement Module 10 or later.

## Source retrieval hints

Use the smallest relevant sources only:

- schema/status/repair enums → `01b_SCHEMA_AND_DATA.md`
- validation formulas/rules/thresholds if defined → `01c_FORMULAS_AND_CONFIGS.md`
- Module 09 responsibility and pipeline/error-handling placement → `01d_MODULES_AND_PIPELINE.md`
- coding/testing/logging/module-boundary rules → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- raw-vs-adjusted and DB-boundary decisions → `02b_ARCHITECTURE_DECISIONS.md`
- Module 08 repair queue and transaction/test pattern → `M08_DAILY_PRICE_INGESTION_SPEC.md`

If source files do not define a validation rule, threshold, status transition, or repair-reason mapping, do not guess. Mark it as an open specification gap in `M09_DATA_VALIDATOR_SPEC.md` and implement only the safest explicitly supported subset.

## Required pre-coding analysis

Before coding, inspect the relevant Project Files and create a concise rule matrix inside `M09_DATA_VALIDATOR_SPEC.md`.

For each candidate validation rule, include:

- check name;
- authoritative source file/section;
- condition;
- resulting `data_quality_status`;
- whether `data_repair_queue` is written;
- exact `repair_reason` enum value, if any;
- implementation status: implemented / open spec gap / blocker.

Investigate at minimum:

- missing price rows / missing expected trading days;
- null OHLC or adjusted OHLC values;
- `high < low`;
- open/close outside `[low, high]`;
- negative or zero prices;
- negative or zero volume;
- duplicate ticker/date rows;
- large price jumps / outlier thresholds;
- stale or incomplete ticker coverage.

Do not invent thresholds, statuses, or repair reasons.

If the Project Files do not define enough rules to implement a meaningful validator, stop and report a blocker instead of inventing behavior.

## Public API

Mirror Module 08 service style unless Project Files define a different interface.

Document and test the exact public API in `M09_DATA_VALIDATOR_SPEC.md`. It must include at minimum:

- `db_role` guard;
- `start_date` / `end_date` guard;
- optional `run_id` propagation;
- `ServiceResult` with exact metadata keys on every return path.

Invalid `db_role`, including `"simulation"`, must return `failed` before DB writes. Invalid date range must return `failed` before DB writes.

Do not call any market data provider. This module validates already-ingested database rows only.

## Scope and allowed files

Implement only Module 09.

Expected additions/changes should be limited to Module 09 files, tests, README note if needed, and the module spec. Prefer a new validation service location that matches the existing project structure, for example:

- `app/services/validation/__init__.py`
- `app/services/validation/data_validator.py`
- `tests/test_data_validator.py`
- `M09_DATA_VALIDATOR_SPEC.md`
- `README.md` note, only if consistent with prior module practice

Do not modify unrelated or frozen Modules 01–08 unless required by a failing test or real integration blocker. If such a change is required, explain the blocker and keep the change minimal.

Module 09 may update only validation-owned fields in `daily_prices`, primarily `data_quality_status`, and may insert validation-related rows into `data_repair_queue` when supported.

Module 09 must not:

- modify price values, OHLCV raw/adjusted columns, dividend/split fields, `adjustment_factor`, or `mutation_flag`;
- write to `ticker_master`, `ticker_universe_snapshot`, `sector_etf_map`, simulation DB, or feature/step/proposal/outcome tables;
- call providers/vendors or fetch data;
- import `duckdb`, use `ATTACH`, run DDL, modify schema, or bypass the DuckDB manager;
- process, resolve, delete, or update existing repair queue rows.

## Required behavior

### Validation ownership

Module 09 owns `daily_prices.data_quality_status`:

```text
ok / warning / suspect / failed / quarantined
```

Module 08 wrote placeholder `"ok"`; Module 09 sets the real validation value.

### Status transition discipline

Use only status values allowed by the frozen schema/spec.

Document the exact status assignment logic in `M09_DATA_VALIDATOR_SPEC.md`.

If multiple checks apply to the same row, use an explicit severity precedence based only on the source files. If no precedence is defined, use the safest conservative order and document it as an assumption, for example:

```text
quarantined > failed > suspect > warning > ok
```

Do not downgrade an existing worse status unless the source files explicitly allow it.

### Repair queue

Use only existing `repair_reason` enum values from the frozen schema/spec.

List allowed `repair_reason` values in `M09_DATA_VALIDATOR_SPEC.md` and map every repairable implemented rule to one of them.

If no suitable enum exists, do not create a new enum, modify schema, or force-map incorrectly. Report the gap or blocker.

Use the deterministic repair-id insert-or-ignore pattern from Module 08 where supported by the schema. Re-runs over the same data/date range must not create duplicate repair rows.

Repair rows inserted by Module 09 must use schema-correct defaults, including attempts/status/max-attempts/updated-at behavior, based on Module 08 pattern and frozen schema.

### Transactions and idempotency

Separate read, compute, and write phases.

All writes to `daily_prices.data_quality_status` and `data_repair_queue` must occur inside one write transaction. On write failure, rollback must leave no partial status updates and no partial repair rows.

Do not hold a write transaction during expensive computation when results can be computed before the write phase.

Re-running Module 09 for the same date range and same input data must produce stable statuses and no duplicate repair queue rows.

## Required tests

Create `tests/test_data_validator.py`.

Tests must be offline and isolated with temp DuckDB paths.

Cover:

- public API signature, guards, exact metadata keys, and `run_id` propagation;
- valid rows remain/become `ok`;
- every implemented rule from the rule matrix;
- status precedence when multiple implemented checks apply to one row;
- missing/ambiguous rules are documented, not invented;
- repair queue insert, deterministic IDs, schema-correct defaults, and no duplicate repair rows on re-run;
- no modification of price values, OHLCV raw/adjusted columns, dividend/split fields, `adjustment_factor`, or `mutation_flag`;
- no writes to forbidden tables or simulation DB;
- invalid `db_role` / `"simulation"` and invalid date range fail before DB writes;
- transaction rollback leaves no partial status updates or repair rows;
- no provider/vendor calls and no network/data-fetch dependency;
- static scan: no direct `duckdb`, `ATTACH`, DDL/schema changes, provider/vendor imports/calls, or `print()`.

Existing Module 01–08 tests must pass unchanged.

## Module-specific source of truth

Create or update:

`M09_DATA_VALIDATOR_SPEC.md`

Keep it concise and implementation-oriented. Include:

- purpose;
- scope / non-scope;
- source references;
- exact public API;
- rule matrix;
- validation status rules and status precedence;
- repair queue rules and allowed repair reasons;
- transaction / idempotency strategy;
- DB-manager usage;
- metadata keys;
- tests;
- assumptions, open specification gaps, and blockers.

Do not invent architecture or override higher-priority Project Files.

## Output

Follow Project Instructions.

Return:

- updated zip;
- added/changed files;
- `M09_DATA_VALIDATOR_SPEC.md`;
- short design notes;
- test command/results;
- assumptions, open gaps, and blockers;
- suggested commit message: `module09_data_validator_stable`.
