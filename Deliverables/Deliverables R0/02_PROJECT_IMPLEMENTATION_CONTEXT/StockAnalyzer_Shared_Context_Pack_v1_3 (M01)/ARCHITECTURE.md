# ARCHITECTURE.md — Swing Trading Stock Analyzer

Status: shared architecture source for ChatGPT + Claude.

## 1. System Type

Local research-grade swing trading platform.

Main objective:
daily EOD stock screening, proposal generation, outcome tracking, simulation, and AI-assisted review.

## 2. Folder Structure

Recommended:

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

## 3. Module Responsibilities

### Module 01 — Project Skeleton
Creates folder structure, config loading, constants, logging, ServiceResult, tests.

### Module 02 — DuckDB Manager
Manages DuckDB connections for prod/debug/simulation DBs.

### Module 03 — Schema Manager
Creates final merged schema. Must include Master TZ + PATCH 1 + MINI-PATCH 2.

### Module 04 — Provider Interface
Defines abstract data provider contract.

### Module 05 — YahooProvider
Implements market data download behind provider interface.

### Module 06 — Universe Snapshot Engine
Maintains ticker universe and monthly snapshots.

### Module 07 — Benchmark / Sector ETF Loader
Loads SPY, QQQ, ^VIX, sector ETFs. Must run before feature engine.

### Module 08 — Daily Price Ingestion
Downloads / updates daily prices.

### Module 09 — Data Validator
Validates OHLCV, missing rows, suspicious values.

### Module 10 — Mutation Detector
Detects splits, historical mutations, and rebuild needs.

### Module 11 — Feature Engine
Calculates daily_features using Polars.

### Module 12 — Market Regime Engine
Computes market_regime from SPY/QQQ/VIX. Can be called by feature engine.

### Module 13 — Step 3 Screening
Applies hard filters and screening score.

### Module 14 — Step 4 Setup Analysis
Classifies setups, estimates stop/target/RR.

### Module 15 — Step 5 Proposal Engine
Calculates raw and diversified rankings.

### Module 16 — Outcome Queue
Creates and processes 5/10/20/40bd outcome tasks.

### Module 17 — Simulation Engine
Runs research, walk-forward, config comparisons.

### Module 18 — Export Package Engine
Creates ticker/simulation review ZIPs.

### Module 19 — AI Review Engine
Sends review packages to AI provider when user manually clicks.

### Module 20 — Pipeline Orchestrator
Coordinates daily pipeline and failure recovery.

### Module 21 — Streamlit Dashboard
Local UI.

### Module 22 — Debug Mode
Fast testing with sampled tickers and partial pipeline runs.

## 4. Data Flow

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

## 5. Database Structure

Three DB files:

```text
prod.duckdb
debug.duckdb
simulation.duckdb
```

Simulation attaches prod read-only:

```sql
ATTACH 'data/duckdb/prod.duckdb' AS prod (READ_ONLY);
```

## 6. Core Tables

Production:
- schema_versions
- pipeline_runs
- pipeline_locks
- ticker_master
- ticker_universe_snapshot
- sector_etf_map
- daily_prices
- daily_features
- strategy_configs
- step3_candidates
- step4_analysis
- step5_proposals
- outcome_tracking_queue
- signal_outcomes
- earnings_calendar
- macro_events_calendar
- data_repair_queue
- feature_rebuild_log
- ai_reviews
- execution_decisions

Simulation:
- sim_runs
- sim_folds
- sim_step3_candidates
- sim_step4_analysis
- sim_step5_proposals
- sim_signal_outcomes
- sim_config_comparisons
- sim_ai_reviews

## 7. Interfaces

All services return:

```python
@dataclass
class ServiceResult:
    status: str
    run_id: str
    rows_processed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

All modules accept:
- db_manager
- run_id
- config
- date range / signal date where applicable

## 8. Caching Strategy

V1:
- no Redis
- no external cache
- use materialized / precomputed tables
- dashboard reads from DuckDB
- Streamlit session state allowed for UI controls
- heavy calculations run in pipeline, not live in dashboard

## 9. Threading / Async Decisions

V1 default:
- simple synchronous pipeline
- no async framework
- no background workers
- no multi-user concurrency target

DuckDB:
- single-writer discipline
- dashboard read-only connection
- pipeline write connection
- avoid concurrent writes

## 10. Failure Recovery

Pipeline runs stored in `pipeline_runs`.

Locks stored in `pipeline_locks`.

Heartbeat:
- interval: 60 seconds
- stale lock threshold: 5 minutes

Failed run:
- status = failed
- steps_completed JSON stores progress
- resume from failed step allowed

## 11. Debug Architecture

Debug mode writes to:

`debug.duckdb`

Presets:
- 20 tickers, 5 days
- 10 tickers, 90 days indicators
- 100 tickers, 30 days
- 500 tickers, 6 months

Debug never writes to prod.
