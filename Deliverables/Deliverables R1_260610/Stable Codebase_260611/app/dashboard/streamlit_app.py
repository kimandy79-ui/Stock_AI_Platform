"""Module 21 -- Streamlit Dashboard entry point.

Launch::

    streamlit run app/dashboard/streamlit_app.py

This module is the thin rendering layer.  All DB access and display logic is
delegated to :mod:`app.dashboard.data_access`, which reads precomputed DuckDB
tables / views through the approved DB manager.  Nothing here recomputes
upstream module logic, writes to the database, or calls a provider.

M20 boundary
------------
The pipeline orchestrator's ``dashboard_materialization`` step (step 12) is a
logged no-op (G-DASHBOARD-MAT).  This dashboard is a *standalone viewer* -- it
is NOT invoked from the pipeline.  It reads from already-populated tables.

Tabs (V1 read-only subset of ``UI/95_Dashboard_Tab_Specs.md``)
--------------------------------------------------------------
- **Daily Proposals** -- final shortlist with the ``Show diversified shortlist``
  checkbox (default ``True``, persisted in
  ``st.session_state["show_diversified"]``).  Rows where
  ``in_raw_top_n != in_diversified_top_n`` are highlighted amber.
- **Outcome Tracking** -- resolved/unresolved counts and average 5/10/20/40bd
  returns from ``signal_outcomes``.
- **Pipeline Health** -- latest run status, recent ``pipeline_runs`` and
  ``data_repair_queue`` rows (includes ``steps_completed`` from M20).
- **AI Review** -- recent ``ai_reviews`` metadata rows.

Signal Explorer, Strategy Performance, Config Manager, Simulation Lab, and
Debug Mode tabs are deferred to future modules.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.dashboard import data_access
from app.dashboard.data_access import DashboardDataLoader

SESSION_SHOW_DIVERSIFIED: str = "show_diversified"
SESSION_DB_ROLE: str = "db_role"


# --------------------------------------------------------------------------- #
# Sidebar.
# --------------------------------------------------------------------------- #
def _render_sidebar() -> None:
    st.sidebar.title("Swing Trading Analyzer")
    st.sidebar.caption("Local single-user dashboard -- read-only.")
    st.session_state.setdefault(SESSION_DB_ROLE, data_access.DB_ROLE_PROD)
    st.sidebar.selectbox(
        "Database",
        options=list(data_access.ALLOWED_DASHBOARD_ROLES),
        key=SESSION_DB_ROLE,
    )


def _loader() -> DashboardDataLoader:
    role = st.session_state.get(SESSION_DB_ROLE, data_access.DB_ROLE_PROD)
    return DashboardDataLoader(db_role=role)


# --------------------------------------------------------------------------- #
# Daily Proposals tab.
# --------------------------------------------------------------------------- #
def _render_daily_proposals(loader: DashboardDataLoader) -> None:
    st.header("Daily Proposals")

    # Persisted diversified checkbox (01e spec; default True).
    st.session_state.setdefault(SESSION_SHOW_DIVERSIFIED, True)
    st.checkbox("Show diversified shortlist", key=SESSION_SHOW_DIVERSIFIED)

    dates = loader.list_signal_dates()
    if not dates:
        st.info("No proposals available yet.")
        return

    signal_date = st.selectbox("Signal date", options=dates, index=0)

    configs = loader.list_strategy_configs()
    config_choice = st.selectbox(
        "Strategy config", options=["(all)"] + configs, index=0
    )
    strategy_config_id = None if config_choice == "(all)" else config_choice

    view = loader.load_daily_proposals(
        signal_date=signal_date,
        strategy_config_id=strategy_config_id,
        show_diversified=st.session_state[SESSION_SHOW_DIVERSIFIED],
    )

    list_label = "diversified" if view.show_diversified else "raw"
    st.caption(
        f"Showing {list_label} shortlist -- run_id={view.run_id} -- "
        f"{len(view.rows)} rows"
    )

    if not view.rows:
        st.info("No proposals match the current selection.")
        return

    # build_proposals_display is the single entry point: it returns display
    # rows (list_disagreement excluded) AND the flags from the SAME source,
    # so the two cannot be accidentally decoupled.
    display_rows, flags = data_access.build_proposals_display(view)
    df = pd.DataFrame(display_rows)
    styled = df.style.apply(data_access.highlight_row, flags=flags, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# Outcome Tracking tab.
# --------------------------------------------------------------------------- #
def _render_outcome_tracking(loader: DashboardDataLoader) -> None:
    st.header("Outcome Tracking")
    summary = loader.load_outcome_summary()
    col1, col2, col3 = st.columns(3)
    col1.metric("Outcomes", summary.total)
    col2.metric("Resolved", summary.resolved)
    col3.metric("Unresolved", summary.unresolved)
    st.subheader("Average returns (all resolved)")
    st.write(
        {
            "5bd %": summary.avg_return_5bd_pct,
            "10bd %": summary.avg_return_10bd_pct,
            "20bd %": summary.avg_return_20bd_pct,
            "40bd %": summary.avg_return_40bd_pct,
        }
    )


# --------------------------------------------------------------------------- #
# Pipeline Health tab.
# --------------------------------------------------------------------------- #
def _render_pipeline_health(loader: DashboardDataLoader) -> None:
    st.header("Pipeline Health")
    latest = loader.latest_pipeline_status()
    if latest is not None:
        col1, col2 = st.columns(2)
        col1.metric("Latest run status", str(latest.get("status", "?")))
        col2.metric(
            "Latest run date", str(latest.get("run_date", "?"))
        )
        steps = latest.get("steps_completed")
        if steps:
            st.caption(f"Steps completed: {steps}")
        err = latest.get("error_message")
        if err:
            st.error(f"Error: {err}")
    else:
        st.info("No pipeline runs recorded yet.")

    st.subheader("Recent runs")
    runs = loader.load_pipeline_runs()
    st.dataframe(runs or [], use_container_width=True, hide_index=True)

    st.subheader("Repair queue")
    repairs = loader.load_repair_queue()
    st.dataframe(repairs or [], use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# AI Review tab.
# --------------------------------------------------------------------------- #
def _render_ai_review(loader: DashboardDataLoader) -> None:
    st.header("AI Review")
    reviews = loader.load_ai_reviews()
    if reviews:
        st.dataframe(reviews, use_container_width=True, hide_index=True)
    else:
        st.info("No AI reviews recorded yet.")


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #
def _selected_db_missing() -> tuple[bool, str]:
    """Return (missing, path_str) for the currently selected DB role.

    A pure filesystem existence check via the approved DB manager's path
    resolver -- it opens no connection and writes nothing, preserving the
    dashboard's read-only contract.
    """
    role = st.session_state.get(SESSION_DB_ROLE, data_access.DB_ROLE_PROD)
    try:
        from app.database import duckdb_manager

        path = duckdb_manager.get_database_path(role)
        return (not path.exists(), str(path))
    except Exception:  # noqa: BLE001 - never block rendering on a path probe
        return (False, "")


def main() -> None:
    st.set_page_config(
        page_title="Swing Trading Analyzer", layout="wide", page_icon="📈"
    )
    _render_sidebar()

    missing, db_path = _selected_db_missing()
    if missing:
        role = st.session_state.get(SESSION_DB_ROLE, data_access.DB_ROLE_PROD)
        st.warning(
            f"The selected **{role}** database does not exist yet at `{db_path}`.\n\n"
            "Run the pipeline first to create and populate it:\n\n"
            "- prod: `python tools/init_prod_db.py` then `python tools/run_prod_pipeline.py`\n"
            "- debug: `python tools/run_debug_pipeline.py --preset fast_smoke_test`"
        )
        return

    loader = _loader()

    proposals_tab, outcomes_tab, health_tab, review_tab = st.tabs(
        ["Daily Proposals", "Outcome Tracking", "Pipeline Health", "AI Review"]
    )
    with proposals_tab:
        _render_daily_proposals(loader)
    with outcomes_tab:
        _render_outcome_tracking(loader)
    with health_tab:
        _render_pipeline_health(loader)
    with review_tab:
        _render_ai_review(loader)


if __name__ == "__main__":
    main()
