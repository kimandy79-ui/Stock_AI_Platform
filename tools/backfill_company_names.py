"""
tools/backfill_company_names.py
────────────────────────────────
Backfill ticker metadata (company_name, sector, industry) in ticker_master
from yfinance.

Fetches metadata for every active ticker where any of company_name, sector,
or industry is missing. Writes results in batches; never overwrites an
existing valid value with a NULL/empty yfinance response.

Usage:
    python tools/backfill_company_names.py
    python tools/backfill_company_names.py --db-path data/duckdb/prod.duckdb
    python tools/backfill_company_names.py --dry-run
    python tools/backfill_company_names.py --limit 100
    python tools/backfill_company_names.py --refresh-all
    python tools/backfill_company_names.py --refresh-all --overwrite-sector-industry

Estimated time: ~90 minutes for 3920 tickers at 1.5 req/sec.
Progress is printed every 50 tickers and saved continuously so the script
can be stopped and restarted safely.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BATCH_SIZE = 50
_DELAY_SEC  = 0.7


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill ticker_master metadata (company_name, sector, industry) from yfinance."
    )
    p.add_argument("--db-path", type=Path, default=None,
                   help="Path to prod.duckdb (auto-detected if omitted)")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch metadata but do not write to DB")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after N tickers (for testing)")
    p.add_argument("--delay", type=float, default=_DELAY_SEC,
                   help=f"Seconds between yfinance requests (default {_DELAY_SEC})")
    p.add_argument("--refresh-all", action="store_true",
                   help="Process all active tickers, not just those missing metadata")
    p.add_argument("--overwrite-sector-industry", action="store_true",
                   help="Replace existing sector/industry with yfinance values when non-empty")
    return p.parse_args(argv)


def _resolve_db(db_path: Path | None) -> Path:
    if db_path is not None:
        if db_path.exists():
            return db_path
        raise FileNotFoundError(f"DB not found: {db_path}")
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


def _fetch_missing(db_path: Path, refresh_all: bool = False) -> list[str]:
    import duckdb
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        if refresh_all:
            sql = (
                "SELECT ticker FROM ticker_master "
                "WHERE active_flag = true "
                "ORDER BY ticker"
            )
        else:
            sql = (
                "SELECT ticker FROM ticker_master "
                "WHERE active_flag = true "
                "  AND ("
                "    company_name IS NULL OR company_name = '' "
                "    OR sector IS NULL OR sector = '' "
                "    OR industry IS NULL OR industry = ''"
                "  ) "
                "ORDER BY ticker"
            )
        rows = conn.execute(sql).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _write_batch(
    db_path: Path,
    batch: dict[str, tuple[str | None, str | None, str | None]],
    overwrite_sector_industry: bool = False,
) -> int:
    import duckdb
    conn = duckdb.connect(str(db_path))
    try:
        updated = 0
        for ticker, (name, sector, industry) in batch.items():
            if overwrite_sector_industry:
                # yfinance value wins when non-empty; fall back to existing DB value.
                # company_name uses the same rule: yfinance wins when non-empty.
                conn.execute(
                    "UPDATE ticker_master "
                    "SET company_name = COALESCE(NULLIF(?, ''), company_name), "
                    "    sector       = COALESCE(NULLIF(?, ''), sector), "
                    "    industry     = COALESCE(NULLIF(?, ''), industry), "
                    "    last_updated = CAST(now() AS TIMESTAMP) "
                    "WHERE ticker = ?",
                    [name, sector, industry, ticker],
                )
            else:
                # Existing DB value wins; fill in only when field is currently NULL/empty.
                # company_name still uses yfinance-first to safely backfill names.
                conn.execute(
                    "UPDATE ticker_master "
                    "SET company_name = COALESCE(NULLIF(?, ''), company_name), "
                    "    sector       = COALESCE(sector, NULLIF(?, '')), "
                    "    industry     = COALESCE(industry, NULLIF(?, '')), "
                    "    last_updated = CAST(now() AS TIMESTAMP) "
                    "WHERE ticker = ?",
                    [name, sector, industry, ticker],
                )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


def _fetch_profile(ticker: str) -> tuple[str | None, str | None, str | None]:
    """Return (company_name, sector, industry) from yfinance; fields may be None."""
    try:
        import yfinance as yf
        info     = yf.Ticker(ticker).info
        name     = info.get("longName") or info.get("shortName") or None
        sector   = info.get("sector") or None
        industry = info.get("industry") or None
        return name, sector, industry
    except Exception:
        return None, None, None


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

    tickers = _fetch_missing(db_path, refresh_all=args.refresh_all)
    if args.limit:
        tickers = tickers[:args.limit]

    total = len(tickers)
    if total == 0:
        print("All metadata already populated. Nothing to do.")
        return 0

    print(f"Tickers to process: {total}")
    if args.dry_run:
        print("DRY RUN — no writes.")
    if args.overwrite_sector_industry:
        print("OVERWRITE MODE — existing sector/industry will be replaced by yfinance values when non-empty.")
    print(f"Estimated time: ~{total * args.delay / 60:.0f} minutes")
    print()

    batch:   dict[str, tuple[str | None, str | None, str | None]] = {}
    updated  = 0
    skipped  = 0
    start_ts = time.time()

    for i, ticker in enumerate(tickers, 1):
        name, sector, industry = _fetch_profile(ticker)

        if any([name, sector, industry]):
            batch[ticker] = (name, sector, industry)
            updated += 1
        else:
            skipped += 1

        if len(batch) >= _BATCH_SIZE and not args.dry_run:
            _write_batch(db_path, batch, overwrite_sector_industry=args.overwrite_sector_industry)
            batch.clear()

        if i % 50 == 0 or i == total:
            elapsed = time.time() - start_ts
            rate    = i / elapsed if elapsed > 0 else 0
            eta_sec = (total - i) / rate if rate > 0 else 0
            print(
                f"  {i:>4}/{total}  updated={updated}  skipped={skipped}  "
                f"elapsed={elapsed/60:.1f}m  eta={eta_sec/60:.1f}m"
            )

        time.sleep(args.delay)

    if batch and not args.dry_run:
        _write_batch(db_path, batch, overwrite_sector_industry=args.overwrite_sector_industry)

    elapsed = time.time() - start_ts
    print()
    print(f"Done in {elapsed/60:.1f} minutes.")
    print(f"  Updated: {updated}")
    print(f"  Skipped: {skipped} (yfinance returned no metadata)")
    if args.dry_run:
        print("DRY RUN — nothing was written to DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
