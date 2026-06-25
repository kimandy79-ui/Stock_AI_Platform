# Decisions Log — Current

## Decision: M21 Dashboard V1 remains accepted stable

M21 Dashboard V1 is not incomplete. It was accepted as a local read-only Streamlit viewer.

## Decision: Dashboard V2 is an update to M21

The next dashboard work is Module 21 Dashboard V2 Update.

The V2 update extends the dashboard workflow but must preserve V1 behavior and tests unless explicitly changed by the V2 spec.

## Decision: Project Files are active source of truth

The stable codebase is implementation baseline only. The old codebase docs folder is removed from the cleaned baseline to avoid source-of-truth drift.

## Decision: Dashboard write boundary

Dashboard UI and dashboard data-access must not directly write to any DB.

Write-like user actions must call approved service APIs only.

## Decision: Database isolation

The system maintains three DB roles/files:

- `prod` / `prod.duckdb`
- `debug` / `debug.duckdb`
- `simulation` / `simulation.duckdb`

Debug Mode must not write prod. Simulation must not mutate prod.
