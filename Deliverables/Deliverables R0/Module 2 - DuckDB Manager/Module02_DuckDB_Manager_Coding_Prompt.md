# Module 02 DuckDB Manager — Coding Prompt

You are a senior Python engineer implementing the next module of a local swing trading stock analyzer.

## SOURCE OF TRUTH

I am attaching:

1. The latest StockAnalyzer shared context pack v1_3
2. The current stable codebase after Module 01.

## IMPORTANT

Use the pack that contains the latest `DECISIONS_LOG.md` entry about:

- Structural domain constants
- VIX 25/30 regime boundaries
- outcome horizons
- screening weights
- Simulation / AI Review vocabulary

Treat the latest attached context pack as the ONLY source of truth.

Treat the Module 01 project zip as the current stable codebase.

Module 01 has already passed final review and is accepted as stable.

## TASK

Implement ONLY **Module 02 — DuckDB Manager**.

According to `ARCHITECTURE.md`:

> Module 02 — DuckDB Manager  
> “Manages DuckDB connections for prod/debug/simulation DBs.”

According to `CODING_STANDARDS.md`:

- Use DuckDB manager for all DB access.
- No module opens arbitrary DB paths directly.
- Use separate DBs:
  - `prod.duckdb`
  - `debug.duckdb`
  - `simulation.duckdb`
- Simulation attaches prod read-only only.
- Use pathlib for paths.
- Use module-level docstrings.
- Use type hints for all functions.
- Use logging format already defined in Module 01.
- Do not implement unrelated modules.

## STRICT SCOPE

Implement only centralized DuckDB connection management.

## Allowed

- create `app/database/`
- create `app/database/__init__.py`
- create `app/database/duckdb_manager.py`
- use existing `app/config/settings.py` paths
- use existing `app/utils/service_result.py` only if genuinely useful
- add tests for Module 02
- update README only if needed to document Module 02 usage
- update `__init__.py` only if needed

## Required behavior

1. Provide a safe DuckDB manager that opens connections only to approved DB roles:

   - `prod`
   - `debug`
   - `simulation`

2. DB paths must come from existing settings constants:

   - `settings.PROD_DB_PATH`
   - `settings.DEBUG_DB_PATH`
   - `settings.SIMULATION_DB_PATH`

3. Do not allow arbitrary DB file paths from callers.

4. The manager must read DB paths dynamically from `app.config.settings` at call time.

   Do NOT cache DB paths at module import time like:

   - `PROD_PATH = settings.PROD_DB_PATH`
   - `ROLE_TO_PATH = {...settings.PROD_DB_PATH...}`

   Reason:

   - tests must be able to monkeypatch settings paths to `tmp_path`
   - future env/path overrides must not be broken

5. Provide a clean API, for example:

   - `get_database_path(db_role: str) -> Path`
   - `connect(db_role: str, read_only: bool = False) -> duckdb.DuckDBPyConnection`
   - `connect_prod(...)`
   - `connect_debug(...)`
   - `connect_simulation(...)`
   - `ensure_database_directory()`

   Exact naming is up to you, but keep it simple and consistent.

6. For simulation support:

   - do not implement simulation engine
   - do not write simulation logic
   - only provide a safe helper for attaching prod read-only to a simulation connection if appropriate
   - if implemented, it must use only the approved prod path from `settings.PROD_DB_PATH`
   - it must not accept arbitrary prod DB paths

7. The manager must not create schema tables.

   Module 03 will handle Schema Manager.

   Do NOT create:

   - `schema_versions`
   - `pipeline_runs`
   - `ticker_master`
   - `daily_prices`
   - `daily_features`
   - any other tables

8. The manager must not implement migrations.

   Do not create base schema and do not apply ALTER patches.

   Module 03 will create the final merged schema directly.

9. No provider calls.

   - No `yfinance` usage.
   - No downloading.
   - No screening/scoring/trading logic.
   - No Streamlit/dashboard code.

## Testing requirements

Add tests for Module 02.

## Critical testing rule

Tests must NOT create or modify real DB files under the real `data/duckdb/` folder.

All tests that open DuckDB connections must redirect settings paths to pytest `tmp_path` using `monkeypatch.setattr(settings, ...)`.

## Tests must verify

1. Only allowed DB roles are accepted.
2. Unknown role raises a clear error.
3. DB paths resolve exactly to prod/debug/simulation paths from settings.
4. DB paths are read dynamically from settings at call time, not cached at import time.
5. `connect()` returns a DuckDB connection for approved roles.
6. `read_only` behavior is respected.

   Note: DuckDB `read_only=True` requires the database file to already exist.

   In tests, first create the DB file with a read-write connection, close it, then reopen read-only.

7. Arbitrary DB paths cannot be passed into the public API.
8. No schema tables are created by Module 02.
9. Database directory creation is idempotent.
10. If a simulation prod read-only attach helper is implemented:

    - test that it attaches only the approved prod DB path
    - test that writing to attached prod fails
    - test that simulation DB itself remains writable if opened read-write

11. Type/import smoke tests pass.
12. Existing Module 01 tests still pass.

## Preserve

- all existing Module 01 tests
- existing `conftest.py`
- existing package structure
- existing Module 01 behavior

## Quality requirements

- Keep Module 01 behavior unchanged.
- All Python files must have module-level docstrings.
- All functions must have type hints.
- Use pathlib only for paths.
- No `print()` in `app/utils`, service, database, or other library modules.
- Use existing `logging_config` if logging is needed.
- Keep code small and boring.
- Do not over-engineer.
- Do not add new dependencies unless absolutely necessary.

## OUTPUT

Return:

1. Updated project zip.
2. List of changed/added files.
3. Short explanation of design decisions.
4. Test command and test results.
5. Any assumptions made.
6. Suggested commit message:

```text
module02_duckdb_manager_stable
```

## IMPORTANT

Do not reopen architecture.

Do not propose new modules.

Do not implement Module 03 or any later module.

Do not add business logic.

Do not add schema creation.

Do not add migrations.

Do not touch provider logic.
