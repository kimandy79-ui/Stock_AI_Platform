"""
tools/normalize_ticker_symbols.py
──────────────────────────────────
One-off migration: rewrite slash-notation share-class tickers (BRK/A, BRK/B,
BF/A, BF/B, ...) to hyphen notation (BRK-A, BRK-B, ...) in every table that
has a ``ticker`` column.

Why: SEC EDGAR and yfinance both 404 on slash notation. The shared
``app.services.universe.ticker_normalization.normalize_ticker()`` now keeps
newly-loaded CSV/DB-sourced tickers in hyphen form, but rows written before
that fix (e.g. an existing 'BRK/A' row in ``ticker_master`` or a pending
``data_repair_queue`` entry) stay in slash form until this migration runs
once.

Usage:
    python tools/normalize_ticker_symbols.py                    # dry run (prod)
    python tools/normalize_ticker_symbols.py --apply             # apply (prod)
    python tools/normalize_ticker_symbols.py --db-role debug --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._bootstrap import ensure_repo_root_on_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Normalize slash-notation share-class tickers to hyphen notation."
    )
    p.add_argument("--db-role", choices=["prod", "debug"], default="prod")
    p.add_argument(
        "--apply", action="store_true",
        help="Write changes. Without this flag, only reports what would change.",
    )
    return p.parse_args(argv)


def _tables_with_ticker_column(conn: object) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND column_name = 'ticker'"
    ).fetchall()
    return sorted({r[0] for r in rows})


def main(argv: list[str] | None = None) -> int:
    ensure_repo_root_on_path()
    from app.database import duckdb_manager
    from app.services.universe.ticker_normalization import normalize_ticker

    args = _parse_args(argv)

    conn = duckdb_manager.connect(args.db_role, read_only=not args.apply)
    try:
        tables = _tables_with_ticker_column(conn)
        plan: list[tuple[str, str, str, int]] = []  # (table, old, new, row_count)
        for table in tables:
            rows = conn.execute(
                f"SELECT DISTINCT ticker FROM {table} WHERE ticker LIKE '%/%'"
            ).fetchall()
            for (old,) in rows:
                new = normalize_ticker(old)
                if new == old:
                    continue
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE ticker = ?", [old]
                ).fetchone()[0]
                plan.append((table, old, new, count))

        if not plan:
            print("No slash-notation tickers found; nothing to do.")
            return 0

        print(f"{'APPLY' if args.apply else 'DRY-RUN'} plan ({len(plan)} table/ticker pair(s)):")
        for table, old, new, count in plan:
            print(f"  {table}: {old!r} -> {new!r} ({count} row(s))")

        if not args.apply:
            print("\nRe-run with --apply to write these changes.")
            return 0

        for table, old, new, _count in plan:
            conflict = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE ticker = ?", [new]
            ).fetchone()[0]
            if conflict:
                print(
                    f"  SKIPPED {table}: {new!r} already has {conflict} row(s); "
                    "resolve the conflict manually before re-running."
                )
                continue
            conn.execute(f"UPDATE {table} SET ticker = ? WHERE ticker = ?", [new, old])
            print(f"  updated {table}: {old!r} -> {new!r}")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
