"""Pipeline orchestrator tests — setup-mode (Phase 6 migration complete).

The strategy-mode orchestrator tests that were PENDING Phase 6 have been
superseded by ``test_phase6_orchestrator.py``, which covers the full
setup-mode orchestrator contract.

This file is retained to preserve the test module path; it imports and
re-exports the Phase 6 test suite so pytest collects everything in one place.
"""
# Re-export Phase 6 orchestrator tests so pytest discovers them here too.
from tests.test_phase6_orchestrator import *  # noqa: F401, F403
