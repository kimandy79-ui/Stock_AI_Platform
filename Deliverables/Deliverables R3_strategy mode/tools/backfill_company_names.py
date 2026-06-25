"""
tools/backfill_company_names.py
────────────────────────────────
One-time backfill of company_name in ticker_master from yfinance.

Fetches longName / shortName for every ticker where company_name IS NULL
or empty. Writes results in batches to avoid long transactions.
Rate-limited to ~1.5 requests/second to stay within yfinance limits.

Usage:
    python tools/backfill_company_names.py
    python tools/backfill_company_names.py --db-path data/duckdb/prod.duckdb
    python tools/backfill_company_names.py --dry-run
    python tools/backfill_company_names.py --limit 100   # test with 100 tickers first

Estimated time: ~90 minutes for 3920 tickers at 1.5 req/sec.
Progress is printed every 50 tickers and saved continuously so
the script can be stopped and restarted safely (already-filled
tickers are skipped on restart).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BATCH_SIZE = 50       # write to DB every N tickers
_DELAY_SEC  = 0.7      # seconds between yfinance requests


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill ticker_master.company_name from yfinance."
    )
    p.add_argument("--db-path", type=Path, default=None,
                   help="Path to prod.duckdb (auto-detected if omitted)")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch names but do not write to DB")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N tickers (for testing)")
    p.add_argument("--delay", type=float, default=_DELAY_SEC,
                   help=f"Seconds between yfinance requests (default {_DELAY_SEC})")
    return p.parse_args(argv)


def _resolve_db(db_path: Path | None) -> Path:
    if db_path and db_path.exists():
        return db_path
    candidates = [
        Path("data/duckdb/prod.duckdb"),
        Path("data/prod.duckdb"),
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "prod.duckdb not found. Use --db-path to specify location."
    )


def _fetch_missing(db_path: Path) -> list[str]:
    import duckdb
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT ticker FROM ticker_master "
            "WHERE (company_name IS NULL OR company_name = '') "
            "  AND active_flag = true "
            "ORDER BY ticker"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _write_batch(db_path: Path, batch: dict[str, str]) -> int:
    import duckdb
    conn = duckdb.connect(str(db_path))
    try:
        updated = 0
        for ticker, name in batch.items():
            conn.execute(
                "UPDATE ticker_master "
                "SET company_name = ?, last_updated = CAST(now() AS TIMESTAMP) "
                "WHERE ticker = ?",
                [name, ticker],
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


def _fetch_name(ticker: str) -> str | None:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return (
            info.get("longName")
            or info.get("shortName")
            or None
        )
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        db_path = _resolve_db(args.db_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"DB: {db_path}")

    try:
        import yfinance  # noqa: F401
    except ImportError:
        print("ERROR: yfinance not installed. Run: pip install yfinance",
              file=sys.stderr)
        return 1

    tickers = _fetch_missing(db_path)
    if args.limit:
        tickers = tickers[:args.limit]

    total = len(tickers)
    if total == 0:
        print("All company names already populated. Nothing to do.")
        return 0

    print(f"Tickers to fill: {total}")
    if args.dry_run:
        print("DRY RUN — no writes.")
    print(f"Estimated time: ~{total * args.delay / 60:.0f} minutes")
    print()

    batch:    dict[str, str] = {}
    filled    = 0
    skipped   = 0
    errors    = 0
    start_ts  = time.time()

    for i, ticker in enumerate(tickers, 1):
        name = _fetch_name(ticker)

        if name:
            batch[ticker] = name
            filled += 1
        else:
            skipped += 1

        # Write batch
        if len(batch) >= _BATCH_SIZE and not args.dry_run:
            written = _write_batch(db_path, batch)
            batch.clear()

        # Progress every 50 tickers
        if i % 50 == 0 or i == total:
            elapsed  = time.time() - start_ts
            rate     = i / elapsed if elapsed > 0 else 0
            eta_sec  = (total - i) / rate if rate > 0 else 0
            eta_min  = eta_sec / 60
            print(
                f"  {i:>4}/{total}  filled={filled}  skipped={skipped}  "
                f"elapsed={elapsed/60:.1f}m  eta={eta_min:.1f}m"
            )

        time.sleep(args.delay)

    # Flush remaining batch
    if batch and not args.dry_run:
        _write_batch(db_path, batch)

    elapsed = time.time() - start_ts
    print()
    print(f"Done in {elapsed/60:.1f} minutes.")
    print(f"  Filled:  {filled}")
    print(f"  Skipped: {skipped} (yfinance returned no name)")
    print(f"  Errors:  {errors}")
    if args.dry_run:
        print("DRY RUN — nothing was written to DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
