"""Tests for the Phase 4 SEC EDGAR fundamentals provider.

Fully offline: no live ``data.sec.gov`` calls. Pure computation functions are
exercised directly against synthetic XBRL ``companyfacts``-shaped fixtures;
:class:`EdgarFundamentalsProvider` is exercised with an injected
``fetch_json`` fake (mirroring ``YahooProvider``'s injected ``yf_module``
pattern) so no ``requests`` network access ever happens in the suite.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import edgar_provider as ep
from app.providers.provider_interface import FundamentalSnapshot
from app.utils import service_result


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _fact(end: str, filed: str, val: float, form: str = "10-K") -> dict:
    return {"end": end, "filed": filed, "val": val, "form": form, "fy": 2024, "fp": "FY"}


def _concept(*entries: dict) -> dict:
    return {"units": {"USD": list(entries)}}


def _companyfacts(concepts: dict[str, dict]) -> dict:
    return {"cik": 320193, "entityName": "Test Co", "facts": {"us-gaap": concepts}}


_AS_OF = date(2024, 6, 1)

# Two clean annual periods, both filed well before the as-of date.
_ASSETS = _concept(
    _fact("2023-12-31", "2024-02-01", 1000),
    _fact("2022-12-31", "2023-02-01", 900),
)
_CURRENT_ASSETS = _concept(
    _fact("2023-12-31", "2024-02-01", 500),
    _fact("2022-12-31", "2023-02-01", 400),
)
_CURRENT_LIABILITIES = _concept(
    _fact("2023-12-31", "2024-02-01", 200),
    _fact("2022-12-31", "2023-02-01", 250),
)
_LIABILITIES = _concept(
    _fact("2023-12-31", "2024-02-01", 600),
    _fact("2022-12-31", "2023-02-01", 550),
)
_EQUITY = _concept(
    _fact("2023-12-31", "2024-02-01", 400),
    _fact("2022-12-31", "2023-02-01", 350),
)
_RETAINED_EARNINGS = _concept(
    _fact("2023-12-31", "2024-02-01", 250),
    _fact("2022-12-31", "2023-02-01", 200),
)
_NET_INCOME = _concept(
    _fact("2023-12-31", "2024-02-01", 100),
    _fact("2022-12-31", "2023-02-01", 80),
)
_OPERATING_INCOME = _concept(
    _fact("2023-12-31", "2024-02-01", 150),
    _fact("2022-12-31", "2023-02-01", 120),
)
_REVENUES = _concept(
    _fact("2023-12-31", "2024-02-01", 1000),
    _fact("2022-12-31", "2023-02-01", 900),
)
_CFO = _concept(
    _fact("2023-12-31", "2024-02-01", 120),
    _fact("2022-12-31", "2023-02-01", 90),
)
_GROSS_PROFIT = _concept(
    _fact("2023-12-31", "2024-02-01", 600),
    _fact("2022-12-31", "2023-02-01", 500),
)
_EPS_DILUTED = _concept(
    _fact("2023-12-31", "2024-02-01", 5.0),
    _fact("2022-12-31", "2023-02-01", 4.0),
)
_LONG_TERM_DEBT = _concept(
    _fact("2023-12-31", "2024-02-01", 300),
    _fact("2022-12-31", "2023-02-01", 350),
)


def _full_companyfacts() -> dict:
    return _companyfacts(
        {
            "Assets": _ASSETS,
            "AssetsCurrent": _CURRENT_ASSETS,
            "LiabilitiesCurrent": _CURRENT_LIABILITIES,
            "Liabilities": _LIABILITIES,
            "StockholdersEquity": _EQUITY,
            "RetainedEarningsAccumulatedDeficit": _RETAINED_EARNINGS,
            "NetIncomeLoss": _NET_INCOME,
            "OperatingIncomeLoss": _OPERATING_INCOME,
            "Revenues": _REVENUES,
            "NetCashProvidedByUsedInOperatingActivities": _CFO,
            "GrossProfit": _GROSS_PROFIT,
            "EarningsPerShareDiluted": _EPS_DILUTED,
            "LongTermDebtNoncurrent": _LONG_TERM_DEBT,
        }
    )


# --------------------------------------------------------------------------- #
# extract_annual_series — point-in-time / no-look-ahead discipline
# --------------------------------------------------------------------------- #
class TestExtractAnnualSeries:
    def test_filters_non_10k_forms(self) -> None:
        concept = _concept(
            _fact("2024-03-31", "2024-05-01", 999, form="10-Q"),
            _fact("2023-12-31", "2024-02-01", 1000, form="10-K"),
        )
        series = ep.extract_annual_series(concept, _AS_OF)
        assert [e["val"] for e in series] == [1000]

    def test_sorted_descending_by_end(self) -> None:
        series = ep.extract_annual_series(_ASSETS, _AS_OF)
        assert [e["end"] for e in series] == ["2023-12-31", "2022-12-31"]

    def test_leak_guard_excludes_fact_filed_after_as_of_date(self) -> None:
        """A FY ending before as_of_date but not yet FILED must not leak."""
        concept = _concept(
            _fact("2023-12-31", "2024-08-01", 1000),  # filed AFTER as_of_date
            _fact("2022-12-31", "2023-02-01", 900),
        )
        series = ep.extract_annual_series(concept, _AS_OF)
        assert [e["val"] for e in series] == [900]

    def test_leak_guard_excludes_fact_ending_after_as_of_date(self) -> None:
        concept = _concept(_fact("2024-12-31", "2025-02-01", 1100))
        series = ep.extract_annual_series(concept, _AS_OF)
        assert series == []

    def test_empty_concept_returns_empty(self) -> None:
        assert ep.extract_annual_series(None, _AS_OF) == []
        assert ep.extract_annual_series({"units": {}}, _AS_OF) == []


# --------------------------------------------------------------------------- #
# Individual field computations
# --------------------------------------------------------------------------- #
class TestComputeEpsGrowthTrend:
    def test_normal_growth(self) -> None:
        series = ep.extract_annual_series(_EPS_DILUTED, _AS_OF)
        result = ep.compute_eps_growth_trend(series)
        assert result == pytest.approx((5.0 - 4.0) / 4.0)

    def test_none_when_only_one_period(self) -> None:
        series = ep.extract_annual_series(_concept(_fact("2023-12-31", "2024-02-01", 5.0)), _AS_OF)
        assert ep.compute_eps_growth_trend(series) is None

    def test_none_when_prior_zero(self) -> None:
        series = ep.extract_annual_series(
            _concept(
                _fact("2023-12-31", "2024-02-01", 5.0),
                _fact("2022-12-31", "2023-02-01", 0.0),
            ),
            _AS_OF,
        )
        assert ep.compute_eps_growth_trend(series) is None


class TestComputeLeverageRatio:
    def test_normal(self) -> None:
        debt = ep.extract_annual_series(_LONG_TERM_DEBT, _AS_OF)
        assets = ep.extract_annual_series(_ASSETS, _AS_OF)
        assert ep.compute_leverage_ratio(debt, assets) == pytest.approx(300 / 1000)

    def test_none_when_missing(self) -> None:
        assert ep.compute_leverage_ratio([], []) is None


class TestComputeValuationBand:
    def test_unknown_when_no_price(self) -> None:
        eps = ep.extract_annual_series(_EPS_DILUTED, _AS_OF)
        assert ep.compute_valuation_band(eps, None) == "unknown"

    def test_unknown_when_eps_non_positive(self) -> None:
        eps = ep.extract_annual_series(_concept(_fact("2023-12-31", "2024-02-01", -1.0)), _AS_OF)
        assert ep.compute_valuation_band(eps, 50.0) == "unknown"

    def test_cheap(self) -> None:
        eps = ep.extract_annual_series(_concept(_fact("2023-12-31", "2024-02-01", 10.0)), _AS_OF)
        assert ep.compute_valuation_band(eps, 100.0) == "cheap"  # PE=10

    def test_fair(self) -> None:
        eps = ep.extract_annual_series(_concept(_fact("2023-12-31", "2024-02-01", 10.0)), _AS_OF)
        assert ep.compute_valuation_band(eps, 200.0) == "fair"  # PE=20

    def test_expensive(self) -> None:
        eps = ep.extract_annual_series(_concept(_fact("2023-12-31", "2024-02-01", 10.0)), _AS_OF)
        assert ep.compute_valuation_band(eps, 300.0) == "expensive"  # PE=30


class TestComputePiotroskiFScore:
    def test_full_signal_computation(self) -> None:
        facts = _full_companyfacts()["facts"]["us-gaap"]
        score = ep.compute_piotroski_f_score(
            assets=ep.extract_annual_series(facts["Assets"], _AS_OF),
            current_assets=ep.extract_annual_series(facts["AssetsCurrent"], _AS_OF),
            current_liabilities=ep.extract_annual_series(facts["LiabilitiesCurrent"], _AS_OF),
            net_income=ep.extract_annual_series(facts["NetIncomeLoss"], _AS_OF),
            cfo=ep.extract_annual_series(
                facts["NetCashProvidedByUsedInOperatingActivities"], _AS_OF
            ),
            long_term_debt=ep.extract_annual_series(facts["LongTermDebtNoncurrent"], _AS_OF),
            gross_profit=ep.extract_annual_series(facts["GrossProfit"], _AS_OF),
            revenues=ep.extract_annual_series(facts["Revenues"], _AS_OF),
        )
        # 7 of 8 computed signals fire (turnover is flat 1.0 == 1.0, not an
        # improvement) -> raw 7/8, scaled to the standard 0-9 range.
        assert score == round(7 * 9 / 8)
        assert 0 <= score <= 9

    def test_none_when_missing_required_concept(self) -> None:
        assert (
            ep.compute_piotroski_f_score(
                assets=[],
                current_assets=[],
                current_liabilities=[],
                net_income=[],
                cfo=[],
                long_term_debt=[],
                gross_profit=[],
                revenues=[],
            )
            is None
        )


class TestComputeAltmanZScore:
    def test_normal_computation_matches_formula(self) -> None:
        facts = _full_companyfacts()["facts"]["us-gaap"]
        result = ep.compute_altman_z_score(
            assets=ep.extract_annual_series(facts["Assets"], _AS_OF),
            liabilities=ep.extract_annual_series(facts["Liabilities"], _AS_OF),
            current_assets=ep.extract_annual_series(facts["AssetsCurrent"], _AS_OF),
            current_liabilities=ep.extract_annual_series(facts["LiabilitiesCurrent"], _AS_OF),
            equity=ep.extract_annual_series(facts["StockholdersEquity"], _AS_OF),
            retained_earnings=ep.extract_annual_series(
                facts["RetainedEarningsAccumulatedDeficit"], _AS_OF
            ),
            operating_income=ep.extract_annual_series(facts["OperatingIncomeLoss"], _AS_OF),
            revenues=ep.extract_annual_series(facts["Revenues"], _AS_OF),
        )
        working_capital = 500 - 200
        expected = (
            0.717 * (working_capital / 1000)
            + 0.847 * (250 / 1000)
            + 3.107 * (150 / 1000)
            + 0.420 * (400 / 600)
            + 0.998 * (1000 / 1000)
        )
        assert result == pytest.approx(expected)

    def test_none_when_missing_required_concept(self) -> None:
        assert (
            ep.compute_altman_z_score(
                assets=[],
                liabilities=[],
                current_assets=[],
                current_liabilities=[],
                equity=[],
                retained_earnings=[],
                operating_income=[],
                revenues=[],
            )
            is None
        )


# --------------------------------------------------------------------------- #
# End-to-end pure assembly
# --------------------------------------------------------------------------- #
class TestComputeFundamentalsFromCompanyfacts:
    def test_assembles_snapshot_with_expected_gaps(self) -> None:
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _full_companyfacts(), "TEST", _AS_OF
        )
        assert isinstance(snapshot, FundamentalSnapshot)
        assert snapshot.ticker == "TEST"
        assert snapshot.as_of_date == _AS_OF
        assert snapshot.source_provider == "sec_edgar"
        assert snapshot.eps_growth_trend is not None
        assert snapshot.leverage_ratio is not None
        assert snapshot.piotroski_f_score is not None
        assert snapshot.altman_z_score is not None
        assert snapshot.valuation_band == "unknown"  # no price injected
        # Explicitly-flagged gaps (see module docstring): never fabricated.
        assert snapshot.insider_trade_flag is None
        assert snapshot.institutional_ownership_delta is None

    def test_valuation_band_uses_injected_price(self) -> None:
        snapshot = ep.compute_fundamentals_from_companyfacts(
            _full_companyfacts(), "TEST", _AS_OF, price=100.0
        )
        assert snapshot.valuation_band == "fair"  # EPS=5.0, price=100 -> PE=20


# --------------------------------------------------------------------------- #
# EdgarFundamentalsProvider (HTTP-fetching wrapper, injected fake fetch_json)
# --------------------------------------------------------------------------- #
class TestEdgarFundamentalsProvider:
    def _provider(self, companyfacts: dict | None = None, cik: str | None = "0000320193"):
        def fake_fetch(url: str, headers: dict) -> dict:
            assert "User-Agent" in headers
            if "companyfacts" in url:
                if companyfacts is None:
                    raise RuntimeError("simulated transport failure")
                return companyfacts
            raise AssertionError(f"unexpected URL in test: {url}")

        return ep.EdgarFundamentalsProvider(
            fetch_json=fake_fetch,
            ticker_to_cik=lambda ticker: cik,
        )

    def test_get_fundamentals_happy_path(self) -> None:
        provider = self._provider(_full_companyfacts())
        result = provider.get_fundamentals("TEST", _AS_OF)
        assert result.status == service_result.STATUS_SUCCESS
        assert result.rows_processed == 1
        snapshot = result.metadata["fundamentals"]
        assert isinstance(snapshot, FundamentalSnapshot)
        assert result.metadata["provider_name"] == "sec_edgar"

    def test_unknown_ticker_returns_unsupported_symbol(self) -> None:
        provider = self._provider(_full_companyfacts(), cik=None)
        result = provider.get_fundamentals("NOPE", _AS_OF)
        assert result.status == service_result.STATUS_FAILED
        assert result.metadata["error_detail"].kind == "unsupported_symbol"

    def test_transport_failure_returns_provider_unavailable(self) -> None:
        provider = self._provider(companyfacts=None)
        result = provider.get_fundamentals("TEST", _AS_OF)
        assert result.status == service_result.STATUS_FAILED
        assert result.metadata["error_detail"].kind == "provider_unavailable"

    def test_malformed_payload_returns_malformed_response(self) -> None:
        provider = self._provider({"facts": {"us-gaap": {"Assets": {"units": "not-a-dict"}}}})
        result = provider.get_fundamentals("TEST", _AS_OF)
        assert result.status == service_result.STATUS_FAILED
        assert result.metadata["error_detail"].kind == "malformed_response"

    def test_capabilities_report_fundamentals_only(self) -> None:
        provider = self._provider(_full_companyfacts())
        caps = provider.get_capabilities().metadata["capabilities"]
        assert caps.supports_fundamentals is True
        assert caps.supports_daily_prices is False
        assert caps.supports_earnings is False
        assert caps.supports_ticker_listing is False

    def test_other_methods_return_unsupported_capability(self) -> None:
        from datetime import date as _date

        from app.providers.provider_interface import PriceHistoryRequest

        provider = self._provider(_full_companyfacts())
        req = PriceHistoryRequest(
            ticker="TEST", start_date=_date(2024, 1, 1), end_date=_date(2024, 1, 2)
        )
        for result in (
            provider.get_price_history(req),
            provider.list_symbols(),
            provider.get_earnings("TEST"),
        ):
            assert result.status == service_result.STATUS_FAILED
            assert result.metadata["error_detail"].kind == "unsupported_capability"

    def test_default_ticker_to_cik_resolves_and_caches(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, headers: dict) -> dict:
            calls.append(url)
            if "company_tickers" in url:
                return {"0": {"ticker": "TEST", "cik_str": 320193}}
            return _full_companyfacts()

        provider = ep.EdgarFundamentalsProvider(fetch_json=fake_fetch)
        provider.get_fundamentals("TEST", _AS_OF)
        provider.get_fundamentals("TEST", _AS_OF)
        ticker_map_calls = [c for c in calls if "company_tickers" in c]
        assert len(ticker_map_calls) == 1  # cached after first resolution
