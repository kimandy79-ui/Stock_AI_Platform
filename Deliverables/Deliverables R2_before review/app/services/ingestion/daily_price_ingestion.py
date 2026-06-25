"""Module 08 — Daily Price Ingestion.

Downloads and updates daily OHLCV prices for every *active stock* in the
universe **before** the feature engine runs. Module 08 is the stock-universe
equivalent of Module 07 (benchmark / sector-ETF loader): it produces the
``stock`` rows in ``daily_prices`` and enqueues missing data into
``data_repair_queue``.

Contract source of truth: ``M08_DAILY_PRICE_INGESTION_SPEC.md``.

Scope (what this module does)
-----------------------------
Given an injected :class:`~app.providers.provider_interface.MarketDataProvider`
and an inclusive ``[start_date, end_date]`` range, it:

- reads the active ticker list from ``ticker_master`` where
  ``symbol_type = 'stock' AND active_flag = TRUE`` (never hardcoded);
- fetches price bars only through the Module 04 provider interface
  (``get_price_history`` → ``metadata['bars']`` → ``list[PriceBar]``);
- upserts the bars into ``daily_prices`` keyed by ``(ticker, date)``;
- enqueues failed / empty-result tickers into ``data_repair_queue`` with
  insert-or-ignore semantics keyed on ``(ticker, repair_date, repair_reason)``;
- returns a :class:`~app.utils.service_result.ServiceResult`.

Out of scope (owned elsewhere)
------------------------------
This module does **not**: call Yahoo / ``yfinance`` or any vendor directly (it
only uses the provider interface); open DuckDB directly or ``ATTACH`` arbitrary
paths (all DB access goes through :mod:`app.database.duckdb_manager`); run DDL
(Module 03 owns the schema); write to ``ticker_master`` (read-only here),
``ticker_universe_snapshot``, ``sector_etf_map``, or ``simulation.duckdb``;
ingest benchmark/index/ETF symbols (Module 07); process / resolve / delete
``data_repair_queue`` entries (enqueuing is the only write — Module 08 is not
the repair processor); derive ``adjustment_factor`` (Module 10); run validation
/ mutation detection (Module 09); or implement screening / proposals / outcomes
/ simulation / AI review / dashboard logic.

Transformation note
-------------------
The per-run work is a bounded set of keyed upserts, not a columnar transform,
so it is implemented with explicit plain Python and parameterized SQL —
mirroring Module 07 — rather than pulling in a dataframe dependency.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.config import constants
from app.database import duckdb_manager
from app.providers.provider_interface import (
    MarketDataProvider,
    PriceBar,
    PriceHistoryRequest,
)
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Roles this module is allowed to target.
# --------------------------------------------------------------------------- #
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module
# 08 never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are
# accepted; any other value yields a ``failed`` result with no writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# Provider metadata key carrying ``list[PriceBar]`` (PROVIDER_INTERFACE_SPEC §8).
_PROVIDER_BARS_KEY: Final[str] = "bars"

# data_repair_queue locked constants (M08 spec §6).
_REPAIR_REASON_MISSING_PRICE: Final[str] = "missing_price"
_REPAIR_STATUS_PENDING: Final[str] = "pending"
_REPAIR_MAX_ATTEMPTS: Final[int] = 3

# Namespace for deterministic repair_id derivation. The frozen schema has no
# UNIQUE(ticker, repair_date, repair_reason) constraint, so insert-or-ignore on
# the logical key cannot use ON CONFLICT over that triple. Instead, repair_id is
# derived deterministically (uuid5) from the logical key, and the insert uses the
# existing ``repair_id`` PRIMARY KEY as the conflict target — giving DB-enforced
# dedup without DDL (spec §16).
_REPAIR_ID_NAMESPACE: Final[uuid.UUID] = uuid.NAMESPACE_URL


def _repair_id_for(ticker: str, repair_date: date, repair_reason: str) -> str:
    """Return a deterministic ``repair_id`` for a logical repair key.

    The same ``(ticker, repair_date, repair_reason)`` always maps to the same
    ``repair_id``, so two inserts of the same logical task collide on the
    ``repair_id`` PRIMARY KEY and the second is a DB-level no-op. This holds even
    across concurrent Module 08 runs (no application-side read-then-write race
    window for the dedup decision).
    """
    return str(
        uuid.uuid5(
            _REPAIR_ID_NAMESPACE,
            f"data_repair_queue:{ticker}:{repair_date.isoformat()}:{repair_reason}",
        )
    )

# The exact metadata key set returned by :meth:`DailyPriceIngestionEngine.ingest`.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "start_date",
    "end_date",
    "tickers_requested",
    "tickers_loaded",
    "tickers_skipped",
    "price_rows_written",
    "repair_queue_enqueued",
)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (operates only on existing tables; no DDL).
# --------------------------------------------------------------------------- #
# Active-stock selection. ``active_flag`` is BOOLEAN in the frozen Module 03
# schema (schema_manager.py / M02_SCHEMA_SPEC.md), so the predicate uses TRUE.
_SELECT_ACTIVE_STOCKS: Final[str] = (
    "SELECT ticker FROM ticker_master "
    "WHERE symbol_type = ? AND active_flag = TRUE "
    "ORDER BY ticker"
)

# Upsert one daily bar keyed by (ticker, date). Locked defaults are inlined:
# volume_adj/adjustment_factor NULL, data_quality_status 'ok', mutation_flag
# FALSE. created_at is set on insert and preserved on conflict; updated_at is
# refreshed on conflict only.
_UPSERT_DAILY_PRICE: Final[str] = (
    "INSERT INTO daily_prices "
    "(ticker, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
    " open_adj, high_adj, low_adj, close_adj, volume_adj, "
    " dividend_amount, split_ratio, adjustment_factor, source_provider, "
    " data_quality_status, mutation_flag, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, 'ok', FALSE, "
    " CAST(now() AS TIMESTAMP), NULL) "
    "ON CONFLICT (ticker, date) DO UPDATE SET "
    "open_raw = excluded.open_raw, high_raw = excluded.high_raw, "
    "low_raw = excluded.low_raw, close_raw = excluded.close_raw, "
    "volume_raw = excluded.volume_raw, open_adj = excluded.open_adj, "
    "high_adj = excluded.high_adj, low_adj = excluded.low_adj, "
    "close_adj = excluded.close_adj, volume_adj = excluded.volume_adj, "
    "dividend_amount = excluded.dividend_amount, "
    "split_ratio = excluded.split_ratio, "
    "adjustment_factor = excluded.adjustment_factor, "
    "source_provider = excluded.source_provider, "
    "data_quality_status = excluded.data_quality_status, "
    "mutation_flag = excluded.mutation_flag, "
    "updated_at = CAST(now() AS TIMESTAMP)"
)

# Existing repair keys for the missing_price reason. Used for an application-side
# insert-or-ignore on (ticker, repair_date, repair_reason): the frozen schema
# has only ``repair_id`` as PRIMARY KEY and a non-unique
# ``idx_repair_status(status, repair_date)`` index, so there is no DB-level
# unique constraint to drive ``ON CONFLICT`` (see spec §6 / §16, blocking
# schema note). Reading the existing keys inside the same write transaction and
# inserting only new ones is the safe, no-DDL equivalent.
_SELECT_EXISTING_REPAIR_KEYS: Final[str] = (
    "SELECT ticker, repair_date FROM data_repair_queue WHERE repair_reason = ?"
)

# Insert one fresh repair task. attempts=0, max_attempts=3, status='pending',
# Insert one fresh repair task. attempts=0, max_attempts=3, status='pending',
# created_at set on insert, updated_at NULL, last_attempt NULL. ``repair_id`` is
# deterministic (see ``_repair_id_for``), so ``ON CONFLICT (repair_id) DO
# NOTHING`` makes re-inserting the same logical task a DB-level no-op even under
# concurrent runs. ``RETURNING repair_id`` yields one row per actual insert and
# zero rows per conflict, which is how ``repair_queue_enqueued`` is counted.
_INSERT_REPAIR: Final[str] = (
    "INSERT INTO data_repair_queue "
    "(repair_id, ticker, repair_date, repair_reason, attempts, max_attempts, "
    " last_attempt, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, 0, ?, NULL, ?, CAST(now() AS TIMESTAMP), NULL) "
    "ON CONFLICT (repair_id) DO NOTHING "
    "RETURNING repair_id"
)


class DailyPriceIngestionEngine:
    """Ingest active-stock daily prices and enqueue missing data for repair.

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
    def ingest(
        self,
        provider: MarketDataProvider,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
        tickers: list[str] | None = None,
    ) -> ServiceResult:
        """Ingest daily prices for the active stock universe (or a ticker scope).

        Parameters
        ----------
        provider:
            A Module 04 :class:`MarketDataProvider`. Price bars are fetched only
            through ``provider.get_price_history`` — never via a direct vendor
            call.
        start_date, end_date:
            Inclusive ``[start_date, end_date]`` range passed to each
            :class:`PriceHistoryRequest`.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns a ``failed`` result with no writes.
        run_id:
            A fresh ``uuid4`` is minted when ``None`` (mirrors Module 07).
        tickers:
            Optional explicit ticker scope. ``None`` (the default, used by the
            daily pipeline) preserves the original behavior exactly: select all
            active stocks from ``ticker_master``. When a list is supplied (e.g.
            by the historical backfill tool to process the universe in batches),
            only those tickers are ingested and no ``ticker_master`` read is
            performed. Empty/duplicate entries are dropped while preserving
            order. This is an additive, behavior-preserving argument.

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``price_rows_written``. ``metadata`` carries
            exactly the keys in :data:`METADATA_KEYS` on every return path.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()

        log.info(
            "ingest start db_role=%s start_date=%s end_date=%s",
            db_role,
            start_iso,
            end_iso,
        )

        # --- db_role guard: prod/debug only, never simulation. No writes. --- #
        # Guarded before any DB access so an invalid role triggers no reads,
        # no provider calls, and no writes. ``tickers_requested`` is therefore
        # unknown here and reported as 0 (spec assumption A2).
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 08 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("ingest failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- date-range guard: fail before any provider call or DB access. -- #
        if start_date > end_date:
            message = (
                f"Invalid date range: start_date {start_iso} > end_date {end_iso}."
            )
            log.error("ingest failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- ticker selection (read-only): active stocks only, unless an
        #     explicit scope is supplied (additive; daily passes None). ------- #
        if tickers is not None:
            # Explicit scope: drop empties/dupes, keep order; no ticker_master
            # read. Used by the historical backfill tool for batched ingestion.
            selected = [t for t in dict.fromkeys(tickers) if t]
        else:
            try:
                selected = self._select_active_stocks(db_role)
            except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
                message = f"ticker selection failed: {type(exc).__name__}: {exc}"
                log.error("ingest failed: %s", message)
                return self._failed(run_id, message, db_role, start_iso, end_iso)

        tickers_requested = len(selected)
        log.info("ingest tickers_requested=%d", tickers_requested)

        # --- fetch phase (no DB writes): one provider call per ticker. ----- #
        # A failed status, missing/empty bars, or a raised exception for a
        # single ticker is non-fatal: the ticker is queued for repair, skipped,
        # and the run continues. Provider-level warnings
        # (``success_with_warnings``) are propagated with ticker context.
        loaded: list[tuple[str, list[PriceBar]]] = []
        repair_tickers: list[str] = []
        warnings: list[str] = []

        for ticker in selected:
            try:
                request = PriceHistoryRequest(
                    ticker=ticker,
                    start_date=start_date,
                    end_date=end_date,
                    symbol_type=constants.SYMBOL_TYPE_STOCK,
                )
                result = provider.get_price_history(request)
            except Exception as exc:  # noqa: BLE001 - degrade to per-ticker skip
                log.warning(
                    "ticker %s: provider raised %s: %s; queued for repair",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                warnings.append(
                    f"{ticker}: provider raised {type(exc).__name__}: {exc}; "
                    "queued for repair"
                )
                repair_tickers.append(ticker)
                continue

            # Propagate provider warnings (with ticker context) regardless of
            # status so ``success_with_warnings`` payloads are not silently
            # dropped. The ticker may still load if valid bars are present.
            for provider_warning in result.warnings:
                warnings.append(f"{ticker}: {provider_warning}")

            if result.status == service_result.STATUS_FAILED:
                detail = "; ".join(result.errors) or "provider returned failed"
                log.warning(
                    "ticker %s: provider failure (%s); queued for repair",
                    ticker,
                    detail,
                )
                warnings.append(
                    f"{ticker}: provider failure ({detail}); queued for repair"
                )
                repair_tickers.append(ticker)
                continue

            # Distinguish a provider-contract violation (no ``bars`` key, or an
            # explicit ``None``) from a legitimate empty range (key present,
            # empty list). Both are queued for repair, but logged differently.
            bars_obj = result.metadata.get(_PROVIDER_BARS_KEY)
            if _PROVIDER_BARS_KEY not in result.metadata or bars_obj is None:
                log.warning(
                    "ticker %s: provider result missing metadata['bars']; "
                    "queued for repair",
                    ticker,
                )
                warnings.append(
                    f"{ticker}: provider result missing metadata['bars']; "
                    "queued for repair"
                )
                repair_tickers.append(ticker)
                continue

            bars = list(bars_obj)
            if not bars:
                log.warning(
                    "ticker %s: provider returned zero bars; queued for repair",
                    ticker,
                )
                warnings.append(
                    f"{ticker}: provider returned zero bars; queued for repair"
                )
                repair_tickers.append(ticker)
                continue

            log.info("ticker %s loaded bars=%d", ticker, len(bars))
            loaded.append((ticker, bars))

        tickers_loaded = len(loaded)
        tickers_skipped = tickers_requested - tickers_loaded

        # --- write phase: single transaction across all DB mutations. ------ #
        try:
            counts = self._write(db_role, end_date, loaded, repair_tickers)
        except Exception as exc:  # noqa: BLE001 - surface as failed ServiceResult
            log.error(
                "ingest failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=0,
                errors=[f"{type(exc).__name__}: {exc}"],
                metadata=self._metadata(
                    db_role=db_role,
                    start_date=start_iso,
                    end_date=end_iso,
                    tickers_requested=tickers_requested,
                    tickers_loaded=tickers_loaded,
                    tickers_skipped=tickers_skipped,
                ),
            )

        metadata = self._metadata(
            db_role=db_role,
            start_date=start_iso,
            end_date=end_iso,
            tickers_requested=tickers_requested,
            tickers_loaded=tickers_loaded,
            tickers_skipped=tickers_skipped,
            price_rows_written=counts["price_rows_written"],
            repair_queue_enqueued=counts["repair_queue_enqueued"],
        )

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if warnings
            else service_result.STATUS_SUCCESS
        )
        log.info(
            "ingest done status=%s tickers_loaded=%d tickers_skipped=%d "
            "price_rows_written=%d repair_queue_enqueued=%d",
            status,
            tickers_loaded,
            tickers_skipped,
            counts["price_rows_written"],
            counts["repair_queue_enqueued"],
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=counts["price_rows_written"],
            warnings=warnings,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers.
    # ------------------------------------------------------------------ #
    def _select_active_stocks(self, db_role: str) -> list[str]:
        """Return active stock tickers from ``ticker_master`` (read-only).

        Reads through the approved DB manager in a short read-only connection
        that is closed before the fetch phase, so no transaction is held open
        while the provider is called.
        """
        connection = self._db.connect(db_role, read_only=True)
        try:
            rows = connection.execute(
                _SELECT_ACTIVE_STOCKS, [constants.SYMBOL_TYPE_STOCK]
            ).fetchall()
        finally:
            connection.close()
        return [row[0] for row in rows]

    def _write(
        self,
        db_role: str,
        end_date: date,
        loaded: list[tuple[str, list[PriceBar]]],
        repair_tickers: list[str],
    ) -> dict[str, int]:
        """Run all DB mutations inside one transaction; return counts.

        Order inside the single ``BEGIN TRANSACTION ... COMMIT``:
        (1) upsert each loaded ticker's bars into ``daily_prices``;
        (2) insert-or-ignore repair tasks for failed / empty tickers into
            ``data_repair_queue``, deduplicated on the logical key
            ``(ticker, repair_date, repair_reason)`` via a deterministic
            ``repair_id`` and ``ON CONFLICT (repair_id) DO NOTHING``.

        Any error triggers ``ROLLBACK`` so no partial / orphaned rows survive
        (no half-written prices, no stray repair rows). The connection is
        obtained only via the approved DB manager.
        """
        price_rows_written = 0
        repair_queue_enqueued = 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                # (1) daily_prices upsert for each loaded bar.
                for ticker, bars in loaded:
                    for bar in bars:
                        connection.execute(
                            _UPSERT_DAILY_PRICE,
                            self._daily_price_params(ticker, bar),
                        )
                        price_rows_written += 1

                # (2) data_repair_queue insert-or-ignore.
                # ``repair_id`` is derived deterministically from the logical
                # key (ticker, repair_date, repair_reason), and the insert uses
                # ``ON CONFLICT (repair_id) DO NOTHING`` over the existing
                # PRIMARY KEY. This is atomic at the DB layer (no read-then-write
                # race window), so concurrent or repeated runs cannot create
                # duplicate deterministic-id rows even though the schema has no
                # UNIQUE on the triple. ``RETURNING repair_id`` yields one row
                # per actual insert and zero rows per conflict, which is how
                # ``repair_queue_enqueued`` is counted (newly inserted rows
                # only).
                #
                # Compatibility guard: a repair row created by a *prior* version
                # (random uuid4 for the same logical key) would NOT collide on
                # the deterministic repair_id, so its logical key is pre-read and
                # used to skip a redundant deterministic insert. A within-run
                # guard avoids redundant no-op inserts for the same ticker.
                if repair_tickers:
                    existing_logical = {
                        (row[0], row[1])
                        for row in connection.execute(
                            _SELECT_EXISTING_REPAIR_KEYS,
                            [_REPAIR_REASON_MISSING_PRICE],
                        ).fetchall()
                    }
                    seen_this_run: set[str] = set()
                    for ticker in repair_tickers:
                        if (ticker, end_date) in existing_logical:
                            continue  # legacy/random-id row already covers it
                        repair_id = _repair_id_for(
                            ticker, end_date, _REPAIR_REASON_MISSING_PRICE
                        )
                        if repair_id in seen_this_run:
                            continue
                        seen_this_run.add(repair_id)
                        returned = connection.execute(
                            _INSERT_REPAIR,
                            [
                                repair_id,
                                ticker,
                                end_date,
                                _REPAIR_REASON_MISSING_PRICE,
                                _REPAIR_MAX_ATTEMPTS,
                                _REPAIR_STATUS_PENDING,
                            ],
                        ).fetchall()
                        repair_queue_enqueued += len(returned)

                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

        return {
            "price_rows_written": price_rows_written,
            "repair_queue_enqueued": repair_queue_enqueued,
        }

    @staticmethod
    def _daily_price_params(ticker: str, bar: PriceBar) -> list[Any]:
        """Build the parameter list for :data:`_UPSERT_DAILY_PRICE`.

        Stocks use the provider's ``close_raw`` / ``volume_raw`` verbatim (no
        ``^VIX`` special case — that is Module 07). Applies the missing-value
        defaults (``dividend_amount = 0``, ``split_ratio = 1``). ``volume_adj``
        and ``adjustment_factor`` are NULL via the SQL literal, not here.
        """
        dividend_amount = bar.dividend_amount if bar.dividend_amount is not None else 0
        split_ratio = bar.split_ratio if bar.split_ratio is not None else 1

        return [
            ticker,
            bar.date,
            bar.open_raw,
            bar.high_raw,
            bar.low_raw,
            bar.close_raw,
            bar.volume_raw,
            bar.open_adj,
            bar.high_adj,
            bar.low_adj,
            bar.close_adj,
            dividend_amount,
            split_ratio,
            bar.source_provider,
        ]

    def _failed(
        self,
        run_id: str,
        message: str,
        db_role: str,
        start_iso: str,
        end_iso: str,
    ) -> ServiceResult:
        """Build a ``failed`` result for a pre-DB guard (no writes performed)."""
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
        tickers_requested: int = 0,
        tickers_loaded: int = 0,
        tickers_skipped: int = 0,
        price_rows_written: int = 0,
        repair_queue_enqueued: int = 0,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "db_role": db_role,
            "start_date": start_date,
            "end_date": end_date,
            "tickers_requested": tickers_requested,
            "tickers_loaded": tickers_loaded,
            "tickers_skipped": tickers_skipped,
            "price_rows_written": price_rows_written,
            "repair_queue_enqueued": repair_queue_enqueued,
        }


__all__ = [
    "DailyPriceIngestionEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "_repair_id_for",
]
