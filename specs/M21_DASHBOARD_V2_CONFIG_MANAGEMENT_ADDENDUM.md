# M21 Dashboard V2 Config Management Addendum

## Status

This addendum is the current source-of-truth update for configuration/settings management, runtime config storage, fresh DB initialization, and sector naming alignment.

It applies to the current stable codebase:

```text
Current_Stable_Codebase_260610.zip
```

This addendum does not create Module 23. All dashboard/settings work remains under Module 21 V2, with required supporting changes to:

```text
M03 Schema Manager
M06 Universe Snapshot
M20 Pipeline Orchestrator
M21 Dashboard V2
M22 Debug Mode
```

Other module specs should be updated only if their actual contract changes during implementation.

---

# 1. Architecture Decision

The project shall use DuckDB-backed versioned configuration for runtime/trading settings.

Python hardcoded values may remain only for:

1. architecture constants, or
2. seed defaults used during fresh DB initialization.

Runtime pipeline execution must not silently depend on hardcoded `DEFAULT_STRATEGY_CONFIGS` after DB config support is implemented.

The runtime source of truth for active strategy settings shall be DuckDB config tables loaded through `ConfigService`.

---

# 2. DB Reset Decision

The user has decided to delete/recreate local DuckDB databases and start fresh from the updated schema.

Therefore:

- no migration of old populated DB rows is required;
- no backward data migration is required;
- schema may be updated cleanly in `schema_manager.py`;
- DB initialization must create the final config/settings schema;
- user will manually delete or rename old DB files;
- code must not automatically delete DB files.

---

# 3. Required Config Tables

## 3.1 `strategy_configs`

The existing `strategy_configs` table must be kept because result tables already reference `strategy_config_id`.

It must support db-role-specific active configs.

Required logical fields:

```sql
config_id VARCHAR PRIMARY KEY,
db_role VARCHAR NOT NULL,
strategy_name VARCHAR NOT NULL,
version VARCHAR NOT NULL,
parent_config_id VARCHAR,
config_json JSON NOT NULL,
config_hash VARCHAR NOT NULL,
active_flag BOOLEAN NOT NULL DEFAULT FALSE,
created_at TIMESTAMP NOT NULL,
created_by VARCHAR,
notes TEXT
```

Required strategy names:

```text
normal
aggressive
conservative
```

Required DB roles:

```text
prod
debug
simulation
```

## 3.2 `runtime_configs`

A generic runtime config table must be added for non-strategy settings.

Required logical fields:

```sql
config_id VARCHAR PRIMARY KEY,
db_role VARCHAR NOT NULL,
config_type VARCHAR NOT NULL,
version VARCHAR NOT NULL,
parent_config_id VARCHAR,
config_json JSON NOT NULL,
config_hash VARCHAR NOT NULL,
active_flag BOOLEAN NOT NULL DEFAULT FALSE,
created_at TIMESTAMP NOT NULL,
created_by VARCHAR,
notes TEXT
```

Required config types:

```text
pipeline
provider
data_completeness
debug
simulation
dashboard
ai_review
export
```

## 3.3 `config_activation_log`

A config activation log must be added.

Required logical fields:

```sql
activation_id VARCHAR PRIMARY KEY,
config_id VARCHAR NOT NULL,
db_role VARCHAR NOT NULL,
config_type VARCHAR NOT NULL,
profile_name VARCHAR,
activated_at TIMESTAMP NOT NULL,
activated_by VARCHAR,
reason TEXT
```

## 3.4 `sector_alias_map`

A sector alias table must be added for canonical sector normalization.

Required logical fields:

```sql
source VARCHAR NOT NULL,
raw_sector VARCHAR NOT NULL,
canonical_sector VARCHAR NOT NULL,
active_flag BOOLEAN NOT NULL DEFAULT TRUE,
created_at TIMESTAMP NOT NULL,
PRIMARY KEY (source, raw_sector)
```

---

# 4. Pipeline Run Traceability

`pipeline_runs` must store config metadata for future reproducibility.

Required fields:

```text
strategy_config_ids_json JSON
runtime_config_ids_json JSON
config_snapshot_hash VARCHAR
```

Each new pipeline run must record:

1. strategy config IDs used;
2. runtime config IDs used where applicable;
3. deterministic hash of the resolved config snapshot.

---

# 5. Default Config Seeding

Fresh DB initialization must seed default configs.

Seeding must be idempotent.

## 5.1 Strategy Config Seeds

Seed active strategy configs for:

```text
normal
aggressive
conservative
```

Use the current `DEFAULT_STRATEGY_CONFIGS` values as seed data.

After implementation:

- `DEFAULT_STRATEGY_CONFIGS` may remain in Python only as seed data;
- pipeline runtime must load active configs from DB through `ConfigService`;
- explicit `strategy_configs` argument may still override DB configs for tests/debug/manual use.

## 5.2 Runtime Config Seeds

Seed default runtime configs for:

```text
pipeline
provider
data_completeness
debug
simulation
dashboard
ai_review
export
```

API keys and secrets must not be stored in DuckDB.

Secrets remain in `.env`.

---

# 6. Required ConfigService

Add a service layer:

```text
app/services/config/
app/services/config/__init__.py
app/services/config/default_configs.py
app/services/config/config_service.py
app/services/config/config_validator.py
```

The service must use existing project conventions:

```text
ServiceResult
DuckDBManager
existing logging style
no Streamlit dependency
```

Minimum required methods:

```python
get_active_strategy_configs(db_role: str) -> ServiceResult
seed_default_strategy_configs(db_role: str) -> ServiceResult
seed_default_runtime_configs(db_role: str) -> ServiceResult

list_strategy_configs(db_role: str, strategy_name: str | None = None) -> ServiceResult
get_strategy_config(config_id: str, db_role: str) -> ServiceResult
create_strategy_config_version(...) -> ServiceResult
activate_strategy_config(config_id: str, db_role: str, activated_by: str | None = None, reason: str | None = None) -> ServiceResult

get_active_runtime_config(db_role: str, config_type: str) -> ServiceResult
list_runtime_configs(db_role: str, config_type: str | None = None) -> ServiceResult
create_runtime_config_version(...) -> ServiceResult
activate_runtime_config(config_id: str, db_role: str, activated_by: str | None = None, reason: str | None = None) -> ServiceResult
```

Config hashing must be deterministic:

```text
normalized JSON
sorted keys
same config = same hash
changed config = different hash
```

---

# 7. Pipeline Orchestrator Rule

Current behavior must be changed.

Old behavior:

```text
if strategy_configs provided:
    use explicit configs
else:
    use hardcoded DEFAULT_STRATEGY_CONFIGS
```

Required behavior:

```text
if strategy_configs provided:
    use explicit override
else:
    load active strategy configs from ConfigService for db_role
    if active configs are missing:
        seed defaults into DB for that db_role
        reload active configs
```

Pipeline must store config traceability in `pipeline_runs`.

---

# 8. Sector Naming Alignment

The project must use canonical internal sector names.

Canonical sectors:

```text
Technology
Financials
Healthcare
Consumer Discretionary
Consumer Staples
Communication Services
Industrials
Energy
Materials
Utilities
Real Estate
```

Required Yahoo/source aliases:

```text
Technology -> Technology
Financial Services -> Financials
Financials -> Financials
Healthcare -> Healthcare
Health Care -> Healthcare
Consumer Cyclical -> Consumer Discretionary
Consumer Discretionary -> Consumer Discretionary
Consumer Defensive -> Consumer Staples
Consumer Staples -> Consumer Staples
Communication Services -> Communication Services
Industrials -> Industrials
Energy -> Energy
Basic Materials -> Materials
Materials -> Materials
Utilities -> Utilities
Real Estate -> Real Estate
```

Provider raw sector must be normalized before writing to:

```text
ticker_master.sector
ticker_universe_snapshot.sector
```

`sector_etf_map` must use canonical sector names only.

Duplicated sector ETF mapping in `pipeline_orchestrator.py` must be removed or replaced with a single shared source.

---

# 9. What Must Remain Hardcoded

The following are architecture contracts and must not become DB-editable settings:

```text
db_role names: prod/debug/simulation
DB filenames
schema version
table names
pipeline step names
status labels
service public method signatures
feature schema version naming
source-of-truth module contracts
debug/prod/simulation isolation safety rules
```

---

# 10. Dashboard Rule

Do not implement full editable Settings / Config UI in this task unless trivial.

Backend support must prepare for M21 V2 Settings / Config View:

```text
view active configs
list config versions
create new config version
activate version
rollback by activating previous version
compare configs later
```

Streamlit dashboard must not directly update config tables. It must call service methods.

---

# 11. Source-of-Truth Files Affected

The following source-of-truth files must be updated or replaced after implementation.

## Required updates

```text
Module 03 Schema Manager spec / SCHEMA_SPEC
Module 06 Universe Snapshot spec
Module 20 Pipeline Orchestrator spec
Module 21 Dashboard V2 workflow spec
Module 22 Debug Mode spec
02_PROJECT_IMPLEMENTATION_CONTEXT.md
SOURCE_OF_TRUTH_INDEX.md / PROJECT_STATUS_CURRENT.md
```

## Conditional updates

Update these only if actual implementation changes their contract:

```text
M04 Provider Interface
M05 Yahoo Provider
M07 Benchmark ETF Loader
M09 Data Validator
M11 Feature Engine
M12 Market Regime Engine
Step 3 Screening spec
Step 4 Analysis spec
Step 5 Proposal spec
M17 Simulation Engine
M18 Export Package
M19 AI Review
```

## New file

This file should be added:

```text
M21_DASHBOARD_V2_CONFIG_MANAGEMENT_ADDENDUM.md
```

No Module 23 shall be created.

---

# 12. Expected Final Runtime Behavior

Fresh DB initialization:

```text
init DB
↓
schema creates config/settings tables
↓
seed strategy_configs from current defaults
↓
seed runtime_configs
↓
seed sector_etf_map and sector_alias_map
```

Pipeline run:

```text
pipeline starts
↓
if explicit strategy_configs provided:
    use explicit override
else:
    ConfigService loads active DB strategy configs
↓
pipeline stores config IDs and config snapshot hash
↓
steps use resolved config
```

Sector handling:

```text
provider raw sector
↓
sector_alias_map normalization
↓
canonical sector stored in ticker tables
↓
canonical sector maps to ETF through sector_etf_map
```

---

# 13. Acceptance Criteria

Implementation is acceptable only if:

```text
fresh DB init creates all config tables
default strategy/runtime configs are seeded
pipeline loads active DB strategy configs by default
explicit strategy_configs override still works
pipeline_runs stores config traceability
ticker sectors are canonicalized before storage
sector ETF map uses canonical sectors only
tests pass
no Module 23 is created
```
