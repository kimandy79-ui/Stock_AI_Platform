"""SEC-EDGAR-native `insider_trade_flag` lookup (P2.7, Step-4 scale).

Replaces the FMP-based approach (`fmp_insider_provider.py`, removed --
`reports/fmp_insider_trade_flag_implementation_2026-07-18.md`'s addendum
confirmed the FMP endpoint it depended on returns HTTP 402 on this project's
actual plan). SEC EDGAR removes the cost ceiling that forced that approach's
Step-5-only workaround: no daily call quota, just a fair-access rate limit
this codebase already throttles under for other EDGAR calls.

Narrow, single-purpose module -- not a :class:`MarketDataProvider`, same
reasoning as the FMP module it replaces: it implements exactly one
capability via a plain function, taking injected `fetch_json`/
`fetch_filing_xml` callables (no I/O of its own, fully testable offline).
The real HTTP client is built and shared by the caller
(`pipeline_orchestrator.py`) via `edgar_provider.build_sec_http_client` --
see that module's docstring for why sharing one client/throttle across both
`companyfacts` and this module's requests matters.

Two-request-shape design, confirmed by the SEC EDGAR investigation
(`reports/sec_edgar_issuer_ownership_lookup_investigation_2026-07-18.md`):

1. ``data.sec.gov/submissions/CIK##########.json`` -- lists a CIK's recent
   filings, including ownership (Form 3/4/5) filings this CIK did not submit
   itself. Confirmed against real AAPL/NVDA/GME filings, cross-checked
   against independently-sourced FMP data for the same accession numbers.
   Exact ``form == "4"`` string matching -- unlike the older
   ``browse-edgar?type=4`` HTML/atom interface, which does *prefix* matching
   and pulls in ``"425"``/``"424B2"`` alongside real Form 4s (a confirmed
   anomaly; this module deliberately does not use that interface).

   Critically, that list mixes filings where the CIK is the **issuer** with
   filings where it is merely a **reporting owner** of some *other* company
   (``insiderTransactionForOwnerExists`` is a separate top-level flag from
   ``insiderTransactionForIssuerExists``, and both can be set). The role is
   not recoverable from ``submissions.json`` at all -- only from the filing
   XML's ``<issuer><issuerCik>`` -- so the issuer check lives in step 2.
2. The raw Form 4 XML at each candidate filing's ``primaryDocument`` path,
   with the XSL-renderer directory prefix stripped (see
   ``_raw_ownership_document_path``) -- a plain, non-namespaced
   ``<ownershipDocument>`` schema giving the ``<issuer><issuerCik>`` needed
   to confirm the requested CIK is this filing's *issuer* (see step 1), and
   the exact transaction data needed:
   ``transactionCoding/transactionCode`` (SEC's
   single-letter code, ``"P"`` for purchase -- the same vocabulary FMP's
   ``P-Purchase`` label was built from), ``transactionAmounts`` (shares,
   price), and a genuine bonus this module exploits that FMP's response
   shape never had at all: a document-level ``<aff10b5One>`` flag --
   Rule 10b5-1(c) affirmative-defense trading-plan indicator -- letting
   this module *actually exclude* 10b5-1 scheduled-plan purchases rather
   than merely documenting the gap as an unresolved caveat.

Point-in-time integrity: gated on ``filingDate`` (the SEC-filed date from
``submissions.json``, not the transaction date) falling in
``(as_of_date - lookback_days, as_of_date]`` -- same discipline as this
provider's other EDGAR fields (see ``edgar_provider.py``'s
``extract_annual_series`` dual end/filed-date check). A transaction dated
before ``as_of_date`` but not yet *filed* as of that date is not knowable.

Scope, still binding from both coder notes: this field is purely
informational/display. It is computed and stored in ``ticker_fundamentals``
alongside the other 5 EDGAR fundamentals fields, but must never feed Step 4
eligibility, scoring, or routing, and carries no ``risk_label_config``
score weight.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Callable, Final

from app.utils import logging_config

_LOG = logging_config.get_logger(__name__)

_SUBMISSIONS_URL: Final[str] = "https://data.sec.gov/submissions/CIK{cik}.json"
_FILING_XML_URL: Final[str] = (
    "https://www.sec.gov/Archives/edgar/data/{cik_nolead}/{accession_nodash}/{doc}"
)

_FORM_TYPE_TRANSACTION: Final[str] = "4"
# ``primaryDocument`` for ownership filings points at the *XSL-rendered HTML*
# view, not the raw XML -- e.g. "xslF345X05/wk-form4.xml". The renderer
# directory name varies by form-schema version (xslF345X05, xslF345X06, ...).
# Stripping that leading directory yields the raw <ownershipDocument> XML in
# the same accession folder. Matched case-insensitively on the "xsl" prefix so
# a future renderer version needs no code change.
_XSL_RENDER_DIR_PREFIX: Final[str] = "xsl"
_TRANSACTION_CODE_PURCHASE: Final[str] = "P"
_TRUTHY_TOKENS: Final[frozenset[str]] = frozenset({"1", "true", "True", "TRUE"})

# Ownership filings carry transactions under one or both of these tables;
# FMP's flat response shape didn't distinguish derivative/non-derivative, so
# neither does this module -- both are checked for a qualifying "P" code.
_TRANSACTION_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("nonDerivativeTable", "nonDerivativeTransaction"),
    ("derivativeTable", "derivativeTransaction"),
)

DEFAULT_LOOKBACK_DAYS: Final[int] = 90
DEFAULT_MIN_TRANSACTION_VALUE_USD: Final[float] = 10_000.0
DEFAULT_EXCLUDE_10B5_1: Final[bool] = True
# Safety bound on how many candidate Form 4s get their content fetched for a
# single ticker -- caps worst-case per-ticker request cost for an
# unusually high-Form-4-frequency issuer within the lookback window.
_DEFAULT_MAX_CANDIDATE_FILINGS: Final[int] = 50


def fetch_insider_purchase_flag(
    ticker: str,
    as_of_date: date,
    cik: str,
    fetch_json: Callable[[str], Any],
    fetch_filing_xml: Callable[[str], Any],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_transaction_value_usd: float = DEFAULT_MIN_TRANSACTION_VALUE_USD,
    exclude_10b5_1: bool = DEFAULT_EXCLUDE_10B5_1,
    max_candidate_filings: int = _DEFAULT_MAX_CANDIDATE_FILINGS,
) -> bool | None:
    """Whether a qualifying open-market insider purchase exists for *ticker*.

    Returns ``True`` if at least one Form 4 (exact ``form == "4"``, not a
    ``type=4``-prefix false-positive like ``"425"``) filed in
    ``(as_of_date - lookback_days, as_of_date]`` contains a
    ``transactionCode == "P"`` entry with ``shares * price >=
    min_transaction_value_usd``, and (when ``exclude_10b5_1``) whose filing
    is not flagged ``aff10b5One`` (a Rule 10b5-1(c) scheduled trading plan).
    Returns ``False`` if data was retrieved but nothing qualified. Returns
    ``None`` only on a genuine retrieval failure (network error, malformed
    response) -- callers must not conflate ``False`` and ``None``.

    ``cik`` is the zero-padded SEC CIK the caller has already resolved (this
    function does not re-resolve it). ``fetch_json`` takes a URL and returns
    parsed JSON; ``fetch_filing_xml`` takes a URL and returns the raw
    response text. Both are required (no default HTTP client here, unlike
    the removed FMP module) -- the real ones are built once by
    ``pipeline_orchestrator.py`` via ``edgar_provider.build_sec_http_client``
    and shared with the provider's own ``companyfacts`` fetches, so tests
    must always inject fakes to stay fully offline.
    """
    cik_padded = str(cik).zfill(10)
    try:
        submissions = fetch_json(_SUBMISSIONS_URL.format(cik=cik_padded))
    except Exception as exc:  # noqa: BLE001 - any retrieval failure -> None, never a crash
        _LOG.warning(
            "edgar_insider_provider: submissions fetch failed ticker=%s: %s", ticker, exc
        )
        return None

    if not submissions.get("insiderTransactionForIssuerExists"):
        # Cheap short-circuit: zero further requests for a ticker with no
        # insider-ownership filing history ever recorded against its CIK.
        return False

    recent = ((submissions.get("filings") or {}).get("recent")) or {}
    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    accession_numbers = recent.get("accessionNumber") or []
    primary_documents = recent.get("primaryDocument") or []

    window_start = as_of_date - timedelta(days=lookback_days)
    candidates: list[tuple[str, str]] = []
    for i, form in enumerate(forms):
        if form != _FORM_TYPE_TRANSACTION:
            continue
        try:
            filing_dt = date.fromisoformat(str(filing_dates[i])[:10])
        except (ValueError, IndexError):
            continue
        # Point-in-time gate: filingDate (SEC-filed date), never
        # transactionDate -- mirrors edgar_provider.py's extract_annual_series.
        if filing_dt > as_of_date or filing_dt <= window_start:
            continue
        try:
            accession = accession_numbers[i]
            primary_doc = primary_documents[i]
        except IndexError:
            continue
        if not accession or not primary_doc:
            continue
        candidates.append((accession, primary_doc))
        if len(candidates) >= max_candidate_filings:
            break

    if not candidates:
        return False

    cik_nolead = str(int(cik_padded))
    try:
        for accession, primary_doc in candidates:
            url = _FILING_XML_URL.format(
                cik_nolead=cik_nolead,
                accession_nodash=accession.replace("-", ""),
                doc=_raw_ownership_document_path(primary_doc),
            )
            xml_text = fetch_filing_xml(url)
            if _is_qualifying_purchase(
                xml_text,
                min_transaction_value_usd,
                exclude_10b5_1,
                expected_cik=cik_padded,
                ticker=ticker,
                url=url,
            ):
                return True  # early exit -- no need to fetch remaining candidates
    except Exception as exc:  # noqa: BLE001 - any retrieval failure -> None, never a crash
        _LOG.warning(
            "edgar_insider_provider: filing fetch/parse failed ticker=%s: %s", ticker, exc
        )
        return None

    return False


def _raw_ownership_document_path(primary_doc: str) -> str:
    """Strip the XSL-renderer directory from a ``primaryDocument`` path.

    ``submissions.json`` reports ownership filings' ``primaryDocument`` as the
    *rendered HTML* view -- ``"xslF345X05/wk-form4.xml"`` -- which is not XML
    and cannot be parsed as an ``<ownershipDocument>``. The raw XML sits at the
    bare filename in the same accession folder, so the leading ``xsl*``
    directory is dropped. Paths without such a prefix pass through unchanged.
    """
    head, sep, tail = primary_doc.partition("/")
    if sep and tail and head.lower().startswith(_XSL_RENDER_DIR_PREFIX):
        return tail
    return primary_doc


def _is_qualifying_purchase(
    xml_text: str,
    min_transaction_value_usd: float,
    exclude_10b5_1: bool,
    *,
    expected_cik: str,
    ticker: str = "",
    url: str = "",
) -> bool:
    """Parse one raw Form 4 ``<ownershipDocument>`` XML for a qualifying purchase.

    ``expected_cik`` is the zero-padded CIK the flag is being computed *for*,
    and the filing is rejected outright unless it names that CIK as its
    ``<issuer><issuerCik>``. This is not redundant with the
    ``submissions.json`` lookup that produced the candidate: that payload
    also lists filings where the CIK is a *reporting owner* of another
    company, and counting one of those credits another issuer's insider
    purchase to this ticker (confirmed on AEI/Alset, whose only Form 4 in
    window was a $500k open-market purchase of *HWH* stock -- see the module
    docstring's step 1). Any holdco, parent/affiliate, or 10%-owner corporate
    can hit this.

    A malformed/unparseable document is treated as "no qualifying purchase
    found here" (``False``), not a retrieval failure -- the filing was
    successfully fetched, it just isn't usable; the outer loop moves on to
    the next candidate rather than failing the whole ticker.

    That fallthrough is deliberately *logged*, not silent: an unparseable
    filing and a cleanly-parsed filing with no purchase both return ``False``,
    and conflating the two is exactly how the XSL-path bug produced a 100%
    false-negative rate through a real batch run without anything looking
    wrong. ``ticker``/``url`` are for that log line only.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        _LOG.warning(
            "edgar_insider_provider: unparseable filing document ticker=%s url=%s: %s",
            ticker or "?",
            url or "?",
            exc,
        )
        return False

    # Issuer-role gate. Zero-padded to 10 the same way the submissions URL's
    # CIK is (``str(cik).zfill(10)``), so a filing writing the CIK unpadded
    # still matches. A document with no <issuerCik> at all is not rejected:
    # real Form 4s always carry one, so an absent value means a shape this
    # module doesn't recognize, and the transaction gates below still apply.
    issuer_cik = (root.findtext("issuer/issuerCik") or "").strip()
    if issuer_cik and issuer_cik.zfill(10) != expected_cik:
        _LOG.debug(
            "edgar_insider_provider: skipping filing whose issuer is another CIK "
            "ticker=%s expected_cik=%s issuer_cik=%s url=%s",
            ticker or "?",
            expected_cik,
            issuer_cik,
            url or "?",
        )
        return False

    if exclude_10b5_1:
        aff = root.findtext("aff10b5One")
        if aff is not None and aff.strip() in _TRUTHY_TOKENS:
            return False  # whole filing excluded: pursuant to a 10b5-1 plan

    for table_tag, txn_tag in _TRANSACTION_TABLES:
        table = root.find(table_tag)
        if table is None:
            continue
        for txn in table.findall(txn_tag):
            code = txn.findtext("transactionCoding/transactionCode")
            if code != _TRANSACTION_CODE_PURCHASE:
                continue
            shares_str = txn.findtext("transactionAmounts/transactionShares/value")
            price_str = txn.findtext("transactionAmounts/transactionPricePerShare/value")
            if shares_str is None or price_str is None:
                continue
            try:
                value = float(shares_str) * float(price_str)
            except ValueError:
                continue
            if value >= min_transaction_value_usd:
                return True
    return False


__all__ = [
    "fetch_insider_purchase_flag",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_MIN_TRANSACTION_VALUE_USD",
    "DEFAULT_EXCLUDE_10B5_1",
]
