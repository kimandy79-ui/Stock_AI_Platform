"""Module 21 V2 -- Streamlit Dashboard entry point.

Launch::

    streamlit run app/dashboard/streamlit_app.py

Layout
------
- **Daily Proposals** -- diversified/raw shortlist with row checkboxes.
  *Run Pipeline* triggers ``PipelineOrchestrator.run()``.
  *Export CSV* calls ``DashboardActionService.export_proposals_csv()`` and
  offers a ``st.download_button`` for the result.
  *Export ZIP (AI Review)* calls
  ``DashboardActionService.export_ticker_review()`` via M18.

- **Outcome Tracking** -- resolved/unresolved counts, average returns.

- **Pipeline Health** -- latest run status, recent runs, repair queue.

- **AI Review** -- recent ``ai_reviews`` rows; *Send to AI* and
  *Record Action* buttons delegate to ``DashboardActionService``.

- **Settings / Config** -- list strategy config versions, *Make Active*
  (activate), *Clone & Save* (create new version), *Export CSV* for a
  selected config.

Architecture
------------
All mutations flow through :class:`~app.dashboard.action_service.DashboardActionService`.
``data_access.DashboardDataLoader`` is **read-only**.  This file never
instantiates ``ExportPackageEngine``, ``AiReviewEngine``,
``ConfigService``, or ``PipelineOrchestrator`` directly.

M20 boundary
------------
``pipeline_orchestrator._step_dashboard`` (step 12) remains a no-op.
This dashboard is a standalone viewer.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.dashboard import data_access
from app.dashboard.action_service import DashboardActionService
from app.dashboard.data_access import DashboardDataLoader

# ------------------------------------------------------------------ #
# Session-state keys.
# ------------------------------------------------------------------ #
_KEY_DB_ROLE = "db_role"
_KEY_DIVERSIFIED = "show_diversified"
_KEY_PIPELINE_MSG = "pipeline_msg"


# ------------------------------------------------------------------ #
# Shared helpers.
# ------------------------------------------------------------------ #

def _loader() -> DashboardDataLoader:
    role = st.session_state.get(_KEY_DB_ROLE, data_access.DB_ROLE_PROD)
    return DashboardDataLoader(db_role=role)


def _action_svc() -> DashboardActionService:
    return DashboardActionService()


def _db_role() -> str:
    return st.session_state.get(_KEY_DB_ROLE, data_access.DB_ROLE_PROD)


def _show_service_result(result: Any, success_msg: str = "Done.") -> None:
    if result.status == "success":
        st.success(success_msg)
    elif result.status == "success_with_warnings":
        st.warning(success_msg + " (warnings: " + "; ".join(result.warnings) + ")")
    else:
        st.error("Action failed: " + "; ".join(result.errors or ["unknown error"]))


# ------------------------------------------------------------------ #
# Sidebar.
# ------------------------------------------------------------------ #

def _render_sidebar() -> None:
    st.sidebar.title("Swing Trading Analyzer")
    st.sidebar.caption("Local single-user dashboard.")
    st.session_state.setdefault(_KEY_DB_ROLE, data_access.DB_ROLE_PROD)
    st.sidebar.selectbox(
        "Database",
        options=list(data_access.ALLOWED_DASHBOARD_ROLES),
        key=_KEY_DB_ROLE,
    )


# ------------------------------------------------------------------ #
# Daily Proposals tab.
# ------------------------------------------------------------------ #

def _render_daily_proposals(loader: DashboardDataLoader) -> None:
    st.header("Daily Proposals")
    role = _db_role()

    # ---- Top action bar ----
    _, col_run, col_csv, col_zip = st.columns([3, 1, 1, 1])
    run_clicked = col_run.button("▶ Run Pipeline", key="btn_run_pipeline", type="primary")
    export_csv_clicked = col_csv.button("⬇ Export CSV", key="btn_export_proposals_csv")
    export_zip_clicked = col_zip.button("📦 Export ZIP", key="btn_export_proposals_zip")

    if run_clicked:
        _do_run_pipeline(role)

    # Pipeline message feedback (persisted across rerun).
    if st.session_state.get(_KEY_PIPELINE_MSG):
        msg_type, msg_text = st.session_state[_KEY_PIPELINE_MSG]
        if msg_type == "success":
            st.success(msg_text)
        elif msg_type == "warning":
            st.warning(msg_text)
        else:
            st.error(msg_text)

    # ---- Filters row ----
    st.session_state.setdefault(_KEY_DIVERSIFIED, True)
    col_date, col_cfg, col_div = st.columns([2, 2, 1])
    col_div.checkbox("Diversified", key=_KEY_DIVERSIFIED)

    dates = loader.list_signal_dates()
    if not dates:
        st.info("No proposals available yet.")
        return

    signal_date: date = col_date.selectbox("Signal date", options=dates, index=0)  # type: ignore[assignment]
    configs = loader.list_strategy_configs()
    config_choice: str = col_cfg.selectbox("Strategy config", options=["(all)"] + configs, index=0)  # type: ignore[assignment]
    strategy_config_id: str | None = None if config_choice == "(all)" else config_choice

    view = loader.load_daily_proposals(
        signal_date=signal_date,
        strategy_config_id=strategy_config_id,
        show_diversified=st.session_state[_KEY_DIVERSIFIED],
    )

    list_label = "diversified" if view.show_diversified else "raw"
    st.caption(
        f"Showing {list_label} shortlist — run_id={view.run_id} — {len(view.rows)} rows"
    )

    if not view.rows:
        st.info("No proposals match the current selection.")
        return

    display_rows, flags = data_access.build_proposals_display(view)
    df = pd.DataFrame(display_rows)
    styled = df.style.apply(data_access.highlight_row, flags=flags, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)

    all_tickers = [r.get("ticker", "") for r in display_rows if r.get("ticker")]
    selected_tickers: list[str] = st.multiselect(
        "Select tickers for export",
        options=all_tickers,
        default=all_tickers[:5] if len(all_tickers) >= 5 else all_tickers,
        key="proposal_ticker_select",
    )

    if export_csv_clicked:
        _do_export_proposals_csv(role=role, loader=loader, view=view, selected_tickers=selected_tickers)

    if export_zip_clicked:
        _do_export_proposals_zip(role=role, loader=loader, view=view, selected_tickers=selected_tickers, strategy_config_id=strategy_config_id)


def _do_run_pipeline(role: str) -> None:
    with st.spinner("Running pipeline…"):
        result = _action_svc().run_pipeline(
            db_role=role,
            run_date=date.today(),
            run_type="manual",
        )
    if result.status == "success":
        st.session_state[_KEY_PIPELINE_MSG] = ("success", f"Pipeline completed. run_id={result.run_id}")
    elif result.status == "success_with_warnings":
        st.session_state[_KEY_PIPELINE_MSG] = (
            "warning",
            "Pipeline completed with warnings: " + "; ".join(result.warnings or []),
        )
    else:
        st.session_state[_KEY_PIPELINE_MSG] = (
            "error",
            "Pipeline failed: " + "; ".join(result.errors or ["unknown error"]),
        )
    st.rerun()


def _do_export_proposals_csv(
    role: str,
    loader: DashboardDataLoader,
    view: data_access.ProposalsView,
    selected_tickers: list[str],
) -> None:
    if not selected_tickers:
        st.warning("Select at least one ticker before exporting.")
        return
    if view.run_id is None or view.signal_date is None:
        st.warning("No valid run to export.")
        return
    proposal_ids = loader.load_proposal_ids(
        run_id=view.run_id,
        signal_date=view.signal_date,
        tickers=selected_tickers,
        strategy_config_id=view.strategy_config_id,
    )
    result = _action_svc().export_proposals_csv(
        signal_date=view.signal_date,
        proposal_ids=proposal_ids,
        db_role=role,
        strategy_config_id=view.strategy_config_id,
    )
    if result.status != "success":
        st.error("Export failed: " + "; ".join(result.errors or []))
        return
    st.download_button(
        label=f"⬇ Download {result.metadata['filename']}",
        data=result.metadata["csv_bytes"],
        file_name=result.metadata["filename"],
        mime="text/csv",
        key="dl_proposals_csv",
    )


def _do_export_proposals_zip(
    role: str,
    loader: DashboardDataLoader,
    view: data_access.ProposalsView,
    selected_tickers: list[str],
    strategy_config_id: str | None,
) -> None:
    if not selected_tickers:
        st.warning("Select at least one ticker before exporting.")
        return
    if strategy_config_id is None:
        st.warning("Select a specific strategy config (not '(all)') before creating a ZIP export.")
        return
    if view.run_id is None or view.signal_date is None:
        st.warning("No valid run to export.")
        return
    proposal_ids = loader.load_proposal_ids(
        run_id=view.run_id,
        signal_date=view.signal_date,
        tickers=selected_tickers,
        strategy_config_id=strategy_config_id,
    )
    if not proposal_ids:
        st.warning("No proposal ids found for the selected tickers.")
        return
    with st.spinner("Building ZIP package…"):
        result = _action_svc().export_ticker_review(
            signal_date=view.signal_date,
            strategy_config_id=strategy_config_id,
            proposal_ids=proposal_ids,
            db_role=role,
        )
    if result.status == "success":
        zip_path = result.metadata.get("zip_path", "")
        st.success(f"ZIP created: `{zip_path}`")
        st.caption(f"ai_review_id={result.metadata.get('run_id', '')} — use AI Review tab to send.")
    else:
        st.error("ZIP export failed: " + "; ".join(result.errors or []))


# ------------------------------------------------------------------ #
# Outcome Tracking tab.
# ------------------------------------------------------------------ #

def _render_outcome_tracking(loader: DashboardDataLoader) -> None:
    st.header("Outcome Tracking")
    summary = loader.load_outcome_summary()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total", summary.total)
    c2.metric("Resolved", summary.resolved)
    c3.metric("Unresolved", summary.unresolved)
    st.subheader("Average returns (resolved outcomes)")
    st.write({
        "5bd %": summary.avg_return_5bd_pct,
        "10bd %": summary.avg_return_10bd_pct,
        "20bd %": summary.avg_return_20bd_pct,
        "40bd %": summary.avg_return_40bd_pct,
    })


# ------------------------------------------------------------------ #
# Pipeline Health tab.
# ------------------------------------------------------------------ #

def _render_pipeline_health(loader: DashboardDataLoader) -> None:
    st.header("Pipeline Health")
    latest = loader.latest_pipeline_status()
    if latest is not None:
        c1, c2 = st.columns(2)
        c1.metric("Status", str(latest.get("status", "?")))
        c2.metric("Run date", str(latest.get("run_date", "?")))
        steps = latest.get("steps_completed")
        if steps:
            st.caption(f"Steps completed: {steps}")
        err = latest.get("error_message")
        if err:
            st.error(f"Error: {err}")
    else:
        st.info("No pipeline runs recorded yet.")
    st.subheader("Recent runs")
    st.dataframe(loader.load_pipeline_runs() or [], use_container_width=True, hide_index=True)
    st.subheader("Repair queue")
    st.dataframe(loader.load_repair_queue() or [], use_container_width=True, hide_index=True)


# ------------------------------------------------------------------ #
# AI Review tab.
# ------------------------------------------------------------------ #

def _render_ai_review(loader: DashboardDataLoader) -> None:
    st.header("AI Review")
    role = _db_role()
    reviews: list[dict] = loader.load_ai_reviews()

    if not reviews:
        st.info("No AI reviews recorded yet. Use Daily Proposals → Export ZIP to create one.")
        return

    st.dataframe(pd.DataFrame(reviews), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Actions")
    ai_review_ids = [r.get("ai_review_id", "") for r in reviews if r.get("ai_review_id")]
    if not ai_review_ids:
        return

    selected_id: str = st.selectbox("Select review", options=ai_review_ids, key="ai_review_id_select")  # type: ignore[assignment]

    col_send, col_action = st.columns(2)
    with col_send:
        if st.button("🤖 Send to AI", key="btn_send_ai"):
            with st.spinner("Sending to AI provider…"):
                result = _action_svc().send_ticker_review(ai_review_id=selected_id, db_role=role)
            _show_service_result(result, f"AI response recorded for review {selected_id}.")

    with col_action:
        human_action: str = st.selectbox(
            "Human action",
            options=["ignored", "accepted", "overrode", "deferred"],
            key="human_action_select",
        )  # type: ignore[assignment]
        if st.button("✔ Record Action", key="btn_record_action"):
            result = _action_svc().record_human_action(
                ai_review_id=selected_id, human_action=human_action, db_role=role
            )
            _show_service_result(result, f"Action '{human_action}' recorded.")


# ------------------------------------------------------------------ #
# Settings / Config tab.
# ------------------------------------------------------------------ #

def _render_settings(loader: DashboardDataLoader) -> None:  # noqa: ARG001 C901
    st.header("Settings / Config")
    role = _db_role()
    svc = _action_svc()

    try:
        from app.services.config.config_service import ConfigService
        sr = ConfigService().list_strategy_configs(db_role=role)
        versions: list[dict] = sr.metadata.get("versions", []) if sr.status == "success" else []
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load strategy configs: {exc}")
        return

    if not versions:
        st.info("No strategy configs found. Run the pipeline (or init DB) to seed defaults.")
        return

    config_options = {
        f"{v['strategy_name']} / {v['version']}{' (Active)' if v['active_flag'] else ''}": v["config_id"]
        for v in versions
    }
    selected_label: str = st.selectbox("Strategy config version", options=list(config_options.keys()), key="cfg_select")  # type: ignore[assignment]
    selected_config_id = config_options[selected_label]
    selected_meta = next((v for v in versions if v["config_id"] == selected_config_id), {})

    st.caption(
        f"config_id={selected_config_id} | "
        f"hash={str(selected_meta.get('config_hash', ''))[:12]}… | "
        f"created={selected_meta.get('created_at', '?')}"
    )

    col_activate, col_export = st.columns(2)
    with col_activate:
        if st.button("⚡ Make Active", key="btn_make_active", type="primary"):
            result = svc.activate_strategy_config(
                config_id=selected_config_id,
                db_role=role,
                activated_by="dashboard",
                reason="activated via dashboard",
            )
            _show_service_result(result, f"Config '{selected_label}' is now active.")
            if result.status == "success":
                st.rerun()

    with col_export:
        if st.button("⬇ Export Config CSV", key="btn_export_cfg_csv"):
            result = svc.export_strategy_config_csv(config_id=selected_config_id, db_role=role)
            if result.status == "success":
                st.download_button(
                    label=f"⬇ Download {result.metadata['filename']}",
                    data=result.metadata["csv_bytes"],
                    file_name=result.metadata["filename"],
                    mime="text/csv",
                    key="dl_cfg_csv",
                )
            else:
                st.error("Export failed: " + "; ".join(result.errors or []))

    st.subheader("All versions")
    df_v = pd.DataFrame(versions)
    if not df_v.empty:
        display_cols = [c for c in ["strategy_name", "version", "active_flag", "created_at", "created_by", "notes", "config_id"] if c in df_v.columns]
        st.dataframe(df_v[display_cols], use_container_width=True, hide_index=True)

    st.divider()
    with st.expander("⧉ Clone / create new version", expanded=False):
        _render_clone_form(svc=svc, role=role, source_meta=selected_meta)


def _render_clone_form(svc: DashboardActionService, role: str, source_meta: dict) -> None:
    st.caption(
        "Clone creates a new version (inactive by default). "
        "Use 'Make Active' on the new version to activate it."
    )
    source_strategy = source_meta.get("strategy_name", "")
    source_config_id = source_meta.get("config_id", "")

    new_strategy_name: str = st.text_input("Strategy name", value=source_strategy, key="clone_strategy_name")  # type: ignore[assignment]
    new_version: str = st.text_input("Version label (optional, auto-generated if blank)", key="clone_version")  # type: ignore[assignment]
    new_notes: str = st.text_input("Notes (optional)", key="clone_notes")  # type: ignore[assignment]

    try:
        from app.services.config.config_service import ConfigService
        import json as _json
        sr = ConfigService().get_strategy_config(config_id=source_config_id, db_role=role)
        source_cfg_json = sr.metadata.get("config", {}).get("config_json", {}) if sr.status == "success" else {}
    except Exception:  # noqa: BLE001
        source_cfg_json = {}
        _json = __import__("json")

    import json as _json2
    cfg_text: str = st.text_area(
        "Config JSON (edit tunable values)",
        value=_json2.dumps(source_cfg_json, indent=2),
        height=250,
        key="clone_config_json",
    )  # type: ignore[assignment]

    col_save, col_save_act = st.columns(2)
    with col_save:
        if st.button("💾 Save Clone", key="btn_save_clone"):
            _do_save_clone(svc, role, new_strategy_name or source_strategy, cfg_text, new_version or None, source_config_id or None, new_notes or None, False)
    with col_save_act:
        if st.button("💾⚡ Save + Activate", key="btn_save_activate_clone"):
            _do_save_clone(svc, role, new_strategy_name or source_strategy, cfg_text, new_version or None, source_config_id or None, new_notes or None, True)


def _do_save_clone(
    svc: DashboardActionService,
    role: str,
    strategy_name: str,
    config_json_text: str,
    version: str | None,
    parent_config_id: str | None,
    notes: str | None,
    activate: bool,
) -> None:
    import json as _json
    try:
        config_json = _json.loads(config_json_text)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Invalid JSON: {exc}")
        return
    result = svc.clone_strategy_config(
        db_role=role,
        strategy_name=strategy_name,
        config_json=config_json,
        version=version,
        parent_config_id=parent_config_id,
        created_by="dashboard",
        notes=notes,
        activate=activate,
    )
    _show_service_result(
        result,
        f"Config '{strategy_name}' saved" + (" and activated." if activate else " (inactive)."),
    )
    if result.status == "success":
        st.rerun()


# ------------------------------------------------------------------ #
# DB presence guard.
# ------------------------------------------------------------------ #

def _selected_db_missing() -> tuple[bool, str]:
    role = st.session_state.get(_KEY_DB_ROLE, data_access.DB_ROLE_PROD)
    try:
        from app.database import duckdb_manager
        path = duckdb_manager.get_database_path(role)
        return (not path.exists(), str(path))
    except Exception:  # noqa: BLE001
        return (False, "")


# ------------------------------------------------------------------ #
# Entry point.
# ------------------------------------------------------------------ #

def main() -> None:
    st.set_page_config(page_title="Swing Trading Analyzer", layout="wide", page_icon="📈")
    _render_sidebar()

    missing, db_path = _selected_db_missing()
    if missing:
        role = st.session_state.get(_KEY_DB_ROLE, data_access.DB_ROLE_PROD)
        st.warning(
            f"The selected **{role}** database does not exist yet at `{db_path}`.\n\n"
            "Run the pipeline first:\n\n"
            "- prod: `python tools/init_prod_db.py` then `python tools/run_prod_pipeline.py`\n"
            "- debug: `python tools/run_debug_pipeline.py --preset fast_smoke_test`"
        )
        return

    loader = _loader()
    tab_proposals, tab_outcomes, tab_health, tab_review, tab_settings = st.tabs(
        ["Daily Proposals", "Outcome Tracking", "Pipeline Health", "AI Review", "Settings / Config"]
    )
    with tab_proposals:
        _render_daily_proposals(loader)
    with tab_outcomes:
        _render_outcome_tracking(loader)
    with tab_health:
        _render_pipeline_health(loader)
    with tab_review:
        _render_ai_review(loader)
    with tab_settings:
        _render_settings(loader)


if __name__ == "__main__":
    main()
