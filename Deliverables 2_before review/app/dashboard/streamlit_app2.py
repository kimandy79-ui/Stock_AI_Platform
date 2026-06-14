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

/* ---- Sidebar background ---- */
[data-testid="stSidebar"] > div:first-child {
  background: linear-gradient(180deg,#041c44,#08285d) !important;
}

/* ---- All sidebar text light ---- */
[data-testid="stSidebar"] * { color: #dbeafe !important; }

/* ---- Nav buttons: transparent bg, border-radius, hover shade ---- */
[data-testid="stSidebar"] .stButton > button {
  background: transparent !important;
  border: none !important;
  color: #cbd5e1 !important;
  text-align: left !important;
  padding: 10px 12px !important;
  border-radius: 10px !important;
  font-size: 14px !important;
  font-weight: 600 !important;
  width: 100% !important;
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
  display:inline-block; background:#fff; border:1px solid var(--border);
  border-radius:10px; padding:9px 12px; font-size:13px; font-weight:700; color:#475569;
}
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
        ("Strategy Name",             "strategy_name",                     False),
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

def _loader() -> DashboardDataLoader:
    return DashboardDataLoader(db_role=_db_role())

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

def _enrich_proposals(rows: list[dict], signal_date: date, db_role: str) -> list[dict]:
    """Add price/feature columns to proposal rows by querying daily_features + daily_prices."""
    if not rows:
        return rows
    tickers = list({r["ticker"] for r in rows if r.get("ticker")})
    if not tickers:
        return rows

    try:
        from app.database import duckdb_manager
        conn = duckdb_manager.connect(db_role, read_only=True)
        placeholders = ",".join(["?"] * len(tickers))
        sql = f"""
            SELECT
                f.ticker,
                f.ema20, f.ema50,
                ROUND((f.ema20 - f.ema50) / NULLIF(f.ema50, 0) * 100, 1) AS ema_spread_pct,
                ROUND(f.rsi14, 1)           AS rsi14,
                f.rvol20                    AS volume_ratio,
                ROUND(f.atr_pct * 100, 1)  AS atr_pct,
                ROUND(f.sector_relative_strength * 100, 1) AS rel_strength_pct,
                ROUND(p.close_adj, 2)       AS price,
                m.company_name
            FROM daily_features f
            LEFT JOIN daily_prices p
                ON p.ticker = f.ticker AND p.date = f.feature_date
            LEFT JOIN ticker_master m ON m.ticker = f.ticker
            WHERE f.feature_date = ?
              AND f.ticker IN ({placeholders})
        """
        params = [signal_date] + tickers
        try:
            cursor = conn.execute(sql, params)
            cols = [d[0] for d in cursor.description]
            feature_map = {
                row_data[0]: dict(zip(cols, row_data))
                for row_data in cursor.fetchall()
            }
        finally:
            conn.close()

        # Also try to get MACD histogram from daily_features if column exists
        # (column name varies by schema version)
        macd_map: dict[str, Any] = {}
        try:
            conn2 = duckdb_manager.connect(db_role, read_only=True)
            try:
                cols_df = conn2.execute("DESCRIBE daily_features").fetchall()
                feat_cols = [c[0] for c in cols_df]
                macd_col = next(
                    (c for c in feat_cols if "macd" in c.lower() and "hist" in c.lower()),
                    next((c for c in feat_cols if "macd" in c.lower()), None),
                )
                if macd_col:
                    cur2 = conn2.execute(
                        f"SELECT ticker, ROUND({macd_col}, 2) as macd_hist "
                        f"FROM daily_features WHERE feature_date = ? "
                        f"AND ticker IN ({placeholders})",
                        params,
                    )
                    for ticker_val, macd_val in cur2.fetchall():
                        macd_map[ticker_val] = macd_val
            finally:
                conn2.close()
        except Exception:
            pass

        enriched = []
        for r in rows:
            out = dict(r)
            feat = feature_map.get(r.get("ticker", ""), {})
            out["company_name"]    = feat.get("company_name") or ""
            out["price"]           = feat.get("price")
            out["ema_spread_pct"]  = feat.get("ema_spread_pct")
            out["rsi14"]           = feat.get("rsi14")
            out["volume_ratio"]    = feat.get("volume_ratio")
            out["atr_pct"]         = feat.get("atr_pct")
            out["rel_strength_pct"]= feat.get("rel_strength_pct")
            out["macd_hist"]       = macd_map.get(r.get("ticker", ""))
            enriched.append(out)
        return enriched

    except Exception:
        # If enrichment fails, return rows as-is (no crash)
        return rows


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
        ("⌂ Dashboard",          "daily"),    # reuse daily for now
        ("▣ Daily Proposals",    "daily"),
        ("⚙ Settings / Config",  "settings"),
        ("◉ Outcome Tracking",   "outcomes"),
        ("🧠 AI Review",          "ai_review"),
        ("⌁ Pipeline Health",    "health"),
    ]

    active = st.session_state[_K_ACTIVE_TAB]
    for label, key in nav_items:
        is_active = active == key and key != "daily" or (key == "daily" and label == "▣ Daily Proposals" and active == "daily")
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
        "db_role_sel", options=["prod", "debug"],
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
# Daily Proposals.
# ------------------------------------------------------------------ #
def _render_daily_proposals() -> None:  # noqa: C901
    loader = _loader()
    role   = _db_role()

    # ---- Header row ----
    col_title, col_right = st.columns([3, 2])
    with col_title:
        st.markdown('<div class="da-title">Daily Proposals</div>', unsafe_allow_html=True)
    with col_right:
        latest = loader.latest_pipeline_status()
        chip_label = "No runs yet"
        if latest:
            chip_label = f"Pipeline run: {latest.get('run_date','?')} {latest.get('status','')}"
        c1, c2, c3 = st.columns([3, 2, 2])
        c1.markdown(f'<div class="da-chip" style="margin-top:6px">{chip_label}</div>', unsafe_allow_html=True)
        run_clicked    = c2.button("▶ Run Pipeline", key="dp_run", type="primary")
        export_clicked = c3.button("⬇ Export",       key="dp_export")

    if run_clicked:
        _do_run_pipeline(role)

    if st.session_state.get(_K_PIPE_MSG):
        mtype, mtext = st.session_state[_K_PIPE_MSG]
        getattr(st, mtype)(mtext)

    # ---- Filter card 1: run date (calendar picker) ----
    with st.container():
        st.markdown('<div class="da-card">', unsafe_allow_html=True)
        date_col, _, _, _, _ = st.columns([2, 1, 1, 1, 1])
        with date_col:
            st.markdown('<span class="da-label">Run date</span>', unsafe_allow_html=True)
            # Calendar picker — default to most recent signal date available
            dates = loader.list_signal_dates()
            default_date = dates[0] if dates else date.today()
            signal_date: date = st.date_input(
                "run_date_picker",
                value=default_date,
                max_value=date.today(),
                label_visibility="collapsed",
                key="dp_date_picker",
            )  # type: ignore[assignment]
        st.markdown('</div>', unsafe_allow_html=True)

    # ---- Filter card 2: strategy + list view ----
    with st.container():
        st.markdown('<div class="da-card">', unsafe_allow_html=True)
        f1, f2, _, _ = st.columns(4)
        configs = loader.list_strategy_configs()
        with f1:
            st.markdown('<span class="da-label">Strategy</span>', unsafe_allow_html=True)
            cfg_choice: str = st.selectbox(
                "strategy_sel", options=["(all)"] + configs,
                index=0, label_visibility="collapsed", key="dp_strategy",
            )  # type: ignore[assignment]
        with f2:
            st.markdown('<span class="da-label">List View</span>', unsafe_allow_html=True)
            list_view: str = st.selectbox(
                "list_view_sel",
                ["Raw + Diversified", "Diversified only", "Raw only"],
                index=0, label_visibility="collapsed", key="dp_listview",
            )  # type: ignore[assignment]
        st.markdown('</div>', unsafe_allow_html=True)

    strategy_config_id = None if cfg_choice == "(all)" else cfg_choice
    show_div = list_view != "Raw only"

    view = loader.load_daily_proposals(
        signal_date=signal_date,
        strategy_config_id=strategy_config_id,
        show_diversified=show_div,
    )

    if not view.rows:
        st.info("No proposals for the selected date and strategy.")
        return

    display_rows, flags = data_access.build_proposals_display(view)

    # Enrich with price + feature columns
    enriched = _enrich_proposals(display_rows, signal_date, role)

    # ---- Proposal table with per-row checkboxes ----
    st.markdown('<div class="da-card">', unsafe_allow_html=True)
    st.markdown('<h3 style="margin:0 0 12px;font-size:17px;font-weight:800">Proposed Stocks</h3>', unsafe_allow_html=True)

    # Build display DataFrame for st.data_editor (checkbox column)
    table_rows = []
    for i, row in enumerate(enriched):
        rank = row.get("diversified_rank") if show_div else row.get("raw_rank")
        score = row.get("proposal_score_final") or row.get("proposal_score_raw")
        ema_spread = row.get("ema_spread_pct")
        macd = row.get("macd_hist")
        vol = row.get("volume_ratio")
        rel = row.get("rel_strength_pct")
        atr = row.get("atr_pct")

        table_rows.append({
            "✓":             True,
            "Rank":          int(rank) if rank is not None else i + 1,
            "Ticker":        row.get("ticker") or "",
            "Company":       row.get("company_name") or "",
            "Sector":        row.get("sector") or "",
            "Score":         round(float(score), 1) if score is not None else None,
            "RR":            round(float(row["estimated_rr"]), 2) if row.get("estimated_rr") is not None else None,
            "Signal":        row.get("setup_type") or "",
            "Price":         round(float(row["price"]), 2) if row.get("price") is not None else None,
            "EMA spread":    f"+{ema_spread:.1f}%" if ema_spread is not None and ema_spread >= 0 else (f"{ema_spread:.1f}%" if ema_spread is not None else "—"),
            "RSI14":         round(float(row["rsi14"]), 1) if row.get("rsi14") is not None else None,
            "MACD hist":     f"+{macd:.2f}" if macd is not None and macd >= 0 else (f"{macd:.2f}" if macd is not None else "—"),
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
            "✓": st.column_config.CheckboxColumn("✓", default=True, width="small"),
            "Rank":   st.column_config.NumberColumn("Rank",   width="small"),
            "Ticker": st.column_config.TextColumn("Ticker",  width="small"),
            "Score":  st.column_config.NumberColumn("Score",  format="%.1f", width="small"),
            "RR":     st.column_config.NumberColumn("RR",     format="%.2f", width="small"),
            "Price":  st.column_config.NumberColumn("Price",  format="%.2f", width="small"),
            "RSI14":  st.column_config.NumberColumn("RSI14",  format="%.1f", width="small"),
        },
        key="proposal_editor",
    )

    # Tickers where checkbox is checked
    checked_tickers: list[str] = list(
        edited_df.loc[edited_df["✓"] == True, "Ticker"]  # noqa: E712
    )

    st.markdown('</div>', unsafe_allow_html=True)

    # ---- Indicator cards for first checked ticker ----
    if checked_tickers:
        sel_ticker = checked_tickers[0]
        sel_row = next(
            (r for r in enriched if r.get("ticker") == sel_ticker), None
        )
        if sel_row:
            _render_indicator_cards(sel_row)

    # ---- Export ----
    if export_clicked:
        _do_export_proposals(role, loader, view, checked_tickers or list(df_display["Ticker"]), strategy_config_id)


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
        ("MACD Hist",
         _fmt_num(row.get("macd_hist"), 2),
         "Positive histogram = expanding bullish momentum."),
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


def _do_run_pipeline(role: str) -> None:
    with st.spinner("Running pipeline…"):
        result = _svc().run_pipeline(
            db_role=role,
            run_date=date.today(),
            run_type="manual",
        )
    if result.status == "success":
        st.session_state[_K_PIPE_MSG] = ("success", f"Pipeline completed. run_id={result.run_id}")
    elif result.status == "success_with_warnings":
        st.session_state[_K_PIPE_MSG] = ("warning", "Completed with warnings: " + "; ".join(result.warnings or []))
    else:
        st.session_state[_K_PIPE_MSG] = ("error", "Pipeline failed: " + "; ".join(result.errors or ["unknown error"]))
    st.rerun()


def _do_export_proposals(
    role: str,
    loader: DashboardDataLoader,
    view: data_access.ProposalsView,
    tickers: list[str],
    strategy_config_id: str | None,
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
        strategy_config_id=strategy_config_id,
    )
    if not proposal_ids:
        st.warning("Could not resolve proposal IDs for the selected tickers.")
        return

    result = _svc().export_proposals_csv(
        signal_date=view.signal_date,
        proposal_ids=proposal_ids,
        db_role=role,
        strategy_config_id=strategy_config_id,
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
        sr = ConfigService().list_strategy_configs(db_role=role)
        if sr.status == "success":
            versions = sr.metadata.get("versions", [])
    except Exception as exc:
        st.error(f"Could not load strategy configs: {exc}")
        return

    if not versions:
        st.info("No strategy configs found. Run `python tools/init_prod_db.py` to seed defaults.")
        return

    def _cfg_label(v: dict) -> str:
        return f"{v['strategy_name']} / {v['version']}{' (Active)' if v['active_flag'] else ''}"

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
        st.markdown('<span class="da-label">Select Strategy (Configuration)</span>', unsafe_allow_html=True)
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
            st.markdown('<span class="da-label">New Strategy Name</span>', unsafe_allow_html=True)
            new_name: str = st.text_input("newname", value=f"{sel_meta.get('strategy_name','')}_v2", label_visibility="collapsed", key="clone_new_name")  # type: ignore[assignment]
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
        result = _svc().activate_strategy_config(
            config_id=sel_cfg_id, db_role=role,
            activated_by="dashboard", reason="activated via dashboard",
        )
        _show_result(result, f"'{sel_label}' is now active.")
        if result.status == "success":
            st.rerun()

    if export_clicked and sel_cfg_id:
        result = _svc().export_strategy_config_csv(config_id=sel_cfg_id, db_role=role)
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
            gr = ConfigService().get_strategy_config(config_id=sel_cfg_id, db_role=role)
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
            gr = ConfigService().get_strategy_config(config_id=source_id, db_role=role)
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
    result = _svc().clone_strategy_config(
        db_role=role,
        strategy_name=new_name.strip() or source_meta.get("strategy_name", "clone"),
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
def _render_outcome_tracking() -> None:
    st.markdown('<div class="da-title">Outcome Tracking</div>', unsafe_allow_html=True)
    loader = _loader()
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
    try:
        from app.database import duckdb_manager
        p = duckdb_manager.get_database_path(_db_role())
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
        "daily":    _render_daily_proposals,
        "settings": _render_settings,
        "outcomes": _render_outcome_tracking,
        "ai_review":_render_ai_review,
        "health":   _render_pipeline_health,
    }
    dispatch.get(tab, _render_daily_proposals)()


if __name__ == "__main__":
    main()
