"""Shared ticker symbol normalization.

Static universe sources (e.g. ``backfill_tickers_common_only.csv``) list
multi-class issuers using slash notation (``BRK/A``, ``BRK/B``, ``BF/A``,
``BF/B``); both SEC EDGAR's ``company_tickers.json`` and ``yfinance`` expect
hyphen notation (``BRK-A``, ``BRK-B``). Slash notation sent to either
provider 404s.

This is the single normalization point so every ticker -- whether it enters
via a CSV (:mod:`app.services.universe.ticker_file_loader`) or is read back
out of ``ticker_master`` -- converges on the same canonical form before any
provider call (:mod:`app.providers.edgar_provider`).
"""

from __future__ import annotations


def normalize_ticker(ticker: str) -> str:
    """Return *ticker* upper-cased, stripped, with ``/`` converted to ``-``.

    Slash notation has no other meaning for the equity/ETF tickers this
    platform tracks, so the substitution is unconditional.
    """
    return ticker.strip().upper().replace("/", "-")


__all__ = ["normalize_ticker"]
