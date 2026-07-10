"""Module 14 — Setup Validators (setup-mode migration).

Implements four pure-Python setup validators:
    validate_breakout
    validate_pullback
    validate_trend_continuation
    validate_consolidation_base

Each validator accepts a feature+price dict and a setup config dict, applies
the hard checks and scoring rules from 01c_FORMULAS_AND_CONFIGS.md
(FORMULAS/62_Step4_Setup_Validation.md), and returns a SetupValidationResult.

Design principles:
- No DB access. No DuckDB imports. No print().
- All values extracted from a flat feature dict (keys match daily_features
  column names plus daily_prices columns for signal_date).
- Raw/adjusted conversion applied here per fix 6:
      level_raw = level_adj * (close_raw / close_adj)
- stop_price_raw / target_price_raw / estimated_rr are computed in Phase 5
  (M15). Phase 4 computes setup_passed, setup_score, and evidence.
- target_is_structural is determined here and reported in evidence_json so
  Phase 5 can honour it when computing the trade plan.
- RVOL rules per AD-22.23:
    breakout: hard/near-hard (rvol_is_hard=True in config)
    pullback: soft only — never hard reject
    trend_continuation: soft confirmation (rvol_is_hard=False)
    consolidation_base: not required (rvol_required=False)
- earnings / macro penalties are score adjustments only (Phase 4 computes
  them; Phase 5 re-reads them for disposition gates).
- market_regime propagated as-is (NULL = unknown; never defaulted).

Output columns written to step4_analysis (via the orchestrator in
step4_setup_validation_engine.py):
    analysis_id, candidate_id, run_id, setup_config_id, ticker, signal_date,
    setup_type, setup_score, setup_passed, setup_reasons, setup_fail_reason,
    entry_price_raw, stop_price_raw (NULL in P4), target_price_raw (NULL in P4),
    estimated_rr (NULL in P4), target_is_structural,
    stop_distance_pct (NULL in P4), support_level, resistance_level,
    next_resistance_level, atr_pct, distance_to_ema20_pct, distance_to_ema50_pct,
    rvol, earnings_days, market_regime, earnings_penalty, macro_penalty,
    explanation_json, created_at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final

from app.config import constants
from app.services.fundamentals import fundamentals_quality as fq

# ---------------------------------------------------------------------------
# Confidence enum values (from 01a_CORE_PRINCIPLES.md §SCHEMA/12_Enum_Values_Reference)
# ---------------------------------------------------------------------------
CONFIDENCE_HIGH: Final[str] = "high"
CONFIDENCE_MEDIUM: Final[str] = "medium"
CONFIDENCE_LOW: Final[str] = "low"

# Thresholds: setup_score >= HIGH_THRESHOLD → "high", >= MEDIUM_THRESHOLD → "medium", else "low"
# These align with the scoring scale (0–100) and min_setup_score defaults (55).
# P2.1 [HC->CFG]: promoted to risk_label_config.scoring.confidence. These
# module constants are the single source of default truth — config overrides
# them, but absent config reproduces exactly this behavior.
_CONFIDENCE_HIGH_THRESHOLD: Final[float] = 75.0
_CONFIDENCE_MEDIUM_THRESHOLD: Final[float] = 50.0


def _derive_confidence(
    setup_score: float,
    high_threshold: float = _CONFIDENCE_HIGH_THRESHOLD,
    medium_threshold: float = _CONFIDENCE_MEDIUM_THRESHOLD,
) -> str:
    """Map setup_score (0–100) to a confidence label.

    Thresholds (defaults, overridable via risk_label_config.scoring.confidence):
        score >= 75  → "high"
        score >= 50  → "medium"
        score <  50  → "low"

    Rationale: min_setup_score is 55 in all seeded configs, so a passed
    setup always has confidence "medium" or "high". A failed setup may
    have confidence "low" or "medium" (useful for diagnostics).
    """
    if setup_score >= high_threshold:
        return CONFIDENCE_HIGH
    if setup_score >= medium_threshold:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _resolve_confidence_thresholds(risk_cfg: dict[str, Any] | None) -> tuple[float, float]:
    """(high, medium) confidence thresholds from risk_label_config.scoring.

    Falls back to the exact module-level literals when the block or a key is
    absent, so behavior is byte-identical without a seeded scoring block.
    """
    scoring = (risk_cfg or {}).get("scoring") or {}
    conf = scoring.get("confidence") or {}
    return (
        float(conf.get("high_threshold", _CONFIDENCE_HIGH_THRESHOLD)),
        float(conf.get("medium_threshold", _CONFIDENCE_MEDIUM_THRESHOLD)),
    )


def _resolve_valuation_band_quality(risk_cfg: dict[str, Any] | None) -> dict[str, float]:
    """Valuation-band quality map from risk_label_config.scoring.

    Falls back to the exact module-level ``_VALUATION_BAND_QUALITY`` when the
    block is absent, so behavior is byte-identical without a seeded scoring
    block.
    """
    scoring = (risk_cfg or {}).get("scoring") or {}
    cfg_map = scoring.get("valuation_band_quality")
    if not isinstance(cfg_map, dict) or not cfg_map:
        return _VALUATION_BAND_QUALITY
    return {str(k): float(v) for k, v in cfg_map.items()}


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class SetupValidationResult:
    """Result of one setup validator call.

    Attributes
    ----------
    ticker: str
    signal_date: str  ISO-8601
    setup_type: str   one of ALLOWED_SETUP_TYPES
    setup_config_id: str
    setup_passed: bool
    setup_score: float  0–100 (may include penalty adjustment)
    confidence: str   explicit confidence label — "high" | "medium" | "low"
                      derived from setup_score via _derive_confidence().
                      Always set; never implicit.
                      Relation to setup_score:
                        score >= 75 → "high"
                        score >= 50 → "medium"
                        score <  50 → "low"
    pass_fail_reasons: list[str]   human-readable reason labels
    setup_fail_reason: str | None  first hard-fail label (None if passed)
    evidence_json: dict  all intermediate values used in validation
    feature_version: str  e.g. "features_v03"
    # Phase 5 placeholders (None in Phase 4)
    entry_price_raw: float | None
    support_level_raw: float | None
    resistance_level_raw: float | None
    next_resistance_level_raw: float | None
    atr_pct: float | None
    distance_to_ema20_pct: float | None
    distance_to_ema50_pct: float | None
    rvol: float | None
    earnings_days: int | None
    market_regime: str | None
    earnings_penalty: float
    macro_penalty: float
    target_is_structural: bool | None  True=structural, False=fixed-R, None=not evaluated
    """

    ticker: str
    signal_date: str
    setup_type: str
    setup_config_id: str
    setup_passed: bool
    setup_score: float
    confidence: str = CONFIDENCE_LOW  # always set explicitly by each validator
    pass_fail_reasons: list[str] = field(default_factory=list)
    setup_fail_reason: str | None = None
    evidence_json: dict[str, Any] = field(default_factory=dict)
    feature_version: str = constants.FEATURE_SCHEMA_VERSION
    # Price-level outputs (raw-converted; used by Phase 5)
    entry_price_raw: float | None = None
    support_level_raw: float | None = None
    resistance_level_raw: float | None = None
    next_resistance_level_raw: float | None = None
    atr_pct: float | None = None
    distance_to_ema20_pct: float | None = None
    distance_to_ema50_pct: float | None = None
    rvol: float | None = None
    earnings_days: int | None = None
    market_regime: str | None = None
    earnings_penalty: float = 0.0
    macro_penalty: float = 0.0
    target_is_structural: bool | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _f(v: Any) -> float | None:
    """Coerce to float or None; NaN → None."""
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(fv) else fv


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _raw_conv(level_adj: float | None, close_raw: float, close_adj: float) -> float | None:
    """Convert an adjusted structural level to raw price units.

    level_raw = level_adj * (close_raw / close_adj)
    Returns None if any input is None or close_adj <= 0.
    """
    if level_adj is None or close_adj is None or close_adj <= 0 or close_raw is None:
        return None
    return level_adj * (close_raw / close_adj)


def _score_norm(value: float | None, ideal: float, max_deviation: float) -> float:
    """Return 0–100 score: 100 when value==ideal, 0 when |value-ideal|>=max_deviation."""
    if value is None:
        return 0.0
    return _clamp(100.0 * (1.0 - abs(value - ideal) / max_deviation))


def _compute_penalties(
    feat: dict[str, Any],
    earnings_cfg: dict[str, Any],
    macro_cfg: dict[str, Any],
) -> tuple[float, float]:
    """Compute earnings and macro score penalties (negative floats or 0).

    Returns (earnings_penalty, macro_penalty).
    """
    avoid_bd = int(earnings_cfg.get("avoid_within_bd", 5))
    penalty_max = float(earnings_cfg.get("penalty_points_max", -15))

    days_to_earnings = _f(feat.get("days_to_earnings_bd"))
    if days_to_earnings is not None and 0 < days_to_earnings <= avoid_bd:
        frac = 1.0 - days_to_earnings / avoid_bd
        earnings_penalty = penalty_max * frac
    else:
        earnings_penalty = 0.0

    macro_flag = feat.get("macro_event_risk_flag")
    macro_enabled = macro_cfg.get("enabled", True)
    if macro_enabled and macro_flag:
        macro_penalty = float(macro_cfg.get("penalty_points", -10))
    else:
        macro_penalty = 0.0

    return earnings_penalty, macro_penalty


def _resolve_earnings_macro_cfg(
    setup_config: dict[str, Any],
    risk_cfg: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefer the shared earnings/macro block on the active risk_label_config;
    fall back to this setup_config's own copy when absent.

    CODER_NOTE v3 item 6 — promotes the earnings/macro penalty config (today
    duplicated identically across every setup_config via _EARNINGS_BLOCK/
    _MACRO_BLOCK) to a single shared source on risk_label_config, without a
    behavior change: risk_label_config_v1 (currently active in every prod/debug
    DB) carries no "earnings"/"macro_event_risk" keys, so this always falls back
    to the setup_config's own copy today. Only once a version carrying the
    shared block (e.g. risk_label_config_v2) is explicitly activated does the
    shared block take over — and its values are identical to the per-setup
    copies by construction, so that activation is also behavior-preserving.
    """
    risk_cfg = risk_cfg or {}
    earnings_cfg = risk_cfg.get("earnings") or setup_config.get("earnings", {})
    macro_cfg = risk_cfg.get("macro_event_risk") or setup_config.get("macro_event_risk", {})
    return earnings_cfg, macro_cfg


# Altman zones / valuation-band map now live in the shared quality module so
# M14 and Step 5 cannot drift apart. Re-exported under their original private
# names because callers (and tests) already reference them here.
_ALTMAN_SAFE_ZONE: Final[float] = fq.ALTMAN_SAFE_ZONE
_ALTMAN_DISTRESS_ZONE: Final[float] = fq.ALTMAN_DISTRESS_ZONE
_VALUATION_BAND_QUALITY: Final[dict[str, float]] = fq.VALUATION_BAND_QUALITY


def _count_fundamentals_fields(
    feat: dict[str, Any], valuation_band_quality: dict[str, float]
) -> int:
    """How many of the 5 fundamentals fields actually contributed to the mean."""
    present = 0
    for key in ("piotroski_f_score", "altman_z_score", "eps_growth_trend", "leverage_ratio"):
        if feat.get(key) is not None:
            present += 1
    if feat.get("valuation_band") in valuation_band_quality:
        present += 1
    return present


def _compute_fundamentals_adjustment(
    feat: dict[str, Any],
    fundamentals_cfg: dict[str, Any],
    valuation_band_quality: dict[str, float] = _VALUATION_BAND_QUALITY,
) -> tuple[float, dict[str, Any]]:
    """Optional, config-weighted soft score adjustment from Phase 4 fundamentals.

    Never a hard gate (mirrors the RVOL precedent, AD-22.23: a signal that is
    not universally required stays advisory/scoring-only, never a pass/fail
    check). Disabled by default (``fundamentals_cfg`` empty or
    ``enabled=False``) -- every existing setup_config that doesn't have a
    ``fundamentals`` block gets an adjustment of exactly ``0.0``, so this is
    byte-identical to pre-Phase-4 behavior unless a setup_config opts in.

    Returns ``(adjustment, evidence)`` where ``adjustment`` is added directly
    into ``penalized_score`` (same additive slot as ``earnings_pen`` /
    ``macro_pen``) and ``evidence`` documents which of the 5 real
    fundamentals fields contributed (absent/None fields are simply excluded
    from the average, not treated as a penalty -- a ticker with no
    fundamentals coverage yet gets adjustment 0.0, not a downgrade).

    Enabling this while Step 5's own fundamentals term is active would score the
    same five fields twice (Step 5 weights ``setup_score`` *and* adds its own
    term). Step 5 skips its term for any setup_type whose config sets
    ``enabled=True``, and ``ConfigService.validate_setup_config`` rejects the
    combination outright at authoring time.
    """
    enabled = bool(fundamentals_cfg.get("enabled", False))
    weight = float(fundamentals_cfg.get("weight", 0.0))
    if not enabled or weight == 0.0:
        return 0.0, {"enabled": False}

    avg_quality = fq.compute_fundamentals_quality(feat, valuation_band_quality)
    if avg_quality is None:
        return 0.0, {"enabled": True, "fields_present": 0}

    # Centered at 50 (neutral): above-average fundamentals add points,
    # below-average subtract -- never enough alone to force a gate.
    adjustment = weight * (avg_quality - 50.0) / 50.0
    evidence = {
        "enabled": True,
        "fields_present": _count_fundamentals_fields(feat, valuation_band_quality),
        "avg_quality": avg_quality,
        "adjustment": adjustment,
    }
    return adjustment, evidence


def _apply_weights(components: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted sum of component scores (0–100 each). Returns 0–100."""
    total_w = 0.0
    total_score = 0.0
    for key, w in weights.items():
        score = components.get(key, 0.0)
        total_score += w * score
        total_w += w
    if total_w <= 0:
        return 0.0
    # Normalise in case weights don't sum exactly to 1.0
    return _clamp(total_score / total_w * 100.0 if total_w != 1.0 else total_score)


# ---------------------------------------------------------------------------
# BREAKOUT validator
# ---------------------------------------------------------------------------

def validate_breakout(
    feat: dict[str, Any],
    setup_config: dict[str, Any],
    risk_cfg: dict[str, Any] | None = None,
) -> SetupValidationResult:
    """Validate a breakout setup.

    Hard checks:
    - resistance_level exists
    - breakout_proximity in [breakout_prox_min, breakout_prox_max]
    - range_duration >= min_base_duration
    - RVOL hard gate when rvol_is_hard=True: rvol20 >= min_rvol_breakout
    - (stop_distance_pct check deferred to Phase 5 when stop is computed)

    Scoring components (weights from config):
    - resistance_clarity: resistance_level exists and is finite
    - breakout_confirmation: breakout_proximity score
    - volume_expansion: rvol20 vs threshold
    - base_quality: range_duration and range_tightness_score
    - target_room: next_resistance_level exists (structural target available)
    """
    cfg_id = setup_config.get("config_id", "")
    ticker = str(feat.get("ticker", ""))
    signal_date = str(feat.get("signal_date", ""))
    val = setup_config.get("validation", {})
    weights = setup_config.get("scoring_weights", {})

    prox_min: float = float(val.get("breakout_prox_min", -1.0))
    prox_max: float = float(val.get("breakout_prox_max", 0.5))
    min_base_dur: int = int(val.get("min_base_duration", 10))
    min_rvol: float = float(val.get("min_rvol_breakout", 1.5))
    rvol_is_hard: bool = bool(val.get("rvol_is_hard", True))
    min_setup_score: float = float(val.get("min_setup_score", 55))
    min_atr_stop_floor: float = float(val.get("min_atr_stop_floor_multiple", 0.5))

    # Feature extraction
    close_raw = _f(feat.get("close_raw"))
    close_adj = _f(feat.get("close_adj"))
    high_raw = _f(feat.get("high_raw"))
    low_raw = _f(feat.get("low_raw"))
    breakout_proximity = _f(feat.get("breakout_proximity"))
    range_duration = feat.get("range_duration")
    range_duration_val = int(range_duration) if range_duration is not None else None
    range_tightness_score = _f(feat.get("range_tightness_score"))
    rvol20 = _f(feat.get("rvol20"))
    volume_expansion_score = _f(feat.get("volume_expansion_score"))
    resistance_adj = _f(feat.get("resistance_level"))
    next_resistance_adj = _f(feat.get("next_resistance_level"))
    support_adj = _f(feat.get("support_level"))
    atr_pct = _f(feat.get("atr_pct"))
    dist_ema20 = _f(feat.get("distance_to_ema20_pct"))
    dist_ema50 = _f(feat.get("distance_to_ema50_pct"))
    earnings_days = feat.get("days_to_earnings_bd")
    market_regime = feat.get("market_regime")

    # Raw conversion
    resistance_raw = _raw_conv(resistance_adj, close_raw, close_adj) if close_raw and close_adj else None
    next_resistance_raw = _raw_conv(next_resistance_adj, close_raw, close_adj) if close_raw and close_adj else None
    support_raw = _raw_conv(support_adj, close_raw, close_adj) if close_raw and close_adj else None

    # Entry proxy
    entry_raw = close_raw

    # P0-1: Early return when resistance is absent — prevents phantom target/score
    # from flowing through to evidence_json and step4_analysis output.
    if resistance_adj is None or resistance_adj <= 0:
        return SetupValidationResult(
            ticker=ticker,
            signal_date=signal_date,
            setup_type=constants.SETUP_BREAKOUT,
            setup_config_id=cfg_id,
            setup_passed=False,
            setup_score=0.0,
            confidence=CONFIDENCE_LOW,
            pass_fail_reasons=["no_resistance_level"],
            setup_fail_reason="no_resistance_level",
            evidence_json={
                "hard_fails": ["no_resistance_level"],
                "resistance_adj": resistance_adj,
                "resistance_blocks": False,
            },
            feature_version=constants.FEATURE_SCHEMA_VERSION,
            entry_price_raw=entry_raw,
            support_level_raw=support_raw,
            atr_pct=atr_pct,
            distance_to_ema20_pct=dist_ema20,
            distance_to_ema50_pct=dist_ema50,
            rvol=rvol20,
            earnings_days=int(earnings_days) if earnings_days is not None else None,
            market_regime=market_regime,
            target_is_structural=None,
        )

    # Penalties
    earnings_cfg, macro_cfg = _resolve_earnings_macro_cfg(setup_config, risk_cfg)
    # P2.1 [HC->CFG]: confidence thresholds + valuation-band map, resolved once
    # from the shared risk_label_config.scoring block (literal defaults if absent).
    conf_high, conf_medium = _resolve_confidence_thresholds(risk_cfg)
    valuation_band_quality = _resolve_valuation_band_quality(risk_cfg)
    earnings_pen, macro_pen = _compute_penalties(feat, earnings_cfg, macro_cfg)
    fundamentals_adj, fundamentals_evidence = _compute_fundamentals_adjustment(
        feat, setup_config.get("fundamentals", {}), valuation_band_quality
    )

    # --- Hard checks ---
    hard_fails: list[str] = []

    # 1. breakout_proximity in range
    if breakout_proximity is None:
        hard_fails.append("missing_breakout_proximity")
    elif not (prox_min <= breakout_proximity <= prox_max):
        hard_fails.append(
            f"breakout_proximity_out_of_range({breakout_proximity:.3f} not in "
            f"[{prox_min},{prox_max}])"
        )

    # 2. base duration
    if range_duration_val is None:
        hard_fails.append("missing_range_duration")
    elif range_duration_val < min_base_dur:
        hard_fails.append(f"range_duration_too_short({range_duration_val}<{min_base_dur})")

    # 3. RVOL hard gate
    if rvol_is_hard:
        if rvol20 is None:
            hard_fails.append("missing_rvol")
        elif rvol20 < min_rvol:
            hard_fails.append(f"rvol_below_hard_threshold({rvol20:.2f}<{min_rvol})")

    # 4. Stop ≥ 0.5 ATR below entry (P1-1)
    # Stop estimated as entry minus support (below-base stop placement)
    if (
        atr_pct is not None and atr_pct > 0
        and support_raw is not None
        and entry_raw is not None and entry_raw > 0
    ):
        stop_distance_pct = (entry_raw - support_raw) / entry_raw
        stop_distance_atr = stop_distance_pct / atr_pct
        if stop_distance_atr < min_atr_stop_floor:
            hard_fails.append(
                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
            )

    # 5. Optional earnings hard-block (P1.2, opt-in — default False, zero
    # behavior change until explicitly enabled on a setup_config version)
    if bool(earnings_cfg.get("earnings_hard_block", False)):
        avoid_bd = int(earnings_cfg.get("avoid_within_bd", 5))
        days_to_earnings = _f(earnings_days)
        if days_to_earnings is not None and 0 < days_to_earnings <= avoid_bd:
            hard_fails.append(
                f"earnings_too_close({days_to_earnings:.0f}bd<={avoid_bd}bd)"
            )

    setup_passed_hard = len(hard_fails) == 0

    # --- Soft checks / scoring ---
    components: dict[str, float] = {}

    # resistance_clarity: 100 if exists, scaled by proximity tightness
    if resistance_adj is not None:
        components["resistance_clarity"] = 80.0  # structural level present
    else:
        components["resistance_clarity"] = 0.0

    # breakout_confirmation: score based on proximity to breakout area
    if breakout_proximity is not None:
        # ideal = 0 (right at resistance), max deviation = max of prox range
        dev_range = max(abs(prox_min), abs(prox_max), 1.0)
        bp_score = _clamp(100.0 * (1.0 - abs(breakout_proximity) / dev_range))
        # bonus for close strength
        close_strength = 0.0
        if high_raw and low_raw and close_raw and (high_raw - low_raw) > 0:
            close_strength = (close_raw - low_raw) / (high_raw - low_raw)
        strength_score = _clamp(close_strength * 100.0)
        components["breakout_confirmation"] = 0.6 * bp_score + 0.4 * strength_score
    else:
        components["breakout_confirmation"] = 0.0

    # volume_expansion: rvol vs threshold
    if rvol20 is not None:
        vol_score = _clamp(100.0 * (rvol20 / max(min_rvol * 1.5, 1.0)))
    elif volume_expansion_score is not None:
        vol_score = volume_expansion_score
    else:
        vol_score = 0.0
    components["volume_expansion"] = vol_score

    # base_quality: range_duration + tightness
    dur_score = 0.0
    if range_duration_val is not None:
        dur_score = _clamp(100.0 * min(range_duration_val / 30.0, 1.0))
    tight_score = range_tightness_score if range_tightness_score is not None else 0.0
    components["base_quality"] = 0.5 * dur_score + 0.5 * tight_score

    # target_room: structural target (next_resistance) exists
    # resistance_blocks=True when resistance sits between entry and any uncapped target (P2-1)
    target_is_structural: bool | None = None
    resistance_blocks: bool = False
    if next_resistance_raw is not None and entry_raw is not None and next_resistance_raw > entry_raw:
        components["target_room"] = 80.0
        target_is_structural = True
        # Resistance blocks if it sits below the next structural level
        if resistance_raw is not None and resistance_raw > entry_raw and resistance_raw < next_resistance_raw:
            resistance_blocks = True
    elif resistance_raw is not None and entry_raw is not None and resistance_raw > entry_raw:
        # Resistance IS the ceiling — structural target but resistance blocks further upside
        components["target_room"] = 40.0
        target_is_structural = True
        resistance_blocks = True
    else:
        components["target_room"] = 0.0
        target_is_structural = False  # would need fixed-R fallback

    raw_score = _apply_weights(components, weights)
    penalized_score = _clamp(raw_score + earnings_pen + macro_pen + fundamentals_adj)

    setup_passed = setup_passed_hard and penalized_score >= min_setup_score

    reasons: list[str] = list(hard_fails)
    if not hard_fails:
        if penalized_score < min_setup_score:
            reasons.append(f"score_below_threshold({penalized_score:.1f}<{min_setup_score})")
        else:
            reasons.append("passed")
    fail_reason = hard_fails[0] if hard_fails else (
        None if setup_passed else f"score_below_threshold({penalized_score:.1f}<{min_setup_score})"
    )

    evidence: dict[str, Any] = {
        "breakout_proximity": breakout_proximity,
        "range_duration": range_duration_val,
        "range_tightness_score": range_tightness_score,
        "rvol20": rvol20,
        "volume_expansion_score": volume_expansion_score,
        "resistance_adj": resistance_adj,
        "next_resistance_adj": next_resistance_adj,
        "resistance_raw": resistance_raw,
        "next_resistance_raw": next_resistance_raw,
        "close_raw": close_raw,
        "close_adj": close_adj,
        "close_strength": (close_raw - low_raw) / (high_raw - low_raw) if (
            close_raw and high_raw and low_raw and (high_raw - low_raw) > 0
        ) else None,
        "hard_fails": hard_fails,
        "component_scores": components,
        "raw_score": raw_score,
        "earnings_penalty": earnings_pen,
        "macro_penalty": macro_pen,
        "fundamentals_adjustment": fundamentals_adj,
        "fundamentals_evidence": fundamentals_evidence,
        "penalized_score": penalized_score,
        "target_is_structural": target_is_structural,
        "resistance_blocks": resistance_blocks,
        "confidence": _derive_confidence(penalized_score, conf_high, conf_medium),
    }

    return SetupValidationResult(
        ticker=ticker,
        signal_date=signal_date,
        setup_type=constants.SETUP_BREAKOUT,
        setup_config_id=cfg_id,
        setup_passed=setup_passed,
        setup_score=penalized_score,
        confidence=_derive_confidence(penalized_score, conf_high, conf_medium),
        pass_fail_reasons=reasons,
        setup_fail_reason=fail_reason,
        evidence_json=evidence,
        feature_version=constants.FEATURE_SCHEMA_VERSION,
        entry_price_raw=entry_raw,
        support_level_raw=support_raw,
        resistance_level_raw=resistance_raw,
        next_resistance_level_raw=next_resistance_raw,
        atr_pct=atr_pct,
        distance_to_ema20_pct=dist_ema20,
        distance_to_ema50_pct=dist_ema50,
        rvol=rvol20,
        earnings_days=int(earnings_days) if earnings_days is not None else None,
        market_regime=market_regime,
        earnings_penalty=earnings_pen,
        macro_penalty=macro_pen,
        target_is_structural=target_is_structural,
    )


# ---------------------------------------------------------------------------
# PULLBACK validator
# ---------------------------------------------------------------------------

def validate_pullback(
    feat: dict[str, Any],
    setup_config: dict[str, Any],
    risk_cfg: dict[str, Any] | None = None,
) -> SetupValidationResult:
    """Validate a pullback setup.

    Hard checks:
    - close_adj > ema200
    - ema20 > ema50
    - pullback_depth_pct <= max_pullback_depth
    - close_raw >= support_raw * (1 - support_break_tol)
    - RVOL: NEVER a hard reject (AD-22.23); soft penalty only

    Scoring components:
    - uptrend_intact: ema alignment + ema200 relationship
    - support_ema_hold: price vs support / ema20 proximity
    - pullback_depth: controlled depth score
    - trend_structure: higher-low structure (approximated from swing features)
    - rr: target room evidence (structural)
    """
    cfg_id = setup_config.get("config_id", "")
    ticker = str(feat.get("ticker", ""))
    signal_date = str(feat.get("signal_date", ""))
    val = setup_config.get("validation", {})
    weights = setup_config.get("scoring_weights", {})

    pull_band: float = float(val.get("pull_band", 0.04))
    max_pullback_depth: float = float(val.get("max_pullback_depth", 0.12))
    support_break_tol: float = float(val.get("support_break_tol", 0.02))
    rvol_bonus_threshold: float = float(val.get("rvol_bonus_threshold", 1.3))
    min_setup_score: float = float(val.get("min_setup_score", 55))
    rebound_required: bool = bool(val.get("rebound_required", True))
    min_rebound_slope: float = float(val.get("min_rebound_slope", 0.002))
    min_atr_stop_floor: float = float(val.get("min_atr_stop_floor_multiple", 0.5))
    # rvol_is_hard MUST be False for pullback (AD-22.23)
    rvol_is_hard: bool = bool(val.get("rvol_is_hard", False))
    if rvol_is_hard:
        # Override per architecture rule — log in evidence
        rvol_is_hard = False

    # Feature extraction
    close_raw = _f(feat.get("close_raw"))
    close_adj = _f(feat.get("close_adj"))
    open_raw = _f(feat.get("open_raw"))
    ema20 = _f(feat.get("ema20"))
    ema50 = _f(feat.get("ema50"))
    ema200 = _f(feat.get("ema200"))
    ema20_slope = _f(feat.get("ema20_slope"))
    roc20 = _f(feat.get("roc20"))
    pullback_depth_pct = _f(feat.get("pullback_depth_pct"))
    pullback_from_high = _f(feat.get("pullback_from_recent_high_pct"))
    support_adj = _f(feat.get("support_level"))
    resistance_adj = _f(feat.get("resistance_level"))
    next_resistance_adj = _f(feat.get("next_resistance_level"))
    swing_low_adj = _f(feat.get("swing_low"))
    swing_high_adj = _f(feat.get("swing_high"))
    rvol20 = _f(feat.get("rvol20"))
    dist_ema20 = _f(feat.get("distance_to_ema20_pct"))
    dist_ema50 = _f(feat.get("distance_to_ema50_pct"))
    atr_pct = _f(feat.get("atr_pct"))
    earnings_days = feat.get("days_to_earnings_bd")
    market_regime = feat.get("market_regime")
    ema_alignment_score = _f(feat.get("ema_alignment_score"))

    # Raw conversion
    support_raw = _raw_conv(support_adj, close_raw, close_adj) if close_raw and close_adj else None
    resistance_raw = _raw_conv(resistance_adj, close_raw, close_adj) if close_raw and close_adj else None
    next_resistance_raw = _raw_conv(next_resistance_adj, close_raw, close_adj) if close_raw and close_adj else None
    swing_low_raw = _raw_conv(swing_low_adj, close_raw, close_adj) if close_raw and close_adj else None
    swing_high_raw = _raw_conv(swing_high_adj, close_raw, close_adj) if close_raw and close_adj else None

    # Guard: swing_low above current price cannot serve as a valid stop anchor
    if swing_low_adj is not None and close_adj is not None and swing_low_adj >= close_adj:
        swing_low_adj = None
        swing_low_raw = None

    entry_raw = close_raw

    earnings_cfg, macro_cfg = _resolve_earnings_macro_cfg(setup_config, risk_cfg)
    # P2.1 [HC->CFG]: confidence thresholds + valuation-band map, resolved once
    # from the shared risk_label_config.scoring block (literal defaults if absent).
    conf_high, conf_medium = _resolve_confidence_thresholds(risk_cfg)
    valuation_band_quality = _resolve_valuation_band_quality(risk_cfg)
    earnings_pen, macro_pen = _compute_penalties(feat, earnings_cfg, macro_cfg)
    fundamentals_adj, fundamentals_evidence = _compute_fundamentals_adjustment(
        feat, setup_config.get("fundamentals", {}), valuation_band_quality
    )

    # --- Hard checks ---
    hard_fails: list[str] = []

    # 1. close_adj > ema200 (uptrend context)
    if close_adj is None or ema200 is None:
        hard_fails.append("missing_close_adj_or_ema200")
    elif close_adj <= ema200:
        hard_fails.append(f"price_below_ema200({close_adj:.2f}<={ema200:.2f})")

    # 2. ema20 > ema50 (short-term trend up)
    if ema20 is None or ema50 is None:
        hard_fails.append("missing_ema20_or_ema50")
    elif ema20 <= ema50:
        hard_fails.append(f"ema20_not_above_ema50({ema20:.2f}<={ema50:.2f})")

    # 3. pullback_depth controlled
    if pullback_depth_pct is None:
        hard_fails.append("missing_pullback_depth_pct")
    elif pullback_depth_pct > max_pullback_depth:
        hard_fails.append(
            f"pullback_too_deep({pullback_depth_pct:.3f}>{max_pullback_depth})"
        )

    # 4. support not broken
    if support_raw is not None and close_raw is not None:
        lower_bound = support_raw * (1.0 - support_break_tol)
        if close_raw < lower_bound:
            hard_fails.append(
                f"support_broken(close={close_raw:.2f}<support_tol={lower_bound:.2f})"
            )
    # (If support_raw is None, skip — penalty via soft score only)

    # 5. Rebound confirmation required (P1-2)
    # At least one of: EMA20 sloping up, ROC20 positive, or bullish day candle
    if rebound_required:
        rebound_signal = (
            (ema20_slope is not None and ema20_slope >= min_rebound_slope)
            or (roc20 is not None and roc20 > 0)
            or (close_raw is not None and open_raw is not None and close_raw > open_raw)
        )
        if not rebound_signal:
            hard_fails.append("pullback_no_rebound_confirmation")

    # 6. Stop ≥ 0.5 ATR below entry (P1-1)
    # Stop estimated as entry minus support (standard pullback stop placement)
    if (
        atr_pct is not None and atr_pct > 0
        and support_raw is not None
        and entry_raw is not None and entry_raw > 0
    ):
        stop_distance_pct = (entry_raw - support_raw) / entry_raw
        stop_distance_atr = stop_distance_pct / atr_pct
        if stop_distance_atr < min_atr_stop_floor:
            hard_fails.append(
                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
            )

    # Note: RVOL is NOT a hard check for pullback (AD-22.23)

    # 7. Optional earnings hard-block (P1.2, opt-in — default False, zero
    # behavior change until explicitly enabled on a setup_config version)
    if bool(earnings_cfg.get("earnings_hard_block", False)):
        avoid_bd = int(earnings_cfg.get("avoid_within_bd", 5))
        days_to_earnings = _f(earnings_days)
        if days_to_earnings is not None and 0 < days_to_earnings <= avoid_bd:
            hard_fails.append(
                f"earnings_too_close({days_to_earnings:.0f}bd<={avoid_bd}bd)"
            )

    setup_passed_hard = len(hard_fails) == 0

    # --- Soft scoring ---
    components: dict[str, float] = {}

    # uptrend_intact: EMA alignment quality
    if ema_alignment_score is not None:
        components["uptrend_intact"] = _clamp(ema_alignment_score)
    elif ema20 and ema50 and ema200 and close_adj:
        if close_adj > ema200 and ema20 > ema50:
            components["uptrend_intact"] = 80.0
        elif close_adj > ema200:
            components["uptrend_intact"] = 50.0
        else:
            components["uptrend_intact"] = 0.0
    else:
        components["uptrend_intact"] = 0.0

    # support_ema_hold: closeness to EMA20 or support
    ema20_proximity = 0.0
    if dist_ema20 is not None:
        # closer to 0 is better; within pull_band = good
        ema20_proximity = _clamp(100.0 * (1.0 - min(abs(dist_ema20) / max(pull_band, 0.001), 1.0)))
    support_proximity = 0.0
    if support_raw is not None and close_raw is not None and support_raw > 0:
        gap = (close_raw - support_raw) / support_raw
        support_proximity = _clamp(100.0 * (1.0 - min(abs(gap) / 0.05, 1.0)))
    components["support_ema_hold"] = max(ema20_proximity, support_proximity)

    # pullback_depth: controlled depth (ideal ~5-8%, penalty for too deep or too shallow)
    if pullback_depth_pct is not None:
        ideal_depth = 0.06
        depth_score = _clamp(100.0 * (1.0 - abs(pullback_depth_pct - ideal_depth) / max(max_pullback_depth, 0.01)))
        components["pullback_depth"] = depth_score
    else:
        components["pullback_depth"] = 0.0

    # trend_structure: higher-low evidence (swing_low > prior range low proxy)
    # Use swing_low relative to ema20 as a proxy for healthy trend structure
    trend_struct_score = 50.0  # neutral default
    if swing_low_adj is not None and ema50 is not None and swing_low_adj > ema50:
        trend_struct_score = 80.0  # swing low above EMA50 = healthy
    elif swing_low_adj is not None and ema200 is not None and swing_low_adj > ema200:
        trend_struct_score = 60.0
    elif swing_low_adj is None:
        trend_struct_score = 30.0
    components["trend_structure"] = trend_struct_score

    # rr: target room (structural = resistance or next_resistance above entry)
    # resistance_blocks=True when a resistance level above entry obstructs the path to target
    target_is_structural: bool | None = None
    resistance_blocks: bool = False
    if next_resistance_raw is not None and entry_raw and next_resistance_raw > entry_raw:
        components["rr"] = 80.0
        target_is_structural = True
        if resistance_raw is not None and resistance_raw > entry_raw and resistance_raw < next_resistance_raw:
            resistance_blocks = True
    elif resistance_raw is not None and entry_raw and resistance_raw > entry_raw:
        components["rr"] = 60.0
        target_is_structural = True
        resistance_blocks = True  # resistance IS the ceiling
    elif swing_high_raw is not None and entry_raw and swing_high_raw > entry_raw:
        components["rr"] = 50.0
        target_is_structural = True
        if resistance_raw is not None and resistance_raw > entry_raw and resistance_raw < swing_high_raw:
            resistance_blocks = True
    else:
        components["rr"] = 0.0
        target_is_structural = False

    # RVOL soft bonus (never hard-rejects)
    if rvol20 is not None and rvol20 >= rvol_bonus_threshold:
        rvol_bonus = _clamp(20.0 * (rvol20 - rvol_bonus_threshold))
    else:
        rvol_bonus = 0.0

    raw_score = _apply_weights(components, weights)
    # Apply RVOL soft bonus capped so total doesn't exceed 100
    raw_score = _clamp(raw_score + rvol_bonus * 0.05)
    penalized_score = _clamp(raw_score + earnings_pen + macro_pen + fundamentals_adj)

    setup_passed = setup_passed_hard and penalized_score >= min_setup_score

    reasons: list[str] = list(hard_fails)
    if not hard_fails:
        if penalized_score < min_setup_score:
            reasons.append(f"score_below_threshold({penalized_score:.1f}<{min_setup_score})")
        else:
            reasons.append("passed")
    fail_reason = hard_fails[0] if hard_fails else (
        None if setup_passed else f"score_below_threshold({penalized_score:.1f}<{min_setup_score})"
    )

    evidence: dict[str, Any] = {
        "close_adj": close_adj,
        "open_raw": open_raw,
        "ema200": ema200,
        "ema20": ema20,
        "ema50": ema50,
        "ema20_slope": ema20_slope,
        "roc20": roc20,
        "ema_alignment_score": ema_alignment_score,
        "pullback_depth_pct": pullback_depth_pct,
        "pullback_from_recent_high_pct": pullback_from_high,
        "dist_ema20_pct": dist_ema20,
        "support_adj": support_adj,
        "support_raw": support_raw,
        "resistance_adj": resistance_adj,
        "resistance_raw": resistance_raw,
        "next_resistance_adj": next_resistance_adj,
        "next_resistance_raw": next_resistance_raw,
        "swing_low_adj": swing_low_adj,
        "swing_high_raw": swing_high_raw,
        "rvol20": rvol20,
        "rvol_is_hard": False,  # always False for pullback (AD-22.23)
        "rvol_bonus": rvol_bonus,
        "rebound_required": rebound_required,
        "hard_fails": hard_fails,
        "component_scores": components,
        "raw_score": raw_score,
        "earnings_penalty": earnings_pen,
        "macro_penalty": macro_pen,
        "fundamentals_adjustment": fundamentals_adj,
        "fundamentals_evidence": fundamentals_evidence,
        "penalized_score": penalized_score,
        "target_is_structural": target_is_structural,
        "resistance_blocks": resistance_blocks,
        "confidence": _derive_confidence(penalized_score, conf_high, conf_medium),
    }

    return SetupValidationResult(
        ticker=ticker,
        signal_date=signal_date,
        setup_type=constants.SETUP_PULLBACK,
        setup_config_id=cfg_id,
        setup_passed=setup_passed,
        setup_score=penalized_score,
        confidence=_derive_confidence(penalized_score, conf_high, conf_medium),
        pass_fail_reasons=reasons,
        setup_fail_reason=fail_reason,
        evidence_json=evidence,
        feature_version=constants.FEATURE_SCHEMA_VERSION,
        entry_price_raw=entry_raw,
        support_level_raw=support_raw,
        resistance_level_raw=resistance_raw,
        next_resistance_level_raw=next_resistance_raw,
        atr_pct=atr_pct,
        distance_to_ema20_pct=dist_ema20,
        distance_to_ema50_pct=dist_ema50,
        rvol=rvol20,
        earnings_days=int(earnings_days) if earnings_days is not None else None,
        market_regime=market_regime,
        earnings_penalty=earnings_pen,
        macro_penalty=macro_pen,
        target_is_structural=target_is_structural,
    )


# ---------------------------------------------------------------------------
# TREND CONTINUATION validator
# ---------------------------------------------------------------------------

def validate_trend_continuation(
    feat: dict[str, Any],
    setup_config: dict[str, Any],
    risk_cfg: dict[str, Any] | None = None,
) -> SetupValidationResult:
    """Validate a trend_continuation setup.

    Hard checks:
    - ema_alignment_score >= min_ema_alignment
    - ema50_slope > min_ema50_slope
    - close_adj > ema50
    - close_adj > ema200
    - roc20 in [roc_min, roc_max]
    - distance_to_ema50_pct <= max_ext (not too extended)
    - RVOL: soft confirmation only (rvol_is_hard=False)

    Scoring components:
    - trend_health: EMA alignment + EMA slopes
    - relative_strength: RS vs SPY and sector
    - extension: how extended price is from EMA50
    - momentum: roc20 score
    - volume_health: rvol moderate check
    - target_room: next_resistance exists
    """
    cfg_id = setup_config.get("config_id", "")
    ticker = str(feat.get("ticker", ""))
    signal_date = str(feat.get("signal_date", ""))
    val = setup_config.get("validation", {})
    weights = setup_config.get("scoring_weights", {})

    min_ema_alignment: float = float(val.get("min_ema_alignment", 50))
    min_ema50_slope: float = float(val.get("min_ema50_slope", 0.0))
    roc_min: float = float(val.get("roc_min", 0.02))
    roc_max: float = float(val.get("roc_max", 0.40))
    max_ext: float = float(val.get("max_ext", 0.15))
    rvol_moderate_threshold: float = float(val.get("rvol_moderate_threshold", 1.2))
    min_setup_score: float = float(val.get("min_setup_score", 55))
    min_atr_stop_floor: float = float(val.get("min_atr_stop_floor_multiple", 0.5))

    # Feature extraction
    close_raw = _f(feat.get("close_raw"))
    close_adj = _f(feat.get("close_adj"))
    ema20 = _f(feat.get("ema20"))
    ema50 = _f(feat.get("ema50"))
    ema200 = _f(feat.get("ema200"))
    ema_alignment_score = _f(feat.get("ema_alignment_score"))
    ema50_slope = _f(feat.get("ema50_slope"))
    ema20_slope = _f(feat.get("ema20_slope"))
    roc20 = _f(feat.get("roc20"))
    dist_ema50 = _f(feat.get("distance_to_ema50_pct"))
    dist_ema20 = _f(feat.get("distance_to_ema20_pct"))
    rvol20 = _f(feat.get("rvol20"))
    rs_vs_spy = _f(feat.get("relative_strength_vs_spy"))
    sector_rs = _f(feat.get("sector_relative_strength"))
    support_adj = _f(feat.get("support_level"))
    resistance_adj = _f(feat.get("resistance_level"))
    next_resistance_adj = _f(feat.get("next_resistance_level"))
    swing_low_adj = _f(feat.get("swing_low"))
    atr_pct = _f(feat.get("atr_pct"))
    earnings_days = feat.get("days_to_earnings_bd")
    market_regime = feat.get("market_regime")

    # Raw conversion
    support_raw = _raw_conv(support_adj, close_raw, close_adj) if close_raw and close_adj else None
    resistance_raw = _raw_conv(resistance_adj, close_raw, close_adj) if close_raw and close_adj else None
    next_resistance_raw = _raw_conv(next_resistance_adj, close_raw, close_adj) if close_raw and close_adj else None
    swing_low_raw = _raw_conv(swing_low_adj, close_raw, close_adj) if close_raw and close_adj else None

    # Guard: swing_low above current price cannot serve as a valid stop anchor
    if swing_low_adj is not None and close_adj is not None and swing_low_adj >= close_adj:
        swing_low_adj = None
        swing_low_raw = None

    entry_raw = close_raw

    earnings_cfg, macro_cfg = _resolve_earnings_macro_cfg(setup_config, risk_cfg)
    # P2.1 [HC->CFG]: confidence thresholds + valuation-band map, resolved once
    # from the shared risk_label_config.scoring block (literal defaults if absent).
    conf_high, conf_medium = _resolve_confidence_thresholds(risk_cfg)
    valuation_band_quality = _resolve_valuation_band_quality(risk_cfg)
    earnings_pen, macro_pen = _compute_penalties(feat, earnings_cfg, macro_cfg)
    fundamentals_adj, fundamentals_evidence = _compute_fundamentals_adjustment(
        feat, setup_config.get("fundamentals", {}), valuation_band_quality
    )

    # --- Hard checks ---
    hard_fails: list[str] = []

    # 1. EMA alignment
    if ema_alignment_score is None:
        hard_fails.append("missing_ema_alignment_score")
    elif ema_alignment_score < min_ema_alignment:
        hard_fails.append(
            f"ema_alignment_too_low({ema_alignment_score:.1f}<{min_ema_alignment})"
        )

    # 2. EMA50 slope positive
    if ema50_slope is None:
        hard_fails.append("missing_ema50_slope")
    elif ema50_slope <= min_ema50_slope:
        hard_fails.append(f"ema50_slope_not_positive({ema50_slope:.4f}<={min_ema50_slope})")

    # 3. close > EMA50
    if close_adj is None or ema50 is None:
        hard_fails.append("missing_close_adj_or_ema50")
    elif close_adj <= ema50:
        hard_fails.append(f"price_below_ema50({close_adj:.2f}<={ema50:.2f})")

    # 4. close > EMA200
    if close_adj is not None and ema200 is not None and close_adj <= ema200:
        hard_fails.append(f"price_below_ema200({close_adj:.2f}<={ema200:.2f})")

    # 5. ROC20 in range
    if roc20 is None:
        hard_fails.append("missing_roc20")
    elif not (roc_min <= roc20 <= roc_max):
        hard_fails.append(f"roc20_out_of_range({roc20:.3f} not in [{roc_min},{roc_max}])")

    # 6. Not too extended
    if dist_ema50 is None:
        # Missing extension data → soft penalty, not hard fail
        pass
    elif abs(dist_ema50) > max_ext:
        hard_fails.append(f"too_extended_from_ema50({abs(dist_ema50):.3f}>{max_ext})")

    # 7. Stop ≥ 0.5 ATR below entry (P1-1)
    # Stop estimated as entry minus swing_low (standard trend continuation stop)
    if (
        atr_pct is not None and atr_pct > 0
        and swing_low_raw is not None
        and entry_raw is not None and entry_raw > 0
    ):
        stop_distance_pct = (entry_raw - swing_low_raw) / entry_raw
        stop_distance_atr = stop_distance_pct / atr_pct
        if stop_distance_atr < min_atr_stop_floor:
            hard_fails.append(
                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
            )

    # 8. Optional earnings hard-block (P1.2, opt-in — default False, zero
    # behavior change until explicitly enabled on a setup_config version)
    if bool(earnings_cfg.get("earnings_hard_block", False)):
        avoid_bd = int(earnings_cfg.get("avoid_within_bd", 5))
        days_to_earnings = _f(earnings_days)
        if days_to_earnings is not None and 0 < days_to_earnings <= avoid_bd:
            hard_fails.append(
                f"earnings_too_close({days_to_earnings:.0f}bd<={avoid_bd}bd)"
            )

    setup_passed_hard = len(hard_fails) == 0

    # --- Soft scoring ---
    components: dict[str, float] = {}

    # trend_health: EMA alignment + slopes
    alignment_score = ema_alignment_score if ema_alignment_score is not None else 0.0
    slope_score = 0.0
    if ema50_slope is not None and ema50_slope > 0:
        slope_score = _clamp(100.0 * min(ema50_slope / 0.02, 1.0))  # 2% slope = 100
    if ema20_slope is not None and ema20_slope > 0:
        slope_score = _clamp((slope_score + _clamp(100.0 * min(ema20_slope / 0.02, 1.0))) / 2)
    components["trend_health"] = 0.6 * alignment_score + 0.4 * slope_score

    # relative_strength
    rs_score = 50.0  # neutral
    if rs_vs_spy is not None:
        rs_score = _clamp(50.0 + rs_vs_spy * 500.0)  # 10% RS edge = 100
    if sector_rs is not None:
        sector_score = _clamp(50.0 + sector_rs * 500.0)
        rs_score = (rs_score + sector_score) / 2
    components["relative_strength"] = rs_score

    # extension: ideal at EMA50 (dist=0), penalise extension
    if dist_ema50 is not None:
        ext_score = _clamp(100.0 * (1.0 - abs(dist_ema50) / max(max_ext, 0.01)))
    else:
        ext_score = 50.0
    components["extension"] = ext_score

    # momentum: roc20 ideally in mid-range
    if roc20 is not None:
        ideal_roc = (roc_min + roc_max) / 2
        roc_score = _clamp(100.0 * (1.0 - abs(roc20 - ideal_roc) / max(roc_max - roc_min, 0.01)))
    else:
        roc_score = 0.0
    components["momentum"] = roc_score

    # volume_health: rvol moderate
    if rvol20 is not None:
        vol_score = _clamp(100.0 * min(rvol20 / max(rvol_moderate_threshold * 1.5, 1.0), 1.0))
    else:
        vol_score = 30.0  # not required; moderate default
    components["volume_health"] = vol_score

    # target_room: structural next resistance (P2-1: track resistance_blocks)
    target_is_structural: bool | None = None
    resistance_blocks: bool = False
    if next_resistance_raw is not None and entry_raw and next_resistance_raw > entry_raw:
        components["target_room"] = 80.0
        target_is_structural = True
        if resistance_raw is not None and resistance_raw > entry_raw and resistance_raw < next_resistance_raw:
            resistance_blocks = True
    elif resistance_raw is not None and entry_raw and resistance_raw > entry_raw:
        components["target_room"] = 50.0
        target_is_structural = True
        resistance_blocks = True
    else:
        components["target_room"] = 0.0
        target_is_structural = False

    raw_score = _apply_weights(components, weights)
    penalized_score = _clamp(raw_score + earnings_pen + macro_pen + fundamentals_adj)

    setup_passed = setup_passed_hard and penalized_score >= min_setup_score

    reasons: list[str] = list(hard_fails)
    if not hard_fails:
        if penalized_score < min_setup_score:
            reasons.append(f"score_below_threshold({penalized_score:.1f}<{min_setup_score})")
        else:
            reasons.append("passed")
    fail_reason = hard_fails[0] if hard_fails else (
        None if setup_passed else f"score_below_threshold({penalized_score:.1f}<{min_setup_score})"
    )

    evidence: dict[str, Any] = {
        "close_adj": close_adj,
        "ema50": ema50,
        "ema200": ema200,
        "ema_alignment_score": ema_alignment_score,
        "ema50_slope": ema50_slope,
        "ema20_slope": ema20_slope,
        "roc20": roc20,
        "dist_ema50_pct": dist_ema50,
        "rvol20": rvol20,
        "rs_vs_spy": rs_vs_spy,
        "sector_rs": sector_rs,
        "resistance_raw": resistance_raw,
        "next_resistance_raw": next_resistance_raw,
        "swing_low_raw": swing_low_raw,
        "hard_fails": hard_fails,
        "component_scores": components,
        "raw_score": raw_score,
        "earnings_penalty": earnings_pen,
        "macro_penalty": macro_pen,
        "fundamentals_adjustment": fundamentals_adj,
        "fundamentals_evidence": fundamentals_evidence,
        "penalized_score": penalized_score,
        "target_is_structural": target_is_structural,
        "resistance_blocks": resistance_blocks,
        "confidence": _derive_confidence(penalized_score, conf_high, conf_medium),
    }

    return SetupValidationResult(
        ticker=ticker,
        signal_date=signal_date,
        setup_type=constants.SETUP_TREND_CONTINUATION,
        setup_config_id=cfg_id,
        setup_passed=setup_passed,
        setup_score=penalized_score,
        confidence=_derive_confidence(penalized_score, conf_high, conf_medium),
        pass_fail_reasons=reasons,
        setup_fail_reason=fail_reason,
        evidence_json=evidence,
        feature_version=constants.FEATURE_SCHEMA_VERSION,
        entry_price_raw=entry_raw,
        support_level_raw=support_raw,
        resistance_level_raw=resistance_raw,
        next_resistance_level_raw=next_resistance_raw,
        atr_pct=atr_pct,
        distance_to_ema20_pct=dist_ema20,
        distance_to_ema50_pct=dist_ema50,
        rvol=rvol20,
        earnings_days=int(earnings_days) if earnings_days is not None else None,
        market_regime=market_regime,
        earnings_penalty=earnings_pen,
        macro_penalty=macro_pen,
        target_is_structural=target_is_structural,
    )


# ---------------------------------------------------------------------------
# CONSOLIDATION BASE validator
# ---------------------------------------------------------------------------

def validate_consolidation_base(
    feat: dict[str, Any],
    setup_config: dict[str, Any],
    risk_cfg: dict[str, Any] | None = None,
) -> SetupValidationResult:
    """Validate a consolidation_base setup.

    Hard checks:
    - range_tightness_score >= min_tightness
    - atr_pct <= max_atr_pct
    - base_low_raw <= close_raw <= base_high_raw  (price inside base)
    - range_duration >= min_range_duration
    - days_to_earnings_bd > min_earnings_days OR within penalty band
    - RVOL: not required (AD-22.23); controlled/low volume inside base acceptable

    Scoring components:
    - range_tightness: range_tightness_score
    - support_resistance_clarity: support and resistance levels clear
    - atr_compression: atr_compression_score
    - volume_dry_up: volume_dry_up_score
    - breakout_readiness: breakout_proximity near top of range
    - stop_tightness: base_low well-defined and close to entry
    """
    cfg_id = setup_config.get("config_id", "")
    ticker = str(feat.get("ticker", ""))
    signal_date = str(feat.get("signal_date", ""))
    val = setup_config.get("validation", {})
    weights = setup_config.get("scoring_weights", {})

    min_tightness: float = float(val.get("min_tightness", 60))
    max_atr_pct: float = float(val.get("max_atr_pct", 0.05))
    min_compression: float = float(val.get("min_compression", 50))
    min_range_duration: int = int(val.get("min_range_duration", 10))
    min_dry_up: float = float(val.get("min_dry_up", 40))
    min_earnings_days: int = int(val.get("min_earnings_days", 5))
    min_setup_score: float = float(val.get("min_setup_score", 55))
    # Consolidation uses a tighter ATR floor because base stops are naturally compressed
    min_atr_stop_floor: float = float(val.get("min_atr_stop_floor_multiple", 0.3))
    # Allow close to be slightly above base_high before rejecting (borderline breakout candidates)
    above_base_tolerance: float = float(val.get("price_above_base_tolerance", 0.01))
    # rvol_required=False always for consolidation_base (AD-22.23)
    # Opt-in hard gate on min_compression/min_dry_up (default False preserves
    # existing v1/preset behavior — these two thresholds were previously read
    # into config but never enforced; CODER_NOTE v3 item 2, option (b)). Flip
    # to True only on a newly-cloned config version, never on the active v1 row.
    enforce_compression_floor: bool = bool(val.get("enforce_compression_floor", False))

    # Feature extraction
    close_raw = _f(feat.get("close_raw"))
    close_adj = _f(feat.get("close_adj"))
    atr_pct = _f(feat.get("atr_pct"))
    atr_compression_score = _f(feat.get("atr_compression_score"))
    range_tightness_score = _f(feat.get("range_tightness_score"))
    range_duration = feat.get("range_duration")
    range_duration_val = int(range_duration) if range_duration is not None else None
    range_width_pct = _f(feat.get("range_width_pct"))
    volume_dry_up_score = _f(feat.get("volume_dry_up_score"))
    volume_expansion_score = _f(feat.get("volume_expansion_score"))
    base_high_adj = _f(feat.get("base_high"))
    base_low_adj = _f(feat.get("base_low"))
    support_adj = _f(feat.get("support_level"))
    resistance_adj = _f(feat.get("resistance_level"))
    next_resistance_adj = _f(feat.get("next_resistance_level"))
    breakout_proximity = _f(feat.get("breakout_proximity"))
    rvol20 = _f(feat.get("rvol20"))
    dist_ema20 = _f(feat.get("distance_to_ema20_pct"))
    dist_ema50 = _f(feat.get("distance_to_ema50_pct"))
    earnings_days_raw = feat.get("days_to_earnings_bd")
    market_regime = feat.get("market_regime")

    # Raw conversion
    base_high_raw = _raw_conv(base_high_adj, close_raw, close_adj) if close_raw and close_adj else None
    base_low_raw = _raw_conv(base_low_adj, close_raw, close_adj) if close_raw and close_adj else None
    support_raw = _raw_conv(support_adj, close_raw, close_adj) if close_raw and close_adj else None
    resistance_raw = _raw_conv(resistance_adj, close_raw, close_adj) if close_raw and close_adj else None
    next_resistance_raw = _raw_conv(next_resistance_adj, close_raw, close_adj) if close_raw and close_adj else None

    entry_raw = close_raw

    earnings_cfg, macro_cfg = _resolve_earnings_macro_cfg(setup_config, risk_cfg)
    # P2.1 [HC->CFG]: confidence thresholds + valuation-band map, resolved once
    # from the shared risk_label_config.scoring block (literal defaults if absent).
    conf_high, conf_medium = _resolve_confidence_thresholds(risk_cfg)
    valuation_band_quality = _resolve_valuation_band_quality(risk_cfg)
    earnings_pen, macro_pen = _compute_penalties(feat, earnings_cfg, macro_cfg)
    fundamentals_adj, fundamentals_evidence = _compute_fundamentals_adjustment(
        feat, setup_config.get("fundamentals", {}), valuation_band_quality
    )

    # --- Hard checks ---
    hard_fails: list[str] = []

    # 1. range tightness
    if range_tightness_score is None:
        hard_fails.append("missing_range_tightness_score")
    elif range_tightness_score < min_tightness:
        hard_fails.append(
            f"range_tightness_too_low({range_tightness_score:.1f}<{min_tightness})"
        )

    # 2. ATR % (volatility controlled)
    if atr_pct is None:
        hard_fails.append("missing_atr_pct")
    elif atr_pct > max_atr_pct:
        hard_fails.append(f"atr_too_high({atr_pct:.4f}>{max_atr_pct})")

    # 3. Base levels must be identified (P1-3)
    if base_high_adj is None or base_low_adj is None:
        hard_fails.append("consolidation_base_not_identified")
    elif base_high_raw is not None and base_low_raw is not None and close_raw is not None:
        # 3b. Price inside base (tolerance allows minor overshoots at top of range)
        if close_raw > base_high_raw * (1.0 + above_base_tolerance):
            hard_fails.append(
                f"price_above_base_high(close={close_raw:.2f}>base_high={base_high_raw:.2f})"
            )
        elif close_raw < base_low_raw:
            hard_fails.append(
                f"price_below_base_low(close={close_raw:.2f}<base_low={base_low_raw:.2f})"
            )

    # 4. Range duration
    if range_duration_val is None:
        hard_fails.append("missing_range_duration")
    elif range_duration_val < min_range_duration:
        hard_fails.append(
            f"range_duration_too_short({range_duration_val}<{min_range_duration})"
        )

    # 5. Earnings avoidance hard check
    earnings_days = int(earnings_days_raw) if earnings_days_raw is not None else None
    if earnings_days is not None and 0 < earnings_days <= min_earnings_days:
        hard_fails.append(
            f"earnings_too_close({earnings_days}bd<={min_earnings_days}bd)"
        )

    # 6. Stop ≥ 0.3 ATR below entry (P1-1; tighter floor for base setups)
    # Stop estimated as entry minus base_low (base stop placement)
    if (
        atr_pct is not None and atr_pct > 0
        and base_low_raw is not None
        and entry_raw is not None and entry_raw > 0
    ):
        stop_distance_pct = (entry_raw - base_low_raw) / entry_raw
        stop_distance_atr = stop_distance_pct / atr_pct
        if stop_distance_atr < min_atr_stop_floor:
            hard_fails.append(
                f"stop_below_atr_floor(stop_atr={stop_distance_atr:.2f}<{min_atr_stop_floor})"
            )

    # 7. ATR-compression / volume dry-up floors — opt-in only (enforce_compression_floor,
    # default False). When disabled (default for v1 and every existing preset), these
    # thresholds are read but not enforced, matching pre-existing behavior exactly.
    if enforce_compression_floor:
        if atr_compression_score is None:
            hard_fails.append("missing_atr_compression_score")
        elif atr_compression_score < min_compression:
            hard_fails.append(
                f"atr_compression_too_low({atr_compression_score:.1f}<{min_compression})"
            )
        if volume_dry_up_score is None:
            hard_fails.append("missing_volume_dry_up_score")
        elif volume_dry_up_score < min_dry_up:
            hard_fails.append(
                f"volume_dry_up_too_low({volume_dry_up_score:.1f}<{min_dry_up})"
            )

    # Note: RVOL not required for consolidation_base (AD-22.23)

    setup_passed_hard = len(hard_fails) == 0

    # --- Soft scoring ---
    components: dict[str, float] = {}

    # range_tightness
    components["range_tightness"] = range_tightness_score if range_tightness_score is not None else 0.0

    # support_resistance_clarity: both levels present and well-separated
    sr_score = 0.0
    if support_raw is not None and resistance_raw is not None and support_raw > 0:
        separation = (resistance_raw - support_raw) / support_raw
        if separation > 0.02:
            sr_score = _clamp(80.0 + separation * 200.0)
    elif support_raw is not None or resistance_raw is not None:
        sr_score = 40.0
    components["support_resistance_clarity"] = sr_score

    # atr_compression
    components["atr_compression"] = atr_compression_score if atr_compression_score is not None else 0.0

    # volume_dry_up
    components["volume_dry_up"] = volume_dry_up_score if volume_dry_up_score is not None else 0.0

    # breakout_readiness: price near top of base + proximity score
    readiness = 0.0
    if base_high_raw is not None and base_low_raw is not None and close_raw is not None:
        base_range = base_high_raw - base_low_raw
        if base_range > 0:
            position = (close_raw - base_low_raw) / base_range  # 0=bottom, 1=top
            readiness = _clamp(position * 100.0)  # higher in base = more ready
    if breakout_proximity is not None:
        # near 0 (near resistance) = more ready
        prox_score = _clamp(100.0 * (1.0 + breakout_proximity))  # prox=-1 → 0, prox=0 → 100
        readiness = 0.5 * readiness + 0.5 * prox_score
    components["breakout_readiness"] = readiness

    # stop_tightness: base_low well-defined + narrow stop distance
    stop_tight = 0.0
    if base_low_raw is not None and close_raw is not None and close_raw > 0:
        stop_dist = (close_raw - base_low_raw) / close_raw
        # ideal stop distance 2-5%
        stop_tight = _clamp(100.0 * (1.0 - abs(stop_dist - 0.035) / 0.05))
    components["stop_tightness"] = stop_tight

    raw_score = _apply_weights(components, weights)
    penalized_score = _clamp(raw_score + earnings_pen + macro_pen + fundamentals_adj)

    setup_passed = setup_passed_hard and penalized_score >= min_setup_score

    # target_is_structural: can we find a structural target above base_high?
    target_is_structural: bool | None = None
    if next_resistance_raw is not None and base_high_raw is not None and next_resistance_raw > (base_high_raw or 0):
        target_is_structural = True
    elif resistance_raw is not None and entry_raw and resistance_raw > entry_raw:
        target_is_structural = True
    else:
        target_is_structural = False

    reasons: list[str] = list(hard_fails)
    if not hard_fails:
        if penalized_score < min_setup_score:
            reasons.append(f"score_below_threshold({penalized_score:.1f}<{min_setup_score})")
        else:
            reasons.append("passed")
    fail_reason = hard_fails[0] if hard_fails else (
        None if setup_passed else f"score_below_threshold({penalized_score:.1f}<{min_setup_score})"
    )

    evidence: dict[str, Any] = {
        "close_raw": close_raw,
        "close_adj": close_adj,
        "atr_pct": atr_pct,
        "atr_compression_score": atr_compression_score,
        "range_tightness_score": range_tightness_score,
        "range_duration": range_duration_val,
        "range_width_pct": range_width_pct,
        "volume_dry_up_score": volume_dry_up_score,
        "volume_expansion_score": volume_expansion_score,
        "base_high_adj": base_high_adj,
        "base_low_adj": base_low_adj,
        "base_high_raw": base_high_raw,
        "base_low_raw": base_low_raw,
        "support_raw": support_raw,
        "resistance_raw": resistance_raw,
        "next_resistance_raw": next_resistance_raw,
        "breakout_proximity": breakout_proximity,
        "rvol20": rvol20,
        "rvol_required": False,  # never required (AD-22.23)
        "earnings_days": earnings_days,
        "hard_fails": hard_fails,
        "component_scores": components,
        "raw_score": raw_score,
        "earnings_penalty": earnings_pen,
        "macro_penalty": macro_pen,
        "fundamentals_adjustment": fundamentals_adj,
        "fundamentals_evidence": fundamentals_evidence,
        "penalized_score": penalized_score,
        "target_is_structural": target_is_structural,
        "confidence": _derive_confidence(penalized_score, conf_high, conf_medium),
    }

    return SetupValidationResult(
        ticker=ticker,
        signal_date=signal_date,
        setup_type=constants.SETUP_CONSOLIDATION_BASE,
        setup_config_id=cfg_id,
        setup_passed=setup_passed,
        setup_score=penalized_score,
        confidence=_derive_confidence(penalized_score, conf_high, conf_medium),
        pass_fail_reasons=reasons,
        setup_fail_reason=fail_reason,
        evidence_json=evidence,
        feature_version=constants.FEATURE_SCHEMA_VERSION,
        entry_price_raw=entry_raw,
        support_level_raw=support_raw,
        resistance_level_raw=resistance_raw,
        next_resistance_level_raw=next_resistance_raw,
        atr_pct=atr_pct,
        distance_to_ema20_pct=dist_ema20,
        distance_to_ema50_pct=dist_ema50,
        rvol=rvol20,
        earnings_days=earnings_days,
        market_regime=market_regime,
        earnings_penalty=earnings_pen,
        macro_penalty=macro_pen,
        target_is_structural=target_is_structural,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_VALIDATORS: Final[dict[str, Any]] = {
    constants.SETUP_BREAKOUT: validate_breakout,
    constants.SETUP_PULLBACK: validate_pullback,
    constants.SETUP_TREND_CONTINUATION: validate_trend_continuation,
    constants.SETUP_CONSOLIDATION_BASE: validate_consolidation_base,
}


def validate_setup(
    setup_type: str,
    feat: dict[str, Any],
    setup_config: dict[str, Any],
    risk_cfg: dict[str, Any] | None = None,
) -> SetupValidationResult:
    """Dispatch to the correct validator by setup_type.

    Raises ValueError for unrecognised setup_type.
    """
    validator = _VALIDATORS.get(setup_type)
    if validator is None:
        raise ValueError(
            f"Unknown setup_type {setup_type!r}. "
            f"Allowed: {list(_VALIDATORS)}"
        )
    return validator(feat, setup_config, risk_cfg)


__all__ = [
    "SetupValidationResult",
    "validate_setup",
    "validate_breakout",
    "validate_pullback",
    "validate_trend_continuation",
    "validate_consolidation_base",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_MEDIUM",
    "CONFIDENCE_LOW",
]
