# TODO_ROADMAP.md

Status file for implementation.

## Current Status

Preparatory work:
- Architecture review: DONE
- Master TZ v1 FULL: DONE
- PATCH 1: ACCEPTED
- MINI-PATCH 2: ACCEPTED
- Ready for coding: YES

## Rule

Do not reopen architecture unless implementation reveals a true blocker.

## Module Roadmap

| # | Module | Status | Blockers / Notes |
|---|---|---|---|
| 01 | Project Skeleton | READY | Start here |
| 02 | DuckDB Manager | READY | After Module 01 |
| 03 | Schema Manager | READY WITH NOTE | Use merged final schema from Master ТЗ v1 FULL + PATCH 1 + MINI-PATCH 2. Do not create base schema first and then apply ALTER patches on a fresh DB. |
| 04 | Provider Interface | READY | Abstract provider contract |
| 05 | YahooProvider | READY | After provider interface |
| 06 | Universe Snapshot Engine | READY | Monthly snapshots |
| 07 | Benchmark / Sector ETF Loader | READY | Must load SPY, QQQ, ^VIX, sector ETFs before features |
| 08 | Daily Price Ingestion | READY | Raw + adjusted data |
| 09 | Data Validator | READY | OHLCV checks |
| 10 | Mutation Detector | READY | Split/mutation handling |
| 11 | Feature Engine | READY | Use MINI-PATCH 2 formulas |
| 12 | Market Regime Engine | READY | Can be called from Feature Engine |
| 13 | Step 3 Screening | READY | Use Step3 scoring formulas |
| 14 | Step 4 Setup Analysis | READY | Use entry_proxy_raw |
| 15 | Step 5 Proposal Engine | READY | Raw + diversified ranking |
| 16 | Outcome Queue | READY | Track raw OR diversified Top 20 |
| 17 | Simulation Engine | READY | list_type/list_membership |
| 18 | Export Package Engine | READY | Include both ranks |
| 19 | AI Review Engine | READY | Manual send only |
| 20 | Pipeline Orchestrator | READY | Lock/heartbeat/resume |
| 21 | Streamlit Dashboard | READY | Checkbox for diversified list |
| 22 | Debug Mode | READY | Separate debug.duckdb |

## Implementation Sequence

Phase 1 — Foundation:
- Module 01
- Module 02
- Module 03
- Module 04

Phase 2 — Data Layer:
- Module 05
- Module 06
- Module 07
- Module 08
- Module 09
- Module 10

Phase 3 — Features and Screening:
- Module 11
- Module 12
- Module 13
- Module 14
- Module 15

Phase 4 — Outcomes and Simulation:
- Module 16
- Module 17

Phase 5 — User Tools:
- Module 18
- Module 19
- Module 20
- Module 21
- Module 22

## Current Immediate Task

Start Module 01: Project Skeleton.


## Module 03 Critical Schema Rule

For Module 03 Schema Manager, use the merged final schema from:

```text
Master ТЗ v1 FULL + PATCH 1 + MINI-PATCH 2
```

Do not implement the base schema first and then apply ALTER patches on a fresh database.

The Schema Manager must create the final merged schema directly.


## Pending Decisions

None blocking.

## Accepted V1 Limitations

- YahooProvider is research-grade.
- Residual survivorship bias remains.
- Macro calendar may be manual.
- Earnings source may be LOW confidence.
- No broker integration.
- No intraday execution.
- No cloud deployment.

## Definition of Done for Each Module

Each module must include:
- implementation
- type hints
- logging
- tests
- no unrelated functionality
- no hardcoded thresholds outside config
- ServiceResult where applicable
