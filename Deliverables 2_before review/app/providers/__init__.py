"""Provider layer for the Swing Trading Stock Analyzer.

Module 04 defines the *abstract* provider contract: the provider-neutral
:class:`MarketDataProvider` interface, its request/response DTOs, and the
structured error vocabulary that downstream modules depend on. The interface
module itself imports no ``yfinance``, no network client, and no DuckDB access;
see ``PROVIDER_INTERFACE_SPEC.md`` for the source-of-truth contract.

Module 05 adds the first **concrete** provider, :class:`YahooProvider`
(``app/providers/yahoo_provider.py``), which implements that contract against
Yahoo via ``yfinance``. Importing this package does **not** import ``yfinance``:
``YahooProvider`` imports it lazily inside ``__init__`` only when no dependency
is injected, and all Yahoo access stays confined to ``yahoo_provider.py``.
"""

from __future__ import annotations

from app.providers.provider_interface import (
    EARNINGS_SESSIONS,
    PROVIDER_ERROR_KINDS,
    EarningsEvent,
    MarketDataProvider,
    PriceBar,
    PriceHistoryRequest,
    ProviderCapabilities,
    ProviderErrorDetail,
    TickerInfo,
)
# Module 05 concrete provider. Importing this does not import ``yfinance``:
# YahooProvider imports it lazily in ``__init__`` only when no fake is injected.
from app.providers.yahoo_provider import YahooProvider

__all__ = [
    "MarketDataProvider",
    "PriceBar",
    "PriceHistoryRequest",
    "TickerInfo",
    "EarningsEvent",
    "ProviderCapabilities",
    "ProviderErrorDetail",
    "PROVIDER_ERROR_KINDS",
    "EARNINGS_SESSIONS",
    "YahooProvider",
]
