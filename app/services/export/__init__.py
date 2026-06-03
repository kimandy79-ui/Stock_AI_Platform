"""Module 18 — Export Package Engine.

Public entry point: :class:`ExportPackageEngine`, which builds reviewer ZIP
packages (ticker review / simulation review) and records exactly one review
row in ``ai_reviews`` / ``sim_ai_reviews``.

All database access is routed through the approved DuckDB manager (or an
injected ``db_manager``). This package never imports ``duckdb`` directly, never
runs DDL or ``ATTACH``, never calls providers, and never uses ``print()``.
"""

from __future__ import annotations

from app.services.export.export_package_engine import ExportPackageEngine

__all__ = ["ExportPackageEngine"]
