"""Module 04 delta — Stooq OHLCV provider (Phase 4, second price source).

Concrete :class:`MarketDataProvider` backed by Stooq's free, keyless daily
CSV endpoint (``stooq.com/q/d/l``). This is the "second OHLCV provider /
yfinance fallback" requested by the Phase 4 coder note — reinterpreted
because :class:`app.providers.yahoo_provider.YahooProvider` *is* the
``yfinance``-backed provider already, so a literal "yfinance fallback for
yfinance" would be circular. Stooq is a genuinely independent data source
(different vendor, different outage domain), with :class:`YahooProvider`
remaining the default and this provider available as an explicit,
configurable fallback (callers choose which provider instance to construct;
no implicit provider-selection logic is added here or elsewhere).

Stooq's daily endpoint returns plain OHLCV only (no split/dividend
adjustment), so ``open_adj``/``high_adj``/``low_adj``/``close_adj`` are
always ``None`` here — :attr:`ProviderCapabilities.supports_adjusted_prices`
is ``False``, letting callers detect this rather than silently receiving
unadjusted values mislabeled as adjusted.

Two layers, deliberately separated for testability:

- :func:`parse_stooq_csv` is pure (no I/O); exercised directly with
  synthetic CSV text in ``tests/test_stooq_provider.py``.
- :class:`StooqProvider` is the thin HTTP-fetching wrapper (``requests``,
  imported lazily inside ``__init__``, mirroring Module 05's lazy
  ``yfinance`` import).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime
from typing import Any, Callable, Final

from app.providers.provider_interface import (
    MarketDataProvider,
    PriceBar,
    PriceHistoryRequest,
    ProviderCapabilities,
    ProviderErrorDetail,
)
from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

_LOG = logging_config.get_logger(__name__)

PROVIDER_NAME: Final[str] = "stooq"

_KEY_BARS: Final[str] = "bars"
_KEY_CAPABILITIES: Final[str] = "capabilities"
_KEY_ERROR_DETAIL: Final[str] = "error_detail"
_KEY_PROVIDER_NAME: Final[str] = "provider_name"

_STOOQ_URL_TEMPLATE: Final[str] = (
    "https://stooq.com/q/d/l/?s={symbol}&d1={d1}&d2={d2}&i=d"
)
_NOT_FOUND_MARKER: Final[str] = "N/D"

_EXPECTED_HEADER: Final[tuple[str, ...]] = (
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
)


def to_stooq_symbol(ticker: str) -> str:
    """Map a plain US-equity ticker to Stooq's ``<ticker>.us`` symbol form."""
    return f"{ticker.lower()}.us"


def parse_stooq_csv(
    csv_text: str, ticker: str, start_date: date, end_date: date
) -> tuple[list[PriceBar], list[str]]:
    """Parse Stooq's daily CSV text into ``PriceBar`` rows within range.

    Returns ``(bars, warnings)``. Stooq returns the literal text
    ``"No data"`` (no CSV body) for an unknown symbol, and rows with
    ``N/D`` fields for known-but-missing trading days; both are handled as
    "no bar for that day" rather than an error — an empty or partial result
    for a valid symbol is not itself a failure (§9 semantics).
    """
    stripped = csv_text.strip()
    if not stripped or stripped.lower().startswith("no data"):
        return [], []

    reader = csv.reader(io.StringIO(stripped))
    rows = list(reader)
    if not rows:
        return [], []

    header = tuple(rows[0])
    if header != _EXPECTED_HEADER:
        raise ValueError(f"Unexpected Stooq CSV header: {header!r}")

    bars: list[PriceBar] = []
    warnings: list[str] = []
    for row in rows[1:]:
        if len(row) != len(_EXPECTED_HEADER):
            warnings.append(f"Skipped malformed Stooq row for {ticker}: {row!r}")
            continue
        date_str, open_s, high_s, low_s, close_s, volume_s = row
        if _NOT_FOUND_MARKER in (open_s, high_s, low_s, close_s, volume_s):
            continue
        try:
            bar_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            warnings.append(f"Skipped unparseable Stooq date for {ticker}: {date_str!r}")
            continue
        if bar_date < start_date or bar_date > end_date:
            continue
        try:
            bars.append(
                PriceBar(
                    ticker=ticker,
                    date=bar_date,
                    open_raw=float(open_s),
                    high_raw=float(high_s),
                    low_raw=float(low_s),
                    close_raw=float(close_s),
                    volume_raw=int(float(volume_s)),
                    source_provider=PROVIDER_NAME,
                )
            )
        except ValueError:
            warnings.append(f"Skipped unparseable Stooq values for {ticker} on {date_str}")
    bars.sort(key=lambda b: b.date)
    return bars, warnings


class StooqProvider(MarketDataProvider):
    """Stooq-backed :class:`MarketDataProvider` (daily OHLCV only).

    Parameters
    ----------
    fetch_text:
        Injected ``Callable[[str], str]`` performing one GET returning raw
        CSV text. When ``None`` (production), a wrapper around
        ``requests.get`` is built lazily inside ``__init__`` (mirroring
        Module 05's lazy ``yfinance`` import), so tests can inject a fake and
        run fully offline.
    """

    def __init__(self, fetch_text: Callable[[str], str] | None = None) -> None:
        if fetch_text is None:
            import requests  # noqa: PLC0415 - intentional lazy import

            def _default_fetch(url: str) -> str:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                return response.text

            fetch_text = _default_fetch
        self._fetch_text = fetch_text

    # ------------------------------------------------------------------ #
    # Capabilities
    # ------------------------------------------------------------------ #
    def get_capabilities(self) -> ServiceResult:
        caps = ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            supports_daily_prices=True,
            supports_ticker_listing=False,
            supports_earnings=False,
            supports_adjusted_prices=False,
            supports_fundamentals=False,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=self._new_run_id(),
            rows_processed=1,
            metadata={_KEY_CAPABILITIES: caps, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    # ------------------------------------------------------------------ #
    # Price history
    # ------------------------------------------------------------------ #
    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult:
        run_id = self._new_run_id()
        log = logging_config.get_logger(__name__, run_id)

        symbol = to_stooq_symbol(request.ticker)
        url = _STOOQ_URL_TEMPLATE.format(
            symbol=symbol,
            d1=request.start_date.strftime("%Y%m%d"),
            d2=request.end_date.strftime("%Y%m%d"),
        )
        try:
            csv_text = self._fetch_text(url)
        except Exception as exc:  # noqa: BLE001 - mapped to a §9 ServiceResult
            detail = ProviderErrorDetail(
                kind="provider_unavailable",
                message=f"Stooq fetch failed for {request.ticker!r}: {exc}",
                symbol=request.ticker,
            )
            log.error("stooq fetch failed ticker=%s: %s", request.ticker, exc)
            return self._failed(run_id, detail)

        try:
            bars, warnings = parse_stooq_csv(
                csv_text, request.ticker, request.start_date, request.end_date
            )
        except ValueError as exc:
            detail = ProviderErrorDetail(
                kind="malformed_response",
                message=f"Could not parse Stooq CSV for {request.ticker!r}: {exc}",
                symbol=request.ticker,
            )
            log.error("malformed stooq csv ticker=%s: %s", request.ticker, exc)
            return self._failed(run_id, detail)

        if not bars and not warnings:
            log.info("empty price history for ticker=%s (valid, no data)", request.ticker)
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id=run_id,
                rows_processed=0,
                warnings=[f"No price rows returned for {request.ticker} in range."],
                metadata={_KEY_BARS: [], _KEY_PROVIDER_NAME: PROVIDER_NAME},
            )

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if warnings
            else service_result.STATUS_SUCCESS
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=len(bars),
            warnings=warnings,
            metadata={_KEY_BARS: bars, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    # ------------------------------------------------------------------ #
    # Unsupported capabilities
    # ------------------------------------------------------------------ #
    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        return self._unsupported("list_symbols", None)

    def get_earnings(self, ticker: str) -> ServiceResult:
        return self._unsupported("get_earnings", ticker)

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_run_id() -> str:
        return str(uuid.uuid4())

    def _unsupported(self, method_name: str, symbol: str | None) -> ServiceResult:
        detail = ProviderErrorDetail(
            kind="unsupported_capability",
            message=f"{method_name} is not supported by {PROVIDER_NAME} (daily-OHLCV-only provider)",
            symbol=symbol,
        )
        return self._failed(self._new_run_id(), detail)

    @staticmethod
    def _failed(run_id: str, detail: ProviderErrorDetail) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[detail.message],
            metadata={_KEY_ERROR_DETAIL: detail, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )


__all__ = [
    "StooqProvider",
    "parse_stooq_csv",
    "to_stooq_symbol",
    "PROVIDER_NAME",
]
