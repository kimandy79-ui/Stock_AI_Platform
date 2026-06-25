"""Step 3 Screening service package (Module 13).

Exposes :class:`~app.services.screening.step3_screening.Step3ScreeningEngine`,
which reads ``daily_features_current`` for a ``signal_date``, joins
``ticker_master`` and ``daily_prices`` (for ``data_quality_status``), applies the
Step 3 hard filters and soft scoring (Polars-vectorized), and appends every
evaluated row — passed and failed — into ``step3_candidates`` in a single
transaction.

It runs as the pipeline step after Module 12 (Market Regime Engine) and before
Module 14 (Step 4 Setup Analysis). See ``M13_STEP3_SCREENING_SPEC.md``.
"""

from __future__ import annotations

from app.services.screening.step3_screening import Step3ScreeningEngine

__all__ = ["Step3ScreeningEngine"]
