# 02_PROJECT_IMPLEMENTATION_CONTEXT.md

Status: AI-friendly implementation context for Claude Project Files.  
Purpose: provide architecture, roadmap, coding rules, and active decisions for coding.  
Use together with: `01_MASTER_SOURCE_OF_TRUTH_MERGED.md`.

Last updated: 2026-06-19
Setup-mode context patch: 2026-06-19

---

## SETUP-MODE OVERRIDE NOTICE (AD-22.19–22.24)

Setup mode is the active selection architecture. The old aggressive / normal /
conservative strategy mode is retired as an active selection driver.

For product logic, schema, formulas, module contracts, dashboard behavior, and
trading behavior, the active Source of Truth is:

1. `01a_CORE_PRINCIPLES.md`
2. `01b_SCHEMA_AND_DATA.md`
3. `01c_FORMULAS_AND_CONFIGS.md`
4. `01d_MODULES_AND_PIPELINE.md`
5. `01e_UI_AND_TESTING.md`
6. `02b_ARCHITECTURE_DECISIONS.md` §22.19–22.24

This file remains implementation context only: workflow, coding standards,
folder layout, service-result pattern, and general engineering rules. If older
wording in this file or any old `MXX_*_SPEC.md` conflicts with setup mode, the
files listed above win.

Old standalone `MXX_*_SPEC.md` files must not be treated as active contracts
unless they have been rewritten for setup mode or explicitly marked as migrated.

---

## QUICK REFERENCE

- Source/file navigation: `00_PROJECT_FILE_MAP.md`
- Module responsibilities: §7
- Pipeline order: §6
- Coding standards: §11–§17
- Architecture decisions index: §22
- Full architecture decisions: `02b_ARCHITECTURE_DECISIONS.md`
- Setup-mode migration decisions: `02b_ARCHITECTURE_DECISIONS.md` §22.19–22.24
- Prompt pattern: §25

Active §22 decision groups:

- Schema/DB: 22.1, 22.4, 22.17, 22.18
- Features: 22.6, 22.7, 22.8
- Proposals: 22.11, 22.12, 22.13
- Outcomes/Simulation: 22.9, 22.14, 22.15, 22.16
- Setup-mode migration: 22.19, 22.20, 22.21, 22.22, 22.23, 22.24

---

# 0. Claude Usage Rules

This file is project implementation context.

Claude must follow these rules:

1. Use `01_MASTER_SOURCE_OF_TRUTH_MERGED.md` as the main product/spec source of truth.
2. Use this file for architecture, module boundaries, implementation sequence, coding standards, and active decisions.
3. Do not use old FULL / PATCH / MINI PATCH archives as coding source if `01_MASTER_SOURCE_OF_TRUTH_MERGED.md` is available.
4. Do not use `manifest.json` as implementation guidance.
5. Implement one module at a time.
6. Do not add unrequested functionality.
7. Do not reopen architecture unless implementation reveals a true blocker.
8. If there is a conflict:
   - `01_MASTER_SOURCE_OF_TRUTH_MERGED.md` wins for product logic, formulas, schema, and trading behavior.
   - This file wins for implementation workflow, coding standards, module boundaries, and active architecture decisions.
9. For Module 03 Schema Manager:
   - create the final merged schema directly;
   - do not create the old base schema first and then immediately apply ALTER patches on a fresh DB.
10. For each coding module, produce tests and keep changes inside the module scope.
11. Setup-mode rule: do not implement legacy strategy-first contracts. Active selection uses `setup_config_id` + `setup_type`; risk is `risk_label` (`low | medium | high`) assigned after setup validation.
12. Old `MXX_*_SPEC.md` files are advisory only until migrated; setup-mode specs in `01a`–`01e` and `02b` override them.

---

# 1. System Type

Local research-grade swing trading platform.

Main objective:
daily EOD stock screening, proposal generation, outcome tracking, simulation, and AI-assisted review.

The system is research-grade V1, not institutional-grade infrastructure.

V1 does not include:
- auto-trading;
- broker integration;
- cloud deployment;
- multi-user production service;
- institutional data SLA.

---

# 2. Recommended Folder Structure

```text
stock_ai_platform/
  app/
    main.py
    config/
      settings.py
      constants.py
      env.py
    database/
      duckdb_manager.py
      schema_manager.py
      migrations/
      query_repository.py
    providers/
      provider_interface.py
      yahoo_provider.py
      nasdaq_provider.py
      earnings_provider.py
      macro_calendar_provider.py
    services/
      downloader/
      features/
      screening/
      analysis/
      proposal/
      outcomes/
      simulation/
      ai_review/
      feedback/
      pipeline/
    dashboard/
      app.py
      pages/
      components/
    utils/
      service_result.py
      logging_config.py
      trading_calendar.py
    tests/
  data/
    duckdb/
      prod.duckdb
      debug.duckdb
      simulation.duckdb
    logs/
    exports/
    backups/
  docs/
    MASTER_SPEC.md
    ARCHITECTURE.md
    DECISIONS_LOG.md
    TODO_ROADMAP.md
    CODING_STANDARDS.md
```

---

# 3. Core Stack

- Python 3.11+
- DuckDB local files
- Polars-first processing
- Streamlit local dashboard
- YahooProvider V1 behind provider abstraction
- pandas-market-calendars for US trading days
- keyring / Windows Credential Manager for API keys
- pytest for testing
- pydantic
- python-dotenv
- numpy
- pandas only when unavoidable

---

# 4. Database Architecture

Use separate DuckDB files:

```text
prod.duckdb
debug.duckdb
simulation.duckdb
```

Rules:

- Production writes only to `prod.duckdb`.
- Debug mode writes only to `debug.duckdb`.
- Simulation writes only to `simulation.duckdb`.
- Simulation may attach production read-only:

```sql
ATTACH 'data/duckdb/prod.duckdb' AS prod (READ_ONLY);
```

Debug data must never contaminate production.

DuckDB concurrency:

- single-writer discipline;
- dashboard uses read-only connection;
- pipeline uses write connection;
- avoid concurrent writes.

---

# 5. Core Data Flow

```text
Provider
→ daily_prices
→ daily_features
→ step3_candidates
→ step4_analysis
→ step5_proposals
→ outcome_tracking_queue
→ signal_outcomes
→ feedback / simulation / AI review / dashboard
```

---

# 6. Daily Pipeline Order

1. Acquire pipeline lock
2. Check already-run state
3. Load benchmarks and sector ETFs
4. Refresh ticker universe if due
5. Download daily stock data
6. Validate data
7. Detect splits / mutations
8. Repair missing data
9. Calculate daily features
10. Step 3 universal eligibility + setup routing
11. Step 4 setup validation + trade plan
12. Step 5 risk labeling + disposition + proposal engine
13. Create outcome queue
14. Process due outcomes
15. Refresh dashboard/materialized views
16. Export/log/backup
17. Release lock

---

# 7. Module Responsibilities

## Module 01 — Project Skeleton

Creates:
- folder structure;
- config loading;
- constants;
- logging;
- `ServiceResult`;
- base tests.

Scope boundaries:
- no database schema implementation;
- no provider implementation;
- no feature engine;
- no dashboard logic.

## Module 02 — DuckDB Manager

Manages DuckDB connections for:
- prod DB;
- debug DB;
- simulation DB.

Rules:
- all DB access goes through DuckDB manager;
- no module opens arbitrary DB paths directly.

## Module 03 — Schema Manager

Creates the final merged schema.

Critical rule:
- use merged final schema from Master TZ v1 FULL + PATCH 1 + MINI-PATCH 2;
- do not create base schema first and then apply ALTER patches on a fresh DB;
- create final merged schema directly.

## Module 04 — Provider Interface

Defines abstract data provider contract.

Rules:
- no direct Yahoo calls outside provider layer;
- concrete providers must follow provider interface.

## Module 05 — YahooProvider

Implements market data download behind provider interface.

## Module 06 — Universe Snapshot Engine

Maintains ticker universe and monthly snapshots.

## Module 07 — Benchmark / Sector ETF Loader

Loads:
- SPY;
- QQQ;
- ^VIX;
- sector ETFs.

Must run before feature engine.

## Module 08 — Daily Price Ingestion

Downloads and updates daily prices.

## Module 09 — Data Validator

Validates:
- OHLCV consistency;
- missing rows;
- suspicious values.

## Module 10 — Mutation Detector

Detects:
- splits;
- historical mutations;
- rebuild needs.

## Module 11 — Feature Engine

Calculates `daily_features` using Polars.

Setup mode requires `features_v02`, including structural features needed for
setup validation and setup-aware stop/target logic. Must follow formulas from
`01a`–`01e` / `02b` setup-mode Source of Truth.

## Module 12 — Market Regime Engine

Computes market regime from:
- SPY;
- QQQ;
- VIX.

Can be called by Feature Engine.

## Module 13 — Step 3 Universal Eligibility + Setup Routing

Applies universal tradability/data filters only, then routes eligible tickers to
candidate setup types. Step 3 runs once per signal date. RVOL, setup score,
momentum, ATR%, EMA extension, and consolidation quality are not universal hard
gates.

## Module 14 — Step 4 Setup Validation + Trade Plan

Validates each routed setup under the matching active `setup_config_id` and
creates the structural trade plan: entry proxy, stop, target, estimated RR.
Uses `entry_proxy_raw`. Applies raw/adjusted conversion for structural levels.

## Module 15 — Step 5 Risk Labeling + Proposal Engine

Assigns `risk_score`, `risk_label`, `disposition`, dedupes multi-route tickers,
then calculates raw and diversified rankings.

## Module 16 — Outcome Queue

Creates and processes 5/10/20/40bd outcome tasks. Outcomes carry `setup_type`,
`risk_label`, `stop_price_raw`, and `target_price_raw`.

Must track proposals where:

```text
in_raw_top_n = TRUE OR in_diversified_top_n = TRUE
```

## Module 17 — Simulation Engine

Runs research simulation, walk-forward testing, and config comparisons in setup
mode. Groups results by `setup_type` and `risk_label`; compares raw and
diversified Top-N lists.

## Module 18 — Export Package Engine

Creates ticker/simulation review ZIPs.

Must include both raw and diversified ranks.

## Module 19 — AI Review Engine

Sends review packages to AI provider only when user manually clicks.

AI review is qualitative overlay only.

## Module 20 — Pipeline Orchestrator

Coordinates daily pipeline and failure recovery.

## Module 21 — Streamlit Dashboard

Local single-user UI.

Primary filters are `setup_type` and `risk_label`. Dashboard reads precomputed
DuckDB tables/views and does not run heavy calculations live.

## Module 22 — Debug Mode

Fast testing with sampled tickers and partial pipeline runs. Includes setup-mode
funnel diagnostics: universal eligibility → routing → setup validation → trade
plan → risk labeling → proposals.

Debug mode writes only to `debug.duckdb`.

---

# 8. Module Roadmap

| # | Module | Status | Notes |
|---|---|---|---|
| 01 | Project Skeleton | READY | Start here |
| 02 | DuckDB Manager | READY | After Module 01 |
| 03 | Schema Manager | READY WITH NOTE | Create final merged schema directly |
| 04 | Provider Interface | READY | Abstract provider contract |
| 05 | YahooProvider | READY | After provider interface |
| 06 | Universe Snapshot Engine | READY | Monthly snapshots |
| 07 | Benchmark / Sector ETF Loader | READY | Load SPY, QQQ, ^VIX, sector ETFs before features |
| 08 | Daily Price Ingestion | READY | Raw + adjusted data |
| 09 | Data Validator | READY | OHLCV checks |
| 10 | Mutation Detector | READY | Split/mutation handling |
| 11 | Feature Engine | READY | Use setup-mode `features_v02` formulas |
| 12 | Market Regime Engine | READY | Can be called from Feature Engine |
| 13 | Step 3 Universal Eligibility + Routing | READY | No RVOL/score/momentum universal hard gate |
| 14 | Step 4 Setup Validation + Trade Plan | READY | Setup-specific validation; structural stop/target |
| 15 | Step 5 Risk Labeling + Proposals | READY | Risk label, disposition, raw + diversified ranking |
| 16 | Outcome Queue | READY | Track raw OR diversified Top 20 |
| 17 | Simulation Engine | READY | list_type/list_membership |
| 18 | Export Package Engine | READY | Include both ranks |
| 19 | AI Review Engine | READY | Manual send only |
| 20 | Pipeline Orchestrator | READY | Lock/heartbeat/resume |
| 21 | Streamlit Dashboard | READY | Setup/risk filters; checkbox for diversified list |
| 22 | Debug Mode | READY | Setup-mode funnel diagnostics; separate debug.duckdb |

---

# 9. Implementation Sequence

## Phase 1 — Foundation

1. Module 01
2. Module 02
3. Module 03
4. Module 04

## Phase 2 — Data Layer

5. Module 05
6. Module 06
7. Module 07
8. Module 08
9. Module 09
10. Module 10

## Phase 3 — Features and Screening

11. Module 11
12. Module 12
13. Module 13
14. Module 14
15. Module 15

## Phase 4 — Outcomes and Simulation

16. Module 16
17. Module 17

## Phase 5 — User Tools

18. Module 18
19. Module 19
20. Module 20
21. Module 21
22. Module 22

---

# 10. Core Service Contract

All service modules should return:

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ServiceResult:
    status: str
    run_id: str
    rows_processed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

Allowed status:

```text
success
success_with_warnings
failed
```

All modules generally accept:
- `db_manager`;
- `run_id`;
- `config`;
- date range / signal date where applicable.

---

# 11. General Coding Rules

- Implement one module at a time.
- Do not mix modules.
- Do not add unrequested features.
- Do not hardcode tunable trading thresholds outside config.
- Do not call provider APIs outside provider layer.
- Do not bypass DuckDB manager.
- Do not use pandas unless unavoidable.
- Prefer Polars for data transformations.
- All functions must have type hints.
- All modules must have module-level docstrings.
- Use snake_case.py for files.
- Use PascalCase for classes.
- Use snake_case for functions.
- Use UPPER_SNAKE_CASE for constants.
- Use UUID4 strings for IDs.
- Use ISO date format.
- Use `datetime.date` for dates.
- Use timezone-aware timestamps where possible.

---

# 12. Logging Rules

Use Python logging.

Format:

```text
timestamp | level | module | run_id | message
```

Every service logs:
- start;
- end;
- rows processed;
- warnings;
- errors.

Do not print from library/service modules.

Dashboard may display messages via Streamlit.

---

# 13. Error Handling Rules

## Warning

Continue:

- low-confidence earnings;
- missing sector;
- partial provider failures.

## Recoverable failure

Continue degraded:

- some tickers failed download;
- repair queue populated.

## Critical failure

Stop:

- DB unavailable;
- schema mismatch;
- invalid config;
- look-ahead validation failure;
- feature calculation crash.

---

# 14. Config Rules

Config is immutable.

Never edit setup configs or risk-label config in place. Clone and create a new
config version, then activate it through approved service logic.

Active config model:
- `setup_configs`: one active config per `setup_type` in prod/debug.
- `risk_label_config`: one active risk-label config in prod/debug.
- Simulation may compare multiple setup configs.

Universal tradability filters are global or identical across all active setup
configs. RVOL, setup score, momentum, ATR%, EMA extension, and consolidation
quality are setup-validation rules, not universal eligibility hard gates.

Tunable thresholds live in JSON config or immutable preset settings, for example:
- `min_price` and `min_avg_dollar_volume_20d` for universal tradability;
- setup-specific RVOL / range / pullback / extension thresholds;
- `min_setup_score`;
- `min_rr_for_buy`;
- `sector_max_positions`;
- `industry_max_positions`;
- `earnings_avoid_window`.

Structural domain constants may live in `app/config/constants.py`.

---

# 15. Performance Rules

- Avoid ticker-by-ticker Python loops when batch/vectorized processing is possible.
- Use Polars for groupby, rolling/window calculations, joins, and feature calculations.
- Query only needed columns.
- Use date filters.
- Dashboard should read precomputed tables/views.
- Heavy calculations run in pipeline, not live in dashboard.

---

# 16. Testing Requirements

Every module must include pytest tests.

Minimum test types:
- unit tests;
- integration hooks;
- invalid input test;
- empty data test;
- expected output test where feasible.

Critical tests:
- no look-ahead;
- schema creation idempotency;
- feature formulas;
- ranking reproducibility;
- raw vs diversified ranking;
- outcome queue membership;
- outcome returns use `entry_price_sim`, not `entry_price_raw`;
- simulation read-only attach.

---

# 17. Documentation Style

Every Python file should have:
- module docstring;
- clear class/function docstrings;
- comments only where logic is non-obvious.

Avoid giant unclear comments.

---

# 18. Git Rules

Commit after each stable module.

Commit message style:

```text
module01_project_skeleton_stable
module02_duckdb_manager_stable
module03_schema_manager_stable
module04_provider_interface_stable
```

Use rollback if AI breaks working code.

---

# 19. Dashboard Rules

Streamlit is local single-user.

Do not do heavy calculations live in dashboard.

Dashboard reads from DuckDB and precomputed outputs.

Checkboxes and filters may use `st.session_state`.

---

# 20. Failure Recovery

Pipeline runs are stored in `pipeline_runs`.

Locks are stored in `pipeline_locks`.

Heartbeat:
- interval: 60 seconds;
- stale lock threshold: 5 minutes.

Failed run:
- status = failed;
- steps_completed JSON stores progress;
- resume from failed step allowed.

---

# 21. Debug Architecture

Debug mode writes to:

```text
debug.duckdb
```

Presets:
- 20 tickers, 5 days;
- 10 tickers, 90 days indicators;
- 100 tickers, 30 days;
- 500 tickers, 6 months.

Debug never writes to prod.

---

# 22. Active Architecture Decisions Index

Full text is in `02b_ARCHITECTURE_DECISIONS.md`.

- 22.1 Use DuckDB only
- 22.2 Polars-first processing
- 22.3 Research-grade V1, not institutional production-grade
- 22.4 Separate DB files
- 22.5 YahooProvider V1 behind provider abstraction
- 22.6 Store raw and adjusted prices
- 22.7 Use feature_cutoff_date
- 22.8 Use zero-padded feature schema versions
- 22.9 Use monthly universe snapshots
- 22.10 Market regime uses SPY, QQQ, VIX
- 22.11 Use raw Top 20 and diversified Top 20
- 22.12 Hard cap mode means no soft penalty in V1
- 22.13 Outcome queue tracks raw OR diversified Top 20
- 22.14 Use entry_price_sim for performance
- 22.15 Step 4 uses signal-date close as entry proxy
- 22.16 Define trend_resume setup detection rule (reconciled under trend_continuation in setup mode)
- 22.17 Module 03 must use merged final schema
- 22.18 Structural domain constants live in constants.py
- 22.19 Setup mode is the primary selection architecture
- 22.20 Active setup taxonomy
- 22.21 Setup-mode schema model
- 22.22 Setup configs replace strategy configs
- 22.23 RVOL is setup-specific, not a universal hard gate
- 22.24 Frozen-module exemption scope for setup-mode migration

# 23. Accepted V1 Limitations

- YahooProvider is research-grade.
- YahooProvider has no SLA.
- Historical delisted data is incomplete.
- Monthly universe snapshots are survivorship-bias mitigation, not perfect solution.
- Residual survivorship bias remains.
- Macro calendar may be manually maintained CSV.
- Earnings source may be LOW confidence in V1.
- No intraday fill modeling.
- No broker integration.
- No auto-trading.
- No cloud deployment.

---

# 24. Do Not Use / Excluded From Claude Project Files

The following should not be used as implementation guidance when this file and `01_MASTER_SOURCE_OF_TRUTH_MERGED.md` are available:

- `manifest.json`;
- duplicate manifests from older folders;
- old raw FULL / PATCH / MINI PATCH archives;
- old AI prompt drafts;
- old Claude review prompts;
- old standalone ALTER-patch instructions;
- intermediate checklists that conflict with the merged source of truth.

Reason:
these files can cause Claude to retrieve outdated or duplicate context.

---

# 25. Recommended Claude Coding Prompt Pattern

Use this pattern when starting a module in Claude:

```text
Use Project Files as source of truth.

Primary source:
- 01a_CORE_PRINCIPLES.md
- 01b_SCHEMA_AND_DATA.md
- 01c_FORMULAS_AND_CONFIGS.md
- 01d_MODULES_AND_PIPELINE.md
- 01e_UI_AND_TESTING.md
- 02b_ARCHITECTURE_DECISIONS.md §22.19–22.24

Implementation context:
- 02_PROJECT_IMPLEMENTATION_CONTEXT.md

Task:
Implement Module XX: <module name>.

Scope:
- implement only this module;
- do not implement later modules;
- do not change unrelated files;
- follow coding standards;
- include pytest tests;
- keep all thresholds/config rules consistent with Project Files.

Important:
If there is a conflict, ask only if it blocks implementation. Otherwise follow the setup-mode Source of Truth. Do not implement legacy strategy-first contracts.
```

For Module 03, add:

```text
Critical Module 03 rule:
Create the final merged + setup-mode schema directly from `01b_SCHEMA_AND_DATA.md`.
Do not create old base tables first and then apply ALTER patches on a fresh DB.
```

For Module 04, add:

```text
Critical Module 04 rule:
Define provider interface only.
Do not implement YahooProvider yet.
Do not call Yahoo directly.
```
