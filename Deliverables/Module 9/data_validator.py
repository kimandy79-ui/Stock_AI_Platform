"""Module 09 — Data Validator.

Validates already-ingested ``daily_prices`` rows **after** Module 08 ingestion
and **before** Module 10 mutation detection / Module 11 feature calculation
(pipeline step 6, "Validate data"; ``MASTER_SPEC.md`` §4). Module 09 owns the
real ``daily_prices.data_quality_status`` value — Module 08 wrote the
placeholder ``"ok"`` — and enqueues validation repairs into
``data_repair_queue``.

Contract source of truth: ``M09_DATA_VALIDATOR_SPEC.md``.

Scope (what this module does)
-----------------------------
Given an inclusive ``[start_date, end_date]`` range, it:

- reads ``daily_prices`` rows in range (read-only);
- evaluates the structural OHLCV integrity checks defined in the spec rule
  matrix (null OHLC, ``high < low``, open/close outside ``[low, high]``,
  non-positive price, negative volume) over the raw and adjusted tuples;
- escalates ``daily_prices.data_quality_status`` to ``failed`` for all failing
  rows, using a strict no-downgrade rule (never lowers an existing worse status);
- enqueues one ``bad_ohlc`` ``data_repair_queue`` row per OHLC-invalid row
  (rules 1–6) with the Module 08 deterministic-``repair_id`` insert-or-ignore
  pattern; negative-volume rows (rule 7) are escalated but **not** enqueued —
  no suitable ``repair_reason`` exists in the frozen enum (open spec gap G6);
- returns a :class:`~app.utils.service_result.ServiceResult`.

Out of scope (owned elsewhere)
------------------------------
This module does **not**: call any provider / vendor or fetch data (it validates
DB rows only); modify price values or any OHLCV raw/adjusted column,
``dividend_amount``, ``split_ratio``, ``adjustment_factor``, or
``mutation_flag``; detect splits / mutations / large jumps (Module 10);
calculate features (Module 11); write ``ticker_master``,
``ticker_universe_snapshot``, ``sector_etf_map``, ``simulation.duckdb``, or any
feature/step/proposal/outcome/AI/execution table; process / resolve / update /
delete existing ``data_repair_queue`` rows (it only inserts — it is not the
repair processor); open DuckDB directly, ``ATTACH``, run DDL, or change the
schema.

Threshold- or calendar-dependent checks (missing trading days, large jumps,
stale coverage) are intentionally **not** implemented: the frozen project
sources define no thresholds, status-transition table, or per-check
repair-reason mapping for them. They are recorded as open spec gaps in the
module spec rather than invented here.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Roles this module is allowed to target.
# --------------------------------------------------------------------------- #
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module
# 09 never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are
# accepted; any other value yields a ``failed`` result with no reads/writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# data_quality_status enum values (SCHEMA_SPEC.md §5). Severity precedence
# (spec §7.1, assumption A1): quarantined > failed > suspect > warning > ok.
STATUS_OK: Final[str] = "ok"
STATUS_WARNING: Final[str] = "warning"
STATUS_SUSPECT: Final[str] = "suspect"
STATUS_FAILED: Final[str] = "failed"
STATUS_QUARANTINED: Final[str] = "quarantined"

# Numeric severity ranks used by the no-downgrade escalation (higher = worse).
STATUS_SEVERITY: Final[dict[str, int]] = {
    STATUS_OK: 0,
    STATUS_WARNING: 1,
    STATUS_SUSPECT: 2,
    STATUS_FAILED: 3,
    STATUS_QUARANTINED: 4,
}

# data_repair_queue locked constants (SCHEMA_SPEC.md §3.17 enum / M08 pattern).
# Every implemented (repairable) validation check maps to ``bad_ohlc`` — the
# only validation-related reason in the frozen enum. No new enum is created.
_REPAIR_REASON_BAD_OHLC: Final[str] = "bad_ohlc"
_REPAIR_STATUS_PENDING: Final[str] = "pending"
_REPAIR_MAX_ATTEMPTS: Final[int] = 3

# Namespace for deterministic repair_id derivation. The frozen schema has no
# UNIQUE(ticker, repair_date, repair_reason) constraint, so insert-or-ignore on
# the logical key cannot use ON CONFLICT over that triple. Instead repair_id is
# derived deterministically (uuid5) from the logical key, and the insert uses the
# existing ``repair_id`` PRIMARY KEY as the conflict target — giving DB-enforced
# dedup without DDL (mirrors Module 08; M08 spec §16).
_REPAIR_ID_NAMESPACE: Final[uuid.UUID] = uuid.NAMESPACE_URL


def _repair_id_for(ticker: str, repair_date: date, repair_reason: str) -> str:
    """Return a deterministic ``repair_id`` for a logical repair key.

    The same ``(ticker, repair_date, repair_reason)`` always maps to the same
    ``repair_id``, so two inserts of the same logical task collide on the
    ``repair_id`` PRIMARY KEY and the second is a DB-level no-op — even across
    concurrent runs (no application-side read-then-write race window).
    """
    return str(
        uuid.uuid5(
            _REPAIR_ID_NAMESPACE,
            f"data_repair_queue:{ticker}:{repair_date.isoformat()}:{repair_reason}",
        )
    )


# The exact metadata key set returned by :meth:`DataValidator.validate`.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "start_date",
    "end_date",
    "rows_validated",
    "rows_ok",
    "rows_failed",
    "status_updates_written",
    "repair_queue_enqueued",
)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (operates only on existing tables; no DDL).
# --------------------------------------------------------------------------- #
# Read the range's rows. Column order is consumed positionally by
# ``_PriceRow.from_tuple``; keep the two in sync.
_SELECT_RANGE: Final[str] = (
    "SELECT ticker, date, "
    "open_raw, high_raw, low_raw, close_raw, volume_raw, "
    "open_adj, high_adj, low_adj, close_adj, volume_adj, "
    "data_quality_status "
    "FROM daily_prices "
    "WHERE date >= ? AND date <= ? "
    "ORDER BY ticker, date"
)

# Escalate one row's status, no-downgrade: overwrite only when the new status is
# *strictly more severe* than the stored one. Severity is encoded inline as a
# CASE so the comparison happens in the DB (atomic with the write) and a
# concurrently-worsened row is never lowered. ``updated_at`` is refreshed as an
# audit timestamp (an allowed validation-owned column on the row); price columns
# and ``mutation_flag`` are untouched. ``RETURNING`` yields one row per actual
# change, which is how ``status_updates_written`` is counted.
#
# The ``?`` parameters are: new_status, new_status_severity (twice via the WHERE
# subexpression is avoided by passing the severity explicitly), ticker, date.
_ESCALATE_STATUS: Final[str] = (
    "UPDATE daily_prices "
    "SET data_quality_status = ?, updated_at = CAST(now() AS TIMESTAMP) "
    "WHERE ticker = ? AND date = ? "
    "AND ? > CASE data_quality_status "
    "WHEN 'ok' THEN 0 WHEN 'warning' THEN 1 WHEN 'suspect' THEN 2 "
    "WHEN 'failed' THEN 3 WHEN 'quarantined' THEN 4 ELSE 0 END "
    "RETURNING ticker"
)

# Insert one fresh repair task. attempts=0, max_attempts=3, status='pending',
# last_attempt NULL, created_at set on insert, updated_at NULL. ``repair_id`` is
# deterministic (see ``_repair_id_for``), so ``ON CONFLICT (repair_id) DO
# NOTHING`` makes re-inserting the same logical task a DB-level no-op even under
# concurrent runs. ``RETURNING repair_id`` yields one row per actual insert and
# zero per conflict, which is how ``repair_queue_enqueued`` is counted.
_INSERT_REPAIR: Final[str] = (
    "INSERT INTO data_repair_queue "
    "(repair_id, ticker, repair_date, repair_reason, attempts, max_attempts, "
    " last_attempt, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, 0, ?, NULL, ?, CAST(now() AS TIMESTAMP), NULL) "
    "ON CONFLICT (repair_id) DO NOTHING "
    "RETURNING repair_id"
)


class _PriceRow:
    """One ``daily_prices`` row, with the validity check (spec §6).

    Only the columns needed for validation are carried. Price/volume operands
    may be ``None``; a predicate is evaluated only when its operands are
    non-null (a null operand is handled by the dedicated null-OHLC check, not by
    silently passing the comparison).
    """

    __slots__ = (
        "ticker",
        "date",
        "open_raw",
        "high_raw",
        "low_raw",
        "close_raw",
        "volume_raw",
        "open_adj",
        "high_adj",
        "low_adj",
        "close_adj",
        "volume_adj",
        "status",
    )

    def __init__(
        self,
        ticker: str,
        date_: date,
        open_raw: float | None,
        high_raw: float | None,
        low_raw: float | None,
        close_raw: float | None,
        volume_raw: int | None,
        open_adj: float | None,
        high_adj: float | None,
        low_adj: float | None,
        close_adj: float | None,
        volume_adj: int | None,
        status: str,
    ) -> None:
        self.ticker = ticker
        self.date = date_
        self.open_raw = open_raw
        self.high_raw = high_raw
        self.low_raw = low_raw
        self.close_raw = close_raw
        self.volume_raw = volume_raw
        self.open_adj = open_adj
        self.high_adj = high_adj
        self.low_adj = low_adj
        self.close_adj = close_adj
        self.volume_adj = volume_adj
        self.status = status

    @classmethod
    def from_tuple(cls, row: tuple[Any, ...]) -> _PriceRow:
        """Build from a ``_SELECT_RANGE`` result tuple (positional)."""
        return cls(*row)

    def _ohlc_prices(self) -> tuple[tuple[float | None, ...], tuple[float | None, ...]]:
        """Return ``(raw_ohlc, adj_ohlc)`` as ``(open, high, low, close)`` tuples."""
        raw = (self.open_raw, self.high_raw, self.low_raw, self.close_raw)
        adj = (self.open_adj, self.high_adj, self.low_adj, self.close_adj)
        return raw, adj

    def ohlc_invalid(self) -> bool:
        """Return ``True`` if the row fails any OHLC structural check (rules 1–6).

        These checks map to ``repair_reason = 'bad_ohlc'`` in the frozen enum.
        A row is OHLC-invalid when any of:
        1. any of the 8 OHLC price columns IS NULL;
        2/3. ``high < low`` on raw or adjusted;
        4/5. ``open``/``close`` outside ``[low, high]`` on raw or adjusted;
        6. any non-null OHLC price ``<= 0``.
        """
        raw, adj = self._ohlc_prices()

        # Rule 1: null OHLC (any of the 8 raw/adjusted price columns NULL).
        for value in (*raw, *adj):
            if value is None:
                return True

        # At this point all 8 OHLC prices are non-null.
        for o, h, l, c in (raw, adj):
            # Rules 2/3: high < low.
            if h < l:
                return True
            # Rules 4/5: open/close outside [low, high].
            if not (l <= o <= h):
                return True
            if not (l <= c <= h):
                return True
            # Rule 6: non-positive price.
            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                return True

        return False

    def volume_invalid(self) -> bool:
        """Return ``True`` if the row has a negative volume value (rule 7).

        Negative volume is structurally invalid and escalates
        ``data_quality_status`` to ``failed``. However, no suitable
        ``repair_reason`` exists in the frozen enum for this condition —
        ``bad_ohlc`` covers price-column invalidity only — so no repair row
        is enqueued. Documented as open spec gap G6. Operands may be NULL;
        only non-null values are checked.
        """
        if self.volume_raw is not None and self.volume_raw < 0:
            return True
        if self.volume_adj is not None and self.volume_adj < 0:
            return True
        return False

    def is_invalid(self) -> bool:
        """Return ``True`` if the row fails any implemented structural check.

        Convenience composite of :meth:`ohlc_invalid` and
        :meth:`volume_invalid`. Call those directly when the failure category
        matters (e.g. to decide whether to enqueue a repair).
        """
        return self.ohlc_invalid() or self.volume_invalid()


class DataValidator:
    """Validate ``daily_prices`` rows, set real ``data_quality_status``, enqueue repairs.

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
    def validate(
        self,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Validate ``daily_prices`` rows in ``[start_date, end_date]``.

        Parameters
        ----------
        start_date, end_date:
            Inclusive ``[start_date, end_date]`` range applied to
            ``daily_prices.date``.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns a ``failed`` result with no reads/writes.
        run_id:
            A fresh ``uuid4`` is minted when ``None`` (mirrors Module 08).

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``rows_validated``. ``metadata`` carries
            exactly the keys in :data:`METADATA_KEYS` on every return path.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()

        log.info(
            "validate start db_role=%s start_date=%s end_date=%s",
            db_role,
            start_iso,
            end_iso,
        )

        # --- db_role guard: prod/debug only, never simulation. No I/O. ----- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 09 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("validate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- date-range guard: fail before any DB access. ------------------ #
        if start_date > end_date:
            message = (
                f"Invalid date range: start_date {start_iso} > end_date {end_iso}."
            )
            log.error("validate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- read phase (read-only): load the range's rows. ---------------- #
        try:
            rows = self._read_range(db_role, start_date, end_date)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
            message = f"range read failed: {type(exc).__name__}: {exc}"
            log.error("validate failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        rows_validated = len(rows)

        # --- compute phase (pure Python, no DB): partition rows. ----------- #
        # OHLC-invalid rows (rules 1–6): escalate status AND enqueue bad_ohlc.
        # Volume-invalid rows (rule 7): escalate status only — no suitable
        # repair_reason in the frozen enum (open spec gap G6).
        ohlc_keys: list[tuple[str, date]] = [
            (row.ticker, row.date) for row in rows if row.ohlc_invalid()
        ]
        # Volume-only failures: invalid but NOT already caught by ohlc_invalid.
        vol_keys: list[tuple[str, date]] = [
            (row.ticker, row.date)
            for row in rows
            if not row.ohlc_invalid() and row.volume_invalid()
        ]
        rows_failed = len(ohlc_keys) + len(vol_keys)
        rows_ok = rows_validated - rows_failed
        log.info(
            "validate computed rows_validated=%d rows_ok=%d rows_failed=%d "
            "(ohlc_invalid=%d volume_only_invalid=%d)",
            rows_validated,
            rows_ok,
            rows_failed,
            len(ohlc_keys),
            len(vol_keys),
        )

        # --- write phase: single transaction across all DB mutations. ------ #
        try:
            counts = self._write(db_role, ohlc_keys, vol_keys)
        except Exception as exc:  # noqa: BLE001 - surface as failed ServiceResult
            log.error(
                "validate failed during write (rolled back): %s: %s",
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
                    rows_validated=rows_validated,
                    rows_ok=rows_ok,
                    rows_failed=rows_failed,
                ),
            )

        metadata = self._metadata(
            db_role=db_role,
            start_date=start_iso,
            end_date=end_iso,
            rows_validated=rows_validated,
            rows_ok=rows_ok,
            rows_failed=rows_failed,
            status_updates_written=counts["status_updates_written"],
            repair_queue_enqueued=counts["repair_queue_enqueued"],
        )

        log.info(
            "validate done status=success rows_validated=%d rows_failed=%d "
            "status_updates_written=%d repair_queue_enqueued=%d",
            rows_validated,
            rows_failed,
            counts["status_updates_written"],
            counts["repair_queue_enqueued"],
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=rows_validated,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers.
    # ------------------------------------------------------------------ #
    def _read_range(
        self, db_role: str, start_date: date, end_date: date
    ) -> list[_PriceRow]:
        """Return ``daily_prices`` rows in range (read-only, connection closed).

        Reads through the approved DB manager in a short read-only connection
        that is closed before the compute phase, so no transaction is held open
        during computation.
        """
        connection = self._db.connect(db_role, read_only=True)
        try:
            raw_rows = connection.execute(
                _SELECT_RANGE, [start_date, end_date]
            ).fetchall()
        finally:
            connection.close()
        return [_PriceRow.from_tuple(row) for row in raw_rows]

    def _write(
        self,
        db_role: str,
        ohlc_keys: list[tuple[str, date]],
        vol_keys: list[tuple[str, date]],
    ) -> dict[str, int]:
        """Run all DB mutations inside one transaction; return counts.

        Order inside the single ``BEGIN TRANSACTION ... COMMIT``:
        (1) escalate ``data_quality_status`` to ``failed`` for each OHLC-invalid
            row (no-downgrade), then insert-or-ignore one ``bad_ohlc`` repair
            per such row;
        (2) escalate ``data_quality_status`` to ``failed`` for each
            volume-only-invalid row (no-downgrade), with **no repair enqueue** —
            no suitable ``repair_reason`` exists in the frozen enum (gap G6).

        Any error triggers ``ROLLBACK`` so no partial status updates and no
        stray repair rows survive. When both lists are empty the transaction
        opens and commits with no mutations (counts stay zero).
        """
        status_updates_written = 0
        repair_queue_enqueued = 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                failed_severity = STATUS_SEVERITY[STATUS_FAILED]

                # (1) OHLC-invalid: escalate + enqueue bad_ohlc repair.
                seen_repair: set[str] = set()
                for ticker, row_date in ohlc_keys:
                    changed = connection.execute(
                        _ESCALATE_STATUS,
                        [STATUS_FAILED, ticker, row_date, failed_severity],
                    ).fetchall()
                    status_updates_written += len(changed)

                    repair_id = _repair_id_for(
                        ticker, row_date, _REPAIR_REASON_BAD_OHLC
                    )
                    if repair_id in seen_repair:
                        continue  # defensive; daily_prices PK prevents dups
                    seen_repair.add(repair_id)
                    returned = connection.execute(
                        _INSERT_REPAIR,
                        [
                            repair_id,
                            ticker,
                            row_date,
                            _REPAIR_REASON_BAD_OHLC,
                            _REPAIR_MAX_ATTEMPTS,
                            _REPAIR_STATUS_PENDING,
                        ],
                    ).fetchall()
                    repair_queue_enqueued += len(returned)

                # (2) Volume-only-invalid: escalate only, no repair enqueue
                #     (no suitable repair_reason in frozen enum — gap G6).
                for ticker, row_date in vol_keys:
                    changed = connection.execute(
                        _ESCALATE_STATUS,
                        [STATUS_FAILED, ticker, row_date, failed_severity],
                    ).fetchall()
                    status_updates_written += len(changed)

                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

        return {
            "status_updates_written": status_updates_written,
            "repair_queue_enqueued": repair_queue_enqueued,
        }

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
        rows_validated: int = 0,
        rows_ok: int = 0,
        rows_failed: int = 0,
        status_updates_written: int = 0,
        repair_queue_enqueued: int = 0,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "db_role": db_role,
            "start_date": start_date,
            "end_date": end_date,
            "rows_validated": rows_validated,
            "rows_ok": rows_ok,
            "rows_failed": rows_failed,
            "status_updates_written": status_updates_written,
            "repair_queue_enqueued": repair_queue_enqueued,
        }


__all__ = [
    "DataValidator",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "STATUS_SEVERITY",
    "_repair_id_for",
]
