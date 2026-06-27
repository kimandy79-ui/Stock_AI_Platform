#!/usr/bin/env python3
"""CLI: Setup-mode funnel diagnostics for a given signal date.

Usage examples::

    python tools/run_funnel_diagnostics.py --date 2026-06-15
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --db-role debug
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --setup-type breakout
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --run-id <uuid>
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --json-out out.json
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --top-n-borderline 5

Note: --strategy and --include-projected-step4 are accepted but ignored (legacy).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from app.services.diagnostics.funnel_diagnostics import (
    SetupModeFunnelDiagnosticsService,
    ACTIVE_SETUP_TYPES,
)


# ---------------------------------------------------------------------------
# Inline DB manager fallback
# ---------------------------------------------------------------------------
class _SimpleDbManager:
    def __init__(self, prod_path: str, debug_path: str, simulation_path: str) -> None:
        import duckdb
        self._duckdb = duckdb
        self._paths = {"prod": prod_path, "debug": debug_path, "simulation": simulation_path}

    def connect(self, db_role: str, read_only: bool = False):  # noqa: ANN201
        path = self._paths.get(db_role)
        if path is None:
            raise ValueError(f"Unknown db_role: {db_role!r}")
        return self._duckdb.connect(path, read_only=read_only)


def _get_db_manager(args: argparse.Namespace) -> Any:
    try:
        from app.database.duckdb_manager import get_db_manager  # type: ignore[import]
        return get_db_manager()
    except (ImportError, AttributeError):
        pass
    try:
        from app.database.duckdb_manager import DuckDBManager  # type: ignore[import]
        try:
            return DuckDBManager(
                prod_path=args.prod_db,
                debug_path=args.debug_db,
                simulation_path=args.sim_db,
            )
        except TypeError:
            try:
                return DuckDBManager()
            except TypeError:
                pass
    except (ImportError, AttributeError):
        pass
    return _SimpleDbManager(
        prod_path=args.prod_db,
        debug_path=args.debug_db,
        simulation_path=args.sim_db,
    )


# ---------------------------------------------------------------------------
# Arg helpers
# ---------------------------------------------------------------------------
def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {s!r}: expected YYYY-MM-DD") from exc


def _bool_arg(s: str) -> bool:
    if s.lower() in ("true", "1", "yes"):
        return True
    if s.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"Expected true/false, got {s!r}")


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------
_W = 72


def _h1(title: str) -> None:
    print()
    print("=" * _W)
    print(f"  {title}")
    print("=" * _W)


def _h2(title: str) -> None:
    bar = "─" * max(0, _W - len(title) - 6)
    print()
    print(f"  ── {title} {bar}")


def _fmt(v: Any, decimals: int = 1) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _pct_str(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(v)


def _pct_frac(n: Any, d: Any) -> float:
    try:
        return float(n) / float(d) if d else 0.0
    except (TypeError, ValueError):
        return 0.0


def _fmt_example(
    actual: Any,
    threshold: Any,
    direction: str | None,
) -> str:
    """Format a comparison as 'actual X < min Y' or 'actual X > max Y'."""
    if actual is None:
        return ""
    if threshold is None:
        return f"actual={actual}"
    if direction == "<":
        return f"actual {actual} < min {threshold}"
    if direction == ">":
        return f"actual {actual} > max {threshold}"
    return f"{actual} vs {threshold}"


def _row(*cells: str, widths: list[int]) -> str:
    parts = [f"  {str(cells[0]):<{widths[0]}}"]
    for c, w in zip(cells[1:], widths[1:]):
        parts.append(f"  {str(c):>{w}}")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Section printers
# ---------------------------------------------------------------------------
def _print_routing(rpt: dict) -> None:
    _h2("1. ROUTING SUMMARY")
    rs = rpt.get("routing_summary", {})
    total  = rs.get("total_universe", 0)
    passed = rs.get("passed_eligibility", 0)
    routed = rs.get("routed", 0)
    print(f"    Universe:            {total:>6}")
    print(f"    Passed eligibility:  {passed:>6}  ({_pct_str(_pct_frac(passed, total))})")
    print(f"    Failed eligibility:  {rs.get('failed_eligibility', 0):>6}")
    print(f"    Routed (any setup):  {routed:>6}  ({_pct_str(_pct_frac(routed, passed))} of eligible)")
    print(f"    Not routed:          {rs.get('not_routed', 0):>6}")
    print(f"    Multi-routed:        {rs.get('multi_routed', 0):>6}  (routed to 2+ setups)")
    by_setup = rs.get("by_setup", {})
    if by_setup:
        print()
        print("    Routed count by setup type:")
        for st in ACTIVE_SETUP_TYPES:
            print(f"      {st:<28}  {by_setup.get(st, 0):>5}")


def _print_setup_funnel(rpt: dict) -> None:
    _h2("2. SETUP-MODE FUNNEL")
    funnel = rpt.get("setup_funnel", [])
    if not funnel:
        print("    (no data)")
        return
    ws = [24, 7, 9, 9, 7, 7, 8]
    print(_row("Setup", "Routed", "Val.Pass", "Val.Fail", "Pass%", "Step5", "Selected",
               widths=ws))
    print("  " + "─" * (_W - 2))
    for f in funnel:
        print(_row(
            f["setup_type"],
            str(f["routed_count"]),
            str(f["validator_pass_count"]),
            str(f["validator_fail_count"]),
            _pct_str(f["pass_rate"]),
            str(f["step5_count"]),
            str(f["selected_count"]),
            widths=ws,
        ))


def _print_failure_reasons(rpt: dict) -> None:
    _h2("3a. VALIDATOR FAILURE REASONS  (step4, by setup, sorted by frequency)")
    reasons = rpt.get("failure_reasons", [])
    if not reasons:
        print("    (no failures or no data)")
        return
    current_setup: str | None = None
    for r in reasons:
        st = r["setup_type"]
        if st != current_setup:
            print()
            print(f"    {st}:")
            current_setup = st
        pct_s = _pct_str(r["pct_of_setup_failures"])
        rule = r["failure_reason"] or "unknown"
        ex_s = _fmt_example(r.get("actual_value"), r.get("threshold"), r.get("direction"))
        tks = r.get("sample_tickers") or []
        ticker_s = f"  [{', '.join(tks[:3])}]" if tks else ""
        suffix = (f"  {ex_s}" if ex_s else "") + ticker_s
        print(f"      {rule:<44}  {r['count']:>5}  ({pct_s}){suffix}")


def _print_s5_rejections(rpt: dict) -> None:
    _h2("3b. STEP5 REJECTION REASONS  (grouped, all dispositions)")
    rows = rpt.get("s5_rejection_reasons", [])
    if not rows:
        print("    (no step5 rejection reasons found)")
        return
    print(f"    {'Reason':<44}  {'Count':>6}  {'% total':>8}  {'Example (actual vs threshold)':}")
    print("  " + "─" * (_W - 2))
    for r in rows:
        rule = r["reason"] or "unknown"
        pct_s = _pct_str(r["pct_of_total"])
        ex_s = _fmt_example(r.get("actual_value"), r.get("threshold"), r.get("direction"))
        tks = r.get("sample_tickers") or []
        ticker_s = f"  [{', '.join(tks[:3])}]" if tks else ""
        print(f"    {rule:<44}  {r['count']:>6}  {pct_s:>8}  {ex_s}{ticker_s}")


def _print_warnings(rpt: dict) -> None:
    warns = rpt.get("diagnostic_warnings", [])
    if not warns:
        return
    print()
    print("  ╔" + "═" * (_W - 4) + "╗")
    print(f"  ║  DIAGNOSTIC WARNINGS ({len(warns)})".ljust(_W - 2) + "║")
    print("  ╠" + "═" * (_W - 4) + "╣")
    for w in warns:
        print(f"  ║  [{w.get('severity', 'warn').upper()}] {w['code']}".ljust(_W - 2) + "║")
        msg = w.get("message", "")
        # Word-wrap to fit box
        while msg:
            chunk = msg[:_W - 10]
            print(f"  ║    {chunk}".ljust(_W - 2) + "║")
            msg = msg[len(chunk):]
    print("  ╚" + "═" * (_W - 4) + "╝")


def _print_routing_detail(rpt: dict) -> None:
    _h2("4. ROUTING DIAGNOSTICS  (routed vs validator coverage)")
    rs  = rpt.get("routing_summary", {})
    sf  = {f["setup_type"]: f for f in rpt.get("setup_funnel", [])}
    by_setup = rs.get("by_setup", {})
    print(f"    {'Setup':<28}  {'Routed':>7}  {'Step4':>7}  {'Coverage':>9}")
    print("  " + "─" * (_W - 2))
    for st in ACTIVE_SETUP_TYPES:
        r_cnt  = by_setup.get(st, 0)
        s4_cnt = sf.get(st, {}).get("step4_count", 0)
        cov    = _pct_str(_pct_frac(s4_cnt, r_cnt))
        print(f"    {st:<28}  {r_cnt:>7}  {s4_cnt:>7}  {cov:>9}")
    multi = rs.get("multi_routed", 0)
    if multi:
        print()
        print(f"    NOTE: {multi} tickers multi-routed — breakout may absorb "
              f"consolidation-like candidates if both qualify.")


def _print_evidence(rpt: dict) -> None:
    _h2("5. EVIDENCE SUMMARIES  (step4 passed rows)")
    evidence = rpt.get("evidence_summaries", {})
    fields = [
        ("setup_score",             "setup_score",          2),
        ("rvol",                    "rvol",                  2),
        ("atr_pct",                 "atr_pct",               4),
        ("ema20_distance_pct",      "ema20_dist%",           4),
        ("ema50_distance_pct",      "ema50_dist%",           4),
        ("estimated_rr",            "estimated_rr",          2),
        ("stop_distance_pct",       "stop_dist%",            4),
        ("range_width_pct",         "range_width%",          4),
        ("days_in_range",           "days_in_range",         1),
        ("price_position_in_range", "price_pos_in_range",    3),
    ]
    cb_fields = [
        ("support_found",    "support_found",    2),
        ("resistance_found", "resistance_found", 2),
    ]
    for st in ACTIVE_SETUP_TYPES:
        ev = evidence.get(st, {})
        n = ev.get("setup_score", {}).get("n", 0)
        if not n:
            continue
        print()
        print(f"    {st}  (n={n})")
        print(f"      {'Field':<24}  {'n':>4}  {'min':>7}  {'p25':>7}  "
              f"{'p50':>7}  {'p75':>7}  {'max':>7}  {'mean':>7}")
        print("      " + "─" * 64)
        for key, label, dec in fields + (cb_fields if st == "consolidation_base" else []):
            s = ev.get(key, {})
            if not s or s.get("n", 0) == 0:
                continue
            print(f"      {label:<24}  {s['n']:>4}  "
                  f"{_fmt(s['min'], dec):>7}  {_fmt(s['p25'], dec):>7}  "
                  f"{_fmt(s['p50'], dec):>7}  {_fmt(s['p75'], dec):>7}  "
                  f"{_fmt(s['max'], dec):>7}  {_fmt(s['mean'], dec):>7}")


def _print_borderline(rpt: dict) -> None:
    _h2("6. BORDERLINE FAILURES  (highest-scoring fails per setup)")
    bl = rpt.get("borderline_failures", {})
    for st in ACTIVE_SETUP_TYPES:
        rows = bl.get(st, [])
        if not rows:
            continue
        print()
        print(f"    {st}:")
        print(f"      {'Ticker':<8}  {'Score':>6}  {'Failed Rule':<40}  Comparison")
        print("      " + "─" * 74)
        for r in rows:
            ex_s = _fmt_example(r.get("actual_value"), r.get("threshold"), r.get("direction"))
            print(f"      {r['ticker']:<8}  {_fmt(r['setup_score'], 4):>6}  "
                  f"{(r['failed_rule'] or '—'):<40}  {ex_s or '—'}")


def _print_layers(rpt: dict) -> None:
    _h2("7. FAILURE LAYERS  (candidate drop-off through pipeline)")
    layers = rpt.get("failure_layers", {})
    total  = rpt.get("total_s3", 0)
    order = [
        ("ineligible",                   "Ineligible (step3 hard gates)"),
        ("not_routed",                   "Eligible but not routed (no setup match)"),
        ("routed_all_validators_failed", "Routed → all validators failed (step4)"),
        ("validator_passed_no_step5",    "Validator passed → no step5 row"),
        ("step5_rejected",               "Step5 REJECTED (setup failed in step5)"),
        ("step5_watchlist_not_selected", "Step5 WATCHLIST_ONLY → not selected"),
        ("step5_buy_diversity_rejected", "Step5 BUY → diversity-rejected"),
        ("selected",                     "SELECTED (final shortlist)"),
    ]
    print(f"    {'Layer':<50}  {'Count':>6}  {'% universe':>11}")
    print("  " + "─" * (_W - 2))
    for key, label in order:
        cnt = layers.get(key, 0)
        print(f"    {label:<50}  {cnt:>6}  {_pct_str(_pct_frac(cnt, total)):>11}")


def _print_diag_rows(rows: list[dict], run_id: str) -> None:
    """Print a compact summary of pipeline_run_diagnostics rows."""
    _h2(f"9. PIPELINE RUN DIAGNOSTICS  ({len(rows)} rows  run_id={run_id[:12]}…)")

    if not rows:
        print("    (no pipeline_run_diagnostics rows found for this run_id)")
        return

    by_key: dict[tuple, float | None] = {}
    for r in rows:
        k = (r.get("step_name", ""), r.get("setup_type"), r.get("metric_name", ""))
        by_key[k] = r.get("metric_value")

    ELIG  = "step3_universal_eligibility"
    ROUT  = "step3_routing"
    VALID = "step4_setup_validation"
    RISK  = "step5_risk_label"
    PROP  = "step5_proposals"

    total  = by_key.get((ELIG, None, "eligibility.total_input"), 0) or 0
    passed = by_key.get((ELIG, None, "eligibility.passed"), 0) or 0
    failed = by_key.get((ELIG, None, "eligibility.failed"), 0) or 0
    not_r  = by_key.get((ROUT, None, "routing.not_routed"), 0) or 0
    inelig = by_key.get((ROUT, None, "routing.ineligible"), 0) or 0
    f_rdy  = by_key.get((ELIG, None, "eligibility.feature_ready"))
    f_miss = by_key.get((ELIG, None, "eligibility.feature_missing"))

    print(f"    Eligibility + Routing:")
    print(f"      total_input      {int(total):>6}")
    print(f"      passed           {int(passed):>6}  ({_pct_str(_pct_frac(passed, total))})")
    print(f"      failed           {int(failed):>6}")
    print(f"      not_routed       {int(not_r):>6}")
    print(f"      ineligible       {int(inelig):>6}")
    if f_rdy is not None:
        print(f"      feature_ready    {int(f_rdy):>6}")
        print(f"      feature_missing  {int(f_miss or 0):>6}")

    print()
    print(f"    Routing by setup_type:")
    for st in ACTIVE_SETUP_TYPES:
        cnt = by_key.get((ROUT, st, "routing.routed"), 0) or 0
        print(f"      {st:<28}  {int(cnt):>5}")

    print()
    print(f"    Validation  pass / fail:")
    for st in ACTIVE_SETUP_TYPES:
        p = by_key.get((VALID, st, "validation.passed"), 0) or 0
        f = by_key.get((VALID, st, "validation.failed"), 0) or 0
        total_v = (p or 0) + (f or 0)
        rate = _pct_str(_pct_frac(p, total_v)) if total_v else "—"
        print(f"      {st:<28}  passed={int(p):>5}  failed={int(f):>5}  ({rate})")

    fail_by_setup: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        mn = r.get("metric_name") or ""
        if "validation.failure_reason." in mn and r.get("metric_value"):
            st = r.get("setup_type") or "unknown"
            reason = mn.split("validation.failure_reason.", 1)[1]
            fail_by_setup.setdefault(st, []).append((reason, float(r["metric_value"])))
    if fail_by_setup:
        print()
        print(f"    Top failure reasons:")
        for st in ACTIVE_SETUP_TYPES:
            reasons = sorted(fail_by_setup.get(st, []), key=lambda x: -x[1])[:5]
            if reasons:
                print(f"      {st}:")
                for reason, cnt in reasons:
                    print(f"        {reason:<46}  {int(cnt):>4}")

    print()
    print(f"    Step5 proposals:")
    buy     = by_key.get((PROP, None, "proposal.buy_eligible"), 0) or 0
    wl      = by_key.get((PROP, None, "proposal.watchlist"), 0) or 0
    rej     = by_key.get((PROP, None, "proposal.rejected"), 0) or 0
    total_p = by_key.get((PROP, None, "proposal.final_count"), 0) or 0
    print(f"      BUY:       {int(buy):>5}")
    print(f"      WATCHLIST: {int(wl):>5}")
    print(f"      REJECTED:  {int(rej):>5}")
    print(f"      total:     {int(total_p):>5}")

    print()
    print(f"    Risk labels:")
    for label in ("low", "medium", "high"):
        cnt = by_key.get((RISK, None, f"risk_label.{label}"), 0) or 0
        if cnt:
            print(f"      {label:<10}  {int(cnt):>5}")

    # Bottleneck: largest drop
    drops = []
    for st in ACTIVE_SETUP_TYPES:
        routed = int(by_key.get((ROUT, st, "routing.routed"), 0) or 0)
        p = int(by_key.get((VALID, st, "validation.passed"), 0) or 0)
        f = int(by_key.get((VALID, st, "validation.failed"), 0) or 0)
        if routed and f:
            drops.append((f, st, routed, p, f))
    if drops:
        drops.sort(reverse=True)
        print()
        print(f"    Bottlenecks (largest validator fail counts):")
        for _, st, routed, p, f in drops[:3]:
            print(f"      {st:<28}  routed={routed:>5}  passed={p:>5}  failed={f:>5}")


def _print_ftd(rpt: dict) -> None:
    _h2("8. FINAL TRADE DECISIONS  (from mechanical_explanation P0)")
    ftd = rpt.get("final_trade_decisions", {})
    if not ftd:
        print("    (no step5 proposals or final_trade_decision not yet populated)")
        return
    order = [
        "BUY",
        "WAIT_FOR_BREAKOUT",
        "WAIT_FOR_PULLBACK_CONFIRMATION",
        "WAIT_FOR_RISK_PLAN_FIX",
        "WATCHLIST_ONLY",
        "REJECTED",
        "unknown",
    ]
    total = sum(ftd.values())
    shown: set[str] = set()
    for key in order:
        cnt = ftd.get(key, 0)
        if cnt:
            print(f"    {key:<44}  {cnt:>5}  ({_pct_str(_pct_frac(cnt, total))})")
        shown.add(key)
    for key in sorted(ftd):
        if key not in shown and ftd[key]:
            cnt = ftd[key]
            print(f"    {key:<44}  {cnt:>5}  ({_pct_str(_pct_frac(cnt, total))})")


def _print_report(rpt: dict) -> None:
    run_id = rpt.get("run_id") or "?"
    run_id_short = (run_id[:12] + "…") if len(run_id) > 12 else run_id
    _h1(
        f"FUNNEL DIAGNOSTICS  |  {rpt.get('signal_date', '?')}  |  "
        f"db_role={rpt.get('db_role', '?')}  |  run_id={run_id_short}"
    )
    print(f"  Rows loaded:  step3={rpt.get('total_s3', 0)}  "
          f"step4={rpt.get('total_s4', 0)}  step5={rpt.get('total_s5', 0)}")

    _print_warnings(rpt)
    _print_routing(rpt)
    _print_setup_funnel(rpt)
    _print_failure_reasons(rpt)
    _print_s5_rejections(rpt)
    _print_routing_detail(rpt)
    _print_evidence(rpt)
    _print_borderline(rpt)
    _print_layers(rpt)
    _print_ftd(rpt)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Setup-mode funnel diagnostics: step3→step4→step5 candidate yield analysis."
        )
    )
    parser.add_argument("--date", required=True, type=_parse_date, metavar="YYYY-MM-DD")
    parser.add_argument("--db-role", choices=["prod", "debug"], default="prod")
    parser.add_argument("--run-id", default=None,
                        help="Pipeline run_id (default: latest for --date).")
    parser.add_argument("--setup-type", default=None, choices=list(ACTIVE_SETUP_TYPES),
                        help="Filter report to one setup type.")
    parser.add_argument("--top-n-borderline", type=int, default=10, metavar="N",
                        help="Top N near-miss failures per setup (default: 10).")
    parser.add_argument("--json-out", default=None, metavar="PATH",
                        help="Write full JSON payload to file.")
    parser.add_argument("--prod-db",  default="data/duckdb/prod.duckdb")
    parser.add_argument("--debug-db", default="data/duckdb/debug.duckdb")
    parser.add_argument("--sim-db",   default="data/duckdb/simulation.duckdb")
    # Legacy args: accepted, ignored
    parser.add_argument("--strategy", default="all", help=argparse.SUPPRESS)
    parser.add_argument("--include-projected-step4", type=_bool_arg,
                        default=True, help=argparse.SUPPRESS)

    args = parser.parse_args()

    db_manager = _get_db_manager(args)
    svc = SetupModeFunnelDiagnosticsService(db_manager=db_manager)

    result = svc.build_report(
        signal_date=args.date,
        db_role=args.db_role,
        run_id=args.run_id,
        top_n_borderline=args.top_n_borderline,
        setup_type_filter=args.setup_type,
    )

    if result.status == "failed":
        print(f"[FAILED] {'; '.join(result.errors)}", file=sys.stderr)
        sys.exit(1)

    for w in result.warnings:
        print(f"[WARNING] {w}", file=sys.stderr)

    rpt = result.metadata.get("report", {})
    _print_report(rpt)

    # Read pipeline_run_diagnostics rows for the resolved run_id
    pipeline_run_id = rpt.get("run_id") or result.run_id
    diag_rows: list[dict] = []
    if pipeline_run_id:
        try:
            diag_rows = svc.read(
                run_id=pipeline_run_id,
                signal_date=args.date,
                db_role=args.db_role,
            )
        except Exception:  # noqa: BLE001
            pass
    _print_diag_rows(diag_rows, pipeline_run_id or "?")

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": pipeline_run_id,
            "signal_date": args.date.isoformat(),
            "db_role": args.db_role,
            "status": result.status,
            "warnings": result.warnings,
            "report": rpt,
            "pipeline_run_diagnostics": diag_rows,
        }
        out_path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"[INFO] JSON written to {out_path}")


if __name__ == "__main__":
    main()
