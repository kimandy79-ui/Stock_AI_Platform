"""Module 07 — Benchmark / Sector ETF Loader.

Loads benchmark, index, and sector-ETF daily price history *before* the feature
engine so that later modules can compute market regime and sector relative
strength. Module 07 is the producer of benchmark/index/ETF rows in
``daily_prices`` and is the **sole owner** of ``sector_etf_map`` (both tables are
created by the frozen Module 03 schema — ``M02_SCHEMA_SPEC.md`` §3.6/§3.7).

Contract source of truth: ``M07_BENCHMARK_ETF_LOADER_SPEC.md``.

Scope (what this module does)
-----------------------------
Given an injected :class:`~app.providers.provider_interface.MarketDataProvider`
and an inclusive ``[start_date, end_date]`` range, it:

- loads exactly ``constants.REQUIRED_BENCHMARK_SYMBOLS`` (SPY/QQQ → benchmark,
  ``^VIX`` → index, sector and industry ETFs → etf); the ETF universe spans
  all 11 broad SPDR sector ETFs plus ~53 industry-specific ETFs defined in
  ``constants.INDUSTRY_ETF_MAP`` — ~67 symbols total;
- fetches price bars only through the Module 04 provider interface
  (``get_price_history`` → ``metadata['bars']`` → ``list[PriceBar]``);
- upserts the bars into ``daily_prices`` keyed by ``(ticker, date)``;
- upserts each loaded symbol into ``ticker_master`` without clobbering
  Module-06-owned fields;
- seeds ``sector_etf_map`` from ``constants.SECTOR_ETF_MAP`` with insert-or-ignore
  semantics (never updating existing rows); note that ``SECTOR_ETF_MAP``
  covers the 11 canonical sector→ETF entries only — industry-level lookups
  use ``constants.INDUSTRY_ETF_MAP`` directly (no DB seeding required);
- returns a :class:`~app.utils.service_result.ServiceResult`.

Out of scope (owned elsewhere)
------------------------------
This module does **not**: call Yahoo / ``yfinance`` or any vendor directly (it
only uses the provider interface); open DuckDB directly or ``ATTACH`` arbitrary
paths (all DB access goes through :mod:`app.database.duckdb_manager`); run DDL
(Module 03 owns the schema); derive ``adjustment_factor`` (Module 10) or run
validation / mutation detection (Module 09); ingest stock prices (Module 08);
write to ``ticker_universe_snapshot`` (Module 06) or ``simulation.duckdb``; or
implement screening / proposals / outcomes / simulation / AI review / dashboard
logic.

Transformation note
-------------------
The per-run work is a bounded set of keyed upserts (≤ a few thousand bars across
~14 symbols), not a columnar transform, so it is implemented with explicit plain
Python and parameterized SQL — mirroring the Module 06 engine — rather than
pulling in a dataframe dependency.
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
# 07 never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are
# accepted; any other value yields a ``failed`` result with no writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# Provider metadata key carrying ``list[PriceBar]`` (PROVIDER_INTERFACE_SPEC §8).
_PROVIDER_BARS_KEY: Final[str] = "bars"

# The exact metadata key set returned by :meth:`BenchmarkEtfLoader.load`.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "start_date",
    "end_date",
    "symbols_requested",
    "symbols_loaded",
    "symbols_skipped",
    "price_rows_written",
    "ticker_master_upserted",
    "sector_etf_map_seeded",
)


class _DbManagerLike(Protocol):
    """Minimal hook the loader needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (operates only on existing tables; no DDL).
# --------------------------------------------------------------------------- #
_SELECT_MASTER_TICKERS: Final[str] = "SELECT ticker FROM ticker_master"

# Fresh benchmark/index/ETF row. Module-06-owned descriptive/lifecycle columns
# (company_name, exchange, sector, industry, security_type, first_seen,
# last_seen) are left NULL here — Module 07 does not author them.
_INSERT_MASTER: Final[str] = (
    "INSERT INTO ticker_master "
    "(ticker, yahoo_symbol, symbol_type, active_flag, delisted_flag, last_updated) "
    "VALUES (?, ?, ?, TRUE, FALSE, CAST(now() AS TIMESTAMP))"
)

# Existing row: refresh only yahoo_symbol/symbol_type/active_flag/last_updated.
# Deliberately does NOT touch first_seen, last_seen, company_name, exchange,
# sector, industry, security_type, or delisted_flag (Module-06-owned).
_UPDATE_MASTER: Final[str] = (
    "UPDATE ticker_master SET "
    "yahoo_symbol = ?, symbol_type = ?, active_flag = TRUE, "
    "last_updated = CAST(now() AS TIMESTAMP) "
    "WHERE ticker = ?"
)

# SQL-level insert-or-ignore: ``ON CONFLICT (sector) DO NOTHING`` makes the
# operation atomic — no read-then-write race window — and existing rows are
# never updated. ``RETURNING sector`` yields one row per actual insert and
# zero rows per conflict, which is how the loader counts seedings.
_INSERT_SECTOR_ETF: Final[str] = (
    "INSERT INTO sector_etf_map (sector, etf_ticker, active_flag, created_at) "
    "VALUES (?, ?, TRUE, CAST(now() AS TIMESTAMP)) "
    "ON CONFLICT (sector) DO NOTHING "
    "RETURNING sector"
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


def _classify_symbol_type(ticker: str) -> str:
    """Return the locked ``symbol_type`` for a required benchmark ``ticker``.

    ``^VIX`` → ``index``; ``SPY``/``QQQ`` → ``benchmark``; all sector and
    industry ETFs → ``etf``. Any unexpected member of
    ``REQUIRED_BENCHMARK_SYMBOLS`` defaults to ``etf`` (defensive).
    """
    if ticker == constants.BENCHMARK_VIX:
        return constants.SYMBOL_TYPE_INDEX
    if ticker in (constants.BENCHMARK_SPY, constants.BENCHMARK_QQQ):
        return constants.SYMBOL_TYPE_BENCHMARK
    return constants.SYMBOL_TYPE_ETF


class BenchmarkEtfLoader:
    """Load benchmark/index/sector-ETF prices and maintain ``sector_etf_map``.

    The loader is effectively stateless; the optional ``db_manager`` constructor
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
    def load(
        self,
        provider: MarketDataProvider,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Load benchmark/index/ETF prices and seed ``sector_etf_map``.

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
            A fresh ``uuid4`` is minted when ``None`` (mirrors Module 06).

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``price_rows_written``. ``metadata`` carries
            exactly the keys in :data:`METADATA_KEYS` on every return path.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        symbols = constants.REQUIRED_BENCHMARK_SYMBOLS
        symbols_requested = len(symbols)
        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()

        log.info(
            "load start db_role=%s start_date=%s end_date=%s symbols_requested=%d",
            db_role,
            start_iso,
            end_iso,
            symbols_requested,
        )

        # --- db_role guard: prod/debug only, never simulation. No writes. --- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 07 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("load failed: %s", message)
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=0,
                errors=[message],
                metadata=self._metadata(
                    db_role=db_role,
                    start_date=start_iso,
                    end_date=end_iso,
                    symbols_requested=symbols_requested,
                ),
            )

        # --- date-range guard: fail before any provider call or DB write. --- #
        # An inverted range is a programmer error, not a per-symbol issue, so
        # the loader returns ``failed`` immediately with no provider traffic and
        # no DB mutations (no ``sector_etf_map`` seeding, no master writes).
        if start_date > end_date:
            message = (
                f"Invalid date range: start_date {start_iso} > end_date {end_iso}."
            )
            log.error("load failed: %s", message)
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=0,
                errors=[message],
                metadata=self._metadata(
                    db_role=db_role,
                    start_date=start_iso,
                    end_date=end_iso,
                    symbols_requested=symbols_requested,
                ),
            )

        # --- fetch phase (no DB writes): one provider call per symbol. ----- #
        # A failed status, missing/empty bars, or a raised exception for a
        # single symbol is a non-fatal warning: the symbol is skipped and the
        # run continues. Provider-level warnings (``success_with_warnings``)
        # are propagated with ticker context regardless of whether the symbol
        # was ultimately loaded.
        loaded: list[tuple[str, str, list[PriceBar]]] = []
        warnings: list[str] = []
        for ticker in symbols:
            symbol_type = _classify_symbol_type(ticker)
            try:
                request = PriceHistoryRequest(
                    ticker=ticker,
                    start_date=start_date,
                    end_date=end_date,
                    symbol_type=symbol_type,
                )
                result = provider.get_price_history(request)
            except Exception as exc:  # noqa: BLE001 - degrade to per-symbol skip
                log.warning(
                    "symbol %s: provider raised %s: %s; skipped",
                    ticker,
                    type(exc).__name__,
                    exc,
                )
                warnings.append(
                    f"{ticker}: provider raised {type(exc).__name__}: {exc}; skipped"
                )
                continue

            # Propagate provider warnings (with ticker context) regardless of
            # status so ``success_with_warnings`` payloads are not silently
            # dropped. The symbol may still load if valid bars are present.
            for provider_warning in result.warnings:
                warnings.append(f"{ticker}: {provider_warning}")

            if result.status == service_result.STATUS_FAILED:
                detail = "; ".join(result.errors) or "provider returned failed"
                log.warning(
                    "symbol %s: provider failure (%s); skipped", ticker, detail
                )
                warnings.append(f"{ticker}: provider failure ({detail}); skipped")
                continue

            # Distinguish a provider-contract violation (no ``bars`` key, or an
            # explicit ``None``) from a legitimate empty range (key present,
            # empty list). The first is a contract issue; the second is normal
            # for, e.g., a weekend or pre-IPO range.
            bars_obj = result.metadata.get(_PROVIDER_BARS_KEY)
            if _PROVIDER_BARS_KEY not in result.metadata or bars_obj is None:
                log.warning(
                    "symbol %s: provider result missing metadata['bars']; skipped",
                    ticker,
                )
                warnings.append(
                    f"{ticker}: provider result missing metadata['bars']; skipped"
                )
                continue

            bars = list(bars_obj)
            if not bars:
                log.warning(
                    "symbol %s: provider returned zero bars; skipped", ticker
                )
                warnings.append(f"{ticker}: provider returned zero bars; skipped")
                continue

            log.info(
                "symbol %s loaded symbol_type=%s bars=%d",
                ticker,
                symbol_type,
                len(bars),
            )
            loaded.append((ticker, symbol_type, bars))

        symbols_loaded = len(loaded)
        symbols_skipped = symbols_requested - symbols_loaded

        # --- write phase: single transaction across all DB mutations. ------ #
        try:
            counts = self._write(db_role, loaded)
        except Exception as exc:  # noqa: BLE001 - surface as failed ServiceResult
            log.error(
                "load failed during write (rolled back): %s: %s",
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
                    symbols_requested=symbols_requested,
                    symbols_loaded=symbols_loaded,
                    symbols_skipped=symbols_skipped,
                ),
            )

        metadata = self._metadata(
            db_role=db_role,
            start_date=start_iso,
            end_date=end_iso,
            symbols_requested=symbols_requested,
            symbols_loaded=symbols_loaded,
            symbols_skipped=symbols_skipped,
            price_rows_written=counts["price_rows_written"],
            ticker_master_upserted=counts["ticker_master_upserted"],
            sector_etf_map_seeded=counts["sector_etf_map_seeded"],
        )

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if warnings
            else service_result.STATUS_SUCCESS
        )
        log.info(
            "load done status=%s symbols_loaded=%d symbols_skipped=%d "
            "price_rows_written=%d ticker_master_upserted=%d "
            "sector_etf_map_seeded=%d",
            status,
            symbols_loaded,
            symbols_skipped,
            counts["price_rows_written"],
            counts["ticker_master_upserted"],
            counts["sector_etf_map_seeded"],
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
    def _write(
        self,
        db_role: str,
        loaded: list[tuple[str, str, list[PriceBar]]],
    ) -> dict[str, int]:
        """Run all DB mutations inside one transaction; return counts.

        Order inside the single ``BEGIN TRANSACTION ... COMMIT``:
        (1) seed ``sector_etf_map`` (constant-driven, insert-or-ignore);
        (2) upsert ``ticker_master`` for each loaded symbol;
        (3) upsert each loaded symbol's bars into ``daily_prices``.

        Any error triggers ``ROLLBACK`` so no partial / orphaned rows survive.
        The connection is obtained only via the approved DB manager; this module
        never opens a path or ``ATTACH``es.
        """
        price_rows_written = 0
        ticker_master_upserted = 0
        sector_etf_map_seeded = 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                # (1) sector_etf_map seed — SQL-level insert-or-ignore.
                # ``ON CONFLICT (sector) DO NOTHING`` is atomic at the DB
                # layer (no read-then-write window) and never updates
                # existing rows. ``RETURNING sector`` yields one row per
                # actual insert and zero rows per conflict, which is how
                # ``sector_etf_map_seeded`` is counted.
                for sector, etf_ticker in constants.SECTOR_ETF_MAP.items():
                    returned = connection.execute(
                        _INSERT_SECTOR_ETF, [sector, etf_ticker]
                    ).fetchall()
                    sector_etf_map_seeded += len(returned)

                # (2) ticker_master upsert for each loaded symbol.
                existing_master = {
                    row[0]
                    for row in connection.execute(
                        _SELECT_MASTER_TICKERS
                    ).fetchall()
                }
                for ticker, symbol_type, _bars in loaded:
                    if ticker in existing_master:
                        connection.execute(
                            _UPDATE_MASTER, [ticker, symbol_type, ticker]
                        )
                    else:
                        connection.execute(
                            _INSERT_MASTER, [ticker, ticker, symbol_type]
                        )
                    ticker_master_upserted += 1

                # (3) daily_prices upsert for each loaded bar.
                for ticker, symbol_type, bars in loaded:
                    is_vix = ticker == constants.BENCHMARK_VIX
                    for bar in bars:
                        connection.execute(
                            _UPSERT_DAILY_PRICE,
                            self._daily_price_params(ticker, bar, is_vix),
                        )
                        price_rows_written += 1

                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

        return {
            "price_rows_written": price_rows_written,
            "ticker_master_upserted": ticker_master_upserted,
            "sector_etf_map_seeded": sector_etf_map_seeded,
        }

    @staticmethod
    def _daily_price_params(
        ticker: str, bar: PriceBar, is_vix: bool
    ) -> list[Any]:
        """Build the parameter list for :data:`_UPSERT_DAILY_PRICE`.

        Applies the locked ``^VIX`` rule (``close_raw = close_adj``,
        ``volume_raw = NULL``) and the missing-value defaults
        (``dividend_amount = 0``, ``split_ratio = 1``). ``volume_adj`` and
        ``adjustment_factor`` are NULL via the SQL literal, not here.
        """
        if is_vix:
            # close_raw mirrors close_adj; volume is dropped for the index.
            close_raw: float | None = bar.close_adj
            volume_raw: int | None = None
        else:
            close_raw = bar.close_raw
            volume_raw = bar.volume_raw

        dividend_amount = bar.dividend_amount if bar.dividend_amount is not None else 0
        split_ratio = bar.split_ratio if bar.split_ratio is not None else 1

        return [
            ticker,
            bar.date,
            bar.open_raw,
            bar.high_raw,
            bar.low_raw,
            close_raw,
            volume_raw,
            bar.open_adj,
            bar.high_adj,
            bar.low_adj,
            bar.close_adj,
            dividend_amount,
            split_ratio,
            bar.source_provider,
        ]

    def _metadata(
        self,
        *,
        db_role: str,
        start_date: str,
        end_date: str,
        symbols_requested: int,
        symbols_loaded: int = 0,
        symbols_skipped: int = 0,
        price_rows_written: int = 0,
        ticker_master_upserted: int = 0,
        sector_etf_map_seeded: int = 0,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "db_role": db_role,
            "start_date": start_date,
            "end_date": end_date,
            "symbols_requested": symbols_requested,
            "symbols_loaded": symbols_loaded,
            "symbols_skipped": symbols_skipped,
            "price_rows_written": price_rows_written,
            "ticker_master_upserted": ticker_master_upserted,
            "sector_etf_map_seeded": sector_etf_map_seeded,
        }


__all__ = ["BenchmarkEtfLoader", "METADATA_KEYS", "ALLOWED_DB_ROLES"]
