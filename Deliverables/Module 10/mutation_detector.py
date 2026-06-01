"""Module 10 — Mutation Detector.

Scans already-ingested ``daily_prices`` rows **after** Module 09 validation and
**before** Module 11 feature calculation. Module 10 owns
``daily_prices.adjustment_factor`` derivation and ``daily_prices.mutation_flag``,
and enqueues mutation-related repairs / feature-rebuild entries.

Contract source of truth: ``M10_MUTATION_DETECTOR_SPEC.md``.

Scope (what this module does)
-----------------------------
Given an inclusive ``[start_date, end_date]`` range, for rows whose
``data_quality_status == 'ok'`` it:

- derives ``adjustment_factor = close_adj / close_raw`` where ``close_raw`` is
  non-null and non-zero and ``close_adj`` is non-null; otherwise the derived
  value is ``NULL`` (an existing stored value may therefore be cleared);
- detects explicit splits: rows where ``split_ratio`` is non-null and ``!= 1``;
- sets ``mutation_flag = TRUE`` on detected rows using a strict no-downgrade
  rule (it only ever flips ``FALSE`` -> ``TRUE``, never the reverse);
- for each eligible ticker with at least one detected mutation, inserts one
  ``data_repair_queue`` row (``repair_reason = 'mutation'``) and one
  ``feature_rebuild_log`` row, both with deterministic ``uuid5`` ids and
  insert-or-ignore semantics so re-runs do not duplicate;
- returns a :class:`~app.utils.service_result.ServiceResult`.

Out of scope (owned elsewhere)
------------------------------
This module does **not**: call any provider / vendor or fetch data (it operates
on DB rows only); modify price values or any OHLCV raw/adjusted column,
``dividend_amount``, ``split_ratio``, ``data_quality_status``, ``created_at``
(Module 08 / Module 09); write ``ticker_master``, ``ticker_universe_snapshot``,
``sector_etf_map``, ``simulation.duckdb``, or any feature/step/proposal/outcome/
AI/execution table; process / resolve / update / delete existing
``data_repair_queue`` or ``feature_rebuild_log`` rows (it only inserts); open
DuckDB directly, ``ATTACH``, run DDL, or change the schema.

Historical / retroactive ``close_raw / close_adj`` ratio-discontinuity detection
is intentionally **not** implemented: the frozen project sources define no
discontinuity threshold (open spec gap ``G1``). Only the explicit
``split_ratio != 1`` path plus ``adjustment_factor`` derivation are implemented.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.config import constants
from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Roles this module is allowed to target.
# --------------------------------------------------------------------------- #
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module
# 10 never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are
# accepted; any other value yields a ``failed`` result with no reads/writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# data_quality_status value that gates eligibility (SCHEMA_SPEC.md §5). Module
# 10 processes only rows Module 09 left/escalated as ``ok``.
STATUS_OK: Final[str] = "ok"

# Default split_ratio (SCHEMA_SPEC.md §3.7 — DEFAULT 1). A row is an explicit
# split when split_ratio is non-null AND not equal to this baseline.
_NO_SPLIT_RATIO: Final[float] = 1.0

# data_repair_queue locked constants (SCHEMA_SPEC.md §3.17 / §5; M08 pattern).
# ``mutation`` is an allowed repair_reason in the frozen enum.
_REPAIR_REASON_MUTATION: Final[str] = "mutation"
_REPAIR_STATUS_PENDING: Final[str] = "pending"
_REPAIR_MAX_ATTEMPTS: Final[int] = 3

# feature_rebuild_log locked constants (SCHEMA_SPEC.md §3.18). The frozen enum
# catalog defines no dedicated ``reason``/``status`` domain for this table, so
# the reason mirrors the repair reason (``mutation``) and the status reuses the
# queue ``pending`` value (assumption A2 in the spec).
_REBUILD_REASON_MUTATION: Final[str] = "mutation"
_REBUILD_STATUS_PENDING: Final[str] = "pending"

# Namespace for deterministic uuid5 id derivation (mirrors Module 08 / 09). The
# frozen schema has no UNIQUE constraint on the logical keys, so ids are derived
# deterministically and the insert uses the PRIMARY KEY as the conflict target,
# giving DB-enforced dedup without DDL.
_ID_NAMESPACE: Final[uuid.UUID] = uuid.NAMESPACE_URL


def _repair_id_for(ticker: str, repair_date: date, repair_reason: str) -> str:
    """Return a deterministic ``repair_id`` for a logical repair key.

    The same ``(ticker, repair_date, repair_reason)`` always maps to the same
    ``repair_id`` (matching the Module 08 / 09 pattern), so two inserts of the
    same logical task collide on the ``repair_id`` PRIMARY KEY and the second is
    a DB-level no-op — even across concurrent runs.
    """
    return str(
        uuid.uuid5(
            _ID_NAMESPACE,
            f"data_repair_queue:{ticker}:{repair_date.isoformat()}:{repair_reason}",
        )
    )


def _rebuild_id_for(ticker: str, affected_start: date, reason: str) -> str:
    """Return a deterministic ``rebuild_id`` for a logical rebuild key.

    The same ``(ticker, affected_start, reason)`` always maps to the same
    ``rebuild_id``, so re-runs over identical input never duplicate rebuild-log
    rows (DB-enforced via the ``rebuild_id`` PRIMARY KEY).
    """
    return str(
        uuid.uuid5(
            _ID_NAMESPACE,
            f"feature_rebuild_log:{ticker}:{affected_start.isoformat()}:{reason}",
        )
    )


# The exact metadata key set returned by :meth:`MutationDetector.detect`.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "start_date",
    "end_date",
    "rows_read",
    "rows_processed",
    "rows_skipped_non_ok",
    "adjustment_factors_written",
    "mutation_rows_detected",
    "mutation_flags_written",
    "tickers_with_mutation",
    "repair_queue_enqueued",
    "rebuild_logs_enqueued",
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
    "SELECT ticker, date, close_raw, close_adj, split_ratio, "
    "adjustment_factor, mutation_flag, data_quality_status "
    "FROM daily_prices "
    "WHERE date >= ? AND date <= ? "
    "ORDER BY ticker, date"
)

# Write a derived ``adjustment_factor`` (may be NULL to clear). Only
# ``adjustment_factor`` and the audit ``updated_at`` are set; price/split/status
# columns are untouched. Issued only when the computed value differs from the
# stored one, so ``RETURNING`` yields exactly one row per real change — that is
# how ``adjustment_factors_written`` is counted.
_UPDATE_ADJUSTMENT_FACTOR: Final[str] = (
    "UPDATE daily_prices "
    "SET adjustment_factor = ?, updated_at = CAST(now() AS TIMESTAMP) "
    "WHERE ticker = ? AND date = ? "
    "RETURNING ticker"
)

# Set ``mutation_flag`` TRUE, no-downgrade: the ``mutation_flag = FALSE`` guard
# means a row already TRUE is never rewritten (and TRUE is never lowered to
# FALSE). ``RETURNING`` yields one row per actual FALSE -> TRUE flip, which is
# how ``mutation_flags_written`` is counted. Only ``mutation_flag`` and the
# audit ``updated_at`` are set.
_SET_MUTATION_FLAG: Final[str] = (
    "UPDATE daily_prices "
    "SET mutation_flag = TRUE, updated_at = CAST(now() AS TIMESTAMP) "
    "WHERE ticker = ? AND date = ? AND mutation_flag = FALSE "
    "RETURNING ticker"
)

# Insert one fresh mutation repair task. attempts=0, max_attempts=3,
# status='pending', last_attempt NULL, created_at set on insert, updated_at
# NULL. ``repair_id`` is deterministic (see ``_repair_id_for``), so ``ON
# CONFLICT (repair_id) DO NOTHING`` makes re-inserting the same logical task a
# DB-level no-op. ``RETURNING repair_id`` yields one row per real insert and
# zero per conflict — how ``repair_queue_enqueued`` is counted.
_INSERT_REPAIR: Final[str] = (
    "INSERT INTO data_repair_queue "
    "(repair_id, ticker, repair_date, repair_reason, attempts, max_attempts, "
    " last_attempt, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, 0, ?, NULL, ?, CAST(now() AS TIMESTAMP), NULL) "
    "ON CONFLICT (repair_id) DO NOTHING "
    "RETURNING repair_id"
)

# Insert one fresh feature-rebuild entry. ``affected_end_date`` is left NULL
# (no end is defined by the frozen sources); ``feature_schema_version`` carries
# ``constants.FEATURE_SCHEMA_VERSION``; ``triggered_at`` set on insert;
# ``status`` = 'pending'. ``rebuild_id`` is deterministic, so ``ON CONFLICT
# (rebuild_id) DO NOTHING`` dedups re-runs. ``RETURNING rebuild_id`` yields one
# row per real insert — how ``rebuild_logs_enqueued`` is counted.
_INSERT_REBUILD: Final[str] = (
    "INSERT INTO feature_rebuild_log "
    "(rebuild_id, ticker, reason, affected_start_date, affected_end_date, "
    " feature_schema_version, triggered_at, status) "
    "VALUES (?, ?, ?, ?, NULL, ?, CAST(now() AS TIMESTAMP), ?) "
    "ON CONFLICT (rebuild_id) DO NOTHING "
    "RETURNING rebuild_id"
)


class _PriceRow:
    """One ``daily_prices`` row with the columns Module 10 needs (spec §6).

    Only ``close_raw`` / ``close_adj`` (for adjustment-factor derivation),
    ``split_ratio`` (for explicit-split detection), the current
    ``adjustment_factor`` / ``mutation_flag`` (to decide whether a write is
    needed), and ``data_quality_status`` (eligibility) are carried.
    """

    __slots__ = (
        "ticker",
        "date",
        "close_raw",
        "close_adj",
        "split_ratio",
        "adjustment_factor",
        "mutation_flag",
        "status",
    )

    def __init__(
        self,
        ticker: str,
        date_: date,
        close_raw: float | None,
        close_adj: float | None,
        split_ratio: float | None,
        adjustment_factor: float | None,
        mutation_flag: bool,
        status: str,
    ) -> None:
        self.ticker = ticker
        self.date = date_
        self.close_raw = close_raw
        self.close_adj = close_adj
        self.split_ratio = split_ratio
        self.adjustment_factor = adjustment_factor
        self.mutation_flag = mutation_flag
        self.status = status

    @classmethod
    def from_tuple(cls, row: tuple[Any, ...]) -> _PriceRow:
        """Build from a ``_SELECT_RANGE`` result tuple (positional)."""
        return cls(*row)

    def is_eligible(self) -> bool:
        """Return ``True`` if the row's ``data_quality_status`` is ``ok``."""
        return self.status == STATUS_OK

    def derived_adjustment_factor(self) -> float | None:
        """Return ``close_adj / close_raw`` if derivable, else ``None``.

        Derivable means ``close_raw`` is non-null and non-zero and ``close_adj``
        is non-null. When not derivable the desired value is ``None`` (NULL),
        which may clear a previously stored value.
        """
        if self.close_raw is None or self.close_raw == 0 or self.close_adj is None:
            return None
        return self.close_adj / self.close_raw

    def is_explicit_split(self) -> bool:
        """Return ``True`` if this is an explicit split (``split_ratio != 1``).

        ``split_ratio`` may be NULL (no split information); only a non-null
        value different from the baseline ``1`` is treated as a split mutation.
        """
        return self.split_ratio is not None and self.split_ratio != _NO_SPLIT_RATIO


def _factor_differs(desired: float | None, stored: float | None) -> bool:
    """Return ``True`` if a derived factor differs from the stored one.

    NULL-aware exact comparison. Recomputing ``close_adj / close_raw`` from the
    same stored doubles yields the identical IEEE-754 value, so an unchanged row
    compares equal and is not rewritten on re-run (idempotency).
    """
    if desired is None and stored is None:
        return False
    if desired is None or stored is None:
        return True
    return desired != stored


class MutationDetector:
    """Detect mutations, derive ``adjustment_factor``, enqueue repairs/rebuilds.

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
    def detect(
        self,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Detect mutations in ``daily_prices`` rows in ``[start_date, end_date]``.

        Parameters
        ----------
        start_date, end_date:
            Inclusive ``[start_date, end_date]`` range applied to
            ``daily_prices.date``.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns a ``failed`` result with no reads/writes.
        run_id:
            A fresh ``uuid4`` is minted when ``None`` (mirrors Module 08 / 09).

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals the eligible-row count. ``metadata``
            carries exactly the keys in :data:`METADATA_KEYS` on every return
            path.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()

        log.info(
            "detect start db_role=%s start_date=%s end_date=%s",
            db_role,
            start_iso,
            end_iso,
        )

        # --- db_role guard: prod/debug only, never simulation. No I/O. ----- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 10 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("detect failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- date-range guard: fail before any DB access. ------------------ #
        if start_date > end_date:
            message = (
                f"Invalid date range: start_date {start_iso} > end_date {end_iso}."
            )
            log.error("detect failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        # --- read phase (read-only): load the range's rows. ---------------- #
        try:
            rows = self._read_range(db_role, start_date, end_date)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
            message = f"range read failed: {type(exc).__name__}: {exc}"
            log.error("detect failed: %s", message)
            return self._failed(run_id, message, db_role, start_iso, end_iso)

        rows_read = len(rows)

        # --- compute phase (pure Python, no DB): build write payloads. ----- #
        plan = self._plan(rows)

        log.info(
            "detect computed rows_read=%d rows_processed=%d rows_skipped_non_ok=%d "
            "mutation_rows_detected=%d tickers_with_mutation=%d "
            "adj_factor_changes=%d mutation_flag_candidates=%d",
            rows_read,
            plan.rows_processed,
            plan.rows_skipped_non_ok,
            plan.mutation_rows_detected,
            len(plan.mutation_tickers),
            len(plan.factor_writes),
            len(plan.flag_rows),
        )

        # --- write phase: single transaction across all DB mutations. ------ #
        try:
            counts = self._write(db_role, plan)
        except Exception as exc:  # noqa: BLE001 - surface as failed ServiceResult
            log.error(
                "detect failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            # Durable write counts are 0 (rolled back); read/compute counts kept.
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id=run_id,
                rows_processed=0,
                errors=[f"{type(exc).__name__}: {exc}"],
                metadata=self._metadata(
                    db_role=db_role,
                    start_date=start_iso,
                    end_date=end_iso,
                    rows_read=rows_read,
                    rows_processed=plan.rows_processed,
                    rows_skipped_non_ok=plan.rows_skipped_non_ok,
                    mutation_rows_detected=plan.mutation_rows_detected,
                    tickers_with_mutation=len(plan.mutation_tickers),
                ),
            )

        metadata = self._metadata(
            db_role=db_role,
            start_date=start_iso,
            end_date=end_iso,
            rows_read=rows_read,
            rows_processed=plan.rows_processed,
            rows_skipped_non_ok=plan.rows_skipped_non_ok,
            adjustment_factors_written=counts["adjustment_factors_written"],
            mutation_rows_detected=plan.mutation_rows_detected,
            mutation_flags_written=counts["mutation_flags_written"],
            tickers_with_mutation=len(plan.mutation_tickers),
            repair_queue_enqueued=counts["repair_queue_enqueued"],
            rebuild_logs_enqueued=counts["rebuild_logs_enqueued"],
        )

        log.info(
            "detect done status=success rows_read=%d rows_processed=%d "
            "adjustment_factors_written=%d mutation_flags_written=%d "
            "repair_queue_enqueued=%d rebuild_logs_enqueued=%d",
            rows_read,
            plan.rows_processed,
            counts["adjustment_factors_written"],
            counts["mutation_flags_written"],
            counts["repair_queue_enqueued"],
            counts["rebuild_logs_enqueued"],
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=plan.rows_processed,
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

    def _plan(self, rows: list[_PriceRow]) -> _WritePlan:
        """Build the (pure-Python) write plan from the read rows.

        Eligibility, adjustment-factor derivation, explicit-split detection and
        per-ticker first-mutation-date selection all happen here without any DB
        access. ``rows`` arrive ordered by ``(ticker, date)`` so the first
        detected mutation per ticker is the earliest date.
        """
        rows_processed = 0
        rows_skipped_non_ok = 0
        mutation_rows_detected = 0

        factor_writes: list[tuple[str, date, float | None]] = []
        flag_rows: list[tuple[str, date]] = []
        # ticker -> earliest detected mutation date in range.
        mutation_tickers: dict[str, date] = {}

        for row in rows:
            if not row.is_eligible():
                rows_skipped_non_ok += 1
                continue
            rows_processed += 1

            # Adjustment-factor derivation (independent of mutation detection).
            desired = row.derived_adjustment_factor()
            if _factor_differs(desired, row.adjustment_factor):
                factor_writes.append((row.ticker, row.date, desired))

            # Explicit-split mutation detection.
            if row.is_explicit_split():
                mutation_rows_detected += 1
                if row.mutation_flag is False:
                    flag_rows.append((row.ticker, row.date))
                # Earliest detected date wins (rows are date-ordered per ticker).
                if row.ticker not in mutation_tickers:
                    mutation_tickers[row.ticker] = row.date

        return _WritePlan(
            rows_processed=rows_processed,
            rows_skipped_non_ok=rows_skipped_non_ok,
            mutation_rows_detected=mutation_rows_detected,
            factor_writes=factor_writes,
            flag_rows=flag_rows,
            mutation_tickers=mutation_tickers,
        )

    def _write(self, db_role: str, plan: _WritePlan) -> dict[str, int]:
        """Run all DB mutations inside one transaction; return counts.

        Order inside the single ``BEGIN TRANSACTION ... COMMIT``:
        (1) write derived ``adjustment_factor`` for changed eligible rows;
        (2) set ``mutation_flag`` TRUE for detected rows still ``FALSE``
            (no-downgrade);
        (3) for each ticker with a detected mutation, insert-or-ignore one
            ``mutation`` repair row and one ``feature_rebuild_log`` row keyed on
            the ticker's earliest detected mutation date.

        Any error triggers ``ROLLBACK`` so no partial factor/flag updates and no
        stray repair/rebuild rows survive. When the plan is empty the
        transaction opens and commits with no mutations (counts stay zero).
        """
        adjustment_factors_written = 0
        mutation_flags_written = 0
        repair_queue_enqueued = 0
        rebuild_logs_enqueued = 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                # (1) adjustment_factor writes.
                for ticker, row_date, factor in plan.factor_writes:
                    changed = connection.execute(
                        _UPDATE_ADJUSTMENT_FACTOR,
                        [factor, ticker, row_date],
                    ).fetchall()
                    adjustment_factors_written += len(changed)

                # (2) mutation_flag writes (no-downgrade FALSE -> TRUE).
                for ticker, row_date in plan.flag_rows:
                    changed = connection.execute(
                        _SET_MUTATION_FLAG,
                        [ticker, row_date],
                    ).fetchall()
                    mutation_flags_written += len(changed)

                # (3) per-ticker repair + rebuild enqueue (insert-or-ignore).
                for ticker in sorted(plan.mutation_tickers):
                    first_date = plan.mutation_tickers[ticker]

                    repair_id = _repair_id_for(
                        ticker, first_date, _REPAIR_REASON_MUTATION
                    )
                    returned_repair = connection.execute(
                        _INSERT_REPAIR,
                        [
                            repair_id,
                            ticker,
                            first_date,
                            _REPAIR_REASON_MUTATION,
                            _REPAIR_MAX_ATTEMPTS,
                            _REPAIR_STATUS_PENDING,
                        ],
                    ).fetchall()
                    repair_queue_enqueued += len(returned_repair)

                    rebuild_id = _rebuild_id_for(
                        ticker, first_date, _REBUILD_REASON_MUTATION
                    )
                    returned_rebuild = connection.execute(
                        _INSERT_REBUILD,
                        [
                            rebuild_id,
                            ticker,
                            _REBUILD_REASON_MUTATION,
                            first_date,
                            constants.FEATURE_SCHEMA_VERSION,
                            _REBUILD_STATUS_PENDING,
                        ],
                    ).fetchall()
                    rebuild_logs_enqueued += len(returned_rebuild)

                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

        return {
            "adjustment_factors_written": adjustment_factors_written,
            "mutation_flags_written": mutation_flags_written,
            "repair_queue_enqueued": repair_queue_enqueued,
            "rebuild_logs_enqueued": rebuild_logs_enqueued,
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
        rows_read: int = 0,
        rows_processed: int = 0,
        rows_skipped_non_ok: int = 0,
        adjustment_factors_written: int = 0,
        mutation_rows_detected: int = 0,
        mutation_flags_written: int = 0,
        tickers_with_mutation: int = 0,
        repair_queue_enqueued: int = 0,
        rebuild_logs_enqueued: int = 0,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "db_role": db_role,
            "start_date": start_date,
            "end_date": end_date,
            "rows_read": rows_read,
            "rows_processed": rows_processed,
            "rows_skipped_non_ok": rows_skipped_non_ok,
            "adjustment_factors_written": adjustment_factors_written,
            "mutation_rows_detected": mutation_rows_detected,
            "mutation_flags_written": mutation_flags_written,
            "tickers_with_mutation": tickers_with_mutation,
            "repair_queue_enqueued": repair_queue_enqueued,
            "rebuild_logs_enqueued": rebuild_logs_enqueued,
        }


class _WritePlan:
    """Computed, DB-free plan of what the write phase must do."""

    __slots__ = (
        "rows_processed",
        "rows_skipped_non_ok",
        "mutation_rows_detected",
        "factor_writes",
        "flag_rows",
        "mutation_tickers",
    )

    def __init__(
        self,
        rows_processed: int,
        rows_skipped_non_ok: int,
        mutation_rows_detected: int,
        factor_writes: list[tuple[str, date, float | None]],
        flag_rows: list[tuple[str, date]],
        mutation_tickers: dict[str, date],
    ) -> None:
        self.rows_processed = rows_processed
        self.rows_skipped_non_ok = rows_skipped_non_ok
        self.mutation_rows_detected = mutation_rows_detected
        self.factor_writes = factor_writes
        self.flag_rows = flag_rows
        self.mutation_tickers = mutation_tickers


__all__ = [
    "MutationDetector",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "_repair_id_for",
    "_rebuild_id_for",
]
