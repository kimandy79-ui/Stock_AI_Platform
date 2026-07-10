"""Shared fundamentals quality score — single source of truth for the 0-100 formula.

Two independent call sites derive a fundamentals signal from the same five
``ticker_fundamentals`` fields, and they must not drift apart:

* **M14** (``m14_setup_validators._compute_fundamentals_adjustment``) turns the
  quality score into a weighted, two-sided adjustment folded into
  ``penalized_score`` -> ``setup_score``. Opt-in per setup_config
  (``fundamentals.enabled``); every seeded config has it off.
* **M15/Step 5** (``step5_proposal_engine._proposal_score_raw``) adds its own
  weighted, two-sided term keyed by the same quality score.

Because Step 5 also weights ``setup_score`` (``_W_SETUP``), letting both paths
run for the same proposal would count one signal twice -- the same defect class
as ``m15_double_credit_bug_finding.md``. The double-credit guard lives in
Step 5 (per-row skip) and ``ConfigService.validate_setup_config`` (creation-time
rejection); this module only guarantees the two paths compute the *same* number
from the same inputs.

Everything here is pure except :func:`read_fundamentals_map`, which performs the
point-in-time read.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final, Protocol


class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# Altman Z'-Score interpretive zones (private-firm/book-value variant; see
# app.providers.edgar_provider.compute_altman_z_score docstring for why this
# variant is used). Standard textbook zones: >2.9 safe, <1.23 distress.
ALTMAN_SAFE_ZONE: Final[float] = 2.9
ALTMAN_DISTRESS_ZONE: Final[float] = 1.23

# "unknown" intentionally absent -> excluded from the average, not scored.
VALUATION_BAND_QUALITY: Final[dict[str, float]] = {
    "cheap": 100.0,
    "fair": 60.0,
    "expensive": 20.0,
}

# Point-in-time correct: only rows with as_of_date <= signal_date are eligible,
# and the most recent such row per ticker wins (mirrors daily_prices' asof-safe
# "date <= end_date" pattern from the Phase 0 point-in-time audit).
SQL_READ_FUNDAMENTALS: Final[str] = """
SELECT
    ticker,
    eps_growth_trend,
    leverage_ratio,
    valuation_band,
    piotroski_f_score,
    altman_z_score
FROM ticker_fundamentals
WHERE as_of_date <= ?
QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY as_of_date DESC) = 1
"""

FUNDAMENTALS_COLS: Final[tuple[str, ...]] = (
    "ticker",
    "eps_growth_trend",
    "leverage_ratio",
    "valuation_band",
    "piotroski_f_score",
    "altman_z_score",
)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def compute_fundamentals_quality(
    feat: dict[str, Any],
    valuation_band_quality: dict[str, float] = VALUATION_BAND_QUALITY,
) -> float | None:
    """Mean 0-100 quality across whichever of the 5 fundamentals fields are present.

    Returns ``None`` when *no* field is present -- "no coverage yet", which every
    caller must treat as "no adjustment", never as a penalty. A field that is
    absent or ``None`` is excluded from the mean rather than scored as zero, so a
    ticker with partial EDGAR coverage is judged only on what is actually known.
    """
    quality_scores: list[float] = []

    piotroski = feat.get("piotroski_f_score")
    if piotroski is not None:
        quality_scores.append(_clamp(100.0 * float(piotroski) / 9.0))

    altman = feat.get("altman_z_score")
    if altman is not None:
        altman = float(altman)
        if altman >= ALTMAN_SAFE_ZONE:
            quality_scores.append(100.0)
        elif altman <= ALTMAN_DISTRESS_ZONE:
            quality_scores.append(0.0)
        else:
            span = ALTMAN_SAFE_ZONE - ALTMAN_DISTRESS_ZONE
            quality_scores.append(100.0 * (altman - ALTMAN_DISTRESS_ZONE) / span)

    band = feat.get("valuation_band")
    if band in valuation_band_quality:
        quality_scores.append(valuation_band_quality[band])

    eps_growth = feat.get("eps_growth_trend")
    if eps_growth is not None:
        quality_scores.append(_clamp(50.0 + float(eps_growth) * 100.0))

    leverage = feat.get("leverage_ratio")
    if leverage is not None:
        quality_scores.append(_clamp(100.0 - float(leverage) * 100.0))

    if not quality_scores:
        return None
    return sum(quality_scores) / len(quality_scores)


def read_fundamentals_map(
    db_mgr: _DbManagerLike, db_role: str, signal_date: date
) -> dict[str, dict[str, Any]]:
    """Return ticker -> most recent ``ticker_fundamentals`` row as of *signal_date*.

    Absence (no row for a ticker, or an empty table) is not an error: callers
    treat missing fundamentals as "no adjustment", exactly like a routed
    candidate with no earnings-calendar row.
    """
    conn = db_mgr.connect(db_role)
    try:
        rows = conn.execute(SQL_READ_FUNDAMENTALS, [signal_date.isoformat()]).fetchall()
    finally:
        conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = dict(zip(FUNDAMENTALS_COLS, row))
        result[d["ticker"]] = d
    return result


# P2.4: shares history for the as-of join M11 needs. A single read covering the
# whole batch beats one query per (ticker, cutoff): M11 computes features for a
# date *range*, and each cutoff must see only the filings knowable by then.
SQL_READ_SHARES_HISTORY: Final[str] = """
SELECT ticker, as_of_date, shares_outstanding
FROM ticker_fundamentals
WHERE as_of_date <= ? AND shares_outstanding IS NOT NULL
ORDER BY ticker, as_of_date
"""


def read_shares_history(
    db_mgr: _DbManagerLike, db_role: str, max_date: date
) -> dict[str, list[tuple[date, float]]]:
    """ticker -> ascending [(as_of_date, shares_outstanding)] up to *max_date*.

    Ascending order is the contract :func:`shares_as_of` relies on. Returns an
    empty map (never raises) when ``ticker_fundamentals`` is absent — it is not
    part of the simulation schema, and M11 runs there too.
    """
    try:
        conn = db_mgr.connect(db_role)
    except Exception:  # noqa: BLE001
        return {}
    try:
        rows = conn.execute(SQL_READ_SHARES_HISTORY, [max_date.isoformat()]).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()

    history: dict[str, list[tuple[date, float]]] = {}
    for ticker, as_of, shares in rows:
        if shares is None:
            continue
        history.setdefault(ticker, []).append((as_of, float(shares)))
    return history


def shares_as_of(
    history: dict[str, list[tuple[date, float]]], ticker: str, as_of: date
) -> float | None:
    """Most recent shares_outstanding known for *ticker* on *as_of*, else None.

    Point-in-time: a filing dated after *as_of* is invisible, even though it sits
    in ``history`` for a later cutoff in the same batch.
    """
    entries = history.get(ticker)
    if not entries:
        return None
    result: float | None = None
    for entry_date, shares in entries:  # ascending
        if entry_date > as_of:
            break
        result = shares
    return result


def compute_market_cap(
    shares_outstanding: float | None, close_raw: float | None
) -> float | None:
    """``shares_outstanding * close_raw``, or ``None`` if either is unusable.

    **``close_raw``, never ``close_adj``.** The adjusted series is retro-restated
    as later splits/dividends occur (hence ``daily_prices.adjustment_factor`` and
    M10's mutation detection), so a market cap built on it would (a) multiply a
    split-adjusted price by an unadjusted share count and (b) embed corporate
    actions that had not happened as of the date -- a look-ahead leak, and a value
    that silently changes on every backfill.
    """
    if shares_outstanding is None or close_raw is None:
        return None
    if shares_outstanding <= 0 or close_raw <= 0:
        return None
    return shares_outstanding * close_raw


def build_fundamentals_scores(
    fundamentals_by_ticker: dict[str, dict[str, Any]],
    valuation_band_quality: dict[str, float] = VALUATION_BAND_QUALITY,
) -> dict[str, float]:
    """Map ticker -> 0-100 quality score, omitting tickers with no coverage.

    Omission (rather than a ``None`` value) is deliberate: Step 5 reads this with
    ``.get(ticker)``, so an absent ticker and a ``None`` score are the same thing
    to it, and omitting keeps the dict honest about what was actually scored.
    """
    scores: dict[str, float] = {}
    for ticker, row in fundamentals_by_ticker.items():
        quality = compute_fundamentals_quality(row, valuation_band_quality)
        if quality is not None:
            scores[ticker] = quality
    return scores
