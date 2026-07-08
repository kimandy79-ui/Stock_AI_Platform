"""Tests for Module 14 — M14 Setup Validators + Step4SetupValidationEngine.

Covers all Phase 4 acceptance criteria:
  - Each routed setup validated independently.
  - Each validator uses setup_config thresholds.
  - Each result has pass/fail, score, reasons, evidence.
  - No risk labels assigned in Phase 4.
  - No stop/target/RR logic (stop_price_raw/target_price_raw/estimated_rr = NULL).
  - No BUY/ranking/disposition logic.
  - Old strategy-mode logic not active in M14.
  - RVOL: pullback never hard-rejects; consolidation_base never requires.
  - setup_score deterministic.
  - evidence_json populated.
  - missing feature handled explicitly (no exception raised).
  - active setup_config used.
  - consolidation_base canonical naming.

Unit tests run fully offline (no DuckDB).
Integration tests use tmp_db_paths + real schema + duckdb_manager.
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.screening.m14_setup_validators import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SetupValidationResult,
    validate_breakout,
    validate_consolidation_base,
    validate_pullback,
    validate_setup,
    validate_trend_continuation,
)
from app.services.analysis.step4_setup_validation_engine import (
    ALLOWED_DB_ROLES,
    METADATA_KEYS,
    Step4SetupValidationEngine,
)
from app.utils.service_result import ServiceResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SIGNAL_DATE = date(2024, 3, 15)
RUN_ID = str(uuid.uuid4())
SCHEMA_VER = constants.FEATURE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------

def _breakout_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "config_id": "setup_breakout_v1",
        "setup_type": "breakout",
        "version": "breakout_v1",
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
        "earnings": {"avoid_within_bd": 5, "penalty_points_max": -15},
        "macro_event_risk": {"enabled": True, "window_bd_before": 1,
                             "window_bd_after": 1, "penalty_points": -10},
    }
    cfg["validation"].update(overrides)
    return cfg


def _pullback_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "config_id": "setup_pullback_v1",
        "setup_type": "pullback",
        "version": "pullback_v1",
        "validation": {
            "pull_band": 0.04,
            "max_pullback_depth": 0.12,
            "support_break_tol": 0.02,
            "k_atr_stop": 1.2,
            "buffer_atr_multiple": 0.25,
            "min_rr": 1.8,
            "rvol_is_hard": False,
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
        "earnings": {"avoid_within_bd": 5, "penalty_points_max": -15},
        "macro_event_risk": {"enabled": True, "window_bd_before": 1,
                             "window_bd_after": 1, "penalty_points": -10},
    }
    cfg["validation"].update(overrides)
    return cfg


def _tc_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "config_id": "setup_trend_continuation_v1",
        "setup_type": "trend_continuation",
        "version": "trend_continuation_v1",
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
        "earnings": {"avoid_within_bd": 5, "penalty_points_max": -15},
        "macro_event_risk": {"enabled": True, "window_bd_before": 1,
                             "window_bd_after": 1, "penalty_points": -10},
    }
    cfg["validation"].update(overrides)
    return cfg


def _cb_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "config_id": "setup_consolidation_base_v1",
        "setup_type": "consolidation_base",
        "version": "consolidation_base_v1",
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
            "rvol_required": False,
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
        "earnings": {"avoid_within_bd": 5, "penalty_points_max": -15},
        "macro_event_risk": {"enabled": True, "window_bd_before": 1,
                             "window_bd_after": 1, "penalty_points": -10},
    }
    cfg["validation"].update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Feature dict builders (all nullable fields default to None unless given)
# ---------------------------------------------------------------------------

def _breakout_feat(**overrides: Any) -> dict[str, Any]:
    """Good breakout feature row that should pass with default config."""
    f: dict[str, Any] = dict(
        ticker="AAPL",
        signal_date=SIGNAL_DATE.isoformat(),
        close_raw=155.0,
        close_adj=155.0,
        open_raw=153.0,
        high_raw=156.5,
        low_raw=152.0,
        breakout_proximity=-0.2,       # within [-1.0, 0.5]
        range_duration=15,             # >= 10
        range_tightness_score=70.0,
        rvol20=2.0,                    # >= 1.5 hard threshold
        volume_expansion_score=80.0,
        resistance_level=154.0,        # adjusted; resistance_raw = ~154
        next_resistance_level=165.0,
        support_level=145.0,
        atr_pct=0.03,
        distance_to_ema20_pct=-0.02,
        distance_to_ema50_pct=-0.05,
        market_regime="neutral",
        days_to_earnings_bd=20,
        macro_event_risk_flag=False,
        # feature_snapshot keys not required by validator
    )
    f.update(overrides)
    return f


def _pullback_feat(**overrides: Any) -> dict[str, Any]:
    """Good pullback feature row."""
    f: dict[str, Any] = dict(
        ticker="MSFT",
        signal_date=SIGNAL_DATE.isoformat(),
        close_raw=300.0,
        close_adj=300.0,
        open_raw=298.0,
        high_raw=305.0,
        low_raw=297.0,
        ema20=310.0,
        ema50=295.0,
        ema200=260.0,
        ema_alignment_score=100.0,
        pullback_depth_pct=0.07,       # <= 0.12
        pullback_from_recent_high_pct=-0.07,
        support_level=292.0,           # adj; support_raw ~ 292 (same adj factor=1)
        resistance_level=320.0,
        next_resistance_level=340.0,
        swing_high=320.0,
        swing_low=290.0,
        distance_to_ema20_pct=-0.03,
        distance_to_ema50_pct=0.017,
        rvol20=1.0,                    # low RVOL — but never hard reject for pullback
        atr_pct=0.025,
        market_regime="bull",
        days_to_earnings_bd=30,
        macro_event_risk_flag=False,
    )
    f.update(overrides)
    return f


def _tc_feat(**overrides: Any) -> dict[str, Any]:
    """Good trend_continuation feature row."""
    f: dict[str, Any] = dict(
        ticker="NVDA",
        signal_date=SIGNAL_DATE.isoformat(),
        close_raw=500.0,
        close_adj=500.0,
        open_raw=495.0,
        high_raw=510.0,
        low_raw=492.0,
        ema20=490.0,
        ema50=470.0,
        ema200=400.0,
        ema_alignment_score=100.0,     # >= 50
        ema50_slope=0.015,             # > 0
        ema20_slope=0.02,
        roc20=0.12,                    # in [0.02, 0.40]
        distance_to_ema50_pct=0.06,    # <= 0.15
        distance_to_ema20_pct=0.02,
        relative_strength_vs_spy=0.05,
        sector_relative_strength=0.03,
        rvol20=1.5,
        support_level=465.0,
        resistance_level=520.0,
        next_resistance_level=560.0,
        swing_low=460.0,
        atr_pct=0.025,
        market_regime="bull",
        days_to_earnings_bd=30,
        macro_event_risk_flag=False,
    )
    f.update(overrides)
    return f


def _cb_feat(**overrides: Any) -> dict[str, Any]:
    """Good consolidation_base feature row."""
    f: dict[str, Any] = dict(
        ticker="JPM",
        signal_date=SIGNAL_DATE.isoformat(),
        close_raw=175.0,
        close_adj=175.0,
        open_raw=174.0,
        high_raw=176.5,
        low_raw=173.0,
        range_tightness_score=75.0,   # >= 60
        atr_pct=0.02,                  # <= 0.05
        atr_compression_score=65.0,
        base_high=178.0,               # adj (close_adj <= base_high)
        base_low=170.0,                # adj (close_adj >= base_low)
        range_duration=20,             # >= 10
        range_width_pct=0.047,
        volume_dry_up_score=60.0,
        volume_expansion_score=30.0,
        support_level=169.0,
        resistance_level=179.0,
        next_resistance_level=190.0,
        breakout_proximity=-0.2,
        rvol20=0.8,                    # low — acceptable for consolidation_base
        distance_to_ema20_pct=-0.01,
        distance_to_ema50_pct=-0.03,
        market_regime="neutral",
        days_to_earnings_bd=20,        # > min_earnings_days=5
        macro_event_risk_flag=False,
    )
    f.update(overrides)
    return f


# ===========================================================================
# BREAKOUT tests
# ===========================================================================

class TestBreakoutValidator:
    def test_breakout_pass(self) -> None:
        result = validate_breakout(_breakout_feat(), _breakout_config())
        assert result.setup_passed is True
        assert result.setup_score > 55
        assert "passed" in result.pass_fail_reasons
        assert result.setup_fail_reason is None
        assert result.setup_type == constants.SETUP_BREAKOUT

    def test_breakout_fail_no_resistance(self) -> None:
        feat = _breakout_feat(resistance_level=None)
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert result.setup_fail_reason is not None
        assert "no_resistance_level" in result.pass_fail_reasons

    def test_breakout_pass_when_only_resistance_adj_set(self) -> None:
        # P2 regression: resistance_adj non-null should satisfy the check even
        # when raw conversion would give None (close_raw/close_adj provided here
        # so raw conversion succeeds; test confirms the guard uses OR logic).
        feat = _breakout_feat(resistance_level=154.0, close_raw=155.0, close_adj=155.0)
        result = validate_breakout(feat, _breakout_config())
        assert "no_resistance_level" not in result.pass_fail_reasons

    def test_breakout_fail_proximity_out_of_range(self) -> None:
        # proximity 1.5 is outside [−1.0, 0.5]
        feat = _breakout_feat(breakout_proximity=1.5)
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert any("breakout_proximity_out_of_range" in r for r in result.pass_fail_reasons)

    def test_breakout_fail_rvol_hard_gate(self) -> None:
        feat = _breakout_feat(rvol20=0.8)  # below 1.5 hard threshold
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert any("rvol_below_hard_threshold" in r for r in result.pass_fail_reasons)

    def test_breakout_fail_range_too_short(self) -> None:
        feat = _breakout_feat(range_duration=5)
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert any("range_duration_too_short" in r for r in result.pass_fail_reasons)

    def test_breakout_score_deterministic(self) -> None:
        feat = _breakout_feat()
        cfg = _breakout_config()
        r1 = validate_breakout(feat, cfg)
        r2 = validate_breakout(feat, cfg)
        assert r1.setup_score == r2.setup_score

    def test_breakout_evidence_populated(self) -> None:
        result = validate_breakout(_breakout_feat(), _breakout_config())
        ev = result.evidence_json
        assert "breakout_proximity" in ev
        assert "rvol20" in ev
        assert "component_scores" in ev
        assert "hard_fails" in ev
        assert isinstance(ev["component_scores"], dict)

    def test_breakout_returns_correct_setup_type(self) -> None:
        result = validate_breakout(_breakout_feat(), _breakout_config())
        assert result.setup_type == constants.SETUP_BREAKOUT
        assert result.setup_type == "breakout"

    def test_breakout_no_risk_label(self) -> None:
        result = validate_breakout(_breakout_feat(), _breakout_config())
        # SetupValidationResult has no risk_label attribute
        assert not hasattr(result, "risk_label")

    def test_breakout_no_disposition(self) -> None:
        result = validate_breakout(_breakout_feat(), _breakout_config())
        assert not hasattr(result, "disposition")

    def test_breakout_stop_target_none_phase5(self) -> None:
        result = validate_breakout(_breakout_feat(), _breakout_config())
        # Phase 4 does not compute stop/target/RR
        # (validators set entry_price_raw but NOT stop_price_raw/target_price_raw/estimated_rr)
        assert not hasattr(result, "stop_price_raw") or result.stop_price_raw is None  # type: ignore[union-attr]

    def test_breakout_missing_all_features(self) -> None:
        # All None features should not raise, just fail
        feat = dict(ticker="X", signal_date=SIGNAL_DATE.isoformat())
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert len(result.pass_fail_reasons) > 0

    def test_breakout_target_is_structural_true(self) -> None:
        feat = _breakout_feat(next_resistance_level=170.0)  # > close_raw 155
        result = validate_breakout(feat, _breakout_config())
        assert result.target_is_structural is True

    def test_breakout_target_is_structural_none_when_no_resistance(self) -> None:
        # Early return when resistance_level is absent → target_is_structural is None (not evaluated)
        feat = _breakout_feat(next_resistance_level=None, resistance_level=None)
        result = validate_breakout(feat, _breakout_config())
        assert result.target_is_structural is None

    def test_breakout_raw_conversion_applied(self) -> None:
        # When close_raw != close_adj, resistance_level_raw should differ from resistance_level
        feat = _breakout_feat(close_raw=100.0, close_adj=110.0, resistance_level=154.0)
        result = validate_breakout(feat, _breakout_config())
        expected_raw = 154.0 * (100.0 / 110.0)
        assert result.resistance_level_raw is not None
        assert abs(result.resistance_level_raw - expected_raw) < 0.01

    def test_breakout_market_regime_propagated(self) -> None:
        feat = _breakout_feat(market_regime="bull")
        result = validate_breakout(feat, _breakout_config())
        assert result.market_regime == "bull"

    def test_breakout_market_regime_null_propagated(self) -> None:
        feat = _breakout_feat(market_regime=None)
        result = validate_breakout(feat, _breakout_config())
        assert result.market_regime is None  # never defaulted to neutral

    def test_breakout_earnings_penalty_applied(self) -> None:
        # Earnings close = large penalty
        feat = _breakout_feat(days_to_earnings_bd=2)
        result_close = validate_breakout(feat, _breakout_config())
        feat_far = _breakout_feat(days_to_earnings_bd=30)
        result_far = validate_breakout(feat_far, _breakout_config())
        # close earnings → lower score
        assert result_close.earnings_penalty < 0
        assert result_far.earnings_penalty == 0.0

    def test_breakout_uses_config_thresholds(self) -> None:
        # Loosen RVOL threshold — ticker with low RVOL should now pass
        cfg = _breakout_config(min_rvol_breakout=0.5)
        feat = _breakout_feat(rvol20=0.6)
        result = validate_breakout(feat, cfg)
        assert not any("rvol_below_hard_threshold" in r for r in result.pass_fail_reasons)


class TestDeadKeyRemovalNoBehaviorChange:
    """CODER_NOTE v3 items 1 & 5 — max_stop_distance_pct and min_close_strength
    removed from setup_config.validation templates (confirmed unused by
    validate_breakout). Injecting them back with adversarial values must not
    change the validator's output at all — proves they were truly inert."""

    def test_min_close_strength_adversarial_value_no_effect(self) -> None:
        feat = _breakout_feat()
        cfg_current = _breakout_config()  # matches today's real (post-removal) shape
        cfg_old_shaped = _breakout_config(min_close_strength=0.99)  # re-add, extreme value
        r1 = validate_breakout(feat, cfg_current)
        r2 = validate_breakout(feat, cfg_old_shaped)
        assert r1.setup_passed == r2.setup_passed
        assert r1.setup_score == r2.setup_score
        assert r1.pass_fail_reasons == r2.pass_fail_reasons

    def test_max_stop_distance_pct_adversarial_value_no_effect(self) -> None:
        feat = _breakout_feat()
        cfg_current = _breakout_config()
        cfg_old_shaped = _breakout_config(max_stop_distance_pct=0.001)  # re-add, tiny value
        r1 = validate_breakout(feat, cfg_current)
        r2 = validate_breakout(feat, cfg_old_shaped)
        assert r1.setup_passed == r2.setup_passed
        assert r1.setup_score == r2.setup_score
        assert r1.pass_fail_reasons == r2.pass_fail_reasons

    def test_current_default_configs_breakout_has_neither_key(self) -> None:
        from app.services.config import default_configs
        v1 = default_configs.DEFAULT_SETUP_CONFIGS["breakout"]["validation"]
        assert "min_close_strength" not in v1
        assert "max_stop_distance_pct" not in v1
        for p in default_configs.get_preset_setup_configs():
            if p["setup_type"] == "breakout":
                assert "min_close_strength" not in p["validation"], p["config_id"]
                assert "max_stop_distance_pct" not in p["validation"], p["config_id"]


# ===========================================================================
# PULLBACK tests
# ===========================================================================

class TestPullbackValidator:
    def test_pullback_pass(self) -> None:
        result = validate_pullback(_pullback_feat(), _pullback_config())
        assert result.setup_passed is True
        assert result.setup_score > 55
        assert "passed" in result.pass_fail_reasons
        assert result.setup_type == constants.SETUP_PULLBACK

    def test_pullback_fail_no_uptrend(self) -> None:
        # close_adj below ema200 → no uptrend
        feat = _pullback_feat(close_adj=250.0, ema200=260.0)
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False
        assert any("price_below_ema200" in r for r in result.pass_fail_reasons)

    def test_pullback_fail_ema20_not_above_ema50(self) -> None:
        feat = _pullback_feat(ema20=290.0, ema50=295.0)
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False
        assert any("ema20_not_above_ema50" in r for r in result.pass_fail_reasons)

    def test_pullback_fail_pullback_too_deep(self) -> None:
        feat = _pullback_feat(pullback_depth_pct=0.20)  # > max 0.12
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False
        assert any("pullback_too_deep" in r for r in result.pass_fail_reasons)

    def test_pullback_fail_support_broken(self) -> None:
        # close_raw well below support_raw (support_adj=292, close=250, tol=2%)
        feat = _pullback_feat(close_raw=250.0, close_adj=250.0, support_level=292.0)
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False
        assert any("support_broken" in r for r in result.pass_fail_reasons)

    def test_pullback_rvol_never_hard_rejects(self) -> None:
        """AD-22.23: low RVOL must NEVER cause hard rejection for pullback."""
        feat = _pullback_feat(rvol20=0.1)  # extremely low RVOL
        result = validate_pullback(feat, _pullback_config())
        # Should not appear in hard failures
        assert not any("rvol" in r.lower() and "hard" in r.lower() for r in result.pass_fail_reasons)
        # setup_passed may be True or False (scoring), but not due to RVOL hard gate
        assert result.evidence_json["rvol_is_hard"] is False

    def test_pullback_rvol_is_hard_override(self) -> None:
        """Even if config sets rvol_is_hard=True, pullback validator overrides to False."""
        cfg = _pullback_config(rvol_is_hard=True)
        feat = _pullback_feat(rvol20=0.1)
        result = validate_pullback(feat, cfg)
        assert result.evidence_json["rvol_is_hard"] is False

    def test_pullback_score_deterministic(self) -> None:
        feat = _pullback_feat()
        cfg = _pullback_config()
        assert validate_pullback(feat, cfg).setup_score == validate_pullback(feat, cfg).setup_score

    def test_pullback_evidence_populated(self) -> None:
        result = validate_pullback(_pullback_feat(), _pullback_config())
        ev = result.evidence_json
        assert "pullback_depth_pct" in ev
        assert "rvol20" in ev
        assert "component_scores" in ev
        assert "hard_fails" in ev

    def test_pullback_missing_features_no_exception(self) -> None:
        feat = dict(ticker="X", signal_date=SIGNAL_DATE.isoformat())
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False

    def test_pullback_market_regime_null_not_defaulted(self) -> None:
        feat = _pullback_feat(market_regime=None)
        result = validate_pullback(feat, _pullback_config())
        assert result.market_regime is None

    def test_pullback_uses_config_thresholds(self) -> None:
        cfg = _pullback_config(max_pullback_depth=0.20)  # looser threshold
        feat = _pullback_feat(pullback_depth_pct=0.15)   # would fail default
        result = validate_pullback(feat, cfg)
        assert not any("pullback_too_deep" in r for r in result.pass_fail_reasons)


# ===========================================================================
# TREND CONTINUATION tests
# ===========================================================================

class TestTrendContinuationValidator:
    def test_tc_pass(self) -> None:
        result = validate_trend_continuation(_tc_feat(), _tc_config())
        assert result.setup_passed is True
        assert result.setup_score > 55
        assert "passed" in result.pass_fail_reasons
        assert result.setup_type == constants.SETUP_TREND_CONTINUATION

    def test_tc_fail_ema_alignment_too_low(self) -> None:
        feat = _tc_feat(ema_alignment_score=30.0)  # < 50
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False
        assert any("ema_alignment_too_low" in r for r in result.pass_fail_reasons)

    def test_tc_fail_ema50_slope_not_positive(self) -> None:
        feat = _tc_feat(ema50_slope=-0.001)
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False
        assert any("ema50_slope_not_positive" in r for r in result.pass_fail_reasons)

    def test_tc_fail_price_below_ema50(self) -> None:
        feat = _tc_feat(close_adj=460.0, ema50=470.0)
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False
        assert any("price_below_ema50" in r for r in result.pass_fail_reasons)

    def test_tc_fail_roc20_out_of_range(self) -> None:
        feat = _tc_feat(roc20=0.50)  # > roc_max 0.40
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False
        assert any("roc20_out_of_range" in r for r in result.pass_fail_reasons)

    def test_tc_fail_roc20_too_low(self) -> None:
        feat = _tc_feat(roc20=0.005)  # < roc_min 0.02
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False
        assert any("roc20_out_of_range" in r for r in result.pass_fail_reasons)

    def test_tc_fail_too_extended(self) -> None:
        feat = _tc_feat(distance_to_ema50_pct=0.20)  # > max_ext 0.15
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False
        assert any("too_extended_from_ema50" in r for r in result.pass_fail_reasons)

    def test_tc_rvol_never_hard_rejects(self) -> None:
        """rvol_is_hard=False for trend_continuation."""
        feat = _tc_feat(rvol20=0.1)
        cfg = _tc_config()
        result = validate_trend_continuation(feat, cfg)
        # Should not hard-fail on RVOL
        assert not any("rvol_below_hard_threshold" in r for r in result.pass_fail_reasons)

    def test_tc_score_deterministic(self) -> None:
        feat = _tc_feat()
        cfg = _tc_config()
        r1 = validate_trend_continuation(feat, cfg)
        r2 = validate_trend_continuation(feat, cfg)
        assert r1.setup_score == r2.setup_score

    def test_tc_evidence_populated(self) -> None:
        result = validate_trend_continuation(_tc_feat(), _tc_config())
        ev = result.evidence_json
        assert "ema_alignment_score" in ev
        assert "ema50_slope" in ev
        assert "roc20" in ev
        assert "component_scores" in ev

    def test_tc_missing_features_no_exception(self) -> None:
        feat = dict(ticker="X", signal_date=SIGNAL_DATE.isoformat())
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False

    def test_tc_rs_vs_spy_in_evidence(self) -> None:
        feat = _tc_feat(relative_strength_vs_spy=0.08)
        result = validate_trend_continuation(feat, _tc_config())
        assert result.evidence_json["rs_vs_spy"] == 0.08

    def test_tc_uses_config_thresholds(self) -> None:
        cfg = _tc_config(roc_min=0.01, roc_max=0.60)
        feat = _tc_feat(roc20=0.45)  # would fail default roc_max=0.40
        result = validate_trend_continuation(feat, cfg)
        assert not any("roc20_out_of_range" in r for r in result.pass_fail_reasons)


# ===========================================================================
# CONSOLIDATION BASE tests
# ===========================================================================

class TestConsolidationBaseValidator:
    def test_cb_pass(self) -> None:
        result = validate_consolidation_base(_cb_feat(), _cb_config())
        assert result.setup_passed is True
        assert result.setup_score > 55
        assert "passed" in result.pass_fail_reasons
        assert result.setup_type == constants.SETUP_CONSOLIDATION_BASE

    def test_cb_canonical_name(self) -> None:
        result = validate_consolidation_base(_cb_feat(), _cb_config())
        assert result.setup_type == "consolidation_base"
        assert result.setup_type != "conservative_consolidation"

    def test_cb_fail_range_tightness_too_low(self) -> None:
        feat = _cb_feat(range_tightness_score=40.0)  # < 60
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("range_tightness_too_low" in r for r in result.pass_fail_reasons)

    def test_cb_fail_atr_too_high(self) -> None:
        feat = _cb_feat(atr_pct=0.08)  # > 0.05
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("atr_too_high" in r for r in result.pass_fail_reasons)

    def test_cb_fail_price_above_base(self) -> None:
        # close_raw > base_high_raw after conversion (adj==raw here)
        feat = _cb_feat(close_raw=180.0, close_adj=180.0, base_high=178.0)
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("price_above_base_high" in r for r in result.pass_fail_reasons)

    def test_cb_fail_price_below_base(self) -> None:
        feat = _cb_feat(close_raw=168.0, close_adj=168.0, base_low=170.0)
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("price_below_base_low" in r for r in result.pass_fail_reasons)

    def test_cb_fail_range_duration_too_short(self) -> None:
        feat = _cb_feat(range_duration=5)  # < 10
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("range_duration_too_short" in r for r in result.pass_fail_reasons)

    def test_cb_fail_earnings_too_close(self) -> None:
        feat = _cb_feat(days_to_earnings_bd=3)  # <= min_earnings_days=5
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("earnings_too_close" in r for r in result.pass_fail_reasons)

    def test_cb_rvol_not_required(self) -> None:
        """AD-22.23: RVOL never required for consolidation_base."""
        feat = _cb_feat(rvol20=None)  # no RVOL at all
        result = validate_consolidation_base(feat, _cb_config())
        # Should not hard-fail on missing RVOL
        assert not any("rvol" in r.lower() for r in result.pass_fail_reasons
                       if "required" in r.lower() or "missing_rvol" in r.lower())
        assert result.evidence_json["rvol_required"] is False

    def test_cb_low_rvol_acceptable(self) -> None:
        feat = _cb_feat(rvol20=0.3)  # very low rvol
        result = validate_consolidation_base(feat, _cb_config())
        # Low RVOL alone must not cause hard fail
        hard_fails = result.evidence_json.get("hard_fails", [])
        assert not any("rvol" in f.lower() for f in hard_fails)

    def test_cb_score_deterministic(self) -> None:
        feat = _cb_feat()
        cfg = _cb_config()
        r1 = validate_consolidation_base(feat, cfg)
        r2 = validate_consolidation_base(feat, cfg)
        assert r1.setup_score == r2.setup_score

    def test_cb_evidence_populated(self) -> None:
        result = validate_consolidation_base(_cb_feat(), _cb_config())
        ev = result.evidence_json
        assert "range_tightness_score" in ev
        assert "atr_pct" in ev
        assert "volume_dry_up_score" in ev
        assert "component_scores" in ev
        assert "rvol_required" in ev

    def test_cb_missing_features_no_exception(self) -> None:
        feat = dict(ticker="X", signal_date=SIGNAL_DATE.isoformat())
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False

    def test_cb_uses_config_thresholds(self) -> None:
        cfg = _cb_config(min_tightness=40)  # looser
        feat = _cb_feat(range_tightness_score=50.0)  # would fail default 60
        result = validate_consolidation_base(feat, cfg)
        assert not any("range_tightness_too_low" in r for r in result.pass_fail_reasons)


# ===========================================================================
# Earnings/macro dual-read fallback (CODER_NOTE v3 item 6)
# ===========================================================================

class TestEarningsMacroDualReadFallback:
    """risk_cfg is optional and, when its shared earnings/macro_event_risk
    block is absent, every validator must reproduce today's exact behavior
    (falls back to the setup_config's own copy). Only when risk_cfg carries
    the shared block does it take priority."""

    def test_no_risk_cfg_arg_falls_back_to_setup_config_copy(self) -> None:
        feat = _breakout_feat(days_to_earnings_bd=2)  # inside avoid_within_bd=5
        cfg = _breakout_config()
        result = validate_breakout(feat, cfg)  # risk_cfg omitted entirely
        expected = -15 * (1.0 - 2 / 5)
        assert result.earnings_penalty == pytest.approx(expected)

    def test_empty_risk_cfg_falls_back_to_setup_config_copy(self) -> None:
        feat = _breakout_feat(days_to_earnings_bd=2)
        cfg = _breakout_config()
        result = validate_breakout(feat, cfg, risk_cfg={})
        expected = -15 * (1.0 - 2 / 5)
        assert result.earnings_penalty == pytest.approx(expected)

    def test_risk_cfg_missing_shared_block_falls_back(self) -> None:
        """Simulates today's active risk_label_config_v1, which carries no
        'earnings'/'macro_event_risk' keys at all."""
        feat = _breakout_feat(days_to_earnings_bd=2)
        cfg = _breakout_config()
        risk_cfg_v1_shape = {"buy_rules": {"min_rr_for_buy": 1.8}}  # no shared block
        result = validate_breakout(feat, cfg, risk_cfg=risk_cfg_v1_shape)
        expected = -15 * (1.0 - 2 / 5)  # setup_config's own copy still wins
        assert result.earnings_penalty == pytest.approx(expected)

    def test_risk_cfg_shared_earnings_block_takes_priority(self) -> None:
        """Once a risk_label_config version carrying the shared block is
        active, its numbers must win over the setup_config's own copy."""
        feat = _breakout_feat(days_to_earnings_bd=2)
        cfg = _breakout_config()  # own copy: avoid_within_bd=5, penalty_points_max=-15
        risk_cfg_v2_shape = {
            "earnings": {"avoid_within_bd": 10, "penalty_points_max": -30},
        }
        result = validate_breakout(feat, cfg, risk_cfg=risk_cfg_v2_shape)
        expected = -30 * (1.0 - 2 / 10)  # risk_cfg's numbers, not the config's own
        assert result.earnings_penalty == pytest.approx(expected)

    def test_risk_cfg_shared_macro_block_takes_priority(self) -> None:
        feat = _breakout_feat(macro_event_risk_flag=True)
        cfg = _breakout_config()  # own copy: penalty_points=-10
        risk_cfg_v2_shape = {
            "macro_event_risk": {"enabled": True, "penalty_points": -25},
        }
        result = validate_breakout(feat, cfg, risk_cfg=risk_cfg_v2_shape)
        assert result.macro_penalty == pytest.approx(-25.0)

    def test_zero_behavior_change_when_shared_block_matches_own_copy(self) -> None:
        """If/when risk_label_config_v2 (identical values to every setup_config's
        own copy, by construction) is active, the resolved penalty must be
        byte-identical to today's fallback-only behavior."""
        feat = _pullback_feat(days_to_earnings_bd=3, macro_event_risk_flag=True)
        cfg = _pullback_config()
        without_risk_cfg = validate_pullback(feat, cfg)
        v2_shaped_risk_cfg = {
            "earnings": {"avoid_within_bd": 5, "penalty_points_max": -15},
            "macro_event_risk": {"enabled": True, "penalty_points": -10},
        }
        with_v2_risk_cfg = validate_pullback(feat, cfg, risk_cfg=v2_shaped_risk_cfg)
        assert with_v2_risk_cfg.earnings_penalty == pytest.approx(without_risk_cfg.earnings_penalty)
        assert with_v2_risk_cfg.macro_penalty == pytest.approx(without_risk_cfg.macro_penalty)

    def test_dual_read_applies_to_all_four_setup_types(self) -> None:
        risk_cfg = {"earnings": {"avoid_within_bd": 8, "penalty_points_max": -20}}
        cases = [
            (validate_breakout, _breakout_feat(days_to_earnings_bd=2), _breakout_config()),
            (validate_pullback, _pullback_feat(days_to_earnings_bd=2), _pullback_config()),
            (validate_trend_continuation, _tc_feat(days_to_earnings_bd=2), _tc_config()),
            (validate_consolidation_base, _cb_feat(days_to_earnings_bd=2), _cb_config()),
        ]
        expected = -20 * (1.0 - 2 / 8)
        for validator, feat, cfg in cases:
            result = validator(feat, cfg, risk_cfg)
            assert result.earnings_penalty == pytest.approx(expected), validator.__name__

    def test_validate_setup_dispatcher_forwards_risk_cfg(self) -> None:
        risk_cfg = {"earnings": {"avoid_within_bd": 8, "penalty_points_max": -20}}
        feat = _breakout_feat(days_to_earnings_bd=2)
        cfg = _breakout_config()
        result = validate_setup(constants.SETUP_BREAKOUT, feat, cfg, risk_cfg)
        expected = -20 * (1.0 - 2 / 8)
        assert result.earnings_penalty == pytest.approx(expected)


# ===========================================================================
# Dispatcher tests
# ===========================================================================

class TestValidateSetupDispatcher:
    def test_dispatches_breakout(self) -> None:
        result = validate_setup(constants.SETUP_BREAKOUT, _breakout_feat(), _breakout_config())
        assert result.setup_type == constants.SETUP_BREAKOUT

    def test_dispatches_pullback(self) -> None:
        result = validate_setup(constants.SETUP_PULLBACK, _pullback_feat(), _pullback_config())
        assert result.setup_type == constants.SETUP_PULLBACK

    def test_dispatches_trend_continuation(self) -> None:
        result = validate_setup(constants.SETUP_TREND_CONTINUATION, _tc_feat(), _tc_config())
        assert result.setup_type == constants.SETUP_TREND_CONTINUATION

    def test_dispatches_consolidation_base(self) -> None:
        result = validate_setup(constants.SETUP_CONSOLIDATION_BASE, _cb_feat(), _cb_config())
        assert result.setup_type == constants.SETUP_CONSOLIDATION_BASE

    def test_unknown_setup_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown setup_type"):
            validate_setup("conservative_consolidation", {}, {})

    def test_legacy_strategy_name_raises(self) -> None:
        for legacy in ("aggressive", "normal", "conservative", "high_tight_flag",
                       "volatility_squeeze", "trend_resume"):
            with pytest.raises(ValueError):
                validate_setup(legacy, {}, {})


# ===========================================================================
# SetupValidationResult contract tests
# ===========================================================================

class TestSetupValidationResultContract:
    def test_no_risk_label_attribute(self) -> None:
        r = validate_breakout(_breakout_feat(), _breakout_config())
        assert not hasattr(r, "risk_label")

    def test_no_disposition_attribute(self) -> None:
        r = validate_breakout(_breakout_feat(), _breakout_config())
        assert not hasattr(r, "disposition")

    def test_no_ranking_fields(self) -> None:
        r = validate_breakout(_breakout_feat(), _breakout_config())
        assert not hasattr(r, "raw_rank")
        assert not hasattr(r, "diversified_rank")

    def test_setup_type_in_allowed_types(self) -> None:
        for fn, feat, cfg in [
            (validate_breakout, _breakout_feat(), _breakout_config()),
            (validate_pullback, _pullback_feat(), _pullback_config()),
            (validate_trend_continuation, _tc_feat(), _tc_config()),
            (validate_consolidation_base, _cb_feat(), _cb_config()),
        ]:
            r = fn(feat, cfg)
            assert r.setup_type in constants.ALLOWED_SETUP_TYPES

    def test_setup_score_in_0_100(self) -> None:
        for fn, feat, cfg in [
            (validate_breakout, _breakout_feat(), _breakout_config()),
            (validate_pullback, _pullback_feat(), _pullback_config()),
            (validate_trend_continuation, _tc_feat(), _tc_config()),
            (validate_consolidation_base, _cb_feat(), _cb_config()),
        ]:
            r = fn(feat, cfg)
            assert 0.0 <= r.setup_score <= 100.0

    def test_feature_version_set(self) -> None:
        r = validate_breakout(_breakout_feat(), _breakout_config())
        assert r.feature_version == constants.FEATURE_SCHEMA_VERSION

    def test_config_id_propagated(self) -> None:
        r = validate_breakout(_breakout_feat(), _breakout_config())
        assert r.setup_config_id == "setup_breakout_v1"


# ===========================================================================
# Confidence field tests
# ===========================================================================

class TestConfidenceField:
    """Confidence must be an explicit field — never left implicit as setup_score."""

    def test_confidence_field_exists_on_result(self) -> None:
        r = validate_breakout(_breakout_feat(), _breakout_config())
        assert hasattr(r, "confidence")

    def test_confidence_is_string(self) -> None:
        r = validate_breakout(_breakout_feat(), _breakout_config())
        assert isinstance(r.confidence, str)

    def test_confidence_in_allowed_values(self) -> None:
        for fn, feat, cfg in [
            (validate_breakout, _breakout_feat(), _breakout_config()),
            (validate_pullback, _pullback_feat(), _pullback_config()),
            (validate_trend_continuation, _tc_feat(), _tc_config()),
            (validate_consolidation_base, _cb_feat(), _cb_config()),
        ]:
            r = fn(feat, cfg)
            assert r.confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW), (
                f"{fn.__name__} returned unexpected confidence={r.confidence!r}"
            )

    def test_high_score_yields_high_confidence(self) -> None:
        # Force a high score: loosen min_setup_score but keep all hard checks passing
        # score >= 75 → "high"
        cfg = _breakout_config(min_setup_score=0)
        feat = _breakout_feat(rvol20=3.0, range_duration=30, range_tightness_score=90.0,
                               next_resistance_level=180.0)
        r = validate_breakout(feat, cfg)
        if r.setup_score >= 75.0:
            assert r.confidence == CONFIDENCE_HIGH

    def test_medium_score_yields_medium_confidence(self) -> None:
        # A passed setup with score in [50, 75) → "medium"
        # Use a pullback that barely passes
        cfg = _pullback_config(min_setup_score=50)
        feat = _pullback_feat(rvol20=0.5, swing_low=None, next_resistance_level=None)
        r = validate_pullback(feat, cfg)
        if 50.0 <= r.setup_score < 75.0:
            assert r.confidence == CONFIDENCE_MEDIUM

    def test_low_score_yields_low_confidence(self) -> None:
        # A failed setup with score < 50 → "low"
        feat = dict(ticker="X", signal_date=SIGNAL_DATE.isoformat())
        r = validate_breakout(feat, _breakout_config())
        assert r.setup_score < 50.0
        assert r.confidence == CONFIDENCE_LOW

    def test_confidence_matches_score_thresholds_all_validators(self) -> None:
        """Verify _derive_confidence mapping is consistent with field value."""
        from app.services.screening.m14_setup_validators import _derive_confidence
        for fn, feat, cfg in [
            (validate_breakout, _breakout_feat(), _breakout_config()),
            (validate_pullback, _pullback_feat(), _pullback_config()),
            (validate_trend_continuation, _tc_feat(), _tc_config()),
            (validate_consolidation_base, _cb_feat(), _cb_config()),
        ]:
            r = fn(feat, cfg)
            assert r.confidence == _derive_confidence(r.setup_score), (
                f"{fn.__name__}: confidence={r.confidence!r} != "
                f"_derive_confidence({r.setup_score})={_derive_confidence(r.setup_score)!r}"
            )

    def test_confidence_in_evidence_json(self) -> None:
        """confidence must also appear in evidence_json for auditability."""
        for fn, feat, cfg in [
            (validate_breakout, _breakout_feat(), _breakout_config()),
            (validate_pullback, _pullback_feat(), _pullback_config()),
            (validate_trend_continuation, _tc_feat(), _tc_config()),
            (validate_consolidation_base, _cb_feat(), _cb_config()),
        ]:
            r = fn(feat, cfg)
            assert "confidence" in r.evidence_json, (
                f"{fn.__name__}: 'confidence' missing from evidence_json"
            )
            assert r.evidence_json["confidence"] == r.confidence

    def test_confidence_not_same_as_risk_label(self) -> None:
        """confidence is a validator output — it must NOT be called risk_label."""
        r = validate_breakout(_breakout_feat(), _breakout_config())
        # The field is named 'confidence', not 'risk_label'
        assert hasattr(r, "confidence")
        assert not hasattr(r, "risk_label")

    def test_confidence_deterministic(self) -> None:
        feat = _breakout_feat()
        cfg = _breakout_config()
        r1 = validate_breakout(feat, cfg)
        r2 = validate_breakout(feat, cfg)
        assert r1.confidence == r2.confidence

    def test_derive_confidence_thresholds(self) -> None:
        """Unit test _derive_confidence boundary values directly."""
        from app.services.screening.m14_setup_validators import _derive_confidence
        assert _derive_confidence(100.0) == CONFIDENCE_HIGH
        assert _derive_confidence(75.0) == CONFIDENCE_HIGH
        assert _derive_confidence(74.9) == CONFIDENCE_MEDIUM
        assert _derive_confidence(50.0) == CONFIDENCE_MEDIUM
        assert _derive_confidence(49.9) == CONFIDENCE_LOW
        assert _derive_confidence(0.0) == CONFIDENCE_LOW


# ===========================================================================
# Static analysis: no strategy-mode references
# ===========================================================================

class TestNoStrategyModeInM14:
    def _source(self) -> str:
        import app.services.screening.m14_setup_validators as mod
        import app.services.analysis.step4_setup_validation_engine as eng
        return (
            inspect.getsource(mod) + "\n" +
            inspect.getsource(eng)
        )

    def test_no_strategy_config_id(self) -> None:
        src = self._source()
        # strategy_config_id must not appear as a live key
        assert "strategy_config_id" not in src

    def test_no_aggressive_normal_conservative(self) -> None:
        src = self._source()
        for term in ("\"aggressive\"", "\"normal\"", "\"conservative\"",
                     "'aggressive'", "'normal'", "'conservative'"):
            assert term not in src

    def test_no_high_tight_flag(self) -> None:
        src = self._source()
        assert "high_tight_flag" not in src

    def test_no_volatility_squeeze(self) -> None:
        src = self._source()
        assert "volatility_squeeze" not in src

    def test_no_conservative_consolidation(self) -> None:
        src = self._source()
        assert "conservative_consolidation" not in src

    def test_no_print_statements(self) -> None:
        src = self._source()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "print":
                    pytest.fail("print() found in M14 source")

    def test_no_direct_duckdb_import(self) -> None:
        """M14 modules must not directly 'import duckdb' at the top level.
        Using duckdb_manager (which itself imports duckdb) is fine.
        """
        import app.services.screening.m14_setup_validators as validators_mod
        import app.services.analysis.step4_setup_validation_engine as engine_mod
        for mod in (validators_mod, engine_mod):
            src = inspect.getsource(mod)
            # Check for bare 'import duckdb' or 'from duckdb import' at module level
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name != "duckdb", (
                            f"Direct 'import duckdb' found in {mod.__name__}"
                        )
                elif isinstance(node, ast.ImportFrom):
                    assert node.module != "duckdb", (
                        f"Direct 'from duckdb import ...' found in {mod.__name__}"
                    )


# ===========================================================================
# Integration tests — Step4SetupValidationEngine with real DB
# ===========================================================================

@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect all DB paths into tmp_path."""
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    return {dbm.DB_ROLE_PROD: prod, dbm.DB_ROLE_DEBUG: debug, dbm.DB_ROLE_SIMULATION: simulation}


def _seed_ticker(conn: Any, ticker: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ticker_master "
        "(ticker, symbol_type, active_flag, delisted_flag) VALUES (?, 'stock', TRUE, FALSE)",
        [ticker],
    )


def _seed_price(conn: Any, ticker: str, d: date, **kw: Any) -> None:
    defaults = dict(
        open_raw=148.0, high_raw=156.5, low_raw=147.0,
        close_raw=155.0, close_adj=155.0, volume_raw=5_000_000,
        data_quality_status="ok", source_provider="fake",
    )
    defaults.update(kw)
    conn.execute(
        "INSERT OR IGNORE INTO daily_prices "
        "(ticker, date, open_raw, high_raw, low_raw, close_raw, close_adj, "
        " volume_raw, data_quality_status, source_provider, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        [
            ticker, d.isoformat(),
            defaults["open_raw"], defaults["high_raw"], defaults["low_raw"],
            defaults["close_raw"], defaults["close_adj"], defaults["volume_raw"],
            defaults["data_quality_status"], defaults["source_provider"],
        ],
    )


def _seed_feature_v02(conn: Any, ticker: str, d: date, **kw: Any) -> None:
    defaults = dict(
        feature_cutoff_date=d,
        feature_schema_version=SCHEMA_VER,
        feature_ready=True,
        avg_dollar_volume_20d=50_000_000.0,
        ema20=300.0, ema50=280.0, ema200=240.0,
        ema_alignment_score=100.0,
        ema20_slope=0.02, ema50_slope=0.015,
        distance_to_ema20_pct=0.02, distance_to_ema50_pct=0.06,
        rsi14=60.0, roc20=0.12,
        atr14=4.5, atr_pct=0.025, atr_compression_score=65.0,
        rvol20=2.0, pullback_from_recent_high_pct=-0.05,
        pullback_depth_pct=0.05,
        breakout_proximity=-0.2,
        consolidation_score=70.0,
        swing_high=320.0, swing_low=270.0,
        support_level=140.0, resistance_level=160.0, next_resistance_level=175.0,
        base_high=162.0, base_low=148.0,
        range_width_pct=0.09, range_duration=20, range_tightness_score=72.0,
        volume_dry_up_score=60.0, volume_expansion_score=80.0,
        relative_strength_vs_spy=0.05, sector_relative_strength=0.03,
        market_regime="neutral",
        days_to_earnings_bd=30, macro_event_risk_flag=False,
    )
    defaults.update(kw)
    conn.execute(
        "INSERT OR IGNORE INTO daily_features "
        "(ticker, feature_date, feature_cutoff_date, feature_schema_version, "
        " feature_ready, avg_dollar_volume_20d, "
        " ema20, ema50, ema200, ema_alignment_score, ema20_slope, ema50_slope, "
        " distance_to_ema20_pct, distance_to_ema50_pct, rsi14, roc20, "
        " atr14, atr_pct, atr_compression_score, rvol20, "
        " pullback_from_recent_high_pct, pullback_depth_pct, "
        " breakout_proximity, consolidation_score, "
        " swing_high, swing_low, support_level, resistance_level, next_resistance_level, "
        " base_high, base_low, range_width_pct, range_duration, range_tightness_score, "
        " volume_dry_up_score, volume_expansion_score, "
        " relative_strength_vs_spy, sector_relative_strength, "
        " market_regime, days_to_earnings_bd, macro_event_risk_flag, calculated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        [
            ticker, d.isoformat(),
            defaults["feature_cutoff_date"].isoformat(),
            defaults["feature_schema_version"],
            defaults["feature_ready"],
            defaults["avg_dollar_volume_20d"],
            defaults["ema20"], defaults["ema50"], defaults["ema200"],
            defaults["ema_alignment_score"],
            defaults["ema20_slope"], defaults["ema50_slope"],
            defaults["distance_to_ema20_pct"], defaults["distance_to_ema50_pct"],
            defaults["rsi14"], defaults["roc20"],
            defaults["atr14"], defaults["atr_pct"], defaults["atr_compression_score"],
            defaults["rvol20"],
            defaults["pullback_from_recent_high_pct"], defaults["pullback_depth_pct"],
            defaults["breakout_proximity"], defaults["consolidation_score"],
            defaults["swing_high"], defaults["swing_low"],
            defaults["support_level"], defaults["resistance_level"], defaults["next_resistance_level"],
            defaults["base_high"], defaults["base_low"],
            defaults["range_width_pct"], defaults["range_duration"], defaults["range_tightness_score"],
            defaults["volume_dry_up_score"], defaults["volume_expansion_score"],
            defaults["relative_strength_vs_spy"], defaults["sector_relative_strength"],
            defaults["market_regime"],
            defaults["days_to_earnings_bd"], defaults["macro_event_risk_flag"],
        ],
    )


def _seed_step3_candidate(
    conn: Any,
    ticker: str,
    d: date,
    run_id: str,
    routed_setup_types: list[str],
) -> str:
    cand_id = str(uuid.uuid4())
    snapshot = {"ticker": ticker, "close_raw": 155.0, "feature_ready": True}
    conn.execute(
        "INSERT INTO step3_candidates "
        "(candidate_id, run_id, ticker, signal_date, eligibility_score, "
        " passed_eligibility, routing_status, routing_fail_reason, "
        " eligibility_fail_reasons, routed_setup_types, feature_snapshot_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, TRUE, 'routed', NULL, '[]', ?, ?, now())",
        [
            cand_id, run_id, ticker, d.isoformat(),
            80.0,
            json.dumps(routed_setup_types),
            json.dumps(snapshot),
        ],
    )
    return cand_id


def _seed_setup_config(conn: Any, cfg: dict[str, Any]) -> None:
    import hashlib
    cj = json.dumps(cfg)
    ch = hashlib.md5(cj.encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO setup_configs "
        "(config_id, setup_type, version, config_json, config_hash, active_flag, created_at) "
        "VALUES (?, ?, ?, ?, ?, TRUE, now())",
        [cfg["config_id"], cfg["setup_type"], cfg.get("version", "v1"), cj, ch],
    )


class TestStep4SetupValidationEngineIntegration:
    """Integration tests using real DuckDB via duckdb_manager + schema_manager."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_schema("debug")

    def test_engine_db_role_guard(self, tmp_db_paths: dict[str, Path]) -> None:
        engine = Step4SetupValidationEngine()
        result = engine.run(SIGNAL_DATE, db_role="simulation", run_id=RUN_ID)
        assert result.status == "failed"
        assert any("simulation" in e.lower() or "unsupported" in e.lower() for e in result.errors)

    def test_engine_no_routed_candidates(self, tmp_db_paths: dict[str, Path]) -> None:
        engine = Step4SetupValidationEngine()
        setup_configs = {
            constants.SETUP_BREAKOUT: _breakout_config(),
        }
        result = engine.run(SIGNAL_DATE, db_role="debug", run_id=RUN_ID,
                            setup_configs=setup_configs)
        assert result.status in ("success", "success_with_warnings")
        assert result.rows_processed == 0

    def test_engine_writes_analyses_to_real_schema(self, tmp_db_paths: dict[str, Path]) -> None:
        """Full integration: seed ticker + price + feature + step3 → run → verify step4_analysis."""
        run_id = str(uuid.uuid4())
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "AAPL")
            _seed_price(conn, "AAPL", SIGNAL_DATE, close_raw=155.0, close_adj=155.0)
            _seed_feature_v02(conn, "AAPL", SIGNAL_DATE)
            _seed_step3_candidate(conn, "AAPL", SIGNAL_DATE, run_id,
                                  [constants.SETUP_BREAKOUT])
        finally:
            conn.close()

        engine = Step4SetupValidationEngine()
        result = engine.run(
            SIGNAL_DATE,
            db_role="debug",
            run_id=run_id,
            setup_configs={constants.SETUP_BREAKOUT: _breakout_config()},
        )

        assert result.status in ("success", "success_with_warnings")
        assert result.rows_processed >= 1

        # Verify row in DB
        conn2 = dbm.connect("debug", read_only=True)
        try:
            rows = conn2.execute(
                "SELECT ticker, setup_type, setup_passed, setup_score, "
                "       stop_price_raw, target_price_raw, estimated_rr "
                "FROM step4_analysis WHERE run_id = ?",
                [run_id],
            ).fetchall()
        finally:
            conn2.close()

        assert len(rows) == 1
        ticker_db, st, passed, score, stop, target, rr = rows[0]
        assert ticker_db == "AAPL"
        assert st == constants.SETUP_BREAKOUT
        assert isinstance(passed, bool)
        assert 0.0 <= score <= 100.0
        # Phase 4: stop/target/rr must be NULL
        assert stop is None
        assert target is None
        assert rr is None

    def test_engine_multi_route_ticker(self, tmp_db_paths: dict[str, Path]) -> None:
        """A ticker routed to two setups produces two analysis rows."""
        run_id = str(uuid.uuid4())
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "MSFT")
            _seed_price(conn, "MSFT", SIGNAL_DATE, close_raw=300.0, close_adj=300.0)
            _seed_feature_v02(conn, "MSFT", SIGNAL_DATE,
                               ema20=310.0, ema50=295.0, ema200=260.0,
                               pullback_depth_pct=0.07)
            _seed_step3_candidate(conn, "MSFT", SIGNAL_DATE, run_id,
                                  [constants.SETUP_BREAKOUT, constants.SETUP_PULLBACK])
        finally:
            conn.close()

        setup_configs = {
            constants.SETUP_BREAKOUT: _breakout_config(),
            constants.SETUP_PULLBACK: _pullback_config(),
        }
        engine = Step4SetupValidationEngine()
        result = engine.run(SIGNAL_DATE, db_role="debug", run_id=run_id,
                            setup_configs=setup_configs)

        assert result.rows_processed == 2

        conn2 = dbm.connect("debug", read_only=True)
        try:
            setup_types = [
                r[0] for r in conn2.execute(
                    "SELECT setup_type FROM step4_analysis WHERE run_id = ? ORDER BY setup_type",
                    [run_id],
                ).fetchall()
            ]
        finally:
            conn2.close()

        assert set(setup_types) == {constants.SETUP_BREAKOUT, constants.SETUP_PULLBACK}

    def test_engine_setup_config_id_not_null(self, tmp_db_paths: dict[str, Path]) -> None:
        run_id = str(uuid.uuid4())
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "XOM")
            _seed_price(conn, "XOM", SIGNAL_DATE)
            _seed_feature_v02(conn, "XOM", SIGNAL_DATE)
            _seed_step3_candidate(conn, "XOM", SIGNAL_DATE, run_id,
                                  [constants.SETUP_BREAKOUT])
        finally:
            conn.close()

        engine = Step4SetupValidationEngine()
        engine.run(SIGNAL_DATE, db_role="debug", run_id=run_id,
                   setup_configs={constants.SETUP_BREAKOUT: _breakout_config()})

        conn2 = dbm.connect("debug", read_only=True)
        try:
            cfg_ids = [
                r[0] for r in conn2.execute(
                    "SELECT setup_config_id FROM step4_analysis WHERE run_id = ?",
                    [run_id],
                ).fetchall()
            ]
        finally:
            conn2.close()

        assert all(c is not None and c != "" for c in cfg_ids)

    def test_engine_market_regime_null_propagated(self, tmp_db_paths: dict[str, Path]) -> None:
        run_id = str(uuid.uuid4())
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "NULL_REGIME")
            _seed_price(conn, "NULL_REGIME", SIGNAL_DATE)
            _seed_feature_v02(conn, "NULL_REGIME", SIGNAL_DATE, market_regime=None)
            _seed_step3_candidate(conn, "NULL_REGIME", SIGNAL_DATE, run_id,
                                  [constants.SETUP_BREAKOUT])
        finally:
            conn.close()

        engine = Step4SetupValidationEngine()
        engine.run(SIGNAL_DATE, db_role="debug", run_id=run_id,
                   setup_configs={constants.SETUP_BREAKOUT: _breakout_config()})

        conn2 = dbm.connect("debug", read_only=True)
        try:
            regime = conn2.execute(
                "SELECT market_regime FROM step4_analysis WHERE run_id = ?",
                [run_id],
            ).fetchone()
        finally:
            conn2.close()

        assert regime is not None
        assert regime[0] is None  # NULL preserved

    def test_engine_metadata_keys_complete(self, tmp_db_paths: dict[str, Path]) -> None:
        engine = Step4SetupValidationEngine()
        result = engine.run(SIGNAL_DATE, db_role="debug", run_id=RUN_ID,
                            setup_configs={constants.SETUP_BREAKOUT: _breakout_config()})
        for key in METADATA_KEYS:
            assert key in result.metadata, f"Missing metadata key: {key}"

    def test_engine_old_strategy_config_not_required(self, tmp_db_paths: dict[str, Path]) -> None:
        """Engine must work without any strategy_config_id in DB or configs."""
        run_id = str(uuid.uuid4())
        # No seeding of strategy_configs table (which doesn't exist in setup-mode schema)
        engine = Step4SetupValidationEngine()
        result = engine.run(SIGNAL_DATE, db_role="debug", run_id=run_id,
                            setup_configs={constants.SETUP_BREAKOUT: _breakout_config()})
        # Should not fail due to missing strategy config
        assert result.status != "failed" or (
            result.errors and "strategy" not in " ".join(result.errors).lower()
        )

    def test_engine_explanation_json_populated(self, tmp_db_paths: dict[str, Path]) -> None:
        run_id = str(uuid.uuid4())
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "EVID")
            _seed_price(conn, "EVID", SIGNAL_DATE)
            _seed_feature_v02(conn, "EVID", SIGNAL_DATE)
            _seed_step3_candidate(conn, "EVID", SIGNAL_DATE, run_id,
                                  [constants.SETUP_BREAKOUT])
        finally:
            conn.close()

        engine = Step4SetupValidationEngine()
        engine.run(SIGNAL_DATE, db_role="debug", run_id=run_id,
                   setup_configs={constants.SETUP_BREAKOUT: _breakout_config()})

        conn2 = dbm.connect("debug", read_only=True)
        try:
            raw = conn2.execute(
                "SELECT explanation_json FROM step4_analysis WHERE run_id = ?",
                [run_id],
            ).fetchone()
        finally:
            conn2.close()

        assert raw is not None
        ev = json.loads(raw[0]) if isinstance(raw[0], str) else raw[0]
        assert isinstance(ev, dict)
        assert "component_scores" in ev

    def test_engine_all_four_setups(self, tmp_db_paths: dict[str, Path]) -> None:
        """All four setup validators can be exercised via the engine."""
        run_id = str(uuid.uuid4())
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "QUAD")
            _seed_price(conn, "QUAD", SIGNAL_DATE, close_raw=175.0, close_adj=175.0)
            _seed_feature_v02(
                conn, "QUAD", SIGNAL_DATE,
                ema20=310.0, ema50=295.0, ema200=260.0,
                pullback_depth_pct=0.07,
                range_tightness_score=72.0, atr_pct=0.02,
                base_high=178.0, base_low=170.0,
                range_duration=20,
                close_raw=175.0, close_adj=175.0,  # Not used by seed but consistent
            )
            _seed_step3_candidate(
                conn, "QUAD", SIGNAL_DATE, run_id,
                list(constants.ALLOWED_SETUP_TYPES),
            )
        finally:
            conn.close()

        setup_configs = {
            constants.SETUP_BREAKOUT: _breakout_config(),
            constants.SETUP_PULLBACK: _pullback_config(),
            constants.SETUP_TREND_CONTINUATION: _tc_config(),
            constants.SETUP_CONSOLIDATION_BASE: _cb_config(),
        }
        engine = Step4SetupValidationEngine()
        result = engine.run(SIGNAL_DATE, db_role="debug", run_id=run_id,
                            setup_configs=setup_configs)

        assert result.rows_processed == 4

        conn2 = dbm.connect("debug", read_only=True)
        try:
            types_written = {
                r[0] for r in conn2.execute(
                    "SELECT setup_type FROM step4_analysis WHERE run_id = ?",
                    [run_id],
                ).fetchall()
            }
        finally:
            conn2.close()

        assert types_written == set(constants.ALLOWED_SETUP_TYPES)


class TestStep4EngineRiskLabelConfigThreading:
    """CODER_NOTE v3 item 6 — Step4SetupValidationEngine.run() threads
    risk_label_config through to validate_setup(); loads it from DB when not
    passed explicitly, exactly mirroring the existing setup_configs pattern."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, tmp_db_paths: dict[str, Path]) -> None:
        sm.apply_schema("debug")

    def _seed_and_run(
        self, run_id: str, risk_label_config: dict[str, Any] | None
    ) -> float:
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "AAPL")
            _seed_price(conn, "AAPL", SIGNAL_DATE, close_raw=155.0, close_adj=155.0)
            _seed_feature_v02(conn, "AAPL", SIGNAL_DATE, days_to_earnings_bd=2)
            _seed_step3_candidate(conn, "AAPL", SIGNAL_DATE, run_id,
                                  [constants.SETUP_BREAKOUT])
        finally:
            conn.close()

        engine = Step4SetupValidationEngine()
        result = engine.run(
            SIGNAL_DATE, db_role="debug", run_id=run_id,
            setup_configs={constants.SETUP_BREAKOUT: _breakout_config()},
            risk_label_config=risk_label_config,
        )
        assert result.status in ("success", "success_with_warnings")

        conn2 = dbm.connect("debug", read_only=True)
        try:
            row = conn2.execute(
                "SELECT earnings_penalty FROM step4_analysis WHERE run_id = ?",
                [run_id],
            ).fetchone()
        finally:
            conn2.close()
        assert row is not None
        return row[0]

    def test_explicit_risk_label_config_param_overrides_penalty(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        run_id = str(uuid.uuid4())
        override = {"earnings": {"avoid_within_bd": 10, "penalty_points_max": -30}}
        penalty = self._seed_and_run(run_id, risk_label_config=override)
        expected = -30 * (1.0 - 2 / 10)  # override's numbers, not the config's own (-15/5)
        assert penalty == pytest.approx(expected)

    def test_no_active_risk_label_config_row_falls_back_gracefully(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        """Fresh debug DB, nothing seeded into risk_label_config at all — the
        engine must still run successfully and reproduce the setup_config's
        own earnings block (unchanged from before this change)."""
        run_id = str(uuid.uuid4())
        penalty = self._seed_and_run(run_id, risk_label_config=None)
        expected = -15 * (1.0 - 2 / 5)  # _breakout_config()'s own earnings block
        assert penalty == pytest.approx(expected)

    def test_loads_active_risk_label_config_from_db_when_not_passed(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        """An active risk_label_config row with a custom shared earnings block
        must be picked up automatically when risk_label_config is omitted."""
        conn = dbm.connect("debug")
        try:
            conn.execute(
                "INSERT INTO risk_label_config "
                "(config_id, version, config_json, config_hash, active_flag, created_at, notes) "
                "VALUES (?, ?, ?, ?, TRUE, now(), 'test row')",
                [
                    "risk_label_config_test",
                    "test_v1",
                    json.dumps({"earnings": {"avoid_within_bd": 10, "penalty_points_max": -30}}),
                    "test_hash",
                ],
            )
        finally:
            conn.close()

        run_id = str(uuid.uuid4())
        penalty = self._seed_and_run(run_id, risk_label_config=None)
        expected = -30 * (1.0 - 2 / 10)  # from the DB row, not the setup_config's own
        assert penalty == pytest.approx(expected)


# ===========================================================================
# New gate tests: ATR stop floor (P1-1), pullback rebound (P1-2),
# consolidation base identified (P1-3), resistance_blocks (P2-1),
# setup independence
# ===========================================================================

class TestAtrStopFloorGate:
    """P1-1: Stop ≥ 0.5 ATR below entry gate — all four setup validators."""

    def test_breakout_stop_below_atr_floor_fails(self) -> None:
        # support very close to close → tiny stop distance → fails ATR floor
        # stop_distance_pct = (155 - 154) / 155 = 0.00645; atr_pct=0.03
        # stop_distance_atr = 0.00645 / 0.03 = 0.215 < 0.5
        feat = _breakout_feat(
            close_raw=155.0, close_adj=155.0,
            support_level=154.0,   # adj; raw ≈ 154 (adj==raw factor here)
            atr_pct=0.03,
        )
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_breakout_stop_sufficient_passes_atr_floor(self) -> None:
        # stop_distance_pct = (155 - 145) / 155 = 0.0645; atr_pct=0.03 → atr_ratio=2.15 > 0.5
        feat = _breakout_feat(
            close_raw=155.0, close_adj=155.0,
            support_level=145.0, atr_pct=0.03,
        )
        result = validate_breakout(feat, _breakout_config())
        assert not any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_breakout_atr_floor_skipped_when_support_none(self) -> None:
        feat = _breakout_feat(support_level=None, atr_pct=0.03)
        result = validate_breakout(feat, _breakout_config())
        assert not any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_breakout_atr_floor_configurable(self) -> None:
        # Loosen to 0.1 → same tiny stop now passes
        cfg = _breakout_config(min_atr_stop_floor_multiple=0.1)
        feat = _breakout_feat(
            close_raw=155.0, close_adj=155.0,
            support_level=154.0, atr_pct=0.03,
        )
        result = validate_breakout(feat, cfg)
        assert not any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_pullback_stop_below_atr_floor_fails(self) -> None:
        # support very close to close → stop_distance_atr << 0.5
        # stop_distance_pct = (300 - 299) / 300 = 0.00333; atr_pct=0.025 → 0.133 < 0.5
        feat = _pullback_feat(
            close_raw=300.0, close_adj=300.0,
            support_level=299.0, atr_pct=0.025,
        )
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False
        assert any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_pullback_stop_sufficient_passes_atr_floor(self) -> None:
        # stop_distance_pct = (300 - 292) / 300 = 0.0267; atr_pct=0.025 → 1.07 > 0.5
        feat = _pullback_feat(
            close_raw=300.0, close_adj=300.0,
            support_level=292.0, atr_pct=0.025,
        )
        result = validate_pullback(feat, _pullback_config())
        assert not any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_trend_continuation_stop_below_atr_floor_fails(self) -> None:
        # swing_low very close to close → stop_atr < 0.5
        # stop_distance_pct = (500 - 498) / 500 = 0.004; atr_pct=0.025 → 0.16 < 0.5
        feat = _tc_feat(
            close_raw=500.0, close_adj=500.0,
            swing_low=498.0, atr_pct=0.025,
        )
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is False
        assert any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_trend_continuation_stop_sufficient_passes(self) -> None:
        feat = _tc_feat(
            close_raw=500.0, close_adj=500.0,
            swing_low=460.0, atr_pct=0.025,   # atr_ratio=3.2 >> 0.5
        )
        result = validate_trend_continuation(feat, _tc_config())
        assert not any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_consolidation_base_stop_below_atr_floor_fails(self) -> None:
        # base_low very close to close → stop_atr < 0.3 (CB floor)
        # stop_distance_pct = (175 - 174.9) / 175 = 0.000571; atr_pct=0.02 → 0.029 < 0.3
        feat = _cb_feat(
            close_raw=175.0, close_adj=175.0,
            base_low=174.9, atr_pct=0.02,
        )
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_consolidation_base_stop_sufficient_passes(self) -> None:
        feat = _cb_feat(
            close_raw=175.0, close_adj=175.0,
            base_low=170.0, atr_pct=0.02,   # atr_ratio = 1.43 > 0.3
        )
        result = validate_consolidation_base(feat, _cb_config())
        assert not any("stop_below_atr_floor" in r for r in result.pass_fail_reasons)

    def test_atr_floor_gate_doesnt_block_other_setups(self) -> None:
        """ATR floor failure on pullback does NOT prevent TC from evaluating."""
        # Pullback: tiny stop → fails ATR floor
        pb_feat = _pullback_feat(
            close_raw=300.0, close_adj=300.0,
            support_level=299.5, atr_pct=0.025,
        )
        pb_result = validate_pullback(pb_feat, _pullback_config())
        assert any("stop_below_atr_floor" in r for r in pb_result.pass_fail_reasons)

        # TC: normal stop → should evaluate independently and not inherit the pullback failure
        tc_feat = _tc_feat(swing_low=460.0, atr_pct=0.025)
        tc_result = validate_trend_continuation(tc_feat, _tc_config())
        assert not any("stop_below_atr_floor" in r for r in tc_result.pass_fail_reasons)


class TestCompressionFloorGate:
    """CODER_NOTE v3 item 2: min_compression/min_dry_up wired behind the new
    opt-in enforce_compression_floor flag (default False, option b) — v1's live
    behavior must stay frozen; enforcement only fires when explicitly enabled."""

    def test_disabled_by_default_low_scores_do_not_fail(self) -> None:
        """Flag omitted (matches v1/every existing preset) -> no new hard fail,
        even though both scores sit below their configured minimums."""
        feat = _cb_feat(atr_compression_score=10.0, volume_dry_up_score=5.0)
        cfg = _cb_config(min_compression=50, min_dry_up=40)
        result = validate_consolidation_base(feat, cfg)
        assert not any("atr_compression_too_low" in r for r in result.pass_fail_reasons)
        assert not any("volume_dry_up_too_low" in r for r in result.pass_fail_reasons)

    def test_disabled_explicitly_false_low_scores_do_not_fail(self) -> None:
        feat = _cb_feat(atr_compression_score=10.0, volume_dry_up_score=5.0)
        cfg = _cb_config(min_compression=50, min_dry_up=40, enforce_compression_floor=False)
        result = validate_consolidation_base(feat, cfg)
        assert result.setup_passed is True
        assert result.pass_fail_reasons == ["passed"]

    def test_enabled_low_compression_fails(self) -> None:
        feat = _cb_feat(atr_compression_score=10.0, volume_dry_up_score=60.0)
        cfg = _cb_config(min_compression=50, min_dry_up=40, enforce_compression_floor=True)
        result = validate_consolidation_base(feat, cfg)
        assert result.setup_passed is False
        assert any("atr_compression_too_low(10.0<50.0)" in r for r in result.pass_fail_reasons)

    def test_enabled_low_dry_up_fails(self) -> None:
        feat = _cb_feat(atr_compression_score=65.0, volume_dry_up_score=5.0)
        cfg = _cb_config(min_compression=50, min_dry_up=40, enforce_compression_floor=True)
        result = validate_consolidation_base(feat, cfg)
        assert result.setup_passed is False
        assert any("volume_dry_up_too_low(5.0<40.0)" in r for r in result.pass_fail_reasons)

    def test_enabled_sufficient_scores_pass(self) -> None:
        feat = _cb_feat(atr_compression_score=65.0, volume_dry_up_score=60.0)
        cfg = _cb_config(min_compression=50, min_dry_up=40, enforce_compression_floor=True)
        result = validate_consolidation_base(feat, cfg)
        assert not any("atr_compression_too_low" in r for r in result.pass_fail_reasons)
        assert not any("volume_dry_up_too_low" in r for r in result.pass_fail_reasons)

    def test_enabled_missing_scores_fail_explicitly(self) -> None:
        feat = _cb_feat(atr_compression_score=None, volume_dry_up_score=None)
        cfg = _cb_config(enforce_compression_floor=True)
        result = validate_consolidation_base(feat, cfg)
        assert "missing_atr_compression_score" in result.pass_fail_reasons
        assert "missing_volume_dry_up_score" in result.pass_fail_reasons

    def test_v1_default_configs_behavior_unchanged(self) -> None:
        """Reproduces default_configs.py's actual v1 shape (enforce_compression_floor
        explicitly False) with scores below its own min_compression/min_dry_up
        defaults -- must behave exactly as before this change (no new hard fails)."""
        feat = _cb_feat(atr_compression_score=20.0, volume_dry_up_score=15.0)
        cfg = _cb_config(enforce_compression_floor=False)  # v1's actual value
        result = validate_consolidation_base(feat, cfg)
        assert not any("atr_compression_too_low" in r for r in result.pass_fail_reasons)
        assert not any("volume_dry_up_too_low" in r for r in result.pass_fail_reasons)


class TestPullbackReboundGate:
    """P1-2: Pullback rebound confirmation gate."""

    def test_pullback_passes_when_close_above_open(self) -> None:
        feat = _pullback_feat(close_raw=300.0, open_raw=298.0)
        result = validate_pullback(feat, _pullback_config())
        assert not any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)

    def test_pullback_passes_when_ema20_slope_positive(self) -> None:
        feat = _pullback_feat(
            close_raw=300.0, open_raw=302.0,  # bearish candle
            ema20_slope=0.01,                  # but slope is positive
        )
        result = validate_pullback(feat, _pullback_config())
        assert not any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)

    def test_pullback_passes_when_roc20_positive(self) -> None:
        feat = _pullback_feat(
            close_raw=300.0, open_raw=302.0,  # bearish candle
            ema20_slope=-0.01,                 # slope negative
            roc20=0.03,                        # but roc20 positive
        )
        result = validate_pullback(feat, _pullback_config())
        assert not any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)

    def test_pullback_fails_when_all_rebound_signals_absent(self) -> None:
        # bearish candle, negative slope, negative roc20 → no rebound signal
        feat = _pullback_feat(
            close_raw=298.0, open_raw=300.0,  # bearish candle
            ema20_slope=-0.01,
            roc20=-0.02,
        )
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False
        assert any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)

    def test_pullback_rebound_required_false_skips_check(self) -> None:
        # Config disables rebound check
        cfg = _pullback_config(rebound_required=False)
        feat = _pullback_feat(
            close_raw=298.0, open_raw=300.0,
            ema20_slope=-0.01, roc20=-0.02,
        )
        result = validate_pullback(feat, cfg)
        assert not any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)

    def test_pullback_rebound_in_evidence(self) -> None:
        feat = _pullback_feat()
        result = validate_pullback(feat, _pullback_config())
        ev = result.evidence_json
        assert "rebound_required" in ev
        assert "ema20_slope" in ev
        assert "roc20" in ev
        assert "open_raw" in ev

    def test_pullback_no_rebound_does_not_block_breakout(self) -> None:
        """Pullback rebound failure doesn't prevent breakout from evaluating independently."""
        pb_feat = _pullback_feat(
            close_raw=298.0, open_raw=300.0, ema20_slope=-0.01, roc20=-0.02,
        )
        pb_result = validate_pullback(pb_feat, _pullback_config())
        assert any("pullback_no_rebound_confirmation" in r for r in pb_result.pass_fail_reasons)

        bo_result = validate_breakout(_breakout_feat(), _breakout_config())
        assert bo_result.setup_passed is True


class TestConsolidationBaseIdentifiedGate:
    """P1-3: Base levels must be explicitly identified."""

    def test_cb_fails_when_base_high_none(self) -> None:
        feat = _cb_feat(base_high=None)
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("consolidation_base_not_identified" in r for r in result.pass_fail_reasons)

    def test_cb_fails_when_base_low_none(self) -> None:
        feat = _cb_feat(base_low=None)
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("consolidation_base_not_identified" in r for r in result.pass_fail_reasons)

    def test_cb_fails_when_both_base_levels_none(self) -> None:
        feat = _cb_feat(base_high=None, base_low=None)
        result = validate_consolidation_base(feat, _cb_config())
        assert result.setup_passed is False
        assert any("consolidation_base_not_identified" in r for r in result.pass_fail_reasons)

    def test_cb_does_not_block_breakout_when_base_missing(self) -> None:
        """Missing base levels block consolidation only — breakout evaluates independently."""
        cb_result = validate_consolidation_base(
            _cb_feat(base_high=None, base_low=None), _cb_config()
        )
        assert any("consolidation_base_not_identified" in r for r in cb_result.pass_fail_reasons)

        bo_result = validate_breakout(_breakout_feat(), _breakout_config())
        assert bo_result.setup_passed is True


class TestResistanceBlocks:
    """P2-1: resistance_blocks flag in evidence_json for breakout and trend_continuation."""

    def test_breakout_resistance_blocks_true_when_below_next_resistance(self) -> None:
        # resistance=160 sits between entry=155 and next_resistance=170 → blocks upside
        feat = _breakout_feat(
            close_raw=155.0, close_adj=155.0,
            resistance_level=160.0, next_resistance_level=170.0,
        )
        result = validate_breakout(feat, _breakout_config())
        ev = result.evidence_json
        assert "resistance_blocks" in ev
        assert ev["resistance_blocks"] is True

    def test_breakout_resistance_blocks_true_when_resistance_is_the_cap(self) -> None:
        # Only resistance above entry (no next_resistance) → resistance IS the ceiling
        feat = _breakout_feat(
            close_raw=155.0, close_adj=155.0,
            resistance_level=160.0, next_resistance_level=None,
        )
        result = validate_breakout(feat, _breakout_config())
        ev = result.evidence_json
        assert "resistance_blocks" in ev
        assert ev["resistance_blocks"] is True  # resistance IS the cap

    def test_breakout_resistance_blocks_false_when_no_resistance(self) -> None:
        feat = _breakout_feat(resistance_level=None, next_resistance_level=None)
        result = validate_breakout(feat, _breakout_config())
        ev = result.evidence_json
        assert "resistance_blocks" in ev
        assert ev["resistance_blocks"] is False

    def test_tc_resistance_blocks_in_evidence(self) -> None:
        feat = _tc_feat(
            resistance_level=520.0, next_resistance_level=560.0,
            close_raw=500.0, close_adj=500.0,
        )
        result = validate_trend_continuation(feat, _tc_config())
        ev = result.evidence_json
        assert "resistance_blocks" in ev

    def test_pullback_resistance_blocks_true_when_blocking_swing_high(self) -> None:
        # resistance=310 sits between entry=300 and swing_high=340 → blocks upside
        feat = _pullback_feat(
            close_raw=300.0, close_adj=300.0,
            resistance_level=310.0, next_resistance_level=None,
            swing_high=340.0,
        )
        result = validate_pullback(feat, _pullback_config())
        ev = result.evidence_json
        assert "resistance_blocks" in ev
        assert ev["resistance_blocks"] is True

    def test_pullback_resistance_blocks_true_when_blocking_next_resistance(self) -> None:
        # resistance=310 between entry=300 and next_resistance=340
        feat = _pullback_feat(
            close_raw=300.0, close_adj=300.0,
            resistance_level=310.0, next_resistance_level=340.0,
            swing_high=None,
        )
        result = validate_pullback(feat, _pullback_config())
        ev = result.evidence_json
        assert "resistance_blocks" in ev
        assert ev["resistance_blocks"] is True

    def test_pullback_resistance_blocks_false_when_no_resistance_above_entry(self) -> None:
        # resistance=290 < entry=300 → no blocking
        feat = _pullback_feat(
            close_raw=300.0, close_adj=300.0,
            resistance_level=290.0, next_resistance_level=None,
        )
        result = validate_pullback(feat, _pullback_config())
        ev = result.evidence_json
        assert "resistance_blocks" in ev
        assert ev["resistance_blocks"] is False


class TestSetupIndependence:
    """Verify setup-specific gate failures do NOT cascade to other setups."""

    def test_breakout_fail_no_resistance_allows_pullback_pass(self) -> None:
        """Resistance NULL fails breakout only; pullback evaluates independently and can pass."""
        bo_feat = _breakout_feat(resistance_level=None)
        bo_result = validate_breakout(bo_feat, _breakout_config())
        assert bo_result.setup_passed is False
        assert any("no_resistance_level" in r for r in bo_result.pass_fail_reasons)

        pb_result = validate_pullback(_pullback_feat(), _pullback_config())
        assert pb_result.setup_passed is True

    def test_pullback_rebound_fail_allows_consolidation_pass(self) -> None:
        """Rebound failure on pullback doesn't affect consolidation evaluation."""
        pb_feat = _pullback_feat(
            close_raw=298.0, open_raw=300.0, ema20_slope=-0.01, roc20=-0.02,
        )
        pb_result = validate_pullback(pb_feat, _pullback_config())
        assert any("pullback_no_rebound_confirmation" in r for r in pb_result.pass_fail_reasons)

        cb_result = validate_consolidation_base(_cb_feat(), _cb_config())
        assert cb_result.setup_passed is True

    def test_consolidation_no_base_allows_tc_pass(self) -> None:
        """Missing base levels fail consolidation; TC evaluates independently."""
        cb_result = validate_consolidation_base(
            _cb_feat(base_high=None, base_low=None), _cb_config()
        )
        assert cb_result.setup_passed is False

        tc_result = validate_trend_continuation(_tc_feat(), _tc_config())
        assert tc_result.setup_passed is True

    def test_all_setups_produce_independent_results(self) -> None:
        """Each validator returns a result regardless of what other validators return."""
        bo_feat = _breakout_feat(resistance_level=None)   # will fail breakout
        pb_feat = _pullback_feat()                          # should pass
        tc_feat = _tc_feat()                               # should pass
        cb_feat = _cb_feat(base_high=None)                 # will fail consolidation

        results = [
            validate_breakout(bo_feat, _breakout_config()),
            validate_pullback(pb_feat, _pullback_config()),
            validate_trend_continuation(tc_feat, _tc_config()),
            validate_consolidation_base(cb_feat, _cb_config()),
        ]

        # All validators return a result (no exception)
        assert all(isinstance(r, SetupValidationResult) for r in results)
        # Breakout and consolidation fail; pullback and TC pass
        assert results[0].setup_passed is False   # breakout: no resistance
        assert results[1].setup_passed is True    # pullback
        assert results[2].setup_passed is True    # trend_continuation
        assert results[3].setup_passed is False   # consolidation: no base


# ---------------------------------------------------------------------------
# Session-2 diagnostic fixes
# ---------------------------------------------------------------------------

class TestResistanceZeroHardFail:
    """Fix #1: resistance stored as 0.0 in DB must also be caught as missing."""

    def test_breakout_fails_when_resistance_is_zero(self) -> None:
        feat = _breakout_feat(resistance_level=0.0)
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert any("no_resistance_level" in r for r in result.pass_fail_reasons)

    def test_breakout_fails_when_resistance_is_none(self) -> None:
        feat = _breakout_feat(resistance_level=None)
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is False
        assert any("no_resistance_level" in r for r in result.pass_fail_reasons)

    def test_breakout_passes_when_resistance_is_positive(self) -> None:
        feat = _breakout_feat(resistance_level=160.0)
        result = validate_breakout(feat, _breakout_config())
        assert not any("no_resistance_level" in r for r in result.pass_fail_reasons)


class TestReboundSlopeThreshold:
    """Fix #2: ema20_slope noise floor — values below 0.002 must not satisfy the rebound gate."""

    def test_pullback_fails_when_slope_is_noise(self) -> None:
        # ema20_slope=0.001 is below min_rebound_slope=0.002 → slope signal fails
        # bearish candle + negative roc20 → all three signals absent → hard fail
        feat = _pullback_feat(
            close_raw=298.0, open_raw=300.0,
            ema20_slope=0.001,
            roc20=-0.005,
        )
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is False
        assert any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)

    def test_pullback_passes_when_slope_clears_threshold(self) -> None:
        # ema20_slope=0.003 >= 0.002 → rebound via slope despite bearish candle + neg roc
        feat = _pullback_feat(
            close_raw=298.0, open_raw=300.0,
            ema20_slope=0.003,
            roc20=-0.005,
        )
        result = validate_pullback(feat, _pullback_config())
        assert not any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)

    def test_min_rebound_slope_configurable(self) -> None:
        # With a custom high threshold of 0.01, slope=0.003 should fail
        cfg = _pullback_config(min_rebound_slope=0.01)
        feat = _pullback_feat(
            close_raw=298.0, open_raw=300.0,
            ema20_slope=0.003,
            roc20=-0.005,
        )
        result = validate_pullback(feat, cfg)
        assert result.setup_passed is False
        assert any("pullback_no_rebound_confirmation" in r for r in result.pass_fail_reasons)


class TestSwingLowGuard:
    """Fix #5: swing_low at or above current price must be nulled (invalid stop anchor)."""

    def test_pullback_nulls_swing_low_at_or_above_close(self) -> None:
        # swing_low=305 >= close_adj=300 → nulled; ATR stop falls back to None → gate skipped
        feat = _pullback_feat(close_raw=300.0, close_adj=300.0, swing_low=305.0)
        result = validate_pullback(feat, _pullback_config())
        # swing_low_adj nulled → should NOT appear as a valid value in evidence
        ev = result.evidence_json
        assert ev.get("swing_low_adj") is None

    def test_pullback_keeps_valid_swing_low_below_close(self) -> None:
        # swing_low=290 < close_adj=300 → valid, kept
        feat = _pullback_feat(close_raw=300.0, close_adj=300.0, swing_low=290.0)
        result = validate_pullback(feat, _pullback_config())
        ev = result.evidence_json
        assert ev.get("swing_low_adj") == 290.0

    def test_tc_nulls_swing_low_at_or_above_close(self) -> None:
        # swing_low=510 >= close_adj=500 → nulled; ATR stop gate skipped
        feat = _tc_feat(close_raw=500.0, close_adj=500.0, swing_low=510.0)
        result = validate_trend_continuation(feat, _tc_config())
        ev = result.evidence_json
        assert ev.get("swing_low_raw") is None


class TestEarningsHardBlockGate:
    """P1.2 (CODER_NOTE P1 batch, architect-approved hard-reject shape,
    2026-07-08): optional `earnings_hard_block` flag on the shared `earnings`
    config block, mirroring `enforce_compression_floor`'s opt-in pattern
    (default False, zero behavior change) — breakout/pullback/trend_continuation
    only; consolidation_base already has its own unconditional hard gate via
    `min_earnings_days`."""

    def _with_earnings(self, cfg: dict[str, Any], **earnings_overrides: Any) -> dict[str, Any]:
        cfg["earnings"] = {**cfg["earnings"], **earnings_overrides}
        return cfg

    # --- breakout ---

    def test_breakout_disabled_by_default_within_window_soft_penalty_only(self) -> None:
        feat = _breakout_feat(days_to_earnings_bd=3)  # within default avoid_within_bd=5
        result = validate_breakout(feat, _breakout_config())
        assert result.setup_passed is True
        assert not any("earnings_too_close" in r for r in result.pass_fail_reasons)

    def test_breakout_enabled_within_window_hard_fails(self) -> None:
        feat = _breakout_feat(days_to_earnings_bd=3)
        cfg = self._with_earnings(_breakout_config(), earnings_hard_block=True)
        result = validate_breakout(feat, cfg)
        assert result.setup_passed is False
        assert any("earnings_too_close(3bd<=5bd)" in r for r in result.pass_fail_reasons)

    def test_breakout_enabled_outside_window_passes(self) -> None:
        feat = _breakout_feat(days_to_earnings_bd=20)
        cfg = self._with_earnings(_breakout_config(), earnings_hard_block=True)
        result = validate_breakout(feat, cfg)
        assert result.setup_passed is True
        assert not any("earnings_too_close" in r for r in result.pass_fail_reasons)

    # --- pullback ---

    def test_pullback_disabled_by_default_within_window_soft_penalty_only(self) -> None:
        feat = _pullback_feat(days_to_earnings_bd=3)
        result = validate_pullback(feat, _pullback_config())
        assert result.setup_passed is True
        assert not any("earnings_too_close" in r for r in result.pass_fail_reasons)

    def test_pullback_enabled_within_window_hard_fails(self) -> None:
        feat = _pullback_feat(days_to_earnings_bd=3)
        cfg = self._with_earnings(_pullback_config(), earnings_hard_block=True)
        result = validate_pullback(feat, cfg)
        assert result.setup_passed is False
        assert any("earnings_too_close(3bd<=5bd)" in r for r in result.pass_fail_reasons)

    def test_pullback_enabled_outside_window_passes(self) -> None:
        feat = _pullback_feat(days_to_earnings_bd=30)
        cfg = self._with_earnings(_pullback_config(), earnings_hard_block=True)
        result = validate_pullback(feat, cfg)
        assert result.setup_passed is True
        assert not any("earnings_too_close" in r for r in result.pass_fail_reasons)

    # --- trend_continuation ---

    def test_tc_disabled_by_default_within_window_soft_penalty_only(self) -> None:
        feat = _tc_feat(days_to_earnings_bd=3)
        result = validate_trend_continuation(feat, _tc_config())
        assert result.setup_passed is True
        assert not any("earnings_too_close" in r for r in result.pass_fail_reasons)

    def test_tc_enabled_within_window_hard_fails(self) -> None:
        feat = _tc_feat(days_to_earnings_bd=3)
        cfg = self._with_earnings(_tc_config(), earnings_hard_block=True)
        result = validate_trend_continuation(feat, cfg)
        assert result.setup_passed is False
        assert any("earnings_too_close(3bd<=5bd)" in r for r in result.pass_fail_reasons)

    def test_tc_enabled_outside_window_passes(self) -> None:
        feat = _tc_feat(days_to_earnings_bd=30)
        cfg = self._with_earnings(_tc_config(), earnings_hard_block=True)
        result = validate_trend_continuation(feat, cfg)
        assert result.setup_passed is True
        assert not any("earnings_too_close" in r for r in result.pass_fail_reasons)

    # --- v1 default configs behavior unchanged (byte-identical proof) ---

    def test_v1_default_configs_have_no_earnings_hard_block_key(self) -> None:
        """default_configs.py's _EARNINGS_BLOCK carries no earnings_hard_block
        key at all (not even False) — confirms the flag is purely opt-in via
        .get(..., False), not a new required field on every existing config."""
        from app.services.config.default_configs import DEFAULT_SETUP_CONFIGS

        for setup_type in ("breakout", "pullback", "trend_continuation"):
            assert "earnings_hard_block" not in DEFAULT_SETUP_CONFIGS[setup_type]["earnings"]
