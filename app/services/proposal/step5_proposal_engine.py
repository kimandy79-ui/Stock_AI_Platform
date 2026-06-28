"""Module 15 — Step 5 Proposal Engine (setup-mode, Phase 5 accepted).

Reads setup-valid Step 4 analyses for one ``signal_date``, computes structural
stop / target / RR for each (ticker, setup_type) pair, assigns a risk score and
risk_label (low / medium / high) from ``risk_label_config``, determines
disposition (BUY / WATCHLIST_ONLY / REJECTED), dedupes multi-route tickers to
their best risk-adjusted route, scores, ranks (raw + diversified), and writes
one row per candidate to ``step5_proposals`` in a single transaction.

Phase 5 accepted contracts:
  Fix 1 — SQL: _SQL_READ_ANALYSES has exactly one explanation_json column;
           sector/industry mapping is correct.
  Fix 2 — Max stop-distance hard gate: config-driven via buy_rules.max_stop_distance_pct.
           A candidate whose stop_distance_pct exceeds this threshold is forced to
           WATCHLIST_ONLY (rejection_reason = "stop_distance_exceeds_max") regardless
           of weighted risk score or RR. Hard gate evaluated before all other BUY checks.
  Fix 3 — Target-room-before-resistance: two distinct cases when no structural target
           clears buy_rules.min_target_room_pct (default 5%):
             • Structural evidence EXISTS above entry but is too close (resistance_blocks=True)
               → WATCHLIST_ONLY (rejection_reason = "target_room_insufficient").
               Fixed-R is NOT used to bypass nearby resistance.
             • No structural evidence above entry at all (resistance_blocks=False)
               → fixed-R fallback is acceptable; target_is_structural=False.
  Fix 4 — Invalidation level: invalidation_level_raw = stop_price_raw, documented
           in mechanical_explanation alongside invalidation_reason (stop basis label).
  P0    — final_trade_decision: separates candidate quality (setup_score / disposition)
           from action readiness. BUY disposition may be demoted to WAIT_FOR_BREAKOUT,
           WAIT_FOR_PULLBACK_CONFIRMATION, or WAIT_FOR_RISK_PLAN_FIX before writing to
           mechanical_explanation. No schema change — stored in mechanical_explanation JSON.

Pipeline position (01d_MODULES_AND_PIPELINE.md):
    Step 4 (per setup_config_id) → Step 5 (once per signal_date) → Outcome Queue

Sources of truth:
    01c §FORMULAS/62  stop/target formulas per setup
    01c §FORMULAS/63  risk scoring, disposition, ranking
    01b §step5_proposals  column contract
    02b AD-22.11/12/13/21/22/23

Key contracts:
- Only setup-valid rows (setup_passed = TRUE) get stop/target/risk; failed → REJECTED.
- stop/target/RR from structural levels (raw-converted adj→raw); fixed-R is fallback only.
- estimated_rr is always an OUTPUT; never a fixed constant.
- risk_label is an OUTPUT (low/medium/high), NOT a config dimension.
- Max stop-distance is a hard BUY gate (fix 2); cannot be overridden by other score factors.
- Target room before resistance checked before accepting structural target (fix 3).
- NULL regime → market_score=0; blocks BUY (AD fix 9).
- top_n exclusively from risk_label_config.ranking.top_n.
- No aggressive/normal/conservative strategy-mode logic.
- DB roles: prod / debug only. simulation is forbidden.
- Append-only inserts. No DDL. No print(). No direct duckdb import.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.config import constants
from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result as sr
from app.utils.service_result import ServiceResult

# ---------------------------------------------------------------------------
# DB role guard
# ---------------------------------------------------------------------------
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# ---------------------------------------------------------------------------
# Diversification sentinel strings
# ---------------------------------------------------------------------------
UNKNOWN_SECTOR: Final[str] = "__UNKNOWN_SECTOR__"
UNKNOWN_INDUSTRY: Final[str] = "__UNKNOWN_INDUSTRY__"

# ---------------------------------------------------------------------------
# Disposition constants
# ---------------------------------------------------------------------------
DISPOSITION_BUY: Final[str] = constants.DISPOSITION_BUY
DISPOSITION_WATCHLIST: Final[str] = constants.DISPOSITION_WATCHLIST_ONLY
DISPOSITION_REJECTED: Final[str] = constants.DISPOSITION_REJECTED

# ---------------------------------------------------------------------------
# Rejection / watchlist reason labels
# ---------------------------------------------------------------------------
REJECT_SECTOR_CAP: Final[str] = "sector_cap"
REJECT_INDUSTRY_CAP: Final[str] = "industry_cap"
WATCHLIST_STOP_TOO_WIDE: Final[str] = "stop_distance_exceeds_max"
WATCHLIST_TARGET_ROOM_INSUFFICIENT: Final[str] = "target_room_insufficient"

# ---------------------------------------------------------------------------
# Final trade decision constants (P0 trade readiness, stored in mechanical_explanation)
# ---------------------------------------------------------------------------
FTD_BUY: Final[str] = "BUY"
FTD_WAIT_FOR_BREAKOUT: Final[str] = "WAIT_FOR_BREAKOUT"
FTD_WAIT_FOR_PULLBACK_CONFIRMATION: Final[str] = "WAIT_FOR_PULLBACK_CONFIRMATION"
FTD_WAIT_FOR_RISK_PLAN_FIX: Final[str] = "WAIT_FOR_RISK_PLAN_FIX"
FTD_WATCHLIST_ONLY: Final[str] = "WATCHLIST_ONLY"
FTD_REJECTED: Final[str] = "REJECTED"

# Maps final_trade_decision → effective_disposition (P0: action-readiness layer).
# Any WAIT_* → WATCHLIST_ONLY so the dashboard never shows a clean BUY when entry
# conditions are not yet met.
_FTD_TO_EFFECTIVE_DISP: Final[dict[str, str]] = {
    FTD_BUY: DISPOSITION_BUY,
    FTD_WAIT_FOR_BREAKOUT: DISPOSITION_WATCHLIST,
    FTD_WAIT_FOR_PULLBACK_CONFIRMATION: DISPOSITION_WATCHLIST,
    FTD_WAIT_FOR_RISK_PLAN_FIX: DISPOSITION_WATCHLIST,
    FTD_WATCHLIST_ONLY: DISPOSITION_WATCHLIST,
    FTD_REJECTED: DISPOSITION_REJECTED,
}

# Minimum stop distance in ATR units to qualify as a tradeable risk plan.
# Config-overridable via buy_rules.min_stop_distance_atr (P4).
_STOP_DISTANCE_ATR_MIN: Final[float] = 0.50

# ---------------------------------------------------------------------------
# Market regime → market_score mapping (01c §63)
# ---------------------------------------------------------------------------
_MARKET_SCORE: Final[dict[str, float]] = {
    "bull": 100.0,
    "neutral": 60.0,
    "bear": 20.0,
    "high_risk": 0.0,
    "extreme_risk": 0.0,
}

# ---------------------------------------------------------------------------
# Proposal score weights (01c §63)
# _W_STOP_DIST: stop tightness quality; reduces confirmation weight to balance RVOL
# ---------------------------------------------------------------------------
_W_SETUP: Final[float] = 0.40
_W_RR: Final[float] = 0.25
_W_CONFIRMATION: Final[float] = 0.15
_W_MARKET: Final[float] = 0.10
_W_STOP_DIST: Final[float] = 0.10

# Normalised stop distance at which stop_quality score reaches 0 (10% = max acceptable)
_STOP_QUALITY_ZERO_PCT: Final[float] = 0.10

# ---------------------------------------------------------------------------
# Metadata keys contract
# ---------------------------------------------------------------------------
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "signal_date",
    "run_id",
    "setup_config_id",
    "analyses_read",
    "proposals_written",
    "raw_top_n_count",
    "diversified_top_n_count",
    "hard_cap_rejections",
)

# Default target-room threshold (fix 3): minimum (target-entry)/entry fraction
# to accept a structural target. Config-overridable via buy_rules.min_target_room_pct.
_DEFAULT_MIN_TARGET_ROOM_PCT: Final[float] = 0.05  # 5%

# Default max stop-distance for BUY gate (fix 2).
# Config-overridable via buy_rules.max_stop_distance_pct.
_DEFAULT_MAX_STOP_DISTANCE_PCT: Final[float] = 0.10  # 10%


# ---------------------------------------------------------------------------
# Protocol for DuckDB manager injection
# ---------------------------------------------------------------------------
class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(fv) else fv


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _raw_conv(level_adj: float | None, close_raw: float, close_adj: float) -> float | None:
    """level_raw = level_adj * (close_raw / close_adj)."""
    if level_adj is None or close_adj is None or close_adj <= 0 or close_raw is None:
        return None
    return level_adj * (close_raw / close_adj)


# ---------------------------------------------------------------------------
# RR tier score (01c §63)
# ---------------------------------------------------------------------------
def _rr_score(rr: float | None) -> float:
    if rr is None:
        return 0.0
    if rr >= 3.0:
        return 100.0
    if rr >= 2.2:
        return 80.0
    if rr >= 1.8:
        return 60.0
    if rr >= 1.3:
        return 30.0
    return 0.0


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class _ConfigError(ValueError):
    pass


def _parse_risk_label_config(cfg: dict) -> dict[str, Any]:
    """Validate and extract required keys from risk_label_config."""
    if not isinstance(cfg, dict):
        raise _ConfigError("risk_label_config must be a dict")

    factor_weights = cfg.get("factor_weights", {})
    if not isinstance(factor_weights, dict):
        raise _ConfigError("risk_label_config.factor_weights must be a dict")

    thresholds = cfg.get("thresholds", {})
    low_max = _f(thresholds.get("low_max", 33))
    med_max = _f(thresholds.get("med_max", 66))
    if low_max is None or med_max is None:
        raise _ConfigError("risk_label_config.thresholds.low_max/med_max required")

    buy_rules = cfg.get("buy_rules", {})
    min_rr_for_buy = _f(buy_rules.get("min_rr_for_buy", 1.8)) or 1.8
    allowed_buy_labels = list(buy_rules.get("allowed_buy_labels", ["low", "medium"]))
    block_market_regimes = list(buy_rules.get("block_market_regimes", ["extreme_risk"]))
    block_if_regime_null = bool(buy_rules.get("block_if_regime_null", True))

    # Fix 2: config-driven max stop-distance hard gate
    max_stop_distance_pct = (
        _f(buy_rules.get("max_stop_distance_pct", _DEFAULT_MAX_STOP_DISTANCE_PCT))
        or _DEFAULT_MAX_STOP_DISTANCE_PCT
    )

    # Fix 3: config-driven minimum target room before resistance
    min_target_room_pct = (
        _f(buy_rules.get("min_target_room_pct", _DEFAULT_MIN_TARGET_ROOM_PCT))
        or _DEFAULT_MIN_TARGET_ROOM_PCT
    )

    # P4: config-driven minimum stop distance in ATR units (FTD gate)
    min_stop_distance_atr = (
        _f(buy_rules.get("min_stop_distance_atr", _STOP_DISTANCE_ATR_MIN))
        or _STOP_DISTANCE_ATR_MIN
    )

    ranking = cfg.get("ranking", {})
    top_n = int(ranking.get("top_n", 20))
    if top_n <= 0:
        raise _ConfigError("risk_label_config.ranking.top_n must be > 0")

    div = cfg.get("diversification", {})
    hard_cap_enabled = bool(div.get("hard_cap_enabled", True))
    max_sector = int(div.get("max_sector_count", div.get("sector_max_positions", 4)))
    max_industry = int(div.get("max_industry_count", div.get("industry_max_positions", 2)))
    sector_penalty = _f(div.get("sector_penalty", div.get("sector_penalty_factor", 0.9))) or 0.9
    industry_penalty = _f(div.get("industry_penalty", div.get("industry_penalty_factor", 0.85))) or 0.85

    return {
        "factor_weights": factor_weights,
        "low_max": low_max,
        "med_max": med_max,
        "min_rr_for_buy": min_rr_for_buy,
        "allowed_buy_labels": allowed_buy_labels,
        "block_market_regimes": block_market_regimes,
        "block_if_regime_null": block_if_regime_null,
        "max_stop_distance_pct": max_stop_distance_pct,
        "min_target_room_pct": min_target_room_pct,
        "min_stop_distance_atr": min_stop_distance_atr,
        "top_n": top_n,
        "hard_cap_enabled": hard_cap_enabled,
        "max_sector_count": max_sector,
        "max_industry_count": max_industry,
        "sector_penalty": sector_penalty,
        "industry_penalty": industry_penalty,
    }


# ---------------------------------------------------------------------------
# Stop / target computation (01c §62 per-setup trade plan)
# ---------------------------------------------------------------------------

def _check_target_room(
    entry: float,
    target: float | None,
    min_target_room_pct: float,
) -> bool:
    """Return True if target has sufficient room above entry (fix 3).

    target_room_pct = (target - entry) / entry >= min_target_room_pct
    """
    if target is None or entry <= 0:
        return False
    return (target - entry) / entry >= min_target_room_pct


def _compute_stop_target(
    setup_type: str,
    feat: dict[str, Any],
    setup_cfg: dict[str, Any],
    entry: float,
    close_raw: float,
    close_adj: float,
    min_target_room_pct: float = _DEFAULT_MIN_TARGET_ROOM_PCT,
) -> tuple[float | None, float | None, bool, str, str | None, bool]:
    """Compute (stop, target, target_is_structural, stop_basis, target_basis, resistance_blocks).

    All levels returned in raw-price units.
    Fixed-R fallback: target = entry + min_rr * (entry - stop).
    target_is_structural = True iff structural target found with sufficient room.
    stop_basis: human-readable source label for stop level.
    target_basis: human-readable source label for target level.
    resistance_blocks: True when a structural level above entry EXISTS but fails
      min_target_room_pct (i.e. evidence proves insufficient room).
      False when no structural evidence above entry exists at all.

    Fix 3 (corrected): two distinct cases when no structural target clears the bar:
      - resistance_blocks=True  → evidence exists and blocks the trade; caller must
        force WATCHLIST_ONLY (WATCHLIST_TARGET_ROOM_INSUFFICIENT). Fixed-R must not
        be used to bypass nearby resistance.
      - resistance_blocks=False → no structural evidence above entry; fixed-R fallback
        is acceptable.
    """
    val = setup_cfg.get("validation", {})
    k_atr = _f(val.get("k_atr_stop", 1.0)) or 1.0
    buf_mult = _f(val.get("buffer_atr_multiple", 0.25)) or 0.25
    min_rr = _f(val.get("min_rr", 1.8)) or 1.8

    def rc(level_adj: float | None) -> float | None:
        return _raw_conv(level_adj, close_raw, close_adj)

    def _has_room(t: float | None) -> bool:
        return _check_target_room(entry, t, min_target_room_pct)

    # Build ATR in raw units
    atr14 = _f(feat.get("atr14"))
    atr_raw: float
    if atr14 is not None:
        atr_conv = rc(atr14)
        atr_raw = atr_conv if atr_conv is not None else entry * 0.02
    else:
        atr_pct_v = _f(feat.get("atr_pct"))
        atr_raw = entry * (atr_pct_v if atr_pct_v else 0.02)
    buffer_atr = buf_mult * atr_raw

    # Raw-convert all structural levels
    support_raw = rc(_f(feat.get("support_level")))
    resistance_raw = rc(_f(feat.get("resistance_level")))
    next_resistance_raw = rc(_f(feat.get("next_resistance_level")))
    swing_high_raw = rc(_f(feat.get("swing_high")))
    swing_low_raw = rc(_f(feat.get("swing_low")))
    base_high_raw = rc(_f(feat.get("base_high")))
    base_low_raw = rc(_f(feat.get("base_low")))

    ema20 = _f(feat.get("ema20"))
    ema50 = _f(feat.get("ema50"))
    ema_area_raw: float | None = None
    if ema20 is not None and ema50 is not None:
        ema_area_raw = rc(min(ema20, ema50))
    elif ema20 is not None:
        ema_area_raw = rc(ema20)
    elif ema50 is not None:
        ema_area_raw = rc(ema50)

    stop: float | None = None
    target: float | None = None
    stop_basis = "atr_fallback"
    target_basis: str | None = None
    structural = False
    # Track whether any structural level above entry EXISTS but fails room check.
    # True means resistance blocks the trade; fixed-R must NOT be used as bypass.
    _any_above_entry: bool = False   # any structural candidate above entry seen
    _any_cleared_room: bool = False  # any structural candidate that cleared room

    # ---- BREAKOUT --------------------------------------------------------
    if setup_type == constants.SETUP_BREAKOUT:
        candidates = []
        if base_low_raw is not None:
            candidates.append(base_low_raw)
        if resistance_raw is not None:
            candidates.append(resistance_raw - k_atr * atr_raw)
        if candidates:
            stop = min(candidates) - buffer_atr
            stop_basis = "base_low+resistance"
        else:
            stop = entry - k_atr * atr_raw - buffer_atr

        # Target priority: next_resistance → swing_high (not same as resistance) → measured_move
        _t = next_resistance_raw
        if _t is not None and _t > entry:
            _any_above_entry = True
            if _has_room(_t):
                target = _t; target_basis = "next_resistance"; structural = True; _any_cleared_room = True
        if not structural:
            _t = swing_high_raw
            if (_t is not None and _t > entry
                    and (resistance_raw is None or abs(_t - resistance_raw) > atr_raw * 0.5)):
                _any_above_entry = True
                if _has_room(_t):
                    target = _t; target_basis = "swing_high"; structural = True; _any_cleared_room = True
        if not structural and base_high_raw is not None and base_low_raw is not None:
            _t = base_high_raw + (base_high_raw - base_low_raw)
            if _t > entry:
                _any_above_entry = True
                if _has_room(_t):
                    target = _t; target_basis = "measured_move"; structural = True; _any_cleared_room = True

    # ---- PULLBACK --------------------------------------------------------
    elif setup_type == constants.SETUP_PULLBACK:
        candidates = []
        if support_raw is not None and support_raw < entry:
            candidates.append(support_raw)
        if swing_low_raw is not None and swing_low_raw < entry:
            candidates.append(swing_low_raw)
        if ema_area_raw is not None and ema_area_raw < entry:
            candidates.append(ema_area_raw)
        if candidates:
            stop = min(candidates) - buffer_atr
            stop_basis = "support+ema"
        else:
            stop = entry - k_atr * atr_raw - buffer_atr

        _t = swing_high_raw
        if _t is not None and _t > entry:
            _any_above_entry = True
            if _has_room(_t):
                target = _t; target_basis = "swing_high"; structural = True; _any_cleared_room = True
        if not structural:
            _t = next_resistance_raw
            if _t is not None and _t > entry:
                _any_above_entry = True
                if _has_room(_t):
                    target = _t; target_basis = "next_resistance"; structural = True; _any_cleared_room = True

    # ---- TREND_CONTINUATION ----------------------------------------------
    elif setup_type == constants.SETUP_TREND_CONTINUATION:
        if swing_low_raw is not None and swing_low_raw < entry:
            stop = swing_low_raw - buffer_atr
            stop_basis = "higher_low"
        else:
            stop = entry - k_atr * atr_raw - buffer_atr

        _t = next_resistance_raw
        if _t is not None and _t > entry:
            _any_above_entry = True
            if _has_room(_t):
                target = _t; target_basis = "next_resistance"; structural = True; _any_cleared_room = True
        if not structural and swing_low_raw is not None and swing_low_raw < entry:
            _t = entry + (entry - swing_low_raw)
            if _t > entry:
                _any_above_entry = True
                if _has_room(_t):
                    target = _t; target_basis = "measured_move"; structural = True; _any_cleared_room = True

    # ---- CONSOLIDATION_BASE ----------------------------------------------
    elif setup_type == constants.SETUP_CONSOLIDATION_BASE:
        base_stop = base_low_raw - buffer_atr if base_low_raw is not None else None
        sup_stop = support_raw - buffer_atr if support_raw is not None else None
        if base_stop is not None and sup_stop is not None:
            stop = min(base_stop, sup_stop); stop_basis = "base_low"
        elif base_stop is not None:
            stop = base_stop; stop_basis = "base_low"
        elif sup_stop is not None:
            stop = sup_stop; stop_basis = "support"
        else:
            stop = entry - k_atr * atr_raw - buffer_atr

        if base_high_raw is not None and base_low_raw is not None:
            rng = base_high_raw - base_low_raw
            upper_thresh = base_low_raw + 0.66 * rng
            if entry < upper_thresh:
                _t = base_high_raw
                if _t > entry:
                    _any_above_entry = True
                    if _has_room(_t):
                        target = _t; target_basis = "base_high"; structural = True; _any_cleared_room = True
                if not structural:
                    _t2 = next_resistance_raw
                    if _t2 is not None and _t2 > entry:
                        _any_above_entry = True
                        if _has_room(_t2):
                            target = _t2; target_basis = "next_resistance"; structural = True; _any_cleared_room = True
            else:
                _t = base_high_raw + rng
                if _t > entry:
                    _any_above_entry = True
                    if _has_room(_t):
                        target = _t; target_basis = "measured_move"; structural = True; _any_cleared_room = True
                if not structural:
                    _t2 = next_resistance_raw
                    if _t2 is not None and _t2 > entry:
                        _any_above_entry = True
                        if _has_room(_t2):
                            target = _t2; target_basis = "next_resistance"; structural = True; _any_cleared_room = True

    else:
        stop = entry - k_atr * atr_raw - buffer_atr

    # Sanity: stop must be below entry
    if stop is not None and stop >= entry:
        stop = entry - k_atr * atr_raw - buffer_atr
        stop_basis = "atr_fallback"

    # resistance_blocks: evidence of levels above entry that failed the room check.
    # True  → structural proof that there is NOT enough room; fixed-R must not bypass.
    # False → no structural evidence above entry; fixed-R fallback is acceptable.
    resistance_blocks = _any_above_entry and not _any_cleared_room

    if not structural or target is None or target <= entry:
        if resistance_blocks:
            # Nearest structural level blocks the trade — do not use fixed-R to bypass.
            # Caller will force WATCHLIST_ONLY via WATCHLIST_TARGET_ROOM_INSUFFICIENT.
            target = None
            target_basis = "blocked_by_resistance"
            structural = False
        elif stop is not None and stop < entry:
            # No structural evidence at all — fixed-R fallback is acceptable.
            target = entry + min_rr * (entry - stop)
            target_basis = "fixed_r_fallback"
            structural = False
        else:
            target = None
            target_basis = None

    return stop, target, structural, stop_basis, target_basis, resistance_blocks


def _compute_estimated_rr(
    entry: float | None,
    stop: float | None,
    target: float | None,
) -> float | None:
    """estimated_rr = (target - entry) / (entry - stop). Always an output."""
    if entry is None or stop is None or target is None:
        return None
    denom = entry - stop
    if denom <= 0:
        return None
    rr = (target - entry) / denom
    return rr if rr > 0 else None


# ---------------------------------------------------------------------------
# Risk score / label (01c §63)
# ---------------------------------------------------------------------------

def _compute_risk_score(
    feat: dict[str, Any],
    entry: float | None,
    stop: float | None,
    estimated_rr: float | None,
    market_regime: str | None,
    setup_score: float | None,
    earnings_days: int | None,
    cfg: dict[str, Any],
) -> tuple[float, str, list[str]]:
    """Return (risk_score 0-100, risk_label, risk_reasons)."""
    weights = cfg.get("factor_weights", {})

    def _w(key: str, default: float) -> float:
        return _f(weights.get(key, default)) or default

    w_sdp = _w("stop_distance_pct", 0.20)
    w_atr = _w("atr_pct", 0.15)
    w_ext = _w("ema_extension", 0.10)
    w_liq = _w("liquidity", 0.10)
    w_earn = _w("earnings_proximity", 0.10)
    w_rr = _w("estimated_rr", 0.15)
    w_reg = _w("market_regime", 0.10)
    w_conf = _w("setup_confirmation", 0.10)

    factors: dict[str, float] = {}

    # stop_distance_pct (wider stop = higher risk)
    if entry is not None and stop is not None and entry > 0:
        sdp = (entry - stop) / entry
        factors["stop_distance_pct"] = _clamp(sdp / 0.15 * 100)
    else:
        factors["stop_distance_pct"] = 100.0

    # atr_pct
    atr_v = _f(feat.get("atr_pct"))
    factors["atr_pct"] = _clamp(atr_v / 0.08 * 100) if atr_v is not None else 50.0

    # ema_extension
    d20 = _f(feat.get("distance_to_ema20_pct"))
    d50 = _f(feat.get("distance_to_ema50_pct"))
    ext_vals = [abs(v) for v in [d20, d50] if v is not None]
    factors["ema_extension"] = _clamp(max(ext_vals) / 0.20 * 100) if ext_vals else 50.0

    # liquidity (inverse)
    adv = _f(feat.get("avg_dollar_volume_20d"))
    factors["liquidity"] = (
        _clamp(100.0 * (1.0 - min(adv / 100_000_000.0, 1.0)))
        if adv is not None and adv > 0
        else 100.0
    )

    # earnings_proximity
    if earnings_days is not None:
        if earnings_days <= 0:
            factors["earnings_proximity"] = 100.0
        elif earnings_days <= 5:
            factors["earnings_proximity"] = 80.0
        elif earnings_days <= 10:
            factors["earnings_proximity"] = 40.0
        else:
            factors["earnings_proximity"] = 0.0
    else:
        factors["earnings_proximity"] = 0.0

    # estimated_rr (inverse)
    factors["estimated_rr"] = (
        _clamp(100.0 * max(0.0, (3.0 - estimated_rr) / 3.0))
        if estimated_rr is not None
        else 100.0
    )

    # market_regime
    if market_regime is None:
        factors["market_regime"] = 100.0  # AD fix 9: NULL = max risk
    elif market_regime == "bull":
        factors["market_regime"] = 0.0
    elif market_regime == "neutral":
        factors["market_regime"] = 30.0
    elif market_regime == "bear":
        factors["market_regime"] = 70.0
    elif market_regime in ("high_risk", "extreme_risk"):
        factors["market_regime"] = 100.0
    else:
        factors["market_regime"] = 100.0

    # setup_confirmation (inverse)
    factors["setup_confirmation"] = (
        _clamp(100.0 - setup_score) if setup_score is not None else 100.0
    )

    w_map = {
        "stop_distance_pct": w_sdp, "atr_pct": w_atr,
        "ema_extension": w_ext, "liquidity": w_liq,
        "earnings_proximity": w_earn, "estimated_rr": w_rr,
        "market_regime": w_reg, "setup_confirmation": w_conf,
    }

    risk_score = _clamp(sum(w_map[k] * v for k, v in factors.items()))

    low_max = _f(cfg.get("low_max", 33)) or 33.0
    med_max = _f(cfg.get("med_max", 66)) or 66.0
    if risk_score <= low_max:
        risk_label = constants.RISK_LABEL_LOW
    elif risk_score <= med_max:
        risk_label = constants.RISK_LABEL_MEDIUM
    else:
        risk_label = constants.RISK_LABEL_HIGH

    sorted_f = sorted(factors.items(), key=lambda x: -w_map[x[0]] * x[1])
    risk_reasons = [f"{k}={v:.1f}" for k, v in sorted_f[:4]]

    return risk_score, risk_label, risk_reasons


# ---------------------------------------------------------------------------
# Disposition (01c §63 + fix 2 max stop-distance hard gate)
# ---------------------------------------------------------------------------

def _assign_disposition(
    setup_passed: bool,
    estimated_rr: float | None,
    risk_label: str,
    market_regime: str | None,
    stop_distance_pct: float | None,
    cfg: dict[str, Any],
) -> tuple[str, str | None]:
    """Return (disposition, watchlist_reason).

    Fix 2: max_stop_distance_pct is a hard gate applied before any other check.
    A candidate exceeding this threshold is forced to WATCHLIST_ONLY regardless
    of how well it scores on other weighted factors.
    """
    if not setup_passed:
        return DISPOSITION_REJECTED, None

    # Fix 2: max stop-distance hard gate
    max_sdp = _f(cfg.get("max_stop_distance_pct", _DEFAULT_MAX_STOP_DISTANCE_PCT)) or _DEFAULT_MAX_STOP_DISTANCE_PCT
    if stop_distance_pct is not None and stop_distance_pct > max_sdp:
        return DISPOSITION_WATCHLIST, WATCHLIST_STOP_TOO_WIDE

    min_rr = _f(cfg.get("min_rr_for_buy", 1.8)) or 1.8
    allowed_buy = cfg.get("allowed_buy_labels", ["low", "medium"])
    block_regimes = cfg.get("block_market_regimes", ["extreme_risk"])
    block_null = bool(cfg.get("block_if_regime_null", True))

    if market_regime is None and block_null:
        return DISPOSITION_WATCHLIST, "null_market_regime"
    if market_regime in block_regimes:
        return DISPOSITION_WATCHLIST, f"blocked_regime:{market_regime}"
    if estimated_rr is None or estimated_rr < min_rr:
        return DISPOSITION_WATCHLIST, "rr_below_min"
    if risk_label not in allowed_buy:
        return DISPOSITION_WATCHLIST, f"risk_label:{risk_label}"

    return DISPOSITION_BUY, None


# ---------------------------------------------------------------------------
# Final trade decision (P0 — candidate quality vs. action readiness)
# ---------------------------------------------------------------------------

def _compute_final_trade_decision(
    setup_type: str,
    disposition: str,
    close_raw: float,
    resistance_raw: float | None,
    stop_distance_atr: float | None,
    dist_ema20: float | None,
    rvol_val: float | None,
    roc20_val: float | None = None,
    ema20_slope_val: float | None = None,
    min_stop_distance_atr: float = _STOP_DISTANCE_ATR_MIN,
) -> tuple[str, list[str]]:
    """Return (final_trade_decision, reason_codes).

    Disposition BUY passes through unless a readiness gate fires:
      breakout — price must have cleared resistance.
      all      — stop must be ≥ min_stop_distance_atr ATR from entry (P4: config-driven).
      pullback — three independent confirmation gates (P1):
                   roc20 < 0            → momentum deteriorating
                   ema20_slope < 0      → EMA20 declining
                   close below EMA20 without volume surge (rvol < 1.2) → no bounce
    WATCHLIST_ONLY / REJECTED pass through unchanged.
    """
    if disposition == DISPOSITION_REJECTED:
        return FTD_REJECTED, []
    if disposition == DISPOSITION_WATCHLIST:
        return FTD_WATCHLIST_ONLY, []

    if setup_type == "breakout":
        if resistance_raw is not None and close_raw > 0 and close_raw < resistance_raw:
            return FTD_WAIT_FOR_BREAKOUT, ["price_below_resistance"]

    if stop_distance_atr is not None and stop_distance_atr < min_stop_distance_atr:
        return FTD_WAIT_FOR_RISK_PLAN_FIX, ["stop_too_tight_vs_atr"]

    if setup_type == "pullback":
        wait_reasons: list[str] = []
        if roc20_val is not None and roc20_val < 0:
            wait_reasons.append("pullback_negative_momentum")
        if ema20_slope_val is not None and ema20_slope_val < 0:
            wait_reasons.append("ema20_slope_negative")
        below_ema20 = dist_ema20 is not None and dist_ema20 < 0
        vol_confirmed = rvol_val is not None and rvol_val >= 1.2
        if below_ema20 and not vol_confirmed:
            wait_reasons.append("rebound_not_confirmed")
        if wait_reasons:
            return FTD_WAIT_FOR_PULLBACK_CONFIRMATION, wait_reasons

    return FTD_BUY, []


# ---------------------------------------------------------------------------
# Proposal score (01c §63)
# ---------------------------------------------------------------------------

def _proposal_score_raw(
    setup_score: float,
    estimated_rr: float | None,
    confirmation_score: float,
    market_regime: str | None,
    stop_distance_pct: float | None = None,
) -> float:
    rrsc = _rr_score(estimated_rr)
    msc = _MARKET_SCORE.get(market_regime or "", 0.0) if market_regime else 0.0
    # Tight stop = higher stop quality. Unknown stop treated as neutral (50).
    if stop_distance_pct is not None and stop_distance_pct >= 0:
        stop_quality = _clamp(100.0 * max(0.0, 1.0 - stop_distance_pct / _STOP_QUALITY_ZERO_PCT))
    else:
        stop_quality = 50.0
    return _clamp(
        _W_SETUP * setup_score
        + _W_RR * rrsc
        + _W_CONFIRMATION * confirmation_score
        + _W_MARKET * msc
        + _W_STOP_DIST * stop_quality
    )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Fix 1 confirmed: no duplicate explanation_json; sector/industry correctly mapped.
_SQL_READ_ANALYSES: Final[str] = """
SELECT
    a.analysis_id,
    a.candidate_id,
    a.setup_config_id,
    a.ticker,
    a.signal_date,
    a.setup_type,
    a.setup_score,
    a.setup_passed,
    a.setup_reasons,
    a.setup_fail_reason,
    a.entry_price_raw,
    a.support_level,
    a.resistance_level,
    a.next_resistance_level,
    a.atr_pct,
    a.distance_to_ema20_pct,
    a.distance_to_ema50_pct,
    a.rvol,
    a.earnings_days,
    a.market_regime,
    a.earnings_penalty,
    a.macro_penalty,
    a.explanation_json,
    t.sector,
    t.industry
FROM step4_analysis a
LEFT JOIN ticker_master t ON t.ticker = a.ticker
WHERE a.signal_date = ?
ORDER BY a.ticker, a.setup_type, a.analysis_id
"""

_ANALYSIS_COLS: Final[tuple[str, ...]] = (
    "analysis_id", "candidate_id", "setup_config_id",
    "ticker", "signal_date", "setup_type", "setup_score", "setup_passed",
    "setup_reasons", "setup_fail_reason", "entry_price_raw",
    "support_level", "resistance_level", "next_resistance_level",
    "atr_pct", "distance_to_ema20_pct", "distance_to_ema50_pct",
    "rvol", "earnings_days", "market_regime",
    "earnings_penalty", "macro_penalty", "explanation_json",
    "sector", "industry",
)

_SQL_READ_FEATURES: Final[str] = """
SELECT
    f.ticker,
    f.atr14,
    f.atr_pct,
    f.ema20,
    f.ema50,
    f.swing_high,
    f.swing_low,
    f.support_level,
    f.resistance_level,
    f.next_resistance_level,
    f.base_high,
    f.base_low,
    f.avg_dollar_volume_20d,
    f.distance_to_ema20_pct,
    f.distance_to_ema50_pct,
    f.roc20,
    f.ema20_slope,
    p.close_raw,
    p.close_adj
FROM daily_features_current f
LEFT JOIN daily_prices p ON p.ticker = f.ticker AND p.date = f.feature_date
WHERE f.feature_date = ?
"""

_FEATURE_COLS: Final[tuple[str, ...]] = (
    "ticker", "atr14", "atr_pct", "ema20", "ema50",
    "swing_high", "swing_low", "support_level", "resistance_level",
    "next_resistance_level", "base_high", "base_low",
    "avg_dollar_volume_20d", "distance_to_ema20_pct", "distance_to_ema50_pct",
    "roc20", "ema20_slope",
    "close_raw", "close_adj",
)

_SQL_READ_RISK_CONFIG: Final[str] = """
SELECT config_id, config_json
FROM risk_label_config
WHERE active_flag = TRUE
LIMIT 1
"""

_SQL_READ_SETUP_CONFIGS: Final[str] = """
SELECT config_id, setup_type, config_json
FROM setup_configs
WHERE active_flag = TRUE
"""

_SQL_INSERT: Final[str] = """
INSERT INTO step5_proposals (
    proposal_id, run_id, setup_config_id, ticker, signal_date,
    setup_type, setup_score, risk_score, risk_label, risk_reasons,
    disposition, entry_price_raw, stop_price_raw, target_price_raw,
    estimated_rr, target_is_structural,
    support_level, resistance_level, next_resistance_level,
    earnings_days, market_regime,
    proposal_score_raw, diversity_penalty, proposal_score_final,
    raw_rank, diversified_rank,
    in_raw_top_n, in_diversified_top_n, diversification_applied,
    selected_top_n, selected_flag,
    ai_reviewed, executed_flag,
    rejection_reason, setup_reasons, mechanical_explanation,
    sector_count_at_selection, industry_count_at_selection,
    created_at
) VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?,
    ?, ?, ?,
    ?, ?,
    ?, ?, ?,
    ?, ?,
    FALSE, FALSE,
    ?, ?, ?,
    ?, ?,
    CAST(now() AS TIMESTAMP)
)
"""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Step5ProposalEngine:
    """Step 5 engine: stop/target + risk labeling + disposition + ranking."""

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = db_manager if db_manager is not None else duckdb_manager

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def propose(
        self,
        signal_date: date,
        risk_label_config: dict | None = None,
        setup_configs: dict[str, dict] | None = None,
        db_role: str = DB_ROLE_PROD,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Run Step 5 for one signal_date."""
        run_id = run_id or str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        sig_iso = signal_date.isoformat()

        log.info("Step5 start db_role=%s signal_date=%s", db_role, sig_iso)

        if db_role not in ALLOWED_DB_ROLES:
            msg = f"Unsupported db_role {db_role!r}. Step 5 writes only to {list(ALLOWED_DB_ROLES)}."
            log.error("Step5 failed: %s", msg)
            return self._failed(run_id, db_role, sig_iso, msg)

        try:
            if risk_label_config is None:
                risk_label_config = self._load_risk_label_config(db_role)
            if setup_configs is None:
                setup_configs = self._load_setup_configs(db_role)
        except Exception as exc:
            msg = f"config load failed: {type(exc).__name__}: {exc}"
            log.error("Step5 failed: %s", msg)
            return self._failed(run_id, db_role, sig_iso, msg)

        try:
            parsed_cfg = _parse_risk_label_config(risk_label_config)
        except _ConfigError as exc:
            log.error("Step5 failed: bad risk_label_config: %s", exc)
            return self._failed(run_id, db_role, sig_iso, str(exc))

        try:
            analyses = self._read_analyses(db_role, signal_date)
            features_map = self._read_features(db_role, signal_date)
        except Exception as exc:
            msg = f"read failed: {type(exc).__name__}: {exc}"
            log.error("Step5 failed: %s", msg)
            return self._failed(run_id, db_role, sig_iso, msg)

        analyses_read = len(analyses)
        if analyses_read == 0:
            log.info("Step5: no step4 analyses for %s", sig_iso)
            return self._success(run_id, db_role, sig_iso, 0, [])

        rows = self._build_rows(
            analyses, features_map, setup_configs or {}, parsed_cfg, run_id, signal_date
        )

        try:
            self._write(db_role, rows)
        except Exception as exc:
            msg = f"write failed (rolled back): {type(exc).__name__}: {exc}"
            log.error("Step5 failed: %s", msg)
            return self._failed(run_id, db_role, sig_iso, msg, analyses_read=analyses_read)

        log.info("Step5 done analyses_read=%d proposals_written=%d", analyses_read, len(rows))
        return self._success(run_id, db_role, sig_iso, analyses_read, rows)

    # ------------------------------------------------------------------ #
    # DB reads
    # ------------------------------------------------------------------ #

    def _load_risk_label_config(self, db_role: str) -> dict:
        conn = self._db.connect(db_role)
        try:
            rows = conn.execute(_SQL_READ_RISK_CONFIG).fetchall()
        finally:
            conn.close()
        if not rows:
            raise _ConfigError("No active risk_label_config found in DB")
        _, cfg_json = rows[0]
        return json.loads(cfg_json) if isinstance(cfg_json, str) else cfg_json

    def _load_setup_configs(self, db_role: str) -> dict[str, dict]:
        conn = self._db.connect(db_role)
        try:
            rows = conn.execute(_SQL_READ_SETUP_CONFIGS).fetchall()
        finally:
            conn.close()
        result: dict[str, dict] = {}
        for config_id, setup_type, cfg_json in rows:
            parsed = json.loads(cfg_json) if isinstance(cfg_json, str) else cfg_json
            parsed.setdefault("config_id", config_id)
            parsed.setdefault("setup_type", setup_type)
            result[setup_type] = parsed
        return result

    def _read_analyses(self, db_role: str, signal_date: date) -> list[dict[str, Any]]:
        conn = self._db.connect(db_role)
        try:
            rows = conn.execute(_SQL_READ_ANALYSES, [signal_date.isoformat()]).fetchall()
        finally:
            conn.close()
        return [dict(zip(_ANALYSIS_COLS, r)) for r in rows]

    def _read_features(self, db_role: str, signal_date: date) -> dict[str, dict[str, Any]]:
        conn = self._db.connect(db_role)
        try:
            rows = conn.execute(_SQL_READ_FEATURES, [signal_date.isoformat()]).fetchall()
        finally:
            conn.close()
        result: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = dict(zip(_FEATURE_COLS, r))
            result[d["ticker"]] = d
        return result

    # ------------------------------------------------------------------ #
    # Compute phase
    # ------------------------------------------------------------------ #

    def _build_rows(
        self,
        analyses: list[dict[str, Any]],
        features_map: dict[str, dict[str, Any]],
        setup_configs: dict[str, dict],
        cfg: dict[str, Any],
        run_id: str,
        signal_date: date,
    ) -> list[dict[str, Any]]:
        top_n = cfg["top_n"]
        min_target_room_pct = cfg.get("min_target_room_pct", _DEFAULT_MIN_TARGET_ROOM_PCT)
        enriched: list[dict[str, Any]] = []

        for a in analyses:
            ticker = a["ticker"]
            setup_type = a.get("setup_type") or ""
            setup_passed = bool(a["setup_passed"])
            setup_score = _f(a["setup_score"]) or 0.0
            market_regime = a.get("market_regime")
            _ed = a.get("earnings_days")
            earnings_days: int | None = None
            if _ed is not None:
                try:
                    earnings_days = int(float(_ed))
                except (ValueError, TypeError):
                    pass

            sector = a["sector"] if a.get("sector") is not None else UNKNOWN_SECTOR
            industry = a["industry"] if a.get("industry") is not None else UNKNOWN_INDUSTRY

            feat = features_map.get(ticker, {})
            entry = _f(a.get("entry_price_raw")) or _f(feat.get("close_raw"))
            close_raw = _f(feat.get("close_raw")) or entry or 0.0
            close_adj = _f(feat.get("close_adj")) or close_raw or 0.0
            resistance_raw_feat = _raw_conv(_f(feat.get("resistance_level")), close_raw, close_adj)

            if not setup_passed:
                enriched.append(self._rejected_item(a, ticker, setup_type, setup_score,
                                                     entry, market_regime, earnings_days,
                                                     sector, industry))
                continue

            setup_cfg = setup_configs.get(setup_type, {})
            eff_entry = entry if (entry is not None and entry > 0) else close_raw

            stop, target, target_is_structural, stop_basis, target_basis, resistance_blocks =                 _compute_stop_target(
                    setup_type, feat, setup_cfg, eff_entry, close_raw, close_adj,
                    min_target_room_pct=min_target_room_pct,
                )
            estimated_rr = _compute_estimated_rr(eff_entry, stop, target)

            stop_distance_pct: float | None = None
            if eff_entry and stop is not None and eff_entry > 0:
                stop_distance_pct = (eff_entry - stop) / eff_entry

            # confirmation_score: rvol-based proxy
            rvol_val = _f(a.get("rvol")) or _f(feat.get("rvol20"))
            confirmation_score = _clamp((rvol_val or 0.0) * 50.0)

            # P1: pullback confirmation inputs from features
            roc20_val = _f(feat.get("roc20"))
            ema20_slope_val = _f(feat.get("ema20_slope"))

            risk_feat: dict[str, Any] = {
                **feat,
                "atr_pct": _f(a.get("atr_pct")) or _f(feat.get("atr_pct")),
                "distance_to_ema20_pct": _f(a.get("distance_to_ema20_pct")),
                "distance_to_ema50_pct": _f(a.get("distance_to_ema50_pct")),
            }

            atr_pct_val = _f(risk_feat.get("atr_pct"))
            stop_distance_atr: float | None = None
            if stop_distance_pct is not None and atr_pct_val is not None and atr_pct_val > 0:
                stop_distance_atr = stop_distance_pct / atr_pct_val
            dist_ema20 = _f(a.get("distance_to_ema20_pct"))

            risk_score, risk_label, risk_reasons = _compute_risk_score(
                feat=risk_feat, entry=eff_entry, stop=stop,
                estimated_rr=estimated_rr, market_regime=market_regime,
                setup_score=setup_score, earnings_days=earnings_days, cfg=cfg,
            )

            # Fix 2 + disposition: stop_distance_pct passed as hard-gate arg
            disposition, watchlist_reason = _assign_disposition(
                setup_passed=True, estimated_rr=estimated_rr,
                risk_label=risk_label, market_regime=market_regime,
                stop_distance_pct=stop_distance_pct, cfg=cfg,
            )

            # Fix 3 (corrected): if resistance blocks the trade, force WATCHLIST_ONLY.
            # Applied to any non-REJECTED candidate: nearby structural evidence proves
            # there is not enough room. Fixed-R must not be used to bypass it, and
            # this override takes precedence over RR/risk-label checks.
            if resistance_blocks and disposition != DISPOSITION_REJECTED:
                disposition = DISPOSITION_WATCHLIST
                watchlist_reason = WATCHLIST_TARGET_ROOM_INSUFFICIENT

            min_stop_atr = float(cfg.get("min_stop_distance_atr", _STOP_DISTANCE_ATR_MIN))
            final_trade_decision, ftd_reason_codes = _compute_final_trade_decision(
                setup_type=setup_type,
                disposition=disposition,
                close_raw=close_raw,
                resistance_raw=resistance_raw_feat,
                stop_distance_atr=stop_distance_atr,
                dist_ema20=dist_ema20,
                rvol_val=rvol_val,
                roc20_val=roc20_val,
                ema20_slope_val=ema20_slope_val,
                min_stop_distance_atr=min_stop_atr,
            )
            # P0: effective_disposition = authoritative action-readiness output
            effective_disposition = _FTD_TO_EFFECTIVE_DISP.get(
                final_trade_decision, DISPOSITION_WATCHLIST
            )
            effective_risk_label = (
                risk_label if final_trade_decision == FTD_BUY
                else constants.RISK_LABEL_HIGH
            )

            psc_raw = _proposal_score_raw(
                setup_score, estimated_rr, confirmation_score, market_regime,
                stop_distance_pct=stop_distance_pct,
            )

            # Fix 4: invalidation level = stop_price_raw; documented explicitly
            invalidation_level_raw = stop
            invalidation_reason = stop_basis if stop_basis else "atr_fallback"

            enriched.append({
                "setup_config_id": a["setup_config_id"],
                "ticker": ticker,
                "setup_type": setup_type,
                "setup_score": setup_score,
                "risk_score": risk_score,
                "risk_label": risk_label,
                "risk_reasons": _json(risk_reasons),
                "disposition": disposition,
                "entry_price_raw": eff_entry,
                "stop_price_raw": stop,
                "target_price_raw": target,
                "estimated_rr": estimated_rr,
                "target_is_structural": target_is_structural,
                "stop_distance_pct": stop_distance_pct,
                "support_level": _f(a.get("support_level")),
                "resistance_level": _f(a.get("resistance_level")),
                "next_resistance_level": _f(a.get("next_resistance_level")),
                "earnings_days": earnings_days,
                "market_regime": market_regime,
                "proposal_score_raw": psc_raw,
                "proposal_score_final": psc_raw,
                "setup_reasons": a.get("setup_reasons"),
                "sector": sector,
                "industry": industry,
                "rankable": disposition != DISPOSITION_REJECTED,
                "rejection_reason": watchlist_reason,
                "raw_rank": None,
                "in_raw_top_n": False,
                "diversified_rank": None,
                "sector_count_at_selection": None,
                "industry_count_at_selection": None,
                # Fix 4 fields (carried into mechanical_explanation)
                "invalidation_level_raw": invalidation_level_raw,
                "invalidation_reason": invalidation_reason,
                "stop_basis": stop_basis,
                "target_basis": target_basis,
                "resistance_blocks": resistance_blocks,
                # P0 trade readiness
                "final_trade_decision": final_trade_decision,
                "ftd_reason_codes": ftd_reason_codes,
                "effective_disposition": effective_disposition,
                "effective_risk_label": effective_risk_label,
            })

        # Dedupe multi-route
        ticker_best: dict[str, dict[str, Any]] = {}
        rejected_items: list[dict[str, Any]] = []
        for item in enriched:
            if not item["rankable"]:
                rejected_items.append(item)
                continue
            t = item["ticker"]
            if t not in ticker_best or item["proposal_score_raw"] > ticker_best[t]["proposal_score_raw"]:
                ticker_best[t] = item

        def _sort_key(x: dict) -> tuple:
            d = 0 if x["disposition"] == DISPOSITION_BUY else 1
            return (d, -x["proposal_score_raw"], -(x["estimated_rr"] or 0.0), x["ticker"])

        all_ranked = sorted(ticker_best.values(), key=_sort_key)

        for idx, item in enumerate(all_ranked, start=1):
            item["raw_rank"] = idx
            item["in_raw_top_n"] = idx <= top_n

        if cfg["hard_cap_enabled"]:
            self._apply_hard_cap(all_ranked, cfg)
        else:
            self._apply_soft_penalty(all_ranked, cfg)

        all_items = all_ranked + rejected_items
        rows: list[dict[str, Any]] = []
        for item in all_items:
            div_rank = item.get("diversified_rank")
            in_div = (
                item.get("rankable", False)
                and div_rank is not None
                and div_rank <= top_n
            )
            raw_sc = item["proposal_score_raw"]
            final_sc = item.get("proposal_score_final", raw_sc)

            # Fix 4: mechanical_explanation includes invalidation level and reason
            explanation = {
                "setup_type": item["setup_type"],
                "setup_score": item["setup_score"],
                "risk_score": item["risk_score"],
                "risk_label": item["risk_label"],
                "disposition": item["disposition"],
                "estimated_rr": item["estimated_rr"],
                "target_is_structural": item.get("target_is_structural"),
                "stop_basis": item.get("stop_basis"),
                "target_basis": item.get("target_basis"),
                "raw_rank": item.get("raw_rank"),
                "diversified_rank": div_rank,
                # Fix 4: explicit invalidation level
                "invalidation_level_raw": item.get("invalidation_level_raw"),
                "invalidation_reason": item.get("invalidation_reason"),
                "resistance_blocks": item.get("resistance_blocks", False),
                # P0: final trade decision (separate from setup quality)
                "final_trade_decision": item.get("final_trade_decision", FTD_REJECTED),
                "ftd_reason_codes": item.get("ftd_reason_codes", []),
                # P0: authoritative action-readiness fields (WAIT_* → WATCHLIST_ONLY)
                "effective_disposition": item.get("effective_disposition", DISPOSITION_WATCHLIST),
                "effective_risk_label": item.get("effective_risk_label", constants.RISK_LABEL_HIGH),
            }

            rows.append({
                "proposal_id": str(uuid.uuid4()),
                "run_id": run_id,
                "setup_config_id": item["setup_config_id"],
                "ticker": item["ticker"],
                "signal_date": signal_date,
                "setup_type": item["setup_type"],
                "setup_score": item["setup_score"],
                "risk_score": item["risk_score"],
                "risk_label": item["risk_label"],
                "risk_reasons": item["risk_reasons"],
                "disposition": item["disposition"],
                "entry_price_raw": item["entry_price_raw"],
                "stop_price_raw": item["stop_price_raw"],
                "target_price_raw": item["target_price_raw"],
                "estimated_rr": item["estimated_rr"],
                "target_is_structural": item.get("target_is_structural"),
                "support_level": item["support_level"],
                "resistance_level": item["resistance_level"],
                "next_resistance_level": item["next_resistance_level"],
                "earnings_days": item["earnings_days"],
                "market_regime": item["market_regime"],
                "proposal_score_raw": raw_sc,
                "diversity_penalty": raw_sc - final_sc,
                "proposal_score_final": final_sc,
                "raw_rank": item.get("raw_rank"),
                "diversified_rank": div_rank,
                "in_raw_top_n": bool(item.get("in_raw_top_n", False)),
                "in_diversified_top_n": bool(in_div),
                "diversification_applied": True,
                "selected_top_n": bool(item.get("in_raw_top_n", False) or in_div),
                "selected_flag": bool(in_div),
                "rejection_reason": item.get("rejection_reason"),
                "setup_reasons": (
                    item["setup_reasons"]
                    if isinstance(item["setup_reasons"], str)
                    else _json(item["setup_reasons"]) if item["setup_reasons"] is not None else None
                ),
                "mechanical_explanation": _json(explanation),
                "sector_count_at_selection": item.get("sector_count_at_selection"),
                "industry_count_at_selection": item.get("industry_count_at_selection"),
            })

        return rows

    @staticmethod
    def _rejected_item(
        a: dict, ticker: str, setup_type: str, setup_score: float,
        entry: float | None, market_regime: str | None,
        earnings_days: int | None, sector: str, industry: str,
    ) -> dict[str, Any]:
        return {
            "setup_config_id": a["setup_config_id"],
            "ticker": ticker,
            "setup_type": setup_type,
            "setup_score": setup_score,
            "risk_score": 100.0,
            "risk_label": constants.RISK_LABEL_HIGH,
            "risk_reasons": _json(["setup_failed"]),
            "disposition": DISPOSITION_REJECTED,
            "entry_price_raw": entry,
            "stop_price_raw": None,
            "target_price_raw": None,
            "estimated_rr": None,
            "target_is_structural": None,
            "stop_distance_pct": None,
            "support_level": _f(a.get("support_level")),
            "resistance_level": _f(a.get("resistance_level")),
            "next_resistance_level": _f(a.get("next_resistance_level")),
            "earnings_days": earnings_days,
            "market_regime": market_regime,
            "proposal_score_raw": 0.0,
            "proposal_score_final": 0.0,
            "setup_reasons": a.get("setup_reasons"),
            "sector": sector,
            "industry": industry,
            "rankable": False,
            "rejection_reason": a.get("setup_fail_reason") or "setup_failed",
            "raw_rank": None,
            "in_raw_top_n": False,
            "diversified_rank": None,
            "sector_count_at_selection": None,
            "industry_count_at_selection": None,
            "invalidation_level_raw": None,
            "invalidation_reason": None,
            "stop_basis": None,
            "target_basis": None,
            "resistance_blocks": False,
            "final_trade_decision": FTD_REJECTED,
            "ftd_reason_codes": [],
            "effective_disposition": DISPOSITION_REJECTED,
            "effective_risk_label": constants.RISK_LABEL_HIGH,
        }

    @staticmethod
    def _apply_hard_cap(ranked: list[dict], cfg: dict) -> None:
        max_s = cfg["max_sector_count"]
        max_i = cfg["max_industry_count"]
        sc: dict[str, int] = {}
        ic: dict[str, int] = {}
        nxt = 1
        for item in ranked:
            s = item["sector"]
            i = item["industry"]
            ps = sc.get(s, 0)
            pi = ic.get(i, 0)
            item["proposal_score_final"] = item["proposal_score_raw"]
            if pi >= max_i or ps >= max_s:
                item["diversified_rank"] = None
                item["rejection_reason"] = (
                    REJECT_INDUSTRY_CAP if pi >= max_i else REJECT_SECTOR_CAP
                )
                item["sector_count_at_selection"] = ps
                item["industry_count_at_selection"] = pi
            else:
                item["diversified_rank"] = nxt
                nxt += 1
                item["rejection_reason"] = item.get("rejection_reason")  # preserve watchlist reason
                sc[s] = ps + 1
                ic[i] = pi + 1
                item["sector_count_at_selection"] = sc[s]
                item["industry_count_at_selection"] = ic[i]

    @staticmethod
    def _apply_soft_penalty(ranked: list[dict], cfg: dict) -> None:
        sp = cfg["sector_penalty"]
        ip_ = cfg["industry_penalty"]
        ss: dict[str, int] = {}
        si: dict[str, int] = {}
        for item in ranked:
            s = item["sector"]
            i = item["industry"]
            ps = ss.get(s, 0)
            pi = si.get(i, 0)
            item["proposal_score_final"] = _clamp(
                item["proposal_score_raw"] * (sp ** ps) * (ip_ ** pi)
            )
            item["sector_count_at_selection"] = ps + 1
            item["industry_count_at_selection"] = pi + 1
            ss[s] = ps + 1
            si[i] = pi + 1
        order = sorted(range(len(ranked)), key=lambda j: (
            -ranked[j]["proposal_score_final"], ranked[j]["ticker"]
        ))
        for div_rank, pos in enumerate(order, start=1):
            ranked[pos]["diversified_rank"] = div_rank

    # ------------------------------------------------------------------ #
    # Write phase
    # ------------------------------------------------------------------ #

    def _write(self, db_role: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        conn = self._db.connect(db_role)
        try:
            conn.execute("BEGIN TRANSACTION")
            try:
                for row in rows:
                    conn.execute(
                        _SQL_INSERT,
                        [
                            row["proposal_id"], row["run_id"], row["setup_config_id"],
                            row["ticker"], row["signal_date"], row["setup_type"],
                            row["setup_score"], row["risk_score"], row["risk_label"],
                            row["risk_reasons"], row["disposition"],
                            row["entry_price_raw"], row["stop_price_raw"],
                            row["target_price_raw"], row["estimated_rr"],
                            row["target_is_structural"],
                            row["support_level"], row["resistance_level"],
                            row["next_resistance_level"],
                            row["earnings_days"], row["market_regime"],
                            row["proposal_score_raw"], row["diversity_penalty"],
                            row["proposal_score_final"],
                            row["raw_rank"], row["diversified_rank"],
                            row["in_raw_top_n"], row["in_diversified_top_n"],
                            row["diversification_applied"],
                            row["selected_top_n"], row["selected_flag"],
                            row["rejection_reason"], row["setup_reasons"],
                            row["mechanical_explanation"],
                            row["sector_count_at_selection"],
                            row["industry_count_at_selection"],
                        ],
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()
        return len(rows)

    # ------------------------------------------------------------------ #
    # Result builders
    # ------------------------------------------------------------------ #

    def _success(
        self, run_id: str, db_role: str, sig_iso: str,
        analyses_read: int, rows: list[dict],
    ) -> ServiceResult:
        written = len(rows)
        return ServiceResult(
            status=sr.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=written,
            metadata={
                "db_role": db_role, "signal_date": sig_iso, "run_id": run_id,
                "setup_config_id": "risk_label_config_v1",
                "analyses_read": analyses_read, "proposals_written": written,
                "raw_top_n_count": sum(1 for r in rows if r.get("in_raw_top_n")),
                "diversified_top_n_count": sum(1 for r in rows if r.get("in_diversified_top_n")),
                "hard_cap_rejections": sum(
                    1 for r in rows
                    if r.get("rejection_reason") in (REJECT_SECTOR_CAP, REJECT_INDUSTRY_CAP)
                ),
            },
        )

    def _failed(
        self, run_id: str, db_role: str, sig_iso: str, message: str,
        *, analyses_read: int = 0,
    ) -> ServiceResult:
        return ServiceResult(
            status=sr.STATUS_FAILED, run_id=run_id, rows_processed=0,
            errors=[message],
            metadata={
                "db_role": db_role, "signal_date": sig_iso, "run_id": run_id,
                "setup_config_id": "", "analyses_read": analyses_read,
                "proposals_written": 0, "raw_top_n_count": 0,
                "diversified_top_n_count": 0, "hard_cap_rejections": 0,
            },
        )


__all__ = [
    "Step5ProposalEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "UNKNOWN_SECTOR",
    "UNKNOWN_INDUSTRY",
    "REJECT_SECTOR_CAP",
    "REJECT_INDUSTRY_CAP",
    "WATCHLIST_STOP_TOO_WIDE",
    "WATCHLIST_TARGET_ROOM_INSUFFICIENT",
    "_compute_stop_target",
    "_compute_estimated_rr",
    "_compute_risk_score",
    "_assign_disposition",
    "_proposal_score_raw",
    "_rr_score",
    "_check_target_room",
]
