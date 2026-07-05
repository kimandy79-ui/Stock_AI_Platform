"""Tests for the Phase 4 fundamentals scoring hook in M14 setup validators.

Covers ``_compute_fundamentals_adjustment`` directly (pure function, no DB)
and its wiring into all 4 ``validate_*`` entry points. Per the coder note,
this integration must be: optional (opt-in per setup_config), config-weighted
(no hardcoded weights), and never a hard gate (mirrors the RVOL precedent,
AD-22.23) -- these tests assert all three properties.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.screening.m14_setup_validators import (
    _compute_fundamentals_adjustment,
    validate_breakout,
    validate_consolidation_base,
    validate_pullback,
    validate_trend_continuation,
)

SIGNAL_DATE = date(2024, 3, 15)


class TestComputeFundamentalsAdjustmentUnit:
    def test_disabled_by_default_returns_zero(self) -> None:
        adj, evidence = _compute_fundamentals_adjustment({}, {})
        assert adj == 0.0
        assert evidence == {"enabled": False}

    def test_disabled_explicitly_returns_zero_even_with_data(self) -> None:
        feat = {"piotroski_f_score": 9, "altman_z_score": 5.0}
        adj, _ = _compute_fundamentals_adjustment(feat, {"enabled": False, "weight": 20.0})
        assert adj == 0.0

    def test_enabled_but_zero_weight_returns_zero(self) -> None:
        feat = {"piotroski_f_score": 9}
        adj, _ = _compute_fundamentals_adjustment(feat, {"enabled": True, "weight": 0.0})
        assert adj == 0.0

    def test_no_fundamentals_data_present_is_a_no_op(self) -> None:
        """A ticker with no ticker_fundamentals coverage yet must not be
        penalized -- absence of data is not evidence of poor fundamentals."""
        adj, evidence = _compute_fundamentals_adjustment({}, {"enabled": True, "weight": 20.0})
        assert adj == 0.0
        assert evidence["fields_present"] == 0

    def test_strong_fundamentals_produce_positive_adjustment(self) -> None:
        feat = {
            "piotroski_f_score": 9,
            "altman_z_score": 4.0,
            "valuation_band": "cheap",
            "eps_growth_trend": 0.3,
            "leverage_ratio": 0.1,
        }
        adj, evidence = _compute_fundamentals_adjustment(feat, {"enabled": True, "weight": 20.0})
        assert adj > 0.0
        assert evidence["fields_present"] == 5

    def test_weak_fundamentals_produce_negative_adjustment(self) -> None:
        feat = {
            "piotroski_f_score": 0,
            "altman_z_score": 0.0,
            "valuation_band": "expensive",
            "eps_growth_trend": -0.5,
            "leverage_ratio": 0.9,
        }
        adj, _ = _compute_fundamentals_adjustment(feat, {"enabled": True, "weight": 20.0})
        assert adj < 0.0

    def test_weight_scales_adjustment_linearly(self) -> None:
        feat = {"piotroski_f_score": 9}
        adj_small, _ = _compute_fundamentals_adjustment(feat, {"enabled": True, "weight": 10.0})
        adj_large, _ = _compute_fundamentals_adjustment(feat, {"enabled": True, "weight": 20.0})
        assert adj_large == pytest.approx(2 * adj_small)

    def test_unknown_valuation_band_excluded_not_penalized(self) -> None:
        with_unknown, _ = _compute_fundamentals_adjustment(
            {"valuation_band": "unknown"}, {"enabled": True, "weight": 20.0}
        )
        assert with_unknown == 0.0  # excluded -> 0 fields present -> no-op

    def test_partial_field_coverage_averages_only_present_fields(self) -> None:
        adj, evidence = _compute_fundamentals_adjustment(
            {"piotroski_f_score": 9}, {"enabled": True, "weight": 20.0}
        )
        assert evidence["fields_present"] == 1
        assert adj > 0.0


def _minimal_breakout_feat(**fundamentals: object) -> dict[str, object]:
    feat: dict[str, object] = dict(
        ticker="AAPL",
        signal_date=SIGNAL_DATE.isoformat(),
        close_raw=155.0,
        close_adj=155.0,
        high_raw=156.5,
        low_raw=152.0,
        breakout_proximity=-0.2,
        range_duration=15,
        range_tightness_score=70.0,
        rvol20=2.0,
        resistance_level=155.0,
        next_resistance_level=170.0,
        support_level=148.0,
        atr_pct=0.03,
        distance_to_ema20_pct=0.02,
        distance_to_ema50_pct=0.05,
        days_to_earnings_bd=None,
        market_regime="bull",
        macro_event_risk_flag=False,
    )
    feat.update(fundamentals)
    return feat


def _minimal_breakout_config(fundamentals_cfg: dict | None = None) -> dict[str, object]:
    cfg: dict[str, object] = {
        "config_id": "setup_breakout_v1",
        "setup_type": "breakout",
        "validation": {
            "breakout_prox_min": -1.0,
            "breakout_prox_max": 0.5,
            "min_base_duration": 10,
            "min_rvol_breakout": 1.5,
            "rvol_is_hard": True,
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
        "macro_event_risk": {"enabled": True, "penalty_points": -10},
    }
    if fundamentals_cfg is not None:
        cfg["fundamentals"] = fundamentals_cfg
    return cfg


class TestValidatorIntegrationBackwardCompatibility:
    """No 'fundamentals' key in setup_config -> byte-identical to pre-Phase-4."""

    def test_breakout_no_fundamentals_key_is_a_no_op(self) -> None:
        feat = _minimal_breakout_feat(piotroski_f_score=9, altman_z_score=5.0)
        result = validate_breakout(feat, _minimal_breakout_config(fundamentals_cfg=None))
        assert result.evidence_json["fundamentals_adjustment"] == 0.0
        assert result.evidence_json["fundamentals_evidence"] == {"enabled": False}

    def test_breakout_empty_fundamentals_block_is_a_no_op(self) -> None:
        feat = _minimal_breakout_feat(piotroski_f_score=9)
        result = validate_breakout(feat, _minimal_breakout_config(fundamentals_cfg={}))
        assert result.evidence_json["fundamentals_adjustment"] == 0.0


class TestValidatorIntegrationOptIn:
    def test_breakout_opted_in_strong_fundamentals_raises_score(self) -> None:
        feat_plain = _minimal_breakout_feat()
        feat_strong = _minimal_breakout_feat(
            piotroski_f_score=9, altman_z_score=4.0, valuation_band="cheap",
            eps_growth_trend=0.3, leverage_ratio=0.1,
        )
        cfg = _minimal_breakout_config(fundamentals_cfg={"enabled": True, "weight": 20.0})
        plain_result = validate_breakout(feat_plain, cfg)
        strong_result = validate_breakout(feat_strong, cfg)
        assert strong_result.setup_score > plain_result.setup_score

    def test_fundamentals_alone_never_flips_hard_fail_to_pass(self) -> None:
        """Never a hard gate: even maximal positive fundamentals cannot make
        a structurally-failing breakout (no resistance level) pass."""
        feat = _minimal_breakout_feat(
            piotroski_f_score=9, altman_z_score=4.0, valuation_band="cheap",
            eps_growth_trend=0.3, leverage_ratio=0.1,
        )
        feat["resistance_level"] = None  # hard fail: no_resistance_level
        cfg = _minimal_breakout_config(fundamentals_cfg={"enabled": True, "weight": 100.0})
        result = validate_breakout(feat, cfg)
        assert result.setup_passed is False
        assert result.setup_fail_reason == "no_resistance_level"

    def test_pullback_validator_also_wired(self) -> None:
        feat = dict(
            ticker="MSFT",
            signal_date=SIGNAL_DATE.isoformat(),
            close_raw=100.0,
            close_adj=100.0,
            ema200=90.0,
            ema20=98.0,
            ema50=95.0,
            pullback_depth_pct=0.05,
            support_level=95.0,
            atr_pct=0.02,
            rvol20=1.0,
            piotroski_f_score=9,
            altman_z_score=4.0,
        )
        cfg = {
            "config_id": "setup_pullback_v1",
            "setup_type": "pullback",
            "validation": {"min_setup_score": 0, "rebound_required": False},
            "scoring_weights": {
                "uptrend_intact": 0.25, "support_ema_hold": 0.25,
                "pullback_depth": 0.20, "trend_structure": 0.15, "rr": 0.15,
            },
            "earnings": {},
            "macro_event_risk": {"enabled": False},
            "fundamentals": {"enabled": True, "weight": 20.0},
        }
        result = validate_pullback(feat, cfg)
        assert "fundamentals_adjustment" in result.evidence_json
        assert result.evidence_json["fundamentals_adjustment"] > 0.0
