"""SEC bulk Insider Transactions Data Sets loader (historical `insider_trade_flag`).

Historical-backfill counterpart to :mod:`edgar_insider_provider`. That module
answers "was there a qualifying insider purchase?" one ticker at a time, from
``submissions.json`` plus one HTTP request per candidate Form 4. Correct, but
the request count is quadratic in the wrong way for a backfill: adjacent
signal dates' 90-day lookback windows overlap by 89 days and Form 4 filings
are immutable once filed, so ~98% of a backfill's traffic re-downloads bytes
that cannot have changed. A measured 130-date backfill over the 3,911-ticker
active universe costs **5,775,143 requests / ~267 hours** on that path
(``reports/insider_flag_cost_optimization_investigation_2026-07-21.md``).

This module answers the same question for the same window from SEC's
**quarterly bulk Insider Transactions Data Sets** -- 3-4 zip downloads,
~40 MB, seconds. Every field the predicate needs is present in the bulk set,
including the ``AFF10B5ONE`` 10b5-1 indicator, and a 120-filing validation
sample showed 120/120 agreement with parsing the raw ownership XML.

**Historical dates only.** The data sets are published quarterly, so the
current (unclosed) quarter is never covered. :meth:`BulkInsiderIndex.
qualifying_purchase_flag` raises rather than silently answering ``False`` for
a window reaching outside the loaded quarters -- an uncovered window looks
exactly like "no purchases" otherwise, which is the same class of silent
false-negative that made the XSL parse bug survive a real batch run. The live
daily step stays on :mod:`edgar_insider_provider`.

Three files are read from each quarterly zip:

===================== ==================================================
``SUBMISSION.tsv``    ``ACCESSION_NUMBER``, ``FILING_DATE``,
                      ``DOCUMENT_TYPE``, ``ISSUERCIK``, ``AFF10B5ONE``
``NONDERIV_TRANS.tsv````TRANS_CODE``, ``TRANS_SHARES``,
                      ``TRANS_PRICEPERSHARE``
``DERIV_TRANS.tsv``   same three, for derivative transactions
===================== ==================================================

``FOOTNOTES.tsv`` is deliberately never extracted: 44 MB of the ~91 MB
uncompressed 2026Q1 set, and nothing here reads it.

Joins on ``ISSUERCIK``, never ``ISSUERTRADINGSYMBOL``: symbol matching
resolves only 3,306 of the 3,911 universe tickers, and the pipeline already
carries a resolved CIK. Note this *inherits* the known CIK-resolution issue
(holdco reorgs such as XOM mapping to the wrong CIK in SEC's own
``company_tickers.json``, anomaly A2 in the investigation) identically to the
live path -- inherited, not introduced, and out of scope here.

Scope, unchanged from :mod:`edgar_insider_provider`: ``insider_trade_flag``
is purely informational/display and must never feed Step 4 eligibility,
scoring, or routing.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, timedelta
from typing import Callable, Final, Iterable, Iterator

from app.providers.edgar_insider_provider import (
    DEFAULT_EXCLUDE_10B5_1,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MIN_TRANSACTION_VALUE_USD,
    _FORM_TYPE_TRANSACTION,
    _TRANSACTION_CODE_PURCHASE,
    _TRUTHY_TOKENS,
)
from app.utils import logging_config

_LOG = logging_config.get_logger(__name__)

# Deliberately imported from the live module rather than redefined: the two
# paths must agree on what qualifies, and a duplicated literal is exactly how
# they would silently drift apart.
_QUALIFICATION_CONSTANTS_SOURCE: Final[str] = "app.providers.edgar_insider_provider"

_LANDING_PAGE_URL: Final[str] = (
    "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
)
_SEC_ORIGIN: Final[str] = "https://www.sec.gov"
_ZIP_FILENAME_TEMPLATE: Final[str] = "{quarter}_form345.zip"

# SEC has moved this dataset between two hosting directories: quarters through
# 2026Q1 sit under /files/structureddata/..., 2026Q2 under
# /files/datastandardsinnovation/... . The landing page is the authority and is
# scraped first; these are the ordered fallbacks if it can't be read.
_ZIP_URL_PREFIX_FALLBACKS: Final[tuple[str, ...]] = (
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/",
    "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/",
)

_SUBMISSION_MEMBER: Final[str] = "SUBMISSION.tsv"
_NONDERIV_MEMBER: Final[str] = "NONDERIV_TRANS.tsv"
_DERIV_MEMBER: Final[str] = "DERIV_TRANS.tsv"
_TRANSACTION_MEMBERS: Final[tuple[str, ...]] = (_NONDERIV_MEMBER, _DERIV_MEMBER)

_COL_ACCESSION: Final[str] = "ACCESSION_NUMBER"
_COL_FILING_DATE: Final[str] = "FILING_DATE"
_COL_DOCUMENT_TYPE: Final[str] = "DOCUMENT_TYPE"
_COL_ISSUER_CIK: Final[str] = "ISSUERCIK"
_COL_AFF10B5ONE: Final[str] = "AFF10B5ONE"
_COL_TRANS_CODE: Final[str] = "TRANS_CODE"
_COL_TRANS_SHARES: Final[str] = "TRANS_SHARES"
_COL_TRANS_PRICE: Final[str] = "TRANS_PRICEPERSHARE"

# SEC note: "In July 2025, the 2023-2025 data sets were updated to include the
# AFF10B5ONE element in the SUBMISSION file." Earlier quarters have no such
# column, so exclude_10b5_1 cannot be honored from bulk data before 2023.
_AFF10B5ONE_FIRST_YEAR: Final[int] = 2023

_QUARTER_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})[qQ]([1-4])$")
_MONTH_ABBR: Final[dict[str, int]] = {
    m: i
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}


# --------------------------------------------------------------------------- #
# Quarter arithmetic and URL resolution
# --------------------------------------------------------------------------- #
def quarters_in_range(start_date: date, end_date: date) -> list[str]:
    """Quarter labels (``"2026q1"``) covering ``[start_date, end_date]``, ascending."""
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} precedes start_date {start_date}")
    quarters: list[str] = []
    year, quarter = start_date.year, (start_date.month - 1) // 3 + 1
    last = (end_date.year, (end_date.month - 1) // 3 + 1)
    while (year, quarter) <= last:
        quarters.append(f"{year}q{quarter}")
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return quarters


def _quarter_bounds(quarter: str) -> tuple[date, date]:
    """Inclusive first/last calendar date of a ``"2026q1"``-style quarter label."""
    match = _QUARTER_RE.match(quarter)
    if not match:
        raise ValueError(f"unrecognized quarter label: {quarter!r} (expected e.g. '2026q1')")
    year, qtr = int(match.group(1)), int(match.group(2))
    first = date(year, 3 * qtr - 2, 1)
    last = date(year + 1, 1, 1) if qtr == 4 else date(year, 3 * qtr + 1, 1)
    return first, last - timedelta(days=1)


def resolve_quarter_zip_urls(
    quarters: Iterable[str],
    fetch_text: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """Map each quarter label to its absolute zip URL.

    Scrapes the landing page when ``fetch_text`` is supplied -- it lists every
    published quarter with whichever hosting directory that quarter actually
    lives under, so it stays correct through further SEC reorganizations. Falls
    back to the known prefixes (newest-style first) when no ``fetch_text`` is
    given or the page can't be read; the caller then discovers a wrong guess as
    a 404 at download time rather than a silent miss.
    """
    wanted = [q.lower() for q in quarters]
    scraped: dict[str, str] = {}
    if fetch_text is not None:
        try:
            scraped = _scrape_landing_page(fetch_text(_LANDING_PAGE_URL))
        except Exception as exc:  # noqa: BLE001 - fall back to static prefixes
            _LOG.warning(
                "edgar_insider_bulk_loader: landing-page scrape failed (%s); "
                "falling back to static URL prefixes",
                exc,
            )

    resolved: dict[str, str] = {}
    for quarter in wanted:
        if quarter in scraped:
            resolved[quarter] = scraped[quarter]
            continue
        if scraped:
            raise ValueError(
                f"quarter {quarter!r} is not published on the SEC insider-transactions "
                f"landing page (published: {min(scraped)}..{max(scraped)}). The bulk "
                "data sets are quarterly; the current unclosed quarter is never covered."
            )
        resolved[quarter] = _ZIP_URL_PREFIX_FALLBACKS[0] + _ZIP_FILENAME_TEMPLATE.format(
            quarter=quarter
        )
    return resolved


def _scrape_landing_page(html: str) -> dict[str, str]:
    """Extract ``{"2026q2": "https://www.sec.gov/files/.../2026q2_form345.zip"}``."""
    found: dict[str, str] = {}
    for href in re.findall(r'href="([^"]*_form345\.zip)"', html):
        filename = href.rsplit("/", 1)[-1]
        quarter = filename.removesuffix("_form345.zip").lower()
        if not _QUARTER_RE.match(quarter):
            continue
        found[quarter] = href if href.startswith("http") else _SEC_ORIGIN + href
    return found


def candidate_zip_urls(quarter: str) -> tuple[str, ...]:
    """Every known URL a quarter's zip might live at, for prefix-fallback probing."""
    filename = _ZIP_FILENAME_TEMPLATE.format(quarter=quarter.lower())
    return tuple(prefix + filename for prefix in _ZIP_URL_PREFIX_FALLBACKS)


# --------------------------------------------------------------------------- #
# TSV parsing
# --------------------------------------------------------------------------- #
def parse_bulk_filing_date(raw: str) -> date | None:
    """Parse a bulk ``FILING_DATE``. Format is ``DD-MON-YYYY`` (e.g. ``02-JAN-2026``).

    ISO ``YYYY-MM-DD`` is also accepted so a change of format upstream degrades
    into working code rather than a silent all-``None`` window gate.
    """
    text = (raw or "").strip()
    if not text:
        return None
    parts = text.split("-")
    if len(parts) == 3 and parts[1].upper() in _MONTH_ABBR:
        try:
            return date(int(parts[2]), _MONTH_ABBR[parts[1].upper()], int(parts[0]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _iter_tsv(archive: zipfile.ZipFile, member: str) -> Iterator[dict[str, str]]:
    """Stream one TSV member's rows.

    Streamed rather than materialized: ``NONDERIV_TRANS`` alone is ~100k rows
    of 28 columns per quarter, and only a small minority ever match a candidate
    accession, so there is no reason to hold the file in memory.
    """
    with archive.open(member) as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
        yield from csv.DictReader(text, delimiter="\t")


def _tsv_fieldnames(archive: zipfile.ZipFile, member: str) -> list[str]:
    """Header row of one TSV member, without reading its body."""
    with archive.open(member) as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
        return list(csv.DictReader(text, delimiter="\t").fieldnames or [])


def _is_truthy(token: str | None) -> bool:
    return (token or "").strip() in _TRUTHY_TOKENS


def qualifying_events_from_archive(
    archive: zipfile.ZipFile,
    *,
    quarter: str,
    min_transaction_value_usd: float = DEFAULT_MIN_TRANSACTION_VALUE_USD,
    exclude_10b5_1: bool = DEFAULT_EXCLUDE_10B5_1,
    ciks: frozenset[str] | None = None,
    allow_missing_aff10b5one: bool = False,
) -> dict[str, set[date]]:
    """Qualifying-purchase filing dates per zero-padded issuer CIK, from one quarter's zip.

    Reproduces :func:`edgar_insider_provider._is_qualifying_purchase` exactly:
    ``DOCUMENT_TYPE == "4"`` (so ``4/A`` amendments are excluded, matching the
    live path's ``form == "4"``), the whole filing dropped when ``AFF10B5ONE``
    is truthy and ``exclude_10b5_1``, and any ``TRANS_CODE == "P"`` row in
    *either* transaction table with ``shares * price >= min_transaction_value_usd``.

    ``ciks`` optionally restricts the result to the active universe, so a
    market-wide quarter (~57k Form 4s) doesn't have to be held in full.
    """
    _check_aff10b5one_available(
        _tsv_fieldnames(archive, _SUBMISSION_MEMBER),
        quarter=quarter,
        exclude_10b5_1=exclude_10b5_1,
        allow_missing_aff10b5one=allow_missing_aff10b5one,
    )

    # accession -> (padded cik, filing date), for Form 4s that clear the
    # filing-level gates. Transaction rows are matched back against this.
    candidates: dict[str, tuple[str, date]] = {}
    for row in _iter_tsv(archive, _SUBMISSION_MEMBER):
        if (row.get(_COL_DOCUMENT_TYPE) or "").strip() != _FORM_TYPE_TRANSACTION:
            continue
        if exclude_10b5_1 and _is_truthy(row.get(_COL_AFF10B5ONE)):
            continue
        cik_raw = (row.get(_COL_ISSUER_CIK) or "").strip()
        if not cik_raw:
            continue
        try:
            cik = str(int(cik_raw)).zfill(10)
        except ValueError:
            continue
        if ciks is not None and cik not in ciks:
            continue
        filing_dt = parse_bulk_filing_date(row.get(_COL_FILING_DATE, ""))
        accession = (row.get(_COL_ACCESSION) or "").strip()
        if filing_dt is None or not accession:
            continue
        candidates[accession] = (cik, filing_dt)

    events: dict[str, set[date]] = {}
    for member in _TRANSACTION_MEMBERS:
        if member not in archive.namelist():
            _LOG.warning(
                "edgar_insider_bulk_loader: %s missing from %s archive", member, quarter
            )
            continue
        for row in _iter_tsv(archive, member):
            accession = (row.get(_COL_ACCESSION) or "").strip()
            match = candidates.get(accession)
            if match is None:
                continue
            if (row.get(_COL_TRANS_CODE) or "").strip() != _TRANSACTION_CODE_PURCHASE:
                continue
            try:
                value = float(row.get(_COL_TRANS_SHARES) or "") * float(
                    row.get(_COL_TRANS_PRICE) or ""
                )
            except ValueError:
                continue
            if value < min_transaction_value_usd:
                continue
            cik, filing_dt = match
            events.setdefault(cik, set()).add(filing_dt)
    return events


def _check_aff10b5one_available(
    fieldnames: list[str],
    *,
    quarter: str,
    exclude_10b5_1: bool,
    allow_missing_aff10b5one: bool,
) -> None:
    """Refuse to silently ignore ``exclude_10b5_1`` on pre-2023 bulk data."""
    if not exclude_10b5_1 or _COL_AFF10B5ONE in fieldnames:
        return
    message = (
        f"SUBMISSION.tsv for {quarter} has no {_COL_AFF10B5ONE} column "
        f"(SEC added it from {_AFF10B5ONE_FIRST_YEAR} onward), so exclude_10b5_1=True "
        "cannot be honored from bulk data for this quarter. Every 10b5-1 scheduled-plan "
        "purchase would be counted as a qualifying open-market purchase. Use the live "
        "per-ticker path for pre-2023 dates, or pass allow_missing_aff10b5one=True to "
        "accept the looser predicate deliberately."
    )
    if not allow_missing_aff10b5one:
        raise ValueError(message)
    _LOG.warning("edgar_insider_bulk_loader: %s", message)


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
class BulkInsiderIndex:
    """Point-in-time ``insider_trade_flag`` answers over a fixed covered window.

    Built by :func:`build_bulk_insider_index`. Holds only qualifying-purchase
    *filing dates* per issuer CIK, so one load answers every signal date in the
    covered window at no further cost -- which is the whole point, given that a
    130-date backfill re-asks the same question 130 times over 89-day-overlapping
    windows.
    """

    def __init__(
        self,
        events: dict[str, set[date]],
        covered_start: date,
        covered_end: date,
        *,
        quarters: tuple[str, ...] = (),
    ) -> None:
        self._events = {cik: tuple(sorted(dates)) for cik, dates in events.items()}
        self.covered_start = covered_start
        self.covered_end = covered_end
        self.quarters = quarters

    @property
    def issuer_count(self) -> int:
        """Issuers with at least one qualifying purchase in the covered window."""
        return len(self._events)

    def qualifying_purchase_flag(
        self,
        cik: str,
        as_of_date: date,
        *,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> bool:
        """Whether *cik* has a qualifying purchase filed in ``(as_of - lookback, as_of]``.

        Same window semantics as
        :func:`edgar_insider_provider.fetch_insider_purchase_flag` -- filing
        date, never transaction date, exclusive lower bound, inclusive upper.

        Returns ``bool``, not ``bool | None``: bulk data has no per-ticker
        retrieval step that can fail, so the live path's ``None`` case has no
        analogue here. Load-time failures raise instead.

        Raises ``ValueError`` if the requested window isn't fully covered by
        the loaded quarters -- an uncovered window is indistinguishable from
        "no qualifying purchases" and must not be answered.
        """
        window_start = as_of_date - timedelta(days=lookback_days)
        if window_start < self.covered_start or as_of_date > self.covered_end:
            raise ValueError(
                f"window ({window_start}, {as_of_date}] for CIK {cik} is not fully "
                f"covered by loaded bulk quarters {self.quarters or '(none)'} "
                f"({self.covered_start}..{self.covered_end}). Load the quarters "
                "spanning the lookback window, or use the live per-ticker path."
            )
        filing_dates = self._events.get(str(cik).zfill(10))
        if not filing_dates:
            return False
        return any(window_start < filed <= as_of_date for filed in filing_dates)


def build_bulk_insider_index(
    start_date: date,
    end_date: date,
    fetch_bytes: Callable[[str], bytes],
    *,
    fetch_text: Callable[[str], str] | None = None,
    min_transaction_value_usd: float = DEFAULT_MIN_TRANSACTION_VALUE_USD,
    exclude_10b5_1: bool = DEFAULT_EXCLUDE_10B5_1,
    ciks: Iterable[str] | None = None,
    allow_missing_aff10b5one: bool = False,
) -> BulkInsiderIndex:
    """Download and index every bulk quarter covering ``[start_date, end_date]``.

    ``start_date`` must already be the *lookback window* start, not the first
    signal date -- a 90-day lookback on a 2026-01-02 signal date needs 2025Q4
    loaded too. ``fetch_bytes``/``fetch_text`` are injected (the real ones are
    :meth:`_SecHttpClient.get_bytes` / ``get_text`` from
    ``edgar_provider.build_sec_http_client``) so this module does no I/O of its
    own and tests stay fully offline -- same DI shape as
    :mod:`edgar_insider_provider`.
    """
    quarters = quarters_in_range(start_date, end_date)
    urls = resolve_quarter_zip_urls(quarters, fetch_text)
    cik_filter = (
        frozenset(str(c).zfill(10) for c in ciks) if ciks is not None else None
    )

    merged: dict[str, set[date]] = {}
    for quarter in quarters:
        payload = _download_quarter(quarter, urls[quarter], fetch_bytes)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            events = qualifying_events_from_archive(
                archive,
                quarter=quarter,
                min_transaction_value_usd=min_transaction_value_usd,
                exclude_10b5_1=exclude_10b5_1,
                ciks=cik_filter,
                allow_missing_aff10b5one=allow_missing_aff10b5one,
            )
        for cik, dates in events.items():
            merged.setdefault(cik, set()).update(dates)
        _LOG.info(
            "edgar_insider_bulk_loader: %s indexed -- %d issuer(s) with qualifying purchases",
            quarter,
            len(events),
        )

    covered_start, _ = _quarter_bounds(quarters[0])
    _, covered_end = _quarter_bounds(quarters[-1])
    return BulkInsiderIndex(
        merged, covered_start, covered_end, quarters=tuple(quarters)
    )


def _download_quarter(
    quarter: str, url: str, fetch_bytes: Callable[[str], bytes]
) -> bytes:
    """Fetch one quarter's zip, probing the alternate URL prefix on failure."""
    attempted: list[str] = []
    for candidate in (url, *(u for u in candidate_zip_urls(quarter) if u != url)):
        attempted.append(candidate)
        try:
            return fetch_bytes(candidate)
        except Exception as exc:  # noqa: BLE001 - try the other hosting directory
            _LOG.warning(
                "edgar_insider_bulk_loader: %s download failed from %s: %s",
                quarter,
                candidate,
                exc,
            )
    raise RuntimeError(
        f"could not download bulk insider data set for {quarter}; tried {attempted}"
    )


__all__ = [
    "BulkInsiderIndex",
    "build_bulk_insider_index",
    "qualifying_events_from_archive",
    "quarters_in_range",
    "resolve_quarter_zip_urls",
    "candidate_zip_urls",
    "parse_bulk_filing_date",
]
