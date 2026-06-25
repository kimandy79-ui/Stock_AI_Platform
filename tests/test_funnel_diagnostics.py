"""Funnel diagnostics tests — setup-mode (Phase 6 migration complete).

The strategy-mode funnel diagnostics tests have been superseded by
``test_phase6_diagnostics.py``, which covers the full setup-mode
``SetupModeFunnelDiagnosticsService`` contract.

This file re-exports the Phase 6 diagnostics test suite so the module
path is preserved and backward-compatible test discovery works.
"""
from tests.test_phase6_diagnostics import *  # noqa: F401, F403
