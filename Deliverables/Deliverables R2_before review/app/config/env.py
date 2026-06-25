"""Environment variable loading for the Swing Trading Stock Analyzer.

This module centralizes ``.env`` loading via ``python-dotenv`` and exposes
small typed helpers for reading environment variables. It deliberately
performs NO database connections and NO provider calls (Module 01 scope).

Behavior
--------
- ``load_environment()`` loads a ``.env`` file once into ``os.environ`` using
  ``python-dotenv``. Existing process environment values are not overridden.
- Typed getters (``get_str``, ``get_int``, ``get_float``, ``get_bool``,
  ``get_path``) provide defaulting and light validation.

The project root is located relative to this file so paths resolve correctly
regardless of the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root: this file lives at <root>/app/config/env.py, so root is parents[2].
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# Default .env location at the project root.
DEFAULT_ENV_PATH: Path = PROJECT_ROOT / ".env"

# Truthy string tokens recognized by ``get_bool``.
_TRUE_TOKENS: frozenset[str] = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_TOKENS: frozenset[str] = frozenset({"0", "false", "no", "n", "off"})

# Module-level guard so repeated imports/calls don't reload the file.
_ENV_LOADED: bool = False


def load_environment(env_path: Path | None = None, *, override: bool = False) -> bool:
    """Load environment variables from a ``.env`` file.

    Parameters
    ----------
    env_path:
        Path to the ``.env`` file. Defaults to ``<project_root>/.env``.
    override:
        If ``True``, values in the file override existing process environment
        variables. Defaults to ``False`` (process env wins).

    Returns
    -------
    bool
        ``True`` if a ``.env`` file was found and loaded, ``False`` otherwise.
        A missing file is not an error: real environment variables may still
        be present.
    """
    global _ENV_LOADED

    target = env_path if env_path is not None else DEFAULT_ENV_PATH
    loaded = load_dotenv(dotenv_path=target, override=override)
    _ENV_LOADED = True
    return loaded


def is_loaded() -> bool:
    """Return ``True`` if :func:`load_environment` has been called."""
    return _ENV_LOADED


def get_str(key: str, default: str | None = None) -> str | None:
    """Return an environment variable as a string, or ``default`` if unset."""
    value = os.environ.get(key)
    return value if value is not None else default


def get_int(key: str, default: int) -> int:
    """Return an environment variable as an ``int``.

    Falls back to ``default`` when the variable is unset or cannot be parsed.
    """
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_float(key: str, default: float) -> float:
    """Return an environment variable as a ``float``.

    Falls back to ``default`` when the variable is unset or cannot be parsed.
    """
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def get_bool(key: str, default: bool) -> bool:
    """Return an environment variable as a ``bool``.

    Recognizes common truthy/falsey tokens (case-insensitive). Falls back to
    ``default`` when unset or unrecognized.
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return default


def get_path(key: str, default: Path) -> Path:
    """Return an environment variable as a :class:`pathlib.Path`.

    Relative paths are resolved against the project root. Falls back to
    ``default`` when unset.
    """
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate)
