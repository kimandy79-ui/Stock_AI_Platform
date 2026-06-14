"""Validation + deterministic hashing helpers for the ConfigService.

This module is dependency-light on purpose: it imports no DuckDB, no provider,
and no Streamlit. It exposes pure functions that the ConfigService (and tests)
can call without any I/O.

Two responsibilities:

1. **Validation** of the architecture-bound enum values that must never become
   DB-editable: ``db_role`` and ``config_type`` (M21 Config Management Addendum
   §9). Strategy names are validated against the addendum's required set.
2. **Deterministic hashing** of a config payload so the same logical config
   always produces the same ``config_hash`` regardless of key insertion order
   (canonical JSON: sorted keys, no insignificant whitespace).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from app.database import duckdb_manager

# db_roles that hold config tables in V1.
ALLOWED_CONFIG_DB_ROLES: Final[tuple[str, ...]] = (
    duckdb_manager.DB_ROLE_PROD,
    duckdb_manager.DB_ROLE_DEBUG,
    duckdb_manager.DB_ROLE_SIMULATION,
)

# Required strategy names (addendum §3.1 / §5.1).
ALLOWED_STRATEGY_NAMES: Final[tuple[str, ...]] = (
    "normal",
    "aggressive",
    "conservative",
)

# Required runtime config types (addendum §3.2 / §5.2).
ALLOWED_CONFIG_TYPES: Final[tuple[str, ...]] = (
    "pipeline",
    "provider",
    "data_completeness",
    "debug",
    "simulation",
    "dashboard",
    "ai_review",
    "export",
)


class ConfigValidationError(ValueError):
    """Raised when a config-management input violates an architecture contract."""


def validate_db_role(db_role: Any) -> str:
    """Return ``db_role`` if it is a config-bearing role, else raise."""
    if db_role not in ALLOWED_CONFIG_DB_ROLES:
        raise ConfigValidationError(
            f"Unsupported config db_role {db_role!r}; "
            f"valid: {list(ALLOWED_CONFIG_DB_ROLES)}"
        )
    return db_role


def validate_config_type(config_type: Any) -> str:
    """Return ``config_type`` if it is an approved runtime type, else raise."""
    if config_type not in ALLOWED_CONFIG_TYPES:
        raise ConfigValidationError(
            f"Unknown config_type {config_type!r}; "
            f"valid: {list(ALLOWED_CONFIG_TYPES)}"
        )
    return config_type


def validate_strategy_name(strategy_name: Any) -> str:
    """Return ``strategy_name`` if it is an approved strategy, else raise."""
    if strategy_name not in ALLOWED_STRATEGY_NAMES:
        raise ConfigValidationError(
            f"Unknown strategy_name {strategy_name!r}; "
            f"valid: {list(ALLOWED_STRATEGY_NAMES)}"
        )
    return strategy_name


def validate_config_payload(config_json: Any) -> dict[str, Any]:
    """Return ``config_json`` if it is a non-empty JSON-serializable dict."""
    if not isinstance(config_json, dict) or not config_json:
        raise ConfigValidationError("config_json must be a non-empty dict")
    try:
        json.dumps(config_json, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ConfigValidationError(
            f"config_json is not JSON-serializable: {exc}"
        ) from exc
    return config_json


def canonical_json(config_json: dict[str, Any]) -> str:
    """Return canonical JSON text for a config payload.

    Keys are sorted recursively and separators are tight, so two dicts that are
    logically equal but differ only in key order serialize identically.
    """
    return json.dumps(
        config_json,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def deterministic_hash(config_json: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of the canonical config JSON.

    Same logical config -> same hash; any change to the payload -> different
    hash (M21 Config Management Addendum §6).
    """
    return hashlib.sha256(canonical_json(config_json).encode("utf-8")).hexdigest()


def snapshot_hash(configs: dict[str, dict[str, Any]]) -> str:
    """Return a deterministic hash over a *set* of resolved configs.

    Used for ``pipeline_runs.config_snapshot_hash``: the mapping of
    ``{strategy_name: config_json}`` (or any config bundle) is canonicalized as
    a whole, so the snapshot hash is order-independent and reproducible.
    """
    return hashlib.sha256(canonical_json(configs).encode("utf-8")).hexdigest()
