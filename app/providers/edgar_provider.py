"""Module 04 delta — SEC EDGAR fundamentals provider (Phase 4, hardened).

Concrete :class:`MarketDataProvider` that overrides
:meth:`get_fundamentals` (Phase 4's additive capability) using SEC EDGAR's
free, keyless XBRL "companyfacts" API (``data.sec.gov``). This is the primary
fundamentals source; a ``yfinance``-backed fallback (see
:func:`compute_fundamentals_from_yfinance_info`) keeps the fundamentals step
degrading gracefully instead of failing outright when SEC EDGAR is
unreachable, rate-limited, or blocked.

Field coverage (of the 8 :class:`FundamentalSnapshot` fields):

- ``eps_growth_trend``, ``leverage_ratio``, ``piotroski_f_score``,
  ``altman_z_score`` — computed here, self-contained, from EDGAR XBRL facts
  only (annual/10-K figures).
- ``shares_outstanding`` (P2.4) — ``dei:EntityCommonStockSharesOutstanding``
  from the ``dei`` namespace: the instantaneous cover-page common-share count,
  taken from the freshest filing (10-K *or* 10-Q) knowable as of the requested
  date. Deliberately not the weighted-average diluted count, which is a period
  average over dilutive instruments and does not pair dimensionally with a
  price. Consumed by M11 to derive ``market_cap = shares_outstanding *
  close_raw`` (never ``close_adj``, which is retro-restated and would embed
  corporate actions occurring after the date).
- ``valuation_band`` — computed here *if* a ``price_lookup`` callable is
  injected (see :class:`EdgarFundamentalsProvider`); otherwise ``"unknown"``
  (a valid catalog value, not a failure) since EPS-to-price bucketing has no
  meaningful book-value-only substitute.
- ``insider_trade_flag`` (P2.7) — computed here *if* an ``insider_lookup``
  callable is injected (see :class:`EdgarFundamentalsProvider`); otherwise
  ``None``, same optional-injection shape as ``valuation_band``'s
  ``price_lookup``. The original concern here -- that SEC EDGAR Form 4s are
  filed under the *insider's* own CIK, not the issuer's, making
  issuer-keyed lookup unverifiable without full-text search -- turned out
  not to hold: ``data.sec.gov/submissions/CIK##########.json`` (the same
  JSON-API generation this module already uses for ``companyfacts``)
  reliably lists Form 4 filings for an issuer's own CIK, confirmed against
  real filings (see ``reports/sec_edgar_issuer_ownership_lookup_investigation_2026-07-18.md``).
  The default production ``insider_lookup`` (built by
  ``pipeline_orchestrator.py``, not this module) lives in
  :mod:`app.providers.edgar_insider_provider`.
- ``institutional_ownership_delta`` — always ``None`` from either source.
  True institutional ownership requires aggregating 13F filings across
  *all* institutional filers for a given issuer, quarter over quarter — a
  substantial data-engineering undertaking, not a single per-ticker API
  call. Flagged as a blocking gap rather than substituted with a proxy.

The ``yfinance`` fallback is **point-in-time restricted** (P2.4): ``Ticker.info``
is a current-only snapshot with no historical addressing, so the fallback
declines any ``as_of_date`` more than ``_FALLBACK_MAX_STALENESS_DAYS`` in the
past (and any future date) rather than stamping today's figures onto a
historical date. Before this restriction a multi-year backfill would have
written present-day fundamentals into every historical ``as_of_date`` for each
ticker whose SEC fetch failed. See :func:`fallback_can_serve`.

Altman Z-Score uses the **Z'-Score (private-firm) variant** — book value of
equity in place of market value of equity — so the computation is entirely
self-contained (no price dependency, no cross-provider reach-in). This is a
standard, well-documented alternate formulation of the same model, not an
invented one; see module-level docstring on :func:`compute_altman_z_score`.

SEC fair-access compliance (this module's hardening pass)
-----------------------------------------------------------
SEC EDGAR rejects (403 Forbidden) any request that doesn't send a
compliant ``User-Agent: "<App Name> <contact-email>"`` header. This module:

- Resolves the header from the ``SEC_USER_AGENT`` environment variable
  (:func:`resolve_sec_user_agent`) and fails fast — *before* attempting any
  network call — if it isn't configured, rather than hitting 403 on every
  ticker.
- Sets the header once on a shared ``requests.Session`` (see
  :class:`_SecHttpClient`), never per-call, so it can't be accidentally
  omitted.
- Throttles to a safe rate under SEC's 10 req/s fair-access limit and
  retries only transient failures (429 / 5xx) with exponential backoff.
  A 403 is never retried — the identical request with the identical header
  will never succeed on retry; it's surfaced as a clear, actionable error.
- Caches ``company_tickers.json`` (a large, effectively-static file) on
  disk with a 24h TTL so it is fetched at most once a day, not once per
  ticker per run.
- Falls back to ``yfinance`` (see :func:`compute_fundamentals_from_yfinance_info`)
  when the SEC path fails for any reason, with the resulting snapshot's
  ``source_provider`` explicitly labeled ``"yfinance_fallback"`` (never
  silently blended with ``"sec_edgar"`` results). If the fallback also
  fails, the ticker is reported as a normal ``failed`` :class:`ServiceResult`
  (per-ticker, non-fatal to the pipeline — the ingestion step already
  treats per-ticker failures as warnings, not hard stops).

Two layers, deliberately separated for testability:

- Pure functions (``compute_*``, ``extract_*``) operate on plain
  ``dict``/``list`` structures mirroring SEC EDGAR's companyfacts JSON shape
  (or, for the fallback, ``yfinance``'s ``Ticker.info`` dict shape). They do
  no I/O and are exercised directly with synthetic fixtures — no live
  EDGAR/Yahoo calls in the suite.
- :class:`EdgarFundamentalsProvider` (and its :class:`_SecHttpClient`
  transport helper) is the thin HTTP-fetching wrapper (``requests``,
  imported lazily inside ``__init__`` — mirroring Module 05's lazy
  ``yfinance`` import) so tests can inject fakes and run fully offline.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Callable, Final

from app.config import env, settings
from app.providers.provider_interface import (
    FundamentalSnapshot,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderErrorDetail,
)
from app.services.universe.ticker_normalization import normalize_ticker
from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

_LOG = logging_config.get_logger(__name__)

PROVIDER_NAME: Final[str] = "sec_edgar"
FALLBACK_PROVIDER_NAME: Final[str] = "yfinance_fallback"

_KEY_FUNDAMENTALS: Final[str] = "fundamentals"
_KEY_ERROR_DETAIL: Final[str] = "error_detail"
_KEY_PROVIDER_NAME: Final[str] = "provider_name"
_KEY_SOURCE_PROVIDER: Final[str] = "source_provider"

_SEC_TICKER_MAP_URL: Final[str] = "https://www.sec.gov/files/company_tickers.json"
_SEC_COMPANYFACTS_URL: Final[str] = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
)

# --------------------------------------------------------------------------- #
# SEC fair-access compliance constants.
# --------------------------------------------------------------------------- #
SEC_USER_AGENT_ENV_VAR: Final[str] = "SEC_USER_AGENT"
# Target well under SEC's published 10 req/s fair-access limit.
_TARGET_REQUESTS_PER_SEC: Final[float] = 6.0
_MIN_REQUEST_INTERVAL_SEC: Final[float] = 1.0 / _TARGET_REQUESTS_PER_SEC
_MAX_RETRIES: Final[int] = 3
_RETRY_BACKOFF_BASE_SEC: Final[float] = 1.0
# Retried with backoff: rate-limited / transient server errors. NEVER 403 --
# a 403 means the header is wrong or the caller is blocked; the identical
# retried request cannot succeed.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_FORBIDDEN_STATUS: Final[int] = 403

_CACHE_TTL_SECONDS_DEFAULT: Final[int] = 24 * 3600
_DEFAULT_CACHE_FILENAME: Final[str] = "sec_company_tickers.json"

# us-gaap XBRL concept names. Several fields have known filer-to-filer tag
# variance; aliases are tried in order and the first with usable annual data
# wins (CLARIFICATION: this mirrors real-world XBRL heterogeneity across
# registrants, not a hypothetical).
_CONCEPT_ASSETS: Final[tuple[str, ...]] = ("Assets",)
_CONCEPT_LIABILITIES: Final[tuple[str, ...]] = ("Liabilities",)
_CONCEPT_CURRENT_ASSETS: Final[tuple[str, ...]] = ("AssetsCurrent",)
_CONCEPT_CURRENT_LIABILITIES: Final[tuple[str, ...]] = ("LiabilitiesCurrent",)
_CONCEPT_EQUITY: Final[tuple[str, ...]] = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
_CONCEPT_RETAINED_EARNINGS: Final[tuple[str, ...]] = (
    "RetainedEarningsAccumulatedDeficit",
)
_CONCEPT_NET_INCOME: Final[tuple[str, ...]] = ("NetIncomeLoss",)
_CONCEPT_OPERATING_INCOME: Final[tuple[str, ...]] = ("OperatingIncomeLoss",)
_CONCEPT_REVENUES: Final[tuple[str, ...]] = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
)
_CONCEPT_CFO: Final[tuple[str, ...]] = (
    "NetCashProvidedByUsedInOperatingActivities",
)
_CONCEPT_GROSS_PROFIT: Final[tuple[str, ...]] = ("GrossProfit",)
_CONCEPT_EPS_DILUTED: Final[tuple[str, ...]] = ("EarningsPerShareDiluted",)
# P2.4: lives in the `dei` namespace, not `us-gaap`. Single concept, no alias
# family -- the cover-page tag is standardized, unlike the us-gaap share
# concepts whose filer inconsistency is why Piotroski signal 7 is omitted.
_CONCEPT_SHARES_OUTSTANDING: Final[str] = "EntityCommonStockSharesOutstanding"
_CONCEPT_LONG_TERM_DEBT: Final[tuple[str, ...]] = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
)

_ANNUAL_FORM: Final[str] = "10-K"


# --------------------------------------------------------------------------- #
# SEC fair-access: User-Agent resolution.
# --------------------------------------------------------------------------- #
def resolve_sec_user_agent(explicit: str | None = None) -> str:
    """Resolve the SEC-compliant ``User-Agent`` header value.

    Checks *explicit* first, then the ``SEC_USER_AGENT`` environment
    variable. Raises ``RuntimeError`` with an actionable message if neither
    is set. SEC EDGAR rejects (403) any request without a compliant header,
    and retrying with the same missing/invalid header never helps -- this
    must be fixed by configuration, not by retrying, which is why this
    check happens *before* any network call is attempted.
    """
    value = explicit if explicit else env.get_str(SEC_USER_AGENT_ENV_VAR)
    if not value or not value.strip():
        raise RuntimeError(
            f"{SEC_USER_AGENT_ENV_VAR} is not set. SEC EDGAR requires a "
            "compliant User-Agent header on every request "
            '(format: "<App Name> <contact-email>", e.g. '
            '"StockAnalyzer you@example.com"). Set the SEC_USER_AGENT '
            "environment variable before running fundamentals ingestion."
        )
    return value.strip()


# --------------------------------------------------------------------------- #
# Rate-limited, retrying HTTP client for SEC EDGAR endpoints.
# --------------------------------------------------------------------------- #
class _SecHttpClient:
    """Encapsulates everything SEC EDGAR requires of a well-behaved caller.

    - A compliant ``User-Agent`` set once on a shared session (never
      per-call, so it can't be accidentally omitted).
    - Throttling to stay under SEC's fair-access rate limit.
    - A retry policy that only retries transient errors (429 / 5xx) with
      exponential backoff -- never 403, which is surfaced immediately as a
      clear, non-retryable error.
    """

    def __init__(
        self,
        user_agent: str | None = None,
        requests_module: Any | None = None,
        min_request_interval_sec: float | None = None,
        max_retries: int | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._explicit_user_agent = user_agent
        self._requests_module = requests_module
        self._session: Any | None = None
        self._min_interval = (
            min_request_interval_sec
            if min_request_interval_sec is not None
            else _MIN_REQUEST_INTERVAL_SEC
        )
        self._max_retries = max_retries if max_retries is not None else _MAX_RETRIES
        self._sleep = sleep_fn or time.sleep
        self._time = time_fn or time.time
        self._last_request_ts: float | None = None

    def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        # Resolved (and may raise) before any session/network object is
        # built -- a missing SEC_USER_AGENT never produces a wasted request.
        user_agent = resolve_sec_user_agent(self._explicit_user_agent)
        requests_module = self._requests_module
        if requests_module is None:
            import requests as requests_module  # noqa: PLC0415 - intentional lazy import
        session = requests_module.Session()
        session.headers.update({"User-Agent": user_agent})
        self._session = session
        return session

    def _throttle(self) -> None:
        if self._last_request_ts is None:
            return
        elapsed = self._time() - self._last_request_ts
        remaining = self._min_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _get(self, url: str) -> Any:
        """GET *url*, honoring the retry/throttle policy, and return the raw response.

        Shared by :meth:`get_json` and :meth:`get_text` so both request
        shapes (companyfacts JSON, Form 4 XML) go through the *same*
        throttle/retry state on the *same* session -- two independently
        rate-limited clients would double SEC's effective request rate
        against the same fair-access budget.
        """
        session = self._ensure_session()
        attempt = 0
        while True:
            self._throttle()
            self._last_request_ts = self._time()
            response = session.get(url, timeout=30)
            status = getattr(response, "status_code", 200)
            if status == _FORBIDDEN_STATUS:
                raise RuntimeError(
                    f"SEC EDGAR returned 403 Forbidden for {url}. This means the "
                    "User-Agent header is missing/non-compliant, or this caller "
                    "has been rate-limited/blocked by SEC -- retrying will not "
                    "help. Verify SEC_USER_AGENT is set to a compliant value."
                )
            if status in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                attempt += 1
                self._sleep(_RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
                continue
            response.raise_for_status()
            return response

    def get_json(self, url: str) -> dict[str, Any]:
        """GET *url* and return parsed JSON, honoring the retry/throttle policy."""
        return self._get(url).json()

    def get_text(self, url: str) -> str:
        """GET *url* and return the raw response body as text.

        Used for Form 4 XML filings (P2.7 insider_trade_flag) -- unlike
        ``companyfacts``, filing documents are XML, not JSON, so
        ``response.json()`` would fail on them.
        """
        return self._get(url).text


def build_sec_http_client(
    sec_user_agent: str | None = None,
    requests_module: Any | None = None,
    min_request_interval_sec: float | None = None,
    max_retries: int | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> _SecHttpClient:
    """Build a :class:`_SecHttpClient`, exposed publicly so callers that need
    to *share* one rate-limited/throttled session across multiple request
    shapes (e.g. :class:`EdgarFundamentalsProvider`'s own ``companyfacts``
    fetches and an externally-injected ``insider_lookup``'s ``submissions``/
    filing-XML fetches) can build exactly one and pass its bound methods to
    both, rather than each independently building its own client and
    unknowingly doubling the effective request rate against SEC's shared
    fair-access budget.
    """
    return _SecHttpClient(
        user_agent=sec_user_agent,
        requests_module=requests_module,
        min_request_interval_sec=min_request_interval_sec,
        max_retries=max_retries,
        sleep_fn=sleep_fn,
    )


# --------------------------------------------------------------------------- #
# Pure XBRL extraction (no I/O; testable with synthetic companyfacts fixtures)
# --------------------------------------------------------------------------- #
def extract_annual_series(
    concept_facts: dict[str, Any] | None, as_of_date: date
) -> list[dict[str, Any]]:
    """Return annual (10-K) fact entries for one XBRL concept, newest first.

    Point-in-time discipline (Phase 0): a fact is eligible only when both its
    period ``end`` *and* its ``filed`` date are on or before ``as_of_date``.
    Filtering on ``end`` alone would leak — a FY ending before ``as_of_date``
    may not have been *filed* (and thus knowable) until well after it.
    """
    if not concept_facts:
        return []
    seen_ends: dict[str, dict[str, Any]] = {}
    for entries in concept_facts.get("units", {}).values():
        for entry in entries:
            if entry.get("form") != _ANNUAL_FORM:
                continue
            end_str = entry.get("end")
            filed_str = entry.get("filed")
            val = entry.get("val")
            if end_str is None or filed_str is None or val is None:
                continue
            try:
                end_dt = date.fromisoformat(end_str)
                filed_dt = date.fromisoformat(filed_str)
            except ValueError:
                continue
            if end_dt > as_of_date or filed_dt > as_of_date:
                continue
            existing = seen_ends.get(end_str)
            if existing is None or filed_dt >= date.fromisoformat(existing["filed"]):
                seen_ends[end_str] = entry
    ordered = sorted(seen_ends.values(), key=lambda e: e["end"], reverse=True)
    return ordered


def extract_shares_outstanding(
    dei_facts: dict[str, Any], as_of_date: date
) -> float | None:
    """Most recent cover-page common shares outstanding knowable as of *as_of_date*.

    Reads the ``dei`` namespace, not ``us-gaap``. ``dei:EntityCommonStockSharesOutstanding``
    is an *instantaneous* cover-page count as of a stated date -- the correct
    input for a market cap. It is deliberately not
    ``us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding``, which is a
    period *average* including dilutive instruments (and which this module
    already declines to trust; see ``compute_piotroski_f_score``).

    Point-in-time discipline (Phase 0), same rule as :func:`extract_annual_series`:
    an entry is eligible only when both its ``end`` (the date the shares were
    counted) and its ``filed`` date are on or before *as_of_date*. Filtering on
    ``end`` alone would leak -- a cover page dated before *as_of_date* is not
    knowable until the filing carrying it is actually filed.

    Unlike :func:`extract_annual_series` this accepts **any** form, not just
    ``10-K``: the same concept appears on 10-Q cover pages, and taking the
    freshest filed count rather than the last annual one is both more accurate
    and still strictly point-in-time.

    Returns the value with the latest ``end`` (ties broken by latest ``filed``),
    or ``None`` when the concept is absent, malformed, or nothing is yet
    knowable as of *as_of_date*.
    """
    concept_facts = (dei_facts or {}).get(_CONCEPT_SHARES_OUTSTANDING)
    if not concept_facts:
        return None
    units = concept_facts.get("units")
    if not isinstance(units, dict):
        return None

    best: tuple[date, date, float] | None = None
    for entries in units.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            end_str = entry.get("end")
            filed_str = entry.get("filed")
            val = entry.get("val")
            if end_str is None or filed_str is None or val is None:
                continue
            try:
                end_dt = date.fromisoformat(end_str)
                filed_dt = date.fromisoformat(filed_str)
                shares = float(val)
            except (ValueError, TypeError):
                continue
            if end_dt > as_of_date or filed_dt > as_of_date:
                continue
            if shares <= 0:
                continue
            candidate = (end_dt, filed_dt, shares)
            if best is None or candidate[:2] > best[:2]:
                best = candidate

    return best[2] if best is not None else None


def extract_metric_series(
    us_gaap: dict[str, Any], concept_names: tuple[str, ...], as_of_date: date
) -> list[dict[str, Any]]:
    """Try each concept alias in order; return the first with usable data."""
    for name in concept_names:
        entries = extract_annual_series(us_gaap.get(name), as_of_date)
        if entries:
            return entries
    return []


def _val(series: list[dict[str, Any]], index: int) -> float | None:
    if index >= len(series):
        return None
    val = series[index].get("val")
    return float(val) if val is not None else None


def _bucket_pe_ratio(pe_ratio: float) -> str:
    """Bucket a trailing P/E ratio into cheap / fair / expensive.

    Shared by the EDGAR path (:func:`compute_valuation_band`) and the
    yfinance fallback path (:func:`compute_fundamentals_from_yfinance_info`)
    so both sources use the identical thresholds.
    """
    if pe_ratio < 15:
        return "cheap"
    if pe_ratio <= 25:
        return "fair"
    return "expensive"


# --------------------------------------------------------------------------- #
# Field computations (pure; each returns None when required inputs are absent
# rather than fabricating a value)
# --------------------------------------------------------------------------- #
def compute_eps_growth_trend(eps_series: list[dict[str, Any]]) -> float | None:
    """Year-over-year diluted EPS growth: ``(current - prior) / abs(prior)``.

    ``None`` when fewer than two annual EPS figures are available, or the
    prior EPS is exactly zero (undefined growth rate).
    """
    current = _val(eps_series, 0)
    prior = _val(eps_series, 1)
    if current is None or prior is None or prior == 0:
        return None
    return (current - prior) / abs(prior)


def compute_leverage_ratio(
    long_term_debt_series: list[dict[str, Any]], assets_series: list[dict[str, Any]]
) -> float | None:
    """Long-term debt / total assets for the most recent annual period."""
    debt = _val(long_term_debt_series, 0)
    assets = _val(assets_series, 0)
    if debt is None or assets is None or assets == 0:
        return None
    return debt / assets


def compute_valuation_band(
    eps_series: list[dict[str, Any]], price: float | None
) -> str:
    """Bucket trailing P/E into ``cheap`` / ``fair`` / ``expensive``.

    Returns ``"unknown"`` (a valid :data:`VALUATION_BANDS` member, not a
    failure) when ``price`` is unavailable or EPS is non-positive — a P/E
    ratio is not meaningful for a loss-making company.
    """
    eps = _val(eps_series, 0)
    if price is None or eps is None or eps <= 0:
        return "unknown"
    return _bucket_pe_ratio(price / eps)


def compute_piotroski_f_score(
    *,
    assets: list[dict[str, Any]],
    current_assets: list[dict[str, Any]],
    current_liabilities: list[dict[str, Any]],
    net_income: list[dict[str, Any]],
    cfo: list[dict[str, Any]],
    long_term_debt: list[dict[str, Any]],
    gross_profit: list[dict[str, Any]],
    revenues: list[dict[str, Any]],
) -> int | None:
    """Classic 9-signal Piotroski F-Score, current annual period vs. prior.

    Each of the 9 signals needs both the current and prior annual value of
    its underlying concept(s). Returns ``None`` (not a partial score) if any
    required concept is missing either period — a partial F-Score is not a
    meaningful 0-9 value.
    """
    a0, a1 = _val(assets, 0), _val(assets, 1)
    ca0, ca1 = _val(current_assets, 0), _val(current_assets, 1)
    cl0, cl1 = _val(current_liabilities, 0), _val(current_liabilities, 1)
    ni0, ni1 = _val(net_income, 0), _val(net_income, 1)
    cfo0 = _val(cfo, 0)
    ltd0, ltd1 = _val(long_term_debt, 0), _val(long_term_debt, 1)
    gp0, gp1 = _val(gross_profit, 0), _val(gross_profit, 1)
    rev0, rev1 = _val(revenues, 0), _val(revenues, 1)

    required = (a0, a1, ca0, ca1, cl0, cl1, ni0, ni1, cfo0, ltd0, ltd1, gp0, gp1, rev0, rev1)
    if any(v is None for v in required) or a0 == 0 or a1 == 0 or rev0 == 0 or rev1 == 0:
        return None

    roa0 = ni0 / a0
    roa1 = ni1 / a1
    leverage0 = ltd0 / a0
    leverage1 = ltd1 / a1
    current_ratio0 = ca0 / cl0 if cl0 else None
    current_ratio1 = ca1 / cl1 if cl1 else None
    gross_margin0 = gp0 / rev0
    gross_margin1 = gp1 / rev1
    turnover0 = rev0 / a0
    turnover1 = rev1 / a1

    if current_ratio0 is None or current_ratio1 is None:
        return None

    score = 0
    score += 1 if roa0 > 0 else 0
    score += 1 if cfo0 > 0 else 0
    score += 1 if roa0 > roa1 else 0
    score += 1 if cfo0 > ni0 else 0
    score += 1 if leverage0 < leverage1 else 0
    score += 1 if current_ratio0 > current_ratio1 else 0
    score += 1 if gross_margin0 > gross_margin1 else 0
    score += 1 if turnover0 > turnover1 else 0
    # Signal 7 (no new share issuance) is intentionally omitted: reliably
    # sourcing weighted-average diluted shares from XBRL needs yet another
    # concept alias family (WeightedAverageNumberOfDilutedSharesOutstanding)
    # with its own filer inconsistency; rather than fabricate a 9th signal on
    # shaky data, the score is computed on the remaining 8 signals and scaled
    # to the standard 0-9 range so it stays comparable to the textbook score.
    return round(score * 9 / 8)


def compute_altman_z_score(
    *,
    assets: list[dict[str, Any]],
    liabilities: list[dict[str, Any]],
    current_assets: list[dict[str, Any]],
    current_liabilities: list[dict[str, Any]],
    equity: list[dict[str, Any]],
    retained_earnings: list[dict[str, Any]],
    operating_income: list[dict[str, Any]],
    revenues: list[dict[str, Any]],
) -> float | None:
    """Altman **Z'-Score** (book value of equity, not market value).

    ``Z' = 0.717*A + 0.847*B + 3.107*C + 0.420*D + 0.998*E`` where
    ``A`` = working capital / total assets, ``B`` = retained earnings /
    total assets, ``C`` = EBIT (operating income) / total assets,
    ``D`` = book value of equity / total liabilities, ``E`` = sales / total
    assets. This is the standard private-firm variant of the model
    (substituting book for market value of equity in the ``D`` term) — a
    deliberate, documented choice so this provider needs no price feed and
    stays self-contained; it is not an invented formula.
    """
    a0 = _val(assets, 0)
    l0 = _val(liabilities, 0)
    ca0 = _val(current_assets, 0)
    cl0 = _val(current_liabilities, 0)
    eq0 = _val(equity, 0)
    re0 = _val(retained_earnings, 0)
    ebit0 = _val(operating_income, 0)
    rev0 = _val(revenues, 0)

    required = (a0, l0, ca0, cl0, eq0, re0, ebit0, rev0)
    if any(v is None for v in required) or a0 == 0 or l0 == 0:
        return None

    working_capital = ca0 - cl0
    term_a = working_capital / a0
    term_b = re0 / a0
    term_c = ebit0 / a0
    term_d = eq0 / l0
    term_e = rev0 / a0
    return 0.717 * term_a + 0.847 * term_b + 3.107 * term_c + 0.420 * term_d + 0.998 * term_e


def compute_fundamentals_from_companyfacts(
    company_facts: dict[str, Any],
    ticker: str,
    as_of_date: date,
    *,
    price: float | None = None,
    insider_flag: bool | None = None,
) -> FundamentalSnapshot:
    """Pure assembly of a :class:`FundamentalSnapshot` from raw EDGAR JSON.

    ``institutional_ownership_delta`` is always ``None`` here (see module
    docstring for why). ``price`` is an optional caller-supplied quote used
    only for ``valuation_band`` bucketing. ``insider_flag`` is an optional
    caller-supplied, already-computed value (P2.7's SEC-EDGAR-native
    ``insider_lookup``) threaded straight into the snapshot -- this function
    does no I/O itself (same contract as ``price``), so the lookup must
    already have run by the time it's passed in.
    """
    facts = company_facts.get("facts", {})
    us_gaap = facts.get("us-gaap", {})
    dei_facts = facts.get("dei", {})

    assets = extract_metric_series(us_gaap, _CONCEPT_ASSETS, as_of_date)
    liabilities = extract_metric_series(us_gaap, _CONCEPT_LIABILITIES, as_of_date)
    current_assets = extract_metric_series(us_gaap, _CONCEPT_CURRENT_ASSETS, as_of_date)
    current_liabilities = extract_metric_series(
        us_gaap, _CONCEPT_CURRENT_LIABILITIES, as_of_date
    )
    equity = extract_metric_series(us_gaap, _CONCEPT_EQUITY, as_of_date)
    retained_earnings = extract_metric_series(
        us_gaap, _CONCEPT_RETAINED_EARNINGS, as_of_date
    )
    net_income = extract_metric_series(us_gaap, _CONCEPT_NET_INCOME, as_of_date)
    operating_income = extract_metric_series(
        us_gaap, _CONCEPT_OPERATING_INCOME, as_of_date
    )
    revenues = extract_metric_series(us_gaap, _CONCEPT_REVENUES, as_of_date)
    cfo = extract_metric_series(us_gaap, _CONCEPT_CFO, as_of_date)
    gross_profit = extract_metric_series(us_gaap, _CONCEPT_GROSS_PROFIT, as_of_date)
    eps_diluted = extract_metric_series(us_gaap, _CONCEPT_EPS_DILUTED, as_of_date)
    long_term_debt = extract_metric_series(us_gaap, _CONCEPT_LONG_TERM_DEBT, as_of_date)

    return FundamentalSnapshot(
        ticker=ticker,
        as_of_date=as_of_date,
        eps_growth_trend=compute_eps_growth_trend(eps_diluted),
        leverage_ratio=compute_leverage_ratio(long_term_debt, assets),
        valuation_band=compute_valuation_band(eps_diluted, price),
        shares_outstanding=extract_shares_outstanding(dei_facts, as_of_date),
        piotroski_f_score=compute_piotroski_f_score(
            assets=assets,
            current_assets=current_assets,
            current_liabilities=current_liabilities,
            net_income=net_income,
            cfo=cfo,
            long_term_debt=long_term_debt,
            gross_profit=gross_profit,
            revenues=revenues,
        ),
        altman_z_score=compute_altman_z_score(
            assets=assets,
            liabilities=liabilities,
            current_assets=current_assets,
            current_liabilities=current_liabilities,
            equity=equity,
            retained_earnings=retained_earnings,
            operating_income=operating_income,
            revenues=revenues,
        ),
        insider_trade_flag=insider_flag,
        institutional_ownership_delta=None,
        source_provider=PROVIDER_NAME,
    )


# --------------------------------------------------------------------------- #
# yfinance fallback (used when the SEC EDGAR path fails for any reason).
# --------------------------------------------------------------------------- #

# P2.4: how far back an `as_of_date` may sit and still be served by the
# fallback. `yfinance`'s Ticker.info is a *current* snapshot with no historical
# addressing, so it can only ever answer "what is true now". A few days of slack
# covers catch-up runs over a weekend/holiday without admitting a backfill.
_FALLBACK_MAX_STALENESS_DAYS: Final[int] = 7


def fallback_can_serve(as_of_date: date, today: date) -> bool:
    """Whether the yfinance fallback may answer for *as_of_date* at all.

    ``Ticker.info`` returns **current** ``trailingPE`` / ``earningsQuarterlyGrowth``
    / ``debtToEquity`` / ``sharesOutstanding`` regardless of the ``as_of_date``
    asked for. Stamping those onto a historical ``as_of_date`` -- which is
    exactly what a multi-year backfill does for every ticker whose SEC fetch
    fails -- writes *today's* fundamentals into the past. That is the Phase 0
    look-ahead class: a value that was not knowable on the date it is recorded
    against.

    The fallback therefore declines historical (and future) dates outright.
    Declining yields no ``ticker_fundamentals`` row, which every consumer
    already treats as "no coverage, no adjustment" -- strictly better than a
    contaminated row, because absence is honest and a wrong value is not.
    """
    if as_of_date > today:
        return False
    return (today - as_of_date).days <= _FALLBACK_MAX_STALENESS_DAYS


def compute_fundamentals_from_yfinance_info(
    info: dict[str, Any], ticker: str, as_of_date: date
) -> FundamentalSnapshot:
    """Pure assembly of a fallback :class:`FundamentalSnapshot` from
    ``yfinance``'s ``Ticker.info`` dict.

    Coverage is intentionally reduced versus the EDGAR path:
    ``piotroski_f_score`` / ``altman_z_score`` need full financial
    statements across two periods, which ``.info`` does not provide, so
    both stay ``None`` here rather than being approximated on partial data.
    ``leverage_ratio`` uses a **different basis** than the EDGAR path:
    yfinance's ``debtToEquity`` is debt relative to *equity* (as a
    percentage), not debt relative to *assets* — documented here rather
    than silently treated as equivalent to the EDGAR field of the same
    name. ``source_provider`` is always ``"yfinance_fallback"``, never
    blended with ``"sec_edgar"``.
    """
    eps_growth = info.get("earningsQuarterlyGrowth")
    if eps_growth is None:
        eps_growth = info.get("earningsGrowth")

    debt_to_equity_pct = info.get("debtToEquity")
    leverage_ratio = (
        float(debt_to_equity_pct) / 100.0 if debt_to_equity_pct is not None else None
    )

    pe_ratio = info.get("trailingPE")
    valuation_band = _bucket_pe_ratio(float(pe_ratio)) if pe_ratio else "unknown"

    raw_shares = info.get("sharesOutstanding")
    try:
        shares_outstanding = float(raw_shares) if raw_shares else None
    except (TypeError, ValueError):
        shares_outstanding = None
    if shares_outstanding is not None and shares_outstanding <= 0:
        shares_outstanding = None

    return FundamentalSnapshot(
        ticker=ticker,
        as_of_date=as_of_date,
        eps_growth_trend=float(eps_growth) if eps_growth is not None else None,
        leverage_ratio=leverage_ratio,
        valuation_band=valuation_band,
        shares_outstanding=shares_outstanding,
        piotroski_f_score=None,
        altman_z_score=None,
        insider_trade_flag=None,
        institutional_ownership_delta=None,
        source_provider=FALLBACK_PROVIDER_NAME,
    )


# --------------------------------------------------------------------------- #
# HTTP-fetching wrapper
# --------------------------------------------------------------------------- #
class EdgarFundamentalsProvider(MarketDataProvider):
    """SEC-EDGAR-backed :class:`MarketDataProvider` implementing only
    ``get_fundamentals``; the four abstract methods are implemented as thin
    ``unsupported_capability`` responses since this provider is fundamentals-
    only, not a price/earnings source (:class:`YahooProvider` remains the
    price/earnings provider; providers are independently invocable, per
    ``PROVIDER_INTERFACE_SPEC.md``).

    Parameters
    ----------
    fetch_json:
        Injected ``Callable[[str], dict]`` performing one GET returning
        parsed JSON: ``fetch_json(url) -> dict``. When ``None`` (production),
        a :class:`_SecHttpClient` is built lazily (rate-limited, retrying,
        compliant ``User-Agent``) so tests can inject a fake and run fully
        offline with no network access.
    ticker_to_cik:
        Injected ``Callable[[str], str | None]`` resolving a ticker to its
        zero-padded SEC CIK. When ``None``, a default resolver reads the
        on-disk ``company_tickers.json`` cache (TTL-bounded) or fetches and
        caches it via ``fetch_json``.
    price_lookup:
        Optional ``Callable[[str, date], float | None]`` for ``valuation_band``
        only (see module docstring). Omitted in production by default;
        ``valuation_band`` then reports ``"unknown"`` rather than reaching
        into another provider.
    insider_lookup:
        Optional ``Callable[[str, date, str], bool | None]`` for
        ``insider_trade_flag`` (P2.7). Takes ``(ticker, as_of_date, cik)`` --
        the ``cik`` is the same zero-padded value this provider already
        resolves for ``companyfacts``, passed through rather than
        re-resolved. Always ``None`` in production as of the 2026-07-20
        step-decoupling coder note: ``pipeline_orchestrator.py``'s
        ``fundamentals_refresh`` step no longer wires one in (the ~82min cost
        moved to its own later ``insider_flag_refresh`` step, which calls
        ``edgar_insider_provider.fetch_insider_purchase_flag`` directly rather
        than through this provider). The parameter and the pass-through to
        :func:`compute_fundamentals_from_companyfacts` are left in place --
        harmless, and still exercised directly by this module's own tests.
    sec_user_agent:
        Explicit override for the ``SEC_USER_AGENT`` environment variable
        (mainly for tests).
    cache_path:
        On-disk path for the ``company_tickers.json`` cache. Defaults to
        ``settings.CACHE_DIR / "sec_company_tickers.json"``.
    cache_ttl_seconds:
        Cache freshness window. Defaults to 24 hours.
    yfinance_fallback:
        Injected ``Callable[[str, date], FundamentalSnapshot | None]`` used
        when the SEC path fails for any reason. When ``None``, a default is
        built from ``yf_module`` (or a lazily-imported real ``yfinance``).
    yf_module:
        Injected fake ``yfinance``-like module (mirrors
        :class:`YahooProvider`'s ``yf_module`` hook) used to build the
        default fallback. Ignored if ``yfinance_fallback`` is supplied.
    today_fn:
        Injected ``Callable[[], date]`` (default :meth:`date.today`) used only
        to decide whether the default fallback may serve a given ``as_of_date``
        (see :func:`fallback_can_serve`). Ignored if ``yfinance_fallback`` is
        supplied.
    min_request_interval_sec, max_retries, requests_module, sleep_fn:
        Passed through to :class:`_SecHttpClient` (mainly for tests).
    """

    def __init__(
        self,
        fetch_json: Callable[[str], dict[str, Any]] | None = None,
        ticker_to_cik: Callable[[str], str | None] | None = None,
        price_lookup: Callable[[str, date], float | None] | None = None,
        insider_lookup: Callable[[str, date, str], bool | None] | None = None,
        sec_user_agent: str | None = None,
        cache_path: Path | None = None,
        cache_ttl_seconds: int | None = None,
        yfinance_fallback: Callable[[str, date], FundamentalSnapshot | None] | None = None,
        yf_module: Any | None = None,
        today_fn: Callable[[], date] | None = None,
        min_request_interval_sec: float | None = None,
        max_retries: int | None = None,
        requests_module: Any | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if fetch_json is None:
            http_client = build_sec_http_client(
                sec_user_agent=sec_user_agent,
                requests_module=requests_module,
                min_request_interval_sec=min_request_interval_sec,
                max_retries=max_retries,
                sleep_fn=sleep_fn,
            )
            fetch_json = http_client.get_json
        self._fetch_json = fetch_json
        self._ticker_to_cik = ticker_to_cik or self._default_ticker_to_cik
        self._price_lookup = price_lookup
        self._insider_lookup = insider_lookup
        self._cik_cache: dict[str, str] | None = None
        self._cache_path = (
            cache_path if cache_path is not None
            else (settings.CACHE_DIR / _DEFAULT_CACHE_FILENAME)
        )
        self._cache_ttl_seconds = (
            cache_ttl_seconds if cache_ttl_seconds is not None
            else _CACHE_TTL_SECONDS_DEFAULT
        )
        self._yfinance_fallback = yfinance_fallback or self._build_default_yfinance_fallback(
            yf_module, today_fn
        )

    @staticmethod
    def _build_default_yfinance_fallback(
        yf_module: Any | None,
        today_fn: Callable[[], date] | None = None,
    ) -> Callable[[str, date], FundamentalSnapshot | None]:
        _today = today_fn or date.today

        def _fallback(ticker: str, as_of_date: date) -> FundamentalSnapshot | None:
            if not fallback_can_serve(as_of_date, _today()):
                return None
            yf = yf_module
            if yf is None:
                import yfinance as yf  # noqa: PLC0415 - intentional lazy import
            info = yf.Ticker(ticker).info
            if not info:
                return None
            return compute_fundamentals_from_yfinance_info(info, ticker, as_of_date)

        return _fallback

    # ------------------------------------------------------------------ #
    # Capabilities
    # ------------------------------------------------------------------ #
    def get_capabilities(self) -> ServiceResult:
        caps = ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            supports_daily_prices=False,
            supports_ticker_listing=False,
            supports_earnings=False,
            supports_adjusted_prices=False,
            supports_fundamentals=True,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=self._new_run_id(),
            rows_processed=1,
            metadata={"capabilities": caps, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    def get_price_history(self, request: Any) -> ServiceResult:
        return self._unsupported("get_price_history", getattr(request, "ticker", None))

    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        return self._unsupported("list_symbols", None)

    def get_earnings(self, ticker: str) -> ServiceResult:
        return self._unsupported("get_earnings", ticker)

    # ------------------------------------------------------------------ #
    # Fundamentals (Phase 4), with yfinance fallback on any SEC-path failure.
    # ------------------------------------------------------------------ #
    def get_fundamentals(self, ticker: str, as_of_date: date) -> ServiceResult:
        run_id = self._new_run_id()
        log = logging_config.get_logger(__name__, run_id)

        # Normalize before any provider call (SEC CIK lookup, yfinance fallback):
        # slash-notation share classes (BRK/A, BF/A, ...) 404 on both SEC EDGAR
        # and yfinance, which expect hyphen notation (BRK-A, BF-A, ...).
        ticker = normalize_ticker(ticker)

        try:
            cik = self._ticker_to_cik(ticker)
            if cik is None:
                raise ValueError(f"No SEC CIK mapping found for ticker {ticker!r}")
            url = _SEC_COMPANYFACTS_URL.format(cik=cik)
            company_facts = self._fetch_json(url)
            price = self._price_lookup(ticker, as_of_date) if self._price_lookup else None
            insider_flag = self._safe_insider_lookup(ticker, as_of_date, cik, log)
            snapshot = compute_fundamentals_from_companyfacts(
                company_facts, ticker, as_of_date, price=price, insider_flag=insider_flag
            )
        except Exception as sec_exc:  # noqa: BLE001 - any SEC-path failure triggers fallback
            log.warning(
                "sec_edgar fundamentals failed ticker=%s: %s; attempting yfinance fallback",
                ticker, sec_exc,
            )
            return self._fallback_or_fail(run_id, log, ticker, as_of_date, sec_exc)

        log.info(
            "computed fundamentals snapshot ticker=%s as_of=%s source=%s",
            ticker, as_of_date, PROVIDER_NAME,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                _KEY_FUNDAMENTALS: snapshot,
                _KEY_PROVIDER_NAME: PROVIDER_NAME,
                _KEY_SOURCE_PROVIDER: snapshot.source_provider,
            },
        )

    def _safe_insider_lookup(
        self, ticker: str, as_of_date: date, cik: str, log: Any
    ) -> bool | None:
        """Call the injected ``insider_lookup``, isolated from the SEC-path
        try/except in :meth:`get_fundamentals`.

        Deliberately NOT called inline inside that try block (unlike
        ``price_lookup``): a failure here must never discard the other 5
        already-computed EDGAR fields or trigger a yfinance fallback, which
        has zero ``piotroski_f_score``/``altman_z_score`` coverage at all --
        that would be a strictly worse outcome caused by a failure in a
        field that both coder notes are explicit is purely informational and
        must never fail anything. Returns ``None`` (not a re-raised
        exception) on any lookup failure, logged as a warning.
        """
        if self._insider_lookup is None:
            return None
        try:
            return self._insider_lookup(ticker, as_of_date, cik)
        except Exception as exc:  # noqa: BLE001
            log.warning("insider_trade_flag lookup failed ticker=%s: %s", ticker, exc)
            return None

    def _fallback_or_fail(
        self, run_id: str, log: Any, ticker: str, as_of_date: date, sec_exc: Exception
    ) -> ServiceResult:
        """Attempt the yfinance fallback; return a clean failure if it also fails.

        Never raises: any yfinance-path exception is caught and folded into
        the "both sources failed" branch, matching the same
        never-crash-the-pipeline contract as the SEC path itself.
        """
        try:
            snapshot = self._yfinance_fallback(ticker, as_of_date)
            fallback_error: str | None = None if snapshot is not None else "yfinance returned no data"
        except Exception as fb_exc:  # noqa: BLE001
            snapshot = None
            fallback_error = str(fb_exc)

        if snapshot is not None:
            log.warning(
                "ticker=%s: sec_edgar failed (%s); used %s",
                ticker, sec_exc, FALLBACK_PROVIDER_NAME,
            )
            return ServiceResult(
                status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id,
                rows_processed=1,
                warnings=[f"sec_edgar failed ({sec_exc}); used {FALLBACK_PROVIDER_NAME}"],
                metadata={
                    _KEY_FUNDAMENTALS: snapshot,
                    _KEY_PROVIDER_NAME: PROVIDER_NAME,
                    _KEY_SOURCE_PROVIDER: snapshot.source_provider,
                },
            )

        log.error(
            "ticker=%s: both sec_edgar and %s failed: sec=%s yfinance=%s",
            ticker, FALLBACK_PROVIDER_NAME, sec_exc, fallback_error,
        )
        detail = ProviderErrorDetail(
            kind="provider_unavailable",
            message=(
                f"sec_edgar failed for {ticker!r} ({sec_exc}); "
                f"{FALLBACK_PROVIDER_NAME} also failed ({fallback_error})"
            ),
            symbol=ticker,
        )
        return self._failed(run_id, detail)

    # ------------------------------------------------------------------ #
    # Ticker -> CIK resolution, with an on-disk TTL cache for
    # company_tickers.json (large, effectively-static; must not be
    # re-fetched per ticker per run).
    # ------------------------------------------------------------------ #
    def resolve_cik(self, ticker: str) -> str | None:
        """Public wrapper around the injected/default ticker->CIK resolver.

        Added for ``insider_flag_refresh`` (2026-07-20 step-decoupling coder
        note): that step needs the same CIK resolution ``get_fundamentals``
        already does internally, but isn't fetching ``companyfacts`` itself,
        so it has no other reason to reach into this provider. Normalizes the
        ticker first, same as ``get_fundamentals``.
        """
        return self._ticker_to_cik(normalize_ticker(ticker))

    def _default_ticker_to_cik(self, ticker: str) -> str | None:
        if self._cik_cache is None:
            self._cik_cache = self._load_ticker_map()
        return self._cik_cache.get(ticker.upper())

    def _load_ticker_map(self) -> dict[str, str]:
        cached = self._read_disk_cache()
        if cached is not None:
            return cached
        payload = self._fetch_json(_SEC_TICKER_MAP_URL)
        cache_map = self._parse_ticker_map(payload)
        self._write_disk_cache(payload)
        return cache_map

    def _read_disk_cache(self) -> dict[str, str] | None:
        if self._cache_path is None:
            return None
        try:
            mtime = self._cache_path.stat().st_mtime
        except OSError:
            return None
        if (time.time() - mtime) >= self._cache_ttl_seconds:
            return None  # stale -> caller refetches
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # unreadable/corrupt -> caller refetches
        return self._parse_ticker_map(payload)

    def _write_disk_cache(self, payload: dict[str, Any]) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass  # caching is best-effort, not required for correctness

    @staticmethod
    def _parse_ticker_map(payload: dict[str, Any]) -> dict[str, str]:
        cache: dict[str, str] = {}
        for entry in payload.values():
            sym = entry.get("ticker")
            cik = entry.get("cik_str")
            if sym and cik is not None:
                cache[sym.upper()] = str(cik).zfill(10)
        return cache

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_run_id() -> str:
        return str(uuid.uuid4())

    def _unsupported(self, method_name: str, symbol: str | None) -> ServiceResult:
        detail = ProviderErrorDetail(
            kind="unsupported_capability",
            message=f"{method_name} is not supported by {PROVIDER_NAME} (fundamentals-only provider)",
            symbol=symbol,
        )
        return self._failed(self._new_run_id(), detail)

    @staticmethod
    def _failed(run_id: str, detail: ProviderErrorDetail) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[detail.message],
            metadata={_KEY_ERROR_DETAIL: detail, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )


__all__ = [
    "EdgarFundamentalsProvider",
    "compute_fundamentals_from_companyfacts",
    "compute_fundamentals_from_yfinance_info",
    "compute_eps_growth_trend",
    "compute_leverage_ratio",
    "compute_valuation_band",
    "compute_piotroski_f_score",
    "compute_altman_z_score",
    "extract_annual_series",
    "extract_metric_series",
    "resolve_sec_user_agent",
    "build_sec_http_client",
    "PROVIDER_NAME",
    "FALLBACK_PROVIDER_NAME",
    "SEC_USER_AGENT_ENV_VAR",
]
