"""Module 21 V2 -- Streamlit Dashboard (HTML-matched layout).

Visually matches module21v2_updated_interactive_v7.html:

Daily Proposals
    - Header row: title left, "Pipeline run: …" chip + Run Pipeline + Export right
    - Filter cards: run date | strategy | list view
    - Proposal table: rank, ticker, company, sector, score pill, RR, signal pill,
      price, EMA spread, RSI14, MACD hist, volume ratio, rel strength, ATR%
    - Per-ticker indicator cards (mechanical_explanation breakdown)

Settings / Config
    - Header: title left, Clone Settings + Make Active + Export right
    - Summary card: strategy selector, version/status/created meta boxes
    - Clone bar (appears on Clone click): new name input, Save Clone, Cancel
    - Filter row: Show settings type (Both/Tunable/Hardcoded) + Expand/Collapse
    - Accordion: one expander per config module group

All mutations go through DashboardActionService. data_access is read-only.
"""

from __future__ import annotations

import json
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
# Design tokens (mirrored from HTML :root + CSS).
# ------------------------------------------------------------------ #
_CSS = """
<style>
/* ---- tokens ---- */
:root {
  --navy:#041c44; --blue:#2563eb; --blue-soft:#eff6ff;
  --green:#16a34a; --green-soft:#dcfce7;
  --text:#0f172a; --muted:#64748b;
  --bg:#f6f7fb; --card:#ffffff; --border:#e5e7eb;
}

/* ---- global resets ---- */
body, [data-testid="stAppViewContainer"] { background: var(--bg) !important; }
[data-testid="stSidebar"] {
  background: linear-gradient(180deg,#041c44,#08285d) !important;
}
[data-testid="stSidebar"] * { color: #dbeafe !important; }
[data-testid="stSidebar"] .stSelectbox label { color:#94a3b8 !important; font-size:12px !important; }

/* ---- cards ---- */
.da-card {
  background:#fff; border:1px solid var(--border);
  border-radius:14px; box-shadow:0 3px 12px rgba(15,23,42,.04);
  padding:14px; margin-bottom:14px;
}

/* ---- header row ---- */
.da-headerrow {
  display:flex; justify-content:space-between; align-items:flex-start;
  gap:16px; margin-bottom:14px;
}
.da-title { font-size:28px; font-weight:800; margin:0 0 2px; color:#0f172a; }
.da-subtitle { font-size:14px; color:#64748b; }

/* ---- top-right chip + buttons ---- */
.da-chip {
  display:inline-block; background:#fff; border:1px solid var(--border);
  border-radius:10px; padding:9px 12px; font-size:13px; font-weight:700;
  color:#475569;
}
.da-btn {
  display:inline-block; background:#fff; border:1px solid var(--border);
  border-radius:10px; padding:9px 14px; font-weight:800; cursor:pointer;
  font-size:13px; color:#0f172a;
}
.da-btn-primary { background:#2563eb !important; border-color:#2563eb !important; color:#fff !important; }
.da-btn-blue   { color:#2563eb !important; border-color:#93c5fd !important; }
.da-btn-green  { color:#15803d !important; border-color:#86efac !important; }

/* ---- filter labels ---- */
.da-label {
  display:block; font-size:12px; color:#334155;
  font-weight:800; margin-bottom:4px;
}

/* ---- proposal table pills ---- */
.da-score {
  display:inline-block; padding:3px 8px; border-radius:999px;
  font-size:12px; font-weight:800;
  background:var(--green-soft); color:#166534;
}
.da-signal {
  display:inline-block; padding:3px 8px; border-radius:999px;
  font-size:12px; font-weight:800;
  background:var(--green-soft); color:#166534;
}
.da-ticker-cell { font-weight:800; color:#1d4ed8; }

/* ---- indicator cards ---- */
.da-indcard {
  border:1px solid #dbeafe; background:#f8fbff;
  border-radius:10px; padding:10px; margin-bottom:8px;
}
.da-indheader { display:flex; justify-content:space-between; gap:10px; margin-bottom:5px; }
.da-indtitle { font-weight:800; font-size:13px; color:#0f172a; }
.da-indvalue { font-weight:800; font-size:13px; color:#1d4ed8; white-space:nowrap; }
.da-indnote  { font-size:12px; color:#334155; line-height:1.45; }

/* ---- settings meta boxes ---- */
.da-metabox { font-size:13px; }
.da-metalabel { font-size:12px; color:#64748b; font-weight:700; margin-bottom:4px; }
.da-metavalue { font-weight:700; color:#0f172a; }
.da-activegreen { color:#16a34a; font-weight:800; }

/* ---- clone bar ---- */
.da-clonebar {
  border-top:1px dashed #cbd5e1; padding-top:14px; margin-top:14px;
}

/* ---- settings table ---- */
.da-settings-table { width:100%; border-collapse:collapse; font-size:13px; }
.da-settings-table th {
  background:#f8fafc; color:#475569; font-size:12px;
  padding:10px; border-bottom:1px solid #eef2f7; text-align:left;
}
.da-settings-table td { padding:10px; border-bottom:1px solid #eef2f7; vertical-align:middle; }
.da-settings-table tr:hover td { background:#fafcff; }
.da-lock { display:inline-block; width:22px; height:22px; border-radius:6px;
  background:#f1f5f9; text-align:center; line-height:22px; font-size:12px; }
.da-edit { display:inline-block; width:22px; height:22px; border-radius:6px;
  background:#eff6ff; color:#2563eb; text-align:center; line-height:22px; font-size:12px; }

/* ---- amber disagreement row ---- */
tr.da-amber td { background:#fff3cd !important; }

/* ---- streamlit widget overrides ---- */
div[data-testid="stDataFrame"] { border-radius:10px; overflow:hidden; }
.stButton > button {
  border-radius:10px !important; font-weight:800 !important;
}
</style>
"""

# ------------------------------------------------------------------ #
# Config module groups → known config_json keys.
# Each entry: (section_title, icon, emoji_bg, [(label, key_path, tunable)])
# key_path uses dot notation for nested keys.
# ------------------------------------------------------------------ #
_CONFIG_SECTIONS = [
    (
        "Universe & Market Data", "📈", "#eef4ff",
        [
            ("Min Price (USD)",            "universe.min_price",                True),
            ("Min Avg Dollar Vol 20d",      "universe.min_avg_dollar_volume_20d", True),
            ("Allowed Symbol Types",        "universe.allowed_symbol_types",     False),
            ("Exclude Benchmarks",          "universe.exclude_benchmarks",       False),
        ],
    ),
    (
        "Technical Indicators", "🛠", "#f3e8ff",
        [
            ("EMA Periods",                 "features.ema_periods",              False),
            ("RSI Lookback",                "features.rsi_period",               True),
            ("MACD Fast/Slow/Signal",       "features.macd_params",              False),
            ("ATR Period",                  "features.atr_period",               False),
            ("Volume Spike Threshold",      "features.volume_spike_threshold",   True),
        ],
    ),
    (
        "Screening & Scoring", "⭐", "#fff7ed",
        [
            ("Min RVOL",                    "screening.min_rvol",                True),
            ("Min Screening Score",         "screening.min_screening_score",     True),
            ("Require Feature Ready",       "screening.require_feature_ready",   False),
            ("Target R (Step 4)",           "step4.target_R",                    True),
        ],
    ),
    (
        "Risk Management", "🛡", "#ecfdf5",
        [
            ("Hard Cap Enabled",            "diversification.hard_cap_enabled",  False),
            ("Top N",                       "diversification.top_n",             True),
            ("Sector Max Positions",        "diversification.sector_max_positions", True),
            ("Industry Max Positions",      "diversification.industry_max_positions", True),
            ("Sector Penalty Factor",       "diversification.sector_penalty_factor", True),
            ("Industry Penalty Factor",     "diversification.industry_penalty_factor", True),
        ],
    ),
    (
        "Proposals & Filters", "⏃", "#fff1f2",
        [
            ("Earnings Avoid Within BD",    "earnings.avoid_within_bd",          True),
            ("Earnings Penalty Max",        "earnings.penalty_points_max",       True),
            ("Scoring Weights",             "scoring_weights",                   False),
        ],
    ),
    (
        "Diversification", "◔", "#ecfeff",
        [
            ("Penalty Before Cap Only",     "diversification.penalty_applies_before_cap_only", False),
            ("Sector ETF Mapping",          "sector_etf_mapping",                False),
        ],
    ),
    (
        "Backtest / Simulation", "⚗", "#eff6ff",
        [
            ("Simulation Mode",             "simulation.mode",                   False),
            ("Min Outcome Pct",             "simulation.min_resolved_pct",       True),
        ],
    ),
    (
        "System / General", "⚙", "#f1f5f9",
        [
            ("Strategy Name",               "strategy_name",                     False),
            ("Version",                     "version",                           False),
            ("Market Regime SPY SMA",       "market_regime.spy_sma_period",      False),
            ("Macro Event Risk",            "macro_event_risk",                  False),
        ],
    ),
]


# ------------------------------------------------------------------ #
# State keys.
# ------------------------------------------------------------------ #
_K_ROLE        = "db_role"
_K_DIV         = "show_diversified"
_K_PIPE_MSG    = "pipeline_msg"
_K_CLONE_MODE  = "clone_mode"
_K_ACTIVE_TAB  = "active_tab"
_K_SEL_TICKER  = "selected_ticker"
_K_TYPE_FILTER = "type_filter"


def _db_role() -> str:
    return st.session_state.get(_K_ROLE, "prod")


def _loader() -> DashboardDataLoader:
    return DashboardDataLoader(db_role=_db_role())


def _svc() -> DashboardActionService:
    return DashboardActionService()


def _get_nested(d: dict, dotpath: str) -> Any:
    """Retrieve nested dict value using dot notation."""
    parts = dotpath.split(".")
    cur: Any = d
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _fmt(val: Any) -> str:
    if val is None:
        return "—"
    if isinstance(val, (list, dict)):
        return json.dumps(val, separators=(",", ":"))
    return str(val)


# ------------------------------------------------------------------ #
# Inject global CSS once.
# ------------------------------------------------------------------ #
def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


# ------------------------------------------------------------------ #
# Sidebar.
# ------------------------------------------------------------------ #
def _render_sidebar() -> None:
    st.sidebar.markdown("## ↗ Stock Analyzer\n*Module 21 V2*")
    st.session_state.setdefault(_K_ROLE, "prod")

    nav_items = [
        ("Daily Proposals",     "daily"),
        ("Settings / Config",   "settings"),
        ("Outcome Tracking",    "outcomes"),
        ("AI Review",           "ai_review"),
        ("Pipeline Health",     "health"),
    ]
    st.session_state.setdefault(_K_ACTIVE_TAB, "daily")
    for label, key in nav_items:
        active = st.session_state[_K_ACTIVE_TAB] == key
        prefix = "▶ " if active else "   "
        if st.sidebar.button(f"{prefix}{label}", key=f"nav_{key}", use_container_width=True):
            st.session_state[_K_ACTIVE_TAB] = key
            st.rerun()

    st.sidebar.divider()
    st.sidebar.markdown('<span style="font-size:14px;color:#94a3b8">Environment (DB Role)</span>', unsafe_allow_html=True)
    st.sidebar.selectbox("Database role", options=["prod", "debug"], key=_K_ROLE, label_visibility="collapsed")

    role = _db_role()
    try:
        from app.database import duckdb_manager
        path = duckdb_manager.get_database_path(role)
        connected = path.exists()
    except Exception:
        connected = False

    status_color = "#22c55e" if connected else "#ef4444"
    status_text  = "Connected" if connected else "Not found"
    st.sidebar.markdown(
        f'<div style="font-size:12px;color:#94a3b8;line-height:1.9">'
        f'DB: {role}.duckdb<br>'
        f'Status: <span style="color:{status_color};font-weight:700">{status_text}</span><br>'
        f'v2.1.0</div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------ #
# Helpers for feedback.
# ------------------------------------------------------------------ #
def _show_result(result: Any, ok_msg: str) -> None:
    if result.status == "success":
        st.success(ok_msg)
    elif result.status == "success_with_warnings":
        st.warning(ok_msg + "  Warnings: " + "; ".join(result.warnings or []))
    else:
        st.error("Failed: " + "; ".join(result.errors or ["unknown error"]))


# ------------------------------------------------------------------ #
# Daily Proposals.
# ------------------------------------------------------------------ #
def _render_daily_proposals() -> None:  # noqa: C901
    loader = _loader()
    role   = _db_role()

    # ---- Header row ----
    col_title, col_right = st.columns([3, 2])
    with col_title:
        st.markdown('<div class="da-title">Daily Proposals</div>', unsafe_allow_html=True)

    # Pipeline run chip + buttons
    with col_right:
        latest = loader.latest_pipeline_status()
        run_label = "No runs yet"
        if latest:
            run_label = f"Pipeline run: {latest.get('run_date', '?')} {latest.get('status', '')}"

        btn_cols = st.columns([3, 2, 2])
        btn_cols[0].markdown(f'<div class="da-chip">{run_label}</div>', unsafe_allow_html=True)
        run_clicked    = btn_cols[1].button("▶ Run Pipeline", key="dp_run", type="primary")
        export_clicked = btn_cols[2].button("⬇ Export",       key="dp_export")

    if run_clicked:
        _do_run_pipeline(role)

    # Pipeline message feedback
    if st.session_state.get(_K_PIPE_MSG):
        mtype, mtext = st.session_state[_K_PIPE_MSG]
        getattr(st, mtype)(mtext)

    # ---- Filter card 1: run date ----
    with st.container():
        st.markdown('<div class="da-card">', unsafe_allow_html=True)
        dates = loader.list_signal_dates()
        if not dates:
            st.info("No proposals yet. Run the pipeline to generate proposals.")
            st.markdown('</div>', unsafe_allow_html=True)
            return

        date_col, _, _, _, _ = st.columns([2, 1, 1, 1, 1])
        with date_col:
            st.markdown('<span class="da-label">Run date</span>', unsafe_allow_html=True)
            signal_date: date = st.selectbox("Run date", options=dates, index=0, label_visibility="collapsed")  # type: ignore[assignment]
        st.markdown('</div>', unsafe_allow_html=True)

    # ---- Filter card 2: strategy + list view ----
    with st.container():
        st.markdown('<div class="da-card">', unsafe_allow_html=True)
        f1, f2, f3, f4 = st.columns(4)
        configs = loader.list_strategy_configs()
        with f1:
            st.markdown('<span class="da-label">Strategy</span>', unsafe_allow_html=True)
            cfg_choice: str = st.selectbox("Strategy", options=["(all)"] + configs, index=0, label_visibility="collapsed")  # type: ignore[assignment]
        with f2:
            st.markdown('<span class="da-label">List View</span>', unsafe_allow_html=True)
            list_view: str = st.selectbox("List View", ["Raw + Diversified", "Diversified only", "Raw only"], index=0, label_visibility="collapsed")  # type: ignore[assignment]
        st.markdown('</div>', unsafe_allow_html=True)

    strategy_config_id = None if cfg_choice == "(all)" else cfg_choice
    show_div = list_view != "Raw only"
    st.session_state[_K_DIV] = show_div

    view = loader.load_daily_proposals(
        signal_date=signal_date,
        strategy_config_id=strategy_config_id,
        show_diversified=show_div,
    )

    if not view.rows:
        st.info("No proposals for the selected date and strategy.")
        return

    # ---- Proposal table ----
    st.markdown('<div class="da-card">', unsafe_allow_html=True)
    st.markdown('<h3 style="margin:0 0 12px;font-size:17px">Proposed Stocks</h3>', unsafe_allow_html=True)

    display_rows, flags = data_access.build_proposals_display(view)

    # Build a styled DataFrame matching the HTML columns.
    table_data = []
    for i, row in enumerate(display_rows):
        rank = row.get("raw_rank") if not show_div else row.get("diversified_rank")
        score = row.get("proposal_score_final") or row.get("proposal_score_raw") or ""
        table_data.append({
            "Rank":          rank,
            "Ticker":        row.get("ticker", ""),
            "Sector":        row.get("sector") or "",
            "Score":         f"{float(score):.1f}" if score else "",
            "RR":            f"{row.get('estimated_rr', ''):.2f}" if row.get("estimated_rr") is not None else "",
            "Signal":        row.get("setup_type") or "",
            "Industry":      row.get("industry") or "",
            "Div Reason":    row.get("div_reason") or "",
        })

    df = pd.DataFrame(table_data)

    def _highlight(row_series: pd.Series) -> list[str]:
        idx = row_series.name
        if idx < len(flags) and flags[idx]:
            return ["background-color:#fff3cd"] * len(row_series)
        return [""] * len(row_series)

    styled = df.style.apply(_highlight, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ---- Ticker selector for indicator cards ----
    all_tickers = [r.get("ticker", "") for r in display_rows if r.get("ticker")]
    st.session_state.setdefault(_K_SEL_TICKER, all_tickers[0] if all_tickers else "")

    sel_ticker: str = st.selectbox(
        "Select ticker to view indicators",
        options=all_tickers,
        index=0,
        key="dp_sel_ticker",
    )  # type: ignore[assignment]

    # Find the row for the selected ticker and show indicator cards
    sel_row = next((r for r in display_rows if r.get("ticker") == sel_ticker), None)
    if sel_row:
        _render_indicator_cards(sel_row)

    st.markdown('</div>', unsafe_allow_html=True)

    # ---- Export logic ----
    if export_clicked:
        _do_export_proposals(role, loader, view, all_tickers, strategy_config_id)


def _render_indicator_cards(row: dict) -> None:
    """Render per-ticker indicator cards matching the HTML indcard style."""
    st.markdown("---")
    st.markdown(f'<div style="font-size:12px;color:#64748b;margin-bottom:8px">Indicators for <strong style="color:#1d4ed8">{row.get("ticker","")}</strong></div>', unsafe_allow_html=True)

    indicators = [
        ("Trend Alignment",         f"EMA20-EMA50 spread",        row.get("mechanical_explanation") or "—"),
        ("Score",                   f"{row.get('proposal_score_final') or row.get('proposal_score_raw') or '—'}", "Final proposal score after diversification adjustments."),
        ("Risk / Reward",           f"{row.get('estimated_rr') or '—'}",   "Estimated RR from Step 4 setup analysis."),
        ("Setup Type",              row.get("setup_type") or "—",           "Classified by Step 4 analysis engine."),
        ("Sector",                  row.get("sector") or "—",               "Canonical sector for diversification bucketing."),
        ("List Membership",         _list_membership_label(row),            "Whether this ticker appears in raw, diversified, or both lists."),
    ]

    cols = st.columns(3)
    for i, (title, value, note) in enumerate(indicators):
        with cols[i % 3]:
            st.markdown(
                f'<div class="da-indcard">'
                f'<div class="da-indheader">'
                f'<div class="da-indtitle">{title}</div>'
                f'<div class="da-indvalue">{value}</div>'
                f'</div>'
                f'<div class="da-indnote">{note}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


def _list_membership_label(row: dict) -> str:
    raw = bool(row.get("in_raw_top_n"))
    div = bool(row.get("in_diversified_top_n"))
    if raw and div:
        return "Raw + Diversified"
    if div:
        return "Diversified only"
    if raw:
        return "Raw only"
    return "—"


def _do_run_pipeline(role: str) -> None:
    with st.spinner("Running pipeline…"):
        result = _svc().run_pipeline(db_role=role, run_date=date.today(), run_type="manual")
    if result.status == "success":
        st.session_state[_K_PIPE_MSG] = ("success", f"Pipeline completed. run_id={result.run_id}")
    elif result.status == "success_with_warnings":
        st.session_state[_K_PIPE_MSG] = ("warning", "Pipeline completed with warnings: " + "; ".join(result.warnings or []))
    else:
        st.session_state[_K_PIPE_MSG] = ("error", "Pipeline failed: " + "; ".join(result.errors or ["unknown error"]))
    st.rerun()


def _do_export_proposals(
    role: str,
    loader: DashboardDataLoader,
    view: data_access.ProposalsView,
    all_tickers: list[str],
    strategy_config_id: str | None,
) -> None:
    if view.run_id is None or view.signal_date is None:
        st.warning("No valid run to export.")
        return
    proposal_ids = loader.load_proposal_ids(
        run_id=view.run_id,
        signal_date=view.signal_date,
        tickers=all_tickers,
        strategy_config_id=strategy_config_id,
    )
    result = _svc().export_proposals_csv(
        signal_date=view.signal_date,
        proposal_ids=proposal_ids,
        db_role=role,
        strategy_config_id=strategy_config_id,
    )
    if result.status == "success":
        st.download_button(
            label=f"⬇ Download {result.metadata['filename']}",
            data=result.metadata["csv_bytes"],
            file_name=result.metadata["filename"],
            mime="text/csv",
            key="dl_proposals",
        )
    else:
        st.error("Export failed: " + "; ".join(result.errors or []))


# ------------------------------------------------------------------ #
# Settings / Config.
# ------------------------------------------------------------------ #
def _render_settings() -> None:  # noqa: C901
    role = _db_role()

    # Load config versions from ConfigService
    versions: list[dict] = []
    try:
        from app.services.config.config_service import ConfigService
        sr = ConfigService().list_strategy_configs(db_role=role)
        if sr.status == "success":
            versions = sr.metadata.get("versions", [])
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load strategy configs: {exc}")
        return

    if not versions:
        st.info("No strategy configs found. Run `python tools/init_prod_db.py` to seed defaults.")
        return

    # ---- Strategy selector (used by header meta boxes) ----
    st.session_state.setdefault("cfg_sel_idx", 0)
    strategy_names_seen: list[str] = []
    active_by_strategy: dict[str, dict] = {}
    for v in versions:
        sn = v["strategy_name"]
        if sn not in strategy_names_seen:
            strategy_names_seen.append(sn)
        if v["active_flag"]:
            active_by_strategy[sn] = v

    # Build display labels: "Normal (Active)" style
    def _cfg_label(v: dict) -> str:
        suffix = " (Active)" if v["active_flag"] else ""
        return f"{v['strategy_name']} / {v['version']}{suffix}"

    cfg_labels = [_cfg_label(v) for v in versions]
    cfg_id_by_label = {_cfg_label(v): v["config_id"] for v in versions}

    # ---- Clone mode state ----
    st.session_state.setdefault(_K_CLONE_MODE, False)

    # ---- Header row ----
    col_title, col_btns = st.columns([3, 2])
    with col_title:
        st.markdown('<div class="da-title">Settings / Config View</div>', unsafe_allow_html=True)
    with col_btns:
        b1, b2, b3 = st.columns(3)
        clone_clicked  = b1.button("⧉ Clone",      key="cfg_clone")
        active_clicked = b2.button("⚡ Make Active", key="cfg_activate", type="primary")
        export_clicked = b3.button("⬇ Export",      key="cfg_export")

    # ---- Summary card: strategy select + meta ----
    st.markdown('<div class="da-card">', unsafe_allow_html=True)
    sel_cols = st.columns([3, 1, 1, 1, 1])
    with sel_cols[0]:
        st.markdown('<span class="da-label">Select Strategy (Configuration)</span>', unsafe_allow_html=True)
        selected_label: str = st.selectbox("cfg_picker", options=cfg_labels, index=0, label_visibility="collapsed", key="cfg_label_sel")  # type: ignore[assignment]

    selected_config_id = cfg_id_by_label.get(selected_label, "")
    selected_meta = next((v for v in versions if v["config_id"] == selected_config_id), {})

    version_str = selected_meta.get("version", "—")
    is_active   = selected_meta.get("active_flag", False)
    created_at  = str(selected_meta.get("created_at", "—"))[:16]
    created_by  = selected_meta.get("created_by") or "system"
    active_html = f'<span class="da-activegreen">{version_str} (Active)</span>' if is_active else f'<span style="color:#64748b">{version_str}</span>'

    with sel_cols[1]:
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Version</div><div class="da-metavalue">{active_html}</div></div>', unsafe_allow_html=True)
    with sel_cols[2]:
        status_html = '<span class="da-activegreen">Active</span>' if is_active else '<span style="color:#94a3b8">Inactive</span>'
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Status</div><div class="da-metavalue">{status_html}</div></div>', unsafe_allow_html=True)
    with sel_cols[3]:
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Created On</div><div class="da-metavalue">{created_at}</div></div>', unsafe_allow_html=True)
    with sel_cols[4]:
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Created By</div><div class="da-metavalue">{created_by}</div></div>', unsafe_allow_html=True)

    # ---- Clone bar ----
    if clone_clicked:
        st.session_state[_K_CLONE_MODE] = True
    if st.session_state[_K_CLONE_MODE]:
        st.markdown('<div class="da-clonebar">', unsafe_allow_html=True)
        clone_cols = st.columns([3, 3, 1, 1])
        with clone_cols[0]:
            st.markdown('<span class="da-label">New Strategy Name</span>', unsafe_allow_html=True)
            new_name: str = st.text_input("newname", value=f"{selected_meta.get('strategy_name','')}_v2", label_visibility="collapsed", key="clone_new_name")  # type: ignore[assignment]
        with clone_cols[1]:
            st.markdown('<span class="da-label" style="color:#94a3b8">Tunable settings are editable in clone mode.</span>', unsafe_allow_html=True)
        with clone_cols[2]:
            save_clone = st.button("💾 Save Clone", key="clone_save", type="primary")
        with clone_cols[3]:
            cancel_clone = st.button("✕ Cancel", key="clone_cancel")
        if cancel_clone:
            st.session_state[_K_CLONE_MODE] = False
            st.rerun()
        if save_clone:
            _do_save_clone(role, selected_meta, new_name)
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)  # end summary card

    # ---- Button actions ----
    if active_clicked and selected_config_id:
        result = _svc().activate_strategy_config(
            config_id=selected_config_id, db_role=role,
            activated_by="dashboard", reason="activated via dashboard",
        )
        _show_result(result, f"'{selected_label}' is now active.")
        if result.status == "success":
            st.rerun()

    if export_clicked and selected_config_id:
        result = _svc().export_strategy_config_csv(config_id=selected_config_id, db_role=role)
        if result.status == "success":
            st.download_button(
                label=f"⬇ Download {result.metadata['filename']}",
                data=result.metadata["csv_bytes"],
                file_name=result.metadata["filename"],
                mime="text/csv",
                key="dl_cfg",
            )
        else:
            st.error("Export failed: " + "; ".join(result.errors or []))

    # ---- Filter controls ----
    ctrl_l, ctrl_r = st.columns([3, 2])
    with ctrl_l:
        type_filter: str = st.selectbox(
            "Show settings type",
            options=["Both", "Tunable only", "Hardcoded only"],
            index=0,
            key=_K_TYPE_FILTER,
        )  # type: ignore[assignment]
    with ctrl_r:
        exp_col, col_col = st.columns(2)
        expand_all   = exp_col.button("⊞ Expand All",   key="cfg_expand")
        collapse_all = col_col.button("⊟ Collapse All", key="cfg_collapse")

    # Manage expand state
    st.session_state.setdefault("cfg_expanded", {i: (i == 0) for i in range(len(_CONFIG_SECTIONS))})
    if expand_all:
        st.session_state["cfg_expanded"] = {i: True  for i in range(len(_CONFIG_SECTIONS))}
        st.rerun()
    if collapse_all:
        st.session_state["cfg_expanded"] = {i: False for i in range(len(_CONFIG_SECTIONS))}
        st.rerun()

    # ---- Load config_json for selected version ----
    config_json: dict = {}
    if selected_config_id:
        try:
            from app.services.config.config_service import ConfigService
            gr = ConfigService().get_strategy_config(config_id=selected_config_id, db_role=role)
            if gr.status == "success":
                config_json = gr.metadata.get("config", {}).get("config_json", {}) or {}
        except Exception:  # noqa: BLE001
            pass

    show_tunable   = type_filter in ("Both", "Tunable only")
    show_hardcoded = type_filter in ("Both", "Hardcoded only")
    clone_mode     = st.session_state[_K_CLONE_MODE]

    # ---- Accordion ----
    for idx, (section_title, icon, bg, settings) in enumerate(_CONFIG_SECTIONS):
        filtered = [
            s for s in settings
            if (s[2] and show_tunable) or (not s[2] and show_hardcoded)
        ]
        if not filtered:
            continue

        default_open = st.session_state["cfg_expanded"].get(idx, idx == 0)
        with st.expander(f"{icon}  {section_title}  ({len(filtered)} settings)", expanded=default_open):
            rows_html = ""
            for label, key_path, tunable in filtered:
                val = _get_nested(config_json, key_path)
                display_val = _fmt(val)
                type_icon = (
                    '<span class="da-edit" title="Tunable">✎</span>' if tunable
                    else '<span class="da-lock" title="Hardcoded">🔒</span>'
                )
                if clone_mode and tunable:
                    # Editable input in clone mode
                    new_val = st.text_input(label, value=display_val, key=f"clone_{key_path}")
                    # Store edits back into session state for Save Clone to read
                    st.session_state[f"_clone_edit_{key_path}"] = new_val
                else:
                    rows_html += (
                        f"<tr>"
                        f"<td>{label}</td>"
                        f"<td><input style='width:100%;padding:7px 10px;border:1px solid #dbe0e8;border-radius:6px;"
                        f"background:{'#fff' if tunable else '#f8fafc'};"
                        f"color:{'#0f172a' if tunable else '#94a3b8'};font-weight:600' "
                        f"{'readonly' if not tunable else ''} value=\"{display_val}\"></td>"
                        f"<td>{type_icon}</td>"
                        f"</tr>"
                    )

            if rows_html:
                st.markdown(
                    f'<table class="da-settings-table">'
                    f'<thead><tr><th>Setting</th><th>Value</th><th>Type</th></tr></thead>'
                    f'<tbody>{rows_html}</tbody>'
                    f'</table>',
                    unsafe_allow_html=True,
                )


def _do_save_clone(role: str, source_meta: dict, new_name: str) -> None:
    """Collect edited values from session state and call clone_strategy_config."""
    source_id = source_meta.get("config_id", "")
    base_json: dict = {}
    if source_id:
        try:
            from app.services.config.config_service import ConfigService
            gr = ConfigService().get_strategy_config(config_id=source_id, db_role=role)
            if gr.status == "success":
                base_json = dict(gr.metadata.get("config", {}).get("config_json", {}) or {})
        except Exception:  # noqa: BLE001
            pass

    # Apply any edits from clone mode inputs.
    for section_title, icon, bg, settings in _CONFIG_SECTIONS:
        for label, key_path, tunable in settings:
            if not tunable:
                continue
            edit_key = f"_clone_edit_{key_path}"
            if edit_key in st.session_state:
                edited = st.session_state[edit_key]
                # Apply to base_json (only top-level keys for now; nested handled best-effort)
                parts = key_path.split(".")
                cur: Any = base_json
                for p in parts[:-1]:
                    if isinstance(cur, dict) and p in cur:
                        cur = cur[p]
                    else:
                        cur = None
                        break
                if isinstance(cur, dict):
                    orig = cur.get(parts[-1])
                    try:
                        if isinstance(orig, bool):
                            cur[parts[-1]] = edited.lower() in ("true", "1", "yes")
                        elif isinstance(orig, int):
                            cur[parts[-1]] = int(edited)
                        elif isinstance(orig, float):
                            cur[parts[-1]] = float(edited)
                        else:
                            cur[parts[-1]] = edited
                    except (ValueError, TypeError):
                        pass  # leave original value if cast fails

    result = _svc().clone_strategy_config(
        db_role=role,
        strategy_name=new_name.strip() or source_meta.get("strategy_name", "clone"),
        config_json=base_json,
        parent_config_id=source_id or None,
        created_by="dashboard",
        notes=f"cloned from {source_meta.get('version','?')} via dashboard",
        activate=False,
    )
    _show_result(result, f"Config '{new_name}' saved (inactive). Select it and click Make Active to activate.")
    if result.status == "success":
        st.session_state[_K_CLONE_MODE] = False
        st.rerun()


# ------------------------------------------------------------------ #
# Other tabs (read-only, same as V1).
# ------------------------------------------------------------------ #

def _render_outcome_tracking() -> None:
    st.markdown('<div class="da-title">Outcome Tracking</div>', unsafe_allow_html=True)
    loader = _loader()
    summary = loader.load_outcome_summary()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total",     summary.total)
    c2.metric("Resolved",  summary.resolved)
    c3.metric("Unresolved", summary.unresolved)
    st.subheader("Average returns (resolved outcomes)")
    st.write({"5bd %": summary.avg_return_5bd_pct, "10bd %": summary.avg_return_10bd_pct,
               "20bd %": summary.avg_return_20bd_pct, "40bd %": summary.avg_return_40bd_pct})


def _render_ai_review() -> None:
    st.markdown('<div class="da-title">AI Review</div>', unsafe_allow_html=True)
    loader = _loader()
    role   = _db_role()
    reviews = loader.load_ai_reviews()
    if not reviews:
        st.info("No AI reviews yet. Use Daily Proposals → Export to create a ZIP package first.")
        return
    st.dataframe(pd.DataFrame(reviews), use_container_width=True, hide_index=True)
    st.divider()
    ids = [r.get("ai_review_id", "") for r in reviews if r.get("ai_review_id")]
    sel_id: str = st.selectbox("Select review", options=ids, key="air_sel")  # type: ignore[assignment]
    c1, c2 = st.columns(2)
    if c1.button("🤖 Send to AI", key="air_send"):
        with st.spinner("Sending…"):
            _show_result(_svc().send_ticker_review(ai_review_id=sel_id, db_role=role),
                         f"AI response recorded for {sel_id}.")
    action: str = c2.selectbox("Human action", ["ignored", "accepted", "overrode", "deferred"], key="air_action")  # type: ignore[assignment]
    if c2.button("✔ Record Action", key="air_record"):
        _show_result(_svc().record_human_action(ai_review_id=sel_id, human_action=action, db_role=role),
                     f"Action '{action}' recorded.")


def _render_pipeline_health() -> None:
    st.markdown('<div class="da-title">Pipeline Health</div>', unsafe_allow_html=True)
    loader = _loader()
    latest = loader.latest_pipeline_status()
    if latest:
        c1, c2 = st.columns(2)
        c1.metric("Status",   str(latest.get("status", "?")))
        c2.metric("Run date", str(latest.get("run_date", "?")))
        if latest.get("steps_completed"):
            st.caption(f"Steps completed: {latest['steps_completed']}")
        if latest.get("error_message"):
            st.error(f"Error: {latest['error_message']}")
    else:
        st.info("No pipeline runs yet.")
    st.subheader("Recent runs")
    st.dataframe(loader.load_pipeline_runs() or [], use_container_width=True, hide_index=True)
    st.subheader("Repair queue")
    st.dataframe(loader.load_repair_queue() or [], use_container_width=True, hide_index=True)


# ------------------------------------------------------------------ #
# DB guard.
# ------------------------------------------------------------------ #
def _db_missing() -> tuple[bool, str]:
    role = _db_role()
    try:
        from app.database import duckdb_manager
        p = duckdb_manager.get_database_path(role)
        return (not p.exists(), str(p))
    except Exception:  # noqa: BLE001
        return (False, "")


# ------------------------------------------------------------------ #
# Entry point.
# ------------------------------------------------------------------ #
def main() -> None:
    st.set_page_config(page_title="Stock Analyzer", layout="wide", page_icon="📈")
    _inject_css()
    _render_sidebar()

    missing, db_path = _db_missing()
    if missing:
        role = _db_role()
        st.warning(
            f"**{role}** database not found at `{db_path}`.\n\n"
            "- prod: `python tools/init_prod_db.py` then `python tools/run_prod_pipeline.py`\n"
            "- debug: `python tools/run_debug_pipeline.py --preset fast_smoke_test`"
        )
        return

    tab = st.session_state.get(_K_ACTIVE_TAB, "daily")
    if tab == "daily":
        _render_daily_proposals()
    elif tab == "settings":
        _render_settings()
    elif tab == "outcomes":
        _render_outcome_tracking()
    elif tab == "ai_review":
        _render_ai_review()
    elif tab == "health":
        _render_pipeline_health()


if __name__ == "__main__":
    main()
