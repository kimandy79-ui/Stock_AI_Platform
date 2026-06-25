"""Module 06 — Universe Snapshot Engine.

Maintains the ticker universe and writes immutable monthly point-in-time
membership snapshots. This module is the producer of two tables already
defined by the frozen Module 03 schema (``M02_SCHEMA_SPEC.md`` §3.4–3.5):

- ``ticker_master`` — the current known-symbol master (one row per ticker);
- ``ticker_universe_snapshot`` — one immutable row per ``(snapshot_month,
  ticker)`` used later by simulation to mitigate survivorship bias
  (``02_PROJECT_IMPLEMENTATION_CONTEXT.md`` decision 22.9: "simulation uses the
  historical snapshot nearest and not after the sim date").

Contract source of truth: ``M06_UNIVERSE_SNAPSHOT_SPEC.md``.

Scope (what this module does)
-----------------------------
Given a set of provider-neutral :class:`~app.providers.provider_interface.TickerInfo`
entries (supplied by a caller — Module 06 never fetches them), it upserts each
into ``ticker_master``, manages the lifecycle flags, writes the monthly
snapshot rows, and returns a :class:`~app.utils.service_result.ServiceResult`.

Out of scope (per the Module 06 task)
-------------------------------------
This module does **not**: call Yahoo / ``yfinance`` (no provider import);
compute market cap; write to ``sector_etf_map`` (Module 07); ingest prices
(Module 08); run validation / screening / scoring / proposals / outcomes /
simulation / AI review / dashboard logic; open DuckDB directly or ``ATTACH``
arbitrary paths (all DB access goes through :mod:`app.database.duckdb_manager`);
or run ``ALTER TABLE`` / ``CREATE TABLE`` (Module 03 owns the schema). It only
``INSERT`` / ``UPDATE`` / ``DELETE``-within-month on the two existing tables.

Transformation note
--------------------
The per-run work is a small set of keyed upserts plus de-duplication, not a
columnar dataframe transformation, so it is implemented with explicit plain
Python (a dict keyed by ticker). Polars is intentionally not pulled in for this
trivial case; this keeps the module small, dependency-light, and easy to review
(``01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md`` "Polars-first" applies to
the heavy feature/screening transforms, not to a handful of upserts).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Final, Iterable, Protocol

from app.database import duckdb_manager
from app.providers.provider_interface import TickerInfo
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Roles this module is allowed to target.
# --------------------------------------------------------------------------- #
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module
# 06 never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are
# accepted; any other value yields a ``failed`` result with no writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# The exact metadata key set returned by :meth:`UniverseSnapshotEngine.apply_snapshot`.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "snapshot_month",
    "db_role",
    "source",
    "input_rows",
    "valid_rows",
    "skipped_rows",
    "tickers_inserted",
    "tickers_updated",
    "tickers_marked_inactive",
    "snapshot_rows",
)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (operates only on existing tables; no DDL).
# --------------------------------------------------------------------------- #
_SELECT_MASTER: Final[str] = "SELECT ticker FROM ticker_master"

_INSERT_MASTER: Final[str] = (
    "INSERT INTO ticker_master "
    "(ticker, yahoo_symbol, company_name, exchange, sector, industry, "
    " security_type, symbol_type, active_flag, delisted_flag, "
    " first_seen, last_seen, last_updated) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRUE, FALSE, ?, ?, CAST(now() AS TIMESTAMP))"
)

_UPDATE_MASTER_PRESENT: Final[str] = (
    "UPDATE ticker_master SET "
    "yahoo_symbol = ?, company_name = ?, exchange = ?, sector = ?, "
    "industry = ?, security_type = ?, symbol_type = ?, "
    "active_flag = TRUE, last_seen = ?, last_updated = CAST(now() AS TIMESTAMP) "
    "WHERE ticker = ?"
)

# Absent previously-known ticker: deactivate, but do NOT flip delisted_flag and
# do NOT touch last_seen (it records the last month the ticker was present).
_UPDATE_MASTER_ABSENT: Final[str] = (
    "UPDATE ticker_master SET "
    "active_flag = FALSE, last_updated = CAST(now() AS TIMESTAMP) "
    "WHERE ticker = ?"
)

_DELETE_SNAPSHOT_MONTH: Final[str] = (
    "DELETE FROM ticker_universe_snapshot WHERE snapshot_month = ?"
)

_INSERT_SNAPSHOT: Final[str] = (
    "INSERT INTO ticker_universe_snapshot "
    "(snapshot_month, ticker, exchange, sector, industry, market_cap_bucket, "
    " active_flag, source, created_at) "
    "VALUES (?, ?, ?, ?, ?, NULL, TRUE, ?, CAST(now() AS TIMESTAMP))"
)


class UniverseSnapshotEngine:
    """Maintain ``ticker_master`` and write monthly universe snapshots.

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
    def apply_snapshot(
        self,
        entries: Iterable[TickerInfo],
        as_of_date: date,
        db_role: str = "prod",
        source: str = "manual",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Upsert ``entries`` into ``ticker_master`` and write the month snapshot.

        Parameters
        ----------
        entries:
            Provider-neutral :class:`TickerInfo` objects. Items that are not
            ``TickerInfo`` are skipped with a warning; duplicate tickers keep
            the last occurrence (earlier ones are skipped).
        as_of_date:
            Calendar date the snapshot is taken for. The snapshot month is
            normalized to the first of that month.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns a ``failed`` result with no writes.
        source:
            Written verbatim to ``ticker_universe_snapshot.source``.
        run_id:
            A fresh ``uuid4`` is minted when ``None`` (mirrors Module 03).

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals the number of snapshot rows written.
            ``metadata`` carries exactly the keys in :data:`METADATA_KEYS`.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        # Materialize once so ``input_rows`` is accurate and a generator is not
        # consumed twice. Listing the input is not a database write.
        raw: list[Any] = list(entries)
        input_rows = len(raw)
        snapshot_month = date(as_of_date.year, as_of_date.month, 1)
        snapshot_month_iso = snapshot_month.isoformat()

        log.info(
            "apply_snapshot start db_role=%s source=%s as_of_date=%s "
            "snapshot_month=%s input_rows=%d",
            db_role,
            source,
            as_of_date.isoformat(),
            snapshot_month_iso,
            input_rows,
        )

        # --- db_role guard: prod/debug only, never simulation. No writes. --- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 06 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("apply_snapshot failed: %s", message)
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=0,
                errors=[message],
                metadata=self._metadata(
                    snapshot_month=snapshot_month_iso,
                    db_role=db_role,
                    source=source,
                    input_rows=input_rows,
                ),
            )

        # --- classify input: dedupe (last wins) + skip bad types. --------- #
        accepted: dict[str, TickerInfo] = {}
        warnings: list[str] = []
        skipped_rows = 0
        for item in raw:
            if not isinstance(item, TickerInfo):
                skipped_rows += 1
                warnings.append(
                    f"skipped non-TickerInfo input of type {type(item).__name__}"
                )
                continue
            if item.ticker in accepted:
                # Keep the last occurrence; the earlier one is a dropped duplicate.
                skipped_rows += 1
                warnings.append(
                    f"duplicate ticker {item.ticker!r} in input; keeping last occurrence"
                )
            accepted[item.ticker] = item

        valid_rows = len(accepted)

        # --- apply within a single transaction (delete-then-insert). ------ #
        try:
            counts = self._write(db_role, snapshot_month, accepted, source)
        except Exception as exc:  # noqa: BLE001 - surface as failed ServiceResult
            log.error(
                "apply_snapshot failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=0,
                errors=[f"{type(exc).__name__}: {exc}"],
                metadata=self._metadata(
                    snapshot_month=snapshot_month_iso,
                    db_role=db_role,
                    source=source,
                    input_rows=input_rows,
                    valid_rows=valid_rows,
                    skipped_rows=skipped_rows,
                ),
            )

        metadata = self._metadata(
            snapshot_month=snapshot_month_iso,
            db_role=db_role,
            source=source,
            input_rows=input_rows,
            valid_rows=valid_rows,
            skipped_rows=skipped_rows,
            tickers_inserted=counts["tickers_inserted"],
            tickers_updated=counts["tickers_updated"],
            tickers_marked_inactive=counts["tickers_marked_inactive"],
            snapshot_rows=counts["snapshot_rows"],
        )

        status = (
            service_result.STATUS_SUCCESS_WITH_WARNINGS
            if warnings
            else service_result.STATUS_SUCCESS
        )
        log.info(
            "apply_snapshot done status=%s inserted=%d updated=%d "
            "marked_inactive=%d snapshot_rows=%d skipped=%d",
            status,
            counts["tickers_inserted"],
            counts["tickers_updated"],
            counts["tickers_marked_inactive"],
            counts["snapshot_rows"],
            skipped_rows,
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=counts["snapshot_rows"],
            warnings=warnings,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers.
    # ------------------------------------------------------------------ #
    def _write(
        self,
        db_role: str,
        snapshot_month: date,
        accepted: dict[str, TickerInfo],
        source: str,
    ) -> dict[str, int]:
        """Run the locked delete-then-insert transaction; return counts.

        All writes happen inside a single ``BEGIN TRANSACTION ... COMMIT``; any
        error triggers ``ROLLBACK`` so no partial rows survive (no orphaned
        snapshot rows for the month, no half-applied master upserts).

        The connection is obtained only via the approved DB manager
        (``connect(db_role)``); this module never opens a path or ``ATTACH``es.
        """
        tickers_inserted = 0
        tickers_updated = 0
        tickers_marked_inactive = 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                existing = {
                    row[0] for row in connection.execute(_SELECT_MASTER).fetchall()
                }

                # Upsert each accepted ticker (lifecycle flags assigned here).
                for ticker, info in accepted.items():
                    if ticker in existing:
                        connection.execute(
                            _UPDATE_MASTER_PRESENT,
                            [
                                info.ticker,  # yahoo_symbol == ticker (V1 identity)
                                info.company_name,
                                info.exchange,
                                info.sector,
                                info.industry,
                                info.security_type,
                                info.symbol_type,
                                snapshot_month,  # last_seen
                                ticker,  # WHERE
                            ],
                        )
                        tickers_updated += 1
                    else:
                        connection.execute(
                            _INSERT_MASTER,
                            [
                                info.ticker,
                                info.ticker,  # yahoo_symbol == ticker
                                info.company_name,
                                info.exchange,
                                info.sector,
                                info.industry,
                                info.security_type,
                                info.symbol_type,
                                snapshot_month,  # first_seen
                                snapshot_month,  # last_seen
                            ],
                        )
                        tickers_inserted += 1

                # Previously-known tickers absent from this input -> deactivate.
                # Absence alone is not delisting, so delisted_flag is untouched
                # and last_seen is preserved.
                for ticker in existing - accepted.keys():
                    connection.execute(_UPDATE_MASTER_ABSENT, [ticker])
                    tickers_marked_inactive += 1

                # Idempotent snapshot write: clear the month, then insert input.
                connection.execute(_DELETE_SNAPSHOT_MONTH, [snapshot_month])
                for ticker, info in accepted.items():
                    connection.execute(
                        _INSERT_SNAPSHOT,
                        [
                            snapshot_month,
                            info.ticker,
                            info.exchange,
                            info.sector,
                            info.industry,
                            source,
                        ],
                    )

                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

        return {
            "tickers_inserted": tickers_inserted,
            "tickers_updated": tickers_updated,
            "tickers_marked_inactive": tickers_marked_inactive,
            "snapshot_rows": len(accepted),
        }

    def _metadata(
        self,
        *,
        snapshot_month: str,
        db_role: str,
        source: str,
        input_rows: int,
        valid_rows: int = 0,
        skipped_rows: int = 0,
        tickers_inserted: int = 0,
        tickers_updated: int = 0,
        tickers_marked_inactive: int = 0,
        snapshot_rows: int = 0,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "snapshot_month": snapshot_month,
            "db_role": db_role,
            "source": source,
            "input_rows": input_rows,
            "valid_rows": valid_rows,
            "skipped_rows": skipped_rows,
            "tickers_inserted": tickers_inserted,
            "tickers_updated": tickers_updated,
            "tickers_marked_inactive": tickers_marked_inactive,
            "snapshot_rows": snapshot_rows,
        }


__all__ = ["UniverseSnapshotEngine", "METADATA_KEYS", "ALLOWED_DB_ROLES"]
