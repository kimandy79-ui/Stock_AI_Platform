"""Phase 0 — Point-in-Time / Look-Ahead Audit for the Phase 4 fundamentals
fields (companion to ``tests/test_point_in_time_integrity.py``, which covers
M11/M12; this file covers the 7 :class:`FundamentalSnapshot` fields per the
Phase 4 coder note's mandatory leak-test requirement).

Unlike M11/M12, EDGAR fundamentals are computed directly from provider XBRL
facts (no DuckDB join), so the leak surface is entirely inside
:func:`app.providers.edgar_provider.extract_annual_series` — the
``filed <= as_of_date`` guard (not just ``end <= as_of_date``) is what
prevents a FY that ended before ``as_of_date`` but was FILED after it from
being treated as known.

Fixture: three annual (10-K) periods for every underlying XBRL concept:
  - Period A (FY2021, end 2021-12-31, filed 2022-02-01) — oldest known.
  - Period B (FY2022, end 2022-12-31, filed 2023-02-01) — newest known as of
    ``as_of_date = 2024-06-01``.
  - Period C (FY2023, end 2023-12-31, filed **2024-08-01** — after
    ``as_of_date``) — deliberately extreme values (EPS ~250x higher, inverted
    leverage/margin trends) so that any leak is unmistakable rather than a
    subtle rounding difference.

Audit inventory (7 fields)
---------------------------
Field                          | Verdict      | Mechanism
--------------------------------|--------------|---------------------------
eps_growth_trend                | asof-safe    | extract_annual_series filed guard
leverage_ratio                  | asof-safe    | extract_annual_series filed guard
valuation_band                  | asof-safe    | extract_annual_series filed guard (EPS side)
piotroski_f_score                | asof-safe    | extract_annual_series filed guard (both periods)
altman_z_score                   | asof-safe    | extract_annual_series filed guard
insider_trade_flag               | not sourced  | always None (no data to leak)
institutional_ownership_delta    | not sourced  | always None (no data to leak)
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import edgar_provider as ep

_AS_OF = date(2024, 6, 1)


def _fact(end: str, filed: str, val: float) -> dict:
    return {"end": end, "filed": filed, "val": val, "form": "10-K", "fy": 2024, "fp": "FY"}


def _concept(period_a: float, period_b: float, period_c_leaked: float) -> dict:
    """Three annual entries: A (oldest), B (newest known-as-of), C (future-filed)."""
    return {
        "units": {
            "USD": [
                _fact("2021-12-31", "2022-02-01", period_a),
                _fact("2022-12-31", "2023-02-01", period_b),
                _fact("2023-12-31", "2024-08-01", period_c_leaked),  # filed AFTER as_of
            ]
        }
    }


# Period A / B are mild, realistic annual progressions. Period C is a wild
# outlier (would be unmistakable in any computed field if it leaked in).
_COMPANYFACTS = {
    "cik": 1,
    "entityName": "Leak Test Co",
    "facts": {
        "us-gaap": {
            "Assets": _concept(800, 900, 50_000_000),
            "AssetsCurrent": _concept(400, 400, 1),
            "LiabilitiesCurrent": _concept(300, 250, 40_000_000),
            "Liabilities": _concept(500, 550, 60_000_000),
            "StockholdersEquity": _concept(300, 350, 1),
            "RetainedEarningsAccumulatedDeficit": _concept(150, 200, -999_999),
            "NetIncomeLoss": _concept(50, 80, -999_999),
            "OperatingIncomeLoss": _concept(80, 120, -999_999),
            "Revenues": _concept(700, 900, 1),
            "NetCashProvidedByUsedInOperatingActivities": _concept(60, 90, -999_999),
            "GrossProfit": _concept(300, 500, 1),
            "EarningsPerShareDiluted": _concept(2.0, 4.0, 999.0),
            "LongTermDebtNoncurrent": _concept(400, 350, 90_000_000),
        }
    },
}


def _expected_series(concept_name: str) -> list[dict]:
    """The non-leaked (A, B) series, newest first — what should be used."""
    facts = _COMPANYFACTS["facts"]["us-gaap"][concept_name]["units"]["USD"]
    known = [e for e in facts if e["end"] != "2023-12-31"]
    return sorted(known, key=lambda e: e["end"], reverse=True)


class TestFundamentalsNoLookAhead:
    """Each computed field must match the value derivable from A/B only —
    never a value influenced by C (filed after as_of_date)."""

    def test_extract_annual_series_excludes_future_filed_period(self) -> None:
        series = ep.extract_annual_series(
            _COMPANYFACTS["facts"]["us-gaap"]["Assets"], _AS_OF
        )
        assert [e["end"] for e in series] == ["2022-12-31", "2021-12-31"]
        assert [e["val"] for e in series] == [900, 800]

    def test_eps_growth_trend_not_leaked(self) -> None:
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", _AS_OF
        )
        # Non-leaked: (4.0 - 2.0) / 2.0 = 1.0. Leaked would use EPS=999 (period
        # C) and produce a wildly different (>100x) growth figure.
        assert snapshot.eps_growth_trend == pytest.approx(1.0)

    def test_leverage_ratio_not_leaked(self) -> None:
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", _AS_OF
        )
        # Non-leaked: 350 / 900. Leaked would divide by Assets=50,000,000.
        assert snapshot.leverage_ratio == pytest.approx(350 / 900)

    def test_valuation_band_not_leaked(self) -> None:
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", _AS_OF, price=100.0
        )
        # Non-leaked EPS=4.0 -> PE=25 -> "fair". Leaked EPS=999 -> PE≈0.1 -> "cheap".
        assert snapshot.valuation_band == "fair"

    def test_piotroski_f_score_not_leaked(self) -> None:
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", _AS_OF
        )
        expected = ep.compute_piotroski_f_score(
            assets=_expected_series("Assets"),
            current_assets=_expected_series("AssetsCurrent"),
            current_liabilities=_expected_series("LiabilitiesCurrent"),
            net_income=_expected_series("NetIncomeLoss"),
            cfo=_expected_series("NetCashProvidedByUsedInOperatingActivities"),
            long_term_debt=_expected_series("LongTermDebtNoncurrent"),
            gross_profit=_expected_series("GrossProfit"),
            revenues=_expected_series("Revenues"),
        )
        assert snapshot.piotroski_f_score == expected
        assert expected is not None

    def test_altman_z_score_not_leaked(self) -> None:
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", _AS_OF
        )
        expected = ep.compute_altman_z_score(
            assets=_expected_series("Assets"),
            liabilities=_expected_series("Liabilities"),
            current_assets=_expected_series("AssetsCurrent"),
            current_liabilities=_expected_series("LiabilitiesCurrent"),
            equity=_expected_series("StockholdersEquity"),
            retained_earnings=_expected_series("RetainedEarningsAccumulatedDeficit"),
            operating_income=_expected_series("OperatingIncomeLoss"),
            revenues=_expected_series("Revenues"),
        )
        assert snapshot.altman_z_score == pytest.approx(expected)
        assert expected is not None

    def test_insider_trade_flag_carries_no_data_to_leak(self) -> None:
        """Not sourced (see edgar_provider module docstring) -- always None,
        regardless of as_of_date, so there is no future value that could leak.
        """
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", _AS_OF
        )
        assert snapshot.insider_trade_flag is None

    def test_institutional_ownership_delta_carries_no_data_to_leak(self) -> None:
        """Not sourced (13F aggregation out of scope) -- always None."""
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", _AS_OF
        )
        assert snapshot.institutional_ownership_delta is None

    def test_advancing_as_of_date_past_period_c_filing_changes_the_result(self) -> None:
        """Control: once as_of_date passes period C's filed date, its data
        legitimately becomes visible and the computation changes -- proving
        the earlier tests' stable result was the guard working, not period C
        being unreachable in principle.
        """
        later = date(2024, 9, 1)  # after period C's filed=2024-08-01
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _COMPANYFACTS, "LEAK", later
        )
        assert snapshot.eps_growth_trend != pytest.approx(1.0)
