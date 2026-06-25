# Stock AI Platform — Swing Trading Stock Analyzer

Local Windows-based US daily swing trading **research-grade V1** platform.

This repository contains the accepted implementation baseline for Modules 01–22.
The next planned work is **Module 21 Dashboard V2 Update**.

> **Safety note.** This system is research support only. It does not provide guaranteed trading predictions. There is no auto-trading and no broker connection in V1.

## Current state

**Accepted stable baseline:** Modules 01–22.

| Area | Status |
|---|---|
| M01–M03 | Project skeleton, DuckDB manager, schema manager |
| M04–M05 | Provider interface and Yahoo provider |
| M06–M10 | Universe snapshot, benchmark loader, price ingestion, validation, mutation detection |
| M11–M12 | Feature engine and market regime engine |
| M13–M15 | Step 3 screening, Step 4 analysis, Step 5 proposal engine |
| M16 | Outcome queue |
| M17 | Simulation engine |
| M18 | Export package engine |
| M19 | AI review engine |
| M20 | Pipeline orchestrator |
| M21 | Streamlit Dashboard V1 — accepted read-only viewer; next update is Dashboard V2 |
| M22 | Debug Mode — accepted backend/control-plane module |

## Source-of-truth priority

When implementing or reviewing code, use this priority:

1. Current task prompt.
2. `SOURCE_OF_TRUTH_INDEX.md` and `PROJECT_STATUS_CURRENT.md` from the aligned Project Files package.
3. The module-specific source-of-truth spec.
4. Dependent module specs.
5. Split Project Files.
6. Current stable codebase.
7. Tests as behavior evidence.

The codebase is the implementation baseline. Project Files are the active source of truth.

## Database architecture

The platform uses **three separate local DuckDB files**. They must remain isolated.

| DB role | File | Purpose | Write rules |
|---|---|---|---|
| `prod` | `data/duckdb/prod.duckdb` | Production/research baseline data: universe, prices, features, screening, proposals, outcomes, pipeline health, ticker AI reviews | Written only by approved production services/pipeline steps |
| `debug` | `data/duckdb/debug.duckdb` | Fast local debug/testing runs with sampled tickers or watchlists | Written only by Debug Mode / debug pipeline flows |
| `simulation` | `data/duckdb/simulation.duckdb` | Historical simulations, config comparisons, simulation outcomes, simulation AI reviews | Written only by Simulation Engine and simulation-specific export/AI review flows |

### `prod.duckdb`

`prod.duckdb` is the main local research database. It stores the canonical daily pipeline outputs: universe, prices, validation/mutation metadata, features, market regime, Step 3 candidates, Step 4 analysis, Step 5 proposals, outcomes, pipeline runs, exports, and ticker-level AI review metadata.

### `debug.duckdb`

`debug.duckdb` has the production schema but is used only for fast debug/testing workflows. Debug Mode must force `db_role="debug"` and `run_type="debug"`. It must never write to production or simulation databases.

### `simulation.duckdb`

`simulation.duckdb` is separate from prod/debug and is owned by Module 17 Simulation Engine. It stores simulation-only tables such as `sim_runs`, `sim_step3_candidates`, `sim_step4_analysis`, `sim_step5_proposals`, `sim_signal_outcomes`, `sim_config_comparisons`, `sim_folds`, and `sim_ai_reviews`.

The Simulation Engine may attach production data read-only for historical inputs, but it must write only to simulation-owned tables.

## M21 Dashboard V1 vs V2

M21 Dashboard V1 is accepted stable and is **not incomplete**.

M21 V1 scope:

- Daily Proposals
- Outcome Tracking
- Pipeline Health
- AI Review metadata
- Read-only Streamlit dashboard behavior

M21 Dashboard V2 Update extends the dashboard workflow while preserving accepted V1 behavior and tests.

M21 V2 scope:

- Home / overview
- Step 4 ticker drill-down
- Export & AI action UI
- Debug Mode UI
- Signal Explorer
- Strategy Performance
- Simulation Lab
- Optional read-only Config Manager

## Dashboard boundaries

The dashboard must not duplicate domain logic.

Rules:

- No provider calls from Streamlit UI or dashboard data-access layer.
- No heavy market-data, screening, scoring, outcome, simulation, or AI logic inside the dashboard.
- No direct database writes from Streamlit UI or dashboard `data_access`.
- Write-like user actions must call approved service APIs only.
- Existing prod/debug/simulation DB isolation rules must remain intact.

Approved service APIs for M21 Dashboard V2 action UI:

- M17 Simulation Engine
- M18 Export Package Engine
- M19 AI Review Engine
- M20 Pipeline Orchestrator, only if explicitly required
- M22 Debug Mode Controller

## Project structure

```text
stock_ai_platform/
  app/
    config/
    dashboard/
    database/
    providers/
    services/
    utils/
  data/
    duckdb/
      prod.duckdb
      debug.duckdb
      simulation.duckdb
    logs/
    exports/
    backups/
  tests/
  tools/
  pyproject.toml
  requirements.txt
  README.md
```

The old codebase `docs/` folder is intentionally removed in the cleaned baseline to avoid source-of-truth drift. Use Project Files for all implementation requirements.

## Setup

Create and activate a virtual environment:

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Optional editable install:

```bash
pip install -e ".[dev]"
```

Create a local environment file if needed:

```bat
copy .env.example .env
```

## Initialize databases

Production DB:

```bash
python tools/init_prod_db.py
```

Debug DB:

```bash
python tools/init_debug_db.py
```

Simulation DB is initialized through the accepted schema manager / simulation schema path used by the codebase. Any future dedicated tool must call approved schema manager APIs and must not create an ad-hoc schema.

## Run pipeline

Production pipeline:

```bash
python tools/run_prod_pipeline.py
```

Debug pipeline:

```bash
python tools/run_debug_pipeline.py --preset pipeline_sanity --sample-count 50
```

Debug runs must target `debug.duckdb` only.

## Run dashboard

```bash
streamlit run app/dashboard/streamlit_app.py
```

Current Dashboard V1 is read-only and displays already-computed outputs.

## Run simulation

Simulation is executed through Module 17 service APIs and writes to `simulation.duckdb` / `sim_*` tables only. It may read production historical data through approved read-only access.

M21 Dashboard V2 may add a Simulation Lab UI, but that UI must call the accepted M17 service instead of implementing simulation logic inside Streamlit.

## Run tests

Run all tests:

```bash
pytest -q
```

Run dashboard tests:

```bash
pytest -q tests/test_dashboard.py
```

Run debug mode tests:

```bash
pytest -q tests/test_debug_mode.py
```

Run simulation tests:

```bash
pytest -q tests/test_simulation_engine.py
```

## Important accepted contracts

- DuckDB roles are limited to `prod`, `debug`, and `simulation`.
- Provider access is confined to provider modules.
- Pipeline orchestration belongs to M20.
- Debug Mode belongs to M22 and must use `debug.duckdb`.
- Simulation belongs to M17 and writes only to `simulation.duckdb` / `sim_*` tables.
- Export and AI review actions must go through M18/M19.
- Dashboard must remain a UI/control layer, not a domain engine.

## Next planned work

**Module 21 Dashboard V2 Update**

Before starting this update, use the aligned source-of-truth package:

```text
StockAnalyzer_Source_of_Truth_M21_Dashboard_V2_Rebuild_v1_0.zip
```

The first files to read are:

```text
Project_Files/SOURCE_OF_TRUTH_INDEX.md
Project_Files/PROJECT_STATUS_CURRENT.md
Project_Files/M21_STREAMLIT_DASHBOARD_V2_WORKFLOW_SPEC.md
Project_Files/CLAUDE_PROJECT_INSTRUCTIONS_UPDATED.md
```
