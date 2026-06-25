"""Configuration management service package (M21 Config Management Addendum).

Provides :class:`ConfigService`, the DuckDB-backed versioned store that is the
runtime source of truth for active strategy and runtime configs, plus the
deterministic hashing / validation helpers and the fresh-DB seed defaults.
"""

from app.services.config.config_service import ConfigService

__all__ = ["ConfigService"]
