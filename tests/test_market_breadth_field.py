"""P2.2 (AD-22.25) — market_breadth_pct additive inert field on M12.

Proves the new field is (a) computed correctly from distance_to_ema200_pct over
the feature-ready universe, and (b) purely additive — the existing
market_regime classification is unchanged on the same fixture. The broader
zero-behavior-change guarantee is proven separately by the full existing
test_market_regime_engine.py suite passing unchanged, plus a before/after
golden diff (see the P2.2 delivery note).

Reuses the seeding fixtures/helpers from test_market_regime_engine.py.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from app.config import constants
from app.database import duckdb_manager as dbm
from app.services.regime.market_regime_engine import MarketRegimeEngine

from tests.test_market_regime_engine import (  # noqa: F401 (tmp_db_paths is a fixture)
    tmp_db_paths,
    _seed_constant_regime_symbols,
    SCHEMA,
)

_INSERT_FEATURE_WITH_DIST = (
    "INSERT INTO daily_features "
    "(ticker, feature_date, feature_cutoff_date, feature_schema_version, "
    " feature_ready, distance_to_ema200_pct, market_regime, calculated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS TIMESTAMP))"
)


def _seed_features_with_distance(
    db_path: Path, feature_date: date, rows: list[tuple[str, float | None, bool]]
) -> None:
    """rows: (ticker, distance_to_ema200_pct, feature_ready)."""
    conn = duckdb.connect(str(db_path))
    try:
        for ticker, dist, ready in rows:
            conn.execute(
                _INSERT_FEATURE_WITH_DIST,
                [ticker, feature_date, feature_date, SCHEMA, ready, dist, None,
                 "2000-01-01 00:00:00"],
            )
    finally:
        conn.close()


def _fetch(db_path: Path, ticker: str, feature_date: date) -> dict:
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        r = conn.execute(
            "SELECT market_regime, market_breadth_pct FROM daily_features "
            "WHERE ticker = ? AND feature_date = ?",
            [ticker, feature_date],
        ).fetchone()
    finally:
        conn.close()
    return {"market_regime": r[0], "market_breadth_pct": r[1]}


class TestMarketBreadthField:
    def test_breadth_computed_and_regime_unchanged(self, tmp_db_paths: dict) -> None:
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        last = date(2023, 3, 1)
        # Bull regime: SPY close above its converged EMA200 (base 100 -> 110),
        # low VIX. This is the exact scenario the M12 suite already covers; we
        # assert the regime is unchanged AND breadth is populated.
        _seed_constant_regime_symbols(prod, last, 220, spy_last=110.0, vix_last=15.0)

        # 6 feature-ready universe tickers: 4 above EMA200 (dist>0), 2 below.
        _seed_features_with_distance(prod, last, [
            ("AAA", 0.05, True),
            ("BBB", 0.02, True),
            ("CCC", 0.10, True),
            ("DDD", 0.01, True),
            ("EEE", -0.03, True),
            ("FFF", -0.08, True),
        ])

        res = MarketRegimeEngine().classify(last, last)
        assert res.is_ok(), res.errors

        got = _fetch(prod, "AAA", last)
        # regime unchanged (bull) — additive field did not perturb classification
        assert got["market_regime"] == "bull"
        # breadth = 4/6 * 100 = 66.666...
        assert got["market_breadth_pct"] == pytest.approx(400.0 / 6.0, abs=1e-9)
        # market-wide: every row for the date gets the same breadth value
        assert _fetch(prod, "FFF", last)["market_breadth_pct"] == pytest.approx(400.0 / 6.0, abs=1e-9)

    def test_breadth_excludes_non_feature_ready_rows(self, tmp_db_paths: dict) -> None:
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        last = date(2023, 3, 1)
        _seed_constant_regime_symbols(prod, last, 220, spy_last=110.0, vix_last=15.0)
        # 2 ready above, 1 ready below, 1 NOT-ready above (excluded), 1 ready NULL dist (excluded)
        _seed_features_with_distance(prod, last, [
            ("AAA", 0.05, True),
            ("BBB", 0.03, True),
            ("CCC", -0.02, True),
            ("DDD", 0.09, False),   # not feature_ready -> excluded
            ("EEE", None, True),    # null distance -> excluded
        ])
        res = MarketRegimeEngine().classify(last, last)
        assert res.is_ok(), res.errors
        # population = AAA,BBB,CCC (ready & non-null) -> 2/3 above
        assert _fetch(prod, "AAA", last)["market_breadth_pct"] == pytest.approx(200.0 / 3.0, abs=1e-9)

    def test_breadth_null_when_no_qualifying_rows(self, tmp_db_paths: dict) -> None:
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        last = date(2023, 3, 1)
        _seed_constant_regime_symbols(prod, last, 220, spy_last=110.0, vix_last=15.0)
        # single feature row with NULL distance -> no qualifying rows -> breadth NULL
        _seed_features_with_distance(prod, last, [("AAA", None, True)])
        res = MarketRegimeEngine().classify(last, last)
        assert res.is_ok(), res.errors
        got = _fetch(prod, "AAA", last)
        assert got["market_regime"] == "bull"      # regime still classified
        assert got["market_breadth_pct"] is None    # breadth undefined, not 0
