"""Module 22 — Debug Mode service package.

Exposes the debug-mode control plane: immutable debug presets, the deterministic
:class:`SamplingProvider` wrapper, and the :class:`DebugModeController` that
drives fast, sampled, partial pipeline runs against ``debug.duckdb`` only.
"""

from __future__ import annotations

from app.services.debug.debug_mode import (
    DB_ROLE_DEBUG,
    DEBUG_PRESETS,
    DEFAULT_DEBUG_SAMPLE,
    FORBIDDEN_DB_ROLES,
    MAX_DEBUG_SAMPLE,
    RUN_TYPE_DEBUG,
    STEP_NAMES,
    DebugModeController,
    DebugPreset,
    DebugRunPlan,
    SamplingProvider,
)

__all__ = [
    "DB_ROLE_DEBUG",
    "DEBUG_PRESETS",
    "DEFAULT_DEBUG_SAMPLE",
    "FORBIDDEN_DB_ROLES",
    "MAX_DEBUG_SAMPLE",
    "RUN_TYPE_DEBUG",
    "STEP_NAMES",
    "DebugModeController",
    "DebugPreset",
    "DebugRunPlan",
    "SamplingProvider",
]
