"""Data validation service package (Module 09).

Exposes
:class:`~app.services.validation.data_validator.DataValidator`, which validates
already-ingested ``daily_prices`` rows for a date range, sets the real
``daily_prices.data_quality_status`` (Module 08 wrote the placeholder ``"ok"``),
and enqueues validation repairs into ``data_repair_queue``. It runs as pipeline
step 6 ("Validate data"), after Module 08 ingestion and before Module 10
mutation detection / Module 11 features. See ``M09_DATA_VALIDATOR_SPEC.md``.
"""

from __future__ import annotations

from app.services.validation.data_validator import DataValidator

__all__ = ["DataValidator"]
