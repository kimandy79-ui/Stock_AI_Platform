"""Tests for the shared ticker-file loader (extracted from
``tools/backfill_prod_history.py`` so the dashboard's CSV-sourced pipeline
trigger can reuse the same parsing logic without duplicating it).

``tests/test_backfill_prod_history.py`` still exercises the same behavior
indirectly via the tool's thin ``_load_tickers_from_file`` wrapper; these
tests target the shared module directly, plus the real project CSV shape
(``symbol,name,price,marketCap,volume,industry`` — no ``ticker``/
``symbol_type``/``sector`` columns) that
``data/input/backfill_tickers_common_only.csv`` actually uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.universe.ticker_file_loader import load_tickers_from_file


def test_real_project_csv_shape(tmp_path: Path) -> None:
    """Matches data/input/backfill_tickers_common_only.csv's actual header:
    symbol,name,price,marketCap,volume,industry -- no ticker/symbol_type/
    sector columns."""
    csv_text = (
        "symbol,name,price,marketCap,volume,industry\n"
        "NVDA,NVIDIA Corporation Common Stock,197.58,4781436000000.0,146149732,Technology\n"
        "AAPL,Apple Inc. Common Stock,294.38,4323663859280.0,50164471,Technology\n"
    )
    f = tmp_path / "backfill_tickers_common_only.csv"
    f.write_text(csv_text, encoding="utf-8")
    entries = load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["NVDA", "AAPL"]
    assert entries[0].company_name == "NVIDIA Corporation Common Stock"
    # No "sector" column in this shape; "industry" backfills both fields.
    assert entries[0].sector == "Technology"
    assert entries[0].industry == "Technology"
    assert all(e.symbol_type == "stock" for e in entries)


def test_csv_with_full_header(tmp_path: Path) -> None:
    f = tmp_path / "tickers.csv"
    f.write_text(
        "ticker,symbol_type,name,industry,sector\n"
        "NVDA,stock,NVIDIA Corporation,Technology,Tech\n",
        encoding="utf-8",
    )
    entries = load_tickers_from_file(f)
    assert entries[0].ticker == "NVDA"
    assert entries[0].company_name == "NVIDIA Corporation"
    assert entries[0].sector == "Tech"
    assert entries[0].industry == "Technology"


def test_deduplicates(tmp_path: Path) -> None:
    f = tmp_path / "t.csv"
    f.write_text("ticker\nAAPL\nMSFT\nAAPL\n", encoding="utf-8")
    entries = load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["AAPL", "MSFT"]


def test_plain_text(tmp_path: Path) -> None:
    f = tmp_path / "t.txt"
    f.write_text("aapl\nmsft\nGOOGL\n", encoding="utf-8")
    entries = load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["AAPL", "MSFT", "GOOGL"]


def test_skips_non_stock_rows(tmp_path: Path) -> None:
    f = tmp_path / "t.csv"
    f.write_text(
        "ticker,symbol_type\nAAPL,stock\nSPY,etf\nMSFT,stock\n", encoding="utf-8"
    )
    entries = load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["AAPL", "MSFT"]


def test_empty_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "empty.csv"
    f.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_tickers_from_file(f)


def test_header_only_raises(tmp_path: Path) -> None:
    f = tmp_path / "header_only.csv"
    f.write_text("ticker,symbol_type\n", encoding="utf-8")
    with pytest.raises(ValueError, match="zero valid"):
        load_tickers_from_file(f)


def test_missing_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "does_not_exist.csv"
    with pytest.raises(ValueError, match="cannot read"):
        load_tickers_from_file(f)


def test_bom_utf8(tmp_path: Path) -> None:
    f = tmp_path / "bom.csv"
    f.write_bytes(b"\xef\xbb\xbfticker\nAAPL\nMSFT\n")
    entries = load_tickers_from_file(f)
    assert entries[0].ticker == "AAPL"


def test_slash_notation_share_classes_normalized_to_hyphen(tmp_path: Path) -> None:
    """BRK/A, BRK/B (as they actually appear in
    data/input/backfill_tickers_common_only.csv) must normalize to hyphen
    notation at load time -- SEC EDGAR and yfinance both 404 on slash
    notation."""
    f = tmp_path / "t.csv"
    f.write_text(
        "symbol,name,price,marketCap,volume,industry\n"
        "BRK/A,Berkshire Hathaway Inc.,750999.99,1104626359291.0,259,Uncategorized\n"
        "BRK/B,Berkshire Hathaway Inc.,499.74,1102582044544.0,4101309,Uncategorized\n",
        encoding="utf-8",
    )
    entries = load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["BRK-A", "BRK-B"]


def test_plain_text_slash_notation_normalized_to_hyphen(tmp_path: Path) -> None:
    f = tmp_path / "t.txt"
    f.write_text("brk/a\nbrk/b\n", encoding="utf-8")
    entries = load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["BRK-A", "BRK-B"]


def test_no_recognizable_header_falls_back_to_first_column(tmp_path: Path) -> None:
    """When no column name matches a ticker alias, the loader assumes there
    is no header at all and treats every row (including the first) as data,
    column 0 = ticker -- pre-existing behavior, preserved unchanged from the
    original tools/backfill_prod_history.py implementation."""
    f = tmp_path / "t.csv"
    f.write_text("name,industry\nNVIDIA,Technology\n", encoding="utf-8")
    entries = load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["NAME", "NVIDIA"]
