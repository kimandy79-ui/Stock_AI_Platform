"""Tests for Module 05 — YahooProvider.

These tests run **fully offline**: no real network, no real Yahoo / ``yfinance``
calls, and no DuckDB. A deterministic fake ``yfinance`` module is injected via
the ``YahooProvider(yf_module=...)`` constructor hook (CLARIFICATION 10), and
vendor price rows are fed as small in-test ``pandas`` DataFrames mirroring the
columns in CLARIFICATIONS 2 / 4. The fake records every vendor entry point it
exposes (``Ticker`` / ``history`` / ``calendar``) so the tests can assert that
no network access happens where none should.

Coverage mirrors the Module 05 prompt's "REQUIRED TESTS" list (items 1-16):
import smoke, signature conformance, capabilities, price-history happy path,
inclusive end-date handling, single-day range, adjusted-OHLC derivation, empty
vs unknown symbol, transport-error mapping, ``list_symbols`` (no scrape),
best-effort ``get_earnings``, VIX mapping, network-free constructor, the
Yahoo-isolation static scan, and style.
"""

from __future__ import annotations

import inspect
import io
import re
import token
import tokenize
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.config import constants
from app.providers import provider_interface as pi
from app.providers import yahoo_provider as yp
from app.providers.provider_interface import (
    EarningsEvent,
    MarketDataProvider,
    PriceBar,
    PriceHistoryRequest,
    ProviderCapabilities,
    ProviderErrorDetail,
    TickerInfo,
)
from app.providers.yahoo_provider import YahooProvider
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Deterministic, network-free fake yfinance
# --------------------------------------------------------------------------- #
class _FakeRateLimitError(Exception):
    """Fake throttling error (message recognized as a rate limit)."""


class _FakeConnectionError(Exception):
    """Fake connectivity error (message recognized as provider-unavailable)."""


class _FakeTicker:
    """Fake ``yfinance.Ticker`` recording calls and replaying configured data."""

    def __init__(self, symbol: str, registry: "FakeYF") -> None:
        self._symbol = symbol
        self._registry = registry
        registry.ticker_calls.append(symbol)

    def history(
        self,
        *,
        start: Any = None,
        end: Any = None,
        auto_adjust: Any = None,
        actions: Any = None,
    ) -> Any:
        self._registry.history_calls.append(
            {
                "symbol": self._symbol,
                "start": start,
                "end": end,
                "auto_adjust": auto_adjust,
                "actions": actions,
            }
        )
        behavior = self._registry.history_behavior.get(
            self._symbol, self._registry.default_history
        )
        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):
            return behavior(start, end)
        return behavior

    @property
    def calendar(self) -> Any:
        self._registry.calendar_calls.append(self._symbol)
        behavior = self._registry.calendar_behavior.get(
            self._symbol, self._registry.default_calendar
        )
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior


class FakeYF:
    """Fake ``yfinance`` module exposing only ``Ticker(symbol)``."""

    def __init__(self) -> None:
        self.ticker_calls: list[str] = []
        self.history_calls: list[dict[str, Any]] = []
        self.calendar_calls: list[str] = []
        self.history_behavior: dict[str, Any] = {}
        self.calendar_behavior: dict[str, Any] = {}
        self.default_history: Any = None
        self.default_calendar: Any = None

    def Ticker(self, symbol: str) -> _FakeTicker:  # noqa: N802 - mirrors yfinance API
        return _FakeTicker(symbol, self)

    def network_calls(self) -> int:
        return len(self.ticker_calls) + len(self.history_calls) + len(self.calendar_calls)


def _price_frame(rows: list[dict[str, Any]], dates: list[str]) -> pd.DataFrame:
    """Build a yfinance-shaped daily price DataFrame indexed by date."""
    return pd.DataFrame(rows, index=pd.to_datetime(dates))


def _make_provider(**kwargs: Any) -> tuple[YahooProvider, FakeYF]:
    fake = FakeYF()
    provider = YahooProvider(yf_module=fake, **kwargs)
    return provider, fake


# --------------------------------------------------------------------------- #
# 1. Import smoke
# --------------------------------------------------------------------------- #
def test_yahoo_provider_is_market_data_provider_subclass() -> None:
    """``YahooProvider`` is a concrete ``MarketDataProvider`` subclass."""
    assert issubclass(YahooProvider, MarketDataProvider)


def test_yahoo_provider_instantiates_with_injected_fake() -> None:
    """It instantiates with an injected fake and implements all four methods."""
    provider, _ = _make_provider()
    assert isinstance(provider, MarketDataProvider)
    for name in ("get_capabilities", "get_price_history", "list_symbols", "get_earnings"):
        assert callable(getattr(provider, name))
    # No abstract methods remain unimplemented.
    assert getattr(YahooProvider, "__abstractmethods__", frozenset()) == frozenset()


# --------------------------------------------------------------------------- #
# 2. Signature conformance (Module 04 contract, spec §7.2)
# --------------------------------------------------------------------------- #
def test_method_signatures_match_module04_contract() -> None:
    """Each method's params/return annotation match the abstract contract."""
    expected_params = {
        "get_capabilities": ["self"],
        "get_price_history": ["self", "request"],
        "list_symbols": ["self", "symbol_type"],
        "get_earnings": ["self", "ticker"],
    }
    for name, params in expected_params.items():
        concrete = inspect.signature(getattr(YahooProvider, name))
        base = inspect.signature(getattr(MarketDataProvider, name))
        assert list(concrete.parameters) == params, name
        assert concrete.return_annotation in (ServiceResult, "ServiceResult"), name
        # Parameter names and annotations match the abstract base exactly.
        assert list(concrete.parameters) == list(base.parameters), name
        for pname in concrete.parameters:
            assert (
                concrete.parameters[pname].annotation
                == base.parameters[pname].annotation
            ), f"{name}.{pname}"
            assert (
                concrete.parameters[pname].default == base.parameters[pname].default
            ), f"{name}.{pname}"

    list_sig = inspect.signature(YahooProvider.list_symbols)
    assert list_sig.parameters["symbol_type"].default is None
    assert list_sig.parameters["symbol_type"].annotation in ("str | None", str | None)
    earnings_sig = inspect.signature(YahooProvider.get_earnings)
    assert earnings_sig.parameters["ticker"].annotation in (str, "str")
    price_sig = inspect.signature(YahooProvider.get_price_history)
    assert price_sig.parameters["request"].annotation in (
        PriceHistoryRequest,
        "PriceHistoryRequest",
    )


# --------------------------------------------------------------------------- #
# 3. Capabilities
# --------------------------------------------------------------------------- #
def test_capabilities_reports_v1_scope() -> None:
    """get_capabilities returns success + a ProviderCapabilities for V1 scope."""
    provider, fake = _make_provider()
    result = provider.get_capabilities()
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["provider_name"] == "yahoo"
    caps = result.metadata["capabilities"]
    assert isinstance(caps, ProviderCapabilities)
    assert caps.provider_name == "yahoo"
    assert caps.supports_daily_prices is True
    assert caps.supports_adjusted_prices is True
    assert caps.supports_ticker_listing is False  # no symbol_source injected
    assert fake.network_calls() == 0


def test_capabilities_ticker_listing_true_with_symbol_source() -> None:
    """A static symbol source flips supports_ticker_listing to True."""
    source = [TickerInfo(ticker="SPY", symbol_type=constants.SYMBOL_TYPE_ETF)]
    provider, _ = _make_provider(symbol_source=source)
    caps = provider.get_capabilities().metadata["capabilities"]
    assert caps.supports_ticker_listing is True


# --------------------------------------------------------------------------- #
# 4. Price-history happy path
# --------------------------------------------------------------------------- #
def test_price_history_happy_path_maps_raw_and_adjusted() -> None:
    """A normal multi-row frame maps to PriceBars with raw + adjusted OHLC."""
    frame = _price_frame(
        rows=[
            {
                "Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 100.0,
                "Volume": 1_000_000, "Dividends": 0.0, "Stock Splits": 0.0,
                "Adj Close": 100.0,
            },
            {
                "Open": 102.0, "High": 112.0, "Low": 92.0, "Close": 104.0,
                "Volume": 1_200_000, "Dividends": 0.5, "Stock Splits": 0.0,
                "Adj Close": 104.0,
            },
        ],
        dates=["2024-01-02", "2024-01-03"],
    )
    provider, fake = _make_provider()
    fake.history_behavior["AAPL"] = frame

    result = provider.get_price_history(
        PriceHistoryRequest(ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    )
    assert result.status == service_result.STATUS_SUCCESS
    bars = result.metadata["bars"]
    assert isinstance(bars, list) and len(bars) == 2
    assert result.rows_processed == len(bars)
    for bar in bars:
        assert isinstance(bar, PriceBar)
        assert isinstance(bar.date, date)
        assert bar.source_provider == "yahoo"
        assert bar.open_raw is not None and bar.close_raw is not None
        assert bar.open_adj is not None and bar.close_adj is not None
        assert not hasattr(bar, "volume_adj")
    assert bars[0].volume_raw == 1_000_000
    assert bars[1].dividend_amount == 0.5


# --------------------------------------------------------------------------- #
# 4b. split_ratio 0.0-sentinel translation (split-ratio convention fix, 2026-07-18)
# --------------------------------------------------------------------------- #
def test_split_ratio_zero_maps_to_none() -> None:
    """yfinance's ``0.0`` 'no split today' sentinel is translated to ``None``
    (missing), so ``daily_price_ingestion.py``'s existing missing-value
    default (``split_ratio = 1``) fires correctly downstream instead of
    ``0.0`` being written verbatim and tripping
    ``MutationDetector.is_explicit_split()``'s ``!= 1`` check."""
    frame = _price_frame(
        rows=[
            {
                "Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 100.0,
                "Volume": 1_000_000, "Dividends": 0.0, "Stock Splits": 0.0,
                "Adj Close": 100.0,
            },
        ],
        dates=["2024-01-02"],
    )
    provider, fake = _make_provider()
    fake.history_behavior["AAPL"] = frame

    result = provider.get_price_history(
        PriceHistoryRequest(ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    )
    bars = result.metadata["bars"]
    assert len(bars) == 1
    assert bars[0].split_ratio is None


def test_split_ratio_nonzero_passes_through_unchanged() -> None:
    """A real (non-zero) split ratio -- forward or reverse -- is passed
    through unmodified, not translated."""
    frame = _price_frame(
        rows=[
            {
                "Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 100.0,
                "Volume": 1_000_000, "Dividends": 0.0, "Stock Splits": 2.0,
                "Adj Close": 100.0,
            },
            {
                "Open": 50.0, "High": 55.0, "Low": 45.0, "Close": 50.0,
                "Volume": 900_000, "Dividends": 0.0, "Stock Splits": 0.5,
                "Adj Close": 50.0,
            },
        ],
        dates=["2024-01-02", "2024-01-03"],
    )
    provider, fake = _make_provider()
    fake.history_behavior["AAPL"] = frame

    result = provider.get_price_history(
        PriceHistoryRequest(ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    )
    bars = result.metadata["bars"]
    assert bars[0].split_ratio == 2.0
    assert bars[1].split_ratio == 0.5


# --------------------------------------------------------------------------- #
# 5. Inclusive end-date handling
# --------------------------------------------------------------------------- #
def test_inclusive_end_date_calls_vendor_with_exclusive_end() -> None:
    """yfinance is called with end == end_date + 1 day; end_date bar included."""
    frame = _price_frame(
        rows=[
            {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1, "Adj Close": 1.0},
            {"Open": 2.0, "High": 2.0, "Low": 2.0, "Close": 2.0, "Volume": 2, "Adj Close": 2.0},
            {"Open": 3.0, "High": 3.0, "Low": 3.0, "Close": 3.0, "Volume": 3, "Adj Close": 3.0},
        ],
        # Last row is after end_date and must be excluded from the output.
        dates=["2024-01-10", "2024-01-15", "2024-01-16"],
    )
    provider, fake = _make_provider()
    fake.history_behavior["MSFT"] = frame

    result = provider.get_price_history(
        PriceHistoryRequest(ticker="MSFT", start_date=date(2024, 1, 10), end_date=date(2024, 1, 15))
    )
    call = fake.history_calls[-1]
    assert call["start"] == date(2024, 1, 10)
    assert call["end"] == date(2024, 1, 16)  # end_date + 1 day
    assert call["auto_adjust"] is False
    assert call["actions"] is True
    out_dates = [bar.date for bar in result.metadata["bars"]]
    assert date(2024, 1, 15) in out_dates
    assert all(d <= date(2024, 1, 15) for d in out_dates)


# --------------------------------------------------------------------------- #
# 6. Single-day inclusive range
# --------------------------------------------------------------------------- #
def test_single_day_inclusive_range() -> None:
    """start == end is accepted and maps the single in-range bar."""
    frame = _price_frame(
        rows=[{"Open": 5.0, "High": 6.0, "Low": 4.0, "Close": 5.5, "Volume": 10, "Adj Close": 5.5}],
        dates=["2024-03-04"],
    )
    provider, fake = _make_provider()
    fake.history_behavior["NVDA"] = frame
    result = provider.get_price_history(
        PriceHistoryRequest(ticker="NVDA", start_date=date(2024, 3, 4), end_date=date(2024, 3, 4))
    )
    assert fake.history_calls[-1]["end"] == date(2024, 3, 5)
    bars = result.metadata["bars"]
    assert len(bars) == 1 and bars[0].date == date(2024, 3, 4)


# --------------------------------------------------------------------------- #
# 7. Adjusted-OHLC derivation
# --------------------------------------------------------------------------- #
def test_adjusted_ohlc_derivation_and_partial_warning() -> None:
    """Adjusted OHLC = raw * (Adj Close / Close); bad rows -> None + warnings."""
    frame = _price_frame(
        rows=[
            # Adj Close != Close -> derived factor 0.9.
            {"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 100.0, "Volume": 1, "Adj Close": 90.0},
            # Close == 0 -> adjusted fields None.
            {"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 0.0, "Volume": 2, "Adj Close": 9.0},
            # Missing Adj Close (NaN) -> adjusted fields None.
            {"Open": 20.0, "High": 21.0, "Low": 19.0, "Close": 20.0, "Volume": 3, "Adj Close": float("nan")},
        ],
        dates=["2024-02-01", "2024-02-02", "2024-02-05"],
    )
    provider, fake = _make_provider()
    fake.history_behavior["TSLA"] = frame
    result = provider.get_price_history(
        PriceHistoryRequest(ticker="TSLA", start_date=date(2024, 2, 1), end_date=date(2024, 2, 28))
    )
    bars = {bar.date: bar for bar in result.metadata["bars"]}

    good = bars[date(2024, 2, 1)]
    assert good.close_adj == pytest.approx(90.0)
    assert good.open_adj == pytest.approx(100.0 * (90.0 / 100.0))
    assert good.high_adj == pytest.approx(110.0 * 0.9)
    assert good.low_adj == pytest.approx(90.0 * 0.9)

    zero_close = bars[date(2024, 2, 2)]
    assert zero_close.open_adj is None and zero_close.close_adj is None
    missing_adj = bars[date(2024, 2, 5)]
    assert missing_adj.open_adj is None and missing_adj.close_adj is None

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.warnings


# --------------------------------------------------------------------------- #
# 8. Empty DataFrame is success, not failed
# --------------------------------------------------------------------------- #
def test_empty_frame_is_success_not_failure() -> None:
    """An empty vendor frame -> success + empty bars + rows_processed 0."""
    provider, fake = _make_provider()
    fake.history_behavior["AAPL"] = pd.DataFrame()
    result = provider.get_price_history(
        PriceHistoryRequest(ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    )
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["bars"] == []
    assert result.rows_processed == 0
    assert "error_detail" not in result.metadata


# --------------------------------------------------------------------------- #
# 9. Unknown symbol -> failed only when explicitly signaled
# --------------------------------------------------------------------------- #
def test_unknown_symbol_is_failed_unsupported_symbol() -> None:
    """A clearly-signaled unknown ticker -> failed + unsupported_symbol."""
    provider, fake = _make_provider()
    fake.history_behavior["BADSYM"] = _FakeConnectionError(
        "No data found, symbol may be delisted: BADSYM"
    )
    result = provider.get_price_history(
        PriceHistoryRequest(ticker="BADSYM", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.rows_processed == 0
    assert result.errors
    detail = result.metadata["error_detail"]
    assert isinstance(detail, ProviderErrorDetail)
    assert detail.kind == "unsupported_symbol"
    assert detail.symbol == "BADSYM"


# --------------------------------------------------------------------------- #
# 10. Transport error mapping
# --------------------------------------------------------------------------- #
def test_rate_limit_maps_to_rate_limited() -> None:
    """A throttle condition -> failed + rate_limited."""
    provider, fake = _make_provider()
    fake.history_behavior["AAPL"] = _FakeRateLimitError("Too Many Requests. Rate limited.")
    result = provider.get_price_history(
        PriceHistoryRequest(ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.rows_processed == 0
    assert result.metadata["error_detail"].kind == "rate_limited"


def test_network_error_maps_to_provider_unavailable() -> None:
    """A network failure -> failed + provider_unavailable (a §9 kind)."""
    provider, fake = _make_provider()
    fake.history_behavior["AAPL"] = _FakeConnectionError("Connection timed out; max retries exceeded")
    result = provider.get_price_history(
        PriceHistoryRequest(ticker="AAPL", start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    )
    assert result.status == service_result.STATUS_FAILED
    detail = result.metadata["error_detail"]
    assert detail.kind in {"provider_unavailable", "rate_limited"}
    assert detail.kind in pi.PROVIDER_ERROR_KINDS


# --------------------------------------------------------------------------- #
# 11. list_symbols does not scrape
# --------------------------------------------------------------------------- #
def test_list_symbols_deferred_is_empty_success_no_network() -> None:
    """V1 list_symbols (no source) -> success + empty + rows 0, no network."""
    provider, fake = _make_provider()
    result = provider.list_symbols()
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["symbols"] == []
    assert result.rows_processed == 0
    assert fake.network_calls() == 0


def test_list_symbols_maps_injected_static_source_with_filter() -> None:
    """An injected static source is mapped and filtered, still without network."""
    source = [
        TickerInfo(ticker="SPY", symbol_type=constants.SYMBOL_TYPE_ETF),
        TickerInfo(ticker="^VIX", symbol_type=constants.SYMBOL_TYPE_INDEX),
        TickerInfo(ticker="AAPL", symbol_type=constants.SYMBOL_TYPE_STOCK),
    ]
    provider, fake = _make_provider(symbol_source=source)
    all_result = provider.list_symbols()
    assert all_result.rows_processed == 3
    etf_result = provider.list_symbols(symbol_type=constants.SYMBOL_TYPE_ETF)
    assert [info.ticker for info in etf_result.metadata["symbols"]] == ["SPY"]
    assert fake.network_calls() == 0


# --------------------------------------------------------------------------- #
# 12. get_earnings best-effort empty success
# --------------------------------------------------------------------------- #
def test_get_earnings_no_data_is_empty_success() -> None:
    """No reliable earnings data -> success + empty events + rows 0."""
    provider, fake = _make_provider()  # default_calendar is None
    result = provider.get_earnings("AAPL")
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["events"] == []
    assert result.rows_processed == 0


def test_get_earnings_event_is_low_confidence_yahoo() -> None:
    """A produced event is low-confidence, session unknown, source yahoo."""
    provider, fake = _make_provider()
    fake.calendar_behavior["AAPL"] = {"Earnings Date": [date(2024, 5, 2)]}
    result = provider.get_earnings("AAPL")
    assert result.rows_processed == 1
    event = result.metadata["events"][0]
    assert isinstance(event, EarningsEvent)
    assert event.earnings_date == date(2024, 5, 2)
    assert event.confidence == "low"
    assert event.session == "unknown"
    assert event.source_provider == "yahoo"


# --------------------------------------------------------------------------- #
# 13. VIX mapping
# --------------------------------------------------------------------------- #
def test_vix_close_raw_equals_close_adj_volume_tolerated() -> None:
    """^VIX maps with close_raw == close_adj and tolerates null/zero volume."""
    frame = _price_frame(
        rows=[{"Open": 18.0, "High": 19.0, "Low": 17.0, "Close": 18.5, "Volume": 0, "Adj Close": 18.5}],
        dates=["2024-01-02"],
    )
    provider, fake = _make_provider()
    fake.history_behavior["^VIX"] = frame
    result = provider.get_price_history(
        PriceHistoryRequest(
            ticker="^VIX",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            symbol_type=constants.SYMBOL_TYPE_INDEX,
        )
    )
    assert result.status == service_result.STATUS_SUCCESS
    bar = result.metadata["bars"][0]
    assert bar.close_raw == bar.close_adj == 18.5
    assert bar.open_raw == bar.open_adj
    assert bar.volume_raw in (0, None)


# --------------------------------------------------------------------------- #
# 14. Constructor performs no network
# --------------------------------------------------------------------------- #
def test_constructor_performs_no_network() -> None:
    """Constructing the provider makes zero vendor/network calls."""
    fake = FakeYF()
    YahooProvider(yf_module=fake)
    assert fake.ticker_calls == []
    assert fake.history_calls == []
    assert fake.calendar_calls == []
    assert fake.network_calls() == 0


# --------------------------------------------------------------------------- #
# 15. Yahoo access isolated to the provider file
# --------------------------------------------------------------------------- #
def _code_only(source: str) -> str:
    """Strip comments and string literals so only executable tokens remain."""
    pieces: list[str] = []
    readline = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type in (token.COMMENT, token.STRING):
            continue
        if tok.type == getattr(token, "FSTRING_MIDDLE", -1):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)


def _app_python_files() -> list[Path]:
    app_dir = Path(pi.__file__).resolve().parent.parent
    return sorted(app_dir.rglob("*.py"))


def test_only_yahoo_provider_references_yfinance() -> None:
    """Among ``app/**``, only yahoo_provider.py has executable ``yfinance``."""
    yahoo_path = Path(yp.__file__).resolve()
    for path in _app_python_files():
        code = _code_only(path.read_text(encoding="utf-8"))
        if path.resolve() == yahoo_path:
            assert "yfinance" in code  # the provider does reference it (lazily)
        else:
            assert "yfinance" not in code, str(path)


def test_yahoo_provider_has_no_db_access_and_no_print() -> None:
    """yahoo_provider.py imports no DuckDB / app.database and uses no print()."""
    code = _code_only(Path(yp.__file__).read_text(encoding="utf-8"))
    assert "duckdb" not in code
    assert "app.database" not in code
    assert "print(" not in code
    for module in ("requests", "urllib", "socket"):
        assert not re.search(rf"\b{module}\b", code), module


# --------------------------------------------------------------------------- #
# 16. Style
# --------------------------------------------------------------------------- #
def test_module_has_docstring() -> None:
    """The provider module has a module-level docstring."""
    assert yp.__doc__ is not None and yp.__doc__.strip()


def test_all_public_methods_have_type_hints() -> None:
    """Every YahooProvider method annotates all params (except self) and return."""
    for name, method in inspect.getmembers(YahooProvider, predicate=inspect.isfunction):
        if name.startswith("__") and name != "__init__":
            continue
        sig = inspect.signature(method)
        assert sig.return_annotation is not inspect.Signature.empty, f"{name} return"
        for pname, param in sig.parameters.items():
            if pname == "self":
                continue
            assert param.annotation is not inspect.Parameter.empty, f"{name}.{pname}"
