# Architecture — Current

## Accepted architecture

The system is a local Windows-based research-grade swing trading stock analyzer.

The backend pipeline is implemented through Modules 01–22.

## Control/data flow

1. Provider interface and provider implementation supply market data.
2. Ingestion and validation modules populate local DuckDB tables.
3. Feature and regime modules compute stored features.
4. Step 3/4/5 modules create candidates, setup analysis, and proposals.
5. Outcome and simulation modules evaluate historical results.
6. Export and AI review modules package and review selected data.
7. Pipeline Orchestrator coordinates production/debug runs.
8. Dashboard displays stored outputs and may trigger approved services through explicit user actions.

## Dashboard architecture

M21 Dashboard V1 is accepted as a read-only viewer.

M21 Dashboard V2 Update extends the dashboard workflow while keeping dashboard code as UI/control layer only.

Dashboard code must not duplicate domain logic.

## Database architecture

Three separate local DuckDB databases are used:

- `prod.duckdb`: production/research pipeline outputs
- `debug.duckdb`: debug-only fast runs
- `simulation.duckdb`: simulation-only outputs

Simulation may read production data only through approved read-only access. Debug Mode targets debug only.

## Source-of-truth architecture

Project Files are authoritative.

The cleaned stable codebase intentionally has no separate `docs/` folder. This avoids a second, stale source-of-truth hierarchy.
