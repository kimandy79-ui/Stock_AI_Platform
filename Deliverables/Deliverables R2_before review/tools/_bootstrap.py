"""Shared bootstrap for operator runner scripts.

When a script under ``tools/`` is launched directly (e.g. a PyCharm Run
Configuration that runs ``tools/run_prod_pipeline.py``), only the script's own
directory is placed on ``sys.path`` by default, so ``import app...`` fails.
This helper prepends the repository root (the parent of ``tools/``) to
``sys.path`` so the application package is importable regardless of launch
style. It mirrors what ``conftest.py`` does for pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_repo_root_on_path() -> Path:
    """Prepend the repository root to ``sys.path`` and return it (idempotent)."""
    repo_root = Path(__file__).resolve().parent.parent
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return repo_root
