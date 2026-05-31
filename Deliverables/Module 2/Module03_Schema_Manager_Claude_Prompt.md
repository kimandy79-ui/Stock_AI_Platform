# Module 03 — Schema Manager Coding Prompt for Claude

## Files to Attach

Attach exactly these **3 files** to Claude:

1. `stock_ai_platform_module02_stable.zip`
2. `StockAnalyzer_Shared_Context_Pack_v1_3 (M01).zip`
3. `SCHEMA_SPEC.md`

Do **not** attach:

- `SwingTradingSystem_Master_TZ_v1_FULL.zip`
- `SwingTradingSystem_Master_TZ_PATCH_1.zip`
- `SwingTradingSystem_Master_TZ_MINI_PATCH_2.zip`

Those three old archives are audit/provenance files only. They are intentionally excluded from the coding prompt because the merge has already been performed in `SCHEMA_SPEC.md`.

---

# Prompt for Claude

You are a senior Python engineer implementing the next module of a local swing trading stock analyzer.

## FILES ATTACHED

I am attaching exactly three files:

1. `stock_ai_platform_module02_stable.zip`
   - Current stable codebase after Modules 01 and 02.
   - Modules 01 and 02 are frozen and accepted.
   - This zip already contains `docs/SCHEMA_SPEC.md`.

2. `StockAnalyzer_Shared_Context_Pack_v1_3 (M01).zip`
   - Latest shared context pack.
   - Contains:
     - `MASTER_SPEC.md`
     - `ARCHITECTURE.md`
     - `DECISIONS_LOG.md`
     - `TODO_ROADMAP.md`
     - `CODING_STANDARDS.md`
     - `manifest.json`

3. `SCHEMA_SPEC.md`
   - Final merged DuckDB schema specification for Module 03.
   - This is the only column-level schema source.
   - It is also present inside the codebase zip at `docs/SCHEMA_SPEC.md`, but it is attached separately for emphasis.

Do not ask for the old source archives. They are intentionally not attached for coding. The merge has already been performed in `SCHEMA_SPEC.md`.

## SOURCE OF TRUTH PRIORITY

Use the sources in this priority order:

1. `SCHEMA_SPEC.md`
   - Highest authority for every table name, column name, column type, primary key, index, view, schema seed row, role-specific schema split, and schema version value.
   - Do not invent any schema element not present in this document.

2. `StockAnalyzer_Shared_Context_Pack_v1_3 (M01).zip`
   - Use for architecture rules, coding standards, module boundaries, `ServiceResult` rules, logging rules, testing discipline, and merged-schema decision history.

3. Existing code inside `stock_ai_platform_module02_stable.zip`
   - Use as the implementation base.
   - Do not modify frozen Module 01 or Module 02 behavior.

If `SCHEMA_SPEC.md` conflicts with older high-level docs, do not guess. Stop and report the conflict.

## CRITICAL MERGED SCHEMA RULE

Module 03 must create the final merged schema directly.

The final merged schema originally comes from:

- Master ТЗ v1 FULL
- PATCH 1
- MINI-PATCH 2

However, do not re-merge those documents during coding. The merge has already been performed in `SCHEMA_SPEC.md`.

For implementation, use `SCHEMA_SPEC.md` as the only column-level schema source.

Do not:

- create the old base schema first,
- apply PATCH 1 or MINI-PATCH 2 later with `ALTER TABLE`,
- create migration-style patch files,
- infer missing columns from old documents,
- create guessed DDL.

Create the final merged schema directly from `SCHEMA_SPEC.md`.

## TASK

Implement ONLY Module 03 — Schema Manager.

According to the project architecture:

- Module 03 creates the final merged DuckDB schema.
- Module 03 must not implement provider logic, screening logic, scoring logic, trading logic, simulation logic, AI review logic, or dashboard logic.
- Module 03 must not implement Module 04 or any later module.

## STRICT SCOPE — ALLOWED

You may add or modify only:

1. `app/database/schema_manager.py`
2. `tests/test_schema_manager.py`
3. `app/database/__init__.py` only if needed to expose Module 03 symbols
4. `README.md` only if needed to document Module 03 usage

Optional:

5. `app/database/schema_sql/merged_schema_v01.sql` only if you decide a single static SQL file is cleaner than Python SQL constants.

Prefer the simplest implementation. A single `schema_manager.py` with clear SQL constants is acceptable.

Do not create both Python DDL constants and an external SQL file unless there is a clear reason.

## STRICT SCOPE — FORBIDDEN

Do NOT modify:

- `app/database/duckdb_manager.py`
- `app/config/settings.py`
- `app/config/constants.py`
- `app/utils/logging_config.py`
- `app/utils/service_result.py`
- existing Module 01 tests
- existing Module 02 tests
- `conftest.py`
- `pyproject.toml`
- `requirements.txt`
- `.gitignore`
- `.env.example`
- any `docs/*.md` file
- the shared context pack files

README is the only documentation file you may update, and only if needed.

Do NOT:

- create `app/database/migrations/`
- create a migration framework
- create numbered migration files
- create upgrade/downgrade migration logic
- use `ALTER TABLE` in Module 03 production code
- call `duckdb.connect(...)` directly
- open arbitrary DB paths
- create DuckDB `ENUM` types
- create production views in the simulation database
- duplicate production tables inside the simulation database
- enforce business semantics like `selected_flag = in_diversified_top_n` at DB level
- call providers
- import `yfinance`
- download data
- implement screening, scoring, trading, simulation, AI review, or dashboard logic
- implement Module 04 or later modules

## REQUIRED PUBLIC API

Implement a simple public API.

Recommended API:

```python
apply_schema(db_role: str) -> ServiceResult
apply_prod_schema() -> ServiceResult
apply_debug_schema() -> ServiceResult
apply_simulation_schema() -> ServiceResult
```

Exact naming may vary slightly, but keep it simple and boring.

Public entry points must return `ServiceResult` from `app.utils.service_result`.

## REQUIRED ROLE BEHAVIOR

Use `SCHEMA_SPEC.md` for exact schema details.

### `prod`

Create:

- production tables from `SCHEMA_SPEC.md` §3
- production indexes from §3.21
- production views from §3.22
- one `schema_versions` seed row:
  - `schema_name = 'prod'`
  - `version = 'schema_v01'`
  - `notes = 'initial merged schema'`

### `debug`

Create:

- the same production schema as prod
- the same production indexes as prod
- the same production views as prod
- one `schema_versions` seed row:
  - `schema_name = 'debug'`
  - `version = 'schema_v01'`
  - `notes = 'initial merged schema'`

### `simulation`

Create:

- `schema_versions`
- the explicitly listed simulation `sim_*` tables from `SCHEMA_SPEC.md` §4
- simulation indexes from §4.10
- no production tables
- no production views

Seed one `schema_versions` row:

- `schema_name = 'simulation'`
- `version = 'schema_v01'`
- `notes = 'initial merged schema'`

Simulation reads production data through Module 02’s read-only attach helper at runtime. Module 03 does not need to attach prod while creating the simulation schema.

## DATABASE ACCESS RULES

All DuckDB connections must go through `app.database.duckdb_manager`.

Allowed:

```python
duckdb_manager.connect(db_role)
duckdb_manager.connect_prod()
duckdb_manager.connect_debug()
duckdb_manager.connect_simulation()
```

Forbidden:

```python
duckdb.connect(...)
```

Do not pass arbitrary file paths. Do not introduce `path=` or `database=` parameters.

## IDEMPOTENCY RULES

Schema creation must be idempotent.

Calling `apply_schema("prod")` twice must:

- not raise
- not duplicate tables
- not duplicate indexes
- not duplicate `schema_versions` rows

Use:

- `CREATE TABLE IF NOT EXISTS`
- `CREATE INDEX IF NOT EXISTS`
- `CREATE OR REPLACE VIEW`

For `schema_versions`, use an explicit insert-if-not-exists pattern, such as:

```sql
INSERT INTO schema_versions (...)
SELECT ...
WHERE NOT EXISTS (
    SELECT 1
    FROM schema_versions
    WHERE schema_name = ?
      AND version = ?
);
```

Do not rely on swallowing primary-key conflicts.

## SERVICE RESULT CONTRACT

Public entry points must return `ServiceResult`.

Use the metadata contract from `SCHEMA_SPEC.md` §7.1.

Recommended metadata keys:

```python
{
    "db_role": "...",
    "tables_created": [...],
    "indexes_created": ...,
    "views_created": ...,
    "schema_version": "schema_v01",
    "seed_row_inserted": ...
}
```

`rows_processed` should be the number of tables present after schema application.

Status behavior:

- `success` on first creation
- `success` on idempotent re-application
- `success_with_warnings` only for non-fatal anomalies
- `failed` only on hard errors

## VERSION RULES

Do not confuse these two values:

1. Database schema version:
   - `schema_v01`
   - seeded into `schema_versions`

2. Feature schema version:
   - `features_v01`
   - comes from `app.config.constants.FEATURE_SCHEMA_VERSION`
   - used by later modules for `daily_features.feature_schema_version`

Module 03 creates the `daily_features.feature_schema_version` column but does not populate feature rows.

## LOGGING AND STYLE

Use:

```python
from app.utils import logging_config

_LOG = logging_config.get_logger(__name__)
```

Do not use `print()`.

Every new Python file must have a module-level docstring.

Every function must have type hints.

Use `pathlib` for paths if paths are needed.

Keep the code small and boring. No over-engineering.

Do not add new third-party dependencies.

## TESTING REQUIREMENTS

Create `tests/test_schema_manager.py`.

Tests must follow the same isolation discipline as `tests/test_duckdb_manager.py`.

No test may create or modify real DB files under the real `data/duckdb/` folder.

Every test that opens a DuckDB connection must redirect these settings to `tmp_path` using `monkeypatch.setattr(settings, ...)`:

- `settings.PROD_DB_PATH`
- `settings.DEBUG_DB_PATH`
- `settings.SIMULATION_DB_PATH`
- `settings.DUCKDB_DIR`

Mirror the fixture pattern from `tests/test_duckdb_manager.py`.

Tests must verify:

1. Fresh schema creation succeeds for `prod`.
2. Fresh schema creation succeeds for `debug`.
3. Fresh schema creation succeeds for `simulation`.

4. Schema creation is idempotent:
   - calling twice does not raise
   - calling twice does not duplicate `schema_versions` rows

5. Exact expected production tables are created for `prod` and `debug`.

Production tables:

```text
schema_versions
pipeline_runs
pipeline_locks
ticker_master
ticker_universe_snapshot
sector_etf_map
daily_prices
daily_features
strategy_configs
step3_candidates
step4_analysis
step5_proposals
outcome_tracking_queue
signal_outcomes
earnings_calendar
macro_events_calendar
data_repair_queue
feature_rebuild_log
ai_reviews
execution_decisions
```

Tests must fail if unexpected extra tables are created.

6. Exact expected simulation tables are created for `simulation`.

Simulation tables:

```text
schema_versions
sim_runs
sim_folds
sim_step3_candidates
sim_step4_analysis
sim_step5_proposals
sim_signal_outcomes
sim_config_comparisons
sim_ai_reviews
```

Tests must fail if any production table appears in simulation, for example:

```text
daily_prices
daily_features
ticker_master
step5_proposals
signal_outcomes
```

7. Production indexes exist for `prod` and `debug`.

Production indexes:

```text
idx_daily_prices_ticker_date
idx_daily_features_ticker_date
idx_step3_run_date
idx_step5_run_date_selected
idx_outcomes_config_date
idx_queue_status_eval
idx_repair_status
idx_step5_run_raw_rank
idx_step5_run_div_rank
```

8. Simulation indexes exist.

Simulation indexes:

```text
idx_sim_props_run_date
idx_sim_outcomes_config_date
```

9. Production views exist for `prod` and `debug`.

Production views:

```text
daily_features_current
selected_proposals_current
```

10. `selected_proposals_current` must use:

```sql
WHERE in_diversified_top_n = TRUE
```

It must not use the legacy condition:

```sql
WHERE selected_flag = TRUE
```

11. Simulation DB must not contain these views:

```text
daily_features_current
selected_proposals_current
```

12. `schema_versions` is populated correctly.

Expected rows:

- prod: exactly one row with `('prod', 'schema_v01')`
- debug: exactly one row with `('debug', 'schema_v01')`
- simulation: exactly one row with `('simulation', 'schema_v01')`

Per `SCHEMA_SPEC.md` §7.2:

- assert exact `(schema_name, version)`
- assert `applied_at IS NOT NULL`
- assert `notes` is non-empty
- do not assert exact timestamp
- do not overfit exact notes wording beyond non-empty

13. `daily_features.feature_schema_version` column exists and is compatible with `constants.FEATURE_SCHEMA_VERSION`.

14. Static source scan of `app/database/schema_manager.py` confirms:

- no `ALTER TABLE` in production code
- no direct `duckdb.connect(` call
- no `CREATE TYPE`
- no DuckDB `ENUM`
- no migration framework imports
- no `app/database/migrations` usage

15. Public entry point returns a valid `ServiceResult`.

16. `ServiceResult.metadata` contains:

```text
db_role
tables_created
indexes_created
views_created
schema_version
seed_row_inserted
```

17. Existing Module 01 and Module 02 tests still pass.

18. Type/import smoke tests pass.

## QUALITY REQUIREMENTS

- Keep Module 01 and Module 02 behavior unchanged.
- No new dependencies.
- No connection pools.
- No abstract base classes.
- No retry framework.
- No threading or locks.
- No provider logic.
- No business logic beyond applying schema and seeding `schema_versions`.
- No schema inference beyond `SCHEMA_SPEC.md`.

If any SQL from `SCHEMA_SPEC.md` fails in real DuckDB, do not silently replace it with a guessed alternative. Explain the failure and make the smallest compatible correction. Document the correction as an assumption.

## OUTPUT REQUIRED

Return:

1. Updated project zip.
   - Top-level folder must be `stock_ai_platform/`.
   - Preserve the same layout as Module 02 stable zip.

2. List of added/changed files.

3. Design notes:
   - where the DDL is stored
   - how role-specific schema application works
   - how idempotency is handled
   - how `schema_versions` insertion is protected from duplicates
   - how direct DuckDB connections are avoided

4. Test command and full test result.

5. Any assumptions.
   - State assumptions explicitly.
   - Do not hide assumptions.

6. Suggested commit message:

```text
module03_schema_manager_stable
```

## STARTING STEPS

Read in this order:

1. `docs/SCHEMA_SPEC.md`
2. `docs/ARCHITECTURE.md`
3. `docs/CODING_STANDARDS.md`
4. `app/database/duckdb_manager.py`
5. `app/utils/service_result.py`
6. `tests/test_duckdb_manager.py`

Then implement Module 03.

Do not reopen architecture. Do not implement Module 04 or later. Do not modify any Module 01 or Module 02 file.
