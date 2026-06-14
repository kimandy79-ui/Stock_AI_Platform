"""Feature engine service package (Module 11).

Exposes :class:`~app.services.features.feature_engine.FeatureEngine`, which
reads eligible ``daily_prices`` rows (``data_quality_status = 'ok'``), computes
``daily_features`` indicators with Polars strictly from the frozen project
formulas, and upserts one feature row per processed ticker (anchored on that
ticker's ``feature_cutoff_date``) into ``daily_features`` on the composite key
``(ticker, feature_date, feature_schema_version)``.

It runs as the pipeline step after Module 10 (Mutation Detector) and before
Module 12 (Market Regime Engine). See ``M11_FEATURE_ENGINE_SPEC.md``.
"""

from __future__ import annotations

from app.services.features.feature_engine import FeatureEngine

__all__ = ["FeatureEngine"]
