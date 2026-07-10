"""Module 11 — Feature Engine (features_v04).

Computes ``daily_features`` rows (schema version ``features_v04``) from already-
ingested, validated, and mutation-checked ``daily_prices`` data.  Runs after
Module 10 (Mutation Detector) and before Module 12 (Market Regime Engine).

Contract source of truth: ``M11_FEATURE_ENGINE_SPEC.md`` (derived from the
frozen split Project Files — ``01c_FORMULAS_AND_CONFIGS.md`` for formulas,
``01b_SCHEMA_AND_DATA.md`` for the ``daily_features`` schema,
``01d_MODULES_AND_PIPELINE.md`` for the pipeline position, and
``02b_ARCHITECTURE_DECISIONS.md`` for Polars-first / feature-cutoff /
raw-vs-adjusted / schema-version decisions).

Phase 2 migration (setup-mode): all ``features_v02`` structural columns are
now computed.  New columns added over the ``features_v01`` baseline:

    ema20_slope, ema50_slope
    atr_compression_score
    pullback_depth_pct
    swing_high, swing_low
    support_level, resistance_level, next_resistance_level
    base_high, base_low, range_width_pct, range_duration, range_tightness_score
    volume_dry_up_score, volume_expansion_score
    relative_strength_vs_spy

P1.1 (2026-07-08): ``features_v03`` adds one column over the ``features_v02``
baseline — ``rs_percentile_126d``, a same-day cross-sectional percentile
rank (0-100) of each ticker's 126-trading-day ROC against every other
*active, currently-processed* ticker with a valid 126d ROC that day.
Distinct in kind from ``relative_strength_vs_spy``/``sector_relative_strength``
(both single-benchmark time-series spreads, unaffected by this addition) —
this is a same-day rank against the universe, not a spread against SPY or a
sector/industry ETF. Scoring input only; no hard gate wired to it. NULL
whenever the ticker has <126 bars of history, or is the only ticker that day
with a valid 126d ROC (a lone value ranks at 100.0, not NULL — see
``_percentile_rank``). ``features_v02`` rows are retained as historical,
frozen, and do not get this field retroactively (same policy as the
``v01``->``v02`` bump).

P2.3/P2.4 (2026-07-10): ``features_v04`` adds two columns over ``features_v03``.
Both are **dormant** — persisted and tested, read by no validator or scoring
path, pending an explicit future decision (same discipline as
``rs_percentile_126d`` and ``market_breadth_pct``).

- ``vcp_sequence_score`` (P2.3) — 0-100 measure of *progressive* contraction
  inside the base window: successively shallower pullbacks on successively drier
  volume. Orthogonal to ``atr_compression_score`` / ``volume_dry_up_score``,
  which are single-window scalars and cannot distinguish a flat quiet range from
  a tightening coil. NULL (never 0.0) when the base is shorter than
  ``_VCP_MIN_BASE_BARS`` or holds fewer than ``_VCP_MIN_LEGS`` legs — VCP is a
  longer-base pattern and a short base has no sequence to judge.
- ``market_cap`` (P2.4) — ``shares_outstanding × close_raw``, where the share
  count is the point-in-time cover-page figure from ``ticker_fundamentals``
  (EDGAR ``dei``). Computed here, not in ``ticker_fundamentals``, because it is a
  daily price-dependent value and ``fundamentals_refresh`` runs *before*
  ``price_ingestion``. Uses ``close_raw`` deliberately: ``close_adj`` is
  retro-restated by later splits/dividends and would embed future corporate
  actions. NULL when the ticker has no share count knowable as of the cutoff.

Scope
-----
- Reads ``daily_prices`` (``data_quality_status = 'ok'``) plus warmup history.
- Writes exactly one ``daily_features`` row per processed ticker at its
  ``feature_cutoff_date`` via upsert on
  ``(ticker, feature_date, feature_schema_version)``.
- All indicators anchored on ``feature_cutoff_date`` — no look-ahead.
- ``market_regime`` is left NULL (owned by Module 12, open gap G-REGIME).
- ``days_to_earnings_bd`` / ``earnings_confidence`` / ``macro_event_risk_flag``
  fall back to NULL / NULL / FALSE (open gaps G-EARN / G-MACRO).

This module never calls providers, never imports ``duckdb`` directly, never
uses ``ATTACH`` / DDL / schema changes, never writes any table other than
``daily_features``, and never uses ``print()``.
"""

from __future__ import annotations

import bisect
import math
import uuid
from datetime import date, timedelta
from typing import Any, Final, Protocol

import polars as pl

from app.config import constants
from app.database import duckdb_manager
from app.services.fundamentals import fundamentals_quality as fq
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult
from app.utils.trading_calendar import trading_days_between

_LOG = logging_config.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Allowed DB roles
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

STATUS_OK: Final[str] = "ok"

# Warmup: calendar days before start_date to cover 252-td lookback.
# 252 trading days ≈ 353 calendar days; 420 gives a holiday buffer.
LOOKBACK_WARMUP_CALENDAR_DAYS: Final[int] = 420

# Minimum trading bars required for the longest lookback (52-week high).
REQUIRED_MIN_BARS: Final[int] = 252

# Per-indicator EWM minimum bars.
_MIN_BARS_EMA20: Final[int] = 20
_MIN_BARS_EMA50: Final[int] = 50
_MIN_BARS_EMA200: Final[int] = 200
_MIN_BARS_RSI14: Final[int] = 15
_MIN_BARS_ATR14: Final[int] = 15

# Wilder smoothing factor for RSI14 / ATR14.
_WILDER_ALPHA_14: Final[float] = 1.0 / 14.0

# --------------------------------------------------------------------------- #
# Pivot confirmation window (both sides) for swing high/low.
# A bar is a confirmed pivot if its high/low is strictly better than the
# _PIVOT_CONFIRM_BARS bars immediately before AND after it.
# --------------------------------------------------------------------------- #
_PIVOT_CONFIRM_BARS: Final[int] = 2
_SWING_LOOKBACK: Final[int] = 20  # bars scanned to find confirmed pivots

# Consolidation base: maximum true range relative to median true range.
_BASE_RANGE_MAX_MULTIPLE: Final[float] = 1.5
_BASE_MAX_DURATION: Final[int] = 60  # maximum bars in a base window

# --------------------------------------------------------------------------- #
# P2.3 — VCP sequencing (features_v04). Dormant: scoring-only, no gate, not read
# by any validator. Values are placeholders pending diagnostics, per CLAUDE.md's
# no-pre-diagnostic-tuning rule.
# --------------------------------------------------------------------------- #
# More sensitive than _PIVOT_CONFIRM_BARS (2): the swings inside a tight base are
# small and would otherwise go unconfirmed, under-counting legs.
_VCP_PIVOT_CONFIRM_BARS: Final[int] = 1
# A base shorter than this cannot hold a meaningful contraction sequence.
_VCP_MIN_BASE_BARS: Final[int] = 10
# Minervini's "2T-4T": two contractions is the minimum profile worth scoring.
_VCP_MIN_LEGS: Final[int] = 2
# Each leg must be at least this much shallower than the prior one to count as a
# real contraction. Without a material step, a flat base's equal-depth legs would
# satisfy a naive "non-increasing" test and score like a genuine coil.
_VCP_MIN_CONTRACTION: Final[float] = 0.10
_VCP_W_DEPTH: Final[float] = 0.45
_VCP_W_VOLUME: Final[float] = 0.25
_VCP_W_CONTRACTION: Final[float] = 0.30

# ATR compression: comparison window (bars).
_ATR_COMPRESSION_LOOKBACK: Final[int] = 60


# --------------------------------------------------------------------------- #
# Required / optional feature columns
# --------------------------------------------------------------------------- #
REQUIRED_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "ema20",
    "ema50",
    "ema200",
    "ema_alignment_score",
    "rsi14",
    "roc20",
    "atr14",
    "atr_pct",
    "rvol20",
    "avg_volume_20d",
    "avg_dollar_volume_20d",
    "distance_from_52w_high_pct",
    "pullback_from_recent_high_pct",
    "breakout_proximity",
    "consolidation_score",
)

OPTIONAL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    # v01 optional
    "distance_to_ema20_pct",
    "distance_to_ema50_pct",
    "distance_to_ema200_pct",
    "sector_relative_strength",
    "market_regime",
    "days_to_earnings_bd",
    "earnings_confidence",
    "macro_event_risk_flag",
    # v02 new (all optional — NULL when insufficient history)
    "ema20_slope",
    "ema50_slope",
    "atr_compression_score",
    "pullback_depth_pct",
    "swing_high",
    "swing_low",
    "support_level",
    "resistance_level",
    "next_resistance_level",
    "base_high",
    "base_low",
    "range_width_pct",
    "range_duration",
    "range_tightness_score",
    "volume_dry_up_score",
    "volume_expansion_score",
    "relative_strength_vs_spy",
    # v03 new (optional — NULL when <126 bars of history, or no other active
    # ticker that day has a valid 126d ROC to rank against)
    "rs_percentile_126d",
    # v04 new (optional, dormant — no validator/scoring path reads either yet)
    "market_cap",
    "vcp_sequence_score",
)

# --------------------------------------------------------------------------- #
# Metadata keys
# --------------------------------------------------------------------------- #
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "start_date",
    "end_date",
    "tickers_requested",
    "tickers_processed",
    "tickers_skipped_no_data",
    "rows_read",
    "feature_rows_written",
    "feature_rows_updated",
    "feature_ready_count",
    "feature_not_ready_count",
)

# --------------------------------------------------------------------------- #
# DB column list for the upsert — must match daily_features DDL order exactly
# --------------------------------------------------------------------------- #
_FEATURE_PARAM_COLUMNS: Final[tuple[str, ...]] = (
    "ticker",
    "feature_date",
    "feature_cutoff_date",
    "feature_schema_version",
    "feature_ready",
    # v01 columns
    "ema20",
    "ema50",
    "ema200",
    "ema_alignment_score",
    "ema20_slope",
    "ema50_slope",
    "distance_to_ema20_pct",
    "distance_to_ema50_pct",
    "distance_to_ema200_pct",
    "rsi14",
    "roc20",
    "atr14",
    "atr_pct",
    "atr_compression_score",
    "rvol20",
    "avg_volume_20d",
    "avg_dollar_volume_20d",
    "distance_from_52w_high_pct",
    "pullback_from_recent_high_pct",
    "pullback_depth_pct",
    "breakout_proximity",
    "consolidation_score",
    # v02 structural
    "swing_high",
    "swing_low",
    "support_level",
    "resistance_level",
    "next_resistance_level",
    "base_high",
    "base_low",
    "range_width_pct",
    "range_duration",
    "range_tightness_score",
    "volume_dry_up_score",
    "volume_expansion_score",
    "relative_strength_vs_spy",
    # v03 cross-sectional
    "rs_percentile_126d",
    # v04 dormant
    "market_cap",
    "vcp_sequence_score",
    # context / open-gap columns
    "sector_relative_strength",
    "market_regime",
    "days_to_earnings_bd",
    "earnings_confidence",
    "macro_event_risk_flag",
)

_KEY_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ticker", "feature_date", "feature_schema_version"}
)


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
_SELECT_DISTINCT_ELIGIBLE_TICKERS: Final[str] = (
    "SELECT DISTINCT ticker FROM daily_prices "
    "WHERE date >= ? AND date <= ? AND data_quality_status = ? "
    "ORDER BY ticker"
)

# Fetch high_raw / low_raw alongside adjusted columns so true-range computation
# in structural helpers has both raw and adjusted OHLC available.
_SELECT_PRICE_COLUMNS: Final[str] = (
    "SELECT ticker, date, close_raw, high_raw, low_raw, "
    "close_adj, high_adj, low_adj, volume_raw "
    "FROM daily_prices "
    "WHERE data_quality_status = ? AND date >= ? AND date <= ? "
    "AND ticker IN ({placeholders}) "
    "ORDER BY ticker, date"
)

_SELECT_SECTORS: Final[str] = (
    "SELECT ticker, sector, industry FROM ticker_master WHERE ticker IN ({placeholders})"
)

# Earnings: fetch all future/recent entries for the batch; filter per-ticker after load.
_SELECT_EARNINGS: Final[str] = (
    "SELECT ticker, earnings_date, confidence FROM earnings_calendar "
    "WHERE ticker IN ({placeholders}) AND CAST(updated_at AS DATE) <= ? "
    "ORDER BY ticker, earnings_date"
)


def _bd_to_earnings(cutoff: date, earnings_date: date) -> int | None:
    """Business days from cutoff (exclusive) to earnings_date (inclusive).

    Returns None if earnings_date < cutoff (already happened).
    Returns 0 if earnings_date == cutoff (same day).
    Uses NYSE trading sessions via trading_days_between.
    """
    if earnings_date < cutoff:
        return None
    if earnings_date == cutoff:
        return 0
    sessions = trading_days_between(cutoff + timedelta(days=1), earnings_date)
    return len(sessions)

_SELECT_EXISTING_KEYS: Final[str] = (
    "SELECT ticker, feature_date FROM daily_features "
    "WHERE feature_schema_version = ? AND ticker IN ({placeholders})"
)


def _build_upsert_sql() -> str:
    cols = list(_FEATURE_PARAM_COLUMNS)
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    update_cols = [c for c in cols if c not in _KEY_COLUMNS]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO daily_features ({col_list}, calculated_at) "
        f"VALUES ({placeholders}, CAST(now() AS TIMESTAMP)) "
        f"ON CONFLICT (ticker, feature_date, feature_schema_version) DO UPDATE SET "
        f"{set_clause}, calculated_at = CAST(now() AS TIMESTAMP)"
    )


_UPSERT_FEATURE_ROW: Final[str] = _build_upsert_sql()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sanitize(value: Any) -> Any:
    """Map NaN / ±inf floats to None so they never reach the DB."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _percentile_rank(sorted_values: list[float], value: float) -> float:
    """0-100 percentile rank of ``value`` within ``sorted_values`` (ascending).

    ``n<=1`` -> 100.0: a lone ticker (or a ticker whose family has no other
    members with a valid 126d ROC that day) has no peers to rank against, so
    it ranks at the top of its own single-member population rather than
    producing an undefined/NaN result.

    Note (rs_percentile_126d design note, P1.1): granularity is `100/(n-1)`
    points per rank and small-`n` percentiles are less statistically stable
    (usual floor `n>=30`) -- both are properties of whichever universe size
    is active on a given signal_date, not something this function solves for.
    """
    n = len(sorted_values)
    if n <= 1:
        return 100.0
    return 100.0 * bisect.bisect_left(sorted_values, value) / (n - 1)


# --------------------------------------------------------------------------- #
# Structural feature helpers (operate on a single ticker's price history)
# --------------------------------------------------------------------------- #
def _true_ranges(
    highs: list[float | None],
    lows: list[float | None],
    closes: list[float | None],
) -> list[float]:
    """Return per-bar true range using the full ATR formula.

    TR_i = max(high_i - low_i, |high_i - close_{i-1}|, |low_i - close_{i-1}|).
    For bar 0 (no previous close) we fall back to high - low.
    Uses adjusted OHLC per the v02 base-detection spec.

    Bars where any of high/low/close is None yield TR=0.0 (treated as
    non-qualifying in base detection via the median threshold).
    """
    n = len(highs)
    trs: list[float] = []
    for i in range(n):
        h = highs[i]
        lo = lows[i]
        c = closes[i]
        if h is None or lo is None or c is None:
            trs.append(0.0)
            continue
        hl = h - lo
        if i == 0:
            trs.append(hl)
        else:
            prev_c = closes[i - 1]
            if prev_c is None:
                trs.append(hl)
            else:
                trs.append(max(hl, abs(h - prev_c), abs(lo - prev_c)))
    return trs


def _compute_swing_pivots(
    ticker_df: pl.DataFrame,
    k: int = _PIVOT_CONFIRM_BARS,
    lookback: int = _SWING_LOOKBACK,
) -> tuple[list[float], list[float]]:
    """Return (swing_highs, swing_lows) lists for one ticker's price series.

    Collects ALL confirmed pivot highs and lows within the last ``lookback``
    confirmable bars (working backward from the most recent confirmable bar).
    A bar at index ``i`` is a confirmed pivot high if its ``high_adj`` is
    strictly greater than each of the ``k`` bars before it AND each of the
    ``k`` bars after it.  Swing lows use ``low_adj`` with strictly less-than.

    Returns lists ordered most-recent first.  Empty list when no confirmed
    pivot exists (or fewer than ``2*k + 1`` bars available).

    Bars with None highs or lows are skipped as pivot candidates and treated
    as non-qualifying neighbours (they cannot confirm a pivot).
    """
    n = len(ticker_df)
    if n < 2 * k + 1:
        return ([], [])

    highs: list[float | None] = ticker_df["high_adj"].to_list()
    lows: list[float | None] = ticker_df["low_adj"].to_list()

    last_confirmable = n - k - 1
    search_start = max(k, n - lookback - k)

    swing_highs: list[float] = []
    swing_lows: list[float] = []

    for i in range(last_confirmable, search_start - 1, -1):
        h = highs[i]
        l = lows[i]  # noqa: E741

        # Skip pivot candidate bars with None values
        if h is None or l is None:
            continue

        # Pivot high check — neighbours with None cannot satisfy strict inequality
        before_ok = all(
            highs[j] is not None and highs[j] < h  # type: ignore[operator]
            for j in range(i - k, i)
        )
        after_ok = all(
            highs[j] is not None and highs[j] < h  # type: ignore[operator]
            for j in range(i + 1, i + k + 1)
        )
        if before_ok and after_ok:
            swing_highs.append(h)

        # Pivot low check
        before_low_ok = all(
            lows[j] is not None and lows[j] > l  # type: ignore[operator]
            for j in range(i - k, i)
        )
        after_low_ok = all(
            lows[j] is not None and lows[j] > l  # type: ignore[operator]
            for j in range(i + 1, i + k + 1)
        )
        if before_low_ok and after_low_ok:
            swing_lows.append(l)

    return (swing_highs, swing_lows)


def _compute_support_resistance(
    close_adj: float | None,
    swing_highs: list[float],
    swing_lows: list[float],
    ema50: float | None,
    high20: float | None,
    high252: float | None,
) -> tuple[float | None, float | None, float | None]:
    """Derive support, resistance, next_resistance from structural level lists.

    Rules (all on adjusted basis; raw conversion deferred to Step 4):

    support_level:
        Nearest swing_low strictly below close_adj (i.e. largest qualifying
        swing low).  Fallback: ema50.

    resistance_level:
        Nearest swing_high strictly above close_adj (i.e. smallest qualifying
        swing high).  Fallback: max(high_adj, prior 20 td).

    next_resistance_level:
        Next swing_high strictly above resistance_level (i.e. smallest swing
        high > resistance).  Fallback: 52-week high if > resistance.

    Returns (support_level, resistance_level, next_resistance_level).
    All may be None when inputs are insufficient.
    """
    if close_adj is None:
        return (None, None, None)

    # --- support: largest swing_low below close ---
    candidates_below = [sl for sl in swing_lows if sl < close_adj]
    support: float | None = max(candidates_below) if candidates_below else None
    if support is None and ema50 is not None:
        support = ema50

    # --- resistance: smallest swing_high above close ---
    candidates_above = [sh for sh in swing_highs if sh > close_adj]
    resistance: float | None = min(candidates_above) if candidates_above else None
    if resistance is None and high20 is not None:
        resistance = high20

    # --- next_resistance: smallest swing_high strictly above resistance ---
    next_resistance: float | None = None
    if resistance is not None:
        above_resistance = [sh for sh in swing_highs if sh > resistance]
        if above_resistance:
            next_resistance = min(above_resistance)
        elif high252 is not None and high252 > resistance:
            next_resistance = high252

    return (support, resistance, next_resistance)


def _find_base_window(ticker_df: pl.DataFrame) -> tuple[int, int] | None:
    """Locate the base window as ``(start_idx, end_idx)``, end exclusive.

    The longest contiguous run, within the last ``_BASE_MAX_DURATION`` bars,
    of bars whose true range is ≤ ``_BASE_RANGE_MAX_MULTIPLE`` × the median true
    range of the prior 60 bars. The run need not end at the cutoff bar, so a
    base the price has partially broken out of is still found.

    Returns ``None`` when there are <60 bars, no usable reference median, or no
    qualifying run of at least 2 bars.

    Extracted from :func:`_compute_base` (P2.3) so ``_compute_vcp_sequence_score``
    measures the sequence inside exactly the window ``_compute_base`` reports.
    """
    n = len(ticker_df)
    if n < 60:
        return None

    highs: list[float | None] = ticker_df["high_adj"].to_list()
    lows: list[float | None] = ticker_df["low_adj"].to_list()
    closes: list[float | None] = ticker_df["close_adj"].to_list()

    trs = _true_ranges(highs, lows, closes)

    # Reference median TR: bars n-61 .. n-2 (60 bars before the cutoff bar)
    ref_start = max(0, n - 61)
    ref_end = n - 1  # exclusive
    # Exclude TR=0 sentinel values (from None bars) from the median
    reference_trs = sorted(tr for tr in trs[ref_start:ref_end] if tr > 0.0)
    if not reference_trs:
        return None
    mid = len(reference_trs) // 2
    median_tr = (
        reference_trs[mid]
        if len(reference_trs) % 2 == 1
        else (reference_trs[mid - 1] + reference_trs[mid]) / 2.0
    )
    if median_tr <= 0:
        return None

    threshold = _BASE_RANGE_MAX_MULTIPLE * median_tr

    # Search the last _BASE_MAX_DURATION bars for the longest qualifying run.
    search_start_idx = max(0, n - _BASE_MAX_DURATION)
    # Build qualifying mask; bars with TR=0 (None sentinel) are non-qualifying
    qualifying = [
        trs[i] > 0.0 and trs[i] <= threshold
        for i in range(search_start_idx, n)
    ]

    # Find longest contiguous True run
    best_start: int | None = None
    best_len = 0
    cur_start: int | None = None
    cur_len = 0
    for offset, q in enumerate(qualifying):
        if q:
            if cur_start is None:
                cur_start = offset
                cur_len = 1
            else:
                cur_len += 1
            if cur_len > best_len:
                best_len = cur_len
                best_start = cur_start
        else:
            cur_start = None
            cur_len = 0

    if best_start is None or best_len < 2:
        return None

    abs_start = search_start_idx + best_start
    return (abs_start, abs_start + best_len)


def _base_scoped_pivots(
    highs: list[float | None],
    lows: list[float | None],
    start: int,
    end: int,
    k: int = _VCP_PIVOT_CONFIRM_BARS,
) -> list[tuple[int, str, float]]:
    """Confirmed pivots strictly inside ``[start, end)``, chronological.

    Deliberately *not* :func:`_compute_swing_pivots`, for two reasons flagged in
    the P2.3 design note: that helper is capped to the last ``_SWING_LOOKBACK``
    bars (a base can start earlier) and returns prices, not indices (legs need
    ordering and bar spans). It also confirms with ``_PIVOT_CONFIRM_BARS`` = 2,
    which under-counts the small swings inside a tight base -- hence the more
    sensitive ``k`` here.

    Each element is ``(index, "H" | "L", price)``. A bar is a pivot high when its
    ``high`` strictly exceeds the ``k`` bars either side; a pivot low mirrors it
    on ``low``. Neighbours must lie inside the window, so pivots never peek at
    bars outside the base.
    """
    pivots: list[tuple[int, str, float]] = []
    for i in range(start + k, end - k):
        h = highs[i]
        lo = lows[i]
        if h is not None:
            window = [highs[j] for j in range(i - k, i + k + 1) if j != i]
            if all(v is not None and h > v for v in window):
                pivots.append((i, "H", h))
                continue
        if lo is not None:
            window = [lows[j] for j in range(i - k, i + k + 1) if j != i]
            if all(v is not None and lo < v for v in window):
                pivots.append((i, "L", lo))
    return pivots


def _extract_contraction_legs(
    pivots: list[tuple[int, str, float]],
    volumes: list[float | None],
) -> list[tuple[float, float]]:
    """Turn alternating pivots into ``(depth_pct, avg_volume)`` pullback legs.

    A leg is a pivot high followed by the next pivot low: the pullback. Depth is
    ``(high - low) / high``; volume is the mean over the leg's bar span. Repeated
    same-kind pivots collapse to the most extreme one (highest H, lowest L), so a
    noisy sequence like H H L still yields one clean leg.
    """
    # Collapse runs of same-kind pivots to their extreme.
    collapsed: list[tuple[int, str, float]] = []
    for pivot in pivots:
        if collapsed and collapsed[-1][1] == pivot[1]:
            prev = collapsed[-1]
            better = pivot[2] > prev[2] if pivot[1] == "H" else pivot[2] < prev[2]
            if better:
                collapsed[-1] = pivot
            continue
        collapsed.append(pivot)

    legs: list[tuple[float, float]] = []
    for first, second in zip(collapsed, collapsed[1:]):
        if first[1] != "H" or second[1] != "L":
            continue
        high_idx, _, high_price = first
        low_idx, _, low_price = second
        if high_price <= 0 or low_price >= high_price:
            continue
        depth = (high_price - low_price) / high_price
        span = [v for v in volumes[high_idx : low_idx + 1] if v is not None]
        if not span:
            continue
        legs.append((depth, sum(span) / len(span)))
    return legs


def _compute_vcp_sequence_score(ticker_df: pl.DataFrame) -> float | None:
    """0–100 score for progressive (VCP) contraction inside the base window.

    Minervini's defining trait: each successive pullback is *shallower* than the
    last, on *drier* volume (e.g. 25% → 12% → 6%). ``atr_compression_score`` and
    ``volume_dry_up_score`` cannot see this -- they are single-window scalars, so
    a uniformly quiet range and a genuine tightening coil score identically. This
    measures the *sequence*, and is computed from raw price/volume only: it reads
    neither of those scores, and neither reads it.

    ``None`` (never 0.0) when the sequence is not measurable:

    * fewer than 60 bars, or no base window (:func:`_find_base_window`);
    * a base shorter than ``_VCP_MIN_BASE_BARS`` -- too short to hold multiple
      legs;
    * fewer than ``_VCP_MIN_LEGS`` identifiable legs.

    That last case is inherent, not a defect: VCP is a longer-base pattern, and a
    10-bar base simply has no sequence to judge. ``None`` means "not measurable
    here", which downstream already distinguishes from a measured-and-poor 0.0.

    The score rewards three things, weighted:

    * ``depth_frac`` — share of adjacent leg pairs where depth contracted by at
      least ``_VCP_MIN_CONTRACTION``. Requiring a *material* step is what
      separates a true coil from a flat base, whose equal-depth legs would
      otherwise pass a mere "non-increasing" test.
    * ``volume_frac`` — share of adjacent pairs whose average volume did not
      rise. Volume is noisier than price, so this only asks for non-increasing.
    * ``contraction_ratio`` — how much tighter the final leg is than the first.

    Thresholds/weights are deliberate placeholders pending diagnostics
    (CLAUDE.md's no-pre-diagnostic-tuning rule); the field is dormant.
    """
    window = _find_base_window(ticker_df)
    if window is None:
        return None
    start, end = window
    if end - start < _VCP_MIN_BASE_BARS:
        return None

    highs: list[float | None] = ticker_df["high_adj"].to_list()
    lows: list[float | None] = ticker_df["low_adj"].to_list()
    volumes: list[float | None] = [
        float(v) if v is not None else None for v in ticker_df["volume_raw"].to_list()
    ]

    legs = _extract_contraction_legs(
        _base_scoped_pivots(highs, lows, start, end), volumes
    )
    if len(legs) < _VCP_MIN_LEGS:
        return None

    depths = [leg[0] for leg in legs]
    vols = [leg[1] for leg in legs]
    pairs = len(legs) - 1

    depth_hits = sum(
        1 for i in range(1, len(depths))
        if depths[i] <= depths[i - 1] * (1.0 - _VCP_MIN_CONTRACTION)
    )
    volume_hits = sum(1 for i in range(1, len(vols)) if vols[i] <= vols[i - 1])

    depth_frac = depth_hits / pairs
    volume_frac = volume_hits / pairs
    contraction_ratio = (
        max(0.0, min(1.0, 1.0 - depths[-1] / depths[0])) if depths[0] > 0 else 0.0
    )

    score = 100.0 * (
        _VCP_W_DEPTH * depth_frac
        + _VCP_W_VOLUME * volume_frac
        + _VCP_W_CONTRACTION * contraction_ratio
    )
    return float(max(0.0, min(100.0, score)))


def _compute_base(
    ticker_df: pl.DataFrame,
) -> tuple[float | None, float | None, int | None, float | None, float | None]:
    """Compute base_high, base_low, range_duration, range_width_pct, range_tightness_score.

    Algorithm:
    1. Compute per-bar true range (full ATR formula: max of hl, |h-prev_c|,
       |l-prev_c|) on adjusted prices.
    2. Derive the reference threshold: median true range over the prior 60 bars
       (bars n-61 .. n-2, i.e. not including the cutoff bar) × 1.5.
    3. Within the last 60 bars (bars n-60 .. n-1), find the *longest*
       contiguous window of bars whose true range ≤ threshold.  The window is
       not required to end at the cutoff bar, supporting detection of a base
       that the price has partially broken out of.
    4. If the longest qualifying run is ≥ 2 bars, compute:
       - base_high = max(high_adj) over the window
       - base_low  = min(low_adj)  over the window
       - range_width_pct = (base_high - base_low) / base_low
       - range_tightness_score = 100 × (1 - min(range_width_pct / 0.20, 1))

    Requires ≥ 60 bars.  Returns all-None on insufficient data or no run ≥ 2.
    Bars with any None in high_adj/low_adj/close_adj are treated as
    non-qualifying (TR=0.0 but excluded from base_high/base_low computation).

    Window detection itself lives in :func:`_find_base_window`, shared with
    ``_compute_vcp_sequence_score`` (P2.3) so the two cannot disagree about
    where the base is.
    """
    window = _find_base_window(ticker_df)
    if window is None:
        return (None, None, None, None, None)
    abs_start, abs_end = window
    best_len = abs_end - abs_start

    highs: list[float | None] = ticker_df["high_adj"].to_list()
    lows: list[float | None] = ticker_df["low_adj"].to_list()

    # Only use bars with valid (non-None) OHLC values
    valid_highs = [h for h in highs[abs_start:abs_end] if h is not None]
    valid_lows = [lo for lo in lows[abs_start:abs_end] if lo is not None]
    if not valid_highs or not valid_lows:
        return (None, None, None, None, None)

    base_high = max(valid_highs)
    base_low = min(valid_lows)

    if base_low <= 0:
        return (None, None, None, None, None)

    range_width_pct = (base_high - base_low) / base_low
    range_tightness_score = float(
        max(0.0, min(100.0, 100.0 * (1.0 - min(range_width_pct / 0.20, 1.0))))
    )

    return (base_high, base_low, best_len, range_width_pct, range_tightness_score)


class _DbManagerLike(Protocol):
    """Minimal interface the engine needs from the DB manager."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# Feature engine
# --------------------------------------------------------------------------- #
class FeatureEngine:
    """Compute and upsert ``daily_features`` rows for a date range.

    Stateless; ``db_manager=None`` uses the real :mod:`app.database.duckdb_manager`.
    Tests inject a fake manager to avoid touching real DB files.
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def calculate(
        self,
        start_date: date,
        end_date: date,
        tickers: list[str] | None = None,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Compute ``daily_features`` for ``[start_date, end_date]``."""
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()
        tickers_requested = 0 if tickers is None else len(dict.fromkeys(tickers))

        log.info(
            "calculate start db_role=%s start_date=%s end_date=%s tickers=%s",
            db_role, start_iso, end_iso,
            "all" if tickers is None else len(tickers),
        )

        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 11 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("calculate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso, tickers_requested)

        if start_date > end_date:
            message = f"Invalid date range: start_date {start_iso} > end_date {end_iso}."
            log.error("calculate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso, tickers_requested)

        warmup_start = start_date - timedelta(days=LOOKBACK_WARMUP_CALENDAR_DAYS)
        try:
            read = self._read(db_role, start_date, end_date, warmup_start, tickers)
        except Exception as exc:  # noqa: BLE001
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("calculate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso, tickers_requested)

        process_tickers = read["process_tickers"]
        tickers_skipped = read["tickers_skipped_no_data"]
        rows_read = read["rows_read"]

        # P2.4: one read for the whole batch; the per-cutoff as-of join happens
        # in _build_feature_rows so an early cutoff never sees a later filing.
        shares_history = fq.read_shares_history(self._db, db_role, end_date)

        feature_rows = self._build_feature_rows(
            read["prices"],
            process_tickers,
            read["sector_by_ticker"],
            read.get("industry_by_ticker", {}),
            read.get("earnings_by_ticker", {}),
            start_date,
            end_date,
            shares_history,
        )
        tickers_processed = len(feature_rows)
        ready_count = sum(1 for r in feature_rows if r["feature_ready"])
        not_ready_count = tickers_processed - ready_count

        log.info(
            "calculate computed rows_read=%d tickers_processed=%d "
            "tickers_skipped_no_data=%d ready=%d not_ready=%d",
            rows_read, tickers_processed, tickers_skipped, ready_count, not_ready_count,
        )

        try:
            written, updated = self._write(db_role, feature_rows)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "calculate failed during write (rolled back): %s: %s",
                type(exc).__name__, exc,
            )
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=tickers_processed,
                errors=[f"{type(exc).__name__}: {exc}"],
                metadata=self._metadata(
                    db_role=db_role,
                    start_date=start_iso,
                    end_date=end_iso,
                    tickers_requested=tickers_requested,
                    tickers_processed=tickers_processed,
                    tickers_skipped_no_data=tickers_skipped,
                    rows_read=rows_read,
                ),
            )

        metadata = self._metadata(
            db_role=db_role,
            start_date=start_iso,
            end_date=end_iso,
            tickers_requested=tickers_requested,
            tickers_processed=tickers_processed,
            tickers_skipped_no_data=tickers_skipped,
            rows_read=rows_read,
            feature_rows_written=written,
            feature_rows_updated=updated,
            feature_ready_count=ready_count,
            feature_not_ready_count=not_ready_count,
        )

        log.info(
            "calculate done status=success rows_read=%d tickers_processed=%d "
            "written=%d updated=%d ready=%d not_ready=%d",
            rows_read, tickers_processed, written, updated, ready_count, not_ready_count,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=tickers_processed,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Read phase
    # ------------------------------------------------------------------ #
    def _read(
        self,
        db_role: str,
        start_date: date,
        end_date: date,
        warmup_start: date,
        tickers: list[str] | None,
    ) -> dict[str, Any]:
        """Resolve selection, sectors, and price rows in one read-only pass."""
        connection = self._db.connect(db_role)
        try:
            eligible_rows = connection.execute(
                _SELECT_DISTINCT_ELIGIBLE_TICKERS,
                [start_date, end_date, STATUS_OK],
            ).fetchall()
            eligible_in_range = {row[0] for row in eligible_rows}

            if tickers is None:
                process_tickers = sorted(eligible_in_range)
                tickers_skipped = 0
            else:
                requested_unique = list(dict.fromkeys(tickers))
                process_tickers = [t for t in requested_unique if t in eligible_in_range]
                tickers_skipped = sum(1 for t in requested_unique if t not in eligible_in_range)

            if not process_tickers:
                empty = pl.DataFrame(
                    schema={
                        "ticker": pl.Utf8,
                        "date": pl.Date,
                        "close_raw": pl.Float64,
                        "high_raw": pl.Float64,
                        "low_raw": pl.Float64,
                        "close_adj": pl.Float64,
                        "high_adj": pl.Float64,
                        "low_adj": pl.Float64,
                        "volume_raw": pl.Int64,
                    }
                )
                return {
                    "process_tickers": [],
                    "tickers_skipped_no_data": tickers_skipped,
                    "sector_by_ticker": {},
                    "industry_by_ticker": {},
                    "prices": empty,
                    "rows_read": 0,
                }

            sector_by_ticker, industry_by_ticker = self._read_sectors(connection, process_tickers)
            benchmark_etfs: set[str] = set()
            for _t in process_tickers:
                _sector = sector_by_ticker.get(_t)
                _industry = industry_by_ticker.get(_t)
                _etf = (
                    constants.INDUSTRY_ETF_MAP.get(_sector or "", {}).get(_industry or "")
                    if _sector and _industry
                    else None
                )
                if _etf is None and _sector:
                    _etf = constants.SECTOR_ETF_MAP.get(_sector)
                if _etf:
                    benchmark_etfs.add(_etf)

            # Always include SPY for relative_strength_vs_spy (v02).
            load_set = sorted(set(process_tickers) | benchmark_etfs | {constants.BENCHMARK_SPY})
            etf_load_set = (benchmark_etfs | {constants.BENCHMARK_SPY}) - set(process_tickers)
            _LOG.debug(
                "etf_lookup db_role=%s end_date=%s etfs_in_load_set=%d etfs=%s",
                db_role, end_date, len(etf_load_set), sorted(etf_load_set),
            )
            placeholders = ", ".join("?" for _ in load_set)
            sql = _SELECT_PRICE_COLUMNS.format(placeholders=placeholders)
            params = [STATUS_OK, warmup_start, end_date, *load_set]
            price_rows = connection.execute(sql, params).fetchall()

            # Verify each ETF has a signal-date row (status='ok'); log any gaps.
            etf_signal_date_found: set[str] = set()
            for row in price_rows:
                if row[0] in etf_load_set and row[1] == end_date:
                    etf_signal_date_found.add(row[0])
            missing_signal = etf_load_set - etf_signal_date_found
            if missing_signal:
                _LOG.debug(
                    "etf_lookup signal_date_missing db_role=%s end_date=%s "
                    "missing_etfs=%s — validator may have degraded their status",
                    db_role, end_date, sorted(missing_signal),
                )
            else:
                _LOG.debug(
                    "etf_lookup signal_date_ok db_role=%s end_date=%s "
                    "all_%d_etfs_have_ok_row",
                    db_role, end_date, len(etf_load_set),
                )

            # G-EARN gap closure: load earnings_calendar for all process_tickers.
            earnings_by_ticker = self._read_earnings(connection, process_tickers, end_date)
        finally:
            connection.close()

        prices = pl.DataFrame(
            price_rows,
            schema=[
                ("ticker", pl.Utf8),
                ("date", pl.Date),
                ("close_raw", pl.Float64),
                ("high_raw", pl.Float64),
                ("low_raw", pl.Float64),
                ("close_adj", pl.Float64),
                ("high_adj", pl.Float64),
                ("low_adj", pl.Float64),
                ("volume_raw", pl.Int64),
            ],
            orient="row",
        ).sort(["ticker", "date"])

        return {
            "process_tickers": process_tickers,
            "tickers_skipped_no_data": tickers_skipped,
            "sector_by_ticker": sector_by_ticker,
            "industry_by_ticker": industry_by_ticker,
            "prices": prices,
            "rows_read": prices.height,
            "earnings_by_ticker": earnings_by_ticker,
        }

    def _read_sectors(
        self, connection: Any, process_tickers: list[str]
    ) -> tuple[dict[str, str | None], dict[str, str | None]]:
        placeholders = ", ".join("?" for _ in process_tickers)
        sql = _SELECT_SECTORS.format(placeholders=placeholders)
        rows = connection.execute(sql, list(process_tickers)).fetchall()
        sector_by_ticker = {row[0]: row[1] for row in rows}
        industry_by_ticker = {row[0]: row[2] for row in rows}
        return sector_by_ticker, industry_by_ticker

    def _read_earnings(
        self, connection: Any, process_tickers: list[str], cutoff_date: date
    ) -> dict[str, list[tuple[date, str]]]:
        """Return {ticker: [(earnings_date, confidence), ...]} sorted ascending.

        Only records with updated_at <= cutoff_date are included (point-in-time safe).
        """
        if not process_tickers:
            return {}
        try:
            placeholders = ", ".join("?" for _ in process_tickers)
            sql = _SELECT_EARNINGS.format(placeholders=placeholders)
            rows = connection.execute(sql, list(process_tickers) + [cutoff_date]).fetchall()
            result: dict[str, list[tuple[date, str]]] = {}
            for ticker, edate, conf in rows:
                result.setdefault(ticker, []).append((edate, conf or "low"))
            return result
        except Exception:  # noqa: BLE001 — earnings are best-effort
            return {}

    # ------------------------------------------------------------------ #
    # Compute phase
    # ------------------------------------------------------------------ #
    def _build_feature_rows(
        self,
        prices: pl.DataFrame,
        process_tickers: list[str],
        sector_by_ticker: dict[str, str | None],
        industry_by_ticker: dict[str, str | None],
        earnings_by_ticker: dict[str, list[tuple[date, str]]],
        start_date: date,
        end_date: date,
        shares_history: dict[str, list[tuple[date, float]]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return one feature-row dict per processed ticker at its cutoff."""
        if prices.height == 0 or not process_tickers:
            return []
        shares_history = shares_history or {}

        features = _compute_features(prices)

        in_range = (pl.col("date") >= start_date) & (pl.col("date") <= end_date)
        features = features.with_columns(
            pl.when(in_range)
            .then(pl.col("date"))
            .otherwise(None)
            .max()
            .over("ticker")
            .alias("_cutoff_date")
        )

        # roc20 lookup keyed by (ticker, date) for sector RS and SPY RS.
        roc_lookup: dict[tuple[str, date], float | None] = {}
        for rec in features.select(["ticker", "date", "roc20"]).iter_rows(named=True):
            roc_lookup[(rec["ticker"], rec["date"])] = _sanitize(rec["roc20"])

        # Per-ETF latest-available-date index.  The data validator can degrade
        # an ETF's signal_date row; the exact-date lookup would then return None
        # even though warmup-history entries exist for that ETF.
        _benchmark_etfs: frozenset[str] = frozenset(constants.REQUIRED_BENCHMARK_SYMBOLS) | frozenset(
            etf
            for sector_map in constants.INDUSTRY_ETF_MAP.values()
            for etf in sector_map.values()
        )
        _etf_latest_date: dict[str, date] = {}
        for (tkr, d) in roc_lookup:
            if tkr in _benchmark_etfs:
                prev = _etf_latest_date.get(tkr)
                if prev is None or d > prev:
                    _etf_latest_date[tkr] = d

        def _etf_roc(sym: str, cutoff: date) -> float | None:
            v = roc_lookup.get((sym, cutoff))
            if v is None:
                best = _etf_latest_date.get(sym)
                if best is not None and best <= cutoff:
                    _LOG.debug(
                        "etf_roc_date_fallback sym=%s cutoff=%s fallback_date=%s",
                        sym, cutoff, best,
                    )
                    v = roc_lookup.get((sym, best))
            return v

        process_set = set(process_tickers)
        cutoff_frame = features.filter(
            pl.col("ticker").is_in(list(process_set))
            & pl.col("_cutoff_date").is_not_null()
            & (pl.col("date") == pl.col("_cutoff_date"))
        )

        # Build deduplicated select list (ema50 lives in REQUIRED_FEATURE_COLUMNS already)
        _select_base = [
            "ticker", "date",
            *REQUIRED_FEATURE_COLUMNS,
            "distance_to_ema20_pct",
            "distance_to_ema50_pct",
            "distance_to_ema200_pct",
            "ema20_slope",
            "ema50_slope",
            "atr_compression_score",
            "pullback_depth_pct",
            "volume_dry_up_score",
            "volume_expansion_score",
            "roc126",
            "_high20",
            "_high252",
        ]
        seen: set[str] = set()
        select_cols: list[str] = []
        for c in _select_base:
            if c not in seen:
                seen.add(c)
                select_cols.append(c)

        rows: list[dict[str, Any]] = []
        for rec in cutoff_frame.select(select_cols).iter_rows(named=True):
            ticker = rec["ticker"]
            cutoff = rec["date"]

            try:
                # Industry RS: industry ETF first, sector ETF fallback (v01 pattern)
                sector = sector_by_ticker.get(ticker)
                industry = industry_by_ticker.get(ticker)
                industry_etf: str | None = None
                if sector and industry:
                    industry_etf = constants.INDUSTRY_ETF_MAP.get(sector, {}).get(industry)
                sector_etf: str | None = constants.SECTOR_ETF_MAP.get(sector) if sector else None
                etf = industry_etf or sector_etf
                sector_rs: float | None = None
                ticker_roc = _sanitize(rec["roc20"])
                if etf is not None and ticker_roc is not None:
                    etf_roc = _etf_roc(etf, cutoff)
                    if (
                        etf_roc is None
                        and industry_etf is not None
                        and sector_etf is not None
                        and industry_etf != sector_etf
                    ):
                        import logging as _log_mod
                        _log_mod.getLogger(__name__).warning(
                            "benchmark fallback_used ticker=%s industry_etf=%s "
                            "sector_etf=%s reason=no_price_rows",
                            ticker, industry_etf, sector_etf,
                        )
                        etf_roc = _etf_roc(sector_etf, cutoff)
                    if etf_roc is not None:
                        sector_rs = ticker_roc - etf_roc

                # relative_strength_vs_spy (v02)
                rs_vs_spy: float | None = None
                if ticker_roc is not None:
                    spy_roc = _etf_roc(constants.BENCHMARK_SPY, cutoff)
                    if spy_roc is not None:
                        rs_vs_spy = ticker_roc - spy_roc

                # Per-ticker structural features: operate on full history slice.
                ticker_prices = prices.filter(pl.col("ticker") == ticker).sort("date")

                swing_highs, swing_lows = _compute_swing_pivots(ticker_prices)

                # close_adj at cutoff for support/resistance derivation
                cutoff_rows = ticker_prices.filter(pl.col("date") == cutoff)
                ca_rows = cutoff_rows.select("close_adj")
                close_adj_val: float | None = (
                    _sanitize(ca_rows[0, "close_adj"]) if ca_rows.height > 0 else None
                )

                # P2.4: market_cap = shares_outstanding x close_raw. Deliberately
                # the *unadjusted* close -- see fq.compute_market_cap. Dormant
                # field: nothing reads it yet.
                close_raw_val: float | None = (
                    _sanitize(cutoff_rows[0, "close_raw"]) if cutoff_rows.height > 0 else None
                )
                market_cap = fq.compute_market_cap(
                    fq.shares_as_of(shares_history, ticker, cutoff), close_raw_val
                )

                support, resistance, next_resistance = _compute_support_resistance(
                    close_adj=close_adj_val,
                    swing_highs=swing_highs,
                    swing_lows=swing_lows,
                    ema50=_sanitize(rec["ema50"]),
                    high20=_sanitize(rec["_high20"]),
                    high252=_sanitize(rec["_high252"]),
                )

                base_high, base_low, range_duration, range_width_pct, range_tightness = (
                    _compute_base(ticker_prices)
                )

                # P2.3: progressive-contraction sequence within the same base
                # window. Dormant field; NULL when the base holds <2 legs.
                vcp_sequence_score = _compute_vcp_sequence_score(ticker_prices)

                # Most-recent single swing pivot values for storage
                swing_high_val = swing_highs[0] if swing_highs else None
                swing_low_val = swing_lows[0] if swing_lows else None
                # Swing low at or above current price is not a valid stop anchor
                if swing_low_val is not None and close_adj_val is not None and swing_low_val >= close_adj_val:
                    swing_low_val = None

                # G-EARN gap closure: compute days_to_earnings_bd from earnings_calendar.
                ticker_earnings = earnings_by_ticker.get(ticker, [])
                days_to_earn: int | None = None
                earn_conf: str | None = None
                for edate, econf in ticker_earnings:
                    bd = _bd_to_earnings(cutoff, edate)
                    if bd is not None and (days_to_earn is None or bd < days_to_earn):
                        days_to_earn = bd
                        earn_conf = econf

                row = self._assemble_row(
                    ticker=ticker,
                    cutoff=cutoff,
                    rec=rec,
                    sector_rs=sector_rs,
                    rs_vs_spy=rs_vs_spy,
                    roc126=_sanitize(rec["roc126"]),
                    market_cap=market_cap,
                    vcp_sequence_score=vcp_sequence_score,
                    swing_high=swing_high_val,
                    swing_low=swing_low_val,
                    support=support,
                    resistance=resistance,
                    next_resistance=next_resistance,
                    base_high=base_high,
                    base_low=base_low,
                    range_duration=range_duration,
                    range_width_pct=range_width_pct,
                    range_tightness_score=range_tightness,
                    days_to_earnings=days_to_earn,
                    earnings_confidence=earn_conf,
                )
                rows.append(row)

            except Exception as exc:  # noqa: BLE001
                # Log and skip this ticker; do not crash the whole batch.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "feature compute error ticker=%s cutoff=%s %s: %s — skipping",
                    ticker, cutoff, type(exc).__name__, exc,
                )

        # P1.1 (features_v03): rs_percentile_126d -- cross-sectional percentile
        # rank of roc126 within the active universe *per cutoff date* (grouped
        # by feature_date, not assumed single-date, since a batch run can span
        # multiple signal_dates' worth of cutoffs). Tickers with <126 bars of
        # history have roc126=None and get rs_percentile_126d=None -- excluded
        # from the ranking population, not ranked at 0.
        by_cutoff: dict[date, list[float]] = {}
        for r in rows:
            v = r["roc126"]
            if v is not None:
                by_cutoff.setdefault(r["feature_date"], []).append(v)
        for values in by_cutoff.values():
            values.sort()
        for r in rows:
            v = r.pop("roc126")
            r["rs_percentile_126d"] = (
                _percentile_rank(by_cutoff[r["feature_date"]], v) if v is not None else None
            )

        rows.sort(key=lambda r: r["ticker"])
        return rows

    def _assemble_row(
        self,
        ticker: str,
        cutoff: date,
        rec: dict[str, Any],
        sector_rs: float | None,
        rs_vs_spy: float | None,
        roc126: float | None,
        swing_high: float | None,
        swing_low: float | None,
        support: float | None,
        resistance: float | None,
        next_resistance: float | None,
        base_high: float | None,
        base_low: float | None,
        range_duration: int | None,
        range_width_pct: float | None,
        range_tightness_score: float | None,
        market_cap: float | None = None,
        vcp_sequence_score: float | None = None,
        days_to_earnings: int | None = None,
        earnings_confidence: str | None = None,
    ) -> dict[str, Any]:
        """Assemble a single ``daily_features`` row dict (sanitised)."""
        required = {col: _sanitize(rec[col]) for col in REQUIRED_FEATURE_COLUMNS}
        feature_ready = all(required[col] is not None for col in REQUIRED_FEATURE_COLUMNS)

        return {
            "ticker": ticker,
            "feature_date": cutoff,
            "feature_cutoff_date": cutoff,
            "feature_schema_version": constants.FEATURE_SCHEMA_VERSION,
            "feature_ready": feature_ready,
            # v01 required
            **required,
            # v01 optional EMAs
            "distance_to_ema20_pct": _sanitize(rec["distance_to_ema20_pct"]),
            "distance_to_ema50_pct": _sanitize(rec["distance_to_ema50_pct"]),
            "distance_to_ema200_pct": _sanitize(rec["distance_to_ema200_pct"]),
            # v02 vectorised
            "ema20_slope": _sanitize(rec["ema20_slope"]),
            "ema50_slope": _sanitize(rec["ema50_slope"]),
            "atr_compression_score": _sanitize(rec["atr_compression_score"]),
            "pullback_depth_pct": _sanitize(rec["pullback_depth_pct"]),
            "volume_dry_up_score": _sanitize(rec["volume_dry_up_score"]),
            "volume_expansion_score": _sanitize(rec["volume_expansion_score"]),
            # v02 structural (per-ticker computed)
            "swing_high": _sanitize(swing_high),
            "swing_low": _sanitize(swing_low),
            "support_level": _sanitize(support),
            "resistance_level": _sanitize(resistance),
            "next_resistance_level": _sanitize(next_resistance),
            "base_high": _sanitize(base_high),
            "base_low": _sanitize(base_low),
            "range_width_pct": _sanitize(range_width_pct),
            "range_duration": range_duration,
            "range_tightness_score": _sanitize(range_tightness_score),
            "relative_strength_vs_spy": _sanitize(rs_vs_spy),
            # v03: raw 126d ROC (transient -- not in _FEATURE_PARAM_COLUMNS,
            # consumed and popped by the cross-sectional percentile pass in
            # _build_feature_rows) and its placeholder percentile output.
            "roc126": roc126,
            "rs_percentile_126d": None,
            # v04 dormant fields (nothing reads these yet)
            "market_cap": _sanitize(market_cap),
            "vcp_sequence_score": _sanitize(vcp_sequence_score),
            # context
            "sector_relative_strength": _sanitize(sector_rs),
            # market_regime: open gap G-REGIME (owned by Module 12)
            "market_regime": None,
            # G-EARN gap closed: populated from earnings_calendar when data exists
            "days_to_earnings_bd": days_to_earnings,
            "earnings_confidence": earnings_confidence,
            # macro_event_risk_flag: open gap G-MACRO
            "macro_event_risk_flag": False,
        }

    # ------------------------------------------------------------------ #
    # Write phase
    # ------------------------------------------------------------------ #
    def _write(self, db_role: str, feature_rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Upsert every feature row in a single transaction; return (written, updated)."""
        if not feature_rows:
            return (0, 0)

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                existing = self._existing_keys(connection, feature_rows)
                written = 0
                updated = 0
                for row in feature_rows:
                    key = (row["ticker"], row["feature_date"])
                    if key in existing:
                        updated += 1
                    else:
                        written += 1
                    params = [row[col] for col in _FEATURE_PARAM_COLUMNS]
                    connection.execute(_UPSERT_FEATURE_ROW, params)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return (written, updated)

    def _existing_keys(
        self, connection: Any, feature_rows: list[dict[str, Any]]
    ) -> set[tuple[str, date]]:
        tickers = sorted({row["ticker"] for row in feature_rows})
        placeholders = ", ".join("?" for _ in tickers)
        sql = _SELECT_EXISTING_KEYS.format(placeholders=placeholders)
        params = [constants.FEATURE_SCHEMA_VERSION, *tickers]
        rows = connection.execute(sql, params).fetchall()
        return {(row[0], row[1]) for row in rows}

    # ------------------------------------------------------------------ #
    # Result builders
    # ------------------------------------------------------------------ #
    def _failed(
        self,
        run_id: str,
        message: str,
        db_role: str,
        start_iso: str,
        end_iso: str,
        tickers_requested: int,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._metadata(
                db_role=db_role,
                start_date=start_iso,
                end_date=end_iso,
                tickers_requested=tickers_requested,
            ),
        )

    def _metadata(
        self,
        *,
        db_role: str,
        start_date: str,
        end_date: str,
        tickers_requested: int = 0,
        tickers_processed: int = 0,
        tickers_skipped_no_data: int = 0,
        rows_read: int = 0,
        feature_rows_written: int = 0,
        feature_rows_updated: int = 0,
        feature_ready_count: int = 0,
        feature_not_ready_count: int = 0,
    ) -> dict[str, Any]:
        return {
            "db_role": db_role,
            "start_date": start_date,
            "end_date": end_date,
            "tickers_requested": tickers_requested,
            "tickers_processed": tickers_processed,
            "tickers_skipped_no_data": tickers_skipped_no_data,
            "rows_read": rows_read,
            "feature_rows_written": feature_rows_written,
            "feature_rows_updated": feature_rows_updated,
            "feature_ready_count": feature_ready_count,
            "feature_not_ready_count": feature_not_ready_count,
        }


# --------------------------------------------------------------------------- #
# Vectorised indicator computation (all tickers at once via Polars)
# --------------------------------------------------------------------------- #
def _compute_features(prices: pl.DataFrame) -> pl.DataFrame:
    """Return per-(ticker, date) feature frame from ``prices``.

    ``prices`` must be sorted by (ticker, date) and contain:
    ticker, date, close_raw, high_raw, low_raw, close_adj, high_adj, low_adj, volume_raw.

    Rows where any required OHLC field (close_raw, high_raw, low_raw, close_adj,
    high_adj, low_adj) is NULL are dropped before indicator computation.  This
    prevents arithmetic crashes on failed / quarantined price rows that passed
    the data_quality_status='ok' filter but still have NULL OHLCV values.

    ``volume_raw`` may be NULL (e.g. index / VIX symbols with valid OHLC but no
    volume).  The downstream dollar / rvol / volume-score columns become NULL for
    those rows — Polars handles this null-safely; no crash results.

    All indicators are vectorised per ticker via Polars window / rolling
    expressions.  Immature recursive (EWM) values are nulled below their
    minimum-bar count.
    """
    # Drop rows with NULL in any required OHLC field; volume_raw may be NULL.
    _required_ohlc = ["close_raw", "high_raw", "low_raw", "close_adj", "high_adj", "low_adj"]
    prices = prices.filter(
        pl.all_horizontal([pl.col(c).is_not_null() for c in _required_ohlc])
    )
    # ------------------------------------------------------------------ #
    # Stage A: primitives
    # ------------------------------------------------------------------ #
    frame = prices.with_columns(
        pl.col("date").cum_count().over("ticker").alias("bar_index"),
        pl.col("close_adj").shift(1).over("ticker").alias("_prev_close_adj"),
        pl.col("close_adj").diff().over("ticker").alias("_delta"),
        pl.col("close_adj").shift(20).over("ticker").alias("_close_adj_lag20"),
        # v03: 126-trading-day (~6mo) lag for rs_percentile_126d's raw ROC input.
        pl.col("close_adj").shift(126).over("ticker").alias("_close_adj_lag126"),
        (pl.col("close_raw") * pl.col("volume_raw")).alias("_dollar"),
        (pl.col("high_adj") - pl.col("low_adj")).alias("_range_hl"),
    )

    # ------------------------------------------------------------------ #
    # Stage B: EMAs, gain/loss, true range, rolling windows
    # ------------------------------------------------------------------ #
    frame = frame.with_columns(
        pl.col("close_adj").ewm_mean(span=20, adjust=False).over("ticker").alias("_ema20_raw"),
        pl.col("close_adj").ewm_mean(span=50, adjust=False).over("ticker").alias("_ema50_raw"),
        pl.col("close_adj").ewm_mean(span=200, adjust=False).over("ticker").alias("_ema200_raw"),
        pl.when(pl.col("_delta") > 0).then(pl.col("_delta")).otherwise(0.0).fill_null(0.0).alias("_gain"),
        pl.when(pl.col("_delta") < 0).then(-pl.col("_delta")).otherwise(0.0).fill_null(0.0).alias("_loss"),
        pl.max_horizontal(
            pl.col("high_adj") - pl.col("low_adj"),
            (pl.col("high_adj") - pl.col("_prev_close_adj")).abs(),
            (pl.col("low_adj") - pl.col("_prev_close_adj")).abs(),
        ).alias("_tr"),
        pl.col("volume_raw").shift(1).over("ticker").alias("_vol_lag1"),
        pl.col("_dollar").shift(1).over("ticker").alias("_dollar_lag1"),
        pl.col("close_adj").rolling_max(window_size=20, min_samples=20).over("ticker").alias("_high20"),
        pl.col("close_adj").rolling_max(window_size=252, min_samples=252).over("ticker").alias("_high252"),
        pl.col("_range_hl").rolling_mean(window_size=10, min_samples=10).over("ticker").alias("_range_mean10"),
        pl.col("_range_hl").rolling_mean(window_size=60, min_samples=60).over("ticker").alias("_range_mean60"),
        pl.col("volume_raw").rolling_mean(window_size=10, min_samples=10).over("ticker").alias("_vol_mean10"),
        pl.col("volume_raw").rolling_mean(window_size=60, min_samples=60).over("ticker").alias("_vol_mean60"),
        pl.col("high_adj").rolling_max(window_size=20, min_samples=20).over("ticker").alias("_high_adj_20"),
    )

    # ------------------------------------------------------------------ #
    # Stage C: Wilder averages, ATR14, volume means
    # ------------------------------------------------------------------ #
    frame = frame.with_columns(
        pl.col("_gain").ewm_mean(alpha=_WILDER_ALPHA_14, adjust=False).over("ticker").alias("_avg_gain"),
        pl.col("_loss").ewm_mean(alpha=_WILDER_ALPHA_14, adjust=False).over("ticker").alias("_avg_loss"),
        pl.when(pl.col("bar_index") >= _MIN_BARS_ATR14)
        .then(pl.col("_tr").ewm_mean(alpha=_WILDER_ALPHA_14, adjust=False).over("ticker"))
        .otherwise(None)
        .alias("atr14"),
        pl.col("_vol_lag1").rolling_mean(window_size=20, min_samples=20).over("ticker").alias("avg_volume_20d"),
        pl.col("_dollar_lag1").rolling_mean(window_size=20, min_samples=20).over("ticker").alias("avg_dollar_volume_20d"),
    )

    # ------------------------------------------------------------------ #
    # Stage D: masked EMAs, ATR 60d mean (for consolidation + compression)
    # ------------------------------------------------------------------ #
    frame = frame.with_columns(
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA20).then(pl.col("_ema20_raw")).otherwise(None).alias("ema20"),
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA50).then(pl.col("_ema50_raw")).otherwise(None).alias("ema50"),
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA200).then(pl.col("_ema200_raw")).otherwise(None).alias("ema200"),
        pl.col("atr14").rolling_mean(window_size=60, min_samples=60).over("ticker").alias("_atr_mean60"),
        pl.col("_ema20_raw").shift(5).over("ticker").alias("_ema20_lag5_raw"),
        pl.col("_ema50_raw").shift(10).over("ticker").alias("_ema50_lag10_raw"),
    )

    # ------------------------------------------------------------------ #
    # Stage E: v01 derived indicators
    # ------------------------------------------------------------------ #
    rsi_rs = pl.col("_avg_gain") / pl.col("_avg_loss")
    frame = frame.with_columns(
        pl.when(pl.col("bar_index") >= _MIN_BARS_RSI14)
        .then(
            pl.when(pl.col("_avg_loss") == 0)
            .then(100.0)
            .otherwise(100.0 - 100.0 / (1.0 + rsi_rs))
        )
        .otherwise(None)
        .alias("rsi14"),
        (pl.col("close_adj") / pl.col("_close_adj_lag20") - 1.0).alias("roc20"),
        # v03: raw input for rs_percentile_126d (cross-sectional rank, computed
        # per-cutoff-date over the active universe in _build_feature_rows --
        # null here whenever <126 bars of history exist, same null-on-insufficient-
        # history convention as every other lagged indicator in this module).
        (pl.col("close_adj") / pl.col("_close_adj_lag126") - 1.0).alias("roc126"),
        (pl.col("volume_raw") / pl.col("avg_volume_20d")).alias("rvol20"),
        (pl.col("close_adj") / pl.col("_high252") - 1.0).alias("distance_from_52w_high_pct"),
        (pl.col("close_adj") / pl.col("_high20") - 1.0).alias("pullback_from_recent_high_pct"),
    )

    frame = frame.with_columns(
        (pl.col("atr14") / pl.col("close_adj")).alias("atr_pct"),
        ((pl.col("close_adj") - pl.col("_high20")) / pl.col("atr14")).alias("breakout_proximity"),
        (pl.col("close_adj") / pl.col("ema20") - 1.0).alias("distance_to_ema20_pct"),
        (pl.col("close_adj") / pl.col("ema50") - 1.0).alias("distance_to_ema50_pct"),
        (pl.col("close_adj") / pl.col("ema200") - 1.0).alias("distance_to_ema200_pct"),
        pl.when(pl.col("ema20").is_null() | pl.col("ema50").is_null() | pl.col("ema200").is_null())
        .then(None)
        .when((pl.col("ema20") > pl.col("ema50")) & (pl.col("ema50") > pl.col("ema200")))
        .then(100.0)
        .when(pl.col("close_adj") > pl.col("ema200"))
        .then(50.0)
        .otherwise(0.0)
        .alias("ema_alignment_score"),
    )

    # Consolidation score (v01, retained)
    atr_contraction = 1.0 - (pl.col("atr14") / pl.col("_atr_mean60")).clip(upper_bound=1.0)
    range_contraction = 1.0 - (pl.col("_range_mean10") / pl.col("_range_mean60")).clip(upper_bound=1.0)
    volume_contraction = 1.0 - (pl.col("_vol_mean10") / pl.col("_vol_mean60")).clip(upper_bound=1.0)
    frame = frame.with_columns(
        (100.0 * (0.4 * atr_contraction + 0.4 * range_contraction + 0.2 * volume_contraction))
        .clip(lower_bound=0.0, upper_bound=100.0)
        .alias("consolidation_score"),
    )

    # ------------------------------------------------------------------ #
    # Stage F: v02 simple derived features (vectorised)
    # ------------------------------------------------------------------ #
    frame = frame.with_columns(
        # EMA slopes
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA20 + 5)
        .then(pl.col("ema20") / pl.col("_ema20_lag5_raw") - 1.0)
        .otherwise(None)
        .alias("ema20_slope"),
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA50 + 10)
        .then(pl.col("ema50") / pl.col("_ema50_lag10_raw") - 1.0)
        .otherwise(None)
        .alias("ema50_slope"),
        # ATR compression: score > 0 when current ATR < historical mean
        pl.when(pl.col("atr14").is_not_null() & pl.col("_atr_mean60").is_not_null())
        .then(
            (100.0 * (1.0 - (pl.col("atr14") / pl.col("_atr_mean60")).clip(upper_bound=1.0)))
            .clip(lower_bound=0.0, upper_bound=100.0)
        )
        .otherwise(None)
        .alias("atr_compression_score"),
        # Pullback depth: (max(high_adj, 20d) - close_adj) / max(high_adj, 20d)
        pl.when(pl.col("_high_adj_20").is_not_null() & (pl.col("_high_adj_20") > 0))
        .then((pl.col("_high_adj_20") - pl.col("close_adj")) / pl.col("_high_adj_20"))
        .otherwise(None)
        .alias("pullback_depth_pct"),
        # Volume dry-up: score > 0 when recent vol < 60-bar mean
        pl.when(
            pl.col("_vol_mean10").is_not_null()
            & pl.col("_vol_mean60").is_not_null()
            & (pl.col("_vol_mean60") > 0)
        )
        .then(
            (100.0 * (1.0 - (pl.col("_vol_mean10") / pl.col("_vol_mean60")).clip(upper_bound=1.0)))
            .clip(lower_bound=0.0, upper_bound=100.0)
        )
        .otherwise(None)
        .alias("volume_dry_up_score"),
        # Volume expansion
        pl.when(pl.col("rvol20").is_not_null())
        .then(
            (100.0 * ((pl.col("rvol20") - 1.0).clip(lower_bound=0.0) / 1.0).clip(upper_bound=1.0))
            .clip(lower_bound=0.0, upper_bound=100.0)
        )
        .otherwise(None)
        .alias("volume_expansion_score"),
    )

    return frame


__all__ = [
    "FeatureEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "REQUIRED_FEATURE_COLUMNS",
    "OPTIONAL_FEATURE_COLUMNS",
    "REQUIRED_MIN_BARS",
    "LOOKBACK_WARMUP_CALENDAR_DAYS",
    # Exposed for unit testing of structural helpers
    "_compute_swing_pivots",
    "_compute_support_resistance",
    "_compute_base",
    "_true_ranges",
]
