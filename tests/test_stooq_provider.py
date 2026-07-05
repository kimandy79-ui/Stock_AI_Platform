"""Tests for the Phase 4 Stooq OHLCV provider (second price source).

Fully offline: no live ``stooq.com`` calls. :func:`parse_stooq_csv` is
exercised directly with synthetic CSV text; :class:`StooqProvider` is
exercised with an injected ``fetch_text`` fake (mirroring ``YahooProvider``'s
injected ``yf_module`` pattern).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import stooq_provider as sp
from app.providers.provider_interface import PriceBar, PriceHistoryRequest
from app.utils import service_result

_HEADER = "Date,Open,High,Low,Close,Volume"


class TestToStooqSymbol:
    def test_lowercases_and_appends_us(self) -> None:
        assert sp.to_stooq_symbol("AAPL") == "aapl.us"


class TestParseStooqCsv:
    def test_happy_path(self) -> None:
        csv_text = "\n".join(
            [
                _HEADER,
                "2024-01-02,100,105,99,103,1000000",
                "2024-01-03,103,108,102,107,1200000",
            ]
        )
        bars, warnings = sp.parse_stooq_csv(
            csv_text, "AAPL", date(2024, 1, 1), date(2024, 1, 31)
        )
        assert warnings == []
        assert len(bars) == 2
        assert bars[0] == PriceBar(
            ticker="AAPL",
            date=date(2024, 1, 2),
            open_raw=100.0,
            high_raw=105.0,
            low_raw=99.0,
            close_raw=103.0,
            volume_raw=1000000,
            source_provider="stooq",
        )

    def test_no_data_symbol_returns_empty(self) -> None:
        bars, warnings = sp.parse_stooq_csv(
            "No data", "UNKNOWNTICKER", date(2024, 1, 1), date(2024, 1, 31)
        )
        assert bars == []
        assert warnings == []

    def test_empty_text_returns_empty(self) -> None:
        bars, warnings = sp.parse_stooq_csv("", "AAPL", date(2024, 1, 1), date(2024, 1, 31))
        assert bars == []
        assert warnings == []

    def test_nd_rows_skipped_without_warning(self) -> None:
        csv_text = "\n".join([_HEADER, "2024-01-02,N/D,N/D,N/D,N/D,N/D"])
        bars, warnings = sp.parse_stooq_csv(
            csv_text, "AAPL", date(2024, 1, 1), date(2024, 1, 31)
        )
        assert bars == []
        assert warnings == []

    def test_rows_outside_range_filtered(self) -> None:
        csv_text = "\n".join(
            [
                _HEADER,
                "2023-12-31,100,105,99,103,1000",
                "2024-01-02,100,105,99,103,1000",
            ]
        )
        bars, _ = sp.parse_stooq_csv(csv_text, "AAPL", date(2024, 1, 1), date(2024, 1, 31))
        assert len(bars) == 1
        assert bars[0].date == date(2024, 1, 2)

    def test_unexpected_header_raises(self) -> None:
        with pytest.raises(ValueError):
            sp.parse_stooq_csv(
                "Wrong,Header\n1,2", "AAPL", date(2024, 1, 1), date(2024, 1, 31)
            )

    def test_malformed_row_produces_warning(self) -> None:
        csv_text = "\n".join([_HEADER, "2024-01-02,100,105,99"])  # too few fields
        bars, warnings = sp.parse_stooq_csv(
            csv_text, "AAPL", date(2024, 1, 1), date(2024, 1, 31)
        )
        assert bars == []
        assert len(warnings) == 1

    def test_bars_sorted_ascending_by_date(self) -> None:
        csv_text = "\n".join(
            [
                _HEADER,
                "2024-01-05,100,105,99,103,1000",
                "2024-01-02,90,95,89,93,900",
            ]
        )
        bars, _ = sp.parse_stooq_csv(csv_text, "AAPL", date(2024, 1, 1), date(2024, 1, 31))
        assert [b.date for b in bars] == [date(2024, 1, 2), date(2024, 1, 5)]


class TestStooqProvider:
    def test_get_capabilities(self) -> None:
        provider = sp.StooqProvider(fetch_text=lambda url: "")
        caps = provider.get_capabilities().metadata["capabilities"]
        assert caps.provider_name == "stooq"
        assert caps.supports_daily_prices is True
        assert caps.supports_adjusted_prices is False
        assert caps.supports_earnings is False
        assert caps.supports_ticker_listing is False

    def test_get_price_history_happy_path(self) -> None:
        csv_text = "\n".join([_HEADER, "2024-01-02,100,105,99,103,1000"])
        provider = sp.StooqProvider(fetch_text=lambda url: csv_text)
        request = PriceHistoryRequest(
            ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31)
        )
        result = provider.get_price_history(request)
        assert result.status == service_result.STATUS_SUCCESS
        assert result.rows_processed == 1
        assert result.metadata["provider_name"] == "stooq"

    def test_get_price_history_empty_is_success_not_failure(self) -> None:
        provider = sp.StooqProvider(fetch_text=lambda url: "No data")
        request = PriceHistoryRequest(
            ticker="UNKNOWNTICKER", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31)
        )
        result = provider.get_price_history(request)
        assert result.status == service_result.STATUS_SUCCESS
        assert result.rows_processed == 0
        assert result.metadata["bars"] == []

    def test_get_price_history_transport_failure(self) -> None:
        def raise_fetch(url: str) -> str:
            raise RuntimeError("connection reset")

        provider = sp.StooqProvider(fetch_text=raise_fetch)
        request = PriceHistoryRequest(
            ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31)
        )
        result = provider.get_price_history(request)
        assert result.status == service_result.STATUS_FAILED
        assert result.metadata["error_detail"].kind == "provider_unavailable"

    def test_get_price_history_malformed_response(self) -> None:
        provider = sp.StooqProvider(fetch_text=lambda url: "Wrong,Header\n1,2")
        request = PriceHistoryRequest(
            ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31)
        )
        result = provider.get_price_history(request)
        assert result.status == service_result.STATUS_FAILED
        assert result.metadata["error_detail"].kind == "malformed_response"

    def test_list_symbols_unsupported(self) -> None:
        provider = sp.StooqProvider(fetch_text=lambda url: "")
        result = provider.list_symbols()
        assert result.status == service_result.STATUS_FAILED
        assert result.metadata["error_detail"].kind == "unsupported_capability"

    def test_get_earnings_unsupported(self) -> None:
        provider = sp.StooqProvider(fetch_text=lambda url: "")
        result = provider.get_earnings("AAPL")
        assert result.status == service_result.STATUS_FAILED
        assert result.metadata["error_detail"].kind == "unsupported_capability"

    def test_url_uses_correct_symbol_and_date_format(self) -> None:
        captured = {}

        def fake_fetch(url: str) -> str:
            captured["url"] = url
            return ""

        provider = sp.StooqProvider(fetch_text=fake_fetch)
        request = PriceHistoryRequest(
            ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31)
        )
        provider.get_price_history(request)
        assert "aapl.us" in captured["url"]
        assert "d1=20240101" in captured["url"]
        assert "d2=20240131" in captured["url"]

    def test_production_default_lazy_imports_requests(self) -> None:
        """No fetch_text injected -> module imports cleanly without a live call."""
        provider = sp.StooqProvider()
        assert provider is not None
