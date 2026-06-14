"""Daily price ingestion service package (Module 08).

Exposes
:class:`~app.services.ingestion.daily_price_ingestion.DailyPriceIngestionEngine`,
which downloads and upserts daily OHLCV prices for every active stock-universe
ticker into ``daily_prices`` and enqueues failed / empty-result tickers into
``data_repair_queue`` before the feature engine runs. See
``M08_DAILY_PRICE_INGESTION_SPEC.md``.
"""

from __future__ import annotations

from app.services.ingestion.daily_price_ingestion import DailyPriceIngestionEngine

__all__ = ["DailyPriceIngestionEngine"]
