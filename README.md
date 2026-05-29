# Stock AI Platform — Swing Trading Stock Analyzer

Local Windows-based US daily swing trading **research-grade V1** platform.
DuckDB local files, Polars-first processing, Streamlit dashboard (added in a
later module). This repository follows the shared source-of-truth documents in
`docs/` (`MASTER_SPEC.md`, `ARCHITECTURE.md`, `DECISIONS_LOG.md`,
`TODO_ROADMAP.md`, `CODING_STANDARDS.md`).

> **Safety note.** This system is research support only. It does not provide
> guaranteed trading predictions. There is no auto-trading and no broker
> connection in V1.

## Current state: Modules 01 — 03

This repository currently implements **Module 01 (Project Skeleton)**,
**Module 02 (DuckDB Manager)**, and **Module 03 (Schema Manager)**. Module 01
establishes the project structure, configuration loading, constants, logging,
and the shared `ServiceResult` contract. Module 02 adds centralized DuckDB
connection management for the three approved database roles (`prod`, `debug`,
`simulation`). Module 03 creates the final merged DuckDB schema on those
databases. Per the roadmap, no provider calls, trading logic, simulation
logic, or dashboard exist yet — those land in Module 04 and beyond.

## Requirements

- Python 3.11+
- Windows local PC (paths use `pathlib`, so other OSes work for development)

## Project structure

```text
stock_ai_platform/
  app/
    config/
      settings.py      # pathlib paths + immutable strategy presets
      constants.py     # domain vocabulary + FEATURE_SCHEMA_VERSION
      env.py           # python-dotenv loading + typed getters
    database/
      duckdb_manager.py  # centralized DuckDB connection manager (Module 02)
    utils/
      service_result.py  # shared ServiceResult dataclass contract
      logging_config.py   # run_id-aware logging (timestamp | level | module | run_id | message)
  data/
    duckdb/            # prod/debug/simulation DuckDB files (created on first connect)
    logs/
    exports/
    backups/
  tests/
    test_project_skeleton.py
    test_duckdb_manager.py
  docs/                # source-of-truth specification documents
  pyproject.toml
  requirements.txt
  .env.example
  README.md
```

## Setup

1. Create and activate a virtual environment.

   ```bat
   py -3.11 -m venv .venv
   .venv\Scripts\activate
   ```

   On POSIX shells:

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies.

   ```bash
   pip install -r requirements.txt
   ```

   Or install the project (with dev extras) from `pyproject.toml`:

   ```bash
   pip install -e ".[dev]"
   ```

3. Create your local environment file (optional; defaults work without it).

   ```bat
   copy .env.example .env
   ```

   ```bash
   cp .env.example .env
   ```

## Configuration overview

- `app/config/constants.py` holds immutable domain constants, including
  `FEATURE_SCHEMA_VERSION = "features_v01"`, symbol types, benchmark symbols,
  market regimes, setup types, and outcome horizons.
- `app/config/settings.py` computes all filesystem paths with `pathlib` and
  exposes immutable strategy presets (`normal`, `aggressive`, `conservative`)
  drawn from `MASTER_SPEC.md`.
- `app/config/env.py` loads `.env` via `python-dotenv` and provides typed
  getters (`get_str`, `get_int`, `get_float`, `get_bool`, `get_path`).
- `app/utils/service_result.py` defines the `ServiceResult` dataclass returned
  by every service module in later phases.
- `app/utils/logging_config.py` configures logging in the required format and
  binds a `run_id` to each record.

To create the data directories on demand:

```python
from app.config import settings
settings.ensure_directories()
```

## Running the tests

From the project root (`stock_ai_platform/`):

```bash
pytest
```

For verbose output:

```bash
pytest -v
```

With coverage of the `app` package:

```bash
pytest --cov=app
```

A `conftest.py` at the project root puts the repository on `sys.path`, so the
tests run without an editable install. If you prefer, `pip install -e .` also
makes the `app` package importable.

## Module 02 — DuckDB Manager (usage)

Module 02 centralizes DuckDB access. All other code in the platform must go
through this manager and must never open arbitrary DB files directly
(`CODING_STANDARDS.md` section 8).

The manager accepts only the three approved database *roles* — `prod`,
`debug`, `simulation` — and resolves their file paths from
`app.config.settings` at call time (so tests and environment overrides take
effect). It does not create schema tables; that is Module 03.

```python
from app.database import duckdb_manager

# Open a writable production connection.
with duckdb_manager.connect_prod() as conn:
    ...

# Open the debug DB read-only (file must already exist).
with duckdb_manager.connect_debug(read_only=True) as conn:
    ...

# Simulation reading from prod safely (prod is attached READ_ONLY).
with duckdb_manager.connect_simulation_with_prod() as sim:
    sim.execute("SELECT * FROM prod.some_table")  # ok
    # sim.execute("INSERT INTO prod.some_table ...")  # fails: prod is read-only
```

Helpers:

- `connect(db_role, read_only=False)` — generic role-based connect.
- `connect_prod`, `connect_debug`, `connect_simulation` — role-specific.
- `get_database_path(db_role)` — resolve the DB path from settings.
- `ensure_database_directory()` — idempotently create `data/duckdb/`.
- `attach_prod_read_only(connection, alias="prod")` — attach prod read-only
  using only `settings.PROD_DB_PATH` (no arbitrary path argument).
- `connect_simulation_with_prod(read_only=False, prod_alias="prod")` —
  convenience wrapper around the above two.

## Module 03 — Schema Manager (usage)

Module 03 creates the **final merged DuckDB schema** directly from
`docs/SCHEMA_SPEC.md` (Master TZ v1 + PATCH 1 + MINI-PATCH 2, already merged).
It goes through Module 02 for every connection and never opens DB files
directly, never runs `ALTER TABLE`, and never creates DuckDB `ENUM` types.

```python
from app.database import schema_manager

# Apply the production schema (20 tables, 9 indexes, 2 views) to prod.duckdb.
schema_manager.apply_prod_schema()

# debug.duckdb gets the identical production schema.
schema_manager.apply_debug_schema()

# simulation.duckdb gets the narrower sim_* schema (9 tables, 2 indexes, 0 views).
schema_manager.apply_simulation_schema()

# Generic role-based entry point (role: "prod" | "debug" | "simulation").
result = schema_manager.apply_schema("prod")
assert result.is_ok()
print(result.metadata["tables_created"], result.metadata["schema_version"])
```

All four entry points return a `ServiceResult`. Schema creation is idempotent:
calling it twice does not raise, does not duplicate tables or indexes, and does
not duplicate the single `schema_versions` seed row (one row per database,
keyed on `(schema_name, 'schema_v01')`). The database schema version
(`schema_v01`) is distinct from the per-row feature schema version
(`features_v01` from `constants.FEATURE_SCHEMA_VERSION`); Module 03 creates the
`daily_features.feature_schema_version` column but does not populate feature
rows.

## Next module

Per `docs/TODO_ROADMAP.md`, the next step is **Module 04 — Provider
Interface**. It is intentionally not implemented here.
