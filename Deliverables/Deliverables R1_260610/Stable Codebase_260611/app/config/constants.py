"""Project-wide constants for the Swing Trading Stock Analyzer.

This module holds immutable, non-tunable constants drawn directly from
``MASTER_SPEC.md`` and ``ARCHITECTURE.md``. Per ``CODING_STANDARDS.md``:

- Tunable trading thresholds (min_price, min_rvol, etc.) live in strategy
  config, NOT here. Those are exposed via ``settings.py`` strategy presets.
- These constants are structural / vocabulary values that define the domain
  (symbol types, regimes, setup types, feature schema version, etc.).

Module 01 scope: definitions only. No database, provider, or trading logic.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------- #
# Feature schema version
# --------------------------------------------------------------------------- #
# Zero-padded per DECISIONS_LOG.md ("Use zero-padded feature schema versions")
# to avoid lexicographic MAX bugs. Required exact value for Module 01.
FEATURE_SCHEMA_VERSION: Final[str] = "features_v01"

# --------------------------------------------------------------------------- #
# Database file names (MASTER_SPEC.md section 3, ARCHITECTURE.md section 5)
# --------------------------------------------------------------------------- #
PROD_DB_FILENAME: Final[str] = "prod.duckdb"
DEBUG_DB_FILENAME: Final[str] = "debug.duckdb"
SIMULATION_DB_FILENAME: Final[str] = "simulation.duckdb"

# --------------------------------------------------------------------------- #
# Symbol types (MASTER_SPEC.md section 6). Only `stock` enters screening.
# --------------------------------------------------------------------------- #
SYMBOL_TYPE_STOCK: Final[str] = "stock"
SYMBOL_TYPE_ETF: Final[str] = "etf"
SYMBOL_TYPE_BENCHMARK: Final[str] = "benchmark"
SYMBOL_TYPE_INDEX: Final[str] = "index"

ALLOWED_SYMBOL_TYPES: Final[tuple[str, ...]] = (
    SYMBOL_TYPE_STOCK,
    SYMBOL_TYPE_ETF,
    SYMBOL_TYPE_BENCHMARK,
    SYMBOL_TYPE_INDEX,
)

# --------------------------------------------------------------------------- #
# Benchmark and sector ETF symbols (MASTER_SPEC.md section 5)
# --------------------------------------------------------------------------- #
BENCHMARK_SPY: Final[str] = "SPY"
BENCHMARK_QQQ: Final[str] = "QQQ"
BENCHMARK_VIX: Final[str] = "^VIX"

# Sector SPDR ETFs used for sector relative strength and exclusions.
SECTOR_ETFS: Final[tuple[str, ...]] = (
    "XLK",
    "XLF",
    "XLV",
    "XLY",
    "XLP",
    "XLC",
    "XLI",
    "XLE",
    "XLB",
    "XLU",
    "XLRE",
)

# Required benchmark universe loaded before the feature engine.
REQUIRED_BENCHMARK_SYMBOLS: Final[tuple[str, ...]] = (
    BENCHMARK_SPY,
    BENCHMARK_QQQ,
    BENCHMARK_VIX,
    *SECTOR_ETFS,
)

# Canonical internal sector vocabulary (M21 Config Management Addendum §8).
# These are the ONLY sector names that may be stored in ``ticker_master.sector``
# / ``ticker_universe_snapshot.sector`` or used as keys in ``SECTOR_ETF_MAP`` /
# ``sector_etf_map``. Provider-raw sectors are normalized to one of these via
# ``SECTOR_ALIAS_MAP`` before storage. This is structural vocabulary, not a
# tunable setting, so it is intentionally a hardcoded constant.
CANONICAL_SECTORS: Final[tuple[str, ...]] = (
    "Technology",
    "Financials",
    "Healthcare",
    "Consumer Discretionary",
    "Consumer Staples",
    "Communication Services",
    "Industrials",
    "Energy",
    "Materials",
    "Utilities",
    "Real Estate",
)

# Sector -> ETF mapping (MASTER_SPEC.md section 10). Keys are canonical sectors.
SECTOR_ETF_MAP: Final[dict[str, str]] = {
    "Technology": "XLK",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
}

# Provider/source raw-sector -> canonical-sector aliases (M21 Config Management
# Addendum §8). This is the single source of truth for sector normalization;
# the DB ``sector_alias_map`` table is seeded from this map for dashboard
# visibility / future editing, and runtime ingestion normalizes via this map.
# Keys are matched case-sensitively first, then case-insensitively as a
# fallback (see ``normalize_sector``).
SECTOR_ALIAS_SOURCE_YAHOO: Final[str] = "yahoo"
SECTOR_ALIAS_MAP: Final[dict[str, str]] = {
    "Technology": "Technology",
    "Financial Services": "Financials",
    "Financials": "Financials",
    "Healthcare": "Healthcare",
    "Health Care": "Healthcare",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Consumer Staples": "Consumer Staples",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Basic Materials": "Materials",
    "Materials": "Materials",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
}

# --------------------------------------------------------------------------- #
# Market regimes (MASTER_SPEC.md section 11)
# --------------------------------------------------------------------------- #
REGIME_EXTREME_RISK: Final[str] = "extreme_risk"
REGIME_HIGH_RISK: Final[str] = "high_risk"
REGIME_BEAR: Final[str] = "bear"
REGIME_BULL: Final[str] = "bull"
REGIME_NEUTRAL: Final[str] = "neutral"

# Listed in priority order (highest priority first).
MARKET_REGIME_PRIORITY: Final[tuple[str, ...]] = (
    REGIME_EXTREME_RISK,
    REGIME_HIGH_RISK,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_NEUTRAL,
)

# VIX thresholds used in regime classification.
VIX_EXTREME_RISK_THRESHOLD: Final[float] = 30.0
VIX_HIGH_RISK_THRESHOLD: Final[float] = 25.0

# --------------------------------------------------------------------------- #
# Setup types (MASTER_SPEC.md section 13)
# --------------------------------------------------------------------------- #
SETUP_TREND_PULLBACK: Final[str] = "trend_pullback"
SETUP_BREAKOUT: Final[str] = "breakout"
SETUP_VOLATILITY_SQUEEZE: Final[str] = "volatility_squeeze"
SETUP_TREND_RESUME: Final[str] = "trend_resume"
SETUP_HIGH_TIGHT_FLAG: Final[str] = "high_tight_flag"
SETUP_UNKNOWN: Final[str] = "unknown"

ALLOWED_SETUP_TYPES: Final[tuple[str, ...]] = (
    SETUP_TREND_PULLBACK,
    SETUP_BREAKOUT,
    SETUP_VOLATILITY_SQUEEZE,
    SETUP_TREND_RESUME,
    SETUP_HIGH_TIGHT_FLAG,
    SETUP_UNKNOWN,
)

# --------------------------------------------------------------------------- #
# Outcome horizons (MASTER_SPEC.md section 16). US trading business days.
# --------------------------------------------------------------------------- #
OUTCOME_HORIZONS_BD: Final[tuple[int, ...]] = (5, 10, 20, 40)

# --------------------------------------------------------------------------- #
# Step 3 screening default block weights (MASTER_SPEC.md section 12).
# Structural composition of the screening score; sums to 1.0.
# --------------------------------------------------------------------------- #
SCREENING_BLOCK_WEIGHTS: Final[dict[str, float]] = {
    "trend": 0.30,
    "momentum": 0.25,
    "setup": 0.20,
    "volume": 0.15,
    "market": 0.10,
}

# --------------------------------------------------------------------------- #
# Simulation vocabulary (MASTER_SPEC.md section 17)
# --------------------------------------------------------------------------- #
LIST_MEMBERSHIP_RAW_ONLY: Final[str] = "raw_only"
LIST_MEMBERSHIP_DIVERSIFIED_ONLY: Final[str] = "diversified_only"
LIST_MEMBERSHIP_BOTH: Final[str] = "both"

ALLOWED_LIST_MEMBERSHIP: Final[tuple[str, ...]] = (
    LIST_MEMBERSHIP_RAW_ONLY,
    LIST_MEMBERSHIP_DIVERSIFIED_ONLY,
    LIST_MEMBERSHIP_BOTH,
)

LIST_TYPE_RAW: Final[str] = "raw"
LIST_TYPE_DIVERSIFIED: Final[str] = "diversified"

ALLOWED_LIST_TYPES: Final[tuple[str, ...]] = (
    LIST_TYPE_RAW,
    LIST_TYPE_DIVERSIFIED,
)

# --------------------------------------------------------------------------- #
# AI review attribution (MASTER_SPEC.md section 19)
# --------------------------------------------------------------------------- #
ATTRIBUTION_MECHANICAL_ONLY: Final[str] = "mechanical_only"
ATTRIBUTION_HUMAN_ONLY: Final[str] = "human_only"
ATTRIBUTION_AI_ASSISTED: Final[str] = "ai_assisted"

ALLOWED_ATTRIBUTIONS: Final[tuple[str, ...]] = (
    ATTRIBUTION_MECHANICAL_ONLY,
    ATTRIBUTION_HUMAN_ONLY,
    ATTRIBUTION_AI_ASSISTED,
)

# --------------------------------------------------------------------------- #
# Logging (CODING_STANDARDS.md section 5)
# Format: timestamp | level | module | run_id | message
# --------------------------------------------------------------------------- #
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)s | %(name)s | %(run_id)s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"

# Default placeholder when no run_id is bound to a log record.
DEFAULT_RUN_ID: Final[str] = "-"
