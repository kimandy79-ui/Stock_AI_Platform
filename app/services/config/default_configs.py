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
            "min_atr_stop_floor_multiple": 0.5,
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
            "rebound_required": True,
            "min_rebound_slope": 0.002,
            "k_atr_stop": 1.2,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_is_hard": False,  # never hard reject on low RVOL (AD-22.23)
            "rvol_bonus_threshold": 1.3,
            "min_atr_stop_floor_multiple": 0.5,
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
            "min_atr_stop_floor_multiple": 0.5,
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
            "price_above_base_tolerance": 0.01,
            "min_dry_up": 40,
            "min_earnings_days": 5,
            "k_atr_stop": 1.0,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_required": False,  # not required; controlled/low vol acceptable (AD-22.23)
            "min_atr_stop_floor_multiple": 0.3,
            # False here freezes v1's existing live behavior exactly (CODER_NOTE v3
            # item 2, option b) — min_compression/min_dry_up stay read-but-unenforced
            # until a newly-cloned config version sets this True and is activated.
            "enforce_compression_floor": False,
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
# Preset setup configs (Phase 1.5) — literature-anchored variants for
# simulation sweeps only. Never activated (active_flag=FALSE always); prod/debug
# keep exactly one active config per setup_type (the DEFAULT_SETUP_CONFIGS v1
# rows above). Each preset's ``parent_config_id`` names the v1 config it was
# cloned/tightened from, per the immutable clone-and-version rule (CLAUDE.md).
#
# Field names are taken verbatim from what each validator in
# app/services/screening/m14_setup_validators.py actually reads under
# setup_config["validation"] — no new fields invented. Two criteria named in
# the Phase 1.5 coder note have **no corresponding field** in the current
# validators and are called out per-preset below rather than silently
# approximated as if fully implemented:
#   - breakout "RS filter": validate_breakout never reads any relative-strength
#     feature (only validate_trend_continuation does). Not representable
#     without a Step 4 code change (out of scope — presets are data only).
#   - trend_continuation "RS vs SPY >0 required not soft" / "price>50MA>150MA>
#     200MA": relative_strength is scoring-only in validate_trend_continuation
#     (no hard RS gate exists), and there is no 150-day EMA/SMA feature in the
#     schema at all. Approximated by raising the "relative_strength" scoring
#     weight and requiring a high min_ema_alignment instead — not a true hard
#     requirement.
# --------------------------------------------------------------------------- #
PRESET_SETUP_CONFIGS: Final[list[dict[str, Any]]] = [
    {
        # canonical — O'Neil/Bulkowski volume-confirmation threshold (~1.5x
        # avg volume) and entry close to the pivot/resistance level.
        "config_id": "setup_breakout_canonical",
        "setup_type": "breakout",
        "version": "breakout_canonical_v1",
        "parent_config_id": "setup_breakout_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "breakout_prox_min": -0.05,
            "breakout_prox_max": 0.5,
            "min_base_duration": 20,
            "min_rvol_breakout": 1.5,
            "rvol_is_hard": True,
            "min_atr_stop_floor_multiple": 0.5,
            "k_atr_stop": 1.0,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "min_setup_score": 55,
        },
        "scoring_weights": dict(DEFAULT_SETUP_CONFIGS["breakout"]["scoring_weights"]),
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    {
        # strict — higher RVOL bar (~2.0x) and a longer, more selective base
        # (Bulkowski's longer-base/higher-reliability breakout profile). RS
        # filter from the coder note is not representable (see module note).
        "config_id": "setup_breakout_strict",
        "setup_type": "breakout",
        "version": "breakout_strict_v1",
        "parent_config_id": "setup_breakout_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "breakout_prox_min": -0.05,
            "breakout_prox_max": 0.3,
            "min_base_duration": 35,
            "min_rvol_breakout": 2.0,
            "rvol_is_hard": True,
            "min_atr_stop_floor_multiple": 0.5,
            "k_atr_stop": 1.0,
            "buffer_atr_multiple": 0.25,
            "min_rr": 2.0,
            "min_setup_score": 65,
        },
        "scoring_weights": dict(DEFAULT_SETUP_CONFIGS["breakout"]["scoring_weights"]),
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    {
        # strict — tighter ATR-compression / volume dry-up (Minervini VCP-style
        # "coiling" profile: narrower range, lower volatility, drier volume).
        "config_id": "setup_consolidation_base_strict",
        "setup_type": "consolidation_base",
        "version": "consolidation_base_strict_v1",
        "parent_config_id": "setup_consolidation_base_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "min_tightness": 75,
            "max_atr_pct": 0.035,
            "min_compression": 65,
            "min_range_duration": 15,
            "price_above_base_tolerance": 0.01,
            "min_dry_up": 55,
            "min_earnings_days": 5,
            "min_atr_stop_floor_multiple": 0.3,
            "k_atr_stop": 1.0,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_required": False,  # not required; controlled/low vol acceptable (AD-22.23)
            # Left False (not enabled) — turning this on is a threshold-tuning
            # decision reserved for post-diagnostics work (CLAUDE.md), even for a
            # simulation-only preset. A human can clone+flip this explicitly later.
            "enforce_compression_floor": False,
            "min_setup_score": 60,
        },
        "scoring_weights": dict(DEFAULT_SETUP_CONFIGS["consolidation_base"]["scoring_weights"]),
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    {
        # template — Minervini-style idealized trend profile: strong EMA
        # stacking/alignment, firmly positive 50EMA slope, not overextended.
        # RS emphasis is scoring-weight only (see module note above).
        "config_id": "setup_trend_continuation_template",
        "setup_type": "trend_continuation",
        "version": "trend_continuation_template_v1",
        "parent_config_id": "setup_trend_continuation_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "min_ema_alignment": 80,
            "min_ema50_slope": 0.005,
            "roc_min": 0.02,
            "roc_max": 0.35,
            "max_ext": 0.12,
            "k_atr_stop": 1.5,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_is_hard": False,
            "rvol_moderate_threshold": 1.2,
            "min_atr_stop_floor_multiple": 0.5,
            "min_setup_score": 65,
        },
        "scoring_weights": {
            "trend_health": 0.25,
            "relative_strength": 0.30,
            "extension": 0.10,
            "momentum": 0.20,
            "volume_health": 0.05,
            "target_room": 0.10,
        },
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    {
        # shallow — classic "first pullback" entry: shallow depth, tight
        # support tolerance (buy the first dip, not a deep retracement).
        "config_id": "setup_pullback_shallow",
        "setup_type": "pullback",
        "version": "pullback_shallow_v1",
        "parent_config_id": "setup_pullback_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "pull_band": 0.04,
            "max_pullback_depth": 0.08,
            "support_break_tol": 0.02,
            "rebound_required": True,
            "min_rebound_slope": 0.002,
            "k_atr_stop": 1.2,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_is_hard": False,  # never hard reject on low RVOL (AD-22.23)
            "rvol_bonus_threshold": 1.3,
            "min_atr_stop_floor_multiple": 0.5,
            "min_setup_score": 55,
        },
        "scoring_weights": dict(DEFAULT_SETUP_CONFIGS["pullback"]["scoring_weights"]),
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
    {
        # fib — deeper retracement toward classic Fibonacci 38.2-61.8% pullback
        # zones, with a wider support-break tolerance to match the deeper depth.
        "config_id": "setup_pullback_fib",
        "setup_type": "pullback",
        "version": "pullback_fib_v1",
        "parent_config_id": "setup_pullback_v1",
        "universe": _UNIVERSE_BLOCK,
        "features": _FEATURES_BLOCK,
        "validation": {
            "pull_band": 0.04,
            "max_pullback_depth": 0.15,
            "support_break_tol": 0.04,
            "rebound_required": True,
            "min_rebound_slope": 0.002,
            "k_atr_stop": 1.2,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_is_hard": False,
            "rvol_bonus_threshold": 1.3,
            "min_atr_stop_floor_multiple": 0.5,
            "min_setup_score": 55,
        },
        "scoring_weights": dict(DEFAULT_SETUP_CONFIGS["pullback"]["scoring_weights"]),
        "ranking": _RANKING_RESERVED,
        "earnings": _EARNINGS_BLOCK,
        "macro_event_risk": _MACRO_BLOCK,
    },
]

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
        # The actual BUY/WATCHLIST stop-distance hard gate (step5_proposal_engine.py
        # Fix 2) reads this key — not the max_stop_distance_pct copy carried in each
        # setup_config's own validation block, which is display-only.
        "max_stop_distance_pct": 0.10,
    },
    "market_regime": {
        "high_risk_vix": 25,
        "extreme_risk_vix": 30,
    },
    "ranking": {"top_n": 20},
    "diversification": {
        "hard_cap_enabled": True,
        "sector_max_positions": 4,
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
# Risk label config v2 (CODER_NOTE v3 item 6) — promotes earnings/macro penalty
# config to a single shared source, instead of the same _EARNINGS_BLOCK/
# _MACRO_BLOCK values being duplicated identically across every setup_config.
# Cloned from v1 (never an edit to the active risk_label_config_v1 row — config
# is immutable; clone-and-version per CLAUDE.md). Seeded via
# ConfigService.seed_risk_label_config_v2() with active_flag=FALSE always; must
# be explicitly activated by a human before m14_setup_validators.py's dual-read
# fallback (_resolve_earnings_macro_cfg) prefers it over each setup_config's own
# copy. Values are identical to what's already active today via the per-setup-
# config route, so activating this is a zero-behavior-change operation.
# --------------------------------------------------------------------------- #
DEFAULT_RISK_LABEL_CONFIG_V2: Final[dict[str, Any]] = {
    **DEFAULT_RISK_LABEL_CONFIG,
    "config_id": "risk_label_config_v2",
    "version": "risk_v2",
    "earnings": _EARNINGS_BLOCK,
    "macro_event_risk": _MACRO_BLOCK,
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
        # Phase 3 — multi-pass AI review (thesis/contrarian/audit). Disabled
        # by default: export_ticker_review / export_simulation_review write
        # the single legacy "manual"/"none" row (review_kind=NULL) exactly as
        # before when this is False or omitted. Set enabled=True (and real
        # provider/model values) to opt into writing one row per pass instead.
        "multi_pass": {
            "enabled": False,
            "thesis": {"provider": "anthropic", "model": "claude-sonnet-5"},
            "contrarian": {"provider": "openai", "model": "gpt-4o"},
            "audit": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        },
    },
    "export": {
        "price_window_bd_around_signal": 40,
        "score_bucket_width": 5,
    },
}


def get_default_setup_configs() -> dict[str, dict[str, Any]]:
    """Return copies of the four default setup configs keyed by setup_type."""
    return {k: dict(v) for k, v in DEFAULT_SETUP_CONFIGS.items()}


def get_preset_setup_configs() -> list[dict[str, Any]]:
    """Return copies of the literature-anchored preset setup configs.

    Unlike ``get_default_setup_configs`` (one row per setup_type, seeded
    active), presets are a list — several per setup_type are allowed, and
    every preset is seeded inactive (simulation-sweep input only).
    """
    return [dict(p) for p in PRESET_SETUP_CONFIGS]


def get_default_risk_label_config() -> dict[str, Any]:
    """Return a copy of the default risk-label config."""
    return dict(DEFAULT_RISK_LABEL_CONFIG)


def get_risk_label_config_v2() -> dict[str, Any]:
    """Return a copy of the v2 risk-label config (shared earnings/macro block).

    Not active by default — see ConfigService.seed_risk_label_config_v2.
    """
    return dict(DEFAULT_RISK_LABEL_CONFIG_V2)


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
