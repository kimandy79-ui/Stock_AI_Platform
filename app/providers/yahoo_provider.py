"""Module 05 — YahooProvider (concrete :class:`MarketDataProvider`).

This is the first concrete provider in the platform. It implements the frozen
Module 04 contract (``PROVIDER_INTERFACE_SPEC.md``) by fetching daily prices,
ticker metadata, and earnings dates from Yahoo via ``yfinance``. All Yahoo /
``yfinance`` access is confined to this single file (``ARCHITECTURE.md`` /
``MASTER_SPEC.md`` §5: "do not call Yahoo directly outside the provider
layer").

Scope (per the Module 05 coding prompt and ``PROVIDER_INTERFACE_SPEC.md``):

- subclasses :class:`app.providers.provider_interface.MarketDataProvider`
  (``abc.ABC``) and implements its four methods with the exact §7.2 signatures;
- returns the Module 04 DTOs wrapped in
  :class:`app.utils.service_result.ServiceResult`, honoring the §7.3 / §9
  success / empty / partial / error semantics;
- maps each Yahoo daily row to a :class:`PriceBar` with BOTH raw and derived
  adjusted OHLC (``MASTER_SPEC.md`` §7), corporate actions, and VIX handling
  (``MASTER_SPEC.md`` §5).

This module does **not** persist anything, open DuckDB, import ``app.database``,
validate business semantics, or do screening / scoring / trading / simulation /
dashboard work. It only fetches and returns provider-neutral DTOs.

``yfinance`` is imported lazily inside :meth:`YahooProvider.__init__` only when
no dependency is injected, so the module (and its tests) import cleanly even
where ``yfinance`` is not installed. ``pandas`` is **not** imported here: the
vendor DataFrame is consumed through its duck-typed interface (``empty``,
``columns``, ``iterrows``) and converted to plain DTOs at this boundary, so no
DataFrame ever leaks out of the provider (``CODING_STANDARDS.md``: "prefer
Polars, pandas only when unavoidable").
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Final, Iterable

from app.config import constants
from app.providers.provider_interface import (
    EarningsEvent,
    MarketDataProvider,
    PriceBar,
    PriceHistoryRequest,
    ProviderCapabilities,
    ProviderErrorDetail,
    TickerInfo,
)
from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

_LOG = logging_config.get_logger(__name__)

# Provider identity carried in ``metadata['provider_name']`` and
# ``PriceBar.source_provider`` / ``EarningsEvent.source_provider``.
PROVIDER_NAME: Final[str] = "yahoo"

# Neutral symbol whose adjusted close equals its raw close (MASTER_SPEC.md §5).
_VIX_SYMBOL: Final[str] = constants.BENCHMARK_VIX  # "^VIX"

# Documented metadata keys (PROVIDER_INTERFACE_SPEC.md §8).
_KEY_CAPABILITIES: Final[str] = "capabilities"
_KEY_BARS: Final[str] = "bars"
# Multi-ticker batch download (get_price_history_many) carries a per-ticker
# mapping under this key in addition to a flattened ``bars`` list. The flat
# ``bars`` key is kept so the batch result is still readable by code that only
# knows the single-ticker contract.
_KEY_BARS_BY_TICKER: Final[str] = "bars_by_ticker"
_KEY_SYMBOLS: Final[str] = "symbols"
_KEY_EVENTS: Final[str] = "events"
_KEY_ERROR_DETAIL: Final[str] = "error_detail"
_KEY_PROVIDER_NAME: Final[str] = "provider_name"

# Vendor (yfinance / Yahoo) column names consumed at this boundary
# (CLARIFICATION 4).
_COL_OPEN: Final[str] = "Open"
_COL_HIGH: Final[str] = "High"
_COL_LOW: Final[str] = "Low"
_COL_CLOSE: Final[str] = "Close"
_COL_VOLUME: Final[str] = "Volume"
_COL_DIVIDENDS: Final[str] = "Dividends"
_COL_SPLITS: Final[str] = "Stock Splits"
_COL_ADJ_CLOSE: Final[str] = "Adj Close"

# Exception-message tokens used to classify expected §9 transport conditions.
_RATE_LIMIT_TOKENS: Final[tuple[str, ...]] = (
    "ratelimit",
    "rate limit",
    "too many requests",
    "429",
)
_UNKNOWN_SYMBOL_TOKENS: Final[tuple[str, ...]] = (
    "delisted",
    "no data found",
    "not found",
    "no timezone found",
    "unknown symbol",
    "invalid symbol",
    "symbol may be delisted",
)


class YahooProvider(MarketDataProvider):
    """Yahoo-backed :class:`MarketDataProvider` (research-grade V1).

    Parameters
    ----------
    yf_module:
        Optional injected ``yfinance``-like dependency exposing
        ``Ticker(symbol)`` whose result has ``history(...)`` and ``calendar``.
        When ``None`` (production), the real ``yfinance`` module is imported
        lazily inside ``__init__``. Injecting a fake lets the offline tests
        exercise mapping and error handling without any network access
        (CLARIFICATION 10).
    symbol_source:
        Optional static universe used by :meth:`list_symbols`. V1 does **not**
        scrape Yahoo or attempt full US-universe discovery (CLARIFICATION 8);
        full universe construction is Module 06. When this is ``None``,
        ``list_symbols`` returns an empty success and
        ``get_capabilities`` reports ``supports_ticker_listing=False``. When a
        list of :class:`TickerInfo` is supplied (e.g. in tests), it is mapped
        and ``supports_ticker_listing`` becomes ``True``.

    Notes
    -----
    ``__init__`` performs **no** network calls and **no** Yahoo access; the only
    side effect when ``yf_module`` is omitted is a local ``import yfinance``
    (CLARIFICATION 10).
    """

    def __init__(
        self,
        yf_module: Any | None = None,
        symbol_source: Iterable[TickerInfo] | None = None,
    ) -> None:
        if yf_module is None:
            # Lazy, network-free import. Confined to this provider file so no
            # other module imports yfinance (ARCHITECTURE.md / MASTER_SPEC §5).
            import yfinance as _yfinance  # noqa: PLC0415 - intentional lazy import

            yf_module = _yfinance
        self._yf: Any = yf_module
        self._symbol_source: tuple[TickerInfo, ...] | None = (
            tuple(symbol_source) if symbol_source is not None else None
        )

    # ----------------------------------------------------------------- #
    # Capabilities (PROVIDER_INTERFACE_SPEC.md §7.1, CLARIFICATION 12)
    # ----------------------------------------------------------------- #
    def get_capabilities(self) -> ServiceResult:
        """Return provider capabilities (pure metadata, no network).

        ``metadata['capabilities']`` carries a :class:`ProviderCapabilities`.
        ``supports_daily_prices`` and ``supports_adjusted_prices`` are ``True``;
        ``supports_earnings`` is ``True`` (best-effort calendar path);
        ``supports_ticker_listing`` is ``True`` only when a static
        ``symbol_source`` was injected (CLARIFICATION 8 / 12).
        """
        run_id = self._new_run_id()
        caps = ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            supports_daily_prices=True,
            supports_ticker_listing=self._symbol_source is not None,
            supports_earnings=True,
            supports_adjusted_prices=True,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={_KEY_CAPABILITIES: caps, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    # ----------------------------------------------------------------- #
    # Price history (PROVIDER_INTERFACE_SPEC.md §7.2/§10/§11, CLARIFICATIONS 1-7)
    # ----------------------------------------------------------------- #
    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult:
        """Return daily OHLCV bars for ``request.ticker`` over the inclusive range.

        ``metadata['bars']`` carries ``list[PriceBar]`` (possibly empty);
        ``rows_processed`` is the number of bars. yfinance treats ``end`` as
        exclusive, so this method calls it with ``end = end_date + 1 day``
        (CLARIFICATION 3) while returning DTOs that honor the inclusive
        ``[start_date, end_date]`` contract.
        """
        run_id = self._new_run_id()
        log = logging_config.get_logger(__name__, run_id)

        vendor_symbol = self._to_vendor_symbol(request.ticker)
        # CLARIFICATION 3: inclusive end -> exclusive yfinance end.
        vendor_end = request.end_date + datetime.timedelta(days=1)
        log.info(
            "fetching price history ticker=%s start=%s end=%s (vendor_end=%s)",
            request.ticker,
            request.start_date,
            request.end_date,
            vendor_end,
        )

        try:
            ticker_obj = self._yf.Ticker(vendor_symbol)
            frame = ticker_obj.history(
                start=request.start_date,
                end=vendor_end,
                auto_adjust=False,
                actions=True,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a §9 ServiceResult
            return self._transport_failure(
                run_id, log, _KEY_BARS, exc, symbol=request.ticker
            )

        if self._frame_is_empty(frame):
            log.info("empty price history for ticker=%s (valid, no data)", request.ticker)
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id=run_id,
                rows_processed=0,
                warnings=[f"No price rows returned for {request.ticker} in range."],
                metadata={_KEY_BARS: [], _KEY_PROVIDER_NAME: PROVIDER_NAME},
            )

        try:
            bars, warnings = self._map_price_frame(frame, request)
        except Exception as exc:  # noqa: BLE001 - unparseable payload -> §9
            detail = ProviderErrorDetail(
                kind="malformed_response",
                message=f"Could not parse Yahoo price payload for {request.ticker}: {exc}",
                symbol=request.ticker,
            )
            log.error("malformed price payload ticker=%s: %s", request.ticker, exc)
            return self._failed(run_id, _KEY_BARS, detail)

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if warnings
            else service_result.STATUS_SUCCESS
        )
        log.info(
            "mapped %d price bar(s) for ticker=%s status=%s",
            len(bars),
            request.ticker,
            status,
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=len(bars),
            warnings=list(warnings),
            metadata={_KEY_BARS: bars, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    # ----------------------------------------------------------------- #
    # Multi-ticker batch price history (additive; backfill-oriented).
    # ----------------------------------------------------------------- #
    def get_price_history_many(
        self,
        tickers: list[str],
        start_date: datetime.date,
        end_date: datetime.date,
        symbol_type: str = constants.SYMBOL_TYPE_STOCK,
    ) -> ServiceResult:
        """Return daily OHLCV bars for many symbols in one vendor download.

        This is an *additive* convenience over :meth:`get_price_history`,
        intended for historical backfill where issuing one network call per
        ticker invites Yahoo throttling. It is the only place a multi-ticker
        ``yf.download`` is allowed (still inside the provider layer); the
        single-ticker contract and behavior of :meth:`get_price_history` are
        unchanged.

        Semantics
        ---------
        - Inclusive ``[start_date, end_date]`` is preserved exactly as in
          :meth:`get_price_history` (vendor ``end`` is exclusive, so the call
          uses ``end_date + 1 day``); per-ticker mapping reuses
          :meth:`_map_price_frame`, so raw/adjusted OHLCV, ``^VIX`` identity,
          dividends/splits, and per-row warnings are identical.
        - Per-ticker isolation: a missing/empty/unparseable single ticker yields
          an empty bar list for that ticker plus a warning; it does **not** fail
          the whole call. Only a failure of the underlying ``yf.download`` call
          itself (transport / rate limit) produces a ``failed`` result.

        Returns
        -------
        ServiceResult
            ``metadata['bars_by_ticker']`` maps each requested ticker to its
            ``list[PriceBar]`` (empty list when no data). ``metadata['bars']``
            is the flattened list across all tickers. ``rows_processed`` is the
            total bar count.
        """
        run_id = self._new_run_id()
        log = logging_config.get_logger(__name__, run_id)

        # Preserve the single-ticker validation contract per symbol/range.
        if start_date > end_date:
            detail = ProviderErrorDetail(
                kind="malformed_response",
                message=(
                    "get_price_history_many requires start_date <= end_date "
                    f"(got {start_date!r} > {end_date!r})"
                ),
                symbol=None,
            )
            return self._failed_batch(run_id, [], detail)

        requested = [t for t in dict.fromkeys(tickers) if t]  # de-dupe, keep order
        if not requested:
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id=run_id,
                rows_processed=0,
                metadata={
                    _KEY_BARS_BY_TICKER: {},
                    _KEY_BARS: [],
                    _KEY_PROVIDER_NAME: PROVIDER_NAME,
                },
            )

        vendor_symbols = [self._to_vendor_symbol(t) for t in requested]
        vendor_end = end_date + datetime.timedelta(days=1)  # inclusive -> exclusive
        log.info(
            "batch download tickers=%d start=%s end=%s (vendor_end=%s)",
            len(requested),
            start_date,
            end_date,
            vendor_end,
        )

        try:
            frame = self._yf.download(
                tickers=vendor_symbols,
                start=start_date,
                end=vendor_end,
                group_by="ticker",
                auto_adjust=False,
                actions=True,
                threads=True,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a §9 failed result
            kind = self._classify_transport_exception(exc)
            message = (
                f"Yahoo batch download for {len(requested)} ticker(s) failed "
                f"({type(exc).__name__}): {exc}"
            )
            log.error("batch transport failure kind=%s: %s", kind, exc)
            detail = ProviderErrorDetail(kind=kind, message=message, symbol=None)
            return self._failed_batch(run_id, requested, detail)

        single = len(requested) == 1
        bars_by_ticker: dict[str, list[PriceBar]] = {}
        flat_bars: list[PriceBar] = []
        warnings: list[str] = []

        for ticker in requested:
            try:
                subframe = self._subframe_for_ticker(frame, ticker, single)
            except Exception as exc:  # noqa: BLE001 - isolate one ticker
                bars_by_ticker[ticker] = []
                warnings.append(
                    f"{ticker}: could not extract sub-frame from batch ({exc})."
                )
                continue

            if self._frame_is_empty(subframe):
                bars_by_ticker[ticker] = []
                warnings.append(f"No price rows returned for {ticker} in range.")
                continue

            request = PriceHistoryRequest(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                symbol_type=symbol_type,
            )
            try:
                bars, bar_warnings = self._map_price_frame(subframe, request)
            except Exception as exc:  # noqa: BLE001 - isolate one ticker
                bars_by_ticker[ticker] = []
                warnings.append(
                    f"{ticker}: could not parse batch price payload ({exc})."
                )
                continue

            bars_by_ticker[ticker] = bars
            flat_bars.extend(bars)
            warnings.extend(f"{ticker}: {w}" for w in bar_warnings)
            if not bars:
                warnings.append(f"No price rows returned for {ticker} in range.")

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if warnings
            else service_result.STATUS_SUCCESS
        )
        log.info(
            "batch mapped tickers=%d total_bars=%d status=%s",
            len(requested),
            len(flat_bars),
            status,
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=len(flat_bars),
            warnings=warnings,
            metadata={
                _KEY_BARS_BY_TICKER: bars_by_ticker,
                _KEY_BARS: flat_bars,
                _KEY_PROVIDER_NAME: PROVIDER_NAME,
            },
        )

    def _subframe_for_ticker(
        self, frame: Any, ticker: str, single: bool
    ) -> Any:
        """Return the per-ticker sub-frame from a ``group_by='ticker'`` download.

        ``yf.download(..., group_by="ticker")`` yields columns keyed by ticker
        (a MultiIndex), so ``frame[ticker]`` is the per-symbol OHLCV frame. For a
        single-ticker request yfinance may instead return a flat frame with no
        ticker level; in that case the whole frame is the sub-frame. Frame
        access is duck-typed (no pandas import at this boundary).
        """
        try:
            sub = frame[ticker]
        except Exception:  # noqa: BLE001 - flat single-ticker frame, or absent
            sub = None
        if sub is not None and not self._frame_is_empty(sub):
            return sub
        if single:
            return frame
        return sub

    def _failed_batch(
        self, run_id: str, requested: list[str], detail: ProviderErrorDetail
    ) -> ServiceResult:
        """Build a ``failed`` batch result with empty per-ticker lists."""
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[detail.message],
            metadata={
                _KEY_BARS_BY_TICKER: {t: [] for t in requested},
                _KEY_BARS: [],
                _KEY_ERROR_DETAIL: detail,
                _KEY_PROVIDER_NAME: PROVIDER_NAME,
            },
        )

    # ----------------------------------------------------------------- #
    # Symbol listing (PROVIDER_INTERFACE_SPEC.md §7.2, CLARIFICATION 8)
    # ----------------------------------------------------------------- #
    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        """Return known symbols, optionally filtered to one ``symbol_type``.

        V1 never scrapes Yahoo (CLARIFICATION 8). With no injected
        ``symbol_source`` this returns ``success`` + empty ``symbols`` +
        ``rows_processed == 0`` and a warning that enumeration is deferred to
        Module 06. With an injected static source it maps (and optionally
        filters) it to :class:`TickerInfo`. Either way it performs no network
        access.
        """
        run_id = self._new_run_id()

        if self._symbol_source is None:
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id=run_id,
                rows_processed=0,
                warnings=[
                    "Symbol enumeration is deferred to Module 06; "
                    "YahooProvider V1 does not scrape the Yahoo universe."
                ],
                metadata={_KEY_SYMBOLS: [], _KEY_PROVIDER_NAME: PROVIDER_NAME},
            )

        symbols: list[TickerInfo] = [
            info
            for info in self._symbol_source
            if symbol_type is None or info.symbol_type == symbol_type
        ]
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(symbols),
            metadata={_KEY_SYMBOLS: symbols, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    # ----------------------------------------------------------------- #
    # Earnings (PROVIDER_INTERFACE_SPEC.md §7.2, CLARIFICATION 9)
    # ----------------------------------------------------------------- #
    def get_earnings(self, ticker: str) -> ServiceResult:
        """Return best-effort earnings events for ``ticker``.

        Reads a candidate earnings date from the yfinance ``Ticker.calendar``
        attribute (no web scraping). When no reliable date is found, returns
        ``success`` + empty ``events`` + ``rows_processed == 0``. Produced
        events use ``confidence == "low"``, ``session == "unknown"``, and
        ``source_provider == "yahoo"`` (CLARIFICATION 9).
        """
        run_id = self._new_run_id()
        log = logging_config.get_logger(__name__, run_id)

        vendor_symbol = self._to_vendor_symbol(ticker)
        try:
            ticker_obj = self._yf.Ticker(vendor_symbol)
            calendar = getattr(ticker_obj, "calendar", None)
        except Exception as exc:  # noqa: BLE001 - mapped to a §9 ServiceResult
            return self._transport_failure(
                run_id, log, _KEY_EVENTS, exc, symbol=ticker
            )

        warnings: list[str] = []
        try:
            earnings_dates = self._extract_earnings_dates(calendar)
        except Exception as exc:  # noqa: BLE001 - best-effort: degrade to empty
            log.warning("could not parse earnings calendar for ticker=%s: %s", ticker, exc)
            earnings_dates = []
            warnings.append(f"Earnings calendar for {ticker} was unparseable; returning none.")

        events: list[EarningsEvent] = [
            EarningsEvent(
                ticker=ticker,
                earnings_date=earnings_date,
                session="unknown",
                confidence="low",
                source_provider=PROVIDER_NAME,
            )
            for earnings_date in earnings_dates
        ]
        if not events and not warnings:
            warnings.append(f"No reliable earnings date found for {ticker}.")

        log.info("earnings events for ticker=%s: %d", ticker, len(events))
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(events),
            warnings=warnings,
            metadata={_KEY_EVENTS: events, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    # ================================================================= #
    # Internal helpers (no network; all Yahoo access already happened above)
    # ================================================================= #
    @staticmethod
    def _new_run_id() -> str:
        """Return a fresh ``uuid4`` run id (spec §8 / assumption A7)."""
        return str(uuid.uuid4())

    @staticmethod
    def _to_vendor_symbol(ticker: str) -> str:
        """Translate a provider-neutral symbol to its Yahoo form.

        V1 needs no translation (Yahoo already uses ``SPY``, ``^VIX``, ``XLK``,
        ...), so this is the identity. It exists as the single internal hook for
        any future vendor quirk, keeping DTO ``ticker`` values provider-neutral
        (assumption A2).
        """
        return ticker

    @staticmethod
    def _frame_is_empty(frame: Any) -> bool:
        """Return ``True`` when the vendor frame carries no rows.

        An empty-but-valid frame is **not** an error (CLARIFICATION 7); it maps
        to a success with an empty ``bars`` list.
        """
        if frame is None:
            return True
        empty = getattr(frame, "empty", None)
        if isinstance(empty, bool):
            return empty
        try:
            return len(frame) == 0
        except TypeError:
            return True

    def _map_price_frame(
        self, frame: Any, request: PriceHistoryRequest
    ) -> tuple[list[PriceBar], list[str]]:
        """Map a vendor price frame to ``(bars, warnings)``.

        Honors the inclusive ``[start_date, end_date]`` contract, derives
        adjusted OHLC per CLARIFICATION 5, applies the ``^VIX`` identity
        (``close_raw == close_adj``), and records a warning whenever a row's
        adjusted fields cannot be derived.
        """
        columns = self._frame_columns(frame)
        is_vix = request.ticker == _VIX_SYMBOL
        bars: list[PriceBar] = []
        warnings: list[str] = []
        skipped_adj = 0

        for index_value, row in frame.iterrows():
            bar_date = self._to_date(index_value)
            if bar_date is None:
                warnings.append("Skipped a row with an unparseable date index.")
                continue
            # Enforce the inclusive contract regardless of vendor edge behavior.
            if bar_date < request.start_date or bar_date > request.end_date:
                continue

            open_raw = self._to_float(self._cell(row, _COL_OPEN, columns))
            high_raw = self._to_float(self._cell(row, _COL_HIGH, columns))
            low_raw = self._to_float(self._cell(row, _COL_LOW, columns))
            close_raw = self._to_float(self._cell(row, _COL_CLOSE, columns))
            volume_raw = self._to_int(self._cell(row, _COL_VOLUME, columns))
            dividend_amount = self._to_float(self._cell(row, _COL_DIVIDENDS, columns))
            split_ratio = self._to_float(self._cell(row, _COL_SPLITS, columns))
            adj_close = self._to_float(self._cell(row, _COL_ADJ_CLOSE, columns))

            open_adj, high_adj, low_adj, close_adj = self._adjusted_ohlc(
                is_vix=is_vix,
                open_raw=open_raw,
                high_raw=high_raw,
                low_raw=low_raw,
                close_raw=close_raw,
                adj_close=adj_close,
            )
            if not is_vix and close_adj is None:
                skipped_adj += 1

            bars.append(
                PriceBar(
                    ticker=request.ticker,
                    date=bar_date,
                    open_raw=open_raw,
                    high_raw=high_raw,
                    low_raw=low_raw,
                    close_raw=close_raw,
                    volume_raw=volume_raw,
                    open_adj=open_adj,
                    high_adj=high_adj,
                    low_adj=low_adj,
                    close_adj=close_adj,
                    dividend_amount=dividend_amount,
                    split_ratio=split_ratio,
                    source_provider=PROVIDER_NAME,
                )
            )

        if skipped_adj:
            warnings.append(
                f"{skipped_adj} of {len(bars)} bar(s) for {request.ticker} "
                "lacked usable Adj Close/Close and have null adjusted OHLC."
            )
        return bars, warnings

    @staticmethod
    def _adjusted_ohlc(
        *,
        is_vix: bool,
        open_raw: float | None,
        high_raw: float | None,
        low_raw: float | None,
        close_raw: float | None,
        adj_close: float | None,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """Derive ``(open_adj, high_adj, low_adj, close_adj)`` for one row.

        For ``^VIX`` the adjusted OHLC mirrors the raw OHLC so that
        ``close_raw == close_adj`` (``MASTER_SPEC.md`` §5). Otherwise the
        adjustment factor ``Adj Close / Close`` is applied (CLARIFICATION 5);
        if ``Close`` is missing/zero or ``Adj Close`` is missing, all adjusted
        fields are ``None``. ``adjustment_factor`` is a transient local only and
        is never exposed on a DTO (CLARIFICATION 6).
        """
        if is_vix:
            return open_raw, high_raw, low_raw, close_raw
        if close_raw is None or close_raw == 0 or adj_close is None:
            return None, None, None, None
        factor = adj_close / close_raw
        open_adj = open_raw * factor if open_raw is not None else None
        high_adj = high_raw * factor if high_raw is not None else None
        low_adj = low_raw * factor if low_raw is not None else None
        return open_adj, high_adj, low_adj, adj_close

    @staticmethod
    def _frame_columns(frame: Any) -> frozenset[str] | None:
        """Return the frame's column names as a set, or ``None`` if unknown."""
        columns = getattr(frame, "columns", None)
        if columns is None:
            return None
        try:
            return frozenset(str(name) for name in columns)
        except TypeError:
            return None

    @staticmethod
    def _cell(row: Any, name: str, columns: frozenset[str] | None) -> Any:
        """Return ``row[name]`` if the column exists, else ``None``."""
        if columns is not None and name not in columns:
            return None
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _is_missing(value: Any) -> bool:
        """Return ``True`` for ``None`` and NaN/NaT (which compare unequal to self)."""
        if value is None:
            return True
        try:
            return bool(value != value)
        except Exception:  # noqa: BLE001 - non-comparable -> treat as present
            return False

    @classmethod
    def _to_float(cls, value: Any) -> float | None:
        """Coerce a vendor cell to ``float`` or ``None`` (missing/non-numeric)."""
        if cls._is_missing(value):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _to_int(cls, value: Any) -> int | None:
        """Coerce a vendor cell to ``int`` or ``None`` (missing/non-numeric)."""
        if cls._is_missing(value):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_date(index_value: Any) -> datetime.date | None:
        """Convert a frame index entry to a plain ``datetime.date``.

        Handles ``datetime.datetime`` / pandas ``Timestamp`` (a ``datetime``
        subclass), plain ``datetime.date``, anything exposing ``.date()``, and
        ISO strings. Returns ``None`` when no date can be determined.
        """
        if isinstance(index_value, datetime.datetime):
            return index_value.date()
        if isinstance(index_value, datetime.date):
            return index_value
        to_date = getattr(index_value, "date", None)
        if callable(to_date):
            try:
                candidate = to_date()
            except Exception:  # noqa: BLE001
                candidate = None
            if isinstance(candidate, datetime.datetime):
                return candidate.date()
            if isinstance(candidate, datetime.date):
                return candidate
        if isinstance(index_value, str):
            try:
                return datetime.date.fromisoformat(index_value[:10])
            except ValueError:
                return None
        return None

    def _extract_earnings_dates(self, calendar: Any) -> list[datetime.date]:
        """Pull candidate earnings dates from a yfinance ``calendar`` object.

        Supports the modern ``dict`` form (``{"Earnings Date": [date, ...]}``)
        and a defensive DataFrame-like fallback. Returns a sorted, de-duplicated
        list; an absent/empty calendar yields ``[]`` (CLARIFICATION 9).
        """
        if calendar is None:
            return []

        raw_values: list[Any] = []
        if isinstance(calendar, dict):
            raw_values.extend(self._calendar_dict_values(calendar))
        else:
            raw_values.extend(self._calendar_frame_values(calendar))

        dates: list[datetime.date] = []
        for value in raw_values:
            converted = self._to_date(value)
            if converted is not None and converted not in dates:
                dates.append(converted)
        dates.sort()
        return dates

    @staticmethod
    def _calendar_dict_values(calendar: dict[Any, Any]) -> list[Any]:
        """Extract earnings-date candidates from a ``dict`` calendar."""
        values: list[Any] = []
        for key, value in calendar.items():
            if str(key).strip().lower() != "earnings date":
                continue
            if isinstance(value, (list, tuple, set)):
                values.extend(value)
            else:
                values.append(value)
        return values

    @staticmethod
    def _calendar_frame_values(calendar: Any) -> list[Any]:
        """Extract earnings-date candidates from a DataFrame-like calendar.

        Older yfinance returned a DataFrame whose ``Earnings Date`` lived under a
        labelled index/column. This reads it defensively without importing
        pandas; anything it cannot interpret yields no candidates.
        """
        loc = getattr(calendar, "loc", None)
        if loc is None:
            return []
        try:
            row = loc["Earnings Date"]
        except (KeyError, TypeError, IndexError):
            return []
        values_attr = getattr(row, "values", None)
        if values_attr is not None:
            try:
                return list(values_attr)
            except TypeError:
                return []
        if isinstance(row, (list, tuple)):
            return list(row)
        return [row]

    # ----------------------------------------------------------------- #
    # ServiceResult builders for §9 failures
    # ----------------------------------------------------------------- #
    def _transport_failure(
        self,
        run_id: str,
        log: Any,
        list_key: str,
        exc: Exception,
        *,
        symbol: str,
    ) -> ServiceResult:
        """Map an exception from a vendor call to a §9 ``failed`` ServiceResult.

        Recognized expected conditions (rate limit, unknown symbol, vendor
        unavailable) become a :class:`ProviderErrorDetail`; nothing is raised
        for these documented conditions (spec §7.3 / §9).
        """
        kind = self._classify_transport_exception(exc)
        message = f"Yahoo fetch for {symbol} failed ({type(exc).__name__}): {exc}"
        log.error("price/earnings transport failure symbol=%s kind=%s: %s", symbol, kind, exc)
        detail = ProviderErrorDetail(kind=kind, message=message, symbol=symbol)
        return self._failed(run_id, list_key, detail)

    @staticmethod
    def _classify_transport_exception(exc: Exception) -> str:
        """Classify a vendor exception into a §9 error ``kind``.

        Matches exception type/message tokens; unrecognized vendor-call failures
        default to ``provider_unavailable`` (the vendor/network failure class),
        keeping every documented §9 condition a returned ``ServiceResult``.
        """
        text = f"{type(exc).__name__} {exc}".lower()
        if any(token in text for token in _RATE_LIMIT_TOKENS):
            return "rate_limited"
        if any(token in text for token in _UNKNOWN_SYMBOL_TOKENS):
            return "unsupported_symbol"
        return "provider_unavailable"

    @staticmethod
    def _failed(run_id: str, list_key: str, detail: ProviderErrorDetail) -> ServiceResult:
        """Build a ``failed`` ServiceResult carrying ``error_detail`` (spec §9)."""
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[detail.message],
            metadata={
                list_key: [],
                _KEY_ERROR_DETAIL: detail,
                _KEY_PROVIDER_NAME: PROVIDER_NAME,
            },
        )


__all__ = ["YahooProvider", "PROVIDER_NAME"]
