"""Ticker selection report generator (app/dashboard/ticker_report.py) — setup-mode.

Produces a plain-text document explaining step by step how and why the pipeline
selected or rejected a ticker on a given signal date under the 4-setup
architecture (breakout / pullback / trend_continuation / consolidation_base).

Public API
----------
build_ticker_report(
    row: dict,
    signal_date: date,
    db_role: str,
    setup_config_id: str,
) -> tuple[bytes, str]

Returns (utf-8 bytes, filename).
Filename: YYMMDD_TICKER_SETUPTYPE.txt
"""

from __future__ import annotations

import datetime as _dt
import json as _json
from datetime import date
from typing import Any



# ──────────────────────────────────────────────────────────────────────────── #
# Field-status constants
# ──────────────────────────────────────────────────────────────────────────── #
_ST_USED        = "USED_BY_SELECTED_SETUP"
_ST_AVAIL       = "AVAILABLE_NOT_USED"
_ST_NA          = "NOT_APPLICABLE_TO_SELECTED_SETUP"
_ST_MISS_ROW    = "MISSING_DB_ROW"
_ST_NOT_CALC    = "NOT_CALCULATED"
_ST_NULL        = "NULL_VALUE"
_ST_INVALID_ETF = "INVALID_BENCHMARK"

# Hard / soft inputs per setup type (used for status labelling only)
_SETUP_USES: dict[str, set[str]] = {
    "breakout": {
        "resistance_level", "breakout_proximity", "range_duration",
        "range_tightness_score", "rvol20", "stop_distance_pct",
        "volume_expansion_score", "base_high", "base_low",
        "next_resistance_level", "swing_high", "atr14", "atr_pct",
    },
    "pullback": {
        "ema200", "ema20", "ema50", "ema_alignment_score",
        "pullback_depth_pct", "support_level", "stop_distance_pct",
        "rvol20", "resistance_level", "next_resistance_level",
        "swing_low", "swing_high", "distance_to_ema20_pct",
    },
    "trend_continuation": {
        "ema200", "ema50", "ema_alignment_score", "ema50_slope",
        "roc20", "distance_to_ema50_pct", "stop_distance_pct",
        "relative_strength_vs_spy", "sector_relative_strength",
        "rvol20", "next_resistance_level", "resistance_level", "swing_low",
    },
    "consolidation_base": {
        "range_duration", "range_tightness_score", "atr_pct",
        "base_high", "base_low", "days_to_earnings_bd",
        "stop_distance_pct", "atr_compression_score", "volume_dry_up_score",
        "next_resistance_level", "resistance_level",
    },
}

ALL_SETUP_TYPES: tuple[str, ...] = (
    "breakout", "pullback", "trend_continuation", "consolidation_base"
)


# ──────────────────────────────────────────────────────────────────────────── #
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────── #

def _nested(d: dict, path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _fmt(v: Any, decimals: int = 2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _pct_fmt(v: Any, decimals: int = 2) -> str:
    """Format a decimal fraction as a percentage string."""
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(v)


def _bool_str(v: Any) -> str:
    if v is None:
        return "—"
    return "Yes" if v else "No"


def _check(
    label: str,
    value: Any,
    threshold: Any,
    direction: str = ">=",
    note: str = "",
    indent: str = "    ",
) -> str:
    """Return a formatted pass/fail comparison line."""
    if value is None:
        note_s = f"  # {note}" if note else ""
        return f"{indent}{label:<50} value=— (no data){note_s}"
    if threshold is None:
        return f"{indent}{label:<50} value={value}  threshold=— (not configured)"
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
        note_s = f"  # {note}" if note else ""
        return f"{indent}{label:<50} {_fmt(fv)} {direction} {_fmt(tv)}  [{mark}]{note_s}"
    except (TypeError, ValueError):
        return f"{indent}{label:<50} value={value}  threshold={threshold}"


def _field_status(
    field_name: str,
    value: Any,
    selected_setup: str | None,
    has_data: bool,
) -> str:
    if not has_data:
        return _ST_MISS_ROW
    if value is None:
        return _ST_NULL
    if selected_setup and field_name in _SETUP_USES.get(selected_setup, set()):
        return _ST_USED
    if selected_setup:
        return _ST_NA
    return _ST_AVAIL


def _ev_row(
    label: str,
    value: Any,
    status: str = "",
    note: str = "",
) -> str:
    """Format one evidence table row."""
    val_str = "—" if value is None else str(value)
    row = f"  {label:<44} {val_str:<16}"
    if status:
        row += f"  [{status}]"
    if note:
        row += f"  # {note}"
    return row


# ──────────────────────────────────────────────────────────────────────────── #
# DB helpers
# ──────────────────────────────────────────────────────────────────────────── #

def _get_cols(conn: Any, table: str) -> list[str]:
    try:
        return [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
    except Exception:
        return []


def _parse_json(row_dict: dict, key: str) -> Any:
    raw = row_dict.get(key)
    if isinstance(raw, str):
        try:
            return _json.loads(raw)
        except Exception:
            return raw
    return raw


# ──────────────────────────────────────────────────────────────────────────── #
# Data fetch
# ──────────────────────────────────────────────────────────────────────────── #

def _fetch_all_data(
    ticker: str,
    signal_date: date,
    db_role: str,
    setup_config_id: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "step3_row":         {},
        "step4_rows":        {},   # keyed by setup_type → dict
        "step5_row":         {},
        "step5_alt_rows":    [],   # all step5 rows for ticker/date (any setup_config_id)
        "config_json":       {},   # config for setup_config_id
        "all_configs":       {},   # setup_type → {config_id, config_json}
        "risk_cfg":          {},   # risk_label_config ranking block
        "buy_rules":         {},   # risk_label_config buy_rules block
        "sector_etf_map":    {},   # sector → etf_ticker from DB table
        "next_earnings_row": None, # (earnings_date, confidence) or None
        "warnings":          [],
    }
    try:
        from app.database import duckdb_manager as _dbm
        conn = _dbm.connect(db_role, read_only=True)
        try:
            # step3_candidates — no setup_config_id column in setup-mode schema
            s3_cols = _get_cols(conn, "step3_candidates")
            if not s3_cols:
                result["warnings"].append(
                    "DB schema mismatch: step3_candidates not found; used fallback query"
                )
            else:
                if "setup_config_id" in s3_cols:
                    result["warnings"].append(
                        "DB schema mismatch: setup_config_id found in step3_candidates "
                        "(old schema?); querying without it"
                    )
                r3 = conn.execute(
                    "SELECT * FROM step3_candidates "
                    "WHERE ticker=? AND signal_date=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    [ticker, signal_date],
                ).fetchone()
                if r3:
                    s3d = dict(zip(s3_cols, r3))
                    for blob in ("feature_snapshot_json", "eligibility_fail_reasons",
                                 "routed_setup_types"):
                        s3d[blob] = _parse_json(s3d, blob)
                    result["step3_row"] = s3d

            # step4_analysis — all setup_types, most recent per type
            s4_cols = _get_cols(conn, "step4_analysis")
            if s4_cols:
                r4_all = conn.execute(
                    "SELECT * FROM step4_analysis "
                    "WHERE ticker=? AND signal_date=? "
                    "ORDER BY setup_type ASC, created_at DESC",
                    [ticker, signal_date],
                ).fetchall()
                seen_types: set[str] = set()
                for r4_row in r4_all:
                    r4d = dict(zip(s4_cols, r4_row))
                    stype = r4d.get("setup_type") or ""
                    if stype and stype not in seen_types:
                        seen_types.add(stype)
                        for blob in ("setup_reasons", "explanation_json"):
                            r4d[blob] = _parse_json(r4d, blob)
                        result["step4_rows"][stype] = r4d

            # step5_proposals — selected proposal (exact setup_config_id match)
            s5_cols = _get_cols(conn, "step5_proposals")
            if s5_cols:
                r5 = conn.execute(
                    "SELECT * FROM step5_proposals "
                    "WHERE ticker=? AND signal_date=? AND setup_config_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    [ticker, signal_date, setup_config_id],
                ).fetchone()
                if r5:
                    r5d = dict(zip(s5_cols, r5))
                    for blob in ("setup_reasons", "risk_reasons"):
                        r5d[blob] = _parse_json(r5d, blob)
                    result["step5_row"] = r5d

                # fallback: all step5 rows for ticker/date (any setup_config_id)
                r5_all = conn.execute(
                    "SELECT * FROM step5_proposals "
                    "WHERE ticker=? AND signal_date=? "
                    "ORDER BY created_at DESC LIMIT 10",
                    [ticker, signal_date],
                ).fetchall()
                for r5a in r5_all:
                    r5ad = dict(zip(s5_cols, r5a))
                    for blob in ("setup_reasons", "risk_reasons"):
                        r5ad[blob] = _parse_json(r5ad, blob)
                    result["step5_alt_rows"].append(r5ad)

            # setup_configs: selected config
            if setup_config_id:
                cfg_row = conn.execute(
                    "SELECT config_json FROM setup_configs WHERE config_id=? LIMIT 1",
                    [setup_config_id],
                ).fetchone()
                if cfg_row and cfg_row[0]:
                    result["config_json"] = (
                        _json.loads(cfg_row[0]) if isinstance(cfg_row[0], str)
                        else cfg_row[0]
                    ) or {}

            # setup_configs: all active (for snapshot thresholds)
            all_cfgs = conn.execute(
                "SELECT setup_type, config_id, config_json FROM setup_configs "
                "WHERE active_flag=TRUE ORDER BY setup_type",
            ).fetchall()
            for c_type, c_id, c_json in all_cfgs:
                parsed = (
                    _json.loads(c_json) if isinstance(c_json, str) else c_json
                ) or {}
                result["all_configs"][c_type] = {
                    "config_id": c_id,
                    "config_json": parsed,
                }

            # risk_label_config: ranking.top_n
            rlc_row = conn.execute(
                "SELECT config_json FROM risk_label_config "
                "WHERE active_flag=TRUE LIMIT 1",
            ).fetchone()
            if rlc_row and rlc_row[0]:
                rlc = (
                    _json.loads(rlc_row[0]) if isinstance(rlc_row[0], str)
                    else rlc_row[0]
                ) or {}
                result["risk_cfg"] = rlc.get("ranking", {})
                result["buy_rules"] = rlc.get("buy_rules", {})

            # sector_etf_map table
            try:
                sem_rows = conn.execute(
                    "SELECT sector, etf_ticker FROM sector_etf_map"
                ).fetchall()
                result["sector_etf_map"] = {s: e for s, e in sem_rows}
            except Exception:
                pass

            # earnings_calendar: next upcoming earnings on/after signal_date
            try:
                earn_row = conn.execute(
                    "SELECT earnings_date, confidence FROM earnings_calendar "
                    "WHERE ticker=? AND earnings_date >= ? "
                    "ORDER BY earnings_date ASC LIMIT 1",
                    [ticker, signal_date],
                ).fetchone()
                result["next_earnings_row"] = earn_row  # (date, confidence) or None
            except Exception:
                pass

        finally:
            conn.close()

    except Exception as exc:
        result["warnings"].append(f"DB fetch error: {exc}")

    return result


# ──────────────────────────────────────────────────────────────────────────── #
# Status / reason helpers
# ──────────────────────────────────────────────────────────────────────────── #

def _determine_final_status(
    step3_row: dict,
    step4_rows: dict,
    step5_row: dict,
) -> str:
    if not step3_row:
        return "report_incomplete"
    if not step3_row.get("passed_eligibility"):
        return "universal_failed"
    if not step4_rows:
        return "report_incomplete"
    any_passed = any(v.get("setup_passed") for v in step4_rows.values())
    if not any_passed:
        return "no_setup_passed"
    if not step5_row:
        return "risk_plan_failed"
    entry = step5_row.get("entry_price_raw")
    stop  = step5_row.get("stop_price_raw")
    tgt   = step5_row.get("target_price_raw")
    if not (entry and stop and tgt):
        return "risk_plan_failed"
    disposition = step5_row.get("disposition", "")
    if disposition == "BUY":
        return "selected_proposal" if step5_row.get("selected_flag") else "not_selected"
    if disposition == "WATCHLIST_ONLY":
        return "external_risk_blocked"
    return "not_selected"


def _reason_list(step4_rows: dict, setup_type: str | None) -> list[str]:
    if not setup_type or setup_type not in step4_rows:
        return []
    reasons = step4_rows[setup_type].get("setup_reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    return reasons


def _failure_reason(
    step3_row: dict,
    step4_rows: dict,
    step5_row: dict,
    selected_setup: str | None,
    final_status: str,
) -> str | None:
    if final_status == "universal_failed":
        fails = step3_row.get("eligibility_fail_reasons") or []
        if not isinstance(fails, list):
            fails = [str(fails)]
        return "; ".join(str(f) for f in fails[:2]) or "eligibility failed"
    if final_status == "no_setup_passed":
        parts = [
            f"{st}: {r4.get('setup_fail_reason')}"
            for st, r4 in step4_rows.items()
            if r4.get("setup_fail_reason")
        ]
        return "; ".join(parts[:3]) or "all setup validations failed"
    if final_status == "risk_plan_failed":
        # Try to give a specific reason from step5 or step4
        if step5_row:
            s5_stop  = step5_row.get("stop_price_raw")
            s5_tgt   = step5_row.get("target_price_raw")
            s5_entry = step5_row.get("entry_price_raw")
            s5_rr    = step5_row.get("estimated_rr")
            s5_rej   = step5_row.get("rejection_reason")
            if s5_rej:
                return s5_rej
            if not s5_entry:
                return "missing entry price anchor in step5"
            if not s5_stop:
                return "missing stop price anchor in step5"
            if not s5_tgt:
                return "missing target price anchor in step5"
            if s5_tgt is not None and s5_entry is not None:
                try:
                    if float(s5_tgt) <= float(s5_entry):
                        return "target_price <= entry_price (invalid trade plan)"
                except (TypeError, ValueError):
                    pass
            if s5_rr is not None:
                return f"estimated_rr={_fmt(s5_rr, 2)} below minimum threshold"
            return "trade plan written but stop/target/RR validation failed"
        if selected_setup and selected_setup in step4_rows:
            fr = step4_rows[selected_setup].get("setup_fail_reason")
            if fr:
                return fr
        return "Step 5 not run or no proposal row written for this ticker/date"
    if final_status == "not_selected":
        return (step5_row or {}).get("rejection_reason") or "not ranked into diversified top-N"
    return None


# ──────────────────────────────────────────────────────────────────────────── #
# Per-setup validation sections
# ──────────────────────────────────────────────────────────────────────────── #

def _soft_components(L: list[str], expl: dict, cfg: dict, names: tuple[str, ...]) -> None:
    comps   = expl.get("component_scores") or {}
    weights = cfg.get("scoring_weights", {})
    for name in names:
        score = comps.get(name)
        w = weights.get(name, "—")
        L.append(f"      {name:<32} {_fmt(score, 1):>8}  (weight={w})")


def _penalties_and_score(
    L: list[str], expl: dict, r4: dict, min_score: Any, earn_days_known: bool = True
) -> None:
    earn_pen  = expl.get("earnings_penalty") if expl.get("earnings_penalty") is not None else r4.get("earnings_penalty")
    macro_pen = expl.get("macro_penalty")    if expl.get("macro_penalty")    is not None else r4.get("macro_penalty")
    raw_sc    = expl.get("raw_score")
    pen_sc    = expl.get("penalized_score")
    if pen_sc is None:
        pen_sc = r4.get("setup_score")
    L.append("")
    if raw_sc is not None:
        L.append(f"      raw_score                                      {_fmt(raw_sc, 2)}")
    earn_unknown_note = (
        "  [WARNING: earnings UNKNOWN — penalty unverified; 0.00 != safe]"
        if not earn_days_known else ""
    )
    L.append(f"      earnings_penalty                               {_fmt(earn_pen, 2)}{earn_unknown_note}")
    L.append(f"      macro_penalty                                  {_fmt(macro_pen, 2)}")
    L.append(_check("setup_score >= min_setup_score", pen_sc, min_score, ">="))


def _section_breakout(
    L: list[str], feat: dict, r4: dict, cfg: dict,
    real_max_stop: float, resolved_stop_dist: float | None,
) -> None:
    val = cfg.get("validation", {})
    expl: dict = r4.get("explanation_json") or {}

    prox_min    = val.get("breakout_prox_min", "—")
    prox_max    = val.get("breakout_prox_max", "—")
    min_dur     = val.get("min_base_duration", "—")
    min_rvol    = val.get("min_rvol_breakout", "—")
    rvol_hard   = val.get("rvol_is_hard", True)
    min_score   = val.get("min_setup_score", "—")
    # The real BUY/WATCHLIST stop-distance gate is risk_label_config.buy_rules
    # .max_stop_distance_pct (step5_proposal_engine.py), not this setup_config's
    # own copy of the field — the latter is never read for that decision.
    max_stop    = real_max_stop

    resistance  = feat.get("resistance_level")
    bp          = feat.get("breakout_proximity")
    range_dur   = feat.get("range_duration")
    rvol20      = feat.get("rvol20") or r4.get("rvol")
    stop_dist   = resolved_stop_dist

    L.append("    HARD CHECKS:")
    res_ok = resistance is not None
    L.append(f"      resistance_level exists                        "
             f"{'Yes — PASS ✓' if res_ok else 'No — FAIL ✗'}")

    if bp is not None and prox_min != "—" and prox_max != "—":
        try:
            in_range = float(prox_min) <= float(bp) <= float(prox_max)
            mark = "PASS ✓" if in_range else "FAIL ✗"
        except (TypeError, ValueError):
            mark = "?"
        L.append(f"      breakout_proximity in [{prox_min}, {prox_max}]              "
                 f"{_fmt(bp, 3)}  [{mark}]")
    else:
        L.append(f"      breakout_proximity in [{prox_min}, {prox_max}]              {_fmt(bp, 3)}")

    L.append(_check("range_duration >= min_base_duration", range_dur, min_dur, ">="))
    if rvol_hard:
        L.append(_check("rvol20 >= min_rvol_breakout  [HARD GATE]", rvol20, min_rvol, ">=",
                         "rvol_is_hard=True"))
    else:
        L.append(f"      rvol20                                         "
                 f"{_fmt(rvol20, 2)}  (soft only — rvol_is_hard=False)")
    if stop_dist is not None:
        L.append(_check("stop_distance_pct <= max_stop_distance_pct", stop_dist, max_stop, "<="))
    else:
        L.append(f"      stop_distance_pct                              — (not yet computed)")

    L.append("")
    L.append("    SOFT SCORE COMPONENTS:")
    _soft_components(L, expl, cfg, (
        "resistance_clarity", "breakout_confirmation",
        "volume_expansion", "base_quality", "target_room",
    ))
    _penalties_and_score(L, expl, r4, min_score)

    L.append("")
    L.append("    TRADE PLAN LOGIC:")
    L.append("      Stop:   base_low_raw - k*ATR - buffer  "
             "(or resistance_raw - k*ATR)")
    L.append("      Target: next_resistance_raw  [structural, first choice]")
    L.append("              prior_swing_high_raw [second choice]")
    L.append("              measured_move_raw    [third choice]")
    L.append("              fixed-R fallback ONLY if no structural target exists")
    L.append(f"      target_is_structural = {_bool_str(r4.get('target_is_structural'))}")


def _section_pullback(
    L: list[str], feat: dict, r4: dict, cfg: dict,
    real_max_stop: float, resolved_stop_dist: float | None,
) -> None:
    val = cfg.get("validation", {})
    expl: dict = r4.get("explanation_json") or {}

    max_pb_depth = val.get("max_pullback_depth", "—")
    support_tol  = val.get("support_break_tol", "—")
    rvol_bonus_t = val.get("rvol_bonus_threshold", "—")
    min_score    = val.get("min_setup_score", "—")
    # Real gate lives in risk_label_config.buy_rules.max_stop_distance_pct —
    # this setup_config's own copy is never read for that decision.
    max_stop     = real_max_stop

    close_adj  = feat.get("close_adj")
    ema200     = feat.get("ema200")
    ema20      = feat.get("ema20")
    ema50      = feat.get("ema50")
    rvol20     = feat.get("rvol20") or r4.get("rvol")
    stop_dist  = resolved_stop_dist

    # pullback_depth_pct — fallback priority: feat → step4 expl → step4 row
    pb_depth = feat.get("pullback_depth_pct")
    pb_depth_src = ""
    if pb_depth is None:
        _v = expl.get("pullback_depth_pct") or r4.get("pullback_depth_pct")
        if _v is not None:
            pb_depth = _v
            pb_depth_src = "source=step4_evidence"

    # support — fallback: feat (support_level) → expl (support_raw / support_adj) → r4
    support = feat.get("support_level")
    support_src = ""
    if support is None:
        _v = (
            expl.get("support_raw")
            or expl.get("support_adj")
            or r4.get("support_level")
        )
        if _v is not None:
            support = _v
            support_src = "source=step4_evidence"

    # close_raw — fallback: feat → expl → r4
    close_raw = feat.get("close_raw")
    close_raw_src = ""
    if close_raw is None:
        _v = expl.get("close_raw") or r4.get("close_raw")
        if _v is not None:
            close_raw = _v
            close_raw_src = "source=step4_evidence"

    earn_days_known = feat.get("days_to_earnings_bd") is not None

    L.append("    HARD CHECKS:")
    L.append(_check("close_adj > ema200  (uptrend required)", close_adj, ema200, ">"))
    L.append(_check("ema20 > ema50  (short-term trend up)", ema20, ema50, ">"))

    pb_check = _check("pullback_depth_pct <= max_pullback_depth", pb_depth, max_pb_depth, "<=")
    if pb_depth_src:
        pb_check += f"  [{pb_depth_src}]"
    L.append(pb_check)

    if support is not None and close_raw is not None and support_tol != "—":
        try:
            lower = float(support) * (1.0 - float(support_tol))
            passed = float(close_raw) >= lower
            mark = "PASS ✓" if passed else "FAIL ✗"
            src_parts = [s for s in (support_src, close_raw_src) if s]
            src_note = f"  [{'; '.join(src_parts)}]" if src_parts else ""
            L.append(f"      close_raw >= support*(1-tol={support_tol})            "
                     f"{_fmt(close_raw)} >= {_fmt(lower, 2)}  [{mark}]{src_note}")
        except (TypeError, ValueError):
            L.append(f"      close_raw >= support*(1-tol)                  "
                     f"close={_fmt(close_raw)}  support={_fmt(support)}")
    else:
        src_parts = [s for s in (support_src, close_raw_src) if s]
        src_note = f"  [{'; '.join(src_parts)}]" if src_parts else ""
        L.append(f"      close_raw >= support*(1-tol)                  "
                 f"support={_fmt(support, 2)}  close={_fmt(close_raw, 2)}{src_note}")

    L.append(f"      rvol20 = {_fmt(rvol20, 2)}"
             f"  (soft bonus >= {rvol_bonus_t} — NEVER hard reject; AD-22.23)")
    if stop_dist is not None:
        L.append(_check("stop_distance_pct <= max_stop_distance_pct", stop_dist, max_stop, "<="))

    L.append("")
    L.append("    SOFT SCORE COMPONENTS:")
    _soft_components(L, expl, cfg, (
        "uptrend_intact", "support_ema_hold", "pullback_depth",
        "trend_structure", "rr",
    ))
    _penalties_and_score(L, expl, r4, min_score, earn_days_known=earn_days_known)

    L.append("")
    L.append("    TRADE PLAN LOGIC:")
    L.append("      Stop:   min(support_raw, swing_low_raw, ema_area) - buffer")
    L.append("      Target: prior_swing_high_raw  [structural, first choice]")
    L.append("              fixed-R fallback if no structural target")
    L.append(f"      target_is_structural = {_bool_str(r4.get('target_is_structural'))}")


def _section_trend_continuation(
    L: list[str], feat: dict, r4: dict, cfg: dict,
    real_max_stop: float, resolved_stop_dist: float | None,
) -> None:
    val = cfg.get("validation", {})
    expl: dict = r4.get("explanation_json") or {}

    min_align  = val.get("min_ema_alignment", "—")
    min_slope  = val.get("min_ema50_slope", "—")
    roc_min    = val.get("roc_min", "—")
    roc_max    = val.get("roc_max", "—")
    max_ext    = val.get("max_ext", "—")
    min_score  = val.get("min_setup_score", "—")
    # Real gate lives in risk_label_config.buy_rules.max_stop_distance_pct —
    # this setup_config's own copy is never read for that decision.
    max_stop   = real_max_stop

    close_adj   = feat.get("close_adj")
    ema50       = feat.get("ema50")
    ema200      = feat.get("ema200")
    ema_align   = feat.get("ema_alignment_score")
    ema50_slope = feat.get("ema50_slope")
    roc20       = feat.get("roc20")
    dist_ema50  = feat.get("distance_to_ema50_pct")
    rvol20      = feat.get("rvol20") or r4.get("rvol")
    stop_dist   = resolved_stop_dist
    rvol_mod    = val.get("rvol_moderate_threshold", "—")

    L.append("    HARD CHECKS:")
    L.append(_check("close_adj > ema200", close_adj, ema200, ">"))
    L.append(_check("ema_alignment_score >= min_ema_alignment", ema_align, min_align, ">="))
    L.append(_check("ema50_slope > min_ema50_slope", ema50_slope, min_slope, ">"))
    L.append(_check("close_adj > ema50", close_adj, ema50, ">"))

    if roc20 is not None and roc_min != "—" and roc_max != "—":
        try:
            in_range = float(roc_min) <= float(roc20) <= float(roc_max)
            mark = "PASS ✓" if in_range else "FAIL ✗"
        except (TypeError, ValueError):
            mark = "?"
        L.append(f"      roc20 in [{roc_min}, {roc_max}]                              "
                 f"{_fmt(roc20, 4)}  [{mark}]")
    else:
        L.append(f"      roc20 in [{roc_min}, {roc_max}]                              "
                 f"{_fmt(roc20, 4)}")

    if dist_ema50 is not None and max_ext != "—":
        try:
            abs_dist = abs(float(dist_ema50))
            passed = abs_dist <= float(max_ext)
            mark = "PASS ✓" if passed else "FAIL ✗"
        except (TypeError, ValueError):
            abs_dist, mark = None, "?"
        L.append(f"      |distance_to_ema50_pct| <= max_ext             "
                 f"{_fmt(abs_dist, 4)} <= {max_ext}  [{mark}]")
    else:
        L.append(f"      |distance_to_ema50_pct| <= max_ext             {_fmt(dist_ema50, 4)}")

    L.append(f"      rvol20 = {_fmt(rvol20, 2)}"
             f"  (soft confirmation >= {rvol_mod} — rvol_is_hard=False)")
    if stop_dist is not None:
        L.append(_check("stop_distance_pct <= max_stop_distance_pct", stop_dist, max_stop, "<="))

    L.append("")
    L.append("    SOFT SCORE COMPONENTS:")
    _soft_components(L, expl, cfg, (
        "trend_health", "relative_strength", "extension",
        "momentum", "volume_health", "target_room",
    ))
    _penalties_and_score(L, expl, r4, min_score)

    L.append("")
    L.append("    TRADE PLAN LOGIC:")
    L.append("      Stop:   max(higher_low_raw, swing_low_raw) - buffer")
    L.append("      Target: next_resistance_raw  [structural, first choice]")
    L.append("              measured_move_raw    [second choice]")
    L.append("              fixed-R fallback if needed")
    L.append(f"      target_is_structural = {_bool_str(r4.get('target_is_structural'))}")


def _section_consolidation_base(
    L: list[str], feat: dict, r4: dict, cfg: dict,
    real_max_stop: float, resolved_stop_dist: float | None,
) -> None:
    val = cfg.get("validation", {})
    expl: dict = r4.get("explanation_json") or {}

    # Field names must match what validate_consolidation_base() actually reads
    # under setup_config["validation"] (m14_setup_validators.py) — this section
    # previously read three keys ("min_base_duration", "min_range_tightness_score",
    # "earnings_avoidance_days"/"min_days_to_earnings") that don't exist in any
    # consolidation_base config, so these checks always rendered as "—".
    min_dur    = val.get("min_range_duration", "—")
    min_tight  = val.get("min_tightness", "—")
    max_atr    = val.get("max_atr_pct", "—")
    earn_avoid = val.get("min_earnings_days", "—")
    min_score  = val.get("min_setup_score", "—")
    # Real gate lives in risk_label_config.buy_rules.max_stop_distance_pct —
    # this setup_config's own copy is never read for that decision.
    max_stop   = real_max_stop

    range_dur  = feat.get("range_duration")
    tightness  = feat.get("range_tightness_score")
    atr_pct    = feat.get("atr_pct") or r4.get("atr_pct")
    base_low   = feat.get("base_low")
    base_high  = feat.get("base_high")
    close_raw  = feat.get("close_raw")
    earn_days  = feat.get("days_to_earnings_bd") or r4.get("earnings_days")
    stop_dist  = resolved_stop_dist

    L.append("    HARD CHECKS:")
    L.append(_check("range_duration >= min_range_duration", range_dur, min_dur, ">="))
    L.append(_check("range_tightness_score >= min_tightness", tightness, min_tight, ">="))
    L.append(_check("atr_pct <= max_atr_pct", atr_pct, max_atr, "<="))

    if base_low is not None and base_high is not None and close_raw is not None:
        try:
            in_range = float(base_low) <= float(close_raw) <= float(base_high)
            mark = "PASS ✓" if in_range else "FAIL ✗"
        except (TypeError, ValueError):
            mark = "?"
        L.append(f"      base_low <= close_raw <= base_high             "
                 f"{_fmt(base_low)} <= {_fmt(close_raw)} <= {_fmt(base_high)}  [{mark}]")
    else:
        L.append(f"      base_low <= close_raw <= base_high             "
                 f"base_low={_fmt(base_low)}  base_high={_fmt(base_high)}")

    L.append(_check("days_to_earnings_bd > min_earnings_days",
                     earn_days, earn_avoid, ">"))
    if stop_dist is not None:
        L.append(_check("stop_distance_pct <= max_stop_distance_pct", stop_dist, max_stop, "<="))

    L.append("")
    L.append("    SOFT SCORE COMPONENTS:")
    _soft_components(L, expl, cfg, (
        "range_tightness", "support_resistance_clarity",
        "atr_compression", "volume_dry_up",
        "breakout_readiness", "stop_tightness",
    ))
    _penalties_and_score(L, expl, r4, min_score)

    L.append("")
    L.append("    TRADE PLAN LOGIC:")
    L.append("      Stop:   base_low_raw - buffer")
    L.append("      Target: measured_move_raw (near upper range, primary)")
    L.append("              next_resistance_raw (if available)")
    L.append("              fixed-R fallback if needed")
    L.append(f"      target_is_structural = {_bool_str(r4.get('target_is_structural'))}")


# ──────────────────────────────────────────────────────────────────────────── #
# Main entry point
# ──────────────────────────────────────────────────────────────────────────── #

def build_ticker_report(
    row: dict,
    signal_date: date,
    db_role: str,
    setup_config_id: str,
) -> tuple[bytes, str]:
    """Build the selection report and return (utf-8 bytes, filename)."""

    ticker       = row.get("ticker", "UNKNOWN")
    company_name = row.get("company_name") or ""
    sector       = row.get("sector") or "—"
    industry     = row.get("industry") or "—"

    # ── Fetch all DB data ──────────────────────────────────────────────────── #
    data          = _fetch_all_data(ticker, signal_date, db_role, setup_config_id)
    step3_row     = data["step3_row"]
    step4_rows    = data["step4_rows"]
    step5_row     = data["step5_row"]
    step5_alt     = data["step5_alt_rows"]
    config_json   = data["config_json"]
    all_configs   = data["all_configs"]
    risk_cfg      = data["risk_cfg"]
    buy_rules     = data["buy_rules"]
    sector_etf_db = data["sector_etf_map"]
    db_warnings   = data["warnings"]

    # The real BUY/WATCHLIST stop-distance gate (step5_proposal_engine.py) reads
    # this from risk_label_config.buy_rules, never from a setup_config's own
    # "max_stop_distance_pct" — use the same source and same fallback here so
    # the diagnostic report matches what the pipeline actually enforced.
    real_max_stop = float(buy_rules.get("max_stop_distance_pct", 0.10))

    # Feature snapshot (from step3 or empty)
    feat     = step3_row.get("feature_snapshot_json") or {}
    has_feat = bool(feat)

    # ── Determine selected setup ───────────────────────────────────────────── #
    selected_setup: str | None = (
        row.get("setup_type")
        or step5_row.get("setup_type")
        or next(
            (st for st in ALL_SETUP_TYPES
             if step4_rows.get(st, {}).get("setup_passed")),
            None,
        )
    )

    # If exact setup_config_id lookup returned no row but alt rows exist,
    # promote the best matching alt row (same setup_type, BUY preferred).
    if not step5_row and step5_alt:
        _best: dict = {}
        for _alt in step5_alt:
            if _alt.get("setup_type") == selected_setup:
                if not _best or _alt.get("disposition") == "BUY":
                    _best = _alt
        if not _best and step5_alt:
            _best = step5_alt[0]
        if _best:
            step5_row = _best
            _resolved_id = _best.get("setup_config_id") or ""
            if not setup_config_id:
                db_warnings.append(
                    f"requested setup_config_id was blank; "
                    f"using selected Step 5 row config_id={_resolved_id!r}"
                )
            else:
                db_warnings.append(
                    f"step5 setup_config_id mismatch: requested {setup_config_id!r}; "
                    f"using row with config_id={_resolved_id!r}"
                )

    # Config for selected setup (prefer direct lookup over passed config_json)
    if not config_json and selected_setup and selected_setup in all_configs:
        config_json = all_configs[selected_setup]["config_json"]
    selected_r4  = step4_rows.get(selected_setup or "") or {}
    if selected_setup:
        selected_cfg = all_configs.get(selected_setup, {}).get("config_json") or config_json
    else:
        selected_cfg = config_json

    # Resolved display config_id: prefer step5_row's actual id over the (possibly blank) passed value
    resolved_config_id: str = (
        (step5_row.get("setup_config_id") if step5_row else None)
        or (all_configs.get(selected_setup or "", {}).get("config_id") if selected_setup else None)
        or setup_config_id
        or "—"
    )

    # ── Final status ───────────────────────────────────────────────────────── #
    final_status    = _determine_final_status(step3_row, step4_rows, step5_row)
    reasons_pass    = _reason_list(step4_rows, selected_setup)
    main_fail       = _failure_reason(
        step3_row, step4_rows, step5_row, selected_setup, final_status
    )

    # Trade plan values (step5 preferred; fallback to step4)
    entry_price  = step5_row.get("entry_price_raw")  or selected_r4.get("entry_price_raw")
    stop_price   = step5_row.get("stop_price_raw")   or selected_r4.get("stop_price_raw")
    target_price = step5_row.get("target_price_raw") or selected_r4.get("target_price_raw")
    estimated_rr = step5_row.get("estimated_rr")     or selected_r4.get("estimated_rr")
    tgt_struct   = step5_row.get("target_is_structural") or selected_r4.get("target_is_structural")
    # stop_distance_pct: step4 stores NULL in P4; derive from step5 prices when missing
    stop_dist     = selected_r4.get("stop_distance_pct")
    stop_dist_src = "step4" if stop_dist is not None else None
    if stop_dist is None and entry_price and stop_price:
        try:
            stop_dist = (float(entry_price) - float(stop_price)) / float(entry_price)
            stop_dist_src = "step5_derived"
        except (ZeroDivisionError, TypeError, ValueError):
            pass

    # ── Earnings calendar resolution ───────────────────────────────────────── #
    # Direct query from earnings_calendar gives accurate data even when the
    # feature_snapshot was computed before the G-EARN gap was closed.
    _next_earn_row = data.get("next_earnings_row")  # (earnings_date, confidence) or None
    earn_date_from_cal: date | None = _next_earn_row[0] if _next_earn_row else None
    earn_conf_from_cal: str | None  = _next_earn_row[1] if _next_earn_row else None
    earn_days_from_cal: int | None  = None
    if earn_date_from_cal is not None:
        try:
            from app.utils.trading_calendar import trading_days_between as _tdb
            import datetime as _dt2
            _next = signal_date + _dt2.timedelta(days=1)
            if _next <= earn_date_from_cal:
                earn_days_from_cal = len(_tdb(_next, earn_date_from_cal))
            else:
                earn_days_from_cal = 0
        except Exception:
            pass

    # Step 5 display values (only meaningful when step5_row exists)
    s5_raw_rank  = step5_row.get("raw_rank")        if step5_row else None
    s5_div_rank  = step5_row.get("diversified_rank") if step5_row else None
    s5_disp      = step5_row.get("disposition", "—") if step5_row else "—"
    s5_sel_flag  = step5_row.get("selected_flag")    if step5_row else None

    # Parse final_trade_decision from mechanical_explanation (P0 — stored in JSON, no schema change)
    _mech_str = step5_row.get("mechanical_explanation") if step5_row else None
    _mech_dict: dict = {}
    if _mech_str:
        try:
            _mech_dict = _json.loads(_mech_str) if isinstance(_mech_str, str) else _mech_str
        except Exception:
            pass
    final_trade_decision: str = _mech_dict.get("final_trade_decision") or (
        "REJECTED" if s5_disp == "REJECTED"
        else "WATCHLIST_ONLY" if s5_disp == "WATCHLIST_ONLY"
        else "—"
    )
    ftd_reason_codes: list[str] = _mech_dict.get("ftd_reason_codes") or []
    # P0: action-readiness fields; WAIT_* FTDs map to WATCHLIST_ONLY
    effective_disposition: str = _mech_dict.get("effective_disposition") or (
        "REJECTED" if s5_disp == "REJECTED"
        else "WATCHLIST_ONLY" if s5_disp == "WATCHLIST_ONLY"
        else "—"
    )
    effective_risk_label: str = _mech_dict.get("effective_risk_label") or "—"

    # Filename
    setup_short = (selected_setup or setup_config_id or "unknown").replace("_v1", "")
    date_short  = signal_date.strftime("%y%m%d")
    filename    = f"{date_short}_{ticker}_{setup_short}.txt".replace(" ", "_")

    # ── Report helpers ─────────────────────────────────────────────────────── #
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

    # ══════════════════════════════════════════════════════════════════════════ #
    # SECTION 0: HEADER & SELECTED SETUP OVERVIEW
    # ══════════════════════════════════════════════════════════════════════════ #
    h1(f"TICKER SELECTION REPORT — {ticker}  ({company_name})")
    kv("Ticker",          ticker)
    kv("Signal date",     signal_date.isoformat())
    kv("Sector",          sector)
    kv("Industry",        industry)
    kv("DB role",         db_role)
    kv("Setup config ID", resolved_config_id)
    kv("Generated at",    _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    blank()

    if db_warnings:
        for w in db_warnings:
            line(f"  [WARNING] {w}")
        blank()

    line("┄" * 72)
    line("  SELECTED SETUP OVERVIEW")
    line("┄" * 72)
    blank()

    kv("  Selected setup mode",   selected_setup or "— (could not determine)")
    cfg_name = (
        selected_cfg.get("config_name") or selected_cfg.get("name") or "—"
    ) if selected_cfg else "—"
    sel_cfg_id = (
        all_configs.get(selected_setup or "", {}).get("config_id") or setup_config_id or "—"
    )
    kv("  Setup config name",     cfg_name)
    kv("  Setup config ID",       sel_cfg_id)

    val_cfg      = (selected_cfg or {}).get("validation", {})
    min_sc       = val_cfg.get("min_setup_score")
    selected_sc  = step5_row.get("setup_score") or selected_r4.get("setup_score")
    kv("  Selected setup score",  _fmt(selected_sc, 2))
    kv("  Min setup score",       _fmt(min_sc, 2) if min_sc is not None else "—")

    # Validator status
    if not step3_row:
        v_status = "INSUFFICIENT_DATA"
    elif not step3_row.get("passed_eligibility"):
        v_status = "FAIL (universal eligibility failed)"
    elif selected_r4.get("setup_passed") is True:
        v_status = "PASS"
    elif selected_r4.get("setup_passed") is False:
        v_status = "FAIL"
    elif not selected_r4:
        v_status = "INSUFFICIENT_DATA (no step4 row)"
    else:
        v_status = "INSUFFICIENT_DATA"
    kv("  Setup validator status", v_status)

    # Final status with human label
    _status_labels = {
        "selected_proposal":    "SELECTED ✓ — accepted into diversified shortlist",
        "not_selected":         "NOT SELECTED — rejected at ranking/diversification",
        "universal_failed":     "NOT SELECTED — failed universal eligibility (Step 3)",
        "no_setup_passed":      "NOT SELECTED — no setup mode passed validation (Step 4)",
        "risk_plan_failed":     "NOT SELECTED — setup passed but trade plan failed (Step 5)",
        "external_risk_blocked":"WATCHLIST ONLY — blocked by macro/earnings risk",
        "report_incomplete":    "REPORT INCOMPLETE — insufficient DB data to determine outcome",
    }
    kv("  Final status",          _status_labels.get(final_status, final_status))
    blank()

    if final_status == "selected_proposal" and reasons_pass:
        line("  Reason selected:")
        for r in reasons_pass:
            line(f"    * {r}")
    elif main_fail:
        line(f"  Main failure reason:  {main_fail}")
    blank()

    # ══════════════════════════════════════════════════════════════════════════ #
    # SECTION 1: UNIVERSAL ELIGIBILITY  (Step 3)
    # ══════════════════════════════════════════════════════════════════════════ #
    h2("SECTION 1 — UNIVERSAL ELIGIBILITY  (M13 → step3_candidates)")
    line("  Setup-neutral gates applied to all tickers before setup routing.")
    line("  These fields are NOT setup-specific.")
    blank()

    # Initialise variables that are only set inside the else-branch below
    # so Section 6 can safely reference them even when step3_row is empty.
    earn_days         = None
    market_reg        = "UNKNOWN"
    market_reg_source = "not_found"

    if not step3_row:
        line("  No step3_candidates row found for this ticker / signal_date.")
        line("  [Possible causes: pipeline not run for this date; DB schema mismatch]")
        blank()
    else:
        passed_elig  = step3_row.get("passed_eligibility")
        routing_stat = step3_row.get("routing_status", "—")
        routed_to    = step3_row.get("routed_setup_types") or []
        elig_fails   = step3_row.get("eligibility_fail_reasons") or []
        elig_score   = step3_row.get("eligibility_score")

        kv("  Eligibility passed",  "PASS ✓" if passed_elig else "FAIL ✗")
        kv("  Eligibility score",   _fmt(elig_score, 2))
        kv("  Routing status",      routing_stat,
           "'routed'=forwarded to setup validators; 'no_route'=no setup matched; "
           "'ineligible'=hard gate failed")
        kv("  Routed to setups",    ", ".join(routed_to) if routed_to else "—")
        if not isinstance(elig_fails, list):
            elig_fails = [str(elig_fails)]
        if elig_fails:
            kv("  Eligibility fail reasons", "; ".join(str(f) for f in elig_fails))
        blank()

        close_raw  = feat.get("close_raw")
        close_adj  = feat.get("close_adj")
        avg_dv     = feat.get("avg_dollar_volume_20d")
        feat_ready = feat.get("feature_ready")
        feat_ver   = feat.get("feature_schema_version") or "—"
        # days_to_earnings_bd: None means UNKNOWN (earnings not in calendar), not "safe"
        earn_days  = feat.get("days_to_earnings_bd")
        # market_regime: feature_snapshot may be stale (M12 updates after snapshot was taken)
        # fall back to step4 value which is read at step4 runtime (after M12)
        _mr_from_snap = feat.get("market_regime")
        _mr_from_s4   = selected_r4.get("market_regime") if selected_r4 else None
        if _mr_from_snap not in (None, "", "None"):
            market_reg        = str(_mr_from_snap)
            market_reg_source = "feature_snapshot"
        elif _mr_from_s4 not in (None, "", "None"):
            market_reg        = str(_mr_from_s4)
            market_reg_source = "step4_analysis"
        else:
            market_reg        = "UNKNOWN"
            market_reg_source = "not_found"
        macro_flag = feat.get("macro_event_risk_flag")
        spy_rs     = feat.get("relative_strength_vs_spy")
        sect_rs    = feat.get("sector_relative_strength")

        # Universe thresholds — pull from any active config
        universe_cfg: dict = {}
        for _, c in all_configs.items():
            u = c.get("config_json", {}).get("universe", {})
            if u:
                universe_cfg = u
                break
        min_price = universe_cfg.get("min_price")
        min_dv    = universe_cfg.get("min_avg_dollar_volume_20d")

        line("  Price / Volume:")
        kv("    close_raw (signal date)",   _fmt(close_raw, 2))
        kv("    close_adj",                 _fmt(close_adj, 2))
        kv("    avg_dollar_volume_20d",     _fmt(avg_dv, 0))
        blank()

        line("  Universal hard gates:")
        line(_check("close_raw >= universe.min_price", close_raw, min_price, ">=",
                     f"min_price={_fmt(min_price, 2)}", indent="    "))
        line(_check("avg_dollar_volume_20d >= min",   avg_dv, min_dv, ">=",
                     f"min={_fmt(min_dv, 0)}", indent="    "))
        blank()

        line("  Data quality:")
        kv("    feature_ready",             _bool_str(feat_ready),
           "must be TRUE to enter setup routing")
        kv("    feature_schema_version",    feat_ver)
        kv("    symbol_type",               feat.get("symbol_type") or "—")
        kv("    active_flag",               _bool_str(feat.get("active_flag")))
        blank()

        line("  Earnings / macro context:")
        # Resolve effective earn_days: feature_snapshot first, calendar direct-query as fallback.
        _eff_earn_days: int | None = earn_days if earn_days is not None else earn_days_from_cal
        if _eff_earn_days is not None:
            _earn_src = "feature_snapshot" if earn_days is not None else "earnings_calendar"
            kv("    days_to_earnings_bd",  str(_eff_earn_days), f"source={_earn_src}")
            if earn_date_from_cal is not None:
                kv("    earnings_date (calendar)", str(earn_date_from_cal),
                   f"confidence={earn_conf_from_cal or 'unknown'}")
        else:
            kv("    days_to_earnings_bd", "UNKNOWN",
               "no earnings_calendar entry — unknown != zero risk; penalty cannot be verified")
            if earn_date_from_cal is None:
                line("    [earnings_calendar has no entry for this ticker/date range]")
        # Expose pipeline mismatch: did the stored step4 penalty match what calendar implies?
        if earn_days is None and earn_days_from_cal is not None:
            _earn_cfg = (selected_cfg or {}).get("earnings", {}) if selected_cfg else {}
            _avoid_bd = int(_earn_cfg.get("avoid_within_bd", 5))
            _pen_max  = float(_earn_cfg.get("penalty_points_max", -15))
            if 0 <= earn_days_from_cal <= _avoid_bd:
                _frac = 1.0 - earn_days_from_cal / _avoid_bd if _avoid_bd > 0 else 1.0
                _correct_pen = _pen_max * _frac
                _stored_pen  = selected_r4.get("earnings_penalty") if selected_r4 else None
                line(f"    [WARNING] earnings data was missing at pipeline run time.")
                line(f"    [WARNING] correct_penalty={_fmt(_correct_pen, 2)} "
                     f"stored_penalty={_fmt(_stored_pen, 2)} — pipeline re-run recommended.")
        kv("    macro_event_risk_flag",     _bool_str(macro_flag))
        blank()

        line("  Market context:")
        if market_reg == "UNKNOWN":
            mr_note = (
                f"NULL in feature_snapshot AND step4_analysis (source={market_reg_source}); "
                "NULL blocks BUY — not treated as neutral (AD-22.23)"
            )
        else:
            mr_note = (
                f"source={market_reg_source}; "
                "bull/neutral=BUY eligible; bear=penalty; high_risk/extreme_risk=BUY blocked"
            )
        kv("    market_regime",             market_reg, mr_note)
        kv("    relative_strength_vs_spy",  _pct_fmt(spy_rs) if spy_rs is not None else "—",
           "vs SPY")
        kv("    sector_relative_strength",  _pct_fmt(sect_rs) if sect_rs is not None else "—",
           "vs sector ETF")
        blank()

    # ══════════════════════════════════════════════════════════════════════════ #
    # SECTION 2: SELECTED SETUP VALIDATION  (Step 4)
    # ══════════════════════════════════════════════════════════════════════════ #
    h2(f"SECTION 2 — SELECTED SETUP VALIDATION  (M14 → step4_analysis)")
    line(f"  Setup mode: {selected_setup or '—'}")
    blank()

    if not selected_setup:
        line("  Selected setup mode could not be determined.")
        blank()
    elif not selected_r4:
        line(f"  No step4_analysis row found for setup_type={selected_setup!r}.")
        line("  [Possible: ticker was not routed to this setup; pipeline step skipped]")
        blank()
    else:
        kv("  Setup passed",        "PASS ✓" if selected_r4.get("setup_passed") else "FAIL ✗")
        kv("  Setup score",         _fmt(selected_r4.get("setup_score"), 2))
        kv("  Setup fail reason",   selected_r4.get("setup_fail_reason") or "—")
        blank()
        line("  Pass / fail reasons (stored):")
        reasons_stored = selected_r4.get("setup_reasons") or []
        if isinstance(reasons_stored, list):
            for r in reasons_stored:
                line(f"    * {r}")
        else:
            line(f"    {reasons_stored}")
        blank()

        # Setup-specific validation detail
        if selected_setup == "breakout":
            _section_breakout(L, feat, selected_r4, selected_cfg or {}, real_max_stop, stop_dist)
        elif selected_setup == "pullback":
            _section_pullback(L, feat, selected_r4, selected_cfg or {}, real_max_stop, stop_dist)
        elif selected_setup == "trend_continuation":
            _section_trend_continuation(L, feat, selected_r4, selected_cfg or {}, real_max_stop, stop_dist)
        elif selected_setup == "consolidation_base":
            _section_consolidation_base(L, feat, selected_r4, selected_cfg or {}, real_max_stop, stop_dist)
        blank()

        # Explanation JSON supplemental evidence
        expl = selected_r4.get("explanation_json")
        if isinstance(expl, dict) and expl:
            line("  Step 4 stored evidence (selected fields):")
            skip_keys = {"component_scores", "hard_fails"}
            for k, v in expl.items():
                if k not in skip_keys:
                    kv(f"    {k}", v)
            blank()

    # ══════════════════════════════════════════════════════════════════════════ #
    # SECTION 3: ALL SETUP MODES SNAPSHOT
    # ══════════════════════════════════════════════════════════════════════════ #
    h2("SECTION 3 — ALL SETUP MODES SNAPSHOT")
    line("  Status of all 4 setup modes for this ticker / signal_date.")
    line("  ► marks the selected setup.")
    blank()
    line(f"  {'Setup mode':<28} {'Status':<26} {'Score':<10} Top reasons")
    line(f"  {'─'*28} {'─'*26} {'─'*10} {'─'*30}")

    routed_set = set(step3_row.get("routed_setup_types") or []) if step3_row else set()

    for stype in ALL_SETUP_TYPES:
        r4 = step4_rows.get(stype)
        if r4 is None:
            if not step3_row:
                status_lbl = "insufficient_data"
                top_reasons: list[str] = ["no step3 row"]
            elif not step3_row.get("passed_eligibility"):
                status_lbl = "not_evaluated"
                top_reasons = ["universal eligibility failed"]
            elif stype not in routed_set:
                status_lbl = "not_evaluated"
                top_reasons = ["not in routed_setup_types"]
            else:
                status_lbl = "insufficient_data"
                top_reasons = ["routed but no step4 row found"]
            score_str = "—"
        else:
            if r4.get("setup_passed"):
                status_lbl = "selected" if stype == selected_setup else "passed"
            else:
                status_lbl = "failed"
            raw_reasons = r4.get("setup_reasons") or []
            if not isinstance(raw_reasons, list):
                raw_reasons = [str(raw_reasons)]
            top_reasons = raw_reasons[:3]
            score_str = _fmt(r4.get("setup_score"), 2)

        prefix = "► " if stype == selected_setup else "  "
        reasons_str = "; ".join(top_reasons)
        line(f"{prefix}{stype:<28} {status_lbl:<26} {score_str:<10} {reasons_str}")

    blank()
    blank()

    # ══════════════════════════════════════════════════════════════════════════ #
    # SECTION 4: FULL TECHNICAL EVIDENCE
    # ══════════════════════════════════════════════════════════════════════════ #
    h2("SECTION 4 — FULL AVAILABLE STOCK EVIDENCE")
    line("  All technical fields for this ticker on the signal date.")
    line(f"  Status: {_ST_USED} | {_ST_NA} | {_ST_AVAIL} | {_ST_NULL} | {_ST_MISS_ROW}")
    blank()

    # Step4 explanation_json for evidence merging.
    # step4 explanation_json uses different key names for some feat fields.
    # Mapping: feat_field → step4_expl_key (or step4 column via selected_r4.get())
    _s4_expl: dict = (selected_r4.get("explanation_json") or {}) if selected_r4 else {}
    _s4_expl_aliases: dict[str, str] = {
        "rvol20":                   "rvol20",          # present in expl as rvol20
        "relative_strength_vs_spy": "rs_vs_spy",
        "sector_relative_strength": "sector_rs",
        "distance_to_ema20_pct":    "dist_ema20_pct",
        "distance_to_ema50_pct":    "dist_ema50_pct",
        "support_level":            "support_adj",
        "resistance_level":         "resistance_adj",
        "next_resistance_level":    "next_resistance_adj",
        "swing_high":               "swing_high_raw",
        "swing_low":                "swing_low_adj",
    }
    # step4 column aliases (feat_field → step4 column name when different)
    _s4_col_aliases: dict[str, str] = {"rvol20": "rvol"}

    # All step4 explanation_jsons for cross-setup fallback (RS fields not in all setups' expls)
    _all_s4_expls: list[dict] = [
        r4.get("explanation_json") or {}
        for r4 in step4_rows.values()
        if isinstance(r4.get("explanation_json"), dict)
    ]

    def ev(label: str, field: str, value: Any = None, note: str = "") -> None:
        """Emit one evidence row; falls back to step4 explanation_json when feat is NULL."""
        src_note = ""
        if value is None:
            value = feat.get(field)
        if value is None:
            # 1. Try selected setup step4 explanation_json (direct key)
            v_s4 = _s4_expl.get(field)
            # 2. Try explanation_json alias (field name differs between feat and expl)
            if v_s4 is None:
                expl_key = _s4_expl_aliases.get(field)
                if expl_key:
                    v_s4 = _s4_expl.get(expl_key)
            # 3. Try step4 direct column (same name or column alias)
            if v_s4 is None and selected_r4:
                col_key = _s4_col_aliases.get(field, field)
                v_s4 = selected_r4.get(col_key)
            # 4. Fallback: search all other step4 explanation_jsons
            if v_s4 is None:
                expl_key = _s4_expl_aliases.get(field, field)
                for _expl in _all_s4_expls:
                    if _expl is _s4_expl:
                        continue
                    v_s4 = _expl.get(field) or _expl.get(expl_key)
                    if v_s4 is not None:
                        break
            if v_s4 is not None:
                value = v_s4
                src_note = "source: step4_evidence"
        # Format numeric values
        if isinstance(value, float):
            disp = _fmt(value, 4)
        elif value is None:
            disp = None
        else:
            disp = str(value)
        status = _field_status(field, feat.get(field), selected_setup, has_feat)
        # If value came from step4, override NULL status
        if feat.get(field) is None and value is not None:
            status = _ST_USED if (selected_setup and field in _SETUP_USES.get(selected_setup, set())) else _ST_AVAIL
        combined_note = (f"{note}  [{src_note}]" if note and src_note else src_note or note)
        L.append(_ev_row(label, disp, status=status, note=combined_note))

    line("  ─── TREND ──────────────────────────────────────────────────────────")
    ev("ema20",                    "ema20")
    ev("ema50",                    "ema50")
    ev("ema200",                   "ema200")

    _ema20_v  = feat.get("ema20")
    _ema50_v  = feat.get("ema50")
    _ema200_v = feat.get("ema200")
    _cadj_v   = feat.get("close_adj")

    ema20_gt_50 = (
        (_ema20_v > _ema50_v) if (_ema20_v is not None and _ema50_v is not None) else None
    )
    c_gt_50 = (
        (_cadj_v > _ema50_v) if (_cadj_v is not None and _ema50_v is not None) else None
    )
    c_gt_200 = (
        (_cadj_v > _ema200_v) if (_cadj_v is not None and _ema200_v is not None) else None
    )
    L.append(_ev_row("ema20 > ema50  (derived)",     _bool_str(ema20_gt_50),
                     status=_field_status("ema20", ema20_gt_50, selected_setup, has_feat)))
    L.append(_ev_row("close_adj > ema50  (derived)", _bool_str(c_gt_50),
                     status=_field_status("ema50", c_gt_50, selected_setup, has_feat)))
    L.append(_ev_row("close_adj > ema200  (derived)",_bool_str(c_gt_200),
                     status=_field_status("ema200", c_gt_200, selected_setup, has_feat)))
    ev("ema_alignment_score",      "ema_alignment_score",
       note="100=EMA20>EMA50>EMA200 and price above all; 50=price>EMA200 only; 0=bearish")
    ev("ema20_slope",              "ema20_slope")
    ev("ema50_slope",              "ema50_slope")
    ev("distance_to_ema20_pct",    "distance_to_ema20_pct")
    ev("distance_to_ema50_pct",    "distance_to_ema50_pct")
    ev("distance_to_ema200_pct",   "distance_to_ema200_pct")
    blank()

    line("  ─── MOMENTUM ───────────────────────────────────────────────────────")
    ev("rsi14",                    "rsi14",   note="50-65=ideal bullish zone")
    ev("roc20",                    "roc20",   note=">0.08=strong; 0.03-0.08=moderate; <0=bearish")
    blank()

    line("  ─── VOLUME / VOLATILITY ────────────────────────────────────────────")
    ev("rvol20",                   "rvol20",  note=">=2.0=strong; 1.5-2.0=elevated; <1.2=low")
    ev("avg_volume_20d",           "avg_volume_20d")
    ev("avg_dollar_volume_20d",    "avg_dollar_volume_20d")
    ev("atr14",                    "atr14")
    ev("atr_pct",                  "atr_pct", note="ATR14 as fraction of price")
    ev("atr_compression_score",    "atr_compression_score")
    ev("volume_dry_up_score",      "volume_dry_up_score")
    ev("volume_expansion_score",   "volume_expansion_score")
    blank()

    line("  ─── SUPPORT / RESISTANCE / BASE ────────────────────────────────────")
    ev("support_level (adj)",      "support_level")
    ev("resistance_level (adj)",   "resistance_level")
    ev("next_resistance_level (adj)", "next_resistance_level")
    ev("swing_high (adj)",         "swing_high")
    ev("swing_low (adj)",          "swing_low")
    ev("base_high (adj)",          "base_high")
    ev("base_low (adj)",           "base_low")
    ev("range_duration (days)",    "range_duration")
    ev("range_width_pct",          "range_width_pct")
    ev("range_tightness_score",    "range_tightness_score")
    ev("consolidation_score",      "consolidation_score")
    ev("breakout_proximity",       "breakout_proximity",
       note="-1 to 0.5=ideal breakout zone; >1.5=extended")
    ev("pullback_depth_pct",       "pullback_depth_pct")
    ev("pullback_from_recent_high_pct", "pullback_from_recent_high_pct")
    ev("distance_from_52w_high_pct", "distance_from_52w_high_pct")
    blank()

    line("  ─── RELATIVE STRENGTH ──────────────────────────────────────────────")
    ev("relative_strength_vs_spy", "relative_strength_vs_spy")

    # Sector RS with benchmark validation (sector_etf_map DB table)
    # Use same fallback chain as ev() to source value from step4 when feat is NULL.
    sect_rs_val  = feat.get("sector_relative_strength")
    sect_rs_s4   = False
    if sect_rs_val is None:
        _v = _s4_expl.get("sector_rs") or _s4_expl.get("sector_relative_strength")
        if _v is None:
            for _expl in _all_s4_expls:
                if _expl is _s4_expl:
                    continue
                _v = _expl.get("sector_rs") or _expl.get("sector_relative_strength")
                if _v is not None:
                    break
        if _v is not None:
            sect_rs_val = _v
            sect_rs_s4  = True
    sector_etf  = sector_etf_db.get(sector, "—") if sector and sector != "—" else "—"
    sect_rs_status = _field_status(
        "sector_relative_strength", feat.get("sector_relative_strength"), selected_setup, has_feat
    )
    if feat.get("sector_relative_strength") is None and sect_rs_val is not None:
        sect_rs_status = (
            _ST_USED if (selected_setup and
                         "sector_relative_strength" in _SETUP_USES.get(selected_setup, set()))
            else _ST_AVAIL
        )
    if sect_rs_val is not None and sector_etf == "—":
        sect_rs_status = _ST_INVALID_ETF
    sect_etf_note = (
        f"benchmark={sector_etf}  sector={sector!r} (source: ticker_master)"
        if sector_etf != "—"
        else f"sector={sector!r} NOT in sector_etf_map — RS cannot be validated"
    )
    if sect_rs_s4:
        sect_etf_note += "  [source: step4_evidence]"
    L.append(_ev_row(
        f"sector_relative_strength [{sector_etf}]",
        _fmt(sect_rs_val, 4) if sect_rs_val is not None else None,
        status=sect_rs_status,
        note=sect_etf_note,
    ))
    blank()

    line("  ─── RISK / TRADE PLAN ──────────────────────────────────────────────")
    line("  (values from step5_proposals when available; step4_analysis otherwise)")
    tp_src = _ST_USED if step5_row else _ST_NOT_CALC
    L.append(_ev_row("entry_price_raw",        _fmt(entry_price, 2),  status=tp_src))
    L.append(_ev_row("stop_price_raw",         _fmt(stop_price, 2),   status=tp_src))
    L.append(_ev_row("target_price_raw",       _fmt(target_price, 2), status=tp_src))
    L.append(_ev_row("estimated_rr",           _fmt(estimated_rr, 2), status=tp_src))
    _sd_lbl = f"  [source={stop_dist_src}]" if stop_dist_src and stop_dist_src != "step4" else ""
    L.append(_ev_row("stop_distance_pct",      _fmt(stop_dist, 4) + _sd_lbl,
                     status=_ST_USED if stop_dist is not None else _ST_NOT_CALC))
    L.append(_ev_row("target_is_structural",   _bool_str(tgt_struct),
                     note="TRUE=structural target used; FALSE=fixed-R fallback"))
    L.append(_ev_row("support_level (step4)",  _fmt(selected_r4.get("support_level"), 2)))
    L.append(_ev_row("resistance_level (step4)", _fmt(selected_r4.get("resistance_level"), 2)))
    L.append(_ev_row("next_resistance (step4)", _fmt(selected_r4.get("next_resistance_level"), 2)))

    # RR recalc sanity check
    if entry_price and stop_price and target_price:
        try:
            rr_calc = (float(target_price) - float(entry_price)) / (
                float(entry_price) - float(stop_price)
            )
            L.append(_ev_row("estimated_rr (recalculated)", _fmt(rr_calc, 2),
                              note="(target-entry)/(entry-stop)"))
        except (ZeroDivisionError, TypeError, ValueError):
            pass
    blank()

    line("  ─── EARNINGS / MACRO ───────────────────────────────────────────────")
    ev_days = feat.get("days_to_earnings_bd")
    if ev_days is None:
        ev_days = selected_r4.get("earnings_days") if selected_r4 else None
    _ev_days_src = "feature_snapshot" if feat.get("days_to_earnings_bd") is not None else (
        "step4" if (selected_r4 and selected_r4.get("earnings_days") is not None) else None
    )
    if ev_days is None and earn_days_from_cal is not None:
        ev_days = earn_days_from_cal
        _ev_days_src = "earnings_calendar"
    if ev_days is not None:
        _ev_note = f"source={_ev_days_src}" if _ev_days_src else None
        L.append(_ev_row("days_to_earnings_bd", str(ev_days), status=_ST_AVAIL, note=_ev_note))
    else:
        L.append(_ev_row(
            "days_to_earnings_bd", "UNKNOWN", status=_ST_NULL,
            note="no earnings_calendar entry — cannot verify earnings risk; NULL != safe"
        ))
    ev("earnings_confidence",     "earnings_confidence")
    ev("macro_event_risk_flag",   "macro_event_risk_flag")
    earn_pen  = selected_r4.get("earnings_penalty")  if selected_r4 else None
    macro_pen = selected_r4.get("macro_penalty")     if selected_r4 else None
    L.append(_ev_row("earnings_penalty (step4)",
                     _fmt(earn_pen, 2) if earn_pen is not None else None))
    L.append(_ev_row("macro_penalty (step4)",
                     _fmt(macro_pen, 2) if macro_pen is not None else None))
    blank()

    # ══════════════════════════════════════════════════════════════════════════ #
    # SECTION 5: PROPOSAL SCORING & RANKING  (Step 5)
    # ══════════════════════════════════════════════════════════════════════════ #
    h2("SECTION 5 — PROPOSAL SCORING & RANKING  (M15 → step5_proposals)")

    if not step5_row:
        line("  No step5_proposals row matched setup_config_id=%r." % setup_config_id)
        if step5_alt:
            line("  However, other step5 rows exist for this ticker/date:")
            for _a in step5_alt[:5]:
                _a_scid = _a.get("setup_config_id", "?")
                _a_stype = _a.get("setup_type", "?")
                _a_disp  = _a.get("disposition", "?")
                _a_stop  = _a.get("stop_price_raw")
                _a_tgt   = _a.get("target_price_raw")
                _a_rr    = _a.get("estimated_rr")
                _a_rej   = _a.get("rejection_reason") or "—"
                line(f"    config={_a_scid!r}  type={_a_stype}  "
                     f"disp={_a_disp}  stop={_fmt(_a_stop,2)}  "
                     f"target={_fmt(_a_tgt,2)}  RR={_fmt(_a_rr,2)}  reject={_a_rej}")
            line("  [Root cause: setup_config_id mismatch — "
                 "dashboard row uses different config_id than step5 rows]")
        elif final_status in ("universal_failed", "no_setup_passed"):
            line("  [Expected: ticker was eliminated before Step 5 ran.]")
        else:
            line("  [Root cause: Step 5 was not run for this date, "
                 "or no proposals were written for this ticker.]")
        blank()
    else:
        kv("  Disposition",           s5_disp,
           "BUY | WATCHLIST_ONLY | REJECTED")
        kv("  Risk label",            step5_row.get("risk_label", "—"),
           "low / medium / high — assigned after setup validation")
        kv("  Risk score",            _fmt(step5_row.get("risk_score"), 2))
        kv("  selected_flag",         _bool_str(s5_sel_flag),
           "TRUE = final diversified shortlist member")
        blank()
        kv("  proposal_score_raw",    _fmt(step5_row.get("proposal_score_raw"), 2))
        kv("  proposal_score_final",  _fmt(step5_row.get("proposal_score_final"), 2))
        kv("  raw_rank",              str(s5_raw_rank) if s5_raw_rank is not None else "—")
        kv("  diversified_rank",      str(s5_div_rank) if s5_div_rank is not None else "—")
        blank()
        kv("  rejection_reason",      step5_row.get("rejection_reason") or "none (accepted)")
        blank()

        # Diversification
        top_n = risk_cfg.get("top_n", "—")
        line("  Diversification:")
        kv("    top_n",               str(top_n))
        kv("    sector_count_at_selection",
           str(step5_row.get("sector_count_at_selection"))
           if step5_row.get("sector_count_at_selection") is not None else "—")
        kv("    industry_count_at_selection",
           str(step5_row.get("industry_count_at_selection"))
           if step5_row.get("industry_count_at_selection") is not None else "—")
        blank()

        mech = step5_row.get("mechanical_explanation") or "—"
        line("  Mechanical explanation (stored):")
        line(f"    {mech}")
        blank()

        line("  Trade plan:")
        line(f"    entry_price_raw  = {_fmt(entry_price, 2)}")
        line(f"    stop_price_raw   = {_fmt(stop_price, 2)}")
        line(f"    target_price_raw = {_fmt(target_price, 2)}")
        line(f"    estimated_rr     = {_fmt(estimated_rr, 2)}")
        line(f"    target_is_structural = {_bool_str(tgt_struct)}")
        if entry_price and stop_price:
            try:
                _sd_frac = (float(entry_price) - float(stop_price)) / float(entry_price)
                line(f"    stop_distance    = {_fmt(_sd_frac * 100, 2)}% of entry")
                _atr_pct_val = feat.get("atr_pct")
                if _atr_pct_val is None and selected_r4:
                    _atr_pct_val = selected_r4.get("atr_pct")
                if _atr_pct_val is not None:
                    try:
                        _sda = _sd_frac / float(_atr_pct_val)
                        line(f"    stop_distance_atr = {_fmt(_sda, 2)} ATR"
                             f"  (stop_distance_pct / atr_pct)")
                        if _sda < 0.5:
                            line(f"    [WARNING] stop is tight versus ATR"
                                 f" (stop_distance_atr={_fmt(_sda, 2)} — typical minimum ~0.50)")
                    except (ZeroDivisionError, TypeError, ValueError):
                        pass
            except (ZeroDivisionError, TypeError, ValueError):
                pass
        blank()

    # ══════════════════════════════════════════════════════════════════════════ #
    # SECTION 6: REPORT INTEGRITY
    # ══════════════════════════════════════════════════════════════════════════ #
    h2("SECTION 6 — REPORT INTEGRITY")

    integrity_issues: list[str] = []
    tech_not_ready:   list[str] = []   # blocks ai_technical_review_readiness
    buy_not_ready:    list[str] = []   # blocks ai_final_buy_readiness

    if db_warnings:
        for w in db_warnings:
            integrity_issues.append(f"db_warning: {w}")

    if not step3_row:
        integrity_issues.append("no step3_candidates row found")
        tech_not_ready.append("missing universal eligibility data")

    if not step4_rows:
        integrity_issues.append("no step4_analysis rows found")
        tech_not_ready.append("no setup validation data")

    if not selected_setup:
        integrity_issues.append("selected_setup_mode could not be determined")
        tech_not_ready.append("selected setup unknown")

    if selected_setup and not selected_r4:
        integrity_issues.append(f"no step4 row for selected setup: {selected_setup}")
        tech_not_ready.append("selected setup evidence missing")

    if not step5_row:
        if final_status not in ("universal_failed", "no_setup_passed", "risk_plan_failed"):
            integrity_issues.append("no step5 row — unexpected given final_status")
        buy_not_ready.append("no step5 proposal row")

    if step5_row and not (entry_price and stop_price and target_price):
        integrity_issues.append("step5 row exists but missing stop/target/RR")
        buy_not_ready.append("trade plan incomplete (missing stop/target)")

    if market_reg == "UNKNOWN":
        integrity_issues.append(
            f"market_regime UNKNOWN in both feature_snapshot and step4_analysis "
            f"(source={market_reg_source}); NULL blocks BUY (AD-22.23)"
        )
        buy_not_ready.append("market_regime unknown — NULL blocks BUY")

    # Use calendar-direct data as fallback; if still unknown, flag NOT_READY.
    _earn_days_effective = earn_days if earn_days is not None else earn_days_from_cal
    _earn_too_close = False
    if _earn_days_effective is None:
        integrity_issues.append(
            "days_to_earnings_bd is NULL — no earnings_calendar entry; "
            "earnings risk unverified"
        )
        buy_not_ready.append("earnings status UNKNOWN — cannot verify earnings risk")
    else:
        # Calendar data available: check whether within avoidance window.
        _sel_earn_cfg = (selected_cfg or {}).get("earnings", {}) if selected_cfg else {}
        _avoid_bd = int(_sel_earn_cfg.get("avoid_within_bd", 5))
        if 0 <= _earn_days_effective <= _avoid_bd:
            _earn_too_close = True
            integrity_issues.append(
                f"earnings within avoidance window: days_to_earnings_bd={_earn_days_effective} "
                f"<= avoid_within_bd={_avoid_bd}"
            )
            buy_not_ready.append(
                f"earnings too close: {_earn_days_effective} bd away "
                f"(within configured avoid_within_bd={_avoid_bd})"
            )
            if earn_days is None:
                buy_not_ready.append(
                    "pipeline earnings_penalty was NOT applied (data missing at pipeline run time)"
                )

    if sector_etf == "—" and selected_setup == "trend_continuation":
        integrity_issues.append(
            "sector benchmark ETF missing — sector_relative_strength invalid "
            "for trend_continuation scoring"
        )
        tech_not_ready.append("sector RS invalid for selected setup")

    if step5_alt and not step5_row:
        integrity_issues.append(
            "step5 rows exist for this ticker/date but under different setup_config_id — "
            "see Section 5 for details"
        )

    # Contradiction: step5 says BUY but final_status logic disagrees
    has_contradiction = (
        step5_row
        and step5_row.get("disposition") == "BUY"
        and final_status in ("universal_failed", "no_setup_passed")
    )
    if has_contradiction:
        integrity_issues.append(
            "contradictory status: step5.disposition=BUY but "
            f"final_status={final_status}"
        )

    # Determine integrity label
    is_invalid  = has_contradiction or any("DB fetch error" in w for w in db_warnings)
    is_complete = not integrity_issues
    if is_invalid:
        integrity_label = "INVALID"
    elif is_complete:
        integrity_label = "COMPLETE"
    else:
        integrity_label = "PARTIAL"

    # Two separate readiness flags
    ai_tech_ready = "NOT_READY" if tech_not_ready else "READY"
    ai_buy_ready  = "NOT_READY" if (tech_not_ready or buy_not_ready) else "READY"

    # ── Machine-readable status fields ─────────────────────────────────────── #

    # earnings_status: KNOWN if calendar data resolves (feature_snapshot OR direct query)
    if _earn_days_effective is not None:
        earnings_status = "KNOWN"
    else:
        earnings_status = "UNKNOWN"

    # earnings_penalty_status
    _ep_val = selected_r4.get("earnings_penalty") if selected_r4 else None
    if _earn_days_effective is None:
        earnings_penalty_status = "UNKNOWN_NOT_SAFE"
    elif earn_days is None and _earn_days_effective is not None:
        # Feature snapshot missed earnings; calendar resolved it.
        # If earnings are within the avoidance window the penalty was missed (genuine gap).
        # If safe distance confirmed by calendar, the outcome is correct regardless.
        earnings_penalty_status = (
            "NOT_APPLIED_PIPELINE_GAP" if _earn_too_close else "NOT_APPLIED_CONFIRMED_SAFE"
        )
    else:
        try:
            earnings_penalty_status = (
                "APPLIED"
                if (_ep_val is not None and float(_ep_val) != 0.0)
                else "NOT_APPLIED_CONFIRMED_SAFE"
            )
        except (TypeError, ValueError):
            earnings_penalty_status = "UNKNOWN_NOT_SAFE"

    mechanical_disposition = s5_disp if step5_row else "—"

    # Machine-readable reason codes (subset of buy_not_ready, but with stable enum values)
    final_buy_not_ready_reason_codes: list[str] = []
    if _earn_days_effective is None:
        final_buy_not_ready_reason_codes.append("earnings_status_unknown")
    if _earn_too_close:
        final_buy_not_ready_reason_codes.append("earnings_too_close")
    if market_reg == "UNKNOWN":
        final_buy_not_ready_reason_codes.append("market_regime_unknown")
    if step5_row and not (entry_price and stop_price and target_price):
        final_buy_not_ready_reason_codes.append("missing_stop_target_rr")
    if not step5_row and final_status not in ("universal_failed", "no_setup_passed"):
        final_buy_not_ready_reason_codes.append("no_step5_proposal")
    if db_warnings and any("mismatch" in w for w in db_warnings):
        final_buy_not_ready_reason_codes.append("config_mismatch")
    if tech_not_ready:
        final_buy_not_ready_reason_codes.append("technical_review_not_ready")

    # live_action: BUY only when all hard conditions are met
    _live_blocks = {
        c for c in final_buy_not_ready_reason_codes
        if c in ("earnings_status_unknown", "earnings_too_close",
                  "market_regime_unknown", "missing_stop_target_rr",
                  "no_step5_proposal", "technical_review_not_ready")
    }
    if (mechanical_disposition == "BUY" and s5_sel_flag
            and entry_price and stop_price and target_price
            and not _live_blocks):
        live_action = "BUY"
    elif mechanical_disposition in ("REJECTED",) and final_status in (
            "universal_failed", "no_setup_passed"):
        live_action = "REJECT"
    elif mechanical_disposition == "—" and final_status in (
            "universal_failed", "no_setup_passed"):
        live_action = "REJECT"
    else:
        live_action = "HOLD_REVIEW_REQUIRED"

    # ── Output ─────────────────────────────────────────────────────────────── #
    kv("  report_integrity",                 integrity_label)
    kv("  mechanical_disposition",           mechanical_disposition,
       "BUY | WATCHLIST_ONLY | REJECTED | — (mechanical pipeline result)")
    kv("  selected_flag",                    _bool_str(s5_sel_flag),
       "TRUE = final diversified shortlist member")
    kv("  earnings_status",                  earnings_status,
       "KNOWN=calendar confirmed; UNKNOWN=no calendar entry; NOT_APPLICABLE=not relevant")
    kv("  earnings_penalty_status",          earnings_penalty_status,
       "APPLIED=penalty applied; NOT_APPLIED_CONFIRMED_SAFE=safe distance confirmed; "
       "NOT_APPLIED_PIPELINE_GAP=earnings close but penalty missed; UNKNOWN_NOT_SAFE=unverifiable")
    kv("  ai_technical_review_readiness",    ai_tech_ready,
       "READY = step3+step4 evidence complete enough for technical review")
    kv("  ai_final_buy_readiness",           ai_buy_ready,
       "READY = step5 BUY + trade plan + known regime + known earnings")
    kv("  final_trade_decision",             final_trade_decision,
       "BUY=entry confirmed; WAIT_FOR_BREAKOUT=price not yet above resistance; "
       "WAIT_FOR_PULLBACK_CONFIRMATION=rebound not confirmed; "
       "WAIT_FOR_RISK_PLAN_FIX=stop too tight vs ATR; "
       "WATCHLIST_ONLY=blocked; REJECTED=failed; —=no step5 data")
    kv("  effective_disposition",            effective_disposition,
       "P0 authoritative action status: WAIT_* FTDs map to WATCHLIST_ONLY")
    kv("  effective_risk_label",             effective_risk_label,
       "high when FTD≠BUY; reflects entry-readiness not just setup quality")
    blank()

    if ftd_reason_codes:
        line("  ftd_reason_codes (from final_trade_decision gate):")
        for _code in ftd_reason_codes:
            line(f"    - {_code}")
        blank()

    if final_buy_not_ready_reason_codes:
        line("  final_buy_not_ready_reasons (machine-readable):")
        for _code in final_buy_not_ready_reason_codes:
            line(f"    - {_code}")
        blank()

    if integrity_issues:
        line("  Issues:")
        for issue in integrity_issues:
            line(f"    * {issue}")
        blank()

    if tech_not_ready:
        line("  Technical review NOT_READY reasons:")
        for r in tech_not_ready:
            line(f"    * {r}")
        blank()

    if buy_not_ready:
        line("  Final BUY NOT_READY reasons:")
        for r in buy_not_ready:
            line(f"    * {r}")
        blank()

    # ══════════════════════════════════════════════════════════════════════════ #
    # VERDICT
    # ══════════════════════════════════════════════════════════════════════════ #
    h1(f"VERDICT — {ticker} / {signal_date.isoformat()} / {selected_setup or '?'}")

    if final_status == "selected_proposal":
        line(f"  SELECTED ✓  {ticker} accepted into diversified shortlist.")
        line(f"              raw_rank={s5_raw_rank}  diversified_rank={s5_div_rank}")
        line(f"              entry={_fmt(entry_price, 2)}  "
             f"stop={_fmt(stop_price, 2)}  "
             f"target={_fmt(target_price, 2)}  "
             f"RR={_fmt(estimated_rr, 2)}")
        line(f"  mechanical_disposition={mechanical_disposition}"
             f"  earnings_status={earnings_status}"
             f"  ai_final_buy_readiness={ai_buy_ready}")
        line(f"  final_trade_decision={final_trade_decision}"
             f"  effective_disposition={effective_disposition}")
        if final_trade_decision.startswith("WAIT_"):
            line(f"  [WAIT — entry condition not yet met; monitor until triggered]")
            if ftd_reason_codes:
                line(f"  Gate reasons: {', '.join(ftd_reason_codes)}")
        elif final_trade_decision not in ("BUY", "REJECTED", "WATCHLIST_ONLY", "—"):
            line(f"  [HOLD_REVIEW_REQUIRED — do not enter until manual review clears all flags]")
            if final_buy_not_ready_reason_codes:
                line(f"  Blocking reasons: {', '.join(final_buy_not_ready_reason_codes)}")
    elif final_status == "external_risk_blocked":
        line(f"  WATCHLIST ONLY  {ticker} passed setup validation but is blocked by "
             f"macro/earnings risk.")
    elif final_status == "not_selected":
        rej = (step5_row or {}).get("rejection_reason") or "below diversified top-N"
        line(f"  NOT SELECTED  {ticker}: {rej}.")
    elif final_status == "universal_failed":
        fails = step3_row.get("eligibility_fail_reasons") or []
        if not isinstance(fails, list):
            fails = [str(fails)]
        line(f"  REJECTED  {ticker} failed universal eligibility: "
             f"{'; '.join(str(f) for f in fails[:2]) or 'unknown reason'}.")
    elif final_status == "no_setup_passed":
        line(f"  NOT SELECTED  {ticker}: no setup mode passed Step 4 validation.")
    elif final_status == "risk_plan_failed":
        line(f"  NOT SELECTED  {ticker}: setup validation passed but "
             f"trade plan (stop/target/RR) could not be computed.")
    else:
        line(f"  UNKNOWN STATUS  {ticker}: final_status={final_status}.")

    blank()
    h1("END OF REPORT")

    return "\n".join(L).encode("utf-8"), filename