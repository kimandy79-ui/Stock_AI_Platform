"""P2.3 — vcp_sequence_score (features_v04).

Measures *progressive* contraction inside the base window: successively
shallower pullbacks on successively drier volume. The point of the feature is
that ``atr_compression_score`` / ``volume_dry_up_score`` cannot see this -- both
are single-window scalars, so a flat quiet range and a genuine tightening coil
read identically. The discrimination test below is therefore the load-bearing
one; the rest guard the NULL contract and orthogonality.

Dormant field: nothing in Step 3/4/5 reads it. Fully offline.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.services.features import feature_engine as fe

_EPS = 0.05


# --------------------------------------------------------------------------- #
# Synthetic price-path builders
# --------------------------------------------------------------------------- #
def _segment(a: float, b: float, n: int) -> list[float]:
    return [a + (b - a) * (i + 1) / n for i in range(n)]


def _path(waypoints: list[float], lengths: list[int]) -> list[float]:
    """Piecewise-linear path through *waypoints*, excluding the seed point.

    Segment lengths are chosen by callers so each bar moves a similar distance;
    that keeps per-bar true range uniform, which is what lets ``_find_base_window``
    treat the whole coil as one low-volatility run rather than clipping the
    deepest leg.
    """
    pts = [waypoints[0]]
    for (a, b), n in zip(zip(waypoints, waypoints[1:]), lengths):
        pts.extend(_segment(a, b, n))
    return pts[1:]


def _volatile_prefix(n: int = 60) -> tuple[list[float], list[int]]:
    """High-true-range lead-in, so the coil is the quiet run, not this."""
    return ([100.0 + (8.0 if i % 2 else -8.0) for i in range(n)], [1_000_000] * n)


def _frame(prices: list[float], volumes: list[int]) -> pl.DataFrame:
    n = len(prices)
    return pl.DataFrame(
        {
            "date": [date(2024, 1, 1) + timedelta(days=i) for i in range(n)],
            "high_adj": [p + _EPS for p in prices],
            "low_adj": [p - _EPS for p in prices],
            "close_adj": list(prices),
            "volume_raw": list(volumes),
        }
    )


def _vcp_frame() -> pl.DataFrame:
    """Contractions 20% -> 10% -> 4.5%, on progressively drier volume."""
    pre, pre_v = _volatile_prefix()
    coil = _path([100, 80, 100, 90, 100, 95.5, 100], [16, 16, 8, 8, 3, 3])
    coil_v = [900_000] * 32 + [600_000] * 16 + [300_000] * (len(coil) - 48)
    return _frame(pre + coil, pre_v + coil_v)


def _flat_base_frame() -> pl.DataFrame:
    """Equal-depth 5% swings on flat volume: tight, quiet, and NOT a VCP."""
    pre, pre_v = _volatile_prefix()
    coil = _path([100, 95, 100, 95, 100, 95, 100], [10] * 6)
    coil_v = [500_000] * len(coil)
    return _frame(pre + coil, pre_v + coil_v)


def _from_deltas(deltas: list[float], start: float = 1000.0) -> list[float]:
    prices = [start]
    for delta in deltas:
        prices.append(prices[-1] + delta)
    return prices


def _no_base_window_prices() -> list[float]:
    """Quiet bars never adjacent: every qualifying run is 1 bar, so no base."""
    deltas = []
    for i in range(119):
        magnitude = 0.5 if i % 2 == 0 else 60.0
        deltas.append(magnitude if (i // 2) % 2 == 0 else -magnitude)
    return _from_deltas(deltas)


def _short_base_prices() -> list[float]:
    """Quiet series broken by a spike every 7 bars: longest run is 6 bars."""
    deltas = [
        40.0 if i % 7 == 6 else 0.4 * (1 if i % 2 else -1) for i in range(119)
    ]
    return _from_deltas(deltas)


def _single_leg_prices() -> list[float]:
    """A long-enough base window holding exactly one high->low contraction."""
    spikes = [40.0 * (1 if i % 2 else -1) for i in range(60)]
    lead_in = [40.0 * (1 if i % 2 else -1) for i in range(20)]
    quiet = [0.5] * 10 + [-0.25] * 20 + [0.5] * 9  # up, down, up -> one H->L
    return _from_deltas(spikes + lead_in + quiet)


# --------------------------------------------------------------------------- #
# The load-bearing test: it must tell a coil from a flat base.
# --------------------------------------------------------------------------- #
class TestDiscrimination:
    def test_vcp_scores_far_above_a_flat_tight_range(self):
        vcp = fe._compute_vcp_sequence_score(_vcp_frame())
        flat = fe._compute_vcp_sequence_score(_flat_base_frame())

        assert vcp is not None and flat is not None
        assert vcp > 80.0, vcp
        assert flat < 40.0, flat
        assert vcp - flat > 40.0

    def test_flat_base_earns_credit_only_for_non_rising_volume(self):
        """A flat base contracts by 0% and drops no volume: its whole score is
        the volume term's 'did not rise' credit. Pins why it lands near 25."""
        flat = fe._compute_vcp_sequence_score(_flat_base_frame())
        assert flat == pytest.approx(100.0 * fe._VCP_W_VOLUME)

    def test_rising_volume_scores_below_flat_volume(self):
        pre, pre_v = _volatile_prefix()
        coil = _path([100, 80, 100, 90, 100, 95.5, 100], [16, 16, 8, 8, 3, 3])
        drying = _frame(pre + coil, pre_v + [900_000] * 32 + [600_000] * 16 + [300_000] * (len(coil) - 48))
        swelling = _frame(pre + coil, pre_v + [300_000] * 32 + [600_000] * 16 + [900_000] * (len(coil) - 48))

        assert fe._compute_vcp_sequence_score(drying) > fe._compute_vcp_sequence_score(swelling)

    def test_score_is_bounded_0_100(self):
        for frame in (_vcp_frame(), _flat_base_frame()):
            score = fe._compute_vcp_sequence_score(frame)
            assert 0.0 <= score <= 100.0


# --------------------------------------------------------------------------- #
# NULL contract — "not measurable", never a silent 0.0 / false pass.
# --------------------------------------------------------------------------- #
class TestNullOnShortOrUnmeasurableBase:
    def test_short_base_returns_none_not_zero(self):
        """A base shorter than the sequencing window has no sequence to judge.
        NULL is the honest answer; 0.0 would read as 'measured, and bad'."""
        prices = _short_base_prices()
        frame = _frame(prices, [500_000] * len(prices))

        window = fe._find_base_window(frame)
        assert window is not None, "precondition: a base exists, it is merely short"
        assert (window[1] - window[0]) < fe._VCP_MIN_BASE_BARS

        score = fe._compute_vcp_sequence_score(frame)
        assert score is None
        assert score != 0.0  # the distinction the schema comment promises

    def test_short_base_still_yields_the_ordinary_base_features(self):
        """NULL vcp_sequence_score must not suppress range_duration et al -- the
        base exists, only its *sequence* is unmeasurable."""
        prices = _short_base_prices()
        frame = _frame(prices, [500_000] * len(prices))
        base_high, base_low, range_duration, _, _ = fe._compute_base(frame)
        assert base_high is not None and base_low is not None
        assert range_duration is not None and range_duration < fe._VCP_MIN_BASE_BARS

    def test_single_leg_base_returns_none(self):
        """One identifiable contraction is not a sequence (needs _VCP_MIN_LEGS)."""
        prices = _single_leg_prices()
        frame = _frame(prices, [500_000] * len(prices))

        window = fe._find_base_window(frame)
        assert window is not None and (window[1] - window[0]) >= fe._VCP_MIN_BASE_BARS
        pivots = fe._base_scoped_pivots(
            [p + _EPS for p in prices], [p - _EPS for p in prices], window[0], window[1]
        )
        legs = fe._extract_contraction_legs(pivots, [500_000.0] * len(prices))
        assert len(legs) == 1, legs

        assert fe._compute_vcp_sequence_score(frame) is None

    def test_insufficient_history_returns_none(self):
        prices = [100.0 + i * 0.1 for i in range(30)]
        frame = _frame(prices, [500_000] * 30)
        assert fe._compute_vcp_sequence_score(frame) is None

    def test_no_base_window_returns_none(self):
        """Quiet bars never adjacent -> every qualifying run is 1 bar -> no base."""
        prices = _no_base_window_prices()
        frame = _frame(prices, [500_000] * len(prices))
        assert fe._find_base_window(frame) is None
        assert fe._compute_vcp_sequence_score(frame) is None


# --------------------------------------------------------------------------- #
# Orthogonality: it cannot be reading the existing compression scores.
# --------------------------------------------------------------------------- #
class TestIndependenceFromExistingScores:
    def test_computes_from_ohlcv_alone(self):
        """Structural proof of independence: the function is handed a frame that
        contains no atr_compression_score / volume_dry_up_score column at all, so
        it cannot consult them. Mirrors the rs_percentile_126d independence test."""
        frame = _vcp_frame()
        assert set(frame.columns) == {
            "date", "high_adj", "low_adj", "close_adj", "volume_raw",
        }
        assert fe._compute_vcp_sequence_score(frame) is not None

    def test_existing_scores_are_not_inputs_to_the_formula(self):
        """Same OHLCV, different (irrelevant) extra columns -> identical score."""
        frame = _vcp_frame()
        polluted = frame.with_columns(
            pl.lit(999.0).alias("atr_compression_score"),
            pl.lit(999.0).alias("volume_dry_up_score"),
        )
        assert fe._compute_vcp_sequence_score(frame) == fe._compute_vcp_sequence_score(polluted)


# --------------------------------------------------------------------------- #
# Leg segmentation internals.
# --------------------------------------------------------------------------- #
class TestLegExtraction:
    def test_collapses_repeated_same_kind_pivots(self):
        pivots = [(0, "H", 100.0), (2, "H", 105.0), (4, "L", 90.0)]
        legs = fe._extract_contraction_legs(pivots, [1_000.0] * 6)
        # The higher H wins, so depth is measured from 105, not 100.
        assert len(legs) == 1
        assert legs[0][0] == pytest.approx((105.0 - 90.0) / 105.0)

    def test_low_before_high_is_not_a_leg(self):
        pivots = [(0, "L", 90.0), (2, "H", 100.0)]
        assert fe._extract_contraction_legs(pivots, [1_000.0] * 4) == []

    def test_leg_volume_is_the_mean_over_its_span(self):
        pivots = [(0, "H", 100.0), (2, "L", 90.0)]
        legs = fe._extract_contraction_legs(pivots, [100.0, 200.0, 300.0])
        assert legs[0][1] == pytest.approx(200.0)

    def test_degenerate_leg_is_skipped(self):
        pivots = [(0, "H", 100.0), (2, "L", 100.0)]  # zero depth
        assert fe._extract_contraction_legs(pivots, [1_000.0] * 3) == []

    def test_pivots_never_reach_outside_the_base_window(self):
        """Neighbour bars must lie inside [start, end); a pivot at the very edge
        cannot be confirmed. This is why a coil whose first peak sits on the
        base's opening bar loses that leg -- documented, not accidental."""
        highs = [float(x) for x in [1, 9, 1, 1, 1, 1, 9, 1]]
        lows = [float(x) for x in [1, 1, 1, 0, 1, 1, 1, 1]]
        pivots = fe._base_scoped_pivots(highs, lows, 2, 6, k=1)
        assert all(2 < idx < 5 for idx, _, _ in pivots), pivots


class TestBaseWindowExtractionIsBehaviourPreserving:
    def test_compute_base_and_vcp_agree_on_the_window(self):
        """_compute_base was refactored onto _find_base_window; range_duration
        must still equal that window's length."""
        frame = _vcp_frame()
        window = fe._find_base_window(frame)
        _, _, range_duration, _, _ = fe._compute_base(frame)
        assert window is not None
        assert range_duration == window[1] - window[0]

    def test_no_window_means_no_base_features(self):
        prices = _no_base_window_prices()
        frame = _frame(prices, [500_000] * len(prices))
        assert fe._find_base_window(frame) is None
        assert fe._compute_base(frame) == (None, None, None, None, None)
