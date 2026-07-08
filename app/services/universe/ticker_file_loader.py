"""Shared ticker-file loader — CSV / plain-text ticker lists to ``TickerInfo``.

Extracted from ``tools/backfill_prod_history.py`` so both that tool and the
dashboard's CSV-sourced pipeline trigger (``DashboardActionService.run_pipeline``)
share one implementation instead of duplicating parsing logic. This module
is pure (no DB, no network, no Streamlit) and does no I/O beyond reading the
one file it's given.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Final

from app.config import constants
from app.providers.provider_interface import TickerInfo
from app.services.universe.ticker_normalization import normalize_ticker

# Header aliases the loader recognises (case-insensitive, stripped).
_TICKER_COL_ALIASES: Final[frozenset[str]] = frozenset(
    {"ticker", "symbol", "tick", "tickers", "symbols"}
)
_NAME_COL_ALIASES: Final[frozenset[str]] = frozenset(
    {"name", "company_name", "company", "description"}
)
_SECTOR_COL_ALIASES: Final[frozenset[str]] = frozenset({"sector"})
_INDUSTRY_COL_ALIASES: Final[frozenset[str]] = frozenset({"industry"})
_SYMTYPE_COL_ALIASES: Final[frozenset[str]] = frozenset({"symbol_type", "type", "symtype"})
# Values that look like a header and must be skipped if the file lacks one.
_HEADER_SENTINEL: Final[frozenset[str]] = frozenset({"ticker", "symbol", "tickers", "symbols"})


def load_tickers_from_file(path: Path) -> list[TickerInfo]:
    """Load ``TickerInfo`` entries from a CSV or plain-text ticker file.

    Supports:
    * CSV with a header row containing a ``ticker`` (or ``symbol``) column, plus
      optional ``symbol_type``, ``name``, ``industry``, ``sector`` columns — the
      format produced by the project's universe export (e.g.
      ``backfill_tickers_common_only.csv``).
    * Plain text, one ticker per line (no header, no commas).

    Normalisation: tickers are uppercased, stripped, and slash-notation
    share classes (e.g. ``BRK/A``) are converted to hyphen notation
    (``BRK-A``) via :func:`~app.services.universe.ticker_normalization.normalize_ticker`
    -- see that module for why. Blank rows and header sentinels are skipped.
    Only rows where ``symbol_type`` equals ``"stock"`` (or where the column
    is absent) are returned.

    Returns
    -------
    list[TickerInfo]
        Deduplicated, ordered as they appear in the file.

    Raises
    ------
    ValueError
        If the file cannot be read or produces zero valid ticker entries after
        filtering.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8-sig")  # utf-8-sig strips BOM
    except OSError as exc:
        raise ValueError(f"cannot read tickers file {path}: {exc}") from exc

    lines = [ln.rstrip("\r\n") for ln in raw.splitlines()]
    if not lines:
        raise ValueError(f"tickers file {path} is empty")

    # Detect whether the file uses commas (CSV) or is plain-text.
    has_comma = any("," in ln for ln in lines if ln.strip())

    entries: list[TickerInfo] = []
    seen: set[str] = set()

    if has_comma:
        rows = list(csv.reader(lines))
        if not rows:
            raise ValueError(f"tickers file {path} parsed to zero rows")

        # Detect header by checking if first row contains a ticker-column alias.
        first = [f.strip().lower() for f in rows[0]]
        has_header = any(f in _TICKER_COL_ALIASES for f in first)

        if has_header:
            header = first
            data_rows = rows[1:]
        else:
            header = []
            data_rows = rows

        # Build column-index map from header (or assume col-0 = ticker).
        def _col(aliases: frozenset[str]) -> int | None:
            for i, h in enumerate(header):
                if h in aliases:
                    return i
            return None

        i_ticker = _col(_TICKER_COL_ALIASES) if header else 0
        i_name = _col(_NAME_COL_ALIASES)
        i_sector = _col(_SECTOR_COL_ALIASES)
        i_industry = _col(_INDUSTRY_COL_ALIASES)
        i_symtype = _col(_SYMTYPE_COL_ALIASES)

        if i_ticker is None:
            raise ValueError(
                f"tickers file {path}: no 'ticker' or 'symbol' column found "
                f"in header {rows[0]}"
            )

        def _cell(row: list[str], idx: int | None) -> str | None:
            if idx is None or idx >= len(row):
                return None
            return row[idx].strip() or None

        for row in data_rows:
            if not row:
                continue
            ticker_raw = _cell(row, i_ticker)
            if not ticker_raw:
                continue
            ticker = normalize_ticker(ticker_raw)
            if not ticker or ticker.lower() in _HEADER_SENTINEL:
                continue  # stray header row
            sym_type = _cell(row, i_symtype) or constants.SYMBOL_TYPE_STOCK
            if sym_type not in constants.ALLOWED_SYMBOL_TYPES:
                sym_type = constants.SYMBOL_TYPE_STOCK
            if sym_type != constants.SYMBOL_TYPE_STOCK:
                continue  # stock tickers only
            if ticker in seen:
                continue
            seen.add(ticker)
            # Map industry column → sector (some source CSVs use "industry" for
            # what is effectively the sector grouping; put it in both fields).
            industry_val = _cell(row, i_industry)
            sector_val = _cell(row, i_sector) or industry_val
            entries.append(
                TickerInfo(
                    ticker=ticker,
                    symbol_type=constants.SYMBOL_TYPE_STOCK,
                    company_name=_cell(row, i_name),
                    sector=sector_val,
                    industry=industry_val,
                )
            )
    else:
        # Plain text: one ticker per line.
        for ln in lines:
            ticker = normalize_ticker(ln)
            if not ticker or ticker.lower() in _HEADER_SENTINEL:
                continue
            if ticker in seen:
                continue
            seen.add(ticker)
            entries.append(
                TickerInfo(
                    ticker=ticker,
                    symbol_type=constants.SYMBOL_TYPE_STOCK,
                )
            )

    if not entries:
        raise ValueError(
            f"tickers file {path} produced zero valid stock ticker entries"
        )
    return entries


__all__ = ["load_tickers_from_file"]
