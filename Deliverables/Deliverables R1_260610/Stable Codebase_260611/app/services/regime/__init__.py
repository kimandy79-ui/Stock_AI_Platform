"""Market regime engine service package (Module 12).

Exposes :class:`~app.services.regime.market_regime_engine.MarketRegimeEngine`,
which reads eligible ``daily_prices`` rows (``data_quality_status = 'ok'``) for
``SPY`` / ``QQQ`` / ``^VIX``, classifies one market-wide ``market_regime`` value
per requested calendar date (VIX risk gates over an SPY/QQQ EMA200 trend rule,
consuming ``constants.MARKET_REGIME_PRIORITY`` top-down), and updates the
existing ``daily_features`` rows for each date / current feature schema version.

It runs as the pipeline step after Module 11 (Feature Engine) and before
Module 13 (Step 3 Screening). See ``M12_MARKET_REGIME_ENGINE_SPEC.md``.
"""

from __future__ import annotations

from app.services.regime.market_regime_engine import MarketRegimeEngine

__all__ = ["MarketRegimeEngine"]
