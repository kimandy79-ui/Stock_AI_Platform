"""Tests for Module 04 — Provider Interface.

Covers, per ``PROVIDER_INTERFACE_SPEC.md`` §13 and the Module 04 coding prompt:

- import smoke: ``MarketDataProvider``, all DTOs, ``PROVIDER_ERROR_KINDS``;
- abstract enforcement: the base class cannot be instantiated directly and an
  incomplete subclass raises ``TypeError`` on instantiation;
- a complete in-test ``FakeProvider`` instantiates and each method returns a
  valid ``ServiceResult``;
- signature conformance via ``inspect.signature`` (names, parameters, return
  annotations) against the spec §7.2;
- DTO construction for every DTO and frozen-ness (assignment raises
  ``FrozenInstanceError``);
- DTO validation: empty ticker, ``start_date > end_date``, invalid
  ``symbol_type`` all raise;
- empty-result semantics: ``success`` + empty list + ``rows_processed == 0``;
- error semantics: ``failed`` + ``error_detail`` is a ``ProviderErrorDetail``
  whose ``kind`` is in ``PROVIDER_ERROR_KINDS``;
- ``ServiceResult`` contract: real ``ServiceResult`` objects, valid status,
  documented metadata keys present;
- forbidden static scans of ``provider_interface.py``: no ``yfinance``, no
  ``requests``/``urllib``/``http``/``socket``, no ``duckdb``, no
  ``app.database``, no ``duckdb.connect(``, no ``print(``;
- no DB/network: the tests import neither the database layer nor any network
  client and open no DuckDB file;
- style: module docstring present; type hints on functions.

These tests perform no network calls and touch no DuckDB file. Module 04 never
opens a database, so no ``tmp_path`` DB redirection is needed; the suite also
avoids importing ``app.database`` entirely.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from datetime import date
from pathlib import Path

import pytest

from app.config import constants
from app.providers import provider_interface as pi
from app.providers.provider_interface import (
    EarningsEvent,
    MarketDataProvider,
    PriceBar,
    PriceHistoryRequest,
    ProviderCapabilities,
    ProviderErrorDetail,
    TickerInfo,
)
from app.utils import service_result
from app.utils.service_result import ServiceResult


_PROVIDER_NAME = "fake"


# --------------------------------------------------------------------------- #
# In-test fake provider (the only place a ServiceResult is constructed)
# --------------------------------------------------------------------------- #
class FakeProvider(MarketDataProvider):
    """Minimal in-memory provider used to exercise the contract.

    Honors the §7.3/§9 behavior contract for the handful of inputs the tests
    drive: empty results are ``success`` + empty list; an unsupported symbol is
    ``failed`` + ``ProviderErrorDetail``.
    """

    def __init__(self, provider_name: str = _PROVIDER_NAME) -> None:
        self.provider_name = provider_name

    def _run_id(self) -> str:
        # Deterministic, network-free run id for tests. Real providers may use
        # uuid4 (spec §8); the contract only requires a string.
        return "test-run-id"

    def get_capabilities(self) -> ServiceResult:
        caps = ProviderCapabilities(
            provider_name=self.provider_name,
            supports_daily_prices=True,
            supports_ticker_listing=True,
            supports_earnings=True,
            supports_adjusted_prices=True,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=self._run_id(),
            rows_processed=1,
            metadata={"capabilities": caps, "provider_name": self.provider_name},
        )

    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult:
        if request.ticker == "BADSYM":
            detail = ProviderErrorDetail(
                kind="unsupported_symbol",
                message="vendor does not know BADSYM",
                symbol=request.ticker,
            )
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=self._run_id(),
                rows_processed=0,
                errors=[detail.message],
                metadata={"error_detail": detail, "provider_name": self.provider_name},
            )
        # Valid query with no data -> success + empty list + rows_processed 0.
        bars: list[PriceBar] = []
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=self._run_id(),
            rows_processed=len(bars),
            metadata={"bars": bars, "provider_name": self.provider_name},
        )

    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        symbols: list[TickerInfo] = []
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=self._run_id(),
            rows_processed=len(symbols),
            metadata={"symbols": symbols, "provider_name": self.provider_name},
        )

    def get_earnings(self, ticker: str) -> ServiceResult:
        events: list[EarningsEvent] = []
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=self._run_id(),
            rows_processed=len(events),
            metadata={"events": events, "provider_name": self.provider_name},
        )


# --------------------------------------------------------------------------- #
# 1. Import smoke
# --------------------------------------------------------------------------- #
def test_public_symbols_importable() -> None:
    """All documented public symbols are present on the module."""
    assert MarketDataProvider is pi.MarketDataProvider
    for dto in (
        PriceBar,
        PriceHistoryRequest,
        TickerInfo,
        EarningsEvent,
        ProviderCapabilities,
        ProviderErrorDetail,
    ):
        assert isinstance(dto, type)
    assert isinstance(pi.PROVIDER_ERROR_KINDS, tuple)


def test_provider_error_kinds_exact() -> None:
    """``PROVIDER_ERROR_KINDS`` matches the spec §9 vocabulary exactly."""
    assert pi.PROVIDER_ERROR_KINDS == (
        "unsupported_symbol",
        "provider_unavailable",
        "rate_limited",
        "malformed_response",
        "unsupported_capability",
    )


# --------------------------------------------------------------------------- #
# 2. Abstract enforcement
# --------------------------------------------------------------------------- #
def test_base_class_cannot_instantiate() -> None:
    """``MarketDataProvider`` is abstract and cannot be instantiated."""
    with pytest.raises(TypeError):
        MarketDataProvider()  # type: ignore[abstract]


def test_incomplete_subclass_cannot_instantiate() -> None:
    """A subclass omitting a required method raises ``TypeError``."""

    class Incomplete(MarketDataProvider):
        def get_capabilities(self) -> ServiceResult:  # pragma: no cover - never built
            raise NotImplementedError

        def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult:
            raise NotImplementedError

        def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
            raise NotImplementedError

        # get_earnings intentionally omitted.

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


# --------------------------------------------------------------------------- #
# 3. Fake provider works
# --------------------------------------------------------------------------- #
def test_fake_provider_instantiates_and_returns_service_results() -> None:
    """A complete subclass instantiates; each method returns a ServiceResult."""
    provider = FakeProvider()
    assert isinstance(provider, MarketDataProvider)

    req = PriceHistoryRequest(
        ticker="AAPL",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
    )
    results = [
        provider.get_capabilities(),
        provider.get_price_history(req),
        provider.list_symbols(),
        provider.get_earnings("AAPL"),
    ]
    for result in results:
        assert isinstance(result, ServiceResult)
        assert result.has_valid_status()


# --------------------------------------------------------------------------- #
# 4. Signature conformance (spec §7.2)
# --------------------------------------------------------------------------- #
def test_method_signatures_match_spec() -> None:
    """Method names, parameters, and return annotations match spec §7.2."""
    expected_params = {
        "get_capabilities": ["self"],
        "get_price_history": ["self", "request"],
        "list_symbols": ["self", "symbol_type"],
        "get_earnings": ["self", "ticker"],
    }
    for name, params in expected_params.items():
        method = getattr(MarketDataProvider, name)
        sig = inspect.signature(method)
        assert list(sig.parameters) == params, name
        assert sig.return_annotation in (ServiceResult, "ServiceResult"), name

    # list_symbols default and annotation.
    list_sig = inspect.signature(MarketDataProvider.list_symbols)
    symbol_type_param = list_sig.parameters["symbol_type"]
    assert symbol_type_param.default is None
    assert symbol_type_param.annotation in ("str | None", str | None)

    # get_price_history request annotation.
    price_sig = inspect.signature(MarketDataProvider.get_price_history)
    request_param = price_sig.parameters["request"]
    assert request_param.annotation in (PriceHistoryRequest, "PriceHistoryRequest")

    # get_earnings ticker annotation.
    earnings_sig = inspect.signature(MarketDataProvider.get_earnings)
    ticker_param = earnings_sig.parameters["ticker"]
    assert ticker_param.annotation in (str, "str")


def test_all_four_methods_are_abstract() -> None:
    """All four contract methods are marked abstract."""
    assert MarketDataProvider.__abstractmethods__ == frozenset(
        {"get_capabilities", "get_price_history", "list_symbols", "get_earnings"}
    )


# --------------------------------------------------------------------------- #
# 5. DTO construction + frozen-ness
# --------------------------------------------------------------------------- #
def test_price_bar_construction_and_frozen() -> None:
    """``PriceBar`` builds with valid fields and is frozen."""
    bar = PriceBar(
        ticker="AAPL",
        date=date(2024, 1, 2),
        source_provider="yahoo",
        open_raw=10.0,
        high_raw=11.0,
        low_raw=9.5,
        close_raw=10.5,
        volume_raw=1_000_000,
        open_adj=10.0,
        high_adj=11.0,
        low_adj=9.5,
        close_adj=10.5,
        dividend_amount=0.0,
        split_ratio=1.0,
    )
    assert bar.ticker == "AAPL"
    assert not hasattr(bar, "volume_adj")
    with pytest.raises(dataclasses.FrozenInstanceError):
        bar.ticker = "MSFT"  # type: ignore[misc]


def test_price_bar_has_no_volume_adj_field() -> None:
    """``PriceBar`` must not declare a ``volume_adj`` field (spec §6.1, A4)."""
    field_names = {f.name for f in dataclasses.fields(PriceBar)}
    assert "volume_adj" not in field_names
    expected = {
        "ticker",
        "date",
        "open_raw",
        "high_raw",
        "low_raw",
        "close_raw",
        "volume_raw",
        "open_adj",
        "high_adj",
        "low_adj",
        "close_adj",
        "dividend_amount",
        "split_ratio",
        "source_provider",
    }
    assert field_names == expected


def test_price_history_request_construction_and_frozen() -> None:
    """``PriceHistoryRequest`` builds and is frozen; default symbol_type stock."""
    req = PriceHistoryRequest(
        ticker="AAPL",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
    )
    assert req.symbol_type == constants.SYMBOL_TYPE_STOCK
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.ticker = "MSFT"  # type: ignore[misc]


def test_ticker_info_construction_and_frozen() -> None:
    """``TickerInfo`` builds with valid fields and is frozen."""
    info = TickerInfo(
        ticker="AAPL",
        symbol_type=constants.SYMBOL_TYPE_STOCK,
        company_name="Apple Inc.",
        exchange="NASDAQ",
        sector="Technology",
        industry="Consumer Electronics",
        security_type="common",
    )
    assert info.symbol_type == constants.SYMBOL_TYPE_STOCK
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.sector = "Financials"  # type: ignore[misc]


def test_earnings_event_construction_and_frozen() -> None:
    """``EarningsEvent`` builds with valid fields and is frozen."""
    event = EarningsEvent(
        ticker="AAPL",
        earnings_date=date(2024, 2, 1),
        confidence="high",
        source_provider="yahoo",
        session="post_market",
    )
    assert event.session == "post_market"
    assert event.session in pi.EARNINGS_SESSIONS
    assert event.confidence in pi.EARNINGS_CONFIDENCE_LEVELS
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.confidence = "low"  # type: ignore[misc]


def test_provider_capabilities_construction_and_frozen() -> None:
    """``ProviderCapabilities`` builds and is frozen."""
    caps = ProviderCapabilities(
        provider_name="yahoo",
        supports_daily_prices=True,
        supports_ticker_listing=True,
        supports_earnings=False,
        supports_adjusted_prices=True,
    )
    assert caps.supports_earnings is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.provider_name = "other"  # type: ignore[misc]


def test_provider_error_detail_construction_and_frozen() -> None:
    """``ProviderErrorDetail`` builds and is frozen."""
    detail = ProviderErrorDetail(
        kind="rate_limited",
        message="throttled",
        symbol="AAPL",
    )
    assert detail.kind in pi.PROVIDER_ERROR_KINDS
    with pytest.raises(dataclasses.FrozenInstanceError):
        detail.kind = "provider_unavailable"  # type: ignore[misc]


def test_optional_dto_fields_default_to_none() -> None:
    """Optional DTO fields default to ``None`` where the spec marks them so."""
    bar = PriceBar(ticker="AAPL", date=date(2024, 1, 2), source_provider="yahoo")
    assert bar.open_raw is None and bar.close_adj is None and bar.volume_raw is None
    info = TickerInfo(ticker="AAPL", symbol_type=constants.SYMBOL_TYPE_STOCK)
    assert info.company_name is None and info.sector is None
    event = EarningsEvent(
        ticker="AAPL",
        earnings_date=date(2024, 2, 1),
        confidence="low",
        source_provider="yahoo",
    )
    assert event.session is None


def test_all_dtos_are_frozen_dataclasses() -> None:
    """Every DTO is a frozen dataclass."""
    for dto in (
        PriceBar,
        PriceHistoryRequest,
        TickerInfo,
        EarningsEvent,
        ProviderCapabilities,
        ProviderErrorDetail,
    ):
        assert dataclasses.is_dataclass(dto)
        assert dto.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 6. DTO validation
# --------------------------------------------------------------------------- #
def test_empty_ticker_raises_for_each_dto() -> None:
    """An empty ticker raises ``ValueError`` for every ticker-bearing DTO."""
    with pytest.raises(ValueError):
        PriceBar(ticker="", date=date(2024, 1, 2), source_provider="yahoo")
    with pytest.raises(ValueError):
        PriceHistoryRequest(
            ticker="",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
        )
    with pytest.raises(ValueError):
        TickerInfo(ticker="", symbol_type=constants.SYMBOL_TYPE_STOCK)
    with pytest.raises(ValueError):
        EarningsEvent(
            ticker="",
            earnings_date=date(2024, 2, 1),
            confidence="high",
            source_provider="yahoo",
        )


def test_price_history_request_rejects_reversed_range() -> None:
    """``start_date > end_date`` raises ``ValueError``."""
    with pytest.raises(ValueError):
        PriceHistoryRequest(
            ticker="AAPL",
            start_date=date(2024, 2, 1),
            end_date=date(2024, 1, 1),
        )


def test_price_history_request_accepts_equal_dates() -> None:
    """A single-day inclusive range (``start == end``) is valid."""
    req = PriceHistoryRequest(
        ticker="AAPL",
        start_date=date(2024, 1, 15),
        end_date=date(2024, 1, 15),
    )
    assert req.start_date == req.end_date


def test_invalid_symbol_type_raises() -> None:
    """An out-of-vocabulary ``symbol_type`` raises for request and ticker info."""
    with pytest.raises(ValueError):
        PriceHistoryRequest(
            ticker="AAPL",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            symbol_type="bogus",
        )
    with pytest.raises(ValueError):
        TickerInfo(ticker="AAPL", symbol_type="bogus")


def test_all_allowed_symbol_types_accepted() -> None:
    """Every value in ``ALLOWED_SYMBOL_TYPES`` is accepted by the DTOs."""
    for symbol_type in constants.ALLOWED_SYMBOL_TYPES:
        req = PriceHistoryRequest(
            ticker="AAPL",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            symbol_type=symbol_type,
        )
        assert req.symbol_type == symbol_type
        info = TickerInfo(ticker="AAPL", symbol_type=symbol_type)
        assert info.symbol_type == symbol_type


# --------------------------------------------------------------------------- #
# 7. Empty-result semantics
# --------------------------------------------------------------------------- #
def test_empty_result_is_success_with_empty_list() -> None:
    """A valid no-data query returns success + empty list + rows_processed 0."""
    provider = FakeProvider()
    req = PriceHistoryRequest(
        ticker="AAPL",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
    )
    result = provider.get_price_history(req)
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["bars"] == []
    assert result.rows_processed == 0
    assert result.errors == []


def test_empty_symbols_and_events_are_success() -> None:
    """list_symbols and get_earnings empty results are success + empty list."""
    provider = FakeProvider()
    symbols_result = provider.list_symbols()
    assert symbols_result.status == service_result.STATUS_SUCCESS
    assert symbols_result.metadata["symbols"] == []
    assert symbols_result.rows_processed == 0

    events_result = provider.get_earnings("AAPL")
    assert events_result.status == service_result.STATUS_SUCCESS
    assert events_result.metadata["events"] == []
    assert events_result.rows_processed == 0


# --------------------------------------------------------------------------- #
# 8. Error semantics
# --------------------------------------------------------------------------- #
def test_error_result_carries_provider_error_detail() -> None:
    """An unsupported symbol returns failed + ProviderErrorDetail with valid kind."""
    provider = FakeProvider()
    req = PriceHistoryRequest(
        ticker="BADSYM",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
    )
    result = provider.get_price_history(req)
    assert result.status == service_result.STATUS_FAILED
    assert result.rows_processed == 0
    assert result.errors
    detail = result.metadata["error_detail"]
    assert isinstance(detail, ProviderErrorDetail)
    assert detail.kind in pi.PROVIDER_ERROR_KINDS
    assert detail.kind == "unsupported_symbol"
    assert detail.symbol == "BADSYM"


# --------------------------------------------------------------------------- #
# 9. ServiceResult contract
# --------------------------------------------------------------------------- #
def test_service_result_metadata_keys_present() -> None:
    """Each method's documented metadata key and ``provider_name`` are present."""
    provider = FakeProvider()
    req = PriceHistoryRequest(
        ticker="AAPL",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
    )

    caps = provider.get_capabilities()
    assert isinstance(caps.metadata["capabilities"], ProviderCapabilities)
    assert caps.metadata["provider_name"] == _PROVIDER_NAME

    bars = provider.get_price_history(req)
    assert "bars" in bars.metadata
    assert isinstance(bars.metadata["bars"], list)
    assert bars.metadata["provider_name"] == _PROVIDER_NAME

    symbols = provider.list_symbols()
    assert "symbols" in symbols.metadata

    events = provider.get_earnings("AAPL")
    assert "events" in events.metadata

    for result in (caps, bars, symbols, events):
        assert result.status in service_result.ALLOWED_STATUSES
        assert isinstance(result.run_id, str) and result.run_id


# --------------------------------------------------------------------------- #
# 10. Forbidden static scans (spec §13.11–13)
# --------------------------------------------------------------------------- #
def _interface_source() -> str:
    """Raw source text of the interface module (includes docstrings/comments)."""
    return Path(pi.__file__).read_text(encoding="utf-8")


def _interface_code_only() -> str:
    """Executable source with comments and string literals removed.

    Forbidden-pattern scans target *code* (imports, attribute access), not
    prose: the module docstring legitimately documents that it avoids
    ``yfinance`` / ``app.database`` / ``duckdb``. This mirrors the literal vs
    descriptive separation used in ``tests/test_schema_manager.py`` so honest
    documentation does not trip the scan, while any real import or call still
    does.
    """
    import io
    import token
    import tokenize

    src = _interface_source()
    pieces: list[str] = []
    readline = io.StringIO(src).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type in (token.COMMENT, token.STRING):
            continue
        if tok.type == getattr(token, "FSTRING_MIDDLE", -1):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)


def test_no_network_imports_in_source() -> None:
    """No network client is imported or referenced in the interface code."""
    code = _interface_code_only()
    assert "yfinance" not in code
    for module in ("requests", "urllib", "socket"):
        assert not re.search(rf"\b{module}\b", code), module
    assert "import http" not in code
    assert "from http" not in code


def test_no_database_access_in_source() -> None:
    """No DuckDB / database-layer access in the interface code."""
    code = _interface_code_only()
    assert "duckdb" not in code
    assert "app.database" not in code
    assert "duckdb.connect(" not in code


def test_no_print_in_source() -> None:
    """Library code must not use ``print(``."""
    assert "print(" not in _interface_code_only()


# --------------------------------------------------------------------------- #
# 11. No DB / network at import or run time
# --------------------------------------------------------------------------- #
def test_interface_module_does_not_import_database_layer() -> None:
    """The interface module references no ``app.database`` symbol."""
    # Importing app.providers must not have pulled in app.database via the
    # interface; the source scan above is the ground truth, and here we assert
    # the interface module's own globals carry no database handle.
    assert not hasattr(pi, "duckdb")
    assert not hasattr(pi, "duckdb_manager")


# --------------------------------------------------------------------------- #
# 12. Style
# --------------------------------------------------------------------------- #
def test_module_has_docstring() -> None:
    """The interface module has a module-level docstring."""
    assert pi.__doc__ is not None and pi.__doc__.strip()


def test_all_interface_methods_have_full_type_hints() -> None:
    """Every abstract method annotates all parameters (except self) and return.

    Walks ``MarketDataProvider``'s four methods via ``inspect.signature`` and
    asserts no parameter (other than ``self``) and no return is left without an
    annotation, per ``CODING_STANDARDS.md`` (type hints on every function).
    """
    method_names = sorted(MarketDataProvider.__abstractmethods__)
    assert method_names  # sanity: there are abstract methods to check
    for name in method_names:
        method = getattr(MarketDataProvider, name)
        sig = inspect.signature(method)
        assert sig.return_annotation is not inspect.Signature.empty, (
            f"{name} is missing a return annotation"
        )
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            assert param.annotation is not inspect.Parameter.empty, (
                f"{name} parameter {param_name!r} is missing a type annotation"
            )


def test_dto_post_init_methods_have_type_hints() -> None:
    """Each DTO ``__post_init__`` (where present) is annotated ``-> None``."""
    for dto in (PriceBar, PriceHistoryRequest, TickerInfo, EarningsEvent):
        post_init = dto.__dict__.get("__post_init__")
        assert post_init is not None, f"{dto.__name__} should define __post_init__"
        sig = inspect.signature(post_init)
        assert sig.return_annotation in (None, "None", type(None)), dto.__name__
