# TODO Roadmap — Current

## Accepted stable baseline

Modules 01–22 are accepted.

## Active next work

Module 21 Dashboard V2 Update.

## Dashboard V2 planned sequence

1. Preserve M21 V1 behavior and tests.
2. Add Home / Overview.
3. Add Step 4 ticker drill-down.
4. Add Export & AI action UI through M18/M19.
5. Add Debug Mode UI through M22.
6. Add Signal Explorer.
7. Add Strategy Performance.
8. Add Simulation Lab through M17.
9. Add optional read-only Config Manager.

## Hard constraints

- No direct DB writes from Streamlit UI or dashboard data-access.
- No provider calls from dashboard.
- No heavy domain logic inside dashboard.
- Actions must delegate to approved service APIs.
- Preserve prod/debug/simulation DB isolation.
