"""Tests for `app/providers/edgar_insider_bulk_loader.py` (SEC bulk Insider
Transactions Data Sets, historical `insider_trade_flag`).

Fully offline: every test builds an in-memory zip of fixture TSVs and injects
fake `fetch_bytes`/`fetch_text` callables, never a real download. Covers the
qualification predicate's parity with the live raw-XML path, the CIK-based
join (explicitly *not* symbol-based), both hosting-directory URL prefixes,
the pre-2023 `AFF10B5ONE` guard, and the uncovered-window refusal.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from app.providers import edgar_insider_bulk_loader as bulk

AS_OF = date(2026, 3, 15)

_SUBMISSION_COLS = [
    "ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE",
    "ISSUERCIK", "ISSUERNAME", "ISSUERTRADINGSYMBOL", "AFF10B5ONE",
]
_TRANS_COLS = [
    "ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE",
    "TRANS_SHARES", "TRANS_PRICEPERSHARE",
]


def _tsv(columns: list[str], rows: list[dict[str, str]]) -> str:
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row.get(c, "") for c in columns) for row in rows)
    return "\n".join(lines) + "\n"


def _submission_row(
    *,
    accession: str = "0000789019-26-000028",
    filing_date: str = "10-MAR-2026",
    document_type: str = "4",
    cik: str = "789019",
    symbol: str = "MSFT",
    aff10b5one: str = "0",
) -> dict[str, str]:
    return {
        "ACCESSION_NUMBER": accession,
        "FILING_DATE": filing_date,
        "DOCUMENT_TYPE": document_type,
        "ISSUERCIK": cik,
        "ISSUERNAME": "TEST ISSUER",
        "ISSUERTRADINGSYMBOL": symbol,
        "AFF10B5ONE": aff10b5one,
    }


def _trans_row(
    *,
    accession: str = "0000789019-26-000028",
    code: str = "P",
    shares: str = "1000",
    price: str = "50.0",
) -> dict[str, str]:
    return {
        "ACCESSION_NUMBER": accession,
        "TRANS_DATE": "09-MAR-2026",
        "TRANS_CODE": code,
        "TRANS_SHARES": shares,
        "TRANS_PRICEPERSHARE": price,
    }


def _archive(
    submissions: list[dict[str, str]],
    nonderiv: list[dict[str, str]] | None = None,
    deriv: list[dict[str, str]] | None = None,
    *,
    submission_cols: list[str] | None = None,
    include_footnotes: bool = True,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SUBMISSION.tsv", _tsv(submission_cols or _SUBMISSION_COLS, submissions)
        )
        archive.writestr("NONDERIV_TRANS.tsv", _tsv(_TRANS_COLS, nonderiv or []))
        archive.writestr("DERIV_TRANS.tsv", _tsv(_TRANS_COLS, deriv or []))
        if include_footnotes:
            # Present in the real zip (44MB of the ~91MB set) and never read.
            archive.writestr("FOOTNOTES.tsv", "ACCESSION_NUMBER\tFOOTNOTE_ID\n")
    return buffer.getvalue()


def _events(zip_bytes: bytes, **kwargs):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        return bulk.qualifying_events_from_archive(archive, quarter="2026q1", **kwargs)


class TestQuartersInRange:
    def test_single_quarter(self):
        assert bulk.quarters_in_range(date(2026, 1, 5), date(2026, 3, 31)) == ["2026q1"]

    def test_spans_year_boundary(self):
        assert bulk.quarters_in_range(date(2025, 9, 23), date(2026, 6, 30)) == [
            "2025q3", "2025q4", "2026q1", "2026q2",
        ]

    def test_reversed_range_raises(self):
        with pytest.raises(ValueError, match="precedes"):
            bulk.quarters_in_range(date(2026, 6, 30), date(2026, 1, 1))


class TestFilingDateParsing:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("02-JAN-2026", date(2026, 1, 2)),   # the real bulk format
            ("31-DEC-2025", date(2025, 12, 31)),
            ("2026-03-10", date(2026, 3, 10)),   # ISO tolerated
            ("", None),
            ("not-a-date", None),
            ("32-JAN-2026", None),
        ],
    )
    def test_parse(self, raw, expected):
        assert bulk.parse_bulk_filing_date(raw) == expected


class TestUrlResolution:
    LANDING_HTML = """
      <a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip">2026 Q2</a>
      <a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">2026 Q1</a>
      <a href="/files/structureddata/data/insider-transactions-data-sets/2025q4_form345.zip">2025 Q4</a>
    """

    def test_both_hosting_prefixes_resolved_from_landing_page(self):
        """2026Q2 moved to /datastandardsinnovation/; earlier quarters did not.

        A hardcoded prefix silently breaks on whichever side it guessed wrong.
        """
        urls = bulk.resolve_quarter_zip_urls(
            ["2026q1", "2026q2"], lambda url: self.LANDING_HTML
        )
        assert urls["2026q1"] == (
            "https://www.sec.gov/files/structureddata/data/"
            "insider-transactions-data-sets/2026q1_form345.zip"
        )
        assert urls["2026q2"] == (
            "https://www.sec.gov/files/datastandardsinnovation/data/"
            "insider-transactions-data-sets/2026q2_form345.zip"
        )

    def test_unpublished_quarter_raises_rather_than_guessing(self):
        with pytest.raises(ValueError, match="not published"):
            bulk.resolve_quarter_zip_urls(["2026q3"], lambda url: self.LANDING_HTML)

    def test_falls_back_to_static_prefix_when_landing_page_unreadable(self, caplog):
        def _boom(url: str) -> str:
            raise RuntimeError("503")

        with caplog.at_level("WARNING"):
            urls = bulk.resolve_quarter_zip_urls(["2026q1"], _boom)
        assert "landing-page scrape failed" in caplog.text
        assert urls["2026q1"].endswith("2026q1_form345.zip")

    def test_candidate_urls_cover_both_prefixes(self):
        candidates = bulk.candidate_zip_urls("2026q2")
        assert len(candidates) == 2
        assert any("structureddata" in u for u in candidates)
        assert any("datastandardsinnovation" in u for u in candidates)
        assert all(u.endswith("2026q2_form345.zip") for u in candidates)


class TestQualificationPredicate:
    def test_purchase_above_threshold_qualifies(self):
        events = _events(_archive([_submission_row()], [_trans_row()]))
        assert events == {"0000789019": {date(2026, 3, 10)}}

    def test_10b5_1_filing_excluded(self):
        assert _events(_archive([_submission_row(aff10b5one="1")], [_trans_row()])) == {}
        # Bulk emits lowercase 'true' where raw XML emits '1' -- both are in
        # the live module's _TRUTHY_TOKENS, which this module reuses.
        assert _events(_archive([_submission_row(aff10b5one="true")], [_trans_row()])) == {}

    def test_10b5_1_included_when_exclude_disabled(self):
        events = _events(
            _archive([_submission_row(aff10b5one="1")], [_trans_row()]),
            exclude_10b5_1=False,
        )
        assert events == {"0000789019": {date(2026, 3, 10)}}

    def test_non_purchase_code_excluded(self):
        assert _events(_archive([_submission_row()], [_trans_row(code="S")])) == {}

    def test_below_dollar_threshold_excluded(self):
        assert _events(
            _archive([_submission_row()], [_trans_row(shares="10", price="5.0")])
        ) == {}

    def test_derivative_transaction_also_counts(self):
        """The live path checks both sibling tables; bulk splits them into two files."""
        events = _events(_archive([_submission_row()], [], [_trans_row()]))
        assert events == {"0000789019": {date(2026, 3, 10)}}

    def test_amendments_excluded_matching_live_form_equals_4(self):
        """Bulk carries 4/A as a distinct DOCUMENT_TYPE; live matches form == '4' exactly."""
        assert _events(
            _archive([_submission_row(document_type="4/A")], [_trans_row()])
        ) == {}

    def test_form_3_and_5_excluded(self):
        rows = [
            _submission_row(accession="acc-3", document_type="3"),
            _submission_row(accession="acc-5", document_type="5"),
        ]
        trans = [_trans_row(accession="acc-3"), _trans_row(accession="acc-5")]
        assert _events(_archive(rows, trans)) == {}

    def test_transaction_row_without_matching_submission_is_ignored(self):
        assert _events(
            _archive([_submission_row()], [_trans_row(accession="orphan-acc")])
        ) == {}

    def test_unparseable_shares_or_price_skipped_not_crashed(self):
        events = _events(
            _archive(
                [_submission_row()],
                [_trans_row(shares="", price=""), _trans_row(shares="n/a", price="1")],
            )
        )
        assert events == {}

    def test_footnotes_tsv_is_never_read(self):
        """A7: FOOTNOTES.tsv is 44MB of the set and irrelevant here."""
        opened: list[str] = []
        raw = _archive([_submission_row()], [_trans_row()])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            original_open = archive.open

            def tracking_open(name, *args, **kwargs):
                opened.append(name if isinstance(name, str) else name.filename)
                return original_open(name, *args, **kwargs)

            archive.open = tracking_open  # type: ignore[method-assign]
            bulk.qualifying_events_from_archive(archive, quarter="2026q1")
        assert "FOOTNOTES.tsv" not in opened


class TestCikJoin:
    def test_join_is_on_issuercik_not_trading_symbol(self):
        """Symbol matching resolves only 3,306 of 3,911 universe tickers; CIK is stable.

        Two filings share a trading symbol but belong to different CIKs -- a
        symbol-keyed index would collapse them; a CIK-keyed one must not.
        """
        rows = [
            _submission_row(accession="acc-a", cik="34088", symbol="XOM"),
            _submission_row(accession="acc-b", cik="2115436", symbol="XOM"),
        ]
        trans = [_trans_row(accession="acc-a")]
        events = _events(_archive(rows, trans))
        assert events == {"0000034088": {date(2026, 3, 10)}}
        assert "0002115436" not in events

    def test_cik_is_zero_padded_to_ten_digits(self):
        events = _events(_archive([_submission_row(cik="1326380")], [_trans_row()]))
        assert list(events) == ["0001326380"]

    def test_cik_filter_restricts_to_supplied_universe(self):
        rows = [
            _submission_row(accession="acc-a", cik="789019"),
            _submission_row(accession="acc-b", cik="320193"),
        ]
        trans = [_trans_row(accession="acc-a"), _trans_row(accession="acc-b")]
        events = _events(_archive(rows, trans), ciks=frozenset({"0000789019"}))
        assert list(events) == ["0000789019"]

    def test_blank_or_malformed_cik_skipped(self):
        rows = [
            _submission_row(accession="acc-a", cik=""),
            _submission_row(accession="acc-b", cik="not-a-cik"),
        ]
        trans = [_trans_row(accession="acc-a"), _trans_row(accession="acc-b")]
        assert _events(_archive(rows, trans)) == {}


class TestPre2023Aff10b5OneGuard:
    """AFF10B5ONE only exists in bulk data from 2023 onward."""

    LEGACY_COLS = [c for c in _SUBMISSION_COLS if c != "AFF10B5ONE"]

    def test_missing_column_raises_when_exclusion_requested(self):
        raw = _archive(
            [_submission_row()], [_trans_row()], submission_cols=self.LEGACY_COLS
        )
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            with pytest.raises(ValueError, match="AFF10B5ONE"):
                bulk.qualifying_events_from_archive(
                    archive, quarter="2019q4", exclude_10b5_1=True
                )

    def test_missing_column_warns_when_explicitly_allowed(self, caplog):
        raw = _archive(
            [_submission_row()], [_trans_row()], submission_cols=self.LEGACY_COLS
        )
        with zipfile.ZipFile(io.BytesIO(raw)) as archive, caplog.at_level("WARNING"):
            events = bulk.qualifying_events_from_archive(
                archive,
                quarter="2019q4",
                exclude_10b5_1=True,
                allow_missing_aff10b5one=True,
            )
        assert "AFF10B5ONE" in caplog.text
        assert events == {"0000789019": {date(2026, 3, 10)}}

    def test_missing_column_is_fine_when_exclusion_not_requested(self):
        raw = _archive(
            [_submission_row()], [_trans_row()], submission_cols=self.LEGACY_COLS
        )
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            events = bulk.qualifying_events_from_archive(
                archive, quarter="2019q4", exclude_10b5_1=False
            )
        assert events == {"0000789019": {date(2026, 3, 10)}}


class TestBulkInsiderIndex:
    def _index(self, events=None):
        return bulk.BulkInsiderIndex(
            events if events is not None else {"0000789019": {date(2026, 3, 10)}},
            covered_start=date(2025, 10, 1),
            covered_end=date(2026, 3, 31),
            quarters=("2025q4", "2026q1"),
        )

    def test_purchase_inside_window_is_true(self):
        assert self._index().qualifying_purchase_flag("0000789019", AS_OF) is True

    def test_unknown_cik_is_false(self):
        assert self._index().qualifying_purchase_flag("0000320193", AS_OF) is False

    def test_filing_after_as_of_date_excluded(self):
        index = self._index({"0000789019": {date(2026, 3, 16)}})
        assert index.qualifying_purchase_flag("0000789019", AS_OF) is False

    def test_filing_outside_lookback_excluded(self):
        index = self._index({"0000789019": {date(2025, 11, 1)}})
        assert index.qualifying_purchase_flag("0000789019", AS_OF, lookback_days=90) is False

    def test_window_lower_bound_is_exclusive_matching_live_path(self):
        boundary = AS_OF - __import__("datetime").timedelta(days=90)
        index = self._index({"0000789019": {boundary}})
        assert index.qualifying_purchase_flag("0000789019", AS_OF, lookback_days=90) is False

    def test_unpadded_cik_accepted(self):
        assert self._index().qualifying_purchase_flag("789019", AS_OF) is True

    def test_uncovered_window_raises_rather_than_answering_false(self):
        """An uncovered window is indistinguishable from 'no purchases' -- refuse it."""
        index = self._index()
        with pytest.raises(ValueError, match="not fully covered"):
            index.qualifying_purchase_flag("0000789019", date(2026, 7, 15))
        with pytest.raises(ValueError, match="not fully covered"):
            index.qualifying_purchase_flag("0000789019", date(2025, 10, 15))


class TestBuildBulkInsiderIndex:
    LANDING_HTML = (
        '<a href="/files/structureddata/data/insider-transactions-data-sets/'
        '2026q1_form345.zip">Q1</a>'
    )

    def test_end_to_end_offline(self):
        payload = _archive([_submission_row()], [_trans_row()])
        fetched: list[str] = []

        def fetch_bytes(url: str) -> bytes:
            fetched.append(url)
            return payload

        index = bulk.build_bulk_insider_index(
            date(2026, 1, 5),
            date(2026, 3, 20),
            fetch_bytes,
            fetch_text=lambda url: self.LANDING_HTML,
        )
        assert len(fetched) == 1
        assert index.quarters == ("2026q1",)
        assert index.issuer_count == 1
        # Only 2026q1 was loaded, so the queried window must stay inside it --
        # start_date is the *lookback* start, per the function's contract.
        assert index.qualifying_purchase_flag("0000789019", AS_OF, lookback_days=60) is True

    def test_falls_back_to_alternate_prefix_on_download_failure(self):
        payload = _archive([_submission_row()], [_trans_row()])
        attempted: list[str] = []

        def fetch_bytes(url: str) -> bytes:
            attempted.append(url)
            if "structureddata" in url:
                raise RuntimeError("404 Not Found")
            return payload

        index = bulk.build_bulk_insider_index(
            date(2026, 1, 5), date(2026, 3, 20), fetch_bytes
        )
        assert len(attempted) == 2
        assert "datastandardsinnovation" in attempted[1]
        assert index.qualifying_purchase_flag("0000789019", AS_OF, lookback_days=60) is True

    def test_all_prefixes_failing_raises(self):
        def fetch_bytes(url: str) -> bytes:
            raise RuntimeError("404 Not Found")

        with pytest.raises(RuntimeError, match="could not download"):
            bulk.build_bulk_insider_index(date(2026, 1, 5), date(2026, 3, 20), fetch_bytes)

    def test_multiple_quarters_merge_and_extend_coverage(self):
        q4 = _archive(
            [_submission_row(accession="acc-q4", filing_date="15-DEC-2025", cik="320193")],
            [_trans_row(accession="acc-q4")],
        )
        q1 = _archive([_submission_row()], [_trans_row()])
        payloads = {"2025q4": q4, "2026q1": q1}

        def fetch_bytes(url: str) -> bytes:
            return payloads["2025q4" if "2025q4" in url else "2026q1"]

        index = bulk.build_bulk_insider_index(
            date(2025, 12, 1), date(2026, 3, 20), fetch_bytes
        )
        assert index.quarters == ("2025q4", "2026q1")
        assert index.covered_start == date(2025, 10, 1)
        assert index.covered_end == date(2026, 3, 31)
        assert index.issuer_count == 2
        assert index.qualifying_purchase_flag("0000320193", date(2026, 1, 10)) is True
