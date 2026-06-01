# CODING_STANDARDS.md

Coding standards for ChatGPT + Claude implementation.

## 1. General Rules

- Implement one module at a time.
- Do not mix modules.
- Do not add unrequested features.
- Do not hardcode trading thresholds outside config.
- Do not call provider APIs outside provider layer.
- Do not bypass DuckDB manager.
- Do not use pandas unless unavoidable.
- Prefer Polars for data transformations.
- All functions must have type hints.
- All modules must have module-level docstrings.

## 2. Python Version

Python 3.11+

## 3. Required Libraries

Minimum:
- duckdb
- polars
- yfinance
- pandas-market-calendars
- streamlit
- pydantic
- keyring
- pytest
- python-dotenv
- numpy
- pandas

## 4. Naming Conventions

Files:
- snake_case.py

Classes:
- PascalCase

Functions:
- snake_case

Constants:
- UPPER_SNAKE_CASE

IDs:
- UUID4 strings

Dates:
- ISO date format
- use `datetime.date` for dates
- use timezone-aware timestamps where possible

## 5. Logging

Use Python logging.

Format:

```text
timestamp | level | module | run_id | message
```

Every service logs:
- start
- end
- rows processed
- warnings
- errors

Do not print from library/service modules.
Dashboard may display messages via Streamlit.

## 6. Error Handling

Use three categories:

### Warning
Continue:
- low-confidence earnings
- missing sector
- partial provider failures

### Recoverable failure
Continue degraded:
- some tickers failed download
- repair queue populated

### Critical failure
Stop:
- DB unavailable
- schema mismatch
- invalid config
- look-ahead validation failure
- feature calculation crash

## 7. ServiceResult Contract

All service modules should return:

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

Allowed status:
- success
- success_with_warnings
- failed

## 8. Database Rules

Use DuckDB manager for all DB access.

No module opens arbitrary DB paths directly.

Use separate DBs:
- prod.duckdb
- debug.duckdb
- simulation.duckdb

Simulation attaches prod read-only only.

## 9. Schema Rules

For fresh DB:
- create final merged schema directly
- do not create old tables and then immediately patch with ALTER TABLE

For Module 03 Schema Manager:
- use the merged final schema from Master ТЗ v1 FULL + PATCH 1 + MINI-PATCH 2
- create the final merged schema directly
- do not create the old base schema first and then apply ALTER patches on a fresh DB

Feature schema version:
- use zero-padded names: features_v01

## 10. Config Rules

Config is immutable.

Never edit a strategy config in place.

Clone and create new config version.

Trading thresholds live in JSON config.

## 11. Performance Rules

Avoid ticker-by-ticker Python loops when batch/vectorized processing is possible.

Use Polars for:
- groupby
- rolling/window calculations
- joins
- feature calculations

Query only needed columns.

Use date filters.

Dashboard should read precomputed tables/views.

## 12. Testing Requirements

Every module must include pytest tests.

Minimum test types:
- unit tests
- integration hooks
- invalid input test
- empty data test
- expected output test where feasible

Critical tests:
- no look-ahead
- schema creation idempotency
- feature formulas
- ranking reproducibility
- raw vs diversified ranking
- outcome queue membership
- outcome returns use entry_price_sim, not entry_price_raw
- simulation read-only attach

## 13. Documentation Style

Every file:
- module docstring
- clear class/function docstrings
- comments only where logic is non-obvious

No giant unclear comments.

## 14. AI Coding Workflow

For each coding task:
1. Provide relevant docs only.
2. Specify exact module.
3. Tell AI not to implement unrelated modules.
4. Ask for tests.
5. Review with second AI.
6. Run tests.
7. Commit after working module.

## 15. Git Rules

Commit after each stable module.

Commit message style:

```text
module01_project_skeleton_stable
module02_duckdb_manager_stable
```

Use rollback if AI breaks working code.

## 16. Dashboard Rules

Streamlit is local single-user.

Do not do heavy calculations live in dashboard.

Dashboard reads from DuckDB and precomputed outputs.

Checkboxes and filters may use `st.session_state`.

## 17. Safety Rules

This system is research support only.

It does not provide guaranteed trading predictions.

No auto-trading in V1.

No broker connection in V1.
