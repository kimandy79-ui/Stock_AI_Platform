"""Module 04 — Provider Interface (abstract market-data contract).

This module defines the single, provider-neutral abstraction through which the
entire platform obtains market data, together with the request/response DTOs
and the structured error vocabulary that downstream modules rely on. It is the
source-of-truth implementation of ``PROVIDER_INTERFACE_SPEC.md``.

Scope (per ``PROVIDER_INTERFACE_SPEC.md`` §3):

- defines :class:`MarketDataProvider` (``abc.ABC`` + ``@abstractmethod``);
- defines the provider DTOs (:class:`PriceBar`, :class:`PriceHistoryRequest`,
  :class:`TickerInfo`, :class:`EarningsEvent`, :class:`ProviderCapabilities`,
  :class:`ProviderErrorDetail`) as frozen dataclasses;
- defines provider error / status semantics (:data:`PROVIDER_ERROR_KINDS`).

This module is **interface only**. It performs no network calls, opens no
DuckDB connection, imports neither ``yfinance`` nor ``app.database``, and
implements no concrete provider. Every abstract method has an empty body;
concrete behavior (real Yahoo downloads) is Module 05.

All data-fetching methods return :class:`app.utils.service_result.ServiceResult`
with the domain DTOs carried in ``ServiceResult.metadata`` under the documented
keys (``PROVIDER_INTERFACE_SPEC.md`` §8).

Symbol-type and benchmark vocabularies are reused from
:mod:`app.config.constants`; this module does not redefine them.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.config import constants
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

# Consistency with project modules: every library module binds a logger even if
# it logs nothing yet. The interface itself emits no log records (it has no
# runtime behavior); concrete providers in Module 05 will use a bound logger.
_LOG = logging_config.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Error-kind vocabulary (PROVIDER_INTERFACE_SPEC.md §9)
# --------------------------------------------------------------------------- #
# Standard ``kind`` values for ProviderErrorDetail.kind. Defined as a module
# constant so downstream modules and tests can branch on error kind without
# parsing strings. Each maps to ServiceResult.status == "failed".
PROVIDER_ERROR_KINDS: Final[tuple[str, ...]] = (
    "unsupported_symbol",
    "provider_unavailable",
    "rate_limited",
    "malformed_response",
    "unsupported_capability",
)


# --------------------------------------------------------------------------- #
# Earnings value catalogs (PROVIDER_INTERFACE_SPEC.md §6.4, §9 session note)
# --------------------------------------------------------------------------- #
# Service-layer value catalogs (NOT DuckDB ENUM types — Module 04 creates no
# schema). ``session`` values come from SCHEMA_SPEC.md §5; ``confidence`` values
# from PROVIDER_INTERFACE_SPEC.md §6.4. These are defined for documentation and
# downstream validation; Module 04 does not enforce them in DTO __post_init__
# beyond what the spec requires (the spec mandates only a non-empty ticker on
# EarningsEvent).
EARNINGS_SESSIONS: Final[tuple[str, ...]] = (
    "pre_market",
    "post_market",
    "during_market",
    "unknown",
)

EARNINGS_CONFIDENCE_LEVELS: Final[tuple[str, ...]] = (
    "high",
    "medium",
    "low",
)


# --------------------------------------------------------------------------- #
# Domain DTOs (PROVIDER_INTERFACE_SPEC.md §6)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class PriceBar:
    """One daily OHLCV row for a single symbol (provider-neutral).

    Maps downstream onto ``daily_prices`` (``SCHEMA_SPEC.md`` §3.7), but
    Module 04 does not persist it. Per ``MASTER_SPEC.md`` §7, the provider
    returns both raw and adjusted OHLC plus ``volume_raw``; ``volume_adj`` is
    reserved/unused in V1 and is intentionally absent (spec §6.1, assumption
    A4).

    The dataclass is ``kw_only`` so the field order can mirror the
    ``PROVIDER_INTERFACE_SPEC.md`` §6.1 table verbatim (``source_provider`` last)
    even though required and optional fields are interleaved there.

    Schema-only fields (``data_quality_status``, ``mutation_flag``,
    ``adjustment_factor``, ``created_at``, ``updated_at``) are assigned by the
    ingestion/validation/mutation modules (08/09/10), not the vendor, and are
    intentionally not present here.
    """

    ticker: str
    date: date
    open_raw: float | None = None
    high_raw: float | None = None
    low_raw: float | None = None
    close_raw: float | None = None
    volume_raw: int | None = None
    open_adj: float | None = None
    high_adj: float | None = None
    low_adj: float | None = None
    close_adj: float | None = None
    dividend_amount: float | None = None
    split_ratio: float | None = None
    source_provider: str

    def __post_init__(self) -> None:
        """Validate light structural invariants (non-empty ticker only).

        A provider may legitimately return ``None`` OHLC for a missing or
        halted day; value validation is Module 09, not here.
        """
        if not self.ticker:
            raise ValueError("PriceBar.ticker must be a non-empty string")


@dataclass(frozen=True)
class PriceHistoryRequest:
    """A price-history query for one symbol over an inclusive date range.

    Range semantics are inclusive on both ends ``[start_date, end_date]``
    (``PROVIDER_INTERFACE_SPEC.md`` §10, assumption A1).
    """

    ticker: str
    start_date: date
    end_date: date
    symbol_type: str = constants.SYMBOL_TYPE_STOCK

    def __post_init__(self) -> None:
        """Validate ticker, range ordering, and symbol-type membership.

        Raises
        ------
        ValueError
            If ``ticker`` is empty, ``start_date > end_date``, or
            ``symbol_type`` is not in ``constants.ALLOWED_SYMBOL_TYPES``.
        """
        if not self.ticker:
            raise ValueError("PriceHistoryRequest.ticker must be a non-empty string")
        if self.start_date > self.end_date:
            raise ValueError(
                "PriceHistoryRequest requires start_date <= end_date "
                f"(got start_date={self.start_date!r}, end_date={self.end_date!r})"
            )
        if self.symbol_type not in constants.ALLOWED_SYMBOL_TYPES:
            raise ValueError(
                f"PriceHistoryRequest.symbol_type {self.symbol_type!r} not in "
                f"{constants.ALLOWED_SYMBOL_TYPES!r}"
            )


@dataclass(frozen=True)
class TickerInfo:
    """A universe / listing item (provider-neutral).

    Maps downstream onto a subset of ``ticker_master`` (``SCHEMA_SPEC.md``
    §3.4). Lifecycle flags (``active_flag``, ``delisted_flag``, ``first_seen``,
    ``last_seen``, ``last_updated``) are assigned by Module 06, not the
    provider, and are intentionally absent here.
    """

    ticker: str
    symbol_type: str
    company_name: str | None = None
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    security_type: str | None = None

    def __post_init__(self) -> None:
        """Validate ticker non-empty and symbol-type membership.

        Raises
        ------
        ValueError
            If ``ticker`` is empty or ``symbol_type`` is not in
            ``constants.ALLOWED_SYMBOL_TYPES``.
        """
        if not self.ticker:
            raise ValueError("TickerInfo.ticker must be a non-empty string")
        if self.symbol_type not in constants.ALLOWED_SYMBOL_TYPES:
            raise ValueError(
                f"TickerInfo.symbol_type {self.symbol_type!r} not in "
                f"{constants.ALLOWED_SYMBOL_TYPES!r}"
            )


@dataclass(frozen=True, kw_only=True)
class EarningsEvent:
    """One earnings date for a symbol (provider-neutral).

    Maps downstream onto ``earnings_calendar`` (``SCHEMA_SPEC.md`` §3.15).
    ``session`` values are drawn from :data:`EARNINGS_SESSIONS` and
    ``confidence`` from :data:`EARNINGS_CONFIDENCE_LEVELS`; confidence may be
    ``"low"`` in V1 (``MASTER_SPEC.md`` §21).

    The dataclass is ``kw_only`` so the field order mirrors the
    ``PROVIDER_INTERFACE_SPEC.md`` §6.4 table verbatim (``session`` before the
    required ``confidence`` / ``source_provider``).
    """

    ticker: str
    earnings_date: date
    session: str | None = None
    confidence: str
    source_provider: str

    def __post_init__(self) -> None:
        """Validate ticker non-empty (only enforcement required by the spec)."""
        if not self.ticker:
            raise ValueError("EarningsEvent.ticker must be a non-empty string")


# --------------------------------------------------------------------------- #
# Fundamentals value catalogs (Phase 4 — M04_PROVIDER_INTERFACE_CONFIG_DELTA.md)
# --------------------------------------------------------------------------- #
VALUATION_BANDS: Final[tuple[str, ...]] = ("cheap", "fair", "expensive", "unknown")


@dataclass(frozen=True, kw_only=True)
class FundamentalSnapshot:
    """One point-in-time fundamentals/events snapshot for a symbol (provider-neutral).

    Maps downstream onto the ``ticker_fundamentals`` companion table (Phase 4
    delta — see ``M04_PROVIDER_INTERFACE_CONFIG_DELTA.md``; a companion table
    was chosen over new ``daily_features`` columns so ``daily_features``'s
    daily-cadence, high-traffic read path stays free of 7 mostly-NULL,
    quarterly-cadence columns — ``FEATURE_SCHEMA_VERSION`` is unaffected).

    Every field except ``ticker`` / ``as_of_date`` / ``source_provider`` is
    optional: a provider that cannot reliably source a given field returns
    ``None`` for it rather than a fabricated value (per the Phase 4 coder
    note: "report as blocking issue rather than substituting an alternative
    field silently" — at the DTO level this means "field absent," not "field
    guessed").

    ``as_of_date`` is the point-in-time anchor per Phase 0's no-look-ahead
    discipline: the value known/computable as of this date (e.g. the most
    recent filing whose period end precedes it), never a later restatement.

    The dataclass is ``kw_only`` for the same reason as :class:`EarningsEvent`
    — field order can follow the coder note's list verbatim regardless of
    which fields are required vs optional.
    """

    ticker: str
    as_of_date: date
    eps_growth_trend: float | None = None
    leverage_ratio: float | None = None
    valuation_band: str | None = None
    piotroski_f_score: int | None = None
    altman_z_score: float | None = None
    insider_trade_flag: bool | None = None
    institutional_ownership_delta: float | None = None
    source_provider: str

    def __post_init__(self) -> None:
        if not self.ticker:
            raise ValueError("FundamentalSnapshot.ticker must be a non-empty string")
        if self.valuation_band is not None and self.valuation_band not in VALUATION_BANDS:
            raise ValueError(
                f"FundamentalSnapshot.valuation_band {self.valuation_band!r} "
                f"not in {VALUATION_BANDS!r}"
            )
        if self.piotroski_f_score is not None and not (0 <= self.piotroski_f_score <= 9):
            raise ValueError(
                f"FundamentalSnapshot.piotroski_f_score must be in [0, 9], "
                f"got {self.piotroski_f_score!r}"
            )


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a concrete provider supports.

    Lets callers/tests introspect a provider without trial-and-error. Returned
    by :meth:`MarketDataProvider.get_capabilities` in
    ``ServiceResult.metadata['capabilities']``.

    ``supports_fundamentals`` (Phase 4) defaults to ``False`` so every
    existing concrete-provider construction call (e.g. Module 05's
    ``YahooProvider``, unchanged by this delta) keeps working without edits —
    see :meth:`MarketDataProvider.get_fundamentals` for why this is a
    concrete-default method rather than an ``@abstractmethod`` like the other
    four.
    """

    provider_name: str
    supports_daily_prices: bool
    supports_ticker_listing: bool
    supports_earnings: bool
    supports_adjusted_prices: bool
    supports_fundamentals: bool = False


@dataclass(frozen=True)
class ProviderErrorDetail:
    """Structured, non-fatal provider error payload.

    Carried inside ``ServiceResult.metadata['error_detail']`` so callers can
    branch on :attr:`kind` (one of :data:`PROVIDER_ERROR_KINDS`) without parsing
    free-text error strings.
    """

    kind: str
    message: str
    symbol: str | None = None


# --------------------------------------------------------------------------- #
# Abstract provider contract (PROVIDER_INTERFACE_SPEC.md §7)
# --------------------------------------------------------------------------- #
class MarketDataProvider(abc.ABC):
    """Provider-neutral abstraction for market data.

    Concrete providers (e.g. Module 05's YahooProvider) implement every
    abstract method below. ``abc.ABC`` + ``@abstractmethod`` enforce, at
    construction time, that a subclass implements all four methods: a subclass
    that omits any of them raises ``TypeError`` when instantiated, and
    ``MarketDataProvider`` itself cannot be instantiated directly. (The
    structural shape is equivalent to a ``typing.Protocol`` with the same four
    methods, but ``abc.ABC`` is the authoritative, runtime-enforced mechanism;
    see ``PROVIDER_INTERFACE_SPEC.md`` §5.)

    Every data-fetching method returns
    :class:`app.utils.service_result.ServiceResult`. Domain DTOs are carried in
    ``ServiceResult.metadata`` under the documented keys
    (``PROVIDER_INTERFACE_SPEC.md`` §8):

    ======================  =================  ==========================
    method                  metadata key       value type
    ======================  =================  ==========================
    ``get_capabilities``    ``capabilities``   :class:`ProviderCapabilities`
    ``get_price_history``   ``bars``           ``list[PriceBar]``
    ``list_symbols``        ``symbols``        ``list[TickerInfo]``
    ``get_earnings``        ``events``         ``list[EarningsEvent]``
    any (on failure)        ``error_detail``   :class:`ProviderErrorDetail`
    any                     ``provider_name``  ``str``
    ======================  =================  ==========================

    Behavior contract for concrete implementations (§7.3, §9):

    - **Success with data**: ``status == "success"``, the DTO list under its
      documented metadata key, ``rows_processed == len(list)``, ``errors == []``.
    - **Empty result** (valid query, no data): ``status == "success"``, metadata
      key present with an empty list, ``rows_processed == 0``. An empty result
      is **not** an error.
    - **Partial result**: ``status == "success_with_warnings"`` with the partial
      list and ``warnings`` describing the gap.
    - **Error** (unsupported symbol, provider unavailable, rate limit, malformed
      response, unsupported capability): ``status == "failed"``,
      ``metadata['error_detail']`` carries a :class:`ProviderErrorDetail` whose
      ``kind`` is in :data:`PROVIDER_ERROR_KINDS`, ``errors`` non-empty,
      ``rows_processed == 0``. Concrete providers must return a
      ``ServiceResult`` for these expected conditions rather than raising.

    Module 04 defines signatures and contracts only; every abstract method body
    is empty. None of the above runs in Module 04 — it is the contract that
    Module 05 and the in-test fake must honor.
    """

    @abc.abstractmethod
    def get_capabilities(self) -> ServiceResult:
        """Return provider capabilities.

        No network. ``metadata['capabilities']`` carries a
        :class:`ProviderCapabilities`. ``metadata['provider_name']`` is the
        provider identity. Returns a :class:`ServiceResult`.
        """
        ...

    @abc.abstractmethod
    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult:
        """Return daily OHLCV bars for ``request.ticker``.

        Bars cover the inclusive ``[request.start_date, request.end_date]``
        range. ``metadata['bars']`` carries ``list[PriceBar]`` (possibly empty);
        ``rows_processed`` is the number of bars. Returns a
        :class:`ServiceResult`.
        """
        ...

    @abc.abstractmethod
    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        """Return the provider's known symbols.

        When ``symbol_type`` is ``None`` all known symbols are returned;
        otherwise results are filtered to a single ``symbol_type`` from
        ``constants.ALLOWED_SYMBOL_TYPES``. ``metadata['symbols']`` carries
        ``list[TickerInfo]``; ``rows_processed`` is the number of symbols.
        Returns a :class:`ServiceResult`.
        """
        ...

    @abc.abstractmethod
    def get_earnings(self, ticker: str) -> ServiceResult:
        """Return known earnings events for ``ticker``.

        ``metadata['events']`` carries ``list[EarningsEvent]`` (possibly empty);
        ``rows_processed`` is the number of events. Returns a
        :class:`ServiceResult`.
        """
        ...

    # ------------------------------------------------------------------ #
    # Phase 4 — fundamentals/events (M04_PROVIDER_INTERFACE_CONFIG_DELTA.md).
    # Concrete-default, NOT @abstractmethod: the other four methods are
    # abstract, so adding a fifth abstract method would force every existing
    # concrete provider (Module 05's frozen YahooProvider) to gain a stub
    # implementation just to keep instantiating — a forced touch to a frozen
    # module for a capability it doesn't support. A concrete default avoids
    # that: providers that don't override this method simply return
    # "unsupported" (this base implementation), exactly like their
    # ``get_capabilities().supports_fundamentals`` already (correctly)
    # reports ``False`` without needing an override.
    # ------------------------------------------------------------------ #
    def get_fundamentals(self, ticker: str, as_of_date: date) -> ServiceResult:
        """Return a point-in-time :class:`FundamentalSnapshot` for ``ticker``.

        ``metadata['fundamentals']`` carries one ``FundamentalSnapshot`` (not
        a list — one point-in-time snapshot per call, unlike the batch-shaped
        ``get_price_history`` / ``list_symbols`` / ``get_earnings``).

        Base implementation returns ``failed`` with
        ``metadata['error_detail'].kind == "unsupported_capability"`` — the
        default for any provider that doesn't override this method. Concrete
        providers that do support fundamentals (per
        ``get_capabilities().supports_fundamentals``) override this.
        """
        detail = ProviderErrorDetail(
            kind="unsupported_capability",
            message="get_fundamentals is not supported by this provider",
            symbol=ticker,
        )
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=str(uuid.uuid4()),
            rows_processed=0,
            errors=[detail.message],
            metadata={"provider_name": "unknown", "error_detail": detail},
        )


__all__ = [
    "MarketDataProvider",
    "PriceBar",
    "PriceHistoryRequest",
    "TickerInfo",
    "EarningsEvent",
    "FundamentalSnapshot",
    "ProviderCapabilities",
    "ProviderErrorDetail",
    "PROVIDER_ERROR_KINDS",
    "EARNINGS_SESSIONS",
    "VALUATION_BANDS",
]
