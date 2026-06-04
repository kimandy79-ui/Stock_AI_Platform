# M21 -- Streamlit Dashboard Spec

Module 21 is the local, single-user **read-only dashboard**.  It surfaces
already-computed pipeline outputs through a Streamlit UI.  It performs **no**
market-data, screening, scoring, outcome, simulation, AI, or provider logic and
**never writes** to the database.

Source of truth for the contracts below: ``01e_UI_AND_TESTING.md``
(``UI/95_Dashboard_Tab_Specs.md`` -- Daily Proposals columns + the diversified
checkbox), ``01b_SCHEMA_AND_DATA.md`` (``step5_proposals``,
``selected_proposals_current``, ``step4_analysis``, ``ticker_master``,
``pipeline_runs``, ``data_repair_queue``, ``signal_outcomes``, ``ai_reviews``),
``02_PROJECT_IMPLEMENTATION_CONTEXT.md`` section 19 (dashboard rules: local,
single-user, read-only, no heavy calc), M20 gap note G-DASHBOARD-MAT
(``dashboard_materialization`` step 12 remains a logged no-op in the pipeline
orchestrator; M21 is a **standalone viewer**, not invoked from the pipeline),
and the current stable ``app/database/schema_manager.py`` for the accepted
``step5_proposals`` column set.

---

## 1. Layout

```
app/dashboard/
  __init__.py       # package; M20 boundary note
  data_access.py    # testable read layer (no Streamlit import)
  app.py            # Streamlit entry: streamlit run app/dashboard/app.py
```

``data_access.py`` does not import Streamlit and imports ``duckdb_manager``
only lazily inside ``DashboardDataLoader.__init__`` (when no fake is injected).
This keeps the entire module importable and unit-testable without duckdb or
Streamlit installed when a ``FakeDbManager`` is provided.  ``app.py`` imports
both pandas and Streamlit; it is not imported by the test suite.

---

## 2. M20 boundary (G-DASHBOARD-MAT)

The pipeline orchestrator's ``_step_dashboard`` (step 12) remains unchanged: it
logs ``"dashboard materialization skipped (G-DASHBOARD-MAT: Module 21 not yet
implemented)"`` and returns a success no-op.  M21 is a standalone viewer; the
pipeline does not invoke it.  No changes are made to Module 20.

---

## 3. Public API (``app.dashboard.data_access``)

```python
DashboardDataLoader(db_manager=None, db_role="prod")
```

``db_manager`` is injected only for tests.  ``db_role`` is validated against
``ALLOWED_DASHBOARD_ROLES = ("prod", "debug")``.  Every connection is opened
with ``read_only=True`` and closed in ``finally``.

### Selector methods

```python
.list_signal_dates(limit=60)         -> list[date]
.list_strategy_configs()             -> list[str]
.latest_signal_date()                -> date | None
.latest_run_id_for_date(signal_date, strategy_config_id=None) -> str | None
```

### Panel methods

```python
.load_daily_proposals(signal_date=None, strategy_config_id=None,
                      show_diversified=True)     -> ProposalsView
.load_pipeline_runs(limit=25)                   -> list[dict]
.load_repair_queue(limit=50)                    -> list[dict]
.latest_pipeline_status()                       -> dict | None
.load_outcome_summary(strategy_config_id=None)  -> OutcomeSummary
.load_ai_reviews(limit=25)                      -> list[dict]
```

### Pure helpers (no I/O)

```python
validate_role(db_role)               -> str            # raises UnknownDashboardRoleError
rank_column_for(show_diversified)    -> str            # "diversified_rank" | "raw_rank"
membership_column_for(show_div)      -> str            # "in_diversified_top_n" | "in_raw_top_n"
derive_div_reason(row)               -> str | None     # rejection_reason -> penalty -> None
annotate_rows(rows)                  -> list[dict]     # adds list_disagreement + div_reason
extract_disagreement_flags(rows)     -> list[bool]
highlight_css_for_row(is_disagree)   -> str            # CSS or ""
build_proposals_display(view)        -> (list[dict], list[bool])  # single source of truth
highlight_row(row: pd.Series, flags) -> list[str]      # pandas Styler applicator
```

### Module-level constants

```python
DISPLAY_COLUMNS: tuple[str, ...]         # 12 visible columns (ordered)
DISAGREEMENT_HIGHLIGHT_CSS: str          # "background-color: #fff3cd"
ALLOWED_DASHBOARD_ROLES: tuple[str, ...] # ("prod", "debug")
```

### Result containers

```python
@dataclass
class ProposalsView:
    rows: list[dict]            # annotated; ordered by active rank
    show_diversified: bool
    rank_column: str            # "diversified_rank" | "raw_rank"
    signal_date: date | None
    run_id: str | None
    strategy_config_id: str | None

@dataclass
class OutcomeSummary:
    total: int;  resolved: int;  unresolved: int
    avg_return_5bd_pct / 10bd / 20bd / 40bd: float | None
    strategy_config_id: str | None
```

---

## 4. Daily Proposals -- raw vs. diversified

Per ``01e_UI_AND_TESTING.md``:

- Checkbox ``"Show diversified shortlist"``, default **True**, persisted in
  ``st.session_state["show_diversified"]``.
- **Checked** -> ``in_diversified_top_n = TRUE``, ordered by
  ``diversified_rank ASC``.
- **Unchecked** -> ``in_raw_top_n = TRUE``, ordered by ``raw_rank ASC``.
- Rows where ``in_raw_top_n != in_diversified_top_n`` are flagged
  (``list_disagreement = True``) and highlighted amber
  (``background-color: #fff3cd``).

Proposals are scoped to the single most recent ``run_id`` for the chosen
``signal_date`` so the table is deterministic when multiple runs share a date.
``step4_analysis`` (setup_type, estimated_rr) and ``ticker_master``
(sector, industry) are joined with ``LEFT JOIN`` -- missing join rows never
drop a proposal.  ``div_reason`` is derived from the stored
``rejection_reason`` (falling back to ``diversity_penalty``); no recomputation.

Visible columns: raw_rank, diversified_rank, ticker, strategy_config_id,
setup_type, proposal_score_raw, proposal_score_final, estimated_rr, sector,
industry, div_reason, mechanical_explanation.

### Highlighting contract

``build_proposals_display(view) -> (display_rows, flags)`` is the single
mandatory entry point in app.py.  It returns the display slice **and** the
disagreement flags from the **same** ``view.rows`` source so they can never
be accidentally decoupled.  ``highlight_row(row: pd.Series, flags)`` lives in
``data_access.py`` (no Streamlit) and is directly unit-testable.  app.py does:

```python
display_rows, flags = data_access.build_proposals_display(view)
df = pd.DataFrame(display_rows)
styled = df.style.apply(data_access.highlight_row, flags=flags, axis=1)
st.dataframe(styled, ...)
```

---

## 5. Other panels

- **Outcome Tracking** -- ``COUNT`` / ``AVG`` read over ``signal_outcomes``
  (resolved = ``outcome_status = 'complete'``).  DB-side aggregation of
  precomputed rows; not a recomputation of Module 16/17 logic.
- **Pipeline Health** -- latest run status (status, run_date,
  steps_completed from M20, error_message) plus recent ``pipeline_runs`` and
  ``data_repair_queue`` rows.
- **AI Review** -- recent ``ai_reviews`` metadata rows.

---

## 6. Boundaries

- **Read-only**: no ``INSERT`` / ``UPDATE`` / ``DELETE`` / DDL / ``ATTACH``.
  DB roles restricted to ``prod`` / ``debug``; ``simulation`` raises
  ``UnknownDashboardRoleError``.
- **No duckdb / streamlit at import time** (data layer): ``duckdb_manager``
  is imported lazily; Streamlit is in ``app.py`` only.
- **No heavy calc**: upstream module logic never re-runs in the dashboard.
- **No provider calls, no** ``print()``: library logging only in data layer;
  ``app.py`` renders via Streamlit.

---

## 7. Tests (``tests/test_dashboard.py``)

| # | Test | Deps |
|---|------|------|
| Pure helpers | ``validate_role``, toggle columns, ``derive_div_reason``, ``annotate_rows`` mutation safety | none |
| Highlighting | ``extract_disagreement_flags``, ``highlight_css_for_row``, ``build_proposals_display`` coupling | none |
| Loader: read-only | Every loader uses ``read_only=True``; connection closed | none |
| Loader: toggle wiring | Diversified -> correct SQL ``IN`` filter + ORDER BY; raw -> same | none |
| Loader: empty paths | No run_id, no dates -> empty ``ProposalsView`` | none |
| Outcome summary | Counts, avg returns, unresolved arithmetic | none |
| Pipeline status | Latest row returned; None when no rows | none |
| Pandas Styler | Real Styler highlights disagreeing rows only | ``importorskip("pandas")`` |
| DuckDB integration | Real schema + synthetic rows; div vs. raw shortlist | ``importorskip("duckdb")`` |

All tests use ``FakeDbManager`` (no real DB file) except the integration test
which monkeypatches ``settings.PROD_DB_PATH`` to ``tmp_path``.

---

## 8. Gaps / assumptions

- V1 renders Daily Proposals, Outcome Tracking, Pipeline Health, AI Review.
  Signal Explorer, Strategy Performance, Config Manager, Simulation Lab, and
  Debug Mode tabs are deferred (require Module 22 or richer read models).
- ``div_reason`` reuses stored proposal fields; if a dedicated diversification
  reason column is added to ``step5_proposals`` later, ``derive_div_reason``
  should prefer it.
- The pipeline orchestrator's ``_step_dashboard`` (M20 step 12) continues to
  log a no-op.  If a future version requires the pipeline to trigger dashboard
  refresh (e.g. cache invalidation), that change belongs in M20 and M21 should
  expose a lightweight hook -- not inline heavy logic.
