"""Pytest bootstrap: ensure the project root is on sys.path.

Allows ``from app.config import ...`` to resolve when running pytest from the
project root without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
