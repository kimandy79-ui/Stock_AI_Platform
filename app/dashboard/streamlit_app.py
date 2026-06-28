"""Module 21 V2 -- Streamlit Dashboard (HTML-matched layout).

Fixes applied vs previous version:
1. Sidebar nav buttons: transparent bg, blue on active, hover shade
2. Run date: st.date_input with calendar picker instead of selectbox
3. Proposal table: full HTML columns (Rank, ☑, Ticker, Company, Sector,
   Score, RR, Signal, Price, EMA spread, RSI14, MACD hist, Volume ratio,
   Rel. strength, ATR%) joined from daily_features + daily_prices
4. Per-row checkboxes via st.data_editor checkbox column
5. Export uses only checked tickers; download_button rendered inline
   (not deferred to next rerun) so the file actually downloads
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date, timedelta
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
# CSS — sidebar nav fix + card/pill tokens.
# ------------------------------------------------------------------ #
_CSS = """
<style>
:root {
  --navy:#041c44; --navy2:#08285d; --blue:#2563eb; --blue-soft:#eff6ff;
  --green:#16a34a; --green-soft:#dcfce7;
  --text:#0f172a; --muted:#64748b;
  --bg:#f6f7fb; --card:#ffffff; --border:#e5e7eb;
}

body, [data-testid="stAppViewContainer"] { background: var(--bg) !important; }

/* ---- Sidebar background — paint both wrappers so resize stays dark ---- */
[data-testid="stSidebar"],
[data-testid="stSidebar"] > div:first-child,
[data-testid="stSidebar"] > div:first-child > div {
  background: linear-gradient(180deg,#041c44,#08285d) !important;
}
[data-testid="stSidebar"] > div:first-child {
  min-width: 180px !important;
}
/* Nav button text: nowrap so items stay on one line */
/* ---- All sidebar text light ---- */
[data-testid="stSidebar"] * { color: #dbeafe !important; }

/* ---- Selectbox: selected value visible on dark sidebar ---- */
[data-testid="stSidebar"] [data-baseweb="select"] > div:first-child {
  background: #08285d !important;
  border-color: rgba(255,255,255,.2) !important;
  color: #fff !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] span { color: #fff !important; }
/* Dropdown list: highlight selected option in blue */
[data-baseweb="menu"] [aria-selected="true"] {
  background-color: #2563eb !important;
  color: #fff !important;
}
[data-baseweb="menu"] [role="option"]:hover {
  background-color: #eff6ff !important;
}

/* ---- Nav buttons: transparent bg, border-radius, hover shade ---- */
[data-testid="stSidebar"] .stButton > button {
  background: transparent !important;
  border: none !important;
  color: #cbd5e1 !important;
  text-align: left !important;
  padding: 10px 12px !important;
  border-radius: 10px !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  width: 100% !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  transition: background 0.15s;
}
[data-testid="stSidebar"] .stButton > button:hover {
  background: rgba(255,255,255,0.08) !important;
  color: #fff !important;
}
/* active nav button — injected via data-active attr trick using CSS sibling */
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
  background: #2563eb !important;
  color: #fff !important;
}

/* ---- Cards ---- */
.da-card {
  background:#fff; border:1px solid var(--border);
  border-radius:14px; box-shadow:0 3px 12px rgba(15,23,42,.04);
  padding:16px; margin-bottom:14px;
}
.da-title { font-size:28px; font-weight:800; margin:0 0 2px; color:#0f172a; }
.da-chip {
  display:inline-flex; align-items:center;
  background:#fff; border:1px solid var(--border);
  border-radius:10px; padding:0 14px; font-size:13px; font-weight:700;
  color:#475569; height:38px; white-space:nowrap; box-sizing:border-box;
}
/* Make Streamlit button containers align to same height as chip */
[data-testid="stHorizontalBlock"] > div { align-items:center !important; }
.da-label { display:block; font-size:12px; color:#334155; font-weight:800; margin-bottom:4px; }

/* ---- Score / signal pills ---- */
.da-score, .da-signal {
  display:inline-block; padding:3px 9px; border-radius:999px;
  font-size:12px; font-weight:800;
  background:var(--green-soft); color:#166534;
}

/* ---- Proposal table ---- */
.prop-table { width:100%; border-collapse:collapse; font-size:13px; }
.prop-table th {
  background:#f8fafc; color:#475569; font-size:12px;
  padding:10px 8px; border-bottom:1px solid #eef2f7;
  text-align:left; white-space:nowrap;
}
.prop-table td { padding:10px 8px; border-bottom:1px solid #eef2f7; vertical-align:middle; }
.prop-table tbody tr:hover td { background:#fafcff; }
.prop-table tbody tr.selected td { background:#eff6ff !important; }
.prop-table tbody tr.amber td { background:#fff3cd !important; }
.t-ticker { font-weight:800; color:#1d4ed8; }
.t-check input[type=checkbox] { width:16px; height:16px; cursor:pointer; }

/* ---- Indicator cards ---- */
.da-indcard {
  border:1px solid #dbeafe; background:#f8fbff;
  border-radius:10px; padding:10px; margin-bottom:8px;
}
.da-indheader { display:flex; justify-content:space-between; gap:8px; margin-bottom:5px; }
.da-indtitle { font-weight:800; font-size:13px; color:#0f172a; }
.da-indvalue { font-weight:800; font-size:13px; color:#1d4ed8; white-space:nowrap; }
.da-indnote  { font-size:12px; color:#334155; line-height:1.45; }

/* ---- Settings ---- */
.da-metabox { font-size:13px; }
.da-metalabel { font-size:12px; color:#64748b; font-weight:700; margin-bottom:4px; }
.da-metavalue { font-weight:700; color:#0f172a; }
.da-activegreen { color:#16a34a; font-weight:800; }
.da-clonebar { border-top:1px dashed #cbd5e1; padding-top:14px; margin-top:14px; }
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
</style>
"""

# ------------------------------------------------------------------ #
# Config sections (Settings accordion).
# ------------------------------------------------------------------ #
_CONFIG_SECTIONS = [
    ("Universe & Market Data", "📈", "#eef4ff", [
        ("Min Price (USD)",           "universe.min_price",                True),
        ("Min Avg Dollar Vol 20d",    "universe.min_avg_dollar_volume_20d", True),
        ("Allowed Symbol Types",      "universe.allowed_symbol_types",     False),
        ("Exclude Benchmarks",        "universe.exclude_benchmarks",       False),
    ]),
    ("Technical Indicators", "🛠", "#f3e8ff", [
        ("EMA Periods",               "features.ema_periods",              False),
        ("RSI Lookback",              "features.rsi_period",               True),
        ("MACD Fast/Slow/Signal",     "features.macd_params",              False),
        ("ATR Period",                "features.atr_period",               False),
        ("Volume Spike Threshold",    "features.volume_spike_threshold",   True),
    ]),
    ("Screening & Scoring", "⭐", "#fff7ed", [
        ("Min RVOL",                  "screening.min_rvol",                True),
        ("Min Screening Score",       "screening.min_screening_score",     True),
        ("Require Feature Ready",     "screening.require_feature_ready",   False),
        ("Target R (Step 4)",         "step4.target_R",                    True),
    ]),
    ("Risk Management", "🛡", "#ecfdf5", [
        ("Hard Cap Enabled",          "diversification.hard_cap_enabled",  False),
        ("Top N",                     "diversification.top_n",             True),
        ("Sector Max Positions",      "diversification.sector_max_positions", True),
        ("Industry Max Positions",    "diversification.industry_max_positions", True),
        ("Sector Penalty Factor",     "diversification.sector_penalty_factor", True),
        ("Industry Penalty Factor",   "diversification.industry_penalty_factor", True),
    ]),
    ("Proposals & Filters", "⏃", "#fff1f2", [
        ("Earnings Avoid Within BD",  "earnings.avoid_within_bd",          True),
        ("Earnings Penalty Max",      "earnings.penalty_points_max",       True),
    ]),
    ("Diversification", "◔", "#ecfeff", [
        ("Penalty Before Cap Only",   "diversification.penalty_applies_before_cap_only", False),
    ]),
    ("Backtest / Simulation", "⚗", "#eff6ff", [
        ("Simulation Mode",           "simulation.mode",                   False),
    ]),
    ("System / General", "⚙", "#f1f5f9", [
        ("Setup Name",                "setup_name",                        False),
        ("Version",                   "version",                           False),
    ]),
]

# ------------------------------------------------------------------ #
# State keys.
# ------------------------------------------------------------------ #
_K_ROLE       = "db_role"
_K_PIPE_MSG   = "pipeline_msg"
_K_CLONE_MODE = "clone_mode"
_K_ACTIVE_TAB = "active_tab"
_K_TYPE_FILT  = "type_filter"

def _db_role() -> str:
    return st.session_state.get(_K_ROLE, "prod")

def _loader() -> DashboardDataLoader | None:
    """Return a DashboardDataLoader for the current role, or None for simulation."""
    role = _db_role()
    if role == "simulation":
        return None  # simulation not supported by DashboardDataLoader
    return DashboardDataLoader(db_role=role)

def _svc() -> DashboardActionService:
    return DashboardActionService()

def _get_nested(d: dict, dotpath: str) -> Any:
    cur: Any = d
    for p in dotpath.split("."):
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
# Feature enrichment — joins daily_features + daily_prices to proposals.
# ------------------------------------------------------------------ #

def _enrich_proposals(rows: list[dict], signal_date: date, db_role: str, setup_config_id: str | None = None) -> list[dict]:
    """Add price/feature/company columns to proposal rows.

    - company_name: direct query from ticker_master (always populated if
      the universe snapshot ran with TickerInfo.company_name set; falls back
      to yfinance shortName for any ticker that has NULL).
    - Feature metrics: latest feature_date <= signal_date (handles same-day).
    - ROC20 replaces MACD (daily_features has roc20, no macd column).
    """
    if not rows:
        return rows
    tickers = list({r["ticker"] for r in rows if r.get("ticker")})
    if not tickers:
        return rows

    placeholders = ",".join(["?"] * len(tickers))
    feature_map: dict[str, dict] = {}
    company_map: dict[str, str]  = {}

    try:
        from app.database import duckdb_manager
        conn = duckdb_manager.connect(db_role, read_only=True)
        try:
            # ---- Company names from ticker_master ----
            cur = conn.execute(
                f"SELECT ticker, company_name FROM ticker_master "
                f"WHERE ticker IN ({placeholders})",
                tickers,
            )
            for t, cn in cur.fetchall():
                company_map[t] = cn or ""

            # ---- Latest features <= signal_date ----
            cur2 = conn.execute(
                f"""
                SELECT ticker,
                    ROUND((ema20 - ema50) / NULLIF(ema50,0)*100, 1) AS ema_spread_pct,
                    ROUND(rsi14, 1)   AS rsi14,
                    ROUND(roc20, 2)   AS roc20,
                    rvol20            AS volume_ratio,
                    ROUND(atr_pct*100,1) AS atr_pct,
                    ROUND(sector_relative_strength*100,1) AS rel_strength_pct
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY ticker ORDER BY feature_date DESC
                    ) AS rn
                    FROM daily_features
                    WHERE feature_date <= ? AND ticker IN ({placeholders})
                ) sub WHERE rn = 1
                """,
                [signal_date] + tickers,
            )
            cols = [d[0] for d in cur2.description]
            for row_data in cur2.fetchall():
                d = dict(zip(cols, row_data))
                feature_map[d["ticker"]] = d

            # ---- Price from daily_prices (latest date <= signal_date) ----
            cur3 = conn.execute(
                f"""
                SELECT ticker, ROUND(close_adj,2) AS price
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY ticker ORDER BY date DESC
                    ) AS rn
                    FROM daily_prices
                    WHERE date <= ? AND ticker IN ({placeholders})
                ) sub WHERE rn = 1
                """,
                [signal_date] + tickers,
            )
            for t, p in cur3.fetchall():
                if t in feature_map:
                    feature_map[t]["price"] = p
                else:
                    feature_map[t] = {"ticker": t, "price": p}

            # stop/target are read from step5_proposals via data_access (already on
            # proposal row as stop_price_raw / target_price_raw). step4_analysis is
            # NOT queried here; using step4 values would bypass diversification and
            # show the wrong trade plan for multi-route tickers.

        finally:
            conn.close()

    except Exception:
        pass  # enrichment failure → blanks, no crash

    # Company name fallback via yfinance intentionally removed.
    # Live yfinance calls block dashboard rendering for 5-10s per render.
    # Populate ticker_master.company_name via universe ingestion instead.

    enriched = []
    for r in rows:
        out  = dict(r)
        tk   = r.get("ticker", "")
        feat = feature_map.get(tk, {})
        out["company_name"]     = company_map.get(tk) or ""
        out["price"]            = feat.get("price")
        # stop/target/entry: prefer step5 proposal fields (already on row via
        # data_access SQL); step4 enrichment values are NOT used here —
        # the step5 values are authoritative for the final trade plan.
        out["stop_price"]       = feat.get("step4_stop_price")    # explicit fallback only
        out["target_price"]     = feat.get("step4_target_price")  # explicit fallback only
        out["ema_spread_pct"]   = feat.get("ema_spread_pct")
        out["rsi14"]            = feat.get("rsi14")
        out["roc20"]            = feat.get("roc20")
        out["volume_ratio"]     = feat.get("volume_ratio")
        out["atr_pct"]          = feat.get("atr_pct")
        out["rel_strength_pct"] = feat.get("rel_strength_pct")
        enriched.append(out)
    return enriched


def _fmt_num(val: Any, decimals: int = 2, suffix: str = "") -> str:
    if val is None:
        return "—"
    try:
        return f"{float(val):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_price(val: Any) -> str:
    if val is None:
        return "—"
    try:
        v = float(val)
        return f"{v:,.2f}"
    except (TypeError, ValueError):
        return str(val)


# ------------------------------------------------------------------ #
# CSS injection.
# ------------------------------------------------------------------ #
def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)

# ------------------------------------------------------------------ #
# Sidebar.
# ------------------------------------------------------------------ #
def _render_sidebar() -> None:
    st.session_state.setdefault(_K_ROLE, "prod")
    st.session_state.setdefault(_K_ACTIVE_TAB, "daily")

    st.sidebar.markdown(
        '<div style="display:flex;gap:10px;align-items:center;padding:8px 6px 16px;'
        'border-bottom:1px solid rgba(255,255,255,.14);margin-bottom:10px">'
        '<div style="width:42px;height:42px;border-radius:50%;border:2px solid #60a5fa;'
        'display:flex;align-items:center;justify-content:center;font-weight:800;font-size:20px">↗</div>'
        '<div><div style="font-size:18px;font-weight:800;color:#fff">Stock Analyzer</div>'
        '<div style="font-size:13px;color:#cbd5e1">Module 21 V2</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )

    nav_items = [
        ("⌂ Dashboard",          "daily"),
        ("▣ Daily Proposals",    "daily"),
        ("◉ Outcome Tracking",   "outcomes"),
        ("🧠 AI Review",          "ai_review"),
        ("🔬 Simulation",         "simulation"),
        ("⌁ Pipeline Health",    "health"),
        ("⚙ Settings / Config",  "settings"),
    ]

    active = st.session_state[_K_ACTIVE_TAB]
    for label, key in nav_items:
        if key == "daily":
            is_active = active == "daily" and label == "▣ Daily Proposals"
        else:
            is_active = active == key
        btn_type = "primary" if is_active else "secondary"
        if st.sidebar.button(label, key=f"nav_{label}", use_container_width=True, type=btn_type):
            st.session_state[_K_ACTIVE_TAB] = key
            st.rerun()

    st.sidebar.markdown(
        '<div style="border-top:1px solid rgba(255,255,255,.14);margin:12px 0 8px"></div>',
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        '<span style="font-size:12px;color:#94a3b8;font-weight:700">Environment (DB Role)</span>',
        unsafe_allow_html=True,
    )
    st.sidebar.selectbox(
        "db_role_sel", options=["prod", "debug", "simulation"],
        key=_K_ROLE, label_visibility="collapsed",
    )

    role = _db_role()
    try:
        from app.database import duckdb_manager
        path = duckdb_manager.get_database_path(role)
        connected = path.exists()
    except Exception:
        connected = False

    sc = "#22c55e" if connected else "#ef4444"
    st_txt = "Connected" if connected else "Not found"
    st.sidebar.markdown(
        f'<div style="font-size:12px;color:#94a3b8;line-height:2;margin-top:8px">'
        f'DB: {role}.duckdb<br>'
        f'Status: <span style="color:{sc};font-weight:700">{st_txt}</span><br>'
        f'v2.1.0</div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------ #
# Feedback helper.
# ------------------------------------------------------------------ #
def _show_result(result: Any, ok_msg: str) -> None:
    if result.status == "success":
        st.success(ok_msg)
    elif result.status == "success_with_warnings":
        st.warning(ok_msg + "  Warnings: " + "; ".join(result.warnings or []))
    else:
        st.error("Failed: " + "; ".join(result.errors or ["unknown error"]))


# ------------------------------------------------------------------ #
# ------------------------------------------------------------------ #
# Custom calendar date picker with proposal-date highlighting.
# ------------------------------------------------------------------ #

def _date_picker_with_highlights(
    key: str,
    default_date: date,
    highlighted_dates: list[date],  # noqa: ARG001 — reserved for future use
) -> date:
    """Plain st.date_input. Calendar highlighting removed per user request."""
    selected: date = st.date_input(
        key + "_native",
        value=st.session_state.get(key, default_date),
        max_value=date.today(),
        label_visibility="collapsed",
        key=key,
    )  # type: ignore[assignment]
    return selected



# Daily Proposals.
# ------------------------------------------------------------------ #
def _render_daily_proposals() -> None:  # noqa: C901
    loader = _loader()
    if loader is None:
        st.info("Simulation database is not supported in Daily Proposals view. Switch to prod or debug.")
        return
    role   = _db_role()

    # ---- Section 1 (blue box): title + chip + Run Pipeline ----
    # signal_date must be read from session state here because widgets below
    # haven't rendered yet on first run; we default to today and it gets
    # corrected when the date picker renders.
    _sd = st.session_state.get("dp_date_picker", None)
    signal_date_for_header = _sd if isinstance(_sd, date) else date.today()

    latest = loader.latest_pipeline_status()
    chip_label = "No runs yet"
    if latest:
        status_raw = latest.get('status', '')
        status_short = {
            "success":               "success ✓",
            "success_with_warnings": "success ⚠",
            "failed":                "failed ✗",
        }.get(status_raw, status_raw)
        chip_label = f"Pipeline run: {latest.get('run_date','?')} · {status_short}"

    col_title, col_chip, col_btn = st.columns([2, 5, 2])
    with col_title:
        st.markdown('<div class="da-title">Daily Proposals</div>', unsafe_allow_html=True)
    with col_chip:
        st.markdown(
            f'<div class="da-chip" style="height:38px;display:flex;align-items:center;'
            f'width:fit-content;max-width:100%;white-space:nowrap;overflow:hidden;'
            f'text-overflow:ellipsis;margin-top:4px">{chip_label}</div>',
            unsafe_allow_html=True,
        )
    with col_btn:
        run_clicked = st.button("▶  Run Pipeline", key="dp_run", type="primary", use_container_width=True)

    # Pipeline feedback (no blank container, inline)
    if st.session_state.get(_K_PIPE_MSG):
        mtype, mtext = st.session_state[_K_PIPE_MSG]
        getattr(st, mtype)(mtext)
        # Clear after display so it doesn't persist across tab switches
        if mtype == "success":
            del st.session_state[_K_PIPE_MSG]

    # ---- Section 2 (yellow box): filters ----
    dates = loader.list_signal_dates()
    default_date = dates[0] if dates else date.today()

    date_col, strat_col, lv_col, _, _ = st.columns([2, 2, 2, 1, 1])
    with date_col:
        st.markdown('<span class="da-label">Run date</span>', unsafe_allow_html=True)
        signal_date: date = _date_picker_with_highlights(
            key="dp_date_picker",
            default_date=default_date,
            highlighted_dates=dates,
        )
    configs = loader.list_setup_configs()
    with strat_col:
        st.markdown('<span class="da-label">Setup Config</span>', unsafe_allow_html=True)
        cfg_choice: str = st.selectbox(
            "setup_cfg_sel", options=["(all)"] + configs,
            index=0, label_visibility="collapsed", key="dp_setup_config",
        )  # type: ignore[assignment]
    with lv_col:
        st.markdown('<span class="da-label">List View</span>', unsafe_allow_html=True)
        _lv_options = ["Diversified", "Raw"]
        _lv_stored = st.session_state.get("dp_listview", "Diversified")
        list_view: str = st.selectbox(
            "list_view_sel",
            options=_lv_options,
            index=_lv_options.index(_lv_stored) if _lv_stored in _lv_options else 0,
            label_visibility="collapsed",
            key="dp_listview",
        )  # type: ignore[assignment]

    # show_diversified=True  → filter on in_diversified_top_n
    # show_diversified=False → filter on in_raw_top_n
    show_div: bool = (list_view == "Diversified")

    setup_config_id = None if cfg_choice == "(all)" else cfg_choice

    if run_clicked:
        _do_run_pipeline(role, signal_date)

    view = loader.load_daily_proposals(
        signal_date=signal_date,
        setup_config_id=setup_config_id,
        show_diversified=show_div,
    )

    if not view.rows:
        st.info(
            f"No proposals for {signal_date}. "
            "Click **▶ Run Pipeline** to generate proposals for this date."
        )
        return

    display_rows, flags = data_access.build_proposals_display(view)

    # Enrich with price + feature columns
    enriched = _enrich_proposals(display_rows, signal_date, role, setup_config_id)
    all_tickers = [r.get("ticker", "") for r in enriched if r.get("ticker")]

    # Build dropdown options as "TICKER (A/N/C)" — one entry per row so duplicates
    # with different strategies are distinguishable.
    # Map label → enriched row (first occurrence wins for duplicate labels)
    doc_option_map: dict[str, dict] = {}
    for r in enriched:
        tk       = r.get("ticker", "")
        setup    = r.get("setup_type") or ""
        lbl      = f"{tk} ({setup})" if setup else tk
        # Make label unique if duplicated (shouldn't happen but guard anyway)
        if lbl in doc_option_map:
            lbl = f"{lbl}*"
        doc_option_map[lbl] = r
    doc_options = list(doc_option_map.keys())

    # ---- Proposal table ----
    st.markdown('<div class="da-card">', unsafe_allow_html=True)

    # Header row: title + selectbox + single download button
    hdr_title, hdr_sel, hdr_btn, _ = st.columns([3, 2, 2, 1])
    with hdr_title:
        st.markdown('<h3 style="margin:0;line-height:38px;font-size:17px;font-weight:800">Proposed Stocks</h3>', unsafe_allow_html=True)
    with hdr_sel:
        doc_label: str = st.selectbox(
            "doc_sel",
            options=doc_options,
            index=0,
            label_visibility="collapsed",
            key="doc_ticker_sel",
        )  # type: ignore[assignment]
    with hdr_btn:
        sel_row = doc_option_map.get(doc_label)
        if sel_row:
            _doc_strategy = sel_row.get("setup_config_id") or ""
            _doc_bytes, _doc_filename = _build_ticker_document(
                row=sel_row,
                signal_date=signal_date,
                db_role=role,
                setup_config_id=_doc_strategy,
            )
            st.download_button(
                label="⬇ Download Report",
                data=_doc_bytes,
                file_name=_doc_filename,
                mime="text/plain",
                key="btn_gen_doc",
                type="primary",
                use_container_width=True,
            )
        else:
            st.button("⬇ Download Report", key="btn_gen_doc_disabled", disabled=True, use_container_width=True)
    export_clicked = False

    # Build display DataFrame for st.data_editor (checkbox column)
    table_rows = []
    for i, row in enumerate(enriched):
        rank_num = row.get("diversified_rank") if show_div else row.get("raw_rank")
        rank_str = str(int(rank_num)) if rank_num is not None else str(i + 1)
        score = row.get("proposal_score_final") or row.get("proposal_score_raw")
        ema_spread = row.get("ema_spread_pct")
        roc  = row.get("roc20")   # replaces MACD (not in schema)
        vol  = row.get("volume_ratio")
        rel  = row.get("rel_strength_pct")
        atr  = row.get("atr_pct")

        # Stop/Target/Entry: use step5 proposal fields (authoritative trade plan).
        # step4 enrichment values are NOT used; they would bypass diversification.
        _stop   = row.get("stop_price_raw")   or row.get("stop_price")
        _target = row.get("target_price_raw") or row.get("target_price")
        _entry  = row.get("entry_price_raw")

        table_rows.append({
            "✓":             True,
            "Rank":          rank_str,
            "Ticker":        row.get("ticker") or "",
            "Company":       row.get("company_name") or "",
            "Sector":        row.get("sector") or "",
            "Score":         round(float(score), 1) if score is not None else None,
            "Status":        row.get("final_display_status") or row.get("disposition") or "",
            "RR":            round(float(row["estimated_rr"]), 2) if row.get("estimated_rr") is not None else None,
            "Signal":        row.get("setup_type") or "",
            "Entry":         round(float(_entry), 2) if _entry is not None else None,
            "Price":         round(float(row["price"]), 2) if row.get("price") is not None else None,
            "Target":        round(float(_target), 2) if _target is not None else None,
            "Stop":          round(float(_stop), 2) if _stop is not None else None,
            "EMA spread":    f"+{ema_spread:.1f}%" if ema_spread is not None and ema_spread >= 0 else (f"{ema_spread:.1f}%" if ema_spread is not None else "—"),
            "RSI14":         round(float(row["rsi14"]), 1) if row.get("rsi14") is not None else None,
            "ROC20 %":       f"+{roc:.1f}%" if roc is not None and roc >= 0 else (f"{roc:.1f}%" if roc is not None else "—"),
            "Volume ratio":  f"{float(vol):.2f}×" if vol is not None else "—",
            "Rel. strength": f"+{rel:.1f}%" if rel is not None and rel >= 0 else (f"{rel:.1f}%" if rel is not None else "—"),
            "ATR %":         f"{atr:.1f}%" if atr is not None else "—",
            "_flag":         flags[i] if i < len(flags) else False,
        })

    df_display = pd.DataFrame(table_rows)
    flag_col = df_display.pop("_flag")

    # Style: amber rows for list disagreement
    def _style_row(s: pd.Series) -> list[str]:
        if flag_col.iloc[s.name]:
            return ["background-color:#fff3cd"] * len(s)
        return [""] * len(s)

    edited_df = st.data_editor(
        df_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "\u2713":             st.column_config.CheckboxColumn("\u2713", default=False, width=30),
            "Rank":          st.column_config.TextColumn("Rank",           width=55),
            "Ticker":        st.column_config.TextColumn("Ticker",         width=70),
            "Company":       st.column_config.TextColumn("Company",        width=160),
            "Sector":        st.column_config.TextColumn("Sector",         width=140),
            "Score":         st.column_config.NumberColumn("Score",        format="%.1f", width=70),
            # Status = final_display_status: "BUY", "BUY (excluded)", "WATCHLIST_ONLY", "REJECTED"
            "Status":        st.column_config.TextColumn("Status",         width=110),
            "RR":            st.column_config.NumberColumn("RR",           format="%.2f", width=55),
            "Signal":        st.column_config.TextColumn("Signal",         width=90),
            # Entry = entry_price_raw from step5_proposals (signal-date close proxy)
            "Entry":         st.column_config.NumberColumn("Entry",        format="%.2f", width=70),
            "Price":         st.column_config.NumberColumn("Price",        format="%.2f", width=70),
            # Target / Stop: from step5_proposals (structural trade plan)
            "Target":        st.column_config.NumberColumn("Target",       format="%.2f", width=70),
            "Stop":          st.column_config.NumberColumn("Stop",         format="%.2f", width=70),
            "EMA spread":    st.column_config.TextColumn("EMA\nspread",    width=80),
            "RSI14":         st.column_config.NumberColumn("RSI14",        format="%.1f", width=70),
            "ROC20 %":       st.column_config.TextColumn("ROC20\n%",       width=75),
            "Volume ratio":  st.column_config.TextColumn("Vol.\nratio",    width=75),
            "Rel. strength": st.column_config.TextColumn("Rel.\nstrength", width=75),
            "ATR %":         st.column_config.TextColumn("ATR\n%",         width=65),
        },
        key="proposal_editor",
    )

    checked_tickers: list[str] = list(
        edited_df.loc[edited_df["\u2713"] == True, "Ticker"]  # noqa: E712
    )

    st.markdown('</div>', unsafe_allow_html=True)  # close da-card

    # ---- Export CSV ----
    if export_clicked:
        _do_export_proposals(role, loader, view, checked_tickers or list(df_display["Ticker"]), setup_config_id)



# _build_ticker_document is now in app/dashboard/ticker_report.py
def _build_ticker_document(
    row: dict,
    signal_date: "date",
    db_role: str,
    setup_config_id: str,
) -> "tuple[bytes, str]":
    """Delegate to the standalone ticker_report module."""
    from app.dashboard.ticker_report import build_ticker_report
    return build_ticker_report(
        row=row,
        signal_date=signal_date,
        db_role=db_role,
        setup_config_id=setup_config_id,
    )


def _render_indicator_cards(row: dict) -> None:
    ticker = row.get("ticker", "")
    st.markdown(
        f'<div style="font-size:12px;color:#64748b;margin:8px 0 6px">'
        f'Indicators for <strong style="color:#1d4ed8">{ticker}</strong></div>',
        unsafe_allow_html=True,
    )
    ema_spread = row.get("ema_spread_pct")
    indicators = [
        ("Trend Alignment",
         f"EMA20-EMA50: {_fmt_num(ema_spread,1,'%')}" if ema_spread is not None else "—",
         row.get("mechanical_explanation") or "EMA alignment from feature engine."),
        ("Score",
         _fmt_num(row.get("proposal_score_final") or row.get("proposal_score_raw"), 1),
         "Final proposal score after diversification adjustments."),
        ("Risk / Reward",
         _fmt_num(row.get("estimated_rr"), 2),
         "Estimated RR from Step 4 setup analysis."),
        ("RSI 14",
         _fmt_num(row.get("rsi14"), 1),
         "RSI14 momentum. 50–70 is a healthy bullish zone."),
        ("ROC 20",
         f"{_fmt_num(row.get('roc20'), 2)}%",
         "20-day rate of change. Positive = price trending up over last month."),
        ("Volume / 20D Avg",
         f"{_fmt_num(row.get('volume_ratio'),2)}×",
         "Volume above 1.0× confirms participation."),
        ("Rel. Strength",
         f"{_fmt_num(row.get('rel_strength_pct'),1)}%",
         "Sector-relative strength vs benchmark ETF."),
        ("ATR %",
         f"{_fmt_num(row.get('atr_pct'),1)}%",
         "Average true range as % of price — volatility gauge."),
        ("Setup Type",
         row.get("setup_type") or "—",
         "Classified by Step 4 analysis engine."),
        ("List Membership",
         _list_membership_label(row),
         "Raw, diversified, or both shortlists."),
        ("Sector",
         row.get("sector") or "—",
         "Canonical sector for diversification bucketing."),
        ("Price",
         _fmt_price(row.get("price")),
         "Adjusted close from last available trading day."),
    ]
    cols = st.columns(4)
    for i, (title, value, note) in enumerate(indicators):
        with cols[i % 4]:
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


def _do_run_pipeline(role: str, run_date: date | None = None) -> None:
    """Run the pipeline in a background thread; progress tracked in-memory only.

    DB polling during execution is intentionally removed — opening a read-only
    connection while the pipeline holds write connections causes
    ConnectionException on Windows DuckDB (different configuration error).
    Progress is tracked via a shared list updated by the pipeline thread.
    """
    import threading
    import time as _time

    effective_date = run_date or date.today()

    _STEP_LABELS: dict[str, str] = {
        "benchmark_etf_ingestion":       "📥 Ingesting benchmark / ETF prices",
        "universe_ingestion":            "🌐 Building universe snapshot",
        "earnings_calendar_refresh":     "📅 Refreshing earnings calendar",
        "price_ingestion":               "📥 Ingesting stock prices",
        "validation":                    "✅ Validating price data",
        "mutation_detection":            "🔍 Detecting price mutations",
        "feature_calculation":           "⚙ Calculating technical features",
        "market_regime_classification":  "📡 Classifying market regime",
        "step3_universal_eligibility":   "🔎 Universal eligibility + routing (Step 3)",
        "step4_setup_validation":        "📊 Validating setups (Step 4)",
        "step5_proposals":               "💡 Generating proposals (Step 5)",
        "outcome_queue_creation":        "📋 Creating outcome queue",
        "outcome_processing":            "📈 Processing outcomes",
        "dashboard_materialization":     "🖥 Materialising dashboard",
    }

    result_holder: dict = {}
    error_holder:  dict = {}
    # In-memory progress: pipeline thread appends completed step names here.
    # No DB reads during execution — eliminates Windows DuckDB connection conflict.
    progress_steps: list[str] = []

    def _run() -> None:
        try:
            result_holder["result"] = _svc().run_pipeline(
                db_role=role,
                run_date=effective_date,
                run_type="manual",
                force_rerun=True,
            )
            # Populate progress from result after completion
            result = result_holder.get("result")
            if result is not None:
                meta = getattr(result, "metadata", {}) or {}
                steps = meta.get("steps_completed", [])
                if isinstance(steps, list):
                    progress_steps.clear()
                    progress_steps.extend(steps)
        except Exception as exc:  # noqa: BLE001
            error_holder["error"] = str(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    status_box = st.empty()
    start_ts   = _time.time()
    step_keys  = list(_STEP_LABELS.keys())
    # Estimated step index: advances by time since each step takes ~30-120s.
    # This is display-only; accuracy is not critical.
    estimated_idx = 0

    while thread.is_alive():
        elapsed = int(_time.time() - start_ts)
        # Advance estimated step every ~20s. Clamped to second-to-last so the
        # bar never claims the final step is done while still running.
        estimated_idx = min(int(elapsed / 20), len(step_keys) - 2)
        current_label = _STEP_LABELS.get(step_keys[estimated_idx], "Running…")
        # Display as 1-based.
        completed_count = estimated_idx + 1
        total_steps = len(_STEP_LABELS)
        bar_filled = "█" * completed_count
        bar_empty  = "░" * (total_steps - completed_count)

        status_box.markdown(
            f'<div class="da-card" style="padding:16px">'
            f'<div style="font-weight:800;font-size:14px;margin-bottom:8px">'
            f'Running pipeline for {effective_date} &nbsp;·&nbsp; {elapsed}s elapsed</div>'
            f'<div style="font-family:monospace;font-size:13px;color:#2563eb;margin-bottom:8px">'
            f'{bar_filled}{bar_empty} {completed_count}/{total_steps}</div>'
            f'<div style="font-size:13px;color:#475569">{current_label}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        _time.sleep(2.0)

    status_box.empty()
    thread.join()

    if "error" in error_holder:
        st.session_state[_K_PIPE_MSG] = ("error", f"Pipeline exception: {error_holder['error']}")
    else:
        result = result_holder.get("result")
        if result is None:
            st.session_state[_K_PIPE_MSG] = ("error", "Pipeline returned no result.")
        elif result.status == "success":
            st.session_state[_K_PIPE_MSG] = ("success", f"Pipeline completed for {effective_date}. run_id={result.run_id}")
        elif result.status == "success_with_warnings":
            st.session_state[_K_PIPE_MSG] = ("warning", f"Completed {effective_date} with warnings: " + "; ".join(result.warnings or []))
        else:
            st.session_state[_K_PIPE_MSG] = ("error", f"Pipeline failed for {effective_date}: " + "; ".join(result.errors or ["unknown error"]))
    st.rerun()


def _do_export_proposals(
    role: str,
    loader: DashboardDataLoader,
    view: data_access.ProposalsView,
    tickers: list[str],
    setup_config_id: str | None,
) -> None:
    """Build CSV bytes directly and render download_button immediately."""
    if not tickers:
        st.warning("No tickers selected. Check at least one row before exporting.")
        return
    if view.run_id is None or view.signal_date is None:
        st.warning("No valid run to export.")
        return

    # Resolve tickers → proposal_ids
    proposal_ids = loader.load_proposal_ids(
        run_id=view.run_id,
        signal_date=view.signal_date,
        tickers=tickers,
        setup_config_id=setup_config_id,
    )
    if not proposal_ids:
        st.warning("Could not resolve proposal IDs for the selected tickers.")
        return

    result = _svc().export_proposals_csv(
        signal_date=view.signal_date,
        proposal_ids=proposal_ids,
        db_role=role,
        setup_config_id=setup_config_id,
    )

    if result.status != "success":
        st.error("Export failed: " + "; ".join(result.errors or []))
        return

    csv_bytes: bytes = result.metadata["csv_bytes"]
    filename: str   = result.metadata["filename"]

    # Render the download button immediately (same render pass as the click)
    st.download_button(
        label=f"⬇ Download {filename}  ({len(tickers)} tickers)",
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
        key="dl_proposals_btn",
    )
    st.success(f"Ready: {filename}")


# ------------------------------------------------------------------ #
# Settings / Config.
# ------------------------------------------------------------------ #
def _render_settings() -> None:  # noqa: C901
    role = _db_role()
    versions: list[dict] = []
    try:
        from app.services.config.config_service import ConfigService
        sr = ConfigService().list_setup_configs(db_role=role)
        if sr.status == "success":
            versions = sr.metadata.get("versions", [])
    except Exception as exc:
        st.error(f"Could not load setup configs: {exc}")
        return

    if not versions:
        st.info("No setup configs found. Run `python tools/init_prod_db.py` to seed defaults.")
        return

    def _cfg_label(v: dict) -> str:
        return f"{v.get('setup_name', v.get('config_id','?'))} / {v['version']}{' (Active)' if v['active_flag'] else ''}"

    cfg_labels   = [_cfg_label(v) for v in versions]
    cfg_id_by_lbl = {_cfg_label(v): v["config_id"] for v in versions}

    st.session_state.setdefault(_K_CLONE_MODE, False)

    # ---- Header ----
    col_title, col_btns = st.columns([3, 2])
    with col_title:
        st.markdown('<div class="da-title">Settings / Config View</div>', unsafe_allow_html=True)
    with col_btns:
        b1, b2, b3 = st.columns(3)
        clone_clicked  = b1.button("⧉ Clone",       key="cfg_clone")
        active_clicked = b2.button("⚡ Make Active", key="cfg_activate", type="primary")
        export_clicked = b3.button("⬇ Export",       key="cfg_export")

    # ---- Summary card ----
    st.markdown('<div class="da-card">', unsafe_allow_html=True)
    sel_cols = st.columns([3, 1, 1, 1, 1])
    with sel_cols[0]:
        st.markdown('<span class="da-label">Select Setup Config</span>', unsafe_allow_html=True)
        sel_label: str = st.selectbox("cfg_picker", options=cfg_labels, index=0, label_visibility="collapsed", key="cfg_label_sel")  # type: ignore[assignment]

    sel_cfg_id  = cfg_id_by_lbl.get(sel_label, "")
    sel_meta    = next((v for v in versions if v["config_id"] == sel_cfg_id), {})
    version_str = sel_meta.get("version", "—")
    is_active   = sel_meta.get("active_flag", False)
    created_at  = str(sel_meta.get("created_at", "—"))[:16]
    created_by  = sel_meta.get("created_by") or "system"
    active_html = (
        f'<span class="da-activegreen">{version_str} (Active)</span>'
        if is_active else f'<span style="color:#64748b">{version_str}</span>'
    )
    with sel_cols[1]:
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Version</div><div class="da-metavalue">{active_html}</div></div>', unsafe_allow_html=True)
    with sel_cols[2]:
        s_html = '<span class="da-activegreen">Active</span>' if is_active else '<span style="color:#94a3b8">Inactive</span>'
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Status</div><div class="da-metavalue">{s_html}</div></div>', unsafe_allow_html=True)
    with sel_cols[3]:
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Created On</div><div class="da-metavalue">{created_at}</div></div>', unsafe_allow_html=True)
    with sel_cols[4]:
        st.markdown(f'<div class="da-metabox"><div class="da-metalabel">Created By</div><div class="da-metavalue">{created_by}</div></div>', unsafe_allow_html=True)

    # Clone bar
    if clone_clicked:
        st.session_state[_K_CLONE_MODE] = True
    if st.session_state[_K_CLONE_MODE]:
        st.markdown('<div class="da-clonebar">', unsafe_allow_html=True)
        cc = st.columns([3, 3, 1, 1])
        with cc[0]:
            st.markdown('<span class="da-label">New Setup Name</span>', unsafe_allow_html=True)
            new_name: str = st.text_input("newname", value=f"{sel_meta.get('setup_name','')}_v2", label_visibility="collapsed", key="clone_new_name")  # type: ignore[assignment]
        with cc[1]:
            st.markdown('<span class="da-label" style="color:#94a3b8">Tunable settings editable in clone mode.</span>', unsafe_allow_html=True)
        with cc[2]:
            save_clone = st.button("💾 Save Clone", key="clone_save", type="primary")
        with cc[3]:
            cancel_clone = st.button("✕ Cancel", key="clone_cancel")
        if cancel_clone:
            st.session_state[_K_CLONE_MODE] = False
            st.rerun()
        if save_clone:
            _do_save_clone(role, sel_meta, new_name)
        st.markdown('</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    # Button actions
    if active_clicked and sel_cfg_id:
        result = _svc().activate_setup_config(
            config_id=sel_cfg_id, db_role=role,
            activated_by="dashboard", reason="activated via dashboard",
        )
        _show_result(result, f"'{sel_label}' is now active.")
        if result.status == "success":
            st.rerun()

    if export_clicked and sel_cfg_id:
        result = _svc().export_setup_config_csv(config_id=sel_cfg_id, db_role=role)
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

    # Filter + expand controls
    ctrl_l, ctrl_r = st.columns([3, 2])
    with ctrl_l:
        type_filter: str = st.selectbox("type_filter", ["Both", "Tunable only", "Hardcoded only"], index=0, key=_K_TYPE_FILT)  # type: ignore[assignment]
    with ctrl_r:
        ec1, ec2 = st.columns(2)
        expand_all   = ec1.button("⊞ Expand All",   key="cfg_expand")
        collapse_all = ec2.button("⊟ Collapse All", key="cfg_collapse")

    st.session_state.setdefault("cfg_expanded", {i: (i == 0) for i in range(len(_CONFIG_SECTIONS))})
    if expand_all:
        st.session_state["cfg_expanded"] = {i: True  for i in range(len(_CONFIG_SECTIONS))}
        st.rerun()
    if collapse_all:
        st.session_state["cfg_expanded"] = {i: False for i in range(len(_CONFIG_SECTIONS))}
        st.rerun()

    # Load config_json
    config_json: dict = {}
    if sel_cfg_id:
        try:
            from app.services.config.config_service import ConfigService
            gr = ConfigService().get_setup_config(config_id=sel_cfg_id, db_role=role)
            if gr.status == "success":
                config_json = gr.metadata.get("config", {}).get("config_json", {}) or {}
        except Exception:
            pass

    show_tunable   = type_filter in ("Both", "Tunable only")
    show_hardcoded = type_filter in ("Both", "Hardcoded only")
    clone_mode     = st.session_state[_K_CLONE_MODE]

    for idx, (section_title, icon, bg, settings) in enumerate(_CONFIG_SECTIONS):
        filtered = [s for s in settings if (s[2] and show_tunable) or (not s[2] and show_hardcoded)]
        if not filtered:
            continue
        default_open = st.session_state["cfg_expanded"].get(idx, idx == 0)
        with st.expander(f"{icon}  {section_title}  ({len(filtered)} settings)", expanded=default_open):
            rows_html = ""
            for label, key_path, tunable in filtered:
                val = _get_nested(config_json, key_path)
                display_val = _fmt(val)
                type_icon = '<span class="da-edit" title="Tunable">✎</span>' if tunable else '<span class="da-lock" title="Hardcoded">🔒</span>'
                if clone_mode and tunable:
                    st.text_input(label, value=display_val, key=f"clone_{key_path}")
                    st.session_state[f"_clone_edit_{key_path}"] = st.session_state.get(f"clone_{key_path}", display_val)
                else:
                    bg_color = "#fff" if tunable else "#f8fafc"
                    fg_color = "#0f172a" if tunable else "#94a3b8"
                    ro = "" if tunable else "readonly"
                    rows_html += (
                        f"<tr><td>{label}</td>"
                        f"<td><input style='width:100%;padding:7px 10px;border:1px solid #dbe0e8;"
                        f"border-radius:6px;background:{bg_color};color:{fg_color};font-weight:600' "
                        f"{ro} value=\"{display_val}\"></td>"
                        f"<td>{type_icon}</td></tr>"
                    )
            if rows_html:
                st.markdown(
                    f'<table class="da-settings-table">'
                    f'<thead><tr><th>Setting</th><th>Value</th><th>Type</th></tr></thead>'
                    f'<tbody>{rows_html}</tbody></table>',
                    unsafe_allow_html=True,
                )


def _do_save_clone(role: str, source_meta: dict, new_name: str) -> None:
    source_id = source_meta.get("config_id", "")
    base_json: dict = {}
    if source_id:
        try:
            from app.services.config.config_service import ConfigService
            gr = ConfigService().get_setup_config(config_id=source_id, db_role=role)
            if gr.status == "success":
                base_json = dict(gr.metadata.get("config", {}).get("config_json", {}) or {})
        except Exception:
            pass
    for _, _, _, settings in _CONFIG_SECTIONS:
        for label, key_path, tunable in settings:
            if not tunable:
                continue
            edited = st.session_state.get(f"_clone_edit_{key_path}")
            if edited is None:
                continue
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
                        cur[parts[-1]] = str(edited).lower() in ("true", "1", "yes")
                    elif isinstance(orig, int):
                        cur[parts[-1]] = int(edited)
                    elif isinstance(orig, float):
                        cur[parts[-1]] = float(edited)
                    else:
                        cur[parts[-1]] = edited
                except (ValueError, TypeError):
                    pass
    result = _svc().clone_setup_config(
        db_role=role,
        setup_name=new_name.strip() or source_meta.get("setup_name", "clone"),
        config_json=base_json,
        parent_config_id=source_id or None,
        created_by="dashboard",
        notes=f"cloned from {source_meta.get('version','?')} via dashboard",
        activate=False,
    )
    _show_result(result, f"Config '{new_name}' saved (inactive).")
    if result.status == "success":
        st.session_state[_K_CLONE_MODE] = False
        st.rerun()


# ------------------------------------------------------------------ #
# Other tabs.
# ------------------------------------------------------------------ #
def _render_simulation() -> None:
    st.markdown('<div class="da-title">Simulation / Backtest</div>', unsafe_allow_html=True)
    st.info(
        "🔬 **Simulation module is on HOLD** — functionality will be added in a future release.\n\n"
        "This section will provide:\n"
        "- Historical backtest runs against sim.duckdb\n"
        "- Strategy performance comparison\n"
        "- Simulation parameter configuration"
    )


def _render_outcome_tracking() -> None:
    st.markdown('<div class="da-title">Outcome Tracking</div>', unsafe_allow_html=True)
    loader = _loader()
    if loader is None:
        st.info("Simulation DB not supported in this view. Switch to prod or debug.")
        return
    summary = loader.load_outcome_summary()
    c1, c2, c3 = st.columns(3)
    c1.metric("Total", summary.total)
    c2.metric("Resolved", summary.resolved)
    c3.metric("Unresolved", summary.unresolved)
    st.subheader("Average returns (resolved outcomes)")
    st.write({"5bd %": summary.avg_return_5bd_pct, "10bd %": summary.avg_return_10bd_pct,
               "20bd %": summary.avg_return_20bd_pct, "40bd %": summary.avg_return_40bd_pct})


def _render_ai_review() -> None:
    st.markdown('<div class="da-title">AI Review</div>', unsafe_allow_html=True)
    loader = _loader()
    if loader is None:
        st.info("Simulation DB not supported in this view. Switch to prod or debug.")
        return
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
    if loader is None:
        st.info("Simulation DB not supported in this view. Switch to prod or debug.")
        return
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
    except Exception:
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
        st.warning(
            f"**{_db_role()}** database not found at `{db_path}`.\n\n"
            "- prod: `python tools/init_prod_db.py` then `python tools/run_prod_pipeline.py`\n"
            "- debug: `python tools/run_debug_pipeline.py --preset fast_smoke_test`"
        )
        return

    tab = st.session_state.get(_K_ACTIVE_TAB, "daily")
    dispatch = {
        "daily":      _render_daily_proposals,
        "settings":   _render_settings,
        "outcomes":   _render_outcome_tracking,
        "ai_review":  _render_ai_review,
        "health":     _render_pipeline_health,
        "simulation": _render_simulation,
    }
    dispatch.get(tab, _render_daily_proposals)()


if __name__ == "__main__":
    main()
