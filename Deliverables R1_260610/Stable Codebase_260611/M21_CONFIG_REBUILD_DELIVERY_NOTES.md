# M21 Config Management Rebuild — Delivery Notes

Implements the M21 Dashboard V2 Config Management Addendum on a fresh-DB basis.
No Module 23 created. No automatic DB-file deletion added.

## Changed / added files

Added:
- `app/services/config/__init__.py`
- `app/services/config/config_validator.py`
- `app/services/config/default_configs.py`
- `app/services/config/config_service.py`
- `tests/test_config_service.py`
- `tests/test_sector_normalization.py`
- `tests/test_orchestrator_config_loading.py`
- `specs/M03_SCHEMA_SPEC_CONFIG_DELTA.md`
- `specs/M06_UNIVERSE_SNAPSHOT_CONFIG_DELTA.md`
- `specs/M20_PIPELINE_ORCHESTRATOR_CONFIG_DELTA.md`
- `specs/M22_DEBUG_MODE_CONFIG_DELTA.md`

Changed:
- `app/config/constants.py` — added `CANONICAL_SECTORS`, `SECTOR_ALIAS_MAP`,
  `SECTOR_ALIAS_SOURCE_YAHOO` (single source for sector normalization).
- `app/database/schema_manager.py` — extended `strategy_configs` (`db_role`,
  `created_by`); added config-traceability columns to `pipeline_runs`; added
  `runtime_configs`, `config_activation_log`, `sector_alias_map` tables (prod/debug/simulation).
- `app/services/universe/universe_snapshot.py` — `normalize_sector()`; canonical
  sector written to `ticker_master` and `ticker_universe_snapshot`.
- `app/services/pipeline/pipeline_orchestrator.py` — removed duplicate sector ETF
  block (now `constants.SECTOR_ETF_MAP`); injectable `config_service`; DB-backed
  config resolution with seed-on-missing; config traceability persisted to
  `pipeline_runs`; `_validate_inputs` accepts `None` override.
- `app/services/debug/debug_mode.py` — replaced silent `DEFAULT_STRATEGY_CONFIGS`
  fallback with DB-backed active-config load (debug role) + seed-on-missing;
  injectable `config_service`.
- `tools/init_prod_db.py`, `tools/init_debug_db.py` — seed defaults after schema apply.
- `tools/init_simulation_db.py` — new; applies simulation schema then seeds defaults.
- `tests/test_schema_manager.py` — expected tables/columns updated for all three roles.
- `tests/test_tools_runners.py` — seed seam patched; integration asserts seeded rows for
  prod, debug, and simulation.

## Schema changes summary

- `strategy_configs`: `+ db_role VARCHAR NOT NULL`, `+ created_by VARCHAR` (all roles).
- `pipeline_runs`: `+ strategy_config_ids_json JSON`, `+ runtime_config_ids_json JSON`,
  `+ config_snapshot_hash VARCHAR` (prod/debug only — `pipeline_runs` is not in the
  simulation schema).
- `runtime_configs`, `config_activation_log`, `sector_alias_map`: new in **all three
  roles** (prod, debug, simulation). The four config tables — `strategy_configs`,
  `runtime_configs`, `config_activation_log`, `sector_alias_map` — are shared across
  prod/debug and simulation. `_SIM_TABLE_DDL` is built by filtering `_PROD_TABLE_DDL`
  for these four DDL strings so there is a single definition.
- Production schema: **24 tables** (was 20). Debug shares the production schema.
- Simulation schema: **13 tables** (was 9): `schema_versions` + 4 config tables +
  8 `sim_*` tables.

## Default configs seeded (fresh DB init, idempotent)

- Strategy: `normal`, `aggressive`, `conservative` (active, from `DEFAULT_STRATEGY_CONFIGS`).
- Runtime: `pipeline`, `provider`, `data_completeness`, `debug`, `simulation`,
  `dashboard`, `ai_review`, `export` (one active each).
- `sector_alias_map`: from `constants.SECTOR_ALIAS_MAP` (16 aliases).
- `sector_etf_map` is NOT seeded here — Module 07 remains its sole owner and seeds
  it from `constants.SECTOR_ETF_MAP` (already canonical).

## Code behavior changes

- Pipeline run with no explicit `strategy_configs` loads active configs from the DB
  via `ConfigService`, keyed by the real `strategy_configs.config_id`; if none active,
  defaults are seeded then reloaded. The Step 3/4/5/outcome engines receive the real
  DB `config_id` (not the strategy name) as `strategy_config_id`, and
  `pipeline_runs.strategy_config_ids_json` matches exactly the ids used in output
  tables. Explicit `strategy_configs` still override (tests/debug/manual); its keys
  are the ids used downstream and recorded. `get_active_strategy_configs` exposes
  `configs_by_strategy` / `configs_by_id` / `config_ids_by_strategy` so strategy-name
  lookup remains available for debug/user selection.
- Each run records strategy/runtime config ids and a deterministic snapshot hash.
- Debug mode resolves configs from the DB (debug role) when no explicit override.

## Sector normalization

`provider raw sector -> SECTOR_ALIAS_MAP (exact, then case-insensitive) -> canonical
sector stored in ticker tables -> SECTOR_ETF_MAP (canonical keys) -> ETF`.
Unknown sectors pass through unchanged (not dropped); `None`/empty preserved.

## What remains hardcoded (architecture constants)

db_role names, DB filenames, schema version, table names, pipeline step names,
status labels, service public method signatures, feature schema version naming,
source-of-truth module contracts, and prod/debug/simulation isolation rules.

## Tests added/updated

See "Changed / added files". New behaviors covered: fresh config schema, strategy
+ runtime seeding (incl. idempotency + default values), config hash determinism,
orchestrator config loading (override / DB-active / seed-on-empty), pipeline-run
traceability, and sector normalization (alias mapping + canonical ingestion).

## Test command and results

Run in an environment with `duckdb` + `pytest` installed:

```
pytest -q
```

Offline validation performed in this sandbox (duckdb/pytest not installable here;
project's py_compile + fake-driven pattern):
- `py_compile` clean on all changed/new modules and tests.
- Pure logic harness: deterministic hashing (order-independent; change-sensitive),
  validators, default configs, sector normalization (16 alias cases + case-insensitive
  + passthrough/None) — PASS.
- ConfigService code paths via recording fakes: seed strategy/runtime/sector,
  idempotency counts, db_role rejection, get_active, create+activate, rollback,
  runtime read — PASS.
- Orchestrator: `_resolve_configs` (override / DB-active / seed-on-empty),
  `_validate_inputs`, full existing `test_pipeline_orchestrator` suite (37/37),
  new `test_orchestrator_config_loading` (5/5) — PASS.
- Pure `test_sector_normalization` (20 assertions) — PASS.

Not yet executed here (require real DuckDB; should pass in your env):
`test_config_service.py`, the ingestion case in `test_sector_normalization.py`,
the updated `test_schema_manager.py`, and the init integration tests in
`test_tools_runners.py`.

## Source-of-truth files that must be updated

Required:
- Module 03 Schema Manager spec / SCHEMA_SPEC (see specs/M03_SCHEMA_SPEC_CONFIG_DELTA.md)
- Module 06 Universe Snapshot spec (see specs/M06_UNIVERSE_SNAPSHOT_CONFIG_DELTA.md)
- Module 20 Pipeline Orchestrator spec (see specs/M20_PIPELINE_ORCHESTRATOR_CONFIG_DELTA.md)
- Module 21 Dashboard V2 workflow spec (M21_DASHBOARD_V2_CONFIG_MANAGEMENT_ADDENDUM.md — already present)
- Module 22 Debug Mode spec (see specs/M22_DEBUG_MODE_CONFIG_DELTA.md)
- `02_PROJECT_IMPLEMENTATION_CONTEXT.md` (note ConfigService + DB-config-as-runtime-truth)
- SOURCE_OF_TRUTH_INDEX.md / PROJECT_STATUS_CURRENT.md (record this change)

Conditional (only if their contract actually changed — it did NOT in this change):
M04, M05, M07, M09, M11, M12, Step 3/4/5 specs, M17, M18, M19.

## Risks / remaining TODOs

- Runtime configs are seeded and queryable but not yet consumed at runtime (reference/visibility-only; `lock_stale_seconds` seed value matches the hardcoded constant so there is no conflict). Wiring consumers to read from the DB is follow-up work.
- Sector normalization reads `constants.SECTOR_ALIAS_MAP` at runtime; the DB `sector_alias_map` table is seeded from it for dashboard visibility/future editing but is not yet the live source.
- Dashboard Settings/Config UI not implemented (backend service support only), per addendum §10.

> **Note (v4):** `db_role="simulation"` is fully supported. `ConfigService.seed_defaults("simulation")` works; the simulation schema includes all four config tables (`strategy_configs`, `runtime_configs`, `config_activation_log`, `sector_alias_map`). `ALLOWED_CONFIG_DB_ROLES = ("prod", "debug", "simulation")`.
