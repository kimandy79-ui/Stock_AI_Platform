"""
tools/reset_pipeline_data.py
────────────────────────────
Wipe all pipeline-generated data from prod.duckdb while preserving:
  • ticker_master
  • ticker_universe_snapshot
  • sector_etf_map
  • sector_alias_map
  • daily_prices          (raw price history — expensive to re-fetch)
  • daily_features        (engineered features — expensive to re-compute)
  • earnings_calendar     (external reference data)
  • macro_events_calendar (external reference data)

Setup-mode migration (AD-22.19–22.24):
  Wipes setup_configs, risk_label_config (replaces old strategy_configs, runtime_configs).
  After wipe, run init_prod_db.py to re-seed with setup-mode defaults.

Usage:
    python tools/reset_pipeline_data.py
    python tools/reset_pipeline_data.py --db-path path/to/prod.duckdb
    python tools/reset_pipeline_data.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Pipeline-generated tables to DELETE (child rows before parent-like rows).
_WIPE_TABLES: list[str] = [
    "ai_reviews",
    "execution_decisions",
    "signal_outcomes",
    "outcome_tracking_queue",
    "step5_proposals",
    "step4_analysis",
    "step3_candidates",
    "feature_rebuild_log",
    "data_repair_queue",
    "risk_label_config",      # setup-mode: replaces strategy_configs
    "setup_configs",          # setup-mode: replaces strategy_configs
    "pipeline_locks",
    "pipeline_runs",
    "schema_versions",
]

# Tables to PRESERVE (not deleted).
_PRESERVE_TABLES: list[str] = [
    "ticker_master",
    "ticker_universe_snapshot",
    "sector_etf_map",
    "sector_alias_map",
    "daily_prices",
    "daily_features",
    "earnings_calendar",
    "macro_events_calendar",
]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset pipeline-generated data from prod.duckdb (setup mode)."
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Path to prod.duckdb")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    try:
        import duckdb
    except ImportError:
        print("FAILURE: duckdb not installed")
        return 1

    if args.db_path:
        db_path = args.db_path
    else:
        try:
            from tools._bootstrap import ensure_repo_root_on_path
            ensure_repo_root_on_path()
            from app.database import duckdb_manager
            db_path = duckdb_manager.get_database_path("prod")
        except Exception as exc:  # noqa: BLE001
            print(f"FAILURE: cannot resolve prod DB path: {exc}")
            return 1

    if not Path(db_path).exists():
        print(f"INFO: {db_path} does not exist; nothing to reset.")
        return 0

    print(f"{'DRY RUN — ' if args.dry_run else ''}Resetting pipeline data in {db_path}")
    print(f"  PRESERVE: {_PRESERVE_TABLES}")
    print(f"  WIPE:     {_WIPE_TABLES}")

    if args.dry_run:
        print("DRY RUN complete — no changes made.")
        return 0

    try:
        conn = duckdb.connect(str(db_path))
        try:
            # Get tables actually present
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
                ).fetchall()
            }
            wiped: list[str] = []
            skipped: list[str] = []
            for table in _WIPE_TABLES:
                if table in present:
                    conn.execute(f"DELETE FROM {table}")
                    wiped.append(table)
                else:
                    skipped.append(table)
            print(f"Wiped {len(wiped)} tables: {wiped}")
            if skipped:
                print(f"Skipped (not present): {skipped}")
            print("SUCCESS: pipeline data reset. Run init_prod_db.py to re-seed configs.")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILURE: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
