# M11_CONFIG_SERVICE_SPEC.md

Setup-mode migration (AD-22.19–22.24). Replaces the legacy M21 ConfigService contract.

## Tables

### setup_configs
```
config_id VARCHAR PRIMARY KEY
setup_type VARCHAR NOT NULL  -- breakout|pullback|trend_continuation|consolidation_base
version VARCHAR NOT NULL
parent_config_id VARCHAR
config_json JSON NOT NULL
config_hash VARCHAR NOT NULL  -- deterministic SHA-256 of canonical_json(config_json)
active_flag BOOLEAN NOT NULL DEFAULT FALSE
created_at TIMESTAMP NOT NULL
notes TEXT
```
**Activation constraint**: exactly one `active_flag=TRUE` row per `setup_type` in prod/debug.
Enforced by service layer (not DB constraint). Simulation may hold multiple for comparison.

### risk_label_config
```
config_id VARCHAR PRIMARY KEY
version VARCHAR NOT NULL
config_json JSON NOT NULL
config_hash VARCHAR NOT NULL
active_flag BOOLEAN NOT NULL DEFAULT FALSE
created_at TIMESTAMP NOT NULL
notes TEXT
```
**Activation constraint**: exactly one `active_flag=TRUE` row in prod/debug.

## ConfigService public API

```python
ConfigService(db_manager=None)  # injects duckdb_manager if None

# Seeding (idempotent)
seed_default_setup_configs(db_role: str) -> ServiceResult
seed_default_risk_label_config(db_role: str) -> ServiceResult
seed_sector_alias_map(db_role: str) -> ServiceResult
seed_defaults(db_role: str) -> ServiceResult  # all three

# Setup config reads
get_active_setup_config(db_role: str, setup_type: str) -> ServiceResult
    # metadata: {config_id, setup_type, config_json, config_hash}
get_all_active_setup_configs(db_role: str) -> ServiceResult
    # metadata: {configs: {setup_type: config_json}, configs_by_id, configs_by_type}

# Setup config writes
activate_setup_config(config_id: str, db_role: str, setup_type: str) -> ServiceResult
    # deactivates siblings for same setup_type, then activates

# Risk label config reads
get_active_risk_label_config(db_role: str) -> ServiceResult
    # metadata: {config_id, config_json, config_hash}

# Validation helpers
validate_setup_config(config_json: dict) -> ServiceResult
    # checks: setup_type valid, scoring_weights sum == 1.0
validate_risk_label_config(config_json: dict) -> ServiceResult
    # checks: factor_weights sum == 1.0

# Universe parity assertion (required before Step 3 runs)
assert_universe_config_parity(db_role: str) -> ServiceResult
    # asserts all four active setup configs have identical universe blocks

# Runtime configs (served from in-memory defaults; runtime_configs table retired)
get_active_runtime_config(db_role: str, config_type: str) -> ServiceResult
```

## config_validator module

```python
ALLOWED_SETUP_TYPES = ("breakout", "pullback", "trend_continuation", "consolidation_base")
ALLOWED_CONFIG_DB_ROLES = ("prod", "debug", "simulation")
ALLOWED_CONFIG_TYPES = ("pipeline", "provider", "data_completeness", "debug",
                        "simulation", "dashboard", "ai_review", "export")

validate_setup_type(setup_type) -> str   # raises ConfigValidationError if invalid
validate_db_role(db_role) -> str
validate_config_type(config_type) -> str
validate_config_payload(config_json) -> dict
canonical_json(config_json) -> str       # sorted keys, tight separators
deterministic_hash(config_json) -> str   # SHA-256 of canonical_json
```

## Seeded configs (migration starting points — not tuned)

| config_id                        | setup_type            |
|----------------------------------|-----------------------|
| setup_breakout_v1                | breakout              |
| setup_pullback_v1                | pullback              |
| setup_trend_continuation_v1      | trend_continuation    |
| setup_consolidation_base_v1      | consolidation_base    |
| risk_label_config_v1             | (risk label config)   |

## Retired from legacy M21 contract

- `strategy_configs` table → replaced by `setup_configs`
- `runtime_configs` table → retired (runtime configs served from defaults)
- `config_activation_log` table → not in setup-mode schema
- `ConfigService.seed_default_strategy_configs()` → replaced by `seed_default_setup_configs()`
- `ConfigService.get_active_strategy_configs()` → replaced by `get_all_active_setup_configs()`
- `ALLOWED_STRATEGY_NAMES = (normal, aggressive, conservative)` → retired
- `validate_strategy_name()` → replaced by `validate_setup_type()`

## Constants (constants.py)

```python
FEATURE_SCHEMA_VERSION = "features_v02"  # bumped from features_v01 (AD-22.8/22.19)
ALLOWED_SETUP_TYPES = ("breakout", "pullback", "trend_continuation", "consolidation_base")
ALLOWED_RISK_LABELS = ("low", "medium", "high")
ALLOWED_DISPOSITIONS = ("BUY", "WATCHLIST_ONLY", "REJECTED")
```
