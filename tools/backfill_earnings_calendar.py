"""Seed ``earnings_calendar`` from the Yahoo Finance provider.

Fetches upcoming / recent earnings dates for every active ticker in
``ticker_master`` (or a caller-supplied list) and upserts them into
``earnings_calendar``.  Run this before the daily pipeline so that
``feature_engine`` can populate ``days_to_earnings_bd`` correctly.

Usage::

    python tools/backfill_earnings_calendar.py
    python tools/backfill_earnings_calendar.py --tickers FDX AAPL MSFT
    python tools/backfill_earnings_calendar.py --db-role debug

Exit code: 0 on success / success_with_warnings, 1 on failure.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401 — adds repo root to sys.path

_bootstrap.ensure_repo_root_on_path()

from app.database import duckdb_manager as dbm  # noqa: E402
from app.providers.yahoo_provider import YahooProvider  # noqa: E402
from app.utils import logging_config  # noqa: E402

_LOG = logging_config.get_logger(__name__)

_SLEEP_SECONDS: float = 0.5  # between ticker fetches


def _load_tickers(db_role: str) -> list[str]:
    conn = dbm.connect(db_role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT ticker FROM ticker_master WHERE active_flag=TRUE ORDER BY ticker"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _upsert_events(db_role: str, events: list) -> int:
    """Insert or replace earnings_calendar rows; returns number of rows written."""
    if not events:
        return 0
    conn = dbm.connect(db_role)
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            now = datetime.datetime.now()
            written = 0
            for ev in events:
                conn.execute(
                    """
                    INSERT INTO earnings_calendar
                        (ticker, earnings_date, session, source, confidence, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (ticker, earnings_date)
                    DO UPDATE SET
                        session    = excluded.session,
                        source     = excluded.source,
                        confidence = excluded.confidence,
                        updated_at = excluded.updated_at
                    """,
                    [
                        ev.ticker,
                        ev.earnings_date,
                        ev.session or "unknown",
                        getattr(ev, "source_provider", "yahoo"),
                        ev.confidence or "low",
                        now,
                    ],
                )
                written += 1
            conn.execute("COMMIT")
            return written
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def run(db_role: str, tickers: list[str] | None) -> int:
    provider = YahooProvider()

    if tickers:
        ticker_list = tickers
        _LOG.info("backfill_earnings ticker_count=%d (caller-supplied)", len(ticker_list))
    else:
        ticker_list = _load_tickers(db_role)
        _LOG.info("backfill_earnings ticker_count=%d (from ticker_master)", len(ticker_list))

    total_written = 0
    errors: list[str] = []

    for i, ticker in enumerate(ticker_list):
        try:
            result = provider.get_earnings(ticker)
            events = result.metadata.get("events", [])
            if events:
                written = _upsert_events(db_role, events)
                total_written += written
                _LOG.info(
                    "ticker=%s events=%d written=%d", ticker, len(events), written
                )
            else:
                _LOG.info("ticker=%s no_earnings_events_found", ticker)
        except Exception as exc:  # noqa: BLE001
            msg = f"ticker={ticker} error={type(exc).__name__}: {exc}"
            _LOG.warning(msg)
            errors.append(msg)

        if i < len(ticker_list) - 1:
            time.sleep(_SLEEP_SECONDS)

    _LOG.info(
        "backfill_earnings complete total_written=%d errors=%d", total_written, len(errors)
    )
    if errors:
        for e in errors[:10]:
            _LOG.warning("error: %s", e)

    return 1 if (errors and not total_written) else 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed earnings_calendar from Yahoo Finance.")
    p.add_argument(
        "--tickers", nargs="*", metavar="TICKER",
        help="specific tickers to fetch (default: all active tickers)",
    )
    p.add_argument(
        "--db-role", default="prod",
        choices=["prod", "debug"],
        help="database role to write to (default: prod)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run(db_role=args.db_role, tickers=args.tickers or None))
