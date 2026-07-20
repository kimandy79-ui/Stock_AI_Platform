"""Tests for Module 11 — features_v03 addition: rs_percentile_126d.

P1.1 (2026-07-08): cross-sectional percentile rank (0-100) of each ticker's
126-trading-day ROC against every other active, currently-processed ticker
with a valid 126d ROC on the same feature_date. See
scratchpad/p1_batch_rs_earnings_breakout_scoring_design_note.md and
specs/01c_FORMULAS_AND_CONFIGS.md ("Cross-sectional RS percentile").

All tests are fully offline (no network, no provider) and use a tmp DuckDB
with the real schema applied, mirroring test_feature_engine_v02.py's fixture
pattern. Beyond standard schema/null-propagation checks, this file explicitly
covers the granularity/statistical-stability caveats the design note
documented as scale-dependent (not something the code "solves for"):
percentile step size shrinks as the active universe (n) grows, and a lone
ticker in its day's population ranks at 100.0 rather than an undefined value.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from app.config import constants
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.features import feature_engine as femod
from app.services.features.feature_engine import FeatureEngine, _percentile_rank
from app.utils import service_result

# Reuse the exact fixture/seeding conventions from test_feature_engine_v02.py
from tests.test_feature_engine_v02 import (
    _fetch_feature,
    _seed_prices,
    _seed_ticker_master,
    _trading_days,
    tmp_db_paths,  # noqa: F401 -- pytest fixture, imported for reuse
)


# --------------------------------------------------------------------------- #
# Unit tests: _percentile_rank (no DB required)
# --------------------------------------------------------------------------- #
class TestPercentileRank:
    def test_lone_value_is_100(self) -> None:
        assert _percentile_rank([42.0], 42.0) == 100.0

    def test_empty_list_is_100(self) -> None:
        assert _percentile_rank([], 5.0) == 100.0

    def test_orders_low_mid_high(self) -> None:
        sorted_values = [10.0, 20.0, 30.0]
        assert _percentile_rank(sorted_values, 10.0) == 0.0
        assert _percentile_rank(sorted_values, 20.0) == 50.0
        assert _percentile_rank(sorted_values, 30.0) == 100.0

    def test_five_member_family_spreads_in_25_point_steps(self) -> None:
        # Matches the n=5 integration test below: granularity = 100/(5-1) = 25.
        sorted_values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert [_percentile_rank(sorted_values, v) for v in sorted_values] == [
            0.0, 25.0, 50.0, 75.0, 100.0,
        ]

    def test_duplicate_values_rank_at_first_occurrence(self) -> None:
        sorted_values = [10.0, 10.0, 20.0]
        assert _percentile_rank(sorted_values, 10.0) == 0.0
        assert _percentile_rank(sorted_values, 20.0) == 100.0


# --------------------------------------------------------------------------- #
# Integration tests: FeatureEngine.calculate() end-to-end
# --------------------------------------------------------------------------- #
_WARMUP_DAYS = 14
_ROC_WINDOW = 126


def _seed_ticker_with_final_close(
    prod: Path, ticker: str, final_close: float, total_days: int = _WARMUP_DAYS + _ROC_WINDOW,
) -> None:
    """Seed a ticker whose close is flat at 100.0 for _WARMUP_DAYS, then
    jumps to (and stays at) final_close for exactly _ROC_WINDOW trading
    days -- so at the last day, close_adj.shift(126) lands exactly on a
    100.0 warmup bar, giving an exact, reviewable roc126 = final_close/100 - 1.
    """
    days = _trading_days(date(2022, 1, 3), total_days)
    closes = [100.0] * _WARMUP_DAYS + [final_close] * (total_days - _WARMUP_DAYS)
    _seed_prices(prod, ticker, days, closes)
    _seed_ticker_master(prod, ticker)
    return days


class TestFeatureSchemaVersionBump:
    def test_new_rows_are_features_v03(self, tmp_db_paths: dict) -> None:
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _seed_ticker_with_final_close(prod, "VBUMP", 110.0)
        FeatureEngine().calculate(days[-1], days[-1], tickers=["VBUMP"])
        row = _fetch_feature(prod, "VBUMP")
        # P2.3/P2.4 (2026-07-10): bumped features_v03 -> features_v04.
        # 2026-07-20: bumped features_v04 -> features_v05 (ema150, dormant).
        assert row["feature_schema_version"] == "features_v05" == constants.FEATURE_SCHEMA_VERSION


class TestRsPercentile126dNullPropagation:
    def test_null_when_fewer_than_126_bars(self, tmp_db_paths: dict) -> None:
        """A ticker with <126 bars of history gets rs_percentile_126d=NULL,
        not 0 -- insufficient data, not "worst possible rank"."""
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _trading_days(date(2022, 1, 3), 60)  # well under 126
        _seed_prices(prod, "SHORT", days, [100.0 + i * 0.3 for i in range(60)])
        _seed_ticker_master(prod, "SHORT")
        FeatureEngine().calculate(days[-1], days[-1], tickers=["SHORT"])
        row = _fetch_feature(prod, "SHORT")
        assert row.get("rs_percentile_126d") is None
        # Confirms it's a data-insufficiency null, not a computed roc126:
        assert row.get("roc20") is not None  # 60 bars is plenty for roc20

    def test_short_history_ticker_does_not_skew_others(self, tmp_db_paths: dict) -> None:
        """A <126-bar ticker mixed into a batch, sharing the *same* cutoff
        date as a full-history ticker, is excluded from the ranking
        population entirely -- it must not appear as a phantom 0 that drags
        down everyone else's percentile."""
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _seed_ticker_with_final_close(prod, "SOLO", 130.0)  # 140 days
        # SHORT2 only has price rows for the *last* 60 of those 140 trading
        # days (e.g. a recent IPO) -- same cutoff date as SOLO, but nowhere
        # near 126 bars of its own history.
        _seed_prices(prod, "SHORT2", days[-60:], [100.0] * 60)
        _seed_ticker_master(prod, "SHORT2")

        FeatureEngine().calculate(days[-1], days[-1], tickers=["SHORT2", "SOLO"])
        row_short = _fetch_feature(prod, "SHORT2")
        row_solo = _fetch_feature(prod, "SOLO")

        assert row_short["rs_percentile_126d"] is None
        # SOLO is the only ticker in the shared feature_date's population
        # with a valid roc126 (SHORT2's is NULL, excluded) -> SOLO ranks at
        # 100.0, not dragged down by a phantom peer.
        assert row_solo["rs_percentile_126d"] == 100.0


class TestRsPercentile126dGranularityCaveat:
    """Directly exercises the design note's documented caveat: percentile
    step size is 100/(n-1) points, shrinking as the active universe (n)
    grows -- coarse at small n, fine-grained at full scale. Neither case is
    a bug; both are asserted explicitly so the scale-dependence is a tested
    property, not just a comment."""

    def test_small_universe_n5_gives_25_point_steps(self, tmp_db_paths: dict) -> None:
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        # Returns: -20%, -10%, 0%, +20%, +30% -> ascending order E,D,C,B,A
        specs = [("E", 80.0), ("D", 90.0), ("C", 100.0), ("B", 120.0), ("A", 130.0)]
        last_day = None
        for ticker, final_close in specs:
            days = _seed_ticker_with_final_close(prod, ticker, final_close)
            last_day = days[-1]

        FeatureEngine().calculate(last_day, last_day, tickers=[t for t, _ in specs])

        expected_rank = {"E": 0.0, "D": 25.0, "C": 50.0, "B": 75.0, "A": 100.0}
        for ticker, expected in expected_rank.items():
            row = _fetch_feature(prod, ticker)
            assert row["rs_percentile_126d"] == pytest.approx(expected, abs=1e-6), ticker

    def test_larger_universe_n21_gives_5_point_steps(self, tmp_db_paths: dict) -> None:
        """n=21, evenly spaced returns -> granularity = 100/(21-1) = 5 points
        per rank -- markedly finer than the n=5 case's 25-point steps,
        demonstrating the same mechanism scales correctly toward
        full-universe granularity without any code change."""
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        tickers = [f"T{i:02d}" for i in range(21)]
        last_day = None
        for i, ticker in enumerate(tickers):
            # Ascending final closes -> ascending roc126, i=0 lowest, i=20 highest
            days = _seed_ticker_with_final_close(prod, ticker, 100.0 + i * 2.0)
            last_day = days[-1]

        FeatureEngine().calculate(last_day, last_day, tickers=tickers)

        # Spot-check rather than all 21: lowest, highest, and one interior
        # point confirming the exact 5-point step (not the n=5 case's 25).
        assert _fetch_feature(prod, "T00")["rs_percentile_126d"] == pytest.approx(0.0)
        assert _fetch_feature(prod, "T20")["rs_percentile_126d"] == pytest.approx(100.0)
        assert _fetch_feature(prod, "T10")["rs_percentile_126d"] == pytest.approx(50.0)
        assert _fetch_feature(prod, "T01")["rs_percentile_126d"] == pytest.approx(5.0)


class TestRsPercentile126dDistinctFromTimeSeriesRs:
    def test_rs_percentile_independent_of_relative_strength_vs_spy(self, tmp_db_paths: dict) -> None:
        """rs_percentile_126d (cross-sectional rank, 126d window) and
        relative_strength_vs_spy (time-series spread vs. SPY, 20d window)
        are independent mechanisms -- two tickers with an identical 20-day
        return (same relative_strength_vs_spy) but different 126-day
        returns get different rs_percentile_126d values, proving the new
        field isn't a relabeled copy of the existing one. Only persisted
        columns are asserted on (roc126 itself is a transient intermediate,
        not written to daily_features)."""
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        n = _WARMUP_DAYS + _ROC_WINDOW  # 140
        days = _trading_days(date(2022, 1, 3), n)

        # Three fixed points control the two independent windows:
        #   index 13  (126 bars before the last)  -> roc126 anchor
        #   index 119 (20 bars before the last)    -> roc20 anchor
        #   index 139 (last bar)                   -> 120.0 for both
        # Both tickers share the same roc20 anchor (100 -> same +20% over
        # the final 20 bars, same relative_strength_vs_spy vs. flat SPY) but
        # a different roc126 anchor (100 vs 60), so only rs_percentile_126d
        # should differ. NOTE: scaling an entire series by a constant leaves
        # % returns unchanged -- the two anchors must differ in isolation
        # from each other, not just from a uniformly-scaled base, or roc126
        # comes out identical for both tickers.
        ramp = [100.0 * (1 + 0.20 * i / 20) for i in range(1, 21)]  # ends at 120.0
        closes_hi = [100.0] * _WARMUP_DAYS + [100.0] * (120 - _WARMUP_DAYS) + ramp
        closes_lo = [60.0] * _WARMUP_DAYS + [100.0] * (120 - _WARMUP_DAYS) + ramp
        assert len(closes_hi) == len(closes_lo) == n == len(days)

        _seed_prices(prod, "HI126", days, closes_hi)
        _seed_ticker_master(prod, "HI126")
        _seed_prices(prod, "LO126", days, closes_lo)
        _seed_ticker_master(prod, "LO126")
        _seed_prices(prod, "SPY", days, [400.0] * n)  # flat SPY
        _seed_ticker_master(prod, "SPY")

        FeatureEngine().calculate(days[-1], days[-1], tickers=["HI126", "LO126"])
        row_hi = _fetch_feature(prod, "HI126")
        row_lo = _fetch_feature(prod, "LO126")

        # Same 20d return (120/100 - 1 = 0.20 for both) -> same
        # relative_strength_vs_spy (both measured against flat SPY).
        assert row_hi["roc20"] == pytest.approx(row_lo["roc20"], abs=1e-6)
        assert row_hi["relative_strength_vs_spy"] == pytest.approx(
            row_lo["relative_strength_vs_spy"], abs=1e-6
        )
        # roc126: HI126 = 120/100 - 1 = 0.20; LO126 = 120/60 - 1 = 1.0 --
        # LO126's 126d return is far larger, so it ranks higher in this
        # 2-member population despite an identical 20d return.
        assert row_hi["rs_percentile_126d"] == 0.0
        assert row_lo["rs_percentile_126d"] == 100.0
