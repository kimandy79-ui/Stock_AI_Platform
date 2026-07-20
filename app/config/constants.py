"""Project-wide constants for the Swing Trading Stock Analyzer.

Setup-mode migration (AD-22.19–22.24): FEATURE_SCHEMA_VERSION bumped to
features_v02. ALLOWED_SETUP_TYPES now carries the four active setup-mode
values. Legacy six-value setup vocabulary is retired; legacy strategy names
(aggressive/normal/conservative) appear only as deprecated notes.

P1.1 (2026-07-08): FEATURE_SCHEMA_VERSION bumped again to features_v03 —
adds rs_percentile_126d (cross-sectional RS percentile). features_v02 rows
are retained as historical/frozen, same policy as v01->v02.

P2.3/P2.4 (2026-07-10): bumped to features_v04 — adds vcp_sequence_score
(progressive base contraction) and market_cap (shares_outstanding x close_raw).
Both dormant: persisted, read by no validator or scoring path.

2026-07-20 (Phase 1.5 RS/150MA follow-up, Item 2): bumped to features_v05 —
adds ema150 (150-day EMA). Dormant: persisted, read by no validator or scoring
path yet -- landed ahead of a separate, still-pending decision on whether/how
to wire a trend_continuation "price>50MA>150MA>200MA" gate.

Module 01 scope: definitions only. No database, provider, or trading logic.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------- #
# Feature schema version (AD-22.8; bumped AD-22.19; bumped for P1.1; bumped for
# P2.3/P2.4 -- adds vcp_sequence_score + market_cap; bumped 2026-07-20 for
# ema150, dormant).
# Zero-padded per DECISIONS_LOG.md to avoid lexicographic MAX bugs.
# --------------------------------------------------------------------------- #
FEATURE_SCHEMA_VERSION: Final[str] = "features_v05"

# --------------------------------------------------------------------------- #
# Database file names
# --------------------------------------------------------------------------- #
PROD_DB_FILENAME: Final[str] = "prod.duckdb"
DEBUG_DB_FILENAME: Final[str] = "debug.duckdb"
SIMULATION_DB_FILENAME: Final[str] = "simulation.duckdb"

# --------------------------------------------------------------------------- #
# Symbol types
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
# Benchmark and sector ETF symbols
# --------------------------------------------------------------------------- #
BENCHMARK_SPY: Final[str] = "SPY"
BENCHMARK_QQQ: Final[str] = "QQQ"
BENCHMARK_VIX: Final[str] = "^VIX"

SECTOR_ETFS: Final[tuple[str, ...]] = (
    # Broad sector (SPDR)
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLRE",
    # Basic Materials
    "COPX", "GDX", "MOO", "PICK", "SIL", "SLX", "WOOD",
    # Communication Services
    "ESPO", "FDN", "IYZ", "PEJ",
    # Consumer Discretionary
    "CARZ", "IBUY", "ITB", "XRT",
    # Consumer Defensive
    "PBJ",
    # Energy
    "AMLP", "CRAK", "OIH", "URA", "XOP",
    # Financial Services
    "IAI", "IAK", "KBE", "KRE", "REM",
    # Healthcare
    "IHE", "IHF", "IHI", "XBI",
    # Industrials
    "BOAT", "IFRA", "ITA", "IYT", "JETS", "PAVE", "XHB", "XME",
    # Real Estate
    "DESK", "INDS", "REZ", "SRVR", "VNQ",
    # Technology
    "IGV", "SOXX", "TAN",
    # Utilities
    "ICLN", "IDU", "PHO",
)

REQUIRED_BENCHMARK_SYMBOLS: Final[tuple[str, ...]] = (
    BENCHMARK_SPY,
    BENCHMARK_QQQ,
    BENCHMARK_VIX,
    *SECTOR_ETFS,
)

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

SECTOR_ALIAS_SOURCE_YAHOO: Final[str] = "yahoo"
SECTOR_ALIAS_MAP: Final[dict[str, str]] = {
    "Technology": "Technology",
    "Financial Services": "Financials",
    "Financials": "Financials",
    "Finance": "Financials",
    "Healthcare": "Healthcare",
    "Health Care": "Healthcare",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Consumer Staples": "Consumer Staples",
    "Communication Services": "Communication Services",
    "Telecommunications": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Basic Materials": "Materials",
    "Materials": "Materials",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
}

# Industry-level ETF map: canonical sector name → {industry → ETF ticker}.
# Outer keys match ticker_master.sector (canonical names after normalize_sector).
# Use this map to look up the best ETF for a specific industry; fall back to
# SECTOR_ETF_MAP when no industry-specific ETF is mapped.
# All ETFs referenced here are members of SECTOR_ETFS (loaded as daily prices).
INDUSTRY_ETF_MAP: Final[dict[str, dict[str, str]]] = {
    "Materials": {
        "Agricultural Inputs": "MOO",
        "Aluminum": "PICK",
        "Building Materials": "XLB",
        "Chemicals": "XLB",
        "Coking Coal": "XLB",
        "Copper": "COPX",
        "Gold": "GDX",
        "Lumber & Wood Products": "WOOD",
        "Other Industrial Metals & Mining": "PICK",
        "Other Precious Metals & Mining": "PICK",
        "Paper & Paper Products": "WOOD",
        "Silver": "SIL",
        "Specialty Chemicals": "XLB",
        "Steel": "SLX",
    },
    "Communication Services": {
        "Advertising Agencies": "XLC",
        "Broadcasting": "XLC",
        "Electronic Gaming & Multimedia": "ESPO",
        "Entertainment": "PEJ",
        "Internet Content & Information": "FDN",
        "Publishing": "XLC",
        "Telecom Services": "IYZ",
    },
    "Consumer Discretionary": {
        "Apparel Manufacturing": "XLY",
        "Apparel Retail": "XRT",
        "Auto & Truck Dealerships": "CARZ",
        "Auto Manufacturers": "CARZ",
        "Auto Parts": "CARZ",
        "Department Stores": "XRT",
        "Footwear & Accessories": "XLY",
        "Furnishings, Fixtures & Appliances": "XLY",
        "Gambling": "XLY",
        "Home Improvement Retail": "XRT",
        "Internet Retail": "IBUY",
        "Leisure": "PEJ",
        "Lodging": "PEJ",
        "Luxury Goods": "XLY",
        "Packaging & Containers": "XLY",
        "Personal Services": "XLY",
        "Recreational Vehicles": "XLY",
        "Residential Construction": "ITB",
        "Resorts & Casinos": "XLY",
        "Restaurants": "PEJ",
        "Specialty Retail": "XRT",
        "Textile Manufacturing": "XLY",
        "Travel Services": "PEJ",
    },
    "Consumer Staples": {
        "Beverages - Brewers": "XLP",
        "Beverages - Non-Alcoholic": "XLP",
        "Beverages - Wineries & Distilleries": "XLP",
        "Confectioners": "PBJ",
        "Discount Stores": "XRT",
        "Drug Stores": "XLP",
        "Education & Training Services": "XLP",
        "Farm Products": "MOO",
        "Food Distribution": "PBJ",
        "Grocery Stores": "XLP",
        "Household & Personal Products": "XLP",
        "Packaged Foods": "PBJ",
        "Tobacco": "XLP",
    },
    "Energy": {
        "Oil & Gas Drilling": "OIH",
        "Oil & Gas E&P": "XOP",
        "Oil & Gas Equipment & Services": "OIH",
        "Oil & Gas Integrated": "XLE",
        "Oil & Gas Midstream": "AMLP",
        "Oil & Gas Refining & Marketing": "CRAK",
        "Thermal Coal": "XLE",
        "Uranium": "URA",
    },
    "Financials": {
        "Asset Management": "IAI",
        "Banks - Diversified": "KBE",
        "Banks - Regional": "KRE",
        "Capital Markets": "IAI",
        "Credit Services": "XLF",
        "Financial Conglomerates": "XLF",
        "Financial Data & Stock Exchanges": "IAI",
        "Insurance - Diversified": "IAK",
        "Insurance - Life": "IAK",
        "Insurance - Property & Casualty": "IAK",
        "Insurance - Reinsurance": "IAK",
        "Insurance - Specialty": "IAK",
        "Mortgage Finance": "REM",
        "Shell Companies": "XLF",
    },
    "Healthcare": {
        "Biotechnology": "XBI",
        "Diagnostics & Research": "XLV",
        "Drug Manufacturers - General": "IHE",
        "Drug Manufacturers - Specialty & Generic": "IHE",
        "Health Information Services": "XLV",
        "Healthcare Plans": "IHF",
        "Medical Care Facilities": "IHF",
        "Medical Devices": "IHI",
        "Medical Distribution": "XLV",
        "Medical Instruments & Supplies": "IHI",
        "Pharmaceutical Retailers": "XLV",
    },
    "Industrials": {
        "Aerospace & Defense": "ITA",
        "Airlines": "JETS",
        "Airports & Air Services": "JETS",
        "Building Products & Equipment": "XHB",
        "Business Equipment & Supplies": "XLI",
        "Conglomerates": "XLI",
        "Consulting Services": "XLI",
        "Electrical Equipment & Parts": "XLI",
        "Engineering & Construction": "PAVE",
        "Farm & Heavy Construction Machinery": "XLI",
        "Industrial Distribution": "XLI",
        "Infrastructure Operations": "IFRA",
        "Integrated Freight & Logistics": "IYT",
        "Marine Shipping": "BOAT",
        "Metal Fabrication": "XME",
        "Pollution & Treatment Controls": "XLI",
        "Railroads": "IYT",
        "Rental & Leasing Services": "XLI",
        "Security & Protection Services": "XLI",
        "Specialty Business Services": "XLI",
        "Specialty Industrial Machinery": "XLI",
        "Staffing & Employment Services": "XLI",
        "Tools & Accessories": "XLI",
        "Trucking": "IYT",
        "Waste Management": "XLI",
    },
    "Real Estate": {
        "Real Estate - Development": "XLRE",
        "Real Estate - Diversified": "XLRE",
        "Real Estate Services": "XLRE",
        "REIT - Diversified": "VNQ",
        "REIT - Healthcare Facilities": "VNQ",
        "REIT - Hotel & Motel": "VNQ",
        "REIT - Industrial": "INDS",
        "REIT - Mortgage": "REM",
        "REIT - Office": "DESK",
        "REIT - Residential": "REZ",
        "REIT - Retail": "VNQ",
        "REIT - Specialty": "SRVR",
    },
    "Technology": {
        "Communication Equipment": "XLK",
        "Computer Hardware": "XLK",
        "Consumer Electronics": "XLK",
        "Electronic Components": "XLK",
        "Electronics & Computer Distribution": "XLK",
        "Information Technology Services": "IGV",
        "Scientific & Technical Instruments": "XLK",
        "Semiconductor Equipment & Materials": "SOXX",
        "Semiconductors": "SOXX",
        "Software - Application": "IGV",
        "Software - Infrastructure": "IGV",
        "Solar": "TAN",
    },
    "Utilities": {
        "Utilities - Diversified": "IDU",
        "Utilities - Independent Power Producers": "XLU",
        "Utilities - Regulated Electric": "IDU",
        "Utilities - Regulated Gas": "IDU",
        "Utilities - Regulated Water": "PHO",
        "Utilities - Renewable": "ICLN",
    },
}

# --------------------------------------------------------------------------- #
# Market regimes (AD-22.18; VIX boundaries are structural domain constants)
# --------------------------------------------------------------------------- #
REGIME_EXTREME_RISK: Final[str] = "extreme_risk"
REGIME_HIGH_RISK: Final[str] = "high_risk"
REGIME_BEAR: Final[str] = "bear"
REGIME_BULL: Final[str] = "bull"
REGIME_NEUTRAL: Final[str] = "neutral"

MARKET_REGIME_PRIORITY: Final[tuple[str, ...]] = (
    REGIME_EXTREME_RISK,
    REGIME_HIGH_RISK,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_NEUTRAL,
)

VIX_EXTREME_RISK_THRESHOLD: Final[float] = 30.0
VIX_HIGH_RISK_THRESHOLD: Final[float] = 25.0

# --------------------------------------------------------------------------- #
# Setup types — active selection unit (AD-22.20)
# Exactly four values. Legacy six-value vocab is retired.
# --------------------------------------------------------------------------- #
SETUP_BREAKOUT: Final[str] = "breakout"
SETUP_PULLBACK: Final[str] = "pullback"
SETUP_TREND_CONTINUATION: Final[str] = "trend_continuation"
SETUP_CONSOLIDATION_BASE: Final[str] = "consolidation_base"

ALLOWED_SETUP_TYPES: Final[tuple[str, ...]] = (
    SETUP_BREAKOUT,
    SETUP_PULLBACK,
    SETUP_TREND_CONTINUATION,
    SETUP_CONSOLIDATION_BASE,
)

# Retired legacy setup type names — kept as deprecated string constants ONLY
# for referencing in migration notes. Must not be used as active selection values.
_LEGACY_SETUP_TREND_PULLBACK: Final[str] = "trend_pullback"       # retired
_LEGACY_SETUP_VOLATILITY_SQUEEZE: Final[str] = "volatility_squeeze"  # retired → consolidation_base
_LEGACY_SETUP_TREND_RESUME: Final[str] = "trend_resume"           # retired → trend_continuation
_LEGACY_SETUP_HIGH_TIGHT_FLAG: Final[str] = "high_tight_flag"     # retired → breakout
_LEGACY_SETUP_MOMENTUM_EXTENSION: Final[str] = "momentum_extension"  # retired
_LEGACY_SETUP_UNKNOWN: Final[str] = "unknown"                     # retired

# --------------------------------------------------------------------------- #
# Risk label (output, never a config dimension — AD-22.19)
# --------------------------------------------------------------------------- #
RISK_LABEL_LOW: Final[str] = "low"
RISK_LABEL_MEDIUM: Final[str] = "medium"
RISK_LABEL_HIGH: Final[str] = "high"

ALLOWED_RISK_LABELS: Final[tuple[str, ...]] = (
    RISK_LABEL_LOW,
    RISK_LABEL_MEDIUM,
    RISK_LABEL_HIGH,
)

# --------------------------------------------------------------------------- #
# Disposition (AD-22.18)
# --------------------------------------------------------------------------- #
DISPOSITION_BUY: Final[str] = "BUY"
DISPOSITION_WATCHLIST_ONLY: Final[str] = "WATCHLIST_ONLY"
DISPOSITION_REJECTED: Final[str] = "REJECTED"

ALLOWED_DISPOSITIONS: Final[tuple[str, ...]] = (
    DISPOSITION_BUY,
    DISPOSITION_WATCHLIST_ONLY,
    DISPOSITION_REJECTED,
)

# --------------------------------------------------------------------------- #
# Outcome horizons
# --------------------------------------------------------------------------- #
OUTCOME_HORIZONS_BD: Final[tuple[int, ...]] = (5, 10, 20, 40)

# --------------------------------------------------------------------------- #
# Simulation vocabulary (AD-22.18)
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
# AI review attribution (AD-22.18)
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
# Logging
# --------------------------------------------------------------------------- #
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)s | %(name)s | %(run_id)s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"
DEFAULT_RUN_ID: Final[str] = "-"
