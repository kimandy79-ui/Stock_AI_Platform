"""Validation + deterministic hashing helpers for ConfigService (setup mode).

Setup-mode migration (AD-22.19–22.24):
- validate_setup_type() replaces validate_strategy_name()
- ALLOWED_SETUP_TYPES: exactly 4 values (breakout/pullback/trend_continuation/consolidation_base)
- validate_risk_label_config_id() for the single risk-label config
- ALLOWED_CONFIG_DB_ROLES: prod/debug/simulation (unchanged)
- No strategy/aggressive/normal/conservative references as active identifiers

Pure functions, no I/O.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from app.database import duckdb_manager
from app.config import constants

ALLOWED_CONFIG_DB_ROLES: Final[tuple[str, ...]] = (
    duckdb_manager.DB_ROLE_PROD,
    duckdb_manager.DB_ROLE_DEBUG,
    duckdb_manager.DB_ROLE_SIMULATION,
)

# Active setup types — the four-value selection unit (AD-22.20)
ALLOWED_SETUP_TYPES: Final[tuple[str, ...]] = constants.ALLOWED_SETUP_TYPES

# Risk label config has a single well-known seed id
RISK_LABEL_CONFIG_SEED_ID: Final[str] = "risk_label_config_v1"

# Runtime config types (retained for pipeline/provider/debug/sim/dashboard/ai_review/export)
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
    """Return db_role if it is a config-bearing role, else raise."""
    if db_role not in ALLOWED_CONFIG_DB_ROLES:
        raise ConfigValidationError(
            f"Unsupported config db_role {db_role!r}; "
            f"valid: {list(ALLOWED_CONFIG_DB_ROLES)}"
        )
    return db_role


def validate_setup_type(setup_type: Any) -> str:
    """Return setup_type if it is one of the four active types, else raise."""
    if setup_type not in ALLOWED_SETUP_TYPES:
        raise ConfigValidationError(
            f"Unknown setup_type {setup_type!r}; "
            f"valid: {list(ALLOWED_SETUP_TYPES)}"
        )
    return setup_type


def validate_config_type(config_type: Any) -> str:
    """Return config_type if it is an approved runtime type, else raise."""
    if config_type not in ALLOWED_CONFIG_TYPES:
        raise ConfigValidationError(
            f"Unknown config_type {config_type!r}; "
            f"valid: {list(ALLOWED_CONFIG_TYPES)}"
        )
    return config_type


def validate_config_payload(config_json: Any) -> dict[str, Any]:
    """Return config_json if it is a non-empty JSON-serializable dict."""
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
    """Return canonical JSON text (sorted keys, tight separators)."""
    return json.dumps(
        config_json,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def deterministic_hash(config_json: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of the canonical config JSON."""
    return hashlib.sha256(canonical_json(config_json).encode("utf-8")).hexdigest()


def snapshot_hash(configs: dict[str, dict[str, Any]]) -> str:
    """Return a deterministic hash over a set of resolved configs."""
    return hashlib.sha256(canonical_json(configs).encode("utf-8")).hexdigest()
