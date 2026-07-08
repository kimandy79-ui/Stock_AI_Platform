"""Tests for the shared ticker-symbol normalizer.

See app/services/universe/ticker_normalization.py for why slash-notation
share classes (BRK/A, BF/A, ...) must be converted to hyphen notation
(BRK-A, BF-A, ...) before any SEC EDGAR / yfinance provider call.
"""

from __future__ import annotations

import pytest

from app.services.universe.ticker_normalization import normalize_ticker


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("BRK/A", "BRK-A"),
        ("BRK/B", "BRK-B"),
        ("BF/A", "BF-A"),
        ("BF/B", "BF-B"),
        ("brk/a", "BRK-A"),
        (" BRK/A ", "BRK-A"),
        ("AAPL", "AAPL"),
        ("aapl", "AAPL"),
    ],
)
def test_normalize_ticker(raw: str, expected: str) -> None:
    assert normalize_ticker(raw) == expected


def test_idempotent_on_already_normalized_ticker() -> None:
    assert normalize_ticker(normalize_ticker("BRK/A")) == "BRK-A"
