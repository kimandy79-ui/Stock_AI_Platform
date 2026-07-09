"""P2.1 [HC->CFG] promotion — byte-identical behavior proof.

Promotes four previously-hardcoded value groups to config objects:
  1. Routing pre-filter thresholds  -> universe.routing (step3)
  2. eligibility_score weights       -> universe.eligibility_score_weights (step3)
  3. Confidence label thresholds     -> risk_label_config.scoring.confidence (m14)
  4. Valuation-band quality map      -> risk_label_config.scoring.valuation_band_quality (m14)

The promotion is behavior-preserving by construction: each module keeps its
literal constants as the *default*, config only overrides. This file proves
byte-identical behavior three ways:
  (A) drift guard — the seeded default config equals the module constants, so
      the seed can never silently diverge from the code default;
  (B) equivalence — a fixed golden dataset produces identical outputs whether
      config is absent (module defaults) or the seeded default config is
      supplied;
  (C) golden snapshot — hardcoded expected outputs, so a future change to the
      literals themselves is caught, not just a config/code mismatch.
"""

from __future__ import annotations

import pytest

from app.services.screening import step3_universal_eligibility as s3
from app.services.screening import m14_setup_validators as m14
from app.services.config.default_configs import (
    DEFAULT_SETUP_CONFIGS,
    DEFAULT_RISK_LABEL_CONFIG,
)

_SEEDED_UNIVERSE = DEFAULT_SETUP_CONFIGS["breakout"]["universe"]


# --------------------------------------------------------------------------- #
# Golden routing rows — one per setup type, plus a no-route and a boundary row
# --------------------------------------------------------------------------- #
def _routing_row(**overrides) -> dict:
    base = dict(
        breakout_proximity=None, range_duration=None,
        close_adj=None, ema200=None, ema20=None, ema50=None,
        pullback_from_recent_high_pct=None, ema_alignment_score=None,
        ema50_slope=None, range_tightness_score=None,
    )
    base.update(overrides)
    return base


_ROUTING_GOLDEN: list[tuple[dict, list[str]]] = [
    # routes breakout only
    (_routing_row(breakout_proximity=-0.5, range_duration=15), ["breakout"]),
    # boundary: bp == -1.0 and rd == 10 exactly (>= comparisons) still routes
    (_routing_row(breakout_proximity=-1.0, range_duration=10), ["breakout"]),
    # just below breakout boundary -> no route
    (_routing_row(breakout_proximity=-1.01, range_duration=9), []),
    # routes consolidation_base only
    (_routing_row(range_tightness_score=60.0, range_duration=20), ["consolidation_base"]),
    # routes pullback only
    (
        _routing_row(close_adj=100.0, ema200=90.0, pullback_from_recent_high_pct=-0.10,
                     ema20=98.0, ema50=95.0),
        ["pullback"],
    ),
    # routes trend_continuation only
    (
        _routing_row(ema_alignment_score=60.0, ema50_slope=0.01, close_adj=100.0, ema50=95.0),
        ["trend_continuation"],
    ),
    # nothing set -> no route
    (_routing_row(), []),
]


class TestDriftGuard:
    """(A) Seeded default config must equal the module-level constants."""

    def test_universe_routing_seed_matches_module_defaults(self):
        resolved = s3._resolve_routing_thresholds(_SEEDED_UNIVERSE)
        assert resolved == s3._DEFAULT_ROUTING_THRESHOLDS

    def test_universe_eligibility_weights_seed_matches_module_defaults(self):
        resolved = s3._resolve_eligibility_weights(_SEEDED_UNIVERSE)
        assert resolved == s3._DEFAULT_ELIGIBILITY_SCORE_WEIGHTS

    def test_confidence_seed_matches_module_defaults(self):
        high, medium = m14._resolve_confidence_thresholds(DEFAULT_RISK_LABEL_CONFIG)
        assert high == m14._CONFIDENCE_HIGH_THRESHOLD == 75.0
        assert medium == m14._CONFIDENCE_MEDIUM_THRESHOLD == 50.0

    def test_valuation_band_seed_matches_module_defaults(self):
        resolved = m14._resolve_valuation_band_quality(DEFAULT_RISK_LABEL_CONFIG)
        assert resolved == m14._VALUATION_BAND_QUALITY


class TestRoutingEquivalence:
    """(B)+(C) routing identical for absent vs seeded config, and matches
    the hardcoded golden expectation."""

    @pytest.mark.parametrize("row,expected", _ROUTING_GOLDEN)
    def test_absent_vs_seeded_and_golden(self, row, expected):
        # absent config -> module-default thresholds
        default_out = s3._evaluate_routing(row)
        # seeded config -> resolved thresholds
        seeded = s3._resolve_routing_thresholds(_SEEDED_UNIVERSE)
        seeded_out = s3._evaluate_routing(row, seeded)
        assert default_out == seeded_out == expected


class TestEligibilityScoreEquivalence:
    """(B)+(C) eligibility score identical for absent vs seeded weights."""

    _POOL_DVOLS = [10_000_000.0, 50_000_000.0, 100_000_000.0]
    _POOL_PRICES = [10.0, 50.0, 100.0]

    def test_absent_vs_seeded_and_golden_value(self):
        row = {"avg_dollar_volume_20d": 50_000_000.0, "close_raw": 50.0}
        default_out = s3._compute_eligibility_score(row, self._POOL_DVOLS, self._POOL_PRICES)
        seeded_weights = s3._resolve_eligibility_weights(_SEEDED_UNIVERSE)
        seeded_out = s3._compute_eligibility_score(
            row, self._POOL_DVOLS, self._POOL_PRICES, seeded_weights
        )
        # liq_norm = price_norm = 100*(50-10)/(100-10) = 44.4444...
        # score = 0.5*44.4444 + 0.3*44.4444 + 0.2*100 = 55.5556...
        expected = 0.5 * (100.0 * 40 / 90) + 0.3 * (100.0 * 40 / 90) + 0.2 * 100.0
        assert default_out == seeded_out == pytest.approx(expected, abs=1e-9)

    def test_none_inputs_return_none_both_paths(self):
        row = {"avg_dollar_volume_20d": None, "close_raw": 50.0}
        assert s3._compute_eligibility_score(row, self._POOL_DVOLS, self._POOL_PRICES) is None
        seeded = s3._resolve_eligibility_weights(_SEEDED_UNIVERSE)
        assert s3._compute_eligibility_score(row, self._POOL_DVOLS, self._POOL_PRICES, seeded) is None


class TestConfidenceEquivalence:
    """(B)+(C) confidence identical for absent vs seeded thresholds."""

    @pytest.mark.parametrize("score,expected", [
        (80.0, m14.CONFIDENCE_HIGH),
        (75.0, m14.CONFIDENCE_HIGH),      # boundary >=75
        (74.9, m14.CONFIDENCE_MEDIUM),
        (50.0, m14.CONFIDENCE_MEDIUM),    # boundary >=50
        (49.9, m14.CONFIDENCE_LOW),
        (0.0, m14.CONFIDENCE_LOW),
    ])
    def test_absent_vs_seeded_and_golden(self, score, expected):
        default_out = m14._derive_confidence(score)
        high, medium = m14._resolve_confidence_thresholds(DEFAULT_RISK_LABEL_CONFIG)
        seeded_out = m14._derive_confidence(score, high, medium)
        assert default_out == seeded_out == expected


class TestValuationBandEquivalence:
    """(B)+(C) fundamentals valuation contribution identical absent vs seeded."""

    def _feat(self, band: str) -> dict:
        # only valuation_band present -> avg_quality == that band's score
        return {"valuation_band": band}

    @pytest.mark.parametrize("band,band_quality", [
        ("cheap", 100.0),
        ("fair", 60.0),
        ("expensive", 20.0),
    ])
    def test_absent_vs_seeded_and_golden(self, band, band_quality):
        fund_cfg = {"enabled": True, "weight": 10.0}
        # absent scoring config -> module default map
        adj_default, _ = m14._compute_fundamentals_adjustment(self._feat(band), fund_cfg)
        seeded_map = m14._resolve_valuation_band_quality(DEFAULT_RISK_LABEL_CONFIG)
        adj_seeded, _ = m14._compute_fundamentals_adjustment(
            self._feat(band), fund_cfg, seeded_map
        )
        # adjustment = weight * (avg_quality - 50) / 50, avg_quality == band score
        expected = 10.0 * (band_quality - 50.0) / 50.0
        assert adj_default == adj_seeded == pytest.approx(expected, abs=1e-9)

    def test_unknown_band_excluded_both_paths(self):
        fund_cfg = {"enabled": True, "weight": 10.0}
        feat = {"valuation_band": "unknown"}
        adj_default, ev_default = m14._compute_fundamentals_adjustment(feat, fund_cfg)
        seeded_map = m14._resolve_valuation_band_quality(DEFAULT_RISK_LABEL_CONFIG)
        adj_seeded, ev_seeded = m14._compute_fundamentals_adjustment(feat, fund_cfg, seeded_map)
        # "unknown" absent from both maps -> no fields present -> adjustment 0.0
        assert adj_default == adj_seeded == 0.0
        assert ev_default["fields_present"] == ev_seeded["fields_present"] == 0
