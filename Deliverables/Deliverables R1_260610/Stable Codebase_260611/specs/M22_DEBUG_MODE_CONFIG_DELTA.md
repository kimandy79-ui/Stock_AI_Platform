# M22 Debug Mode — Config Management Delta (final)

## Config loading
- New injectable: `config_service` (defaults to `ConfigService(db_manager)`).
- `_load_active_debug_configs()` returns `(configs_by_id, config_ids_by_strategy)`.
- `_resolve_strategy_configs(names)`:
  - DB path: selects by strategy_name via `config_ids_by_strategy`, then keys
    the result by real DB `config_id` (not strategy name).
  - Explicit `strategy_configs` override: caller keys are used verbatim (tests).
- Orchestrator receives `{seed_strategy_debug_normal_v1: {...}}` not `{normal: {...}}`.
- Debug outputs store the same `strategy_config_id` format as the prod pipeline.

## Safety unchanged
- Forced `db_role=debug` / `run_type=debug` remain hardcoded.
- Debug can never write to prod.

## run_debug_pipeline auto-init
- `_ensure_debug_db()`: after `apply_debug_schema()`, calls
  `ConfigService().seed_defaults("debug")`.
- Returns error string if seeding fails; controller is never called on failure.
