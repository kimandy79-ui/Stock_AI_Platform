"""Module 19 — AI Review Engine package.

Exposes :class:`AiReviewEngine` and the injectable :class:`AiClientProtocol`.
"""

from __future__ import annotations

from app.services.ai_review.ai_review_engine import (
    AiClientProtocol,
    AiReviewEngine,
    DefaultAiClient,
    FallbackAiClient,
    GeminiClient,
)

__all__ = [
    "AiReviewEngine",
    "AiClientProtocol",
    "DefaultAiClient",
    "FallbackAiClient",
    "GeminiClient",
]
