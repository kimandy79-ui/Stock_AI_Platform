# M03 Schema Manager — Config Management Delta (M21 Review Final)

## strategy_configs (extended, prod/debug/simulation)
```
config_id VARCHAR PRIMARY KEY,
db_role VARCHAR NOT NULL,            -- added
strategy_name VARCHAR NOT NULL,
version VARCHAR NOT NULL,
parent_config_id VARCHAR,
config_json JSON NOT NULL,
config_hash VARCHAR NOT NULL,
active_flag BOOLEAN NOT NULL DEFAULT FALSE,
created_at TIMESTAMP NOT NULL,
created_by VARCHAR,                  -- added
notes TEXT
```

## pipeline_runs (extended, prod/debug only)
```
strategy_config_ids_json JSON,       -- added
runtime_config_ids_json JSON,        -- added
config_snapshot_hash VARCHAR,        -- added
```

## runtime_configs (new, all roles)
```
config_id VARCHAR PRIMARY KEY, db_role VARCHAR NOT NULL, config_type VARCHAR NOT NULL,
version VARCHAR NOT NULL, parent_config_id VARCHAR, config_json JSON NOT NULL,
config_hash VARCHAR NOT NULL, active_flag BOOLEAN NOT NULL DEFAULT FALSE,
created_at TIMESTAMP NOT NULL, created_by VARCHAR, notes TEXT
```

## config_activation_log (new, all roles)
```
activation_id VARCHAR PRIMARY KEY, config_id VARCHAR NOT NULL, db_role VARCHAR NOT NULL,
config_type VARCHAR NOT NULL,   -- "strategy" for strategy configs; config_type for runtime
profile_name VARCHAR,           -- strategy_name for strategy configs; NULL for runtime
activated_at TIMESTAMP NOT NULL, activated_by VARCHAR, reason TEXT
```

## sector_alias_map (new, all roles)
```
source VARCHAR NOT NULL, raw_sector VARCHAR NOT NULL, canonical_sector VARCHAR NOT NULL,
active_flag BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMP NOT NULL,
PRIMARY KEY (source, raw_sector)
```

## Table counts
- prod/debug: 24 tables (was 20). 9 indexes, 2 views.
- simulation: 13 tables (was 9). 2 indexes. 0 views.
  - 4 shared config tables + `schema_versions` + 8 `sim_*` tables.

## DDL construction
`_SIM_TABLE_DDL` is built by filtering `_PROD_TABLE_DDL` for the four config DDL
strings, then prepending them to the sim-specific DDL. No fragile index slicing.

## Seeding
ConfigService (M21) seeds strategy/runtime/sector-alias defaults for all three
roles on fresh init. schema_manager itself does NOT seed config rows.
