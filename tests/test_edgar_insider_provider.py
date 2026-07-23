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


# What the SEC actually serves at the ``xslF345X0N/`` path: HTML, not XML.
# Deliberately shaped like the real renderer output (unclosed <br>, <meta>) so
# `ET.fromstring` raises -- note a *well-formed* stub like
# "<!DOCTYPE html><html></html>" parses fine as XML and would not exercise this.
_RENDERED_FORM4_HTML = (
    '<!DOCTYPE html><html><head><meta http-equiv="Content-Type" content="text/html">'
    "</head><body><span>FORM 4</span><br><table><tr><td>1. Title of Security"
    "</td></tr></table></body></html>"
)


def _form4_xml(
    *,
    transaction_code: str = "P",
    shares: float = 1000,
    price: float = 50.0,
    aff10b5one: str = "0",
    table: str = "nonDerivativeTable",
    txn_tag: str = "nonDerivativeTransaction",
    issuer_cik: str | None = CIK,
) -> str:
    """A raw ownership document. ``issuer_cik=None`` omits the <issuer> block."""
    issuer_block = (
        ""
        if issuer_cik is None
        else f"""
    <issuer>
        <issuerCik>{issuer_cik}</issuerCik>
        <issuerTradingSymbol>GME</issuerTradingSymbol>
    </issuer>"""
    )
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>{issuer_block}
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

    def test_xsl_render_prefix_is_stripped_to_reach_raw_ownership_xml(self):
        """Regression test for the confirmed 100%-parse-failure bug.

        Real ``submissions.json`` reports ownership filings' ``primaryDocument``
        as the XSL-*rendered HTML* view (``xslF345X05/wk-form4.xml``), never the
        raw XML. Fetching that path verbatim returns HTML, `ET.fromstring`
        raises, and `_is_qualifying_purchase` swallowed it as "no purchase" --
        making `insider_trade_flag` structurally incapable of being True.
        Earlier fixtures used bare ``wk-form4.xml`` and so never exercised it.
        """
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["0001990547-26-000014"], docs=["xslF345X05/wk-form4.xml"],
        )
        fetched_urls: list[str] = []

        def fetch_xml(url: str) -> str:
            fetched_urls.append(url)
            # Mirror the real server: the rendered path is HTML, only the raw
            # path is parseable XML.
            if "xslF345X05" in url:
                return _RENDERED_FORM4_HTML
            return _form4_xml(transaction_code="P", shares=1000, price=50.0)

        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)

        assert result is True
        assert fetched_urls == [
            "https://www.sec.gov/Archives/edgar/data/1326380/"
            "000199054726000014/wk-form4.xml"
        ]

    @pytest.mark.parametrize(
        "primary_doc, expected",
        [
            ("xslF345X05/wk-form4.xml", "wk-form4.xml"),   # observed, 1,774 filings
            ("xslF345X06/form4.xml", "form4.xml"),          # observed, 1,235 filings
            ("xslF345X99/doc4.xml", "doc4.xml"),            # future schema version
            ("wk-form4.xml", "wk-form4.xml"),               # already raw -- unchanged
            ("", ""),
        ],
    )
    def test_raw_ownership_document_path(self, primary_doc, expected):
        assert eip._raw_ownership_document_path(primary_doc) == expected

    def test_unparseable_filing_logs_a_warning_distinct_from_no_purchase(self, caplog):
        """A parse failure must be loud; "checked, no purchase" must stay quiet."""
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )

        with caplog.at_level("WARNING"):
            assert eip.fetch_insider_purchase_flag(
                "GME", AS_OF, CIK, fetch_json, lambda url: _RENDERED_FORM4_HTML
            ) is False
        assert "unparseable filing document" in caplog.text
        assert "ticker=GME" in caplog.text

        caplog.clear()
        with caplog.at_level("WARNING"):
            assert eip.fetch_insider_purchase_flag(
                "GME", AS_OF, CIK, fetch_json,
                lambda url: _form4_xml(transaction_code="S"),
            ) is False
        assert caplog.records == []

    def test_filing_where_queried_cik_is_reporting_owner_not_issuer_is_rejected(self):
        """Regression test for the confirmed AEI/HWH false positive (anomaly B-A1).

        ``submissions.json`` lists Form 4s where the queried CIK is merely a
        *reporting owner* of another company alongside the ones where it is the
        issuer -- the two roles have separate top-level flags and the filing
        list does not distinguish them. AEI (Alset Inc., CIK 0001750106) had
        exactly one Form 4 in a 90-day window: a real, well-formed, $500k
        open-market purchase where Alset was the reporting owner and **HWH
        International** was the issuer. Counting it made AEI's
        insider_trade_flag True off another company's purchase.
        """
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-06-09"],
            accessions=["0001493152-26-027990"], docs=["xslF345X05/form4.xml"],
        )
        # Qualifying in every other respect: code P, $500k, not a 10b5-1 plan.
        fetch_xml = lambda url: _form4_xml(
            issuer_cik="0001897245",  # HWH International, not the queried CIK
            transaction_code="P", shares=250_000, price=2.0, aff10b5one="0",
        )

        result = eip.fetch_insider_purchase_flag(
            "AEI", date(2026, 6, 30), "0001750106", fetch_json, fetch_xml,
            lookback_days=90,
        )
        assert result is False

    def test_matching_issuer_cik_still_qualifies_when_filing_writes_it_unpadded(self):
        """No false rejection from a padding mismatch -- the gate normalizes."""
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: _form4_xml(
            issuer_cik="1326380",  # same CIK as the queried one, sans leading zeros
            transaction_code="P", shares=1000, price=50.0,
        )
        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is True

    def test_filing_without_an_issuer_block_is_not_rejected_by_the_issuer_gate(self):
        """An absent <issuerCik> falls through to the transaction gates.

        Real Form 4s always carry one; treating "absent" as "mismatched" would
        turn an unrecognized document shape into a silent False, which is the
        failure mode the XSL parse bug already demonstrated.
        """
        fetch_json = lambda url: _submissions(
            forms=["4"], filing_dates=["2026-07-10"],
            accessions=["acc-1"], docs=["doc.xml"],
        )
        fetch_xml = lambda url: _form4_xml(
            issuer_cik=None, transaction_code="P", shares=1000, price=50.0,
        )
        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is True

    def test_owner_role_filing_does_not_mask_a_later_real_issuer_purchase(self):
        """The rejected filing must not short-circuit the remaining candidates."""
        fetch_json = lambda url: _submissions(
            forms=["4", "4"],
            filing_dates=["2026-07-12", "2026-07-10"],
            accessions=["acc-owner-role", "acc-own-issuer"],
            docs=["owner.xml", "issuer.xml"],
        )
        fetched: list[str] = []

        def fetch_xml(url: str) -> str:
            fetched.append(url)
            other_issuer = "owner.xml" in url
            return _form4_xml(
                issuer_cik="0001897245" if other_issuer else CIK,
                transaction_code="P", shares=1000, price=50.0,
            )

        result = eip.fetch_insider_purchase_flag("GME", AS_OF, CIK, fetch_json, fetch_xml)
        assert result is True
        assert len(fetched) == 2  # kept going past the rejected owner-role filing

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
