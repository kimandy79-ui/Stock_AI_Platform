"""Module 12 — Market Regime Engine.

Classifies one market-wide ``market_regime`` value per requested calendar date
from ``SPY`` / ``QQQ`` / ``^VIX`` price history and writes it back onto the
existing ``daily_features`` rows for the date / current feature schema version.
It runs **after** Module 11 (Feature Engine) and **before** Module 13 (Step 3
Screening).

Contract source of truth: ``M12_MARKET_REGIME_ENGINE_SPEC.md`` (derived from the
frozen split Project Files — ``01a_CORE_PRINCIPLES.md`` for the regime enum /
guardrails, ``01b_SCHEMA_AND_DATA.md`` for the ``daily_prices`` /
``daily_features`` schema, ``01c_FORMULAS_AND_CONFIGS.md`` for the VIX
thresholds, ``01d_MODULES_AND_PIPELINE.md`` for the pipeline position and
``02b_ARCHITECTURE_DECISIONS.md`` §22.2 / §22.7 / §22.10 for Polars-first /
no-look-ahead / SPY-QQQ-VIX regime). The bull/bear/neutral trend rule that the
frozen sources leave open (gap G-REGIME) is closed in §3 of the spec.

Scope
-----
For an inclusive ``[start_date, end_date]`` calendar range it:

- reads only ``daily_prices`` rows whose ``data_quality_status == 'ok'`` for
  ``SPY`` / ``QQQ`` / ``^VIX``, plus enough warmup history before ``start_date``
  to satisfy the EMA200 lookback;
- computes EMA200 per symbol from ``coalesce(close_adj, close_raw)`` with Polars
  (``ewm_mean(span=200, adjust=False)``, masked below 200 bars), matching the
  Module 11 EMA behavior, then as-of aligns each symbol backward onto every
  requested calendar date (no look-ahead);
- classifies each date by consuming ``constants.MARKET_REGIME_PRIORITY``
  top-down (first matching predicate wins) using the VIX gates and the SPY/QQQ
  trend rule; and
- updates, in a single transaction, every existing ``daily_features`` row for
  each classified date / current ``FEATURE_SCHEMA_VERSION`` (setting only
  ``market_regime`` and ``calculated_at``), and returns a
  :class:`~app.utils.service_result.ServiceResult`.

This module never inserts ``daily_features`` rows, never writes ``daily_prices``
or any other table, never calls providers, never imports ``duckdb`` directly,
never uses ``ATTACH`` / DDL / schema changes, and never uses ``print()``.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Callable, Final, Protocol

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
# 12 never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are
# accepted; any other value yields a ``failed`` result with no reads/writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# data_quality_status value that gates eligibility (SCHEMA_SPEC.md §5). Module
# 12 consumes only rows Module 09 left/escalated as ``ok``.
STATUS_OK: Final[str] = "ok"

# Symbols that drive the market regime (constants / 02b §22.10).
REGIME_SYMBOLS: Final[tuple[str, ...]] = (
    constants.BENCHMARK_SPY,
    constants.BENCHMARK_QQQ,
    constants.BENCHMARK_VIX,
)

# Warmup: calendar days before ``start_date`` to read so EMA200 always has
# >= 200 eligible bars by ``start_date`` (~228 trading days of buffer). Defined
# here (not in the frozen Module 01 ``constants.py``) per spec assumption
# A-WARMUP; prompt-suggested value.
LOOKBACK_WARMUP_CALENDAR_DAYS: Final[int] = 320

# Minimum eligible bars for a non-null EMA200 (mirrors Module 11).
_MIN_BARS_EMA200: Final[int] = 200

# Regime vocabulary (frozen enum, constants).
REGIME_EXTREME_RISK: Final[str] = constants.REGIME_EXTREME_RISK
REGIME_HIGH_RISK: Final[str] = constants.REGIME_HIGH_RISK
REGIME_BEAR: Final[str] = constants.REGIME_BEAR
REGIME_BULL: Final[str] = constants.REGIME_BULL
REGIME_NEUTRAL: Final[str] = constants.REGIME_NEUTRAL

# Known regimes that this engine has a classifier predicate for. Any value in
# ``MARKET_REGIME_PRIORITY`` outside this set fails the priority guard.
SUPPORTED_REGIMES: Final[frozenset[str]] = frozenset(
    {
        REGIME_EXTREME_RISK,
        REGIME_HIGH_RISK,
        REGIME_BEAR,
        REGIME_BULL,
        REGIME_NEUTRAL,
    }
)

# Canonical ordered tuple of all five supported regimes. Used to initialise
# ``regimes_by_value`` in metadata so the dict shape is stable and independent
# of the ordering or contents of ``MARKET_REGIME_PRIORITY``. If PRIORITY is ever
# extended, duplicated, or has a value removed, the metadata shape remains fixed
# to exactly these five keys. The priority guard enforces that PRIORITY also
# matches this set before any computation begins.
CANONICAL_REGIMES: Final[tuple[str, ...]] = (
    REGIME_EXTREME_RISK,
    REGIME_HIGH_RISK,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_NEUTRAL,
)


def _empty_regime_counts() -> dict[str, int]:
    """Return ``{regime: 0}`` for every canonical regime key.

    Used to initialise ``regimes_by_value`` so the metadata shape is always the
    same fixed five-key dict regardless of how ``MARKET_REGIME_PRIORITY`` changes.
    """
    return {regime: 0 for regime in CANONICAL_REGIMES}


# The exact metadata key set returned on every return path.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "start_date",
    "end_date",
    "dates_requested",
    "dates_classified",
    "dates_skipped_insufficient_data",
    "rows_read",
    "feature_rows_updated",
    "regimes_by_value",
)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (operates only on existing tables; no DDL).
# --------------------------------------------------------------------------- #
_SELECT_REGIME_PRICES: Final[str] = (
    "SELECT ticker, date, close_raw, close_adj "
    "FROM daily_prices "
    "WHERE data_quality_status = ? AND date >= ? AND date <= ? "
    "AND ticker IN ({placeholders}) "
    "ORDER BY ticker, date"
)

# Count of existing daily_features rows for a date / schema version. Run inside
# the write transaction immediately before the matching UPDATE; the count equals
# the number of rows the UPDATE touches (identical WHERE).
_COUNT_FEATURE_ROWS_FOR_DATE: Final[str] = (
    "SELECT COUNT(*) FROM daily_features "
    "WHERE feature_date = ? AND feature_schema_version = ?"
)

# Required UPDATE shape: only market_regime + calculated_at change; no ticker
# filter (regime is market-wide); current feature schema version only.
_UPDATE_FEATURE_REGIME: Final[str] = (
    "UPDATE daily_features "
    "SET market_regime = ?, calculated_at = CAST(now() AS TIMESTAMP) "
    "WHERE feature_date = ? AND feature_schema_version = ?"
)


# --------------------------------------------------------------------------- #
# Compute helpers.
# --------------------------------------------------------------------------- #
def _calendar_dates(start_date: date, end_date: date) -> list[date]:
    """Return every calendar day in the inclusive ``[start_date, end_date]``."""
    span = (end_date - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(span + 1)]


def _compute_symbol_frames(prices: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Return ``{symbol: frame}`` with per-symbol ``date`` / ``close_used`` /
    ``ema200`` columns, sorted by date.

    ``close_used = coalesce(close_adj, close_raw)`` (for ``^VIX`` the adjusted
    close is normally NULL, so it falls back to the raw close — spec A-VIX-RAW).
    EMA200 is the standard recursive EMA (``span=200, adjust=False``) per symbol,
    masked to NULL below :data:`_MIN_BARS_EMA200` bars, matching Module 11.

    Computation is done per-symbol (filter → sort → compute) rather than via
    Polars ``.over("ticker")`` window expressions because ``ewm_mean`` and
    ``cum_count`` are not guaranteed to support window context in Polars 1.x.
    With only three regime symbols this is efficient and correct by construction.
    """
    if prices.height == 0:
        return {}

    out: dict[str, pl.DataFrame] = {}
    for symbol in REGIME_SYMBOLS:
        symbol_frame = (
            prices.filter(pl.col("ticker") == symbol)
            .sort("date")
        )
        if symbol_frame.height == 0:
            continue

        symbol_frame = symbol_frame.with_columns(
            pl.coalesce([pl.col("close_adj"), pl.col("close_raw")]).alias("close_used")
        )
        symbol_frame = symbol_frame.with_columns(
            pl.col("close_used")
            .ewm_mean(span=200, adjust=False)
            .alias("_ema200_raw")
        )
        # Bar index: 1-based count of rows after sort (always non-null after sort).
        n_rows = symbol_frame.height
        bar_index = pl.Series("_bar_index", list(range(1, n_rows + 1)))
        symbol_frame = symbol_frame.with_columns(bar_index)
        symbol_frame = symbol_frame.with_columns(
            pl.when(pl.col("_bar_index") >= _MIN_BARS_EMA200)
            .then(pl.col("_ema200_raw"))
            .otherwise(None)
            .alias("ema200")
        )
        out[symbol] = symbol_frame.select(["date", "close_used", "ema200"])

    return out


def _asof_align(
    symbol_frames: dict[str, pl.DataFrame], dates: list[date]
) -> pl.DataFrame:
    """Backward as-of align each symbol onto ``dates``.

    Returns one row per requested date with ``date`` plus per-symbol
    ``spy_close`` / ``spy_ema200`` / ``qqq_close`` / ``qqq_ema200`` / ``vix_close``
    columns, each carrying the latest eligible value with ``daily_prices.date <=
    date`` (no look-ahead; weekends / non-trading dates resolve to the prior
    eligible bar). Symbols with no frame contribute NULL columns.
    """
    base = pl.DataFrame({"date": dates}).with_columns(pl.col("date").cast(pl.Date)).sort("date")

    column_map = {
        constants.BENCHMARK_SPY: ("spy_close", "spy_ema200"),
        constants.BENCHMARK_QQQ: ("qqq_close", "qqq_ema200"),
        constants.BENCHMARK_VIX: ("vix_close", None),
    }

    for symbol, (close_alias, ema_alias) in column_map.items():
        frame = symbol_frames.get(symbol)
        if frame is None:
            base = base.with_columns(pl.lit(None, dtype=pl.Float64).alias(close_alias))
            if ema_alias is not None:
                base = base.with_columns(pl.lit(None, dtype=pl.Float64).alias(ema_alias))
            continue

        select_cols = ["date", pl.col("close_used").alias(close_alias)]
        if ema_alias is not None:
            select_cols.append(pl.col("ema200").alias(ema_alias))
        renamed = frame.select(select_cols).sort("date")
        base = base.join_asof(renamed, on="date", strategy="backward")

    return base


def _build_predicates(
    row: dict[str, Any]
) -> dict[str, Callable[[], bool]]:
    """Return ``{regime: predicate}`` for one as-of-aligned date row.

    Predicates are evaluated lazily in priority order by the caller; ``neutral``
    is always the guaranteed fallback (``lambda: True``). The VIX gates only fire
    when a VIX close exists for the date; the trend predicates only fire when the
    relevant EMA200 is available.
    """
    spy_close = row.get("spy_close")
    spy_ema200 = row.get("spy_ema200")
    qqq_close = row.get("qqq_close")
    qqq_ema200 = row.get("qqq_ema200")
    vix_close = row.get("vix_close")

    def extreme() -> bool:
        return vix_close is not None and vix_close >= constants.VIX_EXTREME_RISK_THRESHOLD

    def high() -> bool:
        return vix_close is not None and vix_close >= constants.VIX_HIGH_RISK_THRESHOLD

    def bear() -> bool:
        return (
            spy_ema200 is not None
            and spy_close is not None
            and spy_close < spy_ema200
            and qqq_ema200 is not None
            and qqq_close is not None
            and qqq_close < qqq_ema200
        )

    def bull() -> bool:
        return (
            spy_ema200 is not None
            and spy_close is not None
            and spy_close > spy_ema200
        )

    return {
        REGIME_EXTREME_RISK: extreme,
        REGIME_HIGH_RISK: high,
        REGIME_BEAR: bear,
        REGIME_BULL: bull,
        REGIME_NEUTRAL: lambda: True,
    }


# --------------------------------------------------------------------------- #
# Market regime engine.
# --------------------------------------------------------------------------- #
class MarketRegimeEngine:
    """Classify and persist the market regime for a calendar date range.

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
    def classify(
        self,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Classify the market regime for ``[start_date, end_date]`` and write it.

        Parameters
        ----------
        start_date, end_date:
            Inclusive calendar range. One regime is classified per calendar day.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns ``failed`` before any DB read/write.
        run_id:
            A fresh ``uuid4`` is minted when ``None``.

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``metadata["dates_classified"]`` on every
            return path. ``metadata`` carries exactly :data:`METADATA_KEYS`.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()

        log.info(
            "classify start db_role=%s start_date=%s end_date=%s",
            db_role,
            start_iso,
            end_iso,
        )

        # --- db_role guard: prod/debug only, never simulation. No I/O. ----- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 12 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("classify failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- date-range guard: fail before any DB access. ------------------ #
        if start_date > end_date:
            message = f"Invalid date range: start_date {start_iso} > end_date {end_iso}."
            log.error("classify failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- priority guard: no unknown values, no duplicates, exact set. ---- #
        priority = constants.MARKET_REGIME_PRIORITY

        unknown = [r for r in priority if r not in SUPPORTED_REGIMES]
        if unknown:
            message = (
                f"Unsupported regime value(s) in MARKET_REGIME_PRIORITY: {unknown}. "
                f"Supported: {sorted(SUPPORTED_REGIMES)}."
            )
            log.error("classify failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        if len(priority) != len(set(priority)):
            message = (
                f"Duplicate regime value(s) in MARKET_REGIME_PRIORITY: {list(priority)}."
            )
            log.error("classify failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        if set(priority) != set(CANONICAL_REGIMES):
            message = (
                "MARKET_REGIME_PRIORITY must contain exactly the supported regimes. "
                f"Got {list(priority)}; expected {sorted(CANONICAL_REGIMES)}."
            )
            log.error("classify failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- read phase (read-only): eligible SPY/QQQ/^VIX price rows. ----- #
        warmup_start = start_date - timedelta(days=LOOKBACK_WARMUP_CALENDAR_DAYS)
        try:
            prices = self._read(db_role, warmup_start, end_date)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("classify failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        rows_read = prices.height

        # --- compute phase (pure Polars, no DB): classify each date. ------- #
        requested_dates = _calendar_dates(start_date, end_date)
        dates_requested = len(requested_dates)

        symbol_frames = _compute_symbol_frames(prices)
        aligned = _asof_align(symbol_frames, requested_dates)

        classifications: list[tuple[date, str]] = []
        dates_skipped = 0
        regimes_by_value: dict[str, int] = _empty_regime_counts()
        missing_spy_ema_dates = 0

        for record in aligned.iter_rows(named=True):
            current = record["date"]
            spy_close = record["spy_close"]
            if spy_close is None:
                # SPY had no eligible row on/before this date: skip it.
                dates_skipped += 1
                continue

            predicates = _build_predicates(record)
            regime = REGIME_NEUTRAL
            for candidate in constants.MARKET_REGIME_PRIORITY:
                if predicates[candidate]():
                    regime = candidate
                    break

            # neutral specifically because SPY EMA200 is unavailable -> warn.
            if regime == REGIME_NEUTRAL and record["spy_ema200"] is None:
                missing_spy_ema_dates += 1

            classifications.append((current, regime))
            regimes_by_value[regime] += 1

        dates_classified = len(classifications)

        warnings: list[str] = []
        if missing_spy_ema_dates:
            warnings.append(
                f"{missing_spy_ema_dates} date(s) classified neutral because SPY "
                f"EMA200 was unavailable (insufficient history)."
            )

        log.info(
            "classify computed rows_read=%d dates_requested=%d dates_classified=%d "
            "dates_skipped=%d regimes=%s",
            rows_read,
            dates_requested,
            dates_classified,
            dates_skipped,
            regimes_by_value,
        )

        # --- write phase: single transaction across all per-date updates. -- #
        try:
            feature_rows_updated = self._write(db_role, classifications)
        except Exception as exc:  # noqa: BLE001 - surface as failed ServiceResult
            log.error(
                "classify failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            # Durable write count is 0 (rolled back); compute counts kept.
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=dates_classified,
                errors=[f"{type(exc).__name__}: {exc}"],
                metadata=self._metadata(
                    db_role=db_role,
                    start_date=start_iso,
                    end_date=end_iso,
                    dates_requested=dates_requested,
                    dates_classified=dates_classified,
                    dates_skipped_insufficient_data=dates_skipped,
                    rows_read=rows_read,
                    feature_rows_updated=0,
                    regimes_by_value=regimes_by_value,
                ),
            )

        metadata = self._metadata(
            db_role=db_role,
            start_date=start_iso,
            end_date=end_iso,
            dates_requested=dates_requested,
            dates_classified=dates_classified,
            dates_skipped_insufficient_data=dates_skipped,
            rows_read=rows_read,
            feature_rows_updated=feature_rows_updated,
            regimes_by_value=regimes_by_value,
        )

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if warnings
            else service_result.STATUS_SUCCESS
        )
        log.info(
            "classify done status=%s rows_read=%d dates_classified=%d "
            "dates_skipped=%d feature_rows_updated=%d",
            status,
            rows_read,
            dates_classified,
            dates_skipped,
            feature_rows_updated,
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=dates_classified,
            warnings=warnings,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Read phase.
    # ------------------------------------------------------------------ #
    def _read(
        self, db_role: str, warmup_start: date, end_date: date
    ) -> pl.DataFrame:
        """Read eligible SPY/QQQ/^VIX ``daily_prices`` rows (read-only).

        Returns a Polars frame of ``ticker`` / ``date`` / ``close_raw`` /
        ``close_adj`` sorted by ``(ticker, date)``. The read connection is closed
        before any computation.
        """
        load_set = sorted(set(REGIME_SYMBOLS))
        placeholders = ", ".join("?" for _ in load_set)
        sql = _SELECT_REGIME_PRICES.format(placeholders=placeholders)
        params = [STATUS_OK, warmup_start, end_date, *load_set]

        connection = self._db.connect(db_role, read_only=True)
        try:
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
            ],
            orient="row",
        ).sort(["ticker", "date"])
        return prices

    # ------------------------------------------------------------------ #
    # Write phase.
    # ------------------------------------------------------------------ #
    def _write(
        self, db_role: str, classifications: list[tuple[date, str]]
    ) -> int:
        """Update ``daily_features.market_regime`` for each classified date in a
        single transaction; return the total rows updated.

        For each date the matching-row count is read first (identical ``WHERE``),
        then the UPDATE runs; the counts are summed. Dates with no matching
        ``daily_features`` row contribute ``0``. Any error triggers ``ROLLBACK``
        so no partial Module 12 writes survive. An empty plan opens and commits
        with no updates.
        """
        if not classifications:
            return 0

        schema_version = constants.FEATURE_SCHEMA_VERSION
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                total_updated = 0
                for feature_date, regime in classifications:
                    count_row = connection.execute(
                        _COUNT_FEATURE_ROWS_FOR_DATE,
                        [feature_date, schema_version],
                    ).fetchone()
                    matched = int(count_row[0]) if count_row is not None else 0
                    connection.execute(
                        _UPDATE_FEATURE_REGIME,
                        [regime, feature_date, schema_version],
                    )
                    total_updated += matched
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return total_updated

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
            ),
        )

    def _metadata(
        self,
        *,
        db_role: str,
        start_date: str,
        end_date: str,
        dates_requested: int = 0,
        dates_classified: int = 0,
        dates_skipped_insufficient_data: int = 0,
        rows_read: int = 0,
        feature_rows_updated: int = 0,
        regimes_by_value: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        if regimes_by_value is None:
            regimes_by_value = _empty_regime_counts()
        return {
            "db_role": db_role,
            "start_date": start_date,
            "end_date": end_date,
            "dates_requested": dates_requested,
            "dates_classified": dates_classified,
            "dates_skipped_insufficient_data": dates_skipped_insufficient_data,
            "rows_read": rows_read,
            "feature_rows_updated": feature_rows_updated,
            "regimes_by_value": regimes_by_value,
        }


__all__ = [
    "MarketRegimeEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "REGIME_SYMBOLS",
    "SUPPORTED_REGIMES",
    "CANONICAL_REGIMES",
    "LOOKBACK_WARMUP_CALENDAR_DAYS",
]
