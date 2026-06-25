# M20 Pipeline Orchestrator — Config Management Delta (final)

## Config loading
- New injectable: `config_service` (defaults to `ConfigService(db_manager)`).
- `run(strategy_configs=None)`:
  - Explicit non-empty dict: used verbatim (tests/debug/manual). Its keys are
    the ids passed downstream and stored in `pipeline_runs`.
  - None: loads active configs from ConfigService, keyed by real DB
    `strategy_configs.config_id`. Seeds defaults if none active.
- `_validate_inputs` accepts `strategy_configs=None`; explicit must be non-empty dict.

## Config id contract
- `_resolve_configs` returns `(configs_by_id, strategy_config_ids, runtime_config_ids)`.
- `configs_by_id` keys = ids passed to Step 3/4/5/outcome engines as `strategy_config_id`.
- `strategy_config_ids = list(configs_by_id)`.
- `pipeline_runs.strategy_config_ids_json` always matches the actual output-table ids.

## Traceability
- After the running row is written, `pipeline_runs` is updated with:
  - `strategy_config_ids_json`: list of ids used.
  - `runtime_config_ids_json`: list of active runtime config ids (best-effort).
  - `config_snapshot_hash`: `snapshot_hash({"strategy_configs_by_id": ..., "runtime_config_ids": ...})`.
- Write targets remain pipeline tables only (boundary test still passes).

## ConfigService metadata
`get_active_strategy_configs` returns:
```
configs              (back-compat, by strategy_name)
configs_by_strategy  (by strategy_name)
configs_by_id        (by config_id — primary consumer)
config_ids_by_strategy (name -> config_id, for debug selection)
config_ids           (list of config_ids)
```

## Removed
- `_SECTOR_ETF_MAPPING_BLOCK` literal; uses `constants.SECTOR_ETF_MAP`.
- Silent `DEFAULT_STRATEGY_CONFIGS` fallback; now DB-first with seed-on-missing.
