# 02_PROJECT_IMPLEMENTATION_CONTEXT.md

Status: AI-friendly implementation context for Claude Project Files.  
Purpose: provide architecture, roadmap, coding rules, and active decisions for coding.  
Use together with: `01_MASTER_SOURCE_OF_TRUTH_MERGED.md`.

Last updated: 2026-05-29

---

## QUICK REFERENCE

- Source/file navigation: `00_PROJECT_FILE_MAP.md`
- Module responsibilities: §7
- Pipeline order: §6
- Coding standards: §11–§17
- Architecture decisions index: §22
- Full architecture decisions: `02b_ARCHITECTURE_DECISIONS.md`
- Prompt pattern: §25

Active §22 decision groups:

- Schema/DB: 22.1, 22.4, 22.17, 22.18
- Features: 22.6, 22.7, 22.8
- Proposals: 22.11, 22.12, 22.13
- Outcomes/Simulation: 22.9, 22.14, 22.15, 22.16

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
10. Step 3 screening
11. Step 4 setup analysis
12. Step 5 proposal engine
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

Must follow formulas from `01_MASTER_SOURCE_OF_TRUTH_MERGED.md`.

## Module 12 — Market Regime Engine

Computes market regime from:
- SPY;
- QQQ;
- VIX.

Can be called by Feature Engine.

## Module 13 — Step 3 Screening

Applies:
- hard filters;
- screening score.

## Module 14 — Step 4 Setup Analysis

Classifies setups and estimates:
- stop;
- target;
- estimated RR.

Uses `entry_proxy_raw`.

## Module 15 — Step 5 Proposal Engine

Calculates:
- raw ranking;
- diversified ranking.

## Module 16 — Outcome Queue

Creates and processes 5/10/20/40bd outcome tasks.

Must track proposals where:

```text
in_raw_top_n = TRUE OR in_diversified_top_n = TRUE
```

## Module 17 — Simulation Engine

Runs:
- research simulation;
- walk-forward testing;
- config comparisons.

Must compare:
- raw Top 20;
- diversified Top 20.

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

Rules:
- no heavy calculations live in dashboard;
- dashboard reads precomputed DuckDB tables/views.

## Module 22 — Debug Mode

Fast testing with sampled tickers and partial pipeline runs.

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
| 11 | Feature Engine | READY | Use final formulas from merged source |
| 12 | Market Regime Engine | READY | Can be called from Feature Engine |
| 13 | Step 3 Screening | READY | Use Step 3 scoring formulas |
| 14 | Step 4 Setup Analysis | READY | Use entry_proxy_raw |
| 15 | Step 5 Proposal Engine | READY | Raw + diversified ranking |
| 16 | Outcome Queue | READY | Track raw OR diversified Top 20 |
| 17 | Simulation Engine | READY | list_type/list_membership |
| 18 | Export Package Engine | READY | Include both ranks |
| 19 | AI Review Engine | READY | Manual send only |
| 20 | Pipeline Orchestrator | READY | Lock/heartbeat/resume |
| 21 | Streamlit Dashboard | READY | Checkbox for diversified list |
| 22 | Debug Mode | READY | Separate debug.duckdb |

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

Never edit a strategy config in place.

Clone and create a new config version.

Trading thresholds live in JSON config or immutable preset settings.

Tunable thresholds are only those that vary between presets, for example:
- `min_price`;
- `min_rvol`;
- `min_screening_score`;
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
- 22.16 Define trend_resume setup detection rule
- 22.17 Module 03 must use merged final schema
- 22.18 Structural domain constants live in constants.py

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
- 01_MASTER_SOURCE_OF_TRUTH_MERGED.md

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
If there is a conflict, ask only if it blocks implementation. Otherwise follow the merged source of truth.
```

For Module 03, add:

```text
Critical Module 03 rule:
Create the final merged schema directly.
Do not create old base tables first and then apply ALTER patches on a fresh DB.
```

For Module 04, add:

```text
Critical Module 04 rule:
Define provider interface only.
Do not implement YahooProvider yet.
Do not call Yahoo directly.
```
