"""Application settings for the Swing Trading Stock Analyzer.

This module defines filesystem paths (via ``pathlib``) and immutable strategy
configuration presets. It is the single place Module 01 exposes "where things
live" and "what the default tunable thresholds are".

Scope rules (Module 01):
- No database connections are opened here. Only DB *file paths* are computed.
- No provider calls.
- No trading logic. Strategy presets are plain immutable data.

Path layout (ARCHITECTURE.md section 2 / MASTER_SPEC.md section 3)::

    stock_ai_platform/
      app/
      data/
        duckdb/   prod.duckdb, debug.duckdb, simulation.duckdb
        logs/
        exports/
        backups/
      docs/

Strategy presets (MASTER_SPEC.md section 20) are exposed as frozen dataclasses
to honor the "config is immutable" rule in ``CODING_STANDARDS.md`` section 10.
Callers that need a modified config must clone and create a new version rather
than mutating in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping

from app.config import constants, env

# --------------------------------------------------------------------------- #
# Filesystem paths
# --------------------------------------------------------------------------- #
# Project root resolves regardless of current working directory. This file is
# at <root>/app/config/settings.py, so root is parents[2].
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

APP_DIR: Final[Path] = PROJECT_ROOT / "app"
DOCS_DIR: Final[Path] = PROJECT_ROOT / "docs"
TESTS_DIR: Final[Path] = PROJECT_ROOT / "tests"

# Data directories. Overridable via DATA_DIR env var (resolved by env.get_path).
DATA_DIR: Final[Path] = env.get_path("DATA_DIR", PROJECT_ROOT / "data")
DUCKDB_DIR: Final[Path] = DATA_DIR / "duckdb"
LOGS_DIR: Final[Path] = DATA_DIR / "logs"
EXPORTS_DIR: Final[Path] = DATA_DIR / "exports"
BACKUPS_DIR: Final[Path] = DATA_DIR / "backups"
# On-disk TTL caches for provider responses that are large/static-ish (e.g.
# SEC EDGAR's company_tickers.json) — see app/providers/edgar_provider.py.
CACHE_DIR: Final[Path] = DATA_DIR / "cache"

# DuckDB file paths (computed only; no connections opened in Module 01).
PROD_DB_PATH: Final[Path] = DUCKDB_DIR / constants.PROD_DB_FILENAME
DEBUG_DB_PATH: Final[Path] = DUCKDB_DIR / constants.DEBUG_DB_FILENAME
SIMULATION_DB_PATH: Final[Path] = DUCKDB_DIR / constants.SIMULATION_DB_FILENAME

# Directories the application expects to exist at runtime.
REQUIRED_DIRECTORIES: Final[tuple[Path, ...]] = (
    DATA_DIR,
    DUCKDB_DIR,
    LOGS_DIR,
    EXPORTS_DIR,
    BACKUPS_DIR,
    CACHE_DIR,
)


def ensure_directories() -> tuple[Path, ...]:
    """Create the required data directories if they do not already exist.

    Uses ``pathlib`` with ``parents=True`` and ``exist_ok=True`` so the call is
    idempotent. Returns the tuple of directories that were ensured.
    """
    for directory in REQUIRED_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)
    return REQUIRED_DIRECTORIES


# --------------------------------------------------------------------------- #
# Strategy presets (MASTER_SPEC.md section 20)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StrategyConfig:
    """Immutable strategy threshold preset.

    These are the tunable screening/diversification thresholds. They live in
    config (not constants) per CODING_STANDARDS.md. The dataclass is frozen to
    enforce the "config is immutable; clone to change" rule.
    """

    name: str
    min_price: float
    min_avg_dollar_volume_20d: float
    min_rvol: float
    min_screening_score: float
    sector_max_positions: int
    industry_max_positions: int
    earnings_avoid_window_bd: int


# Default values taken verbatim from MASTER_SPEC.md section 20.
NORMAL_CONFIG: Final[StrategyConfig] = StrategyConfig(
    name="normal",
    min_price=10.0,
    min_avg_dollar_volume_20d=20_000_000.0,
    min_rvol=1.5,
    min_screening_score=65.0,
    sector_max_positions=3,
    industry_max_positions=2,
    earnings_avoid_window_bd=10,
)

AGGRESSIVE_CONFIG: Final[StrategyConfig] = StrategyConfig(
    name="aggressive",
    min_price=5.0,
    min_avg_dollar_volume_20d=5_000_000.0,
    min_rvol=1.2,
    min_screening_score=55.0,
    sector_max_positions=5,
    industry_max_positions=3,
    earnings_avoid_window_bd=3,
)

CONSERVATIVE_CONFIG: Final[StrategyConfig] = StrategyConfig(
    name="conservative",
    min_price=15.0,
    min_avg_dollar_volume_20d=50_000_000.0,
    min_rvol=1.8,
    min_screening_score=75.0,
    sector_max_positions=2,
    industry_max_positions=1,
    earnings_avoid_window_bd=15,
)

# Read-only registry of presets, keyed by name. MappingProxyType prevents
# accidental mutation of the registry itself.
STRATEGY_PRESETS: Final[Mapping[str, StrategyConfig]] = MappingProxyType(
    {
        NORMAL_CONFIG.name: NORMAL_CONFIG,
        AGGRESSIVE_CONFIG.name: AGGRESSIVE_CONFIG,
        CONSERVATIVE_CONFIG.name: CONSERVATIVE_CONFIG,
    }
)

DEFAULT_STRATEGY_NAME: Final[str] = NORMAL_CONFIG.name


def get_strategy(name: str = DEFAULT_STRATEGY_NAME) -> StrategyConfig:
    """Return a strategy preset by name.

    Parameters
    ----------
    name:
        One of ``normal``, ``aggressive``, or ``conservative``.

    Raises
    ------
    KeyError
        If ``name`` is not a known preset. Per CODING_STANDARDS.md, an invalid
        config is a critical failure for downstream modules; surfacing a clear
        error here keeps that contract honest.
    """
    if name not in STRATEGY_PRESETS:
        raise KeyError(
            f"Unknown strategy preset {name!r}. "
            f"Valid presets: {sorted(STRATEGY_PRESETS)}"
        )
    return STRATEGY_PRESETS[name]


# --------------------------------------------------------------------------- #
# Diversification defaults (MASTER_SPEC.md section 15)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DiversificationConfig:
    """Immutable diversification defaults used by the Step 5 proposal engine."""

    hard_cap_enabled: bool = True
    sector_max_positions: int = 3
    industry_max_positions: int = 2
    sector_penalty_factor: float = 0.90
    industry_penalty_factor: float = 0.85


DIVERSIFICATION_DEFAULTS: Final[DiversificationConfig] = DiversificationConfig()
