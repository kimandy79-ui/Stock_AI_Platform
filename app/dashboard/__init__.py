"""Module 21 -- Streamlit Dashboard package.

Split into two layers so the data layer stays testable without a running
Streamlit server or a live DuckDB file:

- :mod:`app.dashboard.data_access`
    Read-only loaders, pure formatting helpers, and the pandas Styler
    applicator.  No Streamlit import; no DDL; no writes; no provider calls.
    Every DB access goes through the approved
    :mod:`app.database.duckdb_manager` (read-only) or an injected
    ``db_manager`` (for tests).  The module imports ``duckdb_manager`` only
    lazily -- inside :class:`DashboardDataLoader.__init__` -- so the whole
    module is importable and unit-testable without duckdb installed when a
    fake manager is injected.

- :mod:`app.dashboard.app`
    Streamlit entry point (``streamlit run app/dashboard/app.py``).
    Renders what :mod:`data_access` returns; owns
    ``st.session_state["show_diversified"]``.  Imports Streamlit; the data
    layer does not.

M20 boundary
------------
The pipeline orchestrator (Module 20) logs a no-op for
``dashboard_materialization`` (G-DASHBOARD-MAT).  Module 21 is a standalone
read-only viewer and is NOT invoked from the pipeline; its data comes from
already-computed DuckDB tables and views.
"""

from __future__ import annotations
