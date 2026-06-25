"""Mutation detection service package (Module 10).

Exposes
:class:`~app.services.mutation.mutation_detector.MutationDetector`, which scans
already-ingested ``daily_prices`` rows for a date range, sets the real
``daily_prices.mutation_flag`` on detected mutation rows, derives/writes
``daily_prices.adjustment_factor`` where computable, and enqueues mutation
repairs into ``data_repair_queue`` plus feature-rebuild entries into
``feature_rebuild_log``. It runs as the pipeline step after Module 09 validation
and before Module 11 feature calculation. See ``M10_MUTATION_DETECTOR_SPEC.md``.
"""

from __future__ import annotations

from app.services.mutation.mutation_detector import MutationDetector

__all__ = ["MutationDetector"]
