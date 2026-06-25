#!/usr/bin/env python3
"""CLI: Run funnel diagnostics for a given signal date and strategy.

Usage examples::

    python tools/run_funnel_diagnostics.py --date 2026-06-15 --db-role prod --strategy all
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --db-role prod --strategy normal
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --db-role debug --strategy conservative
    python tools/run_funnel_diagnostics.py --date 2026-06-15 --db-role prod --strategy all --json-out out.json
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

from app.services.diagnostics.funnel_diagnostics import FunnelDiagnosticsService


# ---------------------------------------------------------------------------
# Inline DB manager — used when real Module 02 is unavailable or path override needed
# ---------------------------------------------------------------------------
class _SimpleDbManager:
    """Minimal DuckDB manager for CLI use only."""

    def __init__(self, prod_path: str, debug_path: str, simulation_path: str) -> None:
        import duckdb
        self._duckdb = duckdb
        self._paths: dict[str, str] = {
            "prod": prod_path,
            "debug": debug_path,
            "simulation": simulation_path,
        }

    def connect(self, db_role: str, read_only: bool = False):  # noqa: ANN201
        path = self._paths.get(db_role)
        if path is None:
            raise ValueError(f"Unknown db_role: {db_role!r}")
        return self._duckdb.connect(path, read_only=read_only)


def _get_db_manager(args: argparse.Namespace) -> Any:
    """Return a db_manager: try real Module 02 first, fall back to inline."""
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
# Helpers
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
# Formatting
# ---------------------------------------------------------------------------
def _print_funnel(result_dict: dict[str, Any]) -> None:
    sid = result_dict["signal_date"]
    cfg = result_dict["strategy_config_id"]
    name = result_dict["strategy_name"]
    db_role = result_dict["db_role"]

    print()
    print("=" * 72)
    print(f"  STRATEGY: {name}  |  config_id: {cfg}")
    print(f"  signal_date: {sid}  |  db_role: {db_role}")
    print("=" * 72)

    stages = result_dict.get("stages", [])
    if stages:
        print()
        print("  STEP 3 FUNNEL")
        print(f"  {'Order':>5}  {'Stage Key':<38}  {'Pass':>7}  {'Fail':>7}  {'Rate':>7}  {'Threshold'}")
        print(f"  {'-'*5}  {'-'*38}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*12}")
        for s in stages:
            if s["stage_key"] == "step3_fail_reason_counts":
                rc = s.get("threshold") or {}
                if any(v > 0 for v in rc.values()):
                    print()
                    print("  Hard filter fail reasons:")
                    for label, count in rc.items():
                        if count > 0:
                            print(f"    {label:<38} {count:>7}")
                continue
            pass_c = s["pass_count"] if s["pass_count"] is not None else "-"
            fail_c = s["fail_count"] if s["fail_count"] is not None else "-"
            rate = f"{s['pass_rate']:.1%}" if s["pass_rate"] is not None else "-"
            thresh = str(s["threshold"]) if s["threshold"] is not None else ""
            print(f"  {s['stage_order']:>5}  {s['stage_key']:<38}  {str(pass_c):>7}  {str(fail_c):>7}  {rate:>7}  {thresh}")

    s4 = result_dict.get("step4_observed", {})
    print()
    print("  STEP 4 OBSERVED")
    print(f"    Rows in step4_analysis:     {s4.get('step4_analysis_rows', 0)}")
    setup_counts = s4.get("setup_type_counts", {})
    if setup_counts:
        print("    Setup type distribution:")
        for k, v in sorted(setup_counts.items()):
            print(f"      {k:<30} {v:>6}")
    if s4.get("setup_score_min") is not None:
        print(f"    Setup score:  min={s4['setup_score_min']}  mean={s4['setup_score_mean']}  max={s4['setup_score_max']}")
    if s4.get("estimated_rr_min") is not None:
        print(f"    Estimated RR: min={s4['estimated_rr_min']}  mean={s4['estimated_rr_mean']}  max={s4['estimated_rr_max']}")
    print(f"    Earnings penalty (nonzero): {s4.get('earnings_penalty_nonzero_count', 0)}")
    print(f"    Macro penalty (nonzero):    {s4.get('macro_penalty_nonzero_count', 0)}")

    s4p = result_dict.get("step4_projected", {})
    if s4p:
        print()
        print("  STEP 4 PROJECTED (score-gate dry-run from step3 data)")
        if not s4p.get("projected_step4_available", True):
            print(f"    NOT AVAILABLE: {s4p.get('reason', '')}")
        else:
            print(f"    Input candidates:           {s4p.get('input_candidates', 0)}")
            print(f"    After projection:           {s4p.get('candidates_after_projection', 0)}")
            for g in s4p.get("gates", []):
                print(f"    Gate {g['gate_order']}: {g['gate_key']:<30}  pass={g['pass_count']}  fail={g['fail_count']}  (threshold={g['threshold']})")
            not_proj = s4p.get("gates_not_projected", [])
            if not_proj:
                print(f"    Gates NOT projected: {', '.join(not_proj)}")

    s5 = result_dict.get("step5_counts", {})
    print()
    print("  STEP 5 PROPOSALS")
    print(f"    Proposals written:          {s5.get('step5_proposals_written', 0)}")
    print(f"    selected_flag = TRUE:       {s5.get('selected_flag_true', 0)}")
    print(f"    in_raw_top_n:               {s5.get('raw_rank_count', 0)}")
    print(f"    in_diversified_top_n:       {s5.get('diversified_rank_count', 0)}")
    print(f"    Diversification rejections: {s5.get('diversification_rejections', 0)}")
    rr_bd = s5.get("rejection_reason_breakdown", {})
    if rr_bd:
        print("    Rejection reason breakdown:")
        for k, v in rr_bd.items():
            print(f"      {k:<30} {v:>6}")
    disp_bd = s5.get("disposition_breakdown", {})
    if disp_bd:
        print("    Disposition breakdown (from mechanical_explanation):")
        for k, v in disp_bd.items():
            print(f"      {k:<30} {v:>6}")

    bn = result_dict.get("bottlenecks", {})
    print()
    print("  BOTTLENECKS (largest candidate drops)")
    for rank, key in [("Main", "main"), ("Second", "second"), ("Third", "third")]:
        val = bn.get(key)
        if val:
            print(f"    {rank:>6}: {val}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run funnel diagnostics for swing trading strategy candidate yield."
    )
    parser.add_argument("--date", required=True, type=_parse_date, metavar="YYYY-MM-DD")
    parser.add_argument("--db-role", choices=["prod", "debug"], default="prod")
    parser.add_argument("--strategy", default="all",
                        help="Strategy name or 'all'.")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--include-projected-step4", type=_bool_arg, default=True)
    parser.add_argument("--json-out", default=None, metavar="PATH")
    parser.add_argument("--prod-db", default="data/duckdb/prod.duckdb")
    parser.add_argument("--debug-db", default="data/duckdb/debug.duckdb")
    parser.add_argument("--sim-db", default="data/duckdb/simulation.duckdb")

    args = parser.parse_args()

    db_manager = _get_db_manager(args)
    svc = FunnelDiagnosticsService(db_manager=db_manager)

    strategy_config_id: str | None = None
    if args.strategy != "all":
        try:
            conn = db_manager.connect(args.db_role, read_only=True)
            try:
                rows = conn.execute(
                    "SELECT config_id FROM strategy_configs "
                    "WHERE strategy_name = ? AND active_flag = TRUE LIMIT 1",
                    [args.strategy],
                ).fetchall()
            finally:
                conn.close()
            if not rows:
                print(f"[ERROR] No active strategy config found for strategy_name={args.strategy!r}", file=sys.stderr)
                sys.exit(1)
            strategy_config_id = rows[0][0]
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] Failed to resolve strategy config: {exc}", file=sys.stderr)
            sys.exit(1)

    result = svc.run(
        signal_date=args.date,
        db_role=args.db_role,
        strategy_config_id=strategy_config_id,
        run_id=args.run_id,
        include_projected_step4=args.include_projected_step4,
    )

    if result.status == "failed":
        print(f"[FAILED] {'; '.join(result.errors)}", file=sys.stderr)
        sys.exit(1)

    for w in result.warnings:
        print(f"[WARNING] {w}", file=sys.stderr)

    funnel_results = result.metadata.get("funnel_results", [])
    for fr in funnel_results:
        _print_funnel(fr)

    if not funnel_results:
        print("No results to display.")

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": result.run_id,
            "status": result.status,
            "warnings": result.warnings,
            "metadata": result.metadata,
        }
        out_path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"\n[INFO] JSON written to {out_path}")


if __name__ == "__main__":
    main()
