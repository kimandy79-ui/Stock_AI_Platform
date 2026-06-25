"""Module 16 — Outcome Queue service package.

Exposes :class:`OutcomeQueueCreator` (enqueues outcome-tracking rows for raw/
diversified Top-N proposals) and :class:`OutcomeQueueProcessor` (computes
``signal_outcomes`` once eval dates are reached). See
``M16_OUTCOME_QUEUE_SPEC.md``.
"""

from __future__ import annotations

from app.services.outcomes.outcome_queue import (
    OutcomeQueueCreator,
    OutcomeQueueProcessor,
)

__all__ = ["OutcomeQueueCreator", "OutcomeQueueProcessor"]
