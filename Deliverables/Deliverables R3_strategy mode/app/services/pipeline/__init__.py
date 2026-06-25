"""Module 20 — Pipeline Orchestrator service package.

Exposes :class:`PipelineOrchestrator`, the daily run coordinator that acquires
the ``daily_pipeline`` lock, records a ``pipeline_runs`` lifecycle row, and
drives the frozen step engines (benchmark/universe/price ingestion, validation,
mutation detection, feature calculation, the per-strategy Step 3/4/5 +
outcome-queue/processing block, dashboard materialization, and backup) in the
canonical order. See ``M20_PIPELINE_ORCHESTRATOR_SPEC.md``.
"""

from __future__ import annotations

from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator

__all__ = ["PipelineOrchestrator"]
