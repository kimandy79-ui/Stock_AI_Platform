# Project Status Current — Stock AI Platform

## Current status

Modules 01–22 are accepted as the current stable baseline.

## Next planned work

**Module 21 Dashboard V2 Update**

This is an update to the existing Dashboard module, not a separate numbered module.

## Dashboard status

### M21 Dashboard V1

Accepted stable. Not incomplete.

Current V1 scope:

- Daily Proposals
- Outcome Tracking
- Pipeline Health
- AI Review metadata
- Read-only dashboard behavior

### M21 Dashboard V2 Update

Active next work.

Planned V2 scope:

- Home / overview
- Step 4 ticker drill-down
- Export & AI action UI
- Debug Mode UI
- Signal Explorer
- Strategy Performance
- Simulation Lab
- Optional read-only Config Manager

## Codebase documentation status

The stale `docs/` folder in the stable codebase is intentionally removed in the cleaned baseline package.

Project Files are the active source of truth. The codebase root README is an orientation file only and does not override Project Files.

## Database isolation

- `prod.duckdb`: production/research pipeline data.
- `debug.duckdb`: debug/testing data only.
- `simulation.duckdb`: simulation-only data and `sim_*` tables.

Dashboard V2 must not directly write to any DB. User-triggered actions must call approved service APIs that own their write contracts.
