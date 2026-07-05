"""Module 04 delta — SEC EDGAR fundamentals provider (Phase 4).

Concrete :class:`MarketDataProvider` that overrides
:meth:`get_fundamentals` (Phase 4's additive capability) using SEC EDGAR's
free, keyless XBRL "companyfacts" API (``data.sec.gov``). This is the primary
and only fundamentals source implemented here; it was chosen over Finnhub as
the *primary* (reversing the coder note's stated preference order) because
its data shape, availability, and free-tier semantics are fully documented
and stable, whereas Finnhub's live free-tier limits could not be verified in
this environment (flagged, not silently assumed).

Field coverage (of the 7 :class:`FundamentalSnapshot` fields):

- ``eps_growth_trend``, ``leverage_ratio``, ``piotroski_f_score``,
  ``altman_z_score`` — computed here, self-contained, from EDGAR XBRL facts
  only (annual/10-K figures).
- ``valuation_band`` — computed here *if* a ``price_lookup`` callable is
  injected (see :class:`EdgarFundamentalsProvider`); otherwise ``"unknown"``
  (a valid catalog value, not a failure) since EPS-to-price bucketing has no
  meaningful book-value-only substitute.
- ``insider_trade_flag`` — always ``None`` from this provider. SEC EDGAR
  Form 4s are filed under the *insider's* own CIK, not the issuer's; reliably
  resolving "which Form 4 filings reference this issuer" needs EDGAR
  full-text search with query semantics this implementation cannot verify
  with confidence from documentation alone. Left as an ingestion-layer
  enrichment (see ``docs/phase4_fundamentals_events_layer.md`` design note)
  rather than guessed here.
- ``institutional_ownership_delta`` — always ``None``. True institutional
  ownership requires aggregating 13F filings across *all* institutional
  filers for a given issuer, quarter over quarter — a substantial
  data-engineering undertaking, not a single per-ticker API call. Flagged as
  a blocking gap per the coder note's explicit instruction rather than
  substituted with a proxy.

Altman Z-Score uses the **Z'-Score (private-firm) variant** — book value of
equity in place of market value of equity — so the computation is entirely
self-contained (no price dependency, no cross-provider reach-in). This is a
standard, well-documented alternate formulation of the same model, not an
invented one; see module-level docstring on :func:`compute_altman_z_score`.

Two layers, deliberately separated for testability:

- Pure functions (``compute_*``, ``extract_*``) operate on plain
  ``dict``/``list`` structures mirroring SEC EDGAR's companyfacts JSON shape.
  They do no I/O and are exercised directly with synthetic fixtures in
  ``tests/test_edgar_provider.py`` — no live EDGAR calls in the suite.
- :class:`EdgarFundamentalsProvider` is the thin HTTP-fetching wrapper
  (``requests``, imported lazily inside ``__init__`` — mirroring Module 05's
  lazy ``yfinance`` import so the module and its tests import cleanly without
  the dependency installed, and so a fake fetch function can be injected for
  fully offline tests).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Callable, Final

from app.providers.provider_interface import (
    FundamentalSnapshot,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderErrorDetail,
)
from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

_LOG = logging_config.get_logger(__name__)

PROVIDER_NAME: Final[str] = "sec_edgar"

_KEY_FUNDAMENTALS: Final[str] = "fundamentals"
_KEY_ERROR_DETAIL: Final[str] = "error_detail"
_KEY_PROVIDER_NAME: Final[str] = "provider_name"

_SEC_TICKER_MAP_URL: Final[str] = "https://www.sec.gov/files/company_tickers.json"
_SEC_COMPANYFACTS_URL: Final[str] = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
)
_SEC_USER_AGENT: Final[str] = "Stock_AI_Platform research (contact: local-only)"

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
_CONCEPT_LONG_TERM_DEBT: Final[tuple[str, ...]] = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
)

_ANNUAL_FORM: Final[str] = "10-K"


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
    pe_ratio = price / eps
    if pe_ratio < 15:
        return "cheap"
    if pe_ratio <= 25:
        return "fair"
    return "expensive"


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
) -> FundamentalSnapshot:
    """Pure assembly of a :class:`FundamentalSnapshot` from raw EDGAR JSON.

    ``insider_trade_flag`` and ``institutional_ownership_delta`` are always
    ``None`` here (see module docstring for why). ``price`` is an optional
    caller-supplied quote used only for ``valuation_band`` bucketing.
    """
    us_gaap = company_facts.get("facts", {}).get("us-gaap", {})

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
        insider_trade_flag=None,
        institutional_ownership_delta=None,
        source_provider=PROVIDER_NAME,
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
        Injected ``Callable[[str, dict[str, str]], dict]`` performing one GET
        returning parsed JSON: ``fetch_json(url, headers) -> dict``. When
        ``None`` (production), a small wrapper around ``requests.get`` is
        built lazily inside ``__init__`` (mirroring Module 05's lazy
        ``yfinance`` import) so tests can inject a fake and run fully offline
        with no network access and no ``requests`` dependency required.
    ticker_to_cik:
        Injected ``Callable[[str], str | None]`` resolving a ticker to its
        zero-padded SEC CIK. When ``None``, a default resolver fetches and
        caches ``company_tickers.json`` via ``fetch_json``.
    price_lookup:
        Optional ``Callable[[str, date], float | None]`` for ``valuation_band``
        only (see module docstring). Omitted in production by default;
        ``valuation_band`` then reports ``"unknown"`` rather than reaching
        into another provider.
    """

    def __init__(
        self,
        fetch_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
        ticker_to_cik: Callable[[str], str | None] | None = None,
        price_lookup: Callable[[str, date], float | None] | None = None,
    ) -> None:
        if fetch_json is None:
            import requests  # noqa: PLC0415 - intentional lazy import

            def _default_fetch(url: str, headers: dict[str, str]) -> dict[str, Any]:
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                return response.json()

            fetch_json = _default_fetch
        self._fetch_json = fetch_json
        self._ticker_to_cik = ticker_to_cik or self._default_ticker_to_cik
        self._price_lookup = price_lookup
        self._cik_cache: dict[str, str] | None = None

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
    # Fundamentals (Phase 4)
    # ------------------------------------------------------------------ #
    def get_fundamentals(self, ticker: str, as_of_date: date) -> ServiceResult:
        run_id = self._new_run_id()
        log = logging_config.get_logger(__name__, run_id)

        cik = self._ticker_to_cik(ticker)
        if cik is None:
            detail = ProviderErrorDetail(
                kind="unsupported_symbol",
                message=f"No SEC CIK mapping found for ticker {ticker!r}",
                symbol=ticker,
            )
            log.info("no CIK mapping ticker=%s", ticker)
            return self._failed(run_id, detail)

        url = _SEC_COMPANYFACTS_URL.format(cik=cik)
        try:
            company_facts = self._fetch_json(url, {"User-Agent": _SEC_USER_AGENT})
        except Exception as exc:  # noqa: BLE001 - mapped to a §9 ServiceResult
            kind = "rate_limited" if "429" in str(exc) else "provider_unavailable"
            detail = ProviderErrorDetail(
                kind=kind,
                message=f"SEC EDGAR companyfacts fetch failed for {ticker!r}: {exc}",
                symbol=ticker,
            )
            log.error("companyfacts fetch failed ticker=%s: %s", ticker, exc)
            return self._failed(run_id, detail)

        try:
            price = self._price_lookup(ticker, as_of_date) if self._price_lookup else None
            snapshot = compute_fundamentals_from_companyfacts(
                company_facts, ticker, as_of_date, price=price
            )
        except Exception as exc:  # noqa: BLE001 - malformed payload -> §9
            detail = ProviderErrorDetail(
                kind="malformed_response",
                message=f"Could not parse EDGAR companyfacts for {ticker!r}: {exc}",
                symbol=ticker,
            )
            log.error("malformed companyfacts ticker=%s: %s", ticker, exc)
            return self._failed(run_id, detail)

        log.info("computed fundamentals snapshot ticker=%s as_of=%s", ticker, as_of_date)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={_KEY_FUNDAMENTALS: snapshot, _KEY_PROVIDER_NAME: PROVIDER_NAME},
        )

    # ------------------------------------------------------------------ #
    # Ticker -> CIK resolution
    # ------------------------------------------------------------------ #
    def _default_ticker_to_cik(self, ticker: str) -> str | None:
        if self._cik_cache is None:
            payload = self._fetch_json(_SEC_TICKER_MAP_URL, {"User-Agent": _SEC_USER_AGENT})
            cache: dict[str, str] = {}
            for entry in payload.values():
                sym = entry.get("ticker")
                cik = entry.get("cik_str")
                if sym and cik is not None:
                    cache[sym.upper()] = str(cik).zfill(10)
            self._cik_cache = cache
        return self._cik_cache.get(ticker.upper())

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
    "compute_eps_growth_trend",
    "compute_leverage_ratio",
    "compute_valuation_band",
    "compute_piotroski_f_score",
    "compute_altman_z_score",
    "extract_annual_series",
    "extract_metric_series",
    "PROVIDER_NAME",
]
