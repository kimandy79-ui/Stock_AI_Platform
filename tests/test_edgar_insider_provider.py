"""Tests for `app/providers/edgar_insider_provider.py` (SEC-EDGAR-native
`insider_trade_flag`, replacing the FMP-based approach).

Fully offline: every test injects fake `fetch_json`/`fetch_filing_xml`
callables, never a real network call. Covers the `insiderTransactionForIssuerExists`
short-circuit, exact `form=="4"` matching (the confirmed `browse-edgar`
prefix-matching anomaly, tested here as a safety net even though
`submissions.json` doesn't have that specific bug), the `aff10b5One`
10b5-1-plan exclusion, point-in-time `filingDate` gating, and the
True/False/None trichotomy.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import edgar_insider_provider as eip

AS_OF = date(2026, 7, 15)
CIK = "0001326380"


def _submissions(
    *,
    exists: bool = True,
    forms: list[str] | None = None,
    filing_dates: list[str] | None = None,
    accessions: list[str] | None = None,
    docs: list[str] | None = None,
) -> dict:
    return {
        "insiderTransactionForIssuerExists": exists,
        "filings": {
            "recent": {
                "form": forms or [],
                "filingDate": filing_dates or [],
                "accessionNumber": accessions or [],
                "primaryDocument": docs or [],
            }
        },
    }


def _form4_xml(
    *,
    transaction_code: str = "P",
    shares: float = 1000,
    price: float = 50.0,
    aff10b5one: str = "0",
    table: str = "nonDerivativeTable",
    txn_tag: str = "nonDerivativeTransaction",
) -> str:
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <aff10b5One>{aff10b5one}</aff10b5One>
    <{table}>
        <{txn_tag}>
            <transactionCoding>
                <transactionCode>{transaction_code}</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
            </transactionAmounts>
        </{txn_tag}>
    </{table}>
</ownershipDocument>"""


class TestFetchInsiderPurchaseFlag:
    def test_no_issuer_history_short_circuits_with_zero_filing_fetches(self):
        fetch_json = lambda url: _submissions(exists=False)
        filing_fetches: list[str] = []
        fetch_xml = lambda url: filing_fetches.append(url) or "<ownershipDocument/>"

        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)

        assert result is False
        assert filing_fetches == []

    def test_qualifying_purchase_in_window_above_threshold_non_10b5_1_is_true(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["0001990547-26-000014"], docs=["wk-form4.xml"],
        )
        fetch_xml = lambda url: _form4_xml(
            transaction_code="P", shares=1000, price=50.0, aff10b5one="0"
        )
        result = eip.fetch_insider_purchase_flag(
            "GME", AS_OF, CIK, fetch_json, fetch_xml,
            lookback_days=90, min_transaction_value_usd=10_000.0,
        )
        assert result is True

    def test_10b5_1_flagged_filing_is_excluded(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["0001990547-26-000014"], docs=["wk-form4.xml"],
        )
        fetch_xml = lambda url: _form4_xml(
            transaction_code="P", shares=1000, price=50.0, aff10b5one="1"
        )
        result = eip.fetch_insider_purchase_flag(
            "GME", AS_OF, CIK, fetch_json, fetch_xml, exclude_10b5_1=True,
        )
        assert result is False

    def test_10b5_1_flag_ignored_when_exclude_10b5_1_is_false(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["0001990547-26-000014"], docs=["wk-form4.xml"],
        )
        fetch_xml = lambda url: _form4_xml(
            transaction_code="P", shares=1000, price=50.0, aff10b5one="1"
        )
        result = eip.fetch_insider_purchase_flag(
            "GME", AS_OF, CIK, fetch_json, fetch_xml, exclude_10b5_1=False,
        )
        assert result is True

    def test_425_and_424b2_entries_alongside_real_form_4_dont_corrupt_result(self):
        """Regression test for the confirmed browse-edgar type=4 prefix-matching
        anomaly. submissions.json's plain array doesn't have this bug, but this
        module filters form == '4' exactly regardless -- verify it holds even
        with '425'/'424B2' rows mixed into the same arrays.
        """
        fetch_json = lambda url: _submissions(
            forms=["425", "424B2", "4"],
            filing_dates=["2026-07-12", "2026-07-11", "2026-07-10"],
            accessions=["acc-425", "acc-424b2", "0001990547-26-000014"],
            docs=["doc425.htm", "doc424b2.htm", "wk-form4.xml"],
        )
        fetched_urls: list[str] = []

        def fetch_xml(url: str) -> str:
            fetched_urls.append(url)
            return _form4_xml(transaction_code="P", shares=1000, price=50.0)

        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)

        assert result is True
        # Only the real Form 4's document was ever fetched -- the 425/424B2
        # rows never triggered a filing-content request at all.
        assert len(fetched_urls) == 1
        assert "wk-form4.xml" in fetched_urls[0]

    def test_filing_date_after_as_of_date_is_excluded(self):
        """Point-in-time integrity: a filing dated after as_of_date must not count."""
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-16"],  # 1 day after AS_OF
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: _form4_xml(transaction_code="P", shares=1000, price=50.0)

        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is False

    def test_filing_date_outside_lookback_window_is_excluded(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2025-12-01"],  # well over 90 days before AS_OF
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: _form4_xml(transaction_code="P", shares=1000, price=50.0)

        result = eip.fetch_insider_purchase_flag(
            "GME", AS_OF, CIK, fetch_json, fetch_xml, lookback_days=90,
        )
        assert result is False

    def test_below_dollar_threshold_is_false(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: _form4_xml(shares=10, price=5.0)  # $50
        result = eip.fetch_insider_purchase_flag(
            "GME", AS_OF, CIK, fetch_json, fetch_xml, min_transaction_value_usd=10_000.0,
        )
        assert result is False

    def test_non_purchase_transaction_code_is_excluded(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: _form4_xml(transaction_code="S", shares=1000, price=50.0)
        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is False

    def test_derivative_table_purchase_also_counts(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: _form4_xml(
            table="derivativeTable", txn_tag="derivativeTransaction",
            transaction_code="P", shares=1000, price=50.0,
        )
        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is True

    def test_submissions_fetch_failure_returns_none(self):
        def _boom(url: str) -> dict:
            raise RuntimeError("network error")

        result = eip.fetch_insider_purchase_flag(
            "GME", AS_OF, CIK, _boom, lambda url: ""
        )
        assert result is None

    def test_filing_xml_fetch_failure_returns_none(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )

        def _boom(url: str) -> str:
            raise RuntimeError("network error")

        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, _boom)
        assert result is None

    def test_malformed_xml_does_not_crash_falls_through_to_false(self):
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: "not valid xml <<<"
        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is False

    def test_no_form_4_candidates_is_false_without_any_filing_fetch(self):
        fetch_json = lambda url: _submissions(
            forms=["8-K", "10-Q"], filing_dates=["2026-07-10", "2026-07-09"],
            accessions=["acc-1", "acc-2"], docs=["doc1.htm", "doc2.htm"],
        )
        fetched: list[str] = []
        fetch_xml = lambda url: fetched.append(url) or ""
        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is False
        assert fetched == []

    def test_early_exit_stops_after_first_qualifying_purchase(self):
        """Confirms the loop doesn't fetch remaining candidates once satisfied."""
        fetch_json = lambda url: _submissions(
            forms=["4", "4"],
            filing_dates=["2026-07-12", "2026-07-10"],
            accessions=["acc-newest", "acc-older"],
            docs=["newest.xml", "older.xml"],
        )
        fetched: list[str] = []

        def fetch_xml(url: str) -> str:
            fetched.append(url)
            return _form4_xml(transaction_code="P", shares=1000, price=50.0)

        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is True
        assert len(fetched) == 1  # stopped after the first (newest) qualifying filing

    def test_cik_is_zero_padded_in_submissions_url(self):
        seen_urls: list[str] = []

        def fetch_json(url: str) -> dict:
            seen_urls.append(url)
            return _submissions(exists=False)

        eip.fetch_insider_purchase_flag("GME", AS_OF, "1326380", fetch_json, lambda u: "")
        assert seen_urls == ["https://data.sec.gov/submissions/CIK0001326380.json"]

    def test_max_candidate_filings_caps_worst_case_requests(self):
        forms = ["4"] * 10
        filing_dates = ["2026-07-10"] * 10
        accessions = [f"acc-{i}" for i in range(10)]
        docs = [f"doc{i}.xml" for i in range(10)]
        fetch_json = lambda url: _submissions(
            forms=forms, filing_dates=filing_dates, accessions=accessions, docs=docs,
        )
        fetched: list[str] = []

        def fetch_xml(url: str) -> str:
            fetched.append(url)
            return _form4_xml(transaction_code="S")  # never qualifies -> exhausts all candidates

        eip.fetch_insider_purchase_flag(
            "GME", AS_OF, CIK, fetch_json, fetch_xml, max_candidate_filings=3,
        )
        assert len(fetched) == 3
