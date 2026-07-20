"""Integration tests: `insider_lookup` threaded through `EdgarFundamentalsProvider`
(SEC-EDGAR-native `insider_trade_flag`, P2.7 -- replaces the removed FMP-based
approach). Mirrors `test_p2_6_valuation_band_price_lookup.py`'s pattern for
`price_lookup`.

Fully offline.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import edgar_provider as ep

RUN_DATE = date(2026, 6, 15)


def _minimal_facts() -> dict:
    return {"facts": {"us-gaap": {}, "dei": {}}}


class TestComputeFundamentalsFromCompanyfactsThreadsInsiderFlag:
    def test_insider_flag_is_threaded_into_the_snapshot(self):
        snap = ep.compute_fundamentals_from_companyfacts(
            _minimal_facts(), "GME", RUN_DATE, insider_flag=True
        )
        assert snap.insider_trade_flag is True

    def test_absent_insider_flag_stays_none(self):
        snap = ep.compute_fundamentals_from_companyfacts(_minimal_facts(), "GME", RUN_DATE)
        assert snap.insider_trade_flag is None

    def test_false_insider_flag_is_preserved_not_coerced_to_none(self):
        snap = ep.compute_fundamentals_from_companyfacts(
            _minimal_facts(), "GME", RUN_DATE, insider_flag=False
        )
        assert snap.insider_trade_flag is False


class TestProviderThreadsInsiderLookupIntoTheSnapshot:
    def test_injected_lookup_populates_insider_trade_flag(self):
        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: _minimal_facts(),
            ticker_to_cik=lambda t: "0001326380",
            insider_lookup=lambda ticker, as_of, cik: True,
        )
        result = provider.get_fundamentals("GME", RUN_DATE)
        assert result.metadata["fundamentals"].insider_trade_flag is True

    def test_lookup_receives_ticker_as_of_date_and_resolved_cik(self):
        calls = []

        def _lookup(ticker, as_of_date, cik):
            calls.append((ticker, as_of_date, cik))
            return False

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: _minimal_facts(),
            ticker_to_cik=lambda t: "0001326380",
            insider_lookup=_lookup,
        )
        provider.get_fundamentals("GME", RUN_DATE)
        assert calls == [("GME", RUN_DATE, "0001326380")]

    def test_without_a_lookup_the_flag_stays_none(self):
        """The pre-P2.7 production behaviour, pinned so the regression is visible."""
        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: _minimal_facts(),
            ticker_to_cik=lambda t: "0001326380",
        )
        result = provider.get_fundamentals("GME", RUN_DATE)
        assert result.metadata["fundamentals"].insider_trade_flag is None

    def test_lookup_failure_does_not_fail_the_whole_fundamentals_call(self):
        """A failure in the informational-only insider check must never discard
        the other 5 already-computed EDGAR fields or trigger the yfinance
        fallback (which has zero piotroski/altman coverage).
        """
        def _boom(ticker, as_of_date, cik):
            raise RuntimeError("SEC EDGAR insider check failed")

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: _minimal_facts(),
            ticker_to_cik=lambda t: "0001326380",
            insider_lookup=_boom,
        )
        result = provider.get_fundamentals("GME", RUN_DATE)
        snapshot = result.metadata["fundamentals"]
        assert snapshot.insider_trade_flag is None
        assert snapshot.source_provider == ep.PROVIDER_NAME  # still sec_edgar, not yfinance_fallback

    def test_lookup_false_result_is_preserved_through_the_full_provider_call(self):
        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: _minimal_facts(),
            ticker_to_cik=lambda t: "0001326380",
            insider_lookup=lambda ticker, as_of, cik: False,
        )
        result = provider.get_fundamentals("GME", RUN_DATE)
        assert result.metadata["fundamentals"].insider_trade_flag is False


class TestBuildSecHttpClient:
    def test_returns_a_client_with_get_json_and_get_text(self):
        client = ep.build_sec_http_client(sec_user_agent="Test test@example.com")
        assert callable(client.get_json)
        assert callable(client.get_text)

    def test_get_json_and_get_text_share_one_throttled_session(self, requests_mock):
        """The whole point of exposing one shared client: both request shapes
        go through the same throttle/retry state, not two independent ones.
        """
        requests_mock.get(
            "https://data.sec.gov/submissions/CIK0000320193.json",
            json={"insiderTransactionForIssuerExists": False},
        )
        requests_mock.get(
            "https://www.sec.gov/Archives/edgar/data/320193/000/doc.xml",
            text="<ownershipDocument/>",
        )
        client = ep.build_sec_http_client(sec_user_agent="Test test@example.com")
        json_result = client.get_json("https://data.sec.gov/submissions/CIK0000320193.json")
        text_result = client.get_text("https://www.sec.gov/Archives/edgar/data/320193/000/doc.xml")
        assert json_result == {"insiderTransactionForIssuerExists": False}
        assert text_result == "<ownershipDocument/>"
