"""Module 11 — Feature Engine.

Computes ``daily_features`` rows from already-ingested, validated and
mutation-checked ``daily_prices`` data. It runs **after** Module 10
(Mutation Detector) and **before** Module 12 (Market Regime Engine).

Contract source of truth: ``M11_FEATURE_ENGINE_SPEC.md`` (which is itself
derived from the frozen split Project Files — ``01c_FORMULAS_AND_CONFIGS.md``
for formulas, ``01b_SCHEMA_AND_DATA.md`` for the ``daily_features`` schema,
``01d_MODULES_AND_PIPELINE.md`` for the pipeline position and
``02b_ARCHITECTURE_DECISIONS.md`` for the Polars-first / feature-cutoff /
raw-vs-adjusted / schema-version decisions).

Scope (what this module does)
-----------------------------
For an inclusive ``[start_date, end_date]`` range, for the selected tickers it:

- reads only ``daily_prices`` rows whose ``data_quality_status == 'ok'``
  (the data-quality boundary), including enough warmup history before
  ``start_date`` to satisfy the longest required lookback (252 trading days);
- anchors every ticker on its ``feature_cutoff_date`` — the latest eligible
  ``daily_prices.date`` within the requested range — and writes exactly one
  ``daily_features`` row per processed ticker at ``feature_date =
  feature_cutoff_date`` (never outside the requested range, never using a row
  after the cutoff: no look-ahead);
- computes the price indicators from **adjusted** prices and the volume
  features from **raw** volume, strictly per the frozen formulas, using Polars
  vectorised rolling / grouped / window expressions (no per-ticker Python
  indicator loops);
- sets ``feature_ready = TRUE`` only when every required indicator is non-null;
- upserts on ``(ticker, feature_date, feature_schema_version)`` (insert-or-
  update), refreshing ``calculated_at`` on every write, in a single
  transaction; and
- returns a :class:`~app.utils.service_result.ServiceResult`.

Out of scope / open gaps (owned elsewhere or undefined in frozen sources)
-------------------------------------------------------------------------
- ``market_regime`` is left ``NULL``: the frozen sources define VIX risk
  thresholds and a regime priority but **no** explicit inline bull / bear /
  neutral classification formula, and the regime is owned by Module 12. Open
  gap ``G-REGIME`` (see spec). It is an optional column and does not block
  readiness.
- ``days_to_earnings_bd`` / ``earnings_confidence`` / ``macro_event_risk_flag``
  fall back to ``NULL`` / ``NULL`` / ``FALSE``: no feature-engine population
  rule for these is defined in the frozen sources and no strategy config is
  passed to this module's API. Open gaps ``G-EARN`` / ``G-MACRO`` (see spec).
  They are optional columns and do not block readiness.

This module never calls providers, never imports ``duckdb`` directly, never
uses ``ATTACH`` / DDL / schema changes, never writes ``daily_prices`` or any
ticker / universe / sector / repair / rebuild / simulation / step / proposal /
outcome / AI / execution table, and never uses ``print()``.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta
from typing import Any, Final, Protocol

import polars as pl

from app.config import constants
from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Roles this module is allowed to target.
# --------------------------------------------------------------------------- #
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module
# 11 never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are
# accepted; any other value yields a ``failed`` result with no reads/writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# data_quality_status value that gates eligibility (SCHEMA_SPEC.md §5). Module
# 11 consumes only rows Module 09 left/escalated as ``ok``.
STATUS_OK: Final[str] = "ok"

# Warmup: how many calendar days before ``start_date`` to read so the longest
# required lookback (the 252-trading-day 52-week-high window) is always fully
# covered. 252 trading days is ~353 calendar days; 420 gives a comfortable
# holiday/closure buffer. See spec assumption A-WARMUP.
LOOKBACK_WARMUP_CALENDAR_DAYS: Final[int] = 420

# Minimum number of trading bars (rows up to and including the cutoff) required
# for the longest required lookback (52-week high). Readiness is enforced
# structurally by per-indicator ``min_samples`` windows; this constant is the
# binding lower bound and is documented for clarity / tests. See spec.
REQUIRED_MIN_BARS: Final[int] = 252

# Per-indicator minimum bars for the recursive (EWM-based) indicators, used to
# null out immature values that an EWM would otherwise emit from the first bar.
# Rolling / shift indicators are gated by their own ``min_samples`` instead.
_MIN_BARS_EMA20: Final[int] = 20
_MIN_BARS_EMA50: Final[int] = 50
_MIN_BARS_EMA200: Final[int] = 200
_MIN_BARS_RSI14: Final[int] = 15  # 14 deltas + 1 seed row
_MIN_BARS_ATR14: Final[int] = 15  # 14 true ranges + 1 prior-close row

# Wilder smoothing factor for RSI14 / ATR14 (recursive EWM, adjust=False).
_WILDER_ALPHA_14: Final[float] = 1.0 / 14.0

# Required indicator columns: ``feature_ready`` is TRUE only when all are
# non-null (frozen Module 11 readiness list).
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

# Optional / context columns that never block readiness.
OPTIONAL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "distance_to_ema20_pct",
    "distance_to_ema50_pct",
    "distance_to_ema200_pct",
    "sector_relative_strength",
    "market_regime",
    "days_to_earnings_bd",
    "earnings_confidence",
    "macro_event_risk_flag",
)

# The exact metadata key set returned on every return path.
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

# Full ordered column list for ``daily_features`` (SCHEMA_SPEC.md §3 /
# ``01b_SCHEMA_AND_DATA.md``). ``calculated_at`` is set via SQL ``now()`` and is
# not parameterised. NOTE: the frozen ``daily_features`` schema has **no**
# ``created_at`` column — only ``calculated_at`` — so there is nothing to
# preserve on conflict (resolved conflict R-CREATED_AT in the spec).
_FEATURE_PARAM_COLUMNS: Final[tuple[str, ...]] = (
    "ticker",
    "feature_date",
    "feature_cutoff_date",
    "feature_schema_version",
    "feature_ready",
    "ema20",
    "ema50",
    "ema200",
    "ema_alignment_score",
    "distance_to_ema20_pct",
    "distance_to_ema50_pct",
    "distance_to_ema200_pct",
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
    "sector_relative_strength",
    "market_regime",
    "days_to_earnings_bd",
    "earnings_confidence",
    "macro_event_risk_flag",
)

# Non-key columns updated on conflict (everything except the three key columns,
# plus the SQL-set ``calculated_at``).
_KEY_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ticker", "feature_date", "feature_schema_version"}
)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (operates only on existing tables; no DDL).
# --------------------------------------------------------------------------- #
_SELECT_DISTINCT_ELIGIBLE_TICKERS: Final[str] = (
    "SELECT DISTINCT ticker FROM daily_prices "
    "WHERE date >= ? AND date <= ? AND data_quality_status = ? "
    "ORDER BY ticker"
)

_SELECT_PRICE_COLUMNS: Final[str] = (
    "SELECT ticker, date, close_raw, close_adj, high_adj, low_adj, volume_raw "
    "FROM daily_prices "
    "WHERE data_quality_status = ? AND date >= ? AND date <= ? "
    "AND ticker IN ({placeholders}) "
    "ORDER BY ticker, date"
)

_SELECT_SECTORS: Final[str] = (
    "SELECT ticker, sector FROM ticker_master WHERE ticker IN ({placeholders})"
)

# Existence probe used to classify inserts vs conflict-updates for the run's
# rows (run inside the write transaction, before the upserts).
_SELECT_EXISTING_KEYS: Final[str] = (
    "SELECT ticker, feature_date FROM daily_features "
    "WHERE feature_schema_version = ? AND ticker IN ({placeholders})"
)


def _build_upsert_sql() -> str:
    """Build the ``INSERT ... ON CONFLICT DO UPDATE`` statement for one row.

    ``calculated_at`` is the only column not parameterised; it is set with the
    DB clock on both insert and conflict-update so reruns refresh it. The key
    columns are excluded from the ``DO UPDATE SET`` clause.
    """
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
# Polars compute helpers.
# --------------------------------------------------------------------------- #
def _sanitize(value: Any) -> Any:
    """Map NaN / +-inf floats to ``None`` so they never reach the DB or skew
    readiness (an infinite value is not a valid indicator and must not count as
    "available").
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _compute_features(prices: pl.DataFrame) -> pl.DataFrame:
    """Return a per-(ticker, date) feature frame computed from ``prices``.

    ``prices`` must contain the columns selected by :data:`_SELECT_PRICE_COLUMNS`
    for **all** loaded tickers (target tickers plus any sector ETFs) and be
    pre-sorted by ``(ticker, date)``. All indicators are vectorised per ticker
    via Polars window (``.over("ticker")``) / rolling expressions — no
    per-ticker Python loops. Immature recursive (EWM) values are nulled below
    their minimum-bar count; rolling / shift indicators are gated by
    ``min_samples`` (so insufficient history naturally yields ``NULL``).
    """
    ticker = pl.col("ticker")

    # Stage A: primitives that other expressions depend on (per-ticker).
    frame = prices.with_columns(
        pl.col("date").cum_count().over("ticker").alias("bar_index"),
        pl.col("close_adj").shift(1).over("ticker").alias("_prev_close_adj"),
        pl.col("close_adj").diff().over("ticker").alias("_delta"),
        pl.col("close_adj").shift(20).over("ticker").alias("_close_adj_lag20"),
        (pl.col("close_raw") * pl.col("volume_raw")).alias("_dollar"),
        (pl.col("high_adj") - pl.col("low_adj")).alias("_range_hl"),
    )

    # Stage B: EMAs (close_adj), Wilder gain/loss inputs, true range.
    frame = frame.with_columns(
        pl.col("close_adj").ewm_mean(span=20, adjust=False).over("ticker").alias("_ema20_raw"),
        pl.col("close_adj").ewm_mean(span=50, adjust=False).over("ticker").alias("_ema50_raw"),
        pl.col("close_adj").ewm_mean(span=200, adjust=False).over("ticker").alias("_ema200_raw"),
        pl.when(pl.col("_delta") > 0)
        .then(pl.col("_delta"))
        .otherwise(0.0)
        .fill_null(0.0)
        .alias("_gain"),
        pl.when(pl.col("_delta") < 0)
        .then(-pl.col("_delta"))
        .otherwise(0.0)
        .fill_null(0.0)
        .alias("_loss"),
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
    )

    # Stage C: Wilder averages, ATR14 (masked), prior-20 volume means.
    frame = frame.with_columns(
        pl.col("_gain").ewm_mean(alpha=_WILDER_ALPHA_14, adjust=False).over("ticker").alias("_avg_gain"),
        pl.col("_loss").ewm_mean(alpha=_WILDER_ALPHA_14, adjust=False).over("ticker").alias("_avg_loss"),
        pl.when(pl.col("bar_index") >= _MIN_BARS_ATR14)
        .then(pl.col("_tr").ewm_mean(alpha=_WILDER_ALPHA_14, adjust=False).over("ticker"))
        .otherwise(None)
        .alias("atr14"),
        # avg_*_20d use the prior 20 trading days (t-20..t-1), matching the
        # RVOL20 denominator definition in the frozen formulas.
        pl.col("_vol_lag1").rolling_mean(window_size=20, min_samples=20).over("ticker").alias("avg_volume_20d"),
        pl.col("_dollar_lag1").rolling_mean(window_size=20, min_samples=20).over("ticker").alias("avg_dollar_volume_20d"),
    )

    # Stage D: masked EMAs and the ATR14 60-day mean for consolidation.
    frame = frame.with_columns(
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA20).then(pl.col("_ema20_raw")).otherwise(None).alias("ema20"),
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA50).then(pl.col("_ema50_raw")).otherwise(None).alias("ema50"),
        pl.when(pl.col("bar_index") >= _MIN_BARS_EMA200).then(pl.col("_ema200_raw")).otherwise(None).alias("ema200"),
        pl.col("atr14").rolling_mean(window_size=60, min_samples=60).over("ticker").alias("_atr_mean60"),
    )

    # Stage E: derived indicators built on the columns above.
    rsi_rs = pl.col("_avg_gain") / pl.col("_avg_loss")
    frame = frame.with_columns(
        # RSI14 (Wilder via recursive EWM). avg_loss == 0 -> RSI 100.
        pl.when(pl.col("bar_index") >= _MIN_BARS_RSI14)
        .then(
            pl.when(pl.col("_avg_loss") == 0)
            .then(100.0)
            .otherwise(100.0 - 100.0 / (1.0 + rsi_rs))
        )
        .otherwise(None)
        .alias("rsi14"),
        (pl.col("close_adj") / pl.col("_close_adj_lag20") - 1.0).alias("roc20"),
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
        # EMA alignment score (null if any EMA is null).
        pl.when(pl.col("ema20").is_null() | pl.col("ema50").is_null() | pl.col("ema200").is_null())
        .then(None)
        .when((pl.col("ema20") > pl.col("ema50")) & (pl.col("ema50") > pl.col("ema200")))
        .then(100.0)
        .when(pl.col("close_adj") > pl.col("ema200"))
        .then(50.0)
        .otherwise(0.0)
        .alias("ema_alignment_score"),
    )

    # Consolidation score: three contraction terms, each clipped to <= 1.
    atr_contraction = 1.0 - (pl.col("atr14") / pl.col("_atr_mean60")).clip(upper_bound=1.0)
    range_contraction = 1.0 - (pl.col("_range_mean10") / pl.col("_range_mean60")).clip(upper_bound=1.0)
    volume_contraction = 1.0 - (pl.col("_vol_mean10") / pl.col("_vol_mean60")).clip(upper_bound=1.0)
    frame = frame.with_columns(
        (
            100.0
            * (0.4 * atr_contraction + 0.4 * range_contraction + 0.2 * volume_contraction)
        )
        .clip(lower_bound=0.0, upper_bound=100.0)
        .alias("consolidation_score"),
    )

    return frame


# --------------------------------------------------------------------------- #
# Feature engine.
# --------------------------------------------------------------------------- #
class FeatureEngine:
    """Compute and upsert ``daily_features`` rows for a date range.

    The engine is effectively stateless; the optional ``db_manager`` constructor
    argument exists only so tests can inject a fake/wrapping manager. When it is
    ``None`` the real :mod:`app.database.duckdb_manager` is used, which is the
    single approved DB entry point (no arbitrary paths, no ``ATTACH``).
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )

    # ------------------------------------------------------------------ #
    # Public API (EXACT signature — do not vary).
    # ------------------------------------------------------------------ #
    def calculate(
        self,
        start_date: date,
        end_date: date,
        tickers: list[str] | None = None,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Compute ``daily_features`` for ``[start_date, end_date]``.

        Parameters
        ----------
        start_date, end_date:
            Inclusive range applied to ``daily_prices.date`` for the cutoff.
        tickers:
            ``None`` processes all distinct tickers with eligible rows in range;
            an explicit list processes only those (requested tickers without
            eligible rows are counted as skipped).
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns ``failed`` before any DB read/write.
        run_id:
            A fresh ``uuid4`` is minted when ``None``.

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``metadata["tickers_processed"]`` on every
            return path. ``metadata`` carries exactly :data:`METADATA_KEYS`.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()
        tickers_requested = 0 if tickers is None else len(dict.fromkeys(tickers))

        log.info(
            "calculate start db_role=%s start_date=%s end_date=%s tickers=%s",
            db_role,
            start_iso,
            end_iso,
            "all" if tickers is None else len(tickers),
        )

        # --- db_role guard: prod/debug only, never simulation. No I/O. ----- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 11 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("calculate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso, tickers_requested)

        # --- date-range guard: fail before any DB access. ------------------ #
        if start_date > end_date:
            message = f"Invalid date range: start_date {start_iso} > end_date {end_iso}."
            log.error("calculate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso, tickers_requested)

        # --- read phase (read-only): selection, sectors, price rows. ------- #
        warmup_start = start_date - timedelta(days=LOOKBACK_WARMUP_CALENDAR_DAYS)
        try:
            read = self._read(db_role, start_date, end_date, warmup_start, tickers)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("calculate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso, tickers_requested)

        process_tickers = read["process_tickers"]
        tickers_skipped = read["tickers_skipped_no_data"]
        rows_read = read["rows_read"]

        # --- compute phase (pure Polars, no DB): build feature rows. ------- #
        feature_rows = self._build_feature_rows(
            read["prices"],
            process_tickers,
            read["sector_by_ticker"],
            start_date,
            end_date,
        )
        tickers_processed = len(feature_rows)
        ready_count = sum(1 for r in feature_rows if r["feature_ready"])
        not_ready_count = tickers_processed - ready_count

        log.info(
            "calculate computed rows_read=%d tickers_processed=%d "
            "tickers_skipped_no_data=%d ready=%d not_ready=%d",
            rows_read,
            tickers_processed,
            tickers_skipped,
            ready_count,
            not_ready_count,
        )

        # --- write phase: single transaction across all upserts. ----------- #
        try:
            written, updated = self._write(db_role, feature_rows)
        except Exception as exc:  # noqa: BLE001 - surface as failed ServiceResult
            log.error(
                "calculate failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            # Durable write counts are 0 (rolled back); read/compute counts kept.
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
            rows_read,
            tickers_processed,
            written,
            updated,
            ready_count,
            not_ready_count,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=tickers_processed,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Read phase.
    # ------------------------------------------------------------------ #
    def _read(
        self,
        db_role: str,
        start_date: date,
        end_date: date,
        warmup_start: date,
        tickers: list[str] | None,
    ) -> dict[str, Any]:
        """Resolve selection, sectors and price rows in one read-only pass.

        Returns the process-ticker set, the skipped-no-data count, the sector
        map, the loaded price :class:`polars.DataFrame`, and ``rows_read``.
        The read connection is closed before any computation.
        """
        connection = self._db.connect(db_role, read_only=True)
        try:
            # Distinct eligible tickers in range (the authoritative "has data" set).
            eligible_rows = connection.execute(
                _SELECT_DISTINCT_ELIGIBLE_TICKERS,
                [start_date, end_date, STATUS_OK],
            ).fetchall()
            eligible_in_range = {row[0] for row in eligible_rows}

            if tickers is None:
                process_tickers = sorted(eligible_in_range)
                tickers_skipped = 0
            else:
                # Preserve uniqueness; only tickers with eligible in-range rows
                # are processed, the rest are skipped-no-data.
                requested_unique = list(dict.fromkeys(tickers))
                process_tickers = [t for t in requested_unique if t in eligible_in_range]
                tickers_skipped = sum(
                    1 for t in requested_unique if t not in eligible_in_range
                )

            if not process_tickers:
                empty = pl.DataFrame(
                    schema={
                        "ticker": pl.Utf8,
                        "date": pl.Date,
                        "close_raw": pl.Float64,
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
                    "prices": empty,
                    "rows_read": 0,
                }

            # Sectors for the process tickers -> mapped sector ETFs (for sector
            # relative strength). Missing tickers / unmapped sectors yield NULL.
            sector_by_ticker = self._read_sectors(connection, process_tickers)
            sector_etfs = {
                constants.SECTOR_ETF_MAP[sector]
                for sector in sector_by_ticker.values()
                if sector in constants.SECTOR_ETF_MAP
            }

            load_set = sorted(set(process_tickers) | sector_etfs)
            placeholders = ", ".join("?" for _ in load_set)
            sql = _SELECT_PRICE_COLUMNS.format(placeholders=placeholders)
            params = [STATUS_OK, warmup_start, end_date, *load_set]
            price_rows = connection.execute(sql, params).fetchall()
        finally:
            connection.close()

        prices = pl.DataFrame(
            price_rows,
            schema=[
                ("ticker", pl.Utf8),
                ("date", pl.Date),
                ("close_raw", pl.Float64),
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
            "prices": prices,
            "rows_read": prices.height,
        }

    def _read_sectors(
        self, connection: Any, process_tickers: list[str]
    ) -> dict[str, str | None]:
        """Return ``{ticker: sector}`` for process tickers present in
        ``ticker_master`` (tickers absent from the table are simply omitted)."""
        placeholders = ", ".join("?" for _ in process_tickers)
        sql = _SELECT_SECTORS.format(placeholders=placeholders)
        rows = connection.execute(sql, list(process_tickers)).fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------ #
    # Compute phase.
    # ------------------------------------------------------------------ #
    def _build_feature_rows(
        self,
        prices: pl.DataFrame,
        process_tickers: list[str],
        sector_by_ticker: dict[str, str | None],
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """Return one feature-row dict per processed ticker (at its cutoff).

        The full per-(ticker, date) indicator frame is computed once with
        Polars; per ticker the row at ``feature_cutoff_date`` (the latest
        in-range eligible date) is selected. Sector relative strength is
        resolved against the mapped ETF's 20-day return at the same cutoff date.
        """
        if prices.height == 0 or not process_tickers:
            return []

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

        # roc20 lookup keyed by (ticker, date) — reused for both the ticker's
        # own 20d return and each mapped sector ETF's 20d return.
        roc_lookup: dict[tuple[str, date], float | None] = {}
        for rec in features.select(["ticker", "date", "roc20"]).iter_rows(named=True):
            roc_lookup[(rec["ticker"], rec["date"])] = _sanitize(rec["roc20"])

        process_set = set(process_tickers)
        cutoff_frame = features.filter(
            pl.col("ticker").is_in(list(process_set))
            & pl.col("_cutoff_date").is_not_null()
            & (pl.col("date") == pl.col("_cutoff_date"))
        )

        out_columns = [
            "ticker",
            "date",
            *REQUIRED_FEATURE_COLUMNS,
            "distance_to_ema20_pct",
            "distance_to_ema50_pct",
            "distance_to_ema200_pct",
        ]
        rows: list[dict[str, Any]] = []
        for rec in cutoff_frame.select(out_columns).iter_rows(named=True):
            ticker = rec["ticker"]
            cutoff = rec["date"]

            sector = sector_by_ticker.get(ticker)
            etf = constants.SECTOR_ETF_MAP.get(sector) if sector is not None else None
            sector_rs: float | None = None
            ticker_roc = _sanitize(rec["roc20"])
            if etf is not None and ticker_roc is not None:
                etf_roc = roc_lookup.get((etf, cutoff))
                if etf_roc is not None:
                    sector_rs = ticker_roc - etf_roc

            row = self._assemble_row(ticker, cutoff, rec, sector_rs)
            rows.append(row)

        rows.sort(key=lambda r: r["ticker"])
        return rows

    def _assemble_row(
        self,
        ticker: str,
        cutoff: date,
        rec: dict[str, Any],
        sector_rs: float | None,
    ) -> dict[str, Any]:
        """Assemble a single ``daily_features`` row dict (sanitised, with
        readiness computed and open-gap columns at their documented defaults)."""
        required = {col: _sanitize(rec[col]) for col in REQUIRED_FEATURE_COLUMNS}
        optional_emas = {
            "distance_to_ema20_pct": _sanitize(rec["distance_to_ema20_pct"]),
            "distance_to_ema50_pct": _sanitize(rec["distance_to_ema50_pct"]),
            "distance_to_ema200_pct": _sanitize(rec["distance_to_ema200_pct"]),
        }
        feature_ready = all(required[col] is not None for col in REQUIRED_FEATURE_COLUMNS)

        return {
            "ticker": ticker,
            "feature_date": cutoff,
            "feature_cutoff_date": cutoff,
            "feature_schema_version": constants.FEATURE_SCHEMA_VERSION,
            "feature_ready": feature_ready,
            **required,
            **optional_emas,
            "sector_relative_strength": _sanitize(sector_rs),
            # Open gaps (documented in the spec): NULL / NULL / FALSE defaults.
            "market_regime": None,
            "days_to_earnings_bd": None,
            "earnings_confidence": None,
            "macro_event_risk_flag": False,
        }

    # ------------------------------------------------------------------ #
    # Write phase.
    # ------------------------------------------------------------------ #
    def _write(
        self, db_role: str, feature_rows: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Upsert every feature row in a single transaction; return
        ``(written, updated)``.

        Inserts vs conflict-updates are classified up front by probing the
        existing ``(ticker, feature_date)`` keys for this feature schema version
        among the rows about to be written, so reruns are stable and the two
        counts are accurate. Any error triggers ``ROLLBACK`` so no partial
        Module 11 writes survive. An empty plan opens and commits with no rows.
        """
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
        """Return the subset of ``(ticker, feature_date)`` keys already present
        in ``daily_features`` for this feature schema version."""
        tickers = sorted({row["ticker"] for row in feature_rows})
        placeholders = ", ".join("?" for _ in tickers)
        sql = _SELECT_EXISTING_KEYS.format(placeholders=placeholders)
        params = [constants.FEATURE_SCHEMA_VERSION, *tickers]
        rows = connection.execute(sql, params).fetchall()
        return {(row[0], row[1]) for row in rows}

    # ------------------------------------------------------------------ #
    # Result builders.
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
        """Build a ``failed`` result for a pre-DB guard (no I/O performed)."""
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
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
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


__all__ = [
    "FeatureEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "REQUIRED_FEATURE_COLUMNS",
    "OPTIONAL_FEATURE_COLUMNS",
    "REQUIRED_MIN_BARS",
    "LOOKBACK_WARMUP_CALENDAR_DAYS",
]
