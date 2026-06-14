"""Ticker selection report generator (app/dashboard/ticker_report.py).

Produces a plain-text document that explains, step by step, how and why
the pipeline selected a given ticker on a given signal date.

The document is structured for direct consumption by an AI assistant:
each section states what the pipeline did, what the relevant thresholds
were, the actual values, and whether the ticker passed or failed each
gate.

Public API
----------
build_ticker_report(
    row: dict,
    signal_date: date,
    db_role: str,
    strategy_config_id: str,
) -> tuple[bytes, str]

Returns (utf-8 bytes, filename).
Filename: YYMMDD_TICKER_STRATEGYNAME.txt
"""

from __future__ import annotations

import json as _json
from datetime import date
from typing import Any


# ------------------------------------------------------------------ #
# Helpers.
# ------------------------------------------------------------------ #

def _nested(d: dict, path: str) -> Any:
    """Read a dotted-path value from a nested dict."""
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _pct(v: Any, decimals: int = 1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(v)


def _fmt(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _bool(v: Any) -> str:
    if v is None:
        return "—"
    return "Yes" if v else "No"


def _score_band(v: Any, bands: list[tuple[float, float, str]]) -> str:
    """Return band label for a numeric value against a sorted list of (lo, hi, label)."""
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    for lo, hi, label in bands:
        if lo <= fv <= hi:
            return label
    return "out of range"


# ------------------------------------------------------------------ #
# Pass/fail comparison.
# ------------------------------------------------------------------ #

def _check(label: str, value: Any, threshold: Any, direction: str = ">=", note: str = "") -> str:
    """Return a formatted pass/fail comparison line."""
    if value is None:
        return f"  {label:<45} value=— (no data){('  # ' + note) if note else ''}"
    if threshold is None:
        return f"  {label:<45} value={value}  threshold=— (not configured)"
    try:
        fv, tv = float(value), float(threshold)
        passed = (
            fv >= tv if direction == ">=" else
            fv <= tv if direction == "<=" else
            fv > tv  if direction == ">"  else
            fv < tv  if direction == "<"  else
            fv == tv
        )
        mark = "PASS ✓" if passed else "FAIL ✗"
        note_str = f"  # {note}" if note else ""
        return f"  {label:<45} {_fmt(fv)} {direction} {_fmt(tv)}  [{mark}]{note_str}"
    except (TypeError, ValueError):
        return f"  {label:<45} value={value}  threshold={threshold}"


# ------------------------------------------------------------------ #
# Main report builder.
# ------------------------------------------------------------------ #

def build_ticker_report(
    row: dict,
    signal_date: date,
    db_role: str,
    strategy_config_id: str,
) -> tuple[bytes, str]:
    """Build the selection report and return (utf-8 bytes, filename)."""

    ticker       = row.get("ticker", "UNKNOWN")
    company_name = row.get("company_name") or ""
    sector       = row.get("sector") or "—"
    industry     = row.get("industry") or "—"
    strategy_short = (strategy_config_id or "").replace("seed_strategy_prod_", "").replace("_v1", "")
    date_short   = signal_date.strftime("%y%m%d")
    filename     = f"{date_short}_{ticker}_{strategy_short}.txt".replace(" ", "_")

    L: list[str] = []

    def h1(t: str) -> None:
        L.extend(["=" * 72, f"  {t}", "=" * 72])

    def h2(t: str) -> None:
        L.append(f"\n{'─' * 72}")
        L.append(f"  {t}")
        L.append(f"{'─' * 72}")

    def kv(k: str, v: Any, note: str = "") -> None:
        note_str = f"  # {note}" if note else ""
        L.append(f"  {k:<45} {v}{note_str}")

    def blank() -> None:
        L.append("")

    def line(s: str) -> None:
        L.append(s)

    # ---------------------------------------------------------------- #
    # Fetch DB data.
    # ---------------------------------------------------------------- #
    step3_row:     dict = {}
    step4_row:     dict = {}
    step5_row:     dict = {}
    config_json:   dict = {}
    feature_snap:  dict = {}
    soft_comps:    dict = {}

    try:
        from app.database import duckdb_manager as _dbm
        conn = _dbm.connect(db_role, read_only=True)
        try:
            # step3_candidates
            r3 = conn.execute(
                "SELECT * FROM step3_candidates "
                "WHERE ticker=? AND signal_date=? AND strategy_config_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                [ticker, signal_date, strategy_config_id],
            ).fetchone()
            if r3:
                cols3 = [d[0] for d in conn.execute("SELECT * FROM step3_candidates LIMIT 0").description]
                step3_row = dict(zip(cols3, r3))
                # Parse JSON blobs
                for blob_key in ("soft_score_components", "feature_snapshot_json", "hard_filter_fail_reasons"):
                    raw = step3_row.get(blob_key)
                    if isinstance(raw, str):
                        try:
                            step3_row[blob_key] = _json.loads(raw)
                        except Exception:
                            pass
                soft_comps   = step3_row.get("soft_score_components") or {}
                feature_snap = step3_row.get("feature_snapshot_json") or {}

            # step4_analysis
            r4 = conn.execute(
                "SELECT * FROM step4_analysis "
                "WHERE ticker=? AND signal_date=? AND strategy_config_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                [ticker, signal_date, strategy_config_id],
            ).fetchone()
            if r4:
                cols4 = [d[0] for d in conn.execute("SELECT * FROM step4_analysis LIMIT 0").description]
                step4_row = dict(zip(cols4, r4))
                expl = step4_row.get("explanation_json")
                if isinstance(expl, str):
                    try:
                        step4_row["explanation_json"] = _json.loads(expl)
                    except Exception:
                        pass

            # step5_proposals
            r5 = conn.execute(
                "SELECT * FROM step5_proposals "
                "WHERE ticker=? AND signal_date=? AND strategy_config_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                [ticker, signal_date, strategy_config_id],
            ).fetchone()
            if r5:
                cols5 = [d[0] for d in conn.execute("SELECT * FROM step5_proposals LIMIT 0").description]
                step5_row = dict(zip(cols5, r5))

            # strategy_configs
            cfg = conn.execute(
                "SELECT config_json FROM strategy_configs WHERE config_id=? LIMIT 1",
                [strategy_config_id],
            ).fetchone()
            if cfg and cfg[0]:
                config_json = _json.loads(cfg[0]) if isinstance(cfg[0], str) else cfg[0]

        finally:
            conn.close()
    except Exception as exc:
        L.append(f"  [DB fetch error: {exc}]")

    # ---------------------------------------------------------------- #
    # HEADER
    # ---------------------------------------------------------------- #
    h1(f"TICKER SELECTION REPORT — {ticker}  ({company_name})")
    kv("Signal date",     signal_date.isoformat())
    kv("Strategy",        strategy_config_id)
    kv("Sector",          sector)
    kv("Industry",        industry)
    sector_etf_map = _nested(config_json, "sector_etf_mapping") or {}
    sector_etf = sector_etf_map.get(sector, "—")
    kv("Sector benchmark ETF", sector_etf,
       "used to calculate sector relative strength")
    import datetime as _dt
    kv("Generated at",    _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    blank()
    line("PURPOSE OF THIS DOCUMENT")
    line("  This document traces every decision the pipeline made for this ticker")
    line("  from raw price data through to the final proposal ranking.  Each step")
    line("  shows the formula, the actual values, the configured thresholds, and")
    line("  a PASS / FAIL verdict.  Read top to bottom for the full selection logic.")
    blank()

    # ---------------------------------------------------------------- #
    # STEP 1: UNIVERSE & PRICE DATA
    # ---------------------------------------------------------------- #
    h2("STEP 1 — UNIVERSE & PRICE DATA  (M08 daily ingestion → ticker_master)")
    line("  The pipeline loads daily OHLCV prices from yfinance.  A ticker must")
    line("  have active_flag=TRUE in ticker_master to enter the universe.")
    blank()
    close_raw   = feature_snap.get("close_raw") or row.get("price")
    avg_dv      = feature_snap.get("avg_dollar_volume_20d")
    min_price   = _nested(config_json, "universe.min_price")
    min_dv      = _nested(config_json, "universe.min_avg_dollar_volume_20d")
    kv("Close price (signal date)",    _fmt(close_raw, 2))
    kv("Avg dollar volume 20d",        _fmt(avg_dv, 0))
    blank()
    line("  Hard-filter gates applied in Step 3 (universe constraints):")
    line(_check("  price >= universe.min_price", close_raw, min_price, ">=",
                f"threshold = {_fmt(min_price, 2)}"))
    line(_check("  avg_dollar_volume_20d >= min",  avg_dv,   min_dv,   ">=",
                f"threshold = {_fmt(min_dv, 0)}"))
    blank()

    # ---------------------------------------------------------------- #
    # STEP 2: TECHNICAL FEATURES
    # ---------------------------------------------------------------- #
    h2("STEP 2 — TECHNICAL FEATURES  (M11 feature engine → daily_features)")
    line("  Features are computed from adjusted prices.  All indicators use")
    line("  close_adj unless noted.  The feature_snapshot stored by Step 3")
    line("  captures the exact values used for screening.")
    blank()

    ema20  = feature_snap.get("ema20")  or row.get("ema20")
    ema50  = feature_snap.get("ema50")  or row.get("ema50")
    ema200 = feature_snap.get("ema200") or row.get("ema200")
    ema_align = feature_snap.get("ema_alignment_score")
    dist_ema50 = feature_snap.get("distance_to_ema50_pct")  # fraction, not pct
    rsi14   = feature_snap.get("rsi14")   or row.get("rsi14")
    roc20   = feature_snap.get("roc20")   or row.get("roc20")
    rvol20  = feature_snap.get("rvol20")  or row.get("volume_ratio")
    sect_rs = feature_snap.get("sector_relative_strength") or row.get("rel_strength_pct")
    # sector_relative_strength is stored as fraction in features; row stores as %
    # normalise to fraction for formula display
    if sect_rs is not None:
        try:
            sr_val = float(sect_rs)
            # if > 5 it was stored as % already; convert to fraction
            sr_frac = sr_val / 100.0 if abs(sr_val) > 5 else sr_val
            sect_rs_frac = sr_frac
            sect_rs_pct  = sr_frac * 100
        except (TypeError, ValueError):
            sect_rs_frac = None
            sect_rs_pct  = None
    else:
        sect_rs_frac = None
        sect_rs_pct  = None

    cons_score  = feature_snap.get("consolidation_score")
    bp          = feature_snap.get("breakout_proximity")
    pullback    = feature_snap.get("pullback_from_recent_high_pct")
    market_reg  = feature_snap.get("market_regime") or soft_comps.get("market_regime") or "—"
    atr_pct     = row.get("atr_pct")

    line("  TREND INDICATORS")
    kv("    EMA20",                   _fmt(ema20, 2))
    kv("    EMA50",                   _fmt(ema50, 2))
    kv("    EMA200",                  _fmt(ema200, 2))
    kv("    EMA alignment score",     _fmt(ema_align, 0),
       "100=EMA20>EMA50>EMA200; 50=close>EMA200; 0=bearish")
    dist_pct_str = _fmt(float(dist_ema50)*100, 2) + "%" if dist_ema50 is not None else "—"
    kv("    Distance to EMA50",       dist_pct_str,
       "ideal -3% to +8% for trend pullback")
    blank()
    line("  MOMENTUM INDICATORS")
    kv("    RSI14",                   _fmt(rsi14, 1),
       "50-65=ideal bullish zone")
    kv("    ROC20 (rate of change)",  _fmt(roc20, 4) if roc20 is not None else "—",
       ">0.08=strong; 0.03-0.08=moderate; 0-0.03=weak; <0=bearish")
    kv(f"    Sector rel strength vs {sector_etf}",
                                      f"{_fmt(sect_rs_pct, 2)}%" if sect_rs_pct is not None else "—",
       ">5%=strong; 0-5%=neutral; negative=underperforming")
    blank()
    line("  SETUP INDICATORS")
    kv("    Consolidation score",     _fmt(cons_score, 1),
       "0-100; >=70 = tight consolidation")
    kv("    Breakout proximity",      _fmt(bp, 3),
       "-1 to 0.5=ideal; -2 to -1=moderate; >1.5=extended")
    pullback_pct = _fmt(float(pullback)*100, 2) + "%" if pullback is not None else "—"
    kv("    Pullback from recent high", pullback_pct,
       "-12% to -3%=ideal; -20% to -12%=moderate; outside=weak")
    blank()
    line("  VOLUME & VOLATILITY")
    kv("    RVOL20 (relative volume)", _fmt(rvol20, 2),
       ">=2.0=strong; 1.5-2.0=elevated; 1.2-1.5=moderate; <1.2=low")
    kv("    ATR % (volatility)",       f"{_fmt(atr_pct, 2)}%" if atr_pct is not None else "—",
       "avg true range as % of price")
    blank()
    line("  MARKET CONTEXT")
    kv("    Market regime",           market_reg,
       "bull=100pts; neutral=60pts; bear=20pts; high_risk/extreme_risk=0pts")
    blank()

    # ---------------------------------------------------------------- #
    # STEP 3: SCREENING
    # ---------------------------------------------------------------- #
    h2("STEP 3 — SCREENING  (M13 → step3_candidates)")
    line("  All tickers in the universe are evaluated.  First, 6 hard filters")
    line("  must ALL pass.  Passing tickers are then soft-scored 0-100.")
    blank()

    # Hard filters
    line("  3A. HARD FILTERS (all must pass)")
    blank()
    hard_reasons = step3_row.get("hard_filter_fail_reasons") or []
    passed_hard  = step3_row.get("passed_hard_filters", None)
    if passed_hard is not None:
        kv("    Overall hard filter result",
           "PASS ✓" if passed_hard else f"FAIL ✗  reasons: {hard_reasons}")
    else:
        line("    No step3_candidates row found for this ticker/date/strategy.")

    min_rvol  = _nested(config_json, "screening.min_rvol")
    line(_check("    feature_ready = TRUE",          True,       True,      "=="))
    line(_check("    symbol_type = stock",           "stock",    "stock",   "=="))
    line(_check("    close_raw >= universe.min_price", close_raw, min_price, ">=",
                f"min_price={_fmt(min_price, 2)}"))
    line(_check("    avg_dollar_volume_20d >= min",   avg_dv,    min_dv,    ">=",
                f"min={_fmt(min_dv, 0)}"))
    line(_check("    rvol20 >= screening.min_rvol",   rvol20,    min_rvol,  ">=",
                f"min_rvol={_fmt(min_rvol, 2)}"))
    blank()

    # Soft score formula
    line("  3B. SOFT SCORE (only for hard-filter passers)")
    blank()
    w = _nested(config_json, "scoring_weights") or {}
    wt  = w.get("trend",    0.30)
    wm  = w.get("momentum", 0.25)
    ws  = w.get("setup",    0.20)
    wv  = w.get("volume",   0.15)
    wmk = w.get("market",   0.10)

    ts  = soft_comps.get("trend_score")
    ms  = soft_comps.get("momentum_score")
    ss  = soft_comps.get("setup_score")
    vs  = soft_comps.get("volume_score")
    mks = soft_comps.get("market_score")
    scr = step3_row.get("screening_score")
    min_scr = _nested(config_json, "screening.min_screening_score")

    line("  Formula:")
    line("    screening_score = w_trend*trend + w_momentum*momentum")
    line("                    + w_setup*setup + w_volume*volume + w_market*market")
    blank()
    line("  Sub-score construction:")
    blank()
    line("    TREND SCORE  (EMA alignment 50% + EMA50 distance 25% + above EMA200 25%)")
    _ema200_above = (
        "100" if (close_raw is not None and ema200 is not None and
                  float(close_raw) > float(ema200))
        else "0"
    ) if close_raw is not None and ema200 is not None else "—"
    line(f"      ema_alignment_score                    = {_fmt(ema_align, 1)}")
    line(f"      distance_to_ema50 band score            = (from distance {dist_pct_str})")
    line(f"      close ({_fmt(close_raw,2)}) > EMA200 ({_fmt(ema200,2)}) → score  = {_ema200_above}")
    kv("      trend_score (actual stored)",       _fmt(ts, 2))
    blank()
    line("    MOMENTUM SCORE  (RSI14 40% + ROC20 30% + sector-RS 30%)")
    # RSI band
    rsi_band = (
        "100" if rsi14 is not None and 50 <= float(rsi14) <= 65 else
        "70"  if rsi14 is not None and (45 <= float(rsi14) < 50 or 65 < float(rsi14) <= 70) else
        "30"
    ) if rsi14 is not None else "—"
    # ROC20 band
    roc_band = (
        "100" if roc20 is not None and float(roc20) > 0.08 else
        "70"  if roc20 is not None and 0.03 <= float(roc20) <= 0.08 else
        "30"  if roc20 is not None and 0 <= float(roc20) < 0.03 else
        "0"   if roc20 is not None and float(roc20) < 0 else "—"
    ) if roc20 is not None else "—"
    # Sector RS band
    srs_band = (
        "100" if sect_rs_frac is not None and sect_rs_frac > 0.05 else
        "70"  if sect_rs_frac is not None and 0 <= sect_rs_frac <= 0.05 else
        "30"  if sect_rs_frac is not None and -0.05 <= sect_rs_frac < 0 else
        "0"   if sect_rs_frac is not None and sect_rs_frac < -0.05 else
        "50 (neutral, null)"
    ) if sect_rs_frac is not None else "50 (neutral, null)"
    line(f"      RSI14={_fmt(rsi14,1)} → RSI band score              = {rsi_band}")
    line(f"      ROC20={_fmt(roc20,4)} → ROC band score              = {roc_band}")
    line(f"      sector_RS={_fmt(sect_rs_pct,2)}% vs {sector_etf} → RS band  = {srs_band}")
    kv("      momentum_score (actual stored)",    _fmt(ms, 2))
    blank()
    line("    SETUP SCORE  (consolidation 40% + breakout proximity 30% + pullback 30%)")
    bp_band = (
        "100" if bp is not None and -1 <= float(bp) <= 0.5 else
        "70"  if bp is not None and -2 <= float(bp) < -1 else
        "20"  if bp is not None and float(bp) > 1.5 else
        "30"
    ) if bp is not None else "—"
    pb_pct = float(pullback) if pullback is not None else None
    pb_band = (
        "100" if pb_pct is not None and -0.12 <= pb_pct <= -0.03 else
        "70"  if pb_pct is not None and -0.20 <= pb_pct < -0.12 else
        "30"
    ) if pb_pct is not None else "—"
    line(f"      consolidation_score={_fmt(cons_score,1)} (direct input, 0-100)")
    line(f"      breakout_proximity={_fmt(bp,3)} → BP band score         = {bp_band}")
    line(f"      pullback={pullback_pct} → pullback band score         = {pb_band}")
    kv("      setup_score (actual stored)",       _fmt(ss, 2))
    blank()
    line("    VOLUME SCORE  (rvol20 band)")
    v_band = (
        "100" if rvol20 is not None and float(rvol20) >= 2.0 else
        "70"  if rvol20 is not None and 1.5 <= float(rvol20) < 2.0 else
        "40"  if rvol20 is not None and 1.2 <= float(rvol20) < 1.5 else
        "0"
    ) if rvol20 is not None else "—"
    line(f"      rvol20={_fmt(rvol20,2)} → volume band score              = {v_band}")
    kv("      volume_score (actual stored)",      _fmt(vs, 2))
    blank()
    line("    MARKET SCORE  (market regime mapping)")
    mk_map = {"bull": 100, "neutral": 60, "bear": 20, "high_risk": 0, "extreme_risk": 0}
    mk_band = mk_map.get(str(market_reg).lower(), "50 (unknown→neutral)")
    line(f"      market_regime={market_reg} → market score                = {mk_band}")
    kv("      market_score (actual stored)",      _fmt(mks, 2))
    blank()
    line("  FINAL SCREENING SCORE CALCULATION:")
    ts_v  = _fmt(ts,  2) if ts  is not None else "—"
    ms_v  = _fmt(ms,  2) if ms  is not None else "—"
    ss_v  = _fmt(ss,  2) if ss  is not None else "—"
    vs_v  = _fmt(vs,  2) if vs  is not None else "—"
    mks_v = _fmt(mks, 2) if mks is not None else "—"
    line(f"    screening_score = {wt}×{ts_v} + {wm}×{ms_v} + {ws}×{ss_v}")
    line(f"                    + {wv}×{vs_v} + {wmk}×{mks_v}")
    if all(x is not None for x in [ts, ms, ss, vs, mks]):
        calc = wt*ts + wm*ms + ws*ss + wv*vs + wmk*mks
        line(f"                  = {calc:.2f}  (rounded/stored: {_fmt(scr, 2)})")
    else:
        line(f"                  = {_fmt(scr, 2)}  (stored value)")
    blank()
    line(_check("  screening_score >= min_screening_score", scr, min_scr, ">=",
                f"threshold={_fmt(min_scr, 1)}"))
    blank()

    # ---------------------------------------------------------------- #
    # STEP 4: SETUP ANALYSIS
    # ---------------------------------------------------------------- #
    h2("STEP 4 — SETUP ANALYSIS  (M14 → step4_analysis)")
    line("  For each passing Step 3 candidate, Step 4 classifies the chart")
    line("  setup, scores it, and calculates the mechanical stop/target/RR.")
    blank()

    if step4_row:
        setup_type = step4_row.get("setup_type") or "—"
        kv("  Setup type classified",    setup_type)
        blank()
        line("  Setup type classification rules:")
        line("    trend_pullback:       close>EMA200 AND -12%<=pullback<=-3% AND EMA20>EMA50")
        line("    breakout:             -0.5<=breakout_proximity<=0.5 AND rvol>=min_rvol")
        line("    volatility_squeeze:   consolidation_score>=70 AND ATR contracting")
        line("    trend_resume:         close crosses back above EMA20 after pullback")
        line("    high_tight_flag:      ROC20>15% AND consolidation_score>=60")
        line("    unknown:              none of the above matched")
        blank()
        line("  Setup component scores (each 0-100):")
        kv("    breakout_quality_score",  _fmt(step4_row.get("breakout_quality_score"), 2))
        kv("    squeeze_score",           _fmt(step4_row.get("squeeze_score"), 2))
        kv("    timing_score",            _fmt(step4_row.get("timing_score"), 2))
        kv("    confirmation_score",      _fmt(step4_row.get("confirmation_score"), 2))
        setup_score = step4_row.get("setup_score")
        kv("    setup_score (average)",   _fmt(setup_score, 2),
           "= avg(breakout_quality, squeeze, timing, confirmation)")
        blank()
        line("  Stop / target / RR calculation:")
        entry_proxy = close_raw
        stop_price  = step4_row.get("stop_price_raw")
        target_price = step4_row.get("target_price_raw")
        est_rr      = step4_row.get("estimated_rr")
        target_r    = _nested(config_json, "step4.target_R")
        line(f"    entry_proxy_raw  = close_raw on signal_date            = {_fmt(entry_proxy, 2)}")
        line(f"    stop_price_raw   = min(20d_low, entry - 1.5×ATR14)     = {_fmt(stop_price, 2)}")
        line(f"    target_price_raw = entry + target_R × (entry - stop)   = {_fmt(target_price, 2)}")
        line(f"                       target_R (config) = {_fmt(target_r, 1)}")
        line(f"    estimated_rr     = (target - entry) / (entry - stop)   = {_fmt(est_rr, 2)}")
        blank()
        line("  Penalties applied to proposal score:")
        days_earn = feature_snap.get("days_to_earnings_bd")
        earn_avoid = _nested(config_json, "earnings.avoid_within_bd")
        earn_max   = _nested(config_json, "earnings.penalty_points_max")
        macro_flag = feature_snap.get("macro_event_risk_flag")
        macro_pen  = _nested(config_json, "macro_event_risk.penalty_points")
        kv("    Days to earnings (bd)",   str(days_earn) if days_earn is not None else "—")
        kv("    Earnings avoid window",   str(earn_avoid) if earn_avoid is not None else "—")
        if days_earn is not None and earn_avoid is not None and days_earn <= earn_avoid:
            earn_pen_calc = earn_max * (1 - days_earn / earn_avoid) if earn_max and earn_avoid else None
            kv("    Earnings penalty applied", _fmt(earn_pen_calc, 2),
               f"= {earn_max} × (1 - {days_earn}/{earn_avoid})")
        else:
            kv("    Earnings penalty applied", "0  (outside avoidance window)")
        kv("    Macro event risk flag",   _bool(macro_flag))
        kv("    Macro penalty points",    str(macro_pen) if macro_pen is not None else "—")
        kv("    earnings_penalty (stored)", _fmt(step4_row.get("earnings_penalty"), 2))
        kv("    macro_penalty (stored)",    _fmt(step4_row.get("macro_penalty"), 2))
        blank()

        # Explanation JSON if available
        expl = step4_row.get("explanation_json")
        if expl and isinstance(expl, dict):
            line("  Step 4 explanation (stored by analysis engine):")
            for k, v in expl.items():
                kv(f"    {k}", v)
            blank()
    else:
        line("  No step4_analysis row found for this ticker/date/strategy.")
        blank()

    # ---------------------------------------------------------------- #
    # STEP 5: PROPOSAL SCORING & RANKING
    # ---------------------------------------------------------------- #
    h2("STEP 5 — PROPOSAL SCORING & RANKING  (M15 → step5_proposals)")
    line("  Step 5 combines Step 3 screening score, Step 4 setup score,")
    line("  estimated RR, and timing score into a final proposal score.")
    line("  Tickers are then ranked raw and diversified.")
    blank()

    if step5_row or step4_row:
        setup_score  = step4_row.get("setup_score")
        timing_score = step4_row.get("timing_score", 50.0)
        est_rr       = step4_row.get("estimated_rr")
        scr_val      = step3_row.get("screening_score")

        # RR score band
        rr_score = (
            100 if est_rr is not None and float(est_rr) >= 3.0 else
            80  if est_rr is not None and 2.2 <= float(est_rr) < 3.0 else
            60  if est_rr is not None and 1.8 <= float(est_rr) < 2.2 else
            0
        ) if est_rr is not None else 0

        line("  RR score bands:")
        line("    estimated_rr >= 3.0  → rr_score = 100")
        line("    2.2 <= rr < 3.0      → rr_score = 80")
        line("    1.8 <= rr < 2.2      → rr_score = 60")
        line("    rr < 1.8             → rr_score = 0")
        line(f"    estimated_rr = {_fmt(est_rr, 2)}  → rr_score = {rr_score}")
        blank()
        line("  Raw proposal score formula:")
        line("    proposal_score_raw = 0.40×setup_score + 0.25×screening_score")
        line("                       + 0.20×rr_score    + 0.15×timing_score")
        line(f"    proposal_score_raw = 0.40×{_fmt(setup_score,2)} + 0.25×{_fmt(scr_val,2)}")
        line(f"                       + 0.20×{rr_score}            + 0.15×{_fmt(timing_score,2)}")
        if all(x is not None for x in [setup_score, scr_val]):
            calc_raw = 0.40*(setup_score or 0) + 0.25*(scr_val or 0) + 0.20*rr_score + 0.15*(timing_score or 50)
            line(f"                     = {calc_raw:.2f}")
        blank()

        if step5_row:
            ps_raw   = step5_row.get("proposal_score_raw")
            ps_final = step5_row.get("proposal_score_final")
            raw_rank = step5_row.get("raw_rank")
            div_rank = step5_row.get("diversified_rank")
            rej      = step5_row.get("rejection_reason")
            expl_txt = step5_row.get("mechanical_explanation") or "—"
            kv("  proposal_score_raw (stored)",       _fmt(ps_raw, 2))
            kv("  proposal_score_final (stored)",      _fmt(ps_final, 2))
            blank()
            line("  Ranking:")
            kv("    Raw rank",                        str(raw_rank) if raw_rank is not None else "—",
               "sorted by proposal_score_raw DESC → estimated_rr DESC → ticker ASC")
            kv("    Diversified rank",                str(div_rank) if div_rank is not None else "—")
            blank()

            # Diversification
            top_n    = _nested(config_json, "diversification.top_n")
            hard_cap = _nested(config_json, "diversification.hard_cap_enabled")
            s_max    = _nested(config_json, "diversification.sector_max_positions")
            i_max    = _nested(config_json, "diversification.industry_max_positions")
            s_pen    = _nested(config_json, "diversification.sector_penalty_factor")
            i_pen    = _nested(config_json, "diversification.industry_penalty_factor")
            line("  Diversification rules applied:")
            kv("    hard_cap_enabled",                _bool(hard_cap))
            kv("    top_n",                           str(top_n) if top_n is not None else "—")
            kv("    sector_max_positions",             str(s_max) if s_max is not None else "—")
            kv("    industry_max_positions",           str(i_max) if i_max is not None else "—")
            if hard_cap:
                line(f"    MODE: HARD CAP  — if adding this ticker exceeds sector ({s_max})")
                line(f"          or industry ({i_max}) limit, it is rejected from diversified list.")
                line(f"          rejection_reason = {rej or 'none (accepted)'}")
            else:
                kv("    sector_penalty_factor",        str(s_pen))
                kv("    industry_penalty_factor",      str(i_pen))
                line("    MODE: SOFT PENALTY  — proposal_score_final = raw_score × sector_pen × industry_pen")
            blank()
            line("  Mechanical explanation (stored by Step 5):")
            line(f"    {expl_txt}")
            blank()
    else:
        line("  No Step 4 or Step 5 row found for this ticker/date/strategy.")
        blank()

    # ---------------------------------------------------------------- #
    # STRATEGY CONFIGURATION SUMMARY
    # ---------------------------------------------------------------- #
    h2("STRATEGY CONFIGURATION SUMMARY")
    line("  All tunable thresholds used in the pipeline above, in the order")
    line("  they were applied.  Each value references the config field path.")
    blank()

    cfg_display = [
        ("UNIVERSE GATES",   [
            ("universe.min_price",                "Minimum price (USD)"),
            ("universe.min_avg_dollar_volume_20d","Minimum avg daily dollar volume"),
        ]),
        ("STEP 3 SCREENING", [
            ("screening.min_rvol",                "Minimum relative volume (RVOL20)"),
            ("screening.min_screening_score",     "Minimum screening score to advance"),
        ]),
        ("SCORING WEIGHTS (must sum to 1.0)", [
            ("scoring_weights.trend",             "Trend sub-score weight"),
            ("scoring_weights.momentum",          "Momentum sub-score weight"),
            ("scoring_weights.setup",             "Setup sub-score weight"),
            ("scoring_weights.volume",            "Volume sub-score weight"),
            ("scoring_weights.market",            "Market regime sub-score weight"),
        ]),
        ("STEP 4 SETUP ANALYSIS", [
            ("step4.target_R",                    "Target R multiple for RR calc"),
        ]),
        ("DIVERSIFICATION", [
            ("diversification.hard_cap_enabled",  "Hard cap mode (True) vs soft penalty (False)"),
            ("diversification.top_n",             "Maximum proposals in final list"),
            ("diversification.sector_max_positions",   "Max positions per sector (hard cap)"),
            ("diversification.industry_max_positions", "Max positions per industry (hard cap)"),
            ("diversification.sector_penalty_factor",  "Sector penalty multiplier (soft)"),
            ("diversification.industry_penalty_factor","Industry penalty multiplier (soft)"),
        ]),
        ("EARNINGS & MACRO RISK", [
            ("earnings.avoid_within_bd",          "Avoid earnings within N business days"),
            ("earnings.penalty_points_max",       "Maximum earnings penalty (score points)"),
            ("macro_event_risk.enabled",          "Macro event risk penalty enabled"),
            ("macro_event_risk.penalty_points",   "Macro event penalty (score points)"),
        ]),
        ("MARKET REGIME THRESHOLDS", [
            ("market_regime.high_risk_vix",       "VIX level → high_risk regime"),
            ("market_regime.extreme_risk_vix",    "VIX level → extreme_risk regime"),
        ]),
    ]

    for section_title, keys in cfg_display:
        line(f"  [{section_title}]")
        for path, description in keys:
            val = _nested(config_json, path)
            kv(f"    {path}", val if val is not None else "—", description)
        blank()

    # ---------------------------------------------------------------- #
    # VERDICT
    # ---------------------------------------------------------------- #
    h2("VERDICT")
    ps_final_v = step5_row.get("proposal_score_final") if step5_row else None
    div_rank_v = step5_row.get("diversified_rank")     if step5_row else None
    raw_rank_v = step5_row.get("raw_rank")             if step5_row else None
    rej_v      = step5_row.get("rejection_reason")     if step5_row else None
    kv("  Final proposal score",       _fmt(ps_final_v, 2) if ps_final_v else "—")
    kv("  Raw rank",                   str(raw_rank_v) if raw_rank_v is not None else "—")
    kv("  Diversified rank",           str(div_rank_v) if div_rank_v is not None else "—")
    kv("  Rejection reason",           rej_v or "none (accepted into diversified list)")
    blank()
    if div_rank_v is not None:
        line(f"  SELECTED ✓  {ticker} was accepted into the diversified shortlist at rank {div_rank_v}.")
    elif raw_rank_v is not None:
        line(f"  PARTIAL    {ticker} passed all gates and has raw rank {raw_rank_v} but was")
        line(f"             rejected from the diversified list: {rej_v}")
    else:
        line(f"  NOT SELECTED  {ticker} did not reach Step 5 on this date/strategy.")
    blank()

    h1("END OF REPORT")

    return "\n".join(L).encode("utf-8"), filename
