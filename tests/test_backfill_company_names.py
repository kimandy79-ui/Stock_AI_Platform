"""Tests for tools/backfill_company_names.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.backfill_company_names import (
    _fetch_missing,
    _fetch_profile,
    _write_batch,
    main,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE ticker_master (
            ticker       VARCHAR PRIMARY KEY,
            company_name VARCHAR,
            sector       VARCHAR,
            industry     VARCHAR,
            active_flag  BOOLEAN NOT NULL DEFAULT true,
            last_updated TIMESTAMP
        )
    """)
    conn.execute("""
        INSERT INTO ticker_master (ticker, company_name, sector, industry, active_flag)
        VALUES
            ('AAPL', NULL,        NULL,          NULL,                             true),
            ('MSFT', 'Microsoft', NULL,          NULL,                             true),
            ('GOOG', 'Alphabet',  'Technology',  'Internet Content & Information', true),
            ('DEAD', NULL,        NULL,          NULL,                             false)
    """)
    conn.commit()
    conn.close()
    return db_path


def _read_row(db_path: Path, ticker: str) -> tuple:
    conn = duckdb.connect(str(db_path), read_only=True)
    row = conn.execute(
        "SELECT company_name, sector, industry FROM ticker_master WHERE ticker = ?",
        [ticker],
    ).fetchone()
    conn.close()
    return row


# ── _fetch_profile ────────────────────────────────────────────────────────────

def test_fetch_profile_full_metadata():
    mock_ticker = MagicMock()
    mock_ticker.info = {
        "longName": "Apple Inc.",
        "shortName": "Apple",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }
    with patch("yfinance.Ticker", return_value=mock_ticker):
        name, sector, industry = _fetch_profile("AAPL")
    assert name == "Apple Inc."
    assert sector == "Technology"
    assert industry == "Consumer Electronics"


def test_fetch_profile_shortname_fallback():
    mock_ticker = MagicMock()
    mock_ticker.info = {
        "shortName": "Apple",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }
    with patch("yfinance.Ticker", return_value=mock_ticker):
        name, sector, industry = _fetch_profile("AAPL")
    assert name == "Apple"
    assert sector == "Technology"
    assert industry == "Consumer Electronics"


def test_fetch_profile_missing_sector_industry():
    mock_ticker = MagicMock()
    mock_ticker.info = {"longName": "Some Corp"}
    with patch("yfinance.Ticker", return_value=mock_ticker):
        name, sector, industry = _fetch_profile("XYZ")
    assert name == "Some Corp"
    assert sector is None
    assert industry is None


def test_fetch_profile_exception_returns_nones():
    with patch("yfinance.Ticker", side_effect=Exception("network error")):
        name, sector, industry = _fetch_profile("BAD")
    assert name is None
    assert sector is None
    assert industry is None


# ── _fetch_missing ────────────────────────────────────────────────────────────

def test_fetch_missing_default_skips_fully_populated(tmp_path):
    db_path = _make_db(tmp_path)
    tickers = _fetch_missing(db_path)
    assert "AAPL" in tickers   # all NULL
    assert "MSFT" in tickers   # sector/industry NULL
    assert "GOOG" not in tickers  # fully populated
    assert "DEAD" not in tickers  # inactive


def test_fetch_missing_refresh_all_includes_fully_populated(tmp_path):
    db_path = _make_db(tmp_path)
    tickers = _fetch_missing(db_path, refresh_all=True)
    assert "AAPL" in tickers
    assert "MSFT" in tickers
    assert "GOOG" in tickers   # included even though fully populated
    assert "DEAD" not in tickers  # inactive still excluded


# ── _write_batch ──────────────────────────────────────────────────────────────

def test_write_batch_writes_all_fields(tmp_path):
    db_path = _make_db(tmp_path)
    _write_batch(db_path, {"AAPL": ("Apple Inc.", "Technology", "Consumer Electronics")})
    assert _read_row(db_path, "AAPL") == ("Apple Inc.", "Technology", "Consumer Electronics")


def test_write_batch_no_overwrite_with_none(tmp_path):
    db_path = _make_db(tmp_path)
    _write_batch(db_path, {"GOOG": (None, None, None)})
    assert _read_row(db_path, "GOOG") == ("Alphabet", "Technology", "Internet Content & Information")


def test_write_batch_no_overwrite_with_empty_string(tmp_path):
    db_path = _make_db(tmp_path)
    _write_batch(db_path, {"GOOG": ("", "", "")})
    assert _read_row(db_path, "GOOG") == ("Alphabet", "Technology", "Internet Content & Information")


def test_write_batch_partial_update_preserves_existing(tmp_path):
    db_path = _make_db(tmp_path)
    # MSFT has company_name but no sector/industry; provide sector only
    _write_batch(db_path, {"MSFT": ("Microsoft Corporation", "Technology", None)})
    row = _read_row(db_path, "MSFT")
    assert row[0] == "Microsoft Corporation"
    assert row[1] == "Technology"
    assert row[2] is None  # industry was NULL and stays NULL


def test_write_batch_returns_count(tmp_path):
    db_path = _make_db(tmp_path)
    n = _write_batch(db_path, {
        "AAPL": ("Apple Inc.", "Technology", "Consumer Electronics"),
        "MSFT": ("Microsoft Corp.", "Technology", "Software—Infrastructure"),
    })
    assert n == 2


# ── main / integration ────────────────────────────────────────────────────────

def test_main_dry_run_does_not_write(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    mock_ticker = MagicMock()
    mock_ticker.info = {"longName": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics"}
    with patch("yfinance.Ticker", return_value=mock_ticker), patch("time.sleep"):
        rc = main(["--db-path", str(db_path), "--dry-run", "--limit", "1"])
    assert rc == 0
    assert _read_row(db_path, "AAPL")[0] is None  # nothing written
    assert "DRY RUN" in capsys.readouterr().out


def test_main_writes_metadata_to_db(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    mock_ticker = MagicMock()
    mock_ticker.info = {"longName": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics"}
    with patch("yfinance.Ticker", return_value=mock_ticker), patch("time.sleep"):
        rc = main(["--db-path", str(db_path), "--limit", "1"])
    assert rc == 0
    assert _read_row(db_path, "AAPL") == ("Apple Inc.", "Technology", "Consumer Electronics")


def test_main_sector_and_industry_remain_distinct(tmp_path):
    db_path = _make_db(tmp_path)
    mock_ticker = MagicMock()
    mock_ticker.info = {"longName": "Corp", "sector": "Healthcare", "industry": "Biotechnology"}
    with patch("yfinance.Ticker", return_value=mock_ticker), patch("time.sleep"):
        main(["--db-path", str(db_path), "--limit", "1"])
    row = _read_row(db_path, "AAPL")
    assert row[1] == "Healthcare"
    assert row[2] == "Biotechnology"
    assert row[1] != row[2]


def test_main_failed_ticker_does_not_stop_run(tmp_path, capsys):
    db_path = _make_db(tmp_path)

    def ticker_factory(symbol):
        t = MagicMock()
        if symbol == "AAPL":
            t.info = {"longName": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics"}
        else:
            raise RuntimeError("simulated fetch failure")
        return t

    with patch("yfinance.Ticker", side_effect=ticker_factory), patch("time.sleep"):
        rc = main(["--db-path", str(db_path), "--limit", "2"])

    assert rc == 0
    assert _read_row(db_path, "AAPL")[0] == "Apple Inc."  # succeeded
    assert _read_row(db_path, "MSFT")[1] is None           # failed, untouched


def test_main_no_db_returns_error(tmp_path, capsys):
    rc = main(["--db-path", str(tmp_path / "nonexistent.duckdb")])
    assert rc == 1
    assert "ERROR" in capsys.readouterr().err


# ── _write_batch overwrite_sector_industry=True ──────────────────────────────

def test_write_batch_overwrite_replaces_existing_sector_industry(tmp_path):
    db_path = _make_db(tmp_path)
    # GOOG already has sector='Technology', industry='Internet Content & Information'
    _write_batch(
        db_path,
        {"GOOG": ("Alphabet Inc.", "Communication Services", "Internet Content & Information")},
        overwrite_sector_industry=True,
    )
    row = _read_row(db_path, "GOOG")
    assert row[0] == "Alphabet Inc."
    assert row[1] == "Communication Services"   # replaced
    assert row[2] == "Internet Content & Information"


def test_write_batch_overwrite_skips_null_values(tmp_path):
    db_path = _make_db(tmp_path)
    # Existing GOOG values must survive a NULL yfinance response even in overwrite mode
    _write_batch(
        db_path,
        {"GOOG": (None, None, None)},
        overwrite_sector_industry=True,
    )
    assert _read_row(db_path, "GOOG") == ("Alphabet", "Technology", "Internet Content & Information")


def test_write_batch_overwrite_skips_empty_string(tmp_path):
    db_path = _make_db(tmp_path)
    _write_batch(
        db_path,
        {"GOOG": ("", "", "")},
        overwrite_sector_industry=True,
    )
    assert _read_row(db_path, "GOOG") == ("Alphabet", "Technology", "Internet Content & Information")


def test_write_batch_overwrite_company_name_protected_from_empty(tmp_path):
    db_path = _make_db(tmp_path)
    # In overwrite mode, company_name must not be cleared by an empty yfinance response
    _write_batch(
        db_path,
        {"GOOG": ("", "Communication Services", "Internet Content & Information")},
        overwrite_sector_industry=True,
    )
    row = _read_row(db_path, "GOOG")
    assert row[0] == "Alphabet"   # unchanged
    assert row[1] == "Communication Services"


def test_write_batch_default_preserves_existing_sector_industry(tmp_path):
    db_path = _make_db(tmp_path)
    # Default mode: yfinance returns a different sector — existing DB value must win
    _write_batch(
        db_path,
        {"GOOG": ("Alphabet Inc.", "Communication Services", "Online Media")},
        overwrite_sector_industry=False,
    )
    row = _read_row(db_path, "GOOG")
    assert row[1] == "Technology"                         # existing preserved
    assert row[2] == "Internet Content & Information"     # existing preserved


def test_main_overwrite_flag_replaces_sector_industry(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    mock_ticker = MagicMock()
    mock_ticker.info = {
        "longName": "Alphabet Inc.",
        "sector": "Communication Services",
        "industry": "Online Media",
    }
    with patch("yfinance.Ticker", return_value=mock_ticker), patch("time.sleep"):
        rc = main([
            "--db-path", str(db_path),
            "--refresh-all",
            "--overwrite-sector-industry",
            "--limit", "1",
        ])
    assert rc == 0
    row = _read_row(db_path, "AAPL")
    assert row[1] == "Communication Services"
    assert row[2] == "Online Media"
    assert "OVERWRITE MODE" in capsys.readouterr().out


def test_main_overwrite_sector_industry_remain_distinct(tmp_path):
    db_path = _make_db(tmp_path)
    mock_ticker = MagicMock()
    mock_ticker.info = {"longName": "Corp", "sector": "Healthcare", "industry": "Biotechnology"}
    with patch("yfinance.Ticker", return_value=mock_ticker), patch("time.sleep"):
        main(["--db-path", str(db_path), "--refresh-all", "--overwrite-sector-industry", "--limit", "1"])
    row = _read_row(db_path, "AAPL")
    assert row[1] == "Healthcare"
    assert row[2] == "Biotechnology"
    assert row[1] != row[2]


# ── existing tests (unchanged) ────────────────────────────────────────────────

def test_main_nothing_to_do_when_all_populated(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    # Fill all active tickers so nothing is missing
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "UPDATE ticker_master SET company_name='X', sector='Y', industry='Z' WHERE active_flag=true"
    )
    conn.commit()
    conn.close()
    with patch("time.sleep"):
        rc = main(["--db-path", str(db_path)])
    assert rc == 0
    assert "Nothing to do" in capsys.readouterr().out
