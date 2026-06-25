"""Default config seed payloads for fresh DB initialization (setup mode).

Setup-mode migration (AD-22.19–22.24):
- get_default_setup_configs() returns the four setup configs (breakout/pullback/
  trend_continuation/consolidation_base) from 01c_FORMULAS_AND_CONFIGS.md.
- get_default_risk_label_config() returns risk_label_config_v1.
- Legacy strategy configs (normal/aggressive/conservative) are retired.

These are seed defaults only. Runtime code must load the active config from DB
via ConfigService, not from here.
"""

from __future__ import annotations

from typing import Any, Final

from app.config import constants

SEED_VERSION: Final[str] = "v1"
SEED_CREATED_BY: Final[str] = "system_seed"

# --------------------------------------------------------------------------- #
# Setup config seeds (01c CONFIG/20–23)
# Threshold values are migration starting points — NOT tuned (AD-22.24 note).
# --------------------------------------------------------------------------- #
_UNIVERSE_BLOCK: Final[dict[str, Any]] = {
    "min_price": 5,
    "min_avg_dollar_volume_20d": 10_000_000,
    "allowed_symbol_types": ["stock"],
    "exclude_benchmarks": True,
}

_FEATURES_BLOCK: Final[dict[str, Any]] = {
    "feature_schema_version": constants.FEATURE_SCHEMA_VERSION,
}

_EARNINGS_BLOCK: Final[dict[str, Any]] = {
    "avoid_within_bd": 5,
    "penalty_points_max": -15,
}

_MACRO_BLOCK: Final[dict[str, Any]] = {
    "enabled": True,
    "window_bd_before": 1,
    "window_bd_after": 1,
    "penalty_points": -10,
}

# top_n is reserved in setup configs but controlled by risk_label_config only.
_RANKING_RESERVED: Final[dict[str, Any]] = {"top_n": None}

DEFAULT_SETUP_CONFIGS: Final[dict[str, dict[str, Any]]] = {
    "breakout": {
        "config_id": "setup_breakout_v1",
        "setup_type": "breakout",
        "version": "breakout_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "breakout_prox_min": -1.0,
            "breakout_prox_max": 0.5,
            "min_base_duration": 10,
            "min_rvol_breakout": 1.5,
            "rvol_is_hard": True,
            "min_close_strength": 0.5,
            "max_stop_distance_pct": 0.10,
            "k_atr_stop": 1.0,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "min_setup_score": 55,
        },
        "scoring_weights": {
            "resistance_clarity": 0.20,
            "breakout_confirmation": 0.25,
            "volume_expansion": 0.20,
            "base_quality": 0.20,
            "target_room": 0.15,
        },
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    "pullback": {
        "config_id": "setup_pullback_v1",
        "setup_type": "pullback",
        "version": "pullback_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "pull_band": 0.04,
            "max_pullback_depth": 0.12,
            "support_break_tol": 0.02,
            "k_atr_stop": 1.2,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_is_hard": False,  # never hard reject on low RVOL (AD-22.23)
            "rvol_bonus_threshold": 1.3,
            "max_stop_distance_pct": 0.10,
            "min_setup_score": 55,
        },
        "scoring_weights": {
            "uptrend_intact": 0.25,
            "support_ema_hold": 0.25,
            "pullback_depth": 0.20,
            "trend_structure": 0.15,
            "rr": 0.15,
        },
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    "trend_continuation": {
        "config_id": "setup_trend_continuation_v1",
        "setup_type": "trend_continuation",
        "version": "trend_continuation_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "min_ema_alignment": 50,
            "min_ema50_slope": 0.0,
            "roc_min": 0.02,
            "roc_max": 0.40,
            "max_ext": 0.15,
            "k_atr_stop": 1.5,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_is_hard": False,
            "rvol_moderate_threshold": 1.2,
            "max_stop_distance_pct": 0.10,
            "min_setup_score": 55,
        },
        "scoring_weights": {
            "trend_health": 0.25,
            "relative_strength": 0.20,
            "extension": 0.15,
            "momentum": 0.20,
            "volume_health": 0.10,
            "target_room": 0.10,
        },
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    "consolidation_base": {
        "config_id": "setup_consolidation_base_v1",
        "setup_type": "consolidation_base",
        "version": "consolidation_base_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "min_tightness": 60,
            "max_atr_pct": 0.05,
            "min_compression": 50,
            "min_range_duration": 10,
            "min_dry_up": 40,
            "min_earnings_days": 5,
            "k_atr_stop": 1.0,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_required": False,  # not required; controlled/low vol acceptable (AD-22.23)
            "max_stop_distance_pct": 0.10,
            "min_setup_score": 55,
        },
        "scoring_weights": {
            "range_tightness": 0.25,
            "support_resistance_clarity": 0.20,
            "atr_compression": 0.20,
            "volume_dry_up": 0.15,
            "breakout_readiness": 0.10,
            "stop_tightness": 0.10,
        },
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
}

# --------------------------------------------------------------------------- #
# Risk label config seed (01c CONFIG/24)
# --------------------------------------------------------------------------- #
DEFAULT_RISK_LABEL_CONFIG: Final[dict[str, Any]] = {
    "config_id": "risk_label_config_v1",
    "version": "risk_v1",
    "factor_weights": {
        "stop_distance_pct": 0.20,
        "atr_pct": 0.15,
        "ema_extension": 0.10,
        "liquidity": 0.10,
        "earnings_proximity": 0.10,
        "estimated_rr": 0.15,
        "market_regime": 0.10,
        "setup_confirmation": 0.10,
    },
    "thresholds": {"low_max": 33, "med_max": 66},
    "buy_rules": {
        "min_rr_for_buy": 1.8,
        "allowed_buy_labels": ["low", "medium"],
        "block_market_regimes": ["extreme_risk"],
        "block_if_regime_null": True,
    },
    "market_regime": {
        "high_risk_vix": 25,
        "extreme_risk_vix": 30,
    },
    "ranking": {"top_n": 20},
    "diversification": {
        "hard_cap_enabled": True,
        "sector_max_positions": 3,
        "industry_max_positions": 2,
        "sector_penalty_factor": 0.9,
        "industry_penalty_factor": 0.85,
        "penalty_applies_before_cap_only": True,
    },
    "sector_etf_mapping": {
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
    },
    "simulation": {
        "entry_rule": "next_trading_day_open_raw",
        "return_price_type": "adjusted_close",
        "slippage_bps": 10,
        "commission_per_trade": 0,
        "horizons_bd": [5, 10, 20, 40],
        "min_resolved_outcomes_pct": 0.85,
        "max_drawdown_constraint_pct": 25,
    },
}

# --------------------------------------------------------------------------- #
# Runtime config defaults (retained; pipeline/provider/debug/sim/dashboard etc.)
# --------------------------------------------------------------------------- #
DEFAULT_RUNTIME_CONFIGS: Final[dict[str, dict[str, Any]]] = {
    "pipeline": {
        "default_run_type": "scheduled",
        "lock_stale_seconds": 300,
        "force_rerun_default": False,
        "resume_behavior": "resume_from_step",
    },
    "provider": {
        "provider_name": "yahoo",
        "default_batch_size": 50,
        "retry_count": 3,
        "sleep_seconds": 1.0,
        "jitter_seconds": 0.5,
        "timeout_seconds": 30,
    },
    "data_completeness": {
        "target_historical_years": 3,
        "full_completeness_mode": "all_active_tickers",
        "sample_completeness_tickers": ["AAPL", "MSFT", "SPY"],
        "repair_max_attempts": 3,
        "max_auto_repair_gap_days": 5,
    },
    "debug": {
        "default_sample_count": 25,
        "max_sample_count": 100,
        "debug_presets": ["tiny", "small", "medium"],
        "forced_db_role": "debug",
        "forced_run_type": "debug",
    },
    "simulation": {
        "default_mode": "walk_forward",
        "min_resolved_outcomes_pct": 0.85,
        "max_drawdown_constraint_pct": 25,
    },
    "dashboard": {
        "default_db_role": "prod",
        "default_row_limit": 100,
        "default_diversified_display": True,
    },
    "ai_review": {
        "prompt_version": "v1",
        "max_tokens": 1500,
        "provider_mode": "manual",
    },
    "export": {
        "price_window_bd_around_signal": 40,
        "score_bucket_width": 5,
    },
}


def get_default_setup_configs() -> dict[str, dict[str, Any]]:
    """Return copies of the four default setup configs keyed by setup_type."""
    return {k: dict(v) for k, v in DEFAULT_SETUP_CONFIGS.items()}


def get_default_risk_label_config() -> dict[str, Any]:
    """Return a copy of the default risk-label config."""
    return dict(DEFAULT_RISK_LABEL_CONFIG)


def get_default_runtime_configs() -> dict[str, dict[str, Any]]:
    """Return copies of the default runtime configs keyed by config_type."""
    return {k: dict(v) for k, v in DEFAULT_RUNTIME_CONFIGS.items()}


def get_sector_alias_seeds() -> list[tuple[str, str, str]]:
    """Return (source, raw_sector, canonical_sector) seed rows."""
    source = constants.SECTOR_ALIAS_SOURCE_YAHOO
    return [
        (source, raw, canonical)
        for raw, canonical in constants.SECTOR_ALIAS_MAP.items()
    ]
