"""Module 16 — Outcome Queue (setup-mode).

Two services that turn Step 5 proposals into realized ``signal_outcomes``:

``OutcomeQueueCreator``
    For one ``signal_date`` / ``setup_config_id`` it reads every Step 5
    proposal that is in the raw OR diversified Top-N
    (``in_raw_top_n OR in_diversified_top_n`` — AD-22.13) and enqueues one
    ``outcome_tracking_queue`` row per outcome horizon
    (``constants.OUTCOME_HORIZONS_BD`` = ``[5, 10, 20, 40]``). Inserts are
    idempotent on a deterministic ``tracking_id``, so reruns are silent no-ops.

``OutcomeQueueProcessor``
    For one ``run_date`` it processes every ``pending`` queue row whose
    ``eval_date`` has been reached, computes slippage-adjusted entry prices,
    horizon returns, 40bd MFE/MAE, stop_hit/target_hit, realized R-multiple and
    the ``earnings_within_window`` flag (AD-22.14: ``entry_price_sim`` is the
    denominator for every performance figure), and upserts one
    ``signal_outcomes`` row per queue row. Missing entry-date prices increment a
    repair counter and mark the row ``unresolvable`` after three attempts.

Setup-mode contract: signal_outcomes carries ``setup_config_id``, ``setup_type``,
``risk_label``, ``stop_price_raw``, ``target_price_raw``, ``stop_hit``,
``target_hit`` (AD-22.21). The old ``strategy_config_id`` key is retired.

Contract source of truth: ``01b_SCHEMA_AND_DATA.md`` for the
``outcome_tracking_queue`` / ``signal_outcomes`` / ``step5_proposals`` /
``step4_analysis`` / ``daily_prices`` / ``earnings_calendar`` schema,
``01c_FORMULAS_AND_CONFIGS.md`` §64 for the entry / return / MFE / MAE /
R-multiple / stop_hit / target_hit formulas, ``02b_ARCHITECTURE_DECISIONS.md``
AD-22.13 / AD-22.14 / AD-22.21 / AD-22.23.
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
# Roles this module is allowed to target (never simulation).
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# Queue / outcome status vocabulary (subset used by Module 16).
QUEUE_PENDING: Final[str] = "pending"
QUEUE_DONE: Final[str] = "done"
QUEUE_UNRESOLVABLE: Final[str] = "unresolvable"

OUTCOME_COMPLETE: Final[str] = "complete"
OUTCOME_PARTIAL: Final[str] = "partial"

# Repair budget for a missing entry-date price (FORMULAS §64: 3 business days).
MAX_REPAIR_ATTEMPTS: Final[int] = 3

# Deterministic-id prefixes (uuid5 over NAMESPACE_URL).
_TRACKING_ID_PREFIX: Final[str] = "outcome_tracking_queue"
_OUTCOME_ID_PREFIX: Final[str] = "signal_outcomes"

# Exact metadata key sets (one per public API; returned on every path).
ENQUEUE_METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "signal_date",
    "setup_config_id",
    "run_id",
    "proposals_read",
    "rows_enqueued",
)
PROCESS_METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "run_date",
    "run_id",
    "queue_rows_read",
    "outcomes_written",
    "unresolvable_count",
    "repair_incremented_count",
)


# --------------------------------------------------------------------------- #
# Injection hooks (kept off the public ``__init__`` signatures).
# --------------------------------------------------------------------------- #
def _default_calendar() -> Any:
    """Return the project NYSE trading-calendar utility."""
    from app.utils import trading_calendar
    return trading_calendar


class _DbManagerLike(Protocol):
    """Minimal hook the services need from the DB manager (for injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class _ConfigError(ValueError):
    """Raised internally when ``setup_config`` is missing / invalid a key."""


# --------------------------------------------------------------------------- #
# SQL (operates only on existing objects; no DDL).
# --------------------------------------------------------------------------- #
# Eligible proposals for enqueue: raw OR diversified Top-N (AD-22.13).
# Uses setup_config_id (setup-mode column name, AD-22.21).
_SELECT_ELIGIBLE_PROPOSALS: Final[str] = (
    "SELECT proposal_id, ticker "
    "FROM step5_proposals "
    "WHERE signal_date = ? "
    "  AND setup_config_id = ? "
    "  AND (in_raw_top_n = TRUE OR in_diversified_top_n = TRUE) "
    "ORDER BY proposal_id"
)

# Idempotent queue insert.
_INSERT_QUEUE_ROW: Final[str] = (
    "INSERT INTO outcome_tracking_queue "
    "(tracking_id, proposal_id, ticker, signal_date, entry_date, eval_date, "
    " horizon_bd, status, repair_attempts, last_repair_attempt, created_at, "
    " completed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, "
    " CAST(now() AS TIMESTAMP), NULL) "
    "ON CONFLICT (tracking_id) DO NOTHING "
    "RETURNING tracking_id"
)

# Pending, due queue rows for a run_date.
_SELECT_DUE_QUEUE_ROWS: Final[str] = (
    "SELECT tracking_id, proposal_id, ticker, signal_date, entry_date, "
    "       eval_date, horizon_bd, repair_attempts "
    "FROM outcome_tracking_queue "
    "WHERE status = 'pending' AND eval_date <= ? "
    "ORDER BY tracking_id"
)

# Read setup_config_id, setup_type, risk_label, stop/target from proposal.
_SELECT_PROPOSAL_SETUP_FIELDS: Final[str] = (
    "SELECT setup_config_id, setup_type, risk_label, stop_price_raw, target_price_raw "
    "FROM step5_proposals WHERE proposal_id = ?"
)

_SELECT_OPEN_RAW: Final[str] = (
    "SELECT open_raw FROM daily_prices WHERE ticker = ? AND date = ?"
)

_SELECT_CLOSE_ADJ: Final[str] = (
    "SELECT close_adj FROM daily_prices WHERE ticker = ? AND date = ?"
)

# High/low candles for stop_hit/target_hit and MFE/MAE.
_SELECT_WINDOW_CANDLES: Final[str] = (
    "SELECT date, high_adj, low_adj, high_raw, low_raw FROM daily_prices "
    "WHERE ticker = ? AND date BETWEEN ? AND ?"
)

# Earnings in-window counts.
_SELECT_EARNINGS: Final[str] = (
    "SELECT "
    "  COUNT(*) AS total, "
    "  COUNT(*) FILTER (WHERE earnings_date > ? AND earnings_date <= ?) "
    "    AS in_window "
    "FROM earnings_calendar WHERE ticker = ?"
)

_UPSERT_OUTCOME: Final[str] = (
    "INSERT INTO signal_outcomes "
    "(outcome_id, proposal_id, ticker, setup_config_id, setup_type, risk_label, "
    " signal_date, entry_date, entry_price_raw, entry_price_sim, "
    " stop_price_raw, target_price_raw, "
    " return_5bd_pct, return_10bd_pct, return_20bd_pct, return_40bd_pct, "
    " mfe_40bd_pct, mae_40bd_pct, stop_hit, target_hit, "
    " realized_r_multiple, earnings_within_window, "
    " cross_fold_outcome, outcome_status, calculated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    " FALSE, ?, CAST(now() AS TIMESTAMP)) "
    "ON CONFLICT (outcome_id) DO UPDATE SET "
    "  proposal_id = excluded.proposal_id, "
    "  ticker = excluded.ticker, "
    "  setup_config_id = excluded.setup_config_id, "
    "  setup_type = excluded.setup_type, "
    "  risk_label = excluded.risk_label, "
    "  signal_date = excluded.signal_date, "
    "  entry_date = excluded.entry_date, "
    "  entry_price_raw = excluded.entry_price_raw, "
    "  entry_price_sim = excluded.entry_price_sim, "
    "  stop_price_raw = excluded.stop_price_raw, "
    "  target_price_raw = excluded.target_price_raw, "
    "  return_5bd_pct = excluded.return_5bd_pct, "
    "  return_10bd_pct = excluded.return_10bd_pct, "
    "  return_20bd_pct = excluded.return_20bd_pct, "
    "  return_40bd_pct = excluded.return_40bd_pct, "
    "  mfe_40bd_pct = excluded.mfe_40bd_pct, "
    "  mae_40bd_pct = excluded.mae_40bd_pct, "
    "  stop_hit = excluded.stop_hit, "
    "  target_hit = excluded.target_hit, "
    "  realized_r_multiple = excluded.realized_r_multiple, "
    "  earnings_within_window = excluded.earnings_within_window, "
    "  outcome_status = excluded.outcome_status, "
    "  calculated_at = excluded.calculated_at"
)

_UPDATE_QUEUE_DONE: Final[str] = (
    "UPDATE outcome_tracking_queue "
    "SET status = 'done', completed_at = CAST(now() AS TIMESTAMP) "
    "WHERE tracking_id = ?"
)

_UPDATE_QUEUE_REPAIR_PENDING: Final[str] = (
    "UPDATE outcome_tracking_queue "
    "SET repair_attempts = ?, last_repair_attempt = CAST(now() AS TIMESTAMP) "
    "WHERE tracking_id = ?"
)

_UPDATE_QUEUE_REPAIR_UNRESOLVABLE: Final[str] = (
    "UPDATE outcome_tracking_queue "
    "SET repair_attempts = ?, last_repair_attempt = CAST(now() AS TIMESTAMP), "
    "    status = 'unresolvable' "
    "WHERE tracking_id = ?"
)


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #
def _f(value: Any) -> float | None:
    """Coerce a DB cell to ``float`` or ``None`` (NaN -> ``None``)."""
    if value is None:
        return None
    fv = float(value)
    if fv != fv:  # NaN
        return None
    return fv


def _tracking_id_for(proposal_id: str, horizon_bd: int) -> str:
    """Deterministic ``tracking_id`` for a proposal / horizon pair."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{_TRACKING_ID_PREFIX}:{proposal_id}:{horizon_bd}",
        )
    )


def _outcome_id_for(proposal_id: str, horizon_bd: int) -> str:
    """Deterministic ``outcome_id`` for a proposal / horizon pair."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{_OUTCOME_ID_PREFIX}:{proposal_id}:{horizon_bd}",
        )
    )


def _validate_config(setup_config: dict) -> float:
    """Validate ``simulation.slippage_bps`` and return it as ``float``.

    Raises
    ------
    _ConfigError
        If ``setup_config`` is not a dict, the ``simulation`` section is
        missing / not a dict, or ``slippage_bps`` is missing / non-numeric /
        negative.
    """
    if not isinstance(setup_config, dict):
        raise _ConfigError("setup_config must be a dict")

    sim = setup_config.get("simulation")
    if not isinstance(sim, dict):
        raise _ConfigError("missing config section simulation")

    if "slippage_bps" not in sim:
        raise _ConfigError("missing config key simulation.slippage_bps")
    value = sim["slippage_bps"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ConfigError("config key simulation.slippage_bps must be numeric")
    fvalue = float(value)
    if fvalue != fvalue:  # NaN
        raise _ConfigError("config key simulation.slippage_bps must be numeric")
    if fvalue < 0:
        raise _ConfigError("config key simulation.slippage_bps must be >= 0")
    return fvalue


# --------------------------------------------------------------------------- #
# Outcome queue creator.
# --------------------------------------------------------------------------- #
class OutcomeQueueCreator:
    """Enqueue ``outcome_tracking_queue`` rows for raw/diversified Top-N names.

    The optional ``db_manager`` argument exists only for test injection; when
    ``None`` the approved :mod:`app.database.duckdb_manager` is used.
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )

    def enqueue(
        self,
        signal_date: date,
        setup_config_id: str,
        setup_config: dict,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Create queue rows for every eligible proposal × outcome horizon.

        Parameters
        ----------
        signal_date:
            Proposal signal date; only ``step5_proposals`` with this
            ``signal_date`` / ``setup_config_id`` are considered.
        setup_config_id:
            Setup config id used in the eligibility filter (AD-22.21).
        setup_config:
            Parsed setup-config JSON. ``simulation.slippage_bps`` is validated
            before any DB access.
        db_role:
            ``"prod"`` or ``"debug"`` only.
        run_id:
            A fresh ``uuid4`` is minted when ``None``.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        signal_iso = signal_date.isoformat()
        log.info(
            "enqueue start db_role=%s signal_date=%s setup_config_id=%s",
            db_role,
            signal_iso,
            setup_config_id,
        )

        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 16 targets only {list(ALLOWED_DB_ROLES)}."
            )
            log.error("enqueue failed: %s", message)
            return self._enqueue_failed(
                run_id, db_role, signal_iso, setup_config_id, message
            )

        try:
            _validate_config(setup_config)
        except _ConfigError as exc:
            log.error("enqueue failed: %s", exc)
            return self._enqueue_failed(
                run_id, db_role, signal_iso, setup_config_id, str(exc)
            )

        try:
            proposals = self._read_eligible(db_role, signal_date, setup_config_id)
        except Exception as exc:  # noqa: BLE001
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("enqueue failed: %s", message)
            return self._enqueue_failed(
                run_id, db_role, signal_iso, setup_config_id, message
            )

        proposals_read = len(proposals)
        if proposals_read == 0:
            log.info("enqueue done: no eligible proposals for %s", signal_iso)
            return self._enqueue_success(
                run_id, db_role, signal_iso, setup_config_id,
                proposals_read=0, rows_enqueued=0,
            )

        cal = _default_calendar()
        entry_date = cal.next_trading_day(signal_date)
        eval_by_horizon = {
            h: cal.add_trading_days(entry_date, h)
            for h in constants.OUTCOME_HORIZONS_BD
        }

        plan: list[dict[str, Any]] = []
        for proposal in proposals:
            for horizon in constants.OUTCOME_HORIZONS_BD:
                plan.append(
                    {
                        "tracking_id": _tracking_id_for(proposal["proposal_id"], horizon),
                        "proposal_id": proposal["proposal_id"],
                        "ticker": proposal["ticker"],
                        "signal_date": signal_date,
                        "entry_date": entry_date,
                        "eval_date": eval_by_horizon[horizon],
                        "horizon_bd": horizon,
                    }
                )

        try:
            rows_enqueued = self._write_queue(db_role, plan)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "enqueue failed during write (rolled back): %s: %s",
                type(exc).__name__, exc,
            )
            return self._enqueue_failed(
                run_id, db_role, signal_iso, setup_config_id,
                f"{type(exc).__name__}: {exc}", proposals_read=proposals_read,
            )

        log.info("enqueue done proposals_read=%d rows_enqueued=%d", proposals_read, rows_enqueued)
        return self._enqueue_success(
            run_id, db_role, signal_iso, setup_config_id,
            proposals_read=proposals_read, rows_enqueued=rows_enqueued,
        )

    def _read_eligible(
        self, db_role: str, signal_date: date, setup_config_id: str
    ) -> list[dict[str, Any]]:
        connection = self._db.connect(db_role, read_only=True)
        try:
            raw = connection.execute(
                _SELECT_ELIGIBLE_PROPOSALS, [signal_date, setup_config_id]
            ).fetchall()
        finally:
            connection.close()
        return [{"proposal_id": r[0], "ticker": r[1]} for r in raw]

    def _write_queue(self, db_role: str, plan: list[dict[str, Any]]) -> int:
        if not plan:
            return 0
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for row in plan:
                    returned = connection.execute(
                        _INSERT_QUEUE_ROW,
                        [
                            row["tracking_id"], row["proposal_id"], row["ticker"],
                            row["signal_date"], row["entry_date"], row["eval_date"],
                            row["horizon_bd"],
                        ],
                    ).fetchone()
                    if returned is not None:
                        inserted += 1
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        finally:
            connection.close()
        return inserted

    def _enqueue_success(
        self, run_id: str, db_role: str, signal_iso: str, setup_config_id: str,
        *, proposals_read: int, rows_enqueued: int,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=rows_enqueued,
            metadata=self._enqueue_metadata(
                db_role=db_role, signal_date=signal_iso,
                setup_config_id=setup_config_id, run_id=run_id,
                proposals_read=proposals_read, rows_enqueued=rows_enqueued,
            ),
        )

    def _enqueue_failed(
        self, run_id: str, db_role: str, signal_iso: str, setup_config_id: str,
        message: str, *, proposals_read: int = 0,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._enqueue_metadata(
                db_role=db_role, signal_date=signal_iso,
                setup_config_id=setup_config_id, run_id=run_id,
                proposals_read=proposals_read, rows_enqueued=0,
            ),
        )

    @staticmethod
    def _enqueue_metadata(
        *, db_role: str, signal_date: str, setup_config_id: str, run_id: str,
        proposals_read: int, rows_enqueued: int,
    ) -> dict[str, Any]:
        return {
            "db_role": db_role,
            "signal_date": signal_date,
            "setup_config_id": setup_config_id,
            "run_id": run_id,
            "proposals_read": proposals_read,
            "rows_enqueued": rows_enqueued,
        }


# --------------------------------------------------------------------------- #
# Outcome queue processor.
# --------------------------------------------------------------------------- #
class OutcomeQueueProcessor:
    """Compute ``signal_outcomes`` for every due pending queue row."""

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )

    def process(
        self,
        run_date: date,
        setup_config: dict,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Process due queue rows and upsert their ``signal_outcomes``.

        Parameters
        ----------
        run_date:
            Processing date; only ``pending`` queue rows with
            ``eval_date <= run_date`` are processed.
        setup_config:
            Parsed setup-config JSON. ``simulation.slippage_bps`` is validated
            before any DB access and used for ``entry_price_sim``.
        db_role:
            ``"prod"`` or ``"debug"`` only.
        run_id:
            A fresh ``uuid4`` is minted when ``None``.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        run_iso = run_date.isoformat()
        log.info("process start db_role=%s run_date=%s", db_role, run_iso)

        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 16 targets only {list(ALLOWED_DB_ROLES)}."
            )
            log.error("process failed: %s", message)
            return self._process_failed(run_id, db_role, run_iso, message)

        try:
            slippage_bps = _validate_config(setup_config)
        except _ConfigError as exc:
            log.error("process failed: %s", exc)
            return self._process_failed(run_id, db_role, run_iso, str(exc))

        try:
            plan = self._build_plan(db_role, run_date, slippage_bps)
        except Exception as exc:  # noqa: BLE001
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("process failed: %s", message)
            return self._process_failed(run_id, db_role, run_iso, message)

        queue_rows_read = plan["queue_rows_read"]
        if queue_rows_read == 0:
            log.info("process done: no due queue rows for %s", run_iso)
            return self._process_success(
                run_id, db_role, run_iso, queue_rows_read=0, outcomes_written=0,
                unresolvable_count=0, repair_incremented_count=0,
            )

        try:
            self._write_plan(db_role, plan)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "process failed during write (rolled back): %s: %s",
                type(exc).__name__, exc,
            )
            return self._process_failed(
                run_id, db_role, run_iso, f"{type(exc).__name__}: {exc}",
                queue_rows_read=queue_rows_read,
            )

        log.info(
            "process done queue_rows_read=%d outcomes_written=%d "
            "unresolvable=%d repair_incremented=%d",
            queue_rows_read, len(plan["outcomes"]),
            plan["unresolvable_count"], plan["repair_incremented_count"],
        )
        return self._process_success(
            run_id, db_role, run_iso, queue_rows_read=queue_rows_read,
            outcomes_written=len(plan["outcomes"]),
            unresolvable_count=plan["unresolvable_count"],
            repair_incremented_count=plan["repair_incremented_count"],
        )

    def _build_plan(
        self, db_role: str, run_date: date, slippage_bps: float
    ) -> dict[str, Any]:
        """Read all due queue rows, compute outcomes / repair decisions."""
        cal = _default_calendar()
        connection = self._db.connect(db_role, read_only=True)
        try:
            queue_rows = [
                {
                    "tracking_id": r[0], "proposal_id": r[1], "ticker": r[2],
                    "signal_date": r[3], "entry_date": r[4], "eval_date": r[5],
                    "horizon_bd": int(r[6]), "repair_attempts": int(r[7]),
                }
                for r in connection.execute(_SELECT_DUE_QUEUE_ROWS, [run_date]).fetchall()
            ]

            outcomes: list[dict[str, Any]] = []
            queue_done: list[str] = []
            queue_repair: list[dict[str, Any]] = []
            unresolvable_count = 0
            repair_incremented_count = 0

            for row in queue_rows:
                ticker = row["ticker"]
                entry_date = row["entry_date"]
                horizon_bd = row["horizon_bd"]

                entry_open = _f(self._scalar(connection, _SELECT_OPEN_RAW, [ticker, entry_date]))

                if entry_open is None:
                    attempts = row["repair_attempts"] + 1
                    repair_incremented_count += 1
                    if attempts >= MAX_REPAIR_ATTEMPTS:
                        unresolvable_count += 1
                        queue_repair.append({
                            "tracking_id": row["tracking_id"],
                            "repair_attempts": attempts, "unresolvable": True,
                        })
                    else:
                        queue_repair.append({
                            "tracking_id": row["tracking_id"],
                            "repair_attempts": attempts, "unresolvable": False,
                        })
                    continue

                entry_price_raw = entry_open
                entry_price_sim = entry_price_raw * (1 + slippage_bps / 10000.0)

                # Read setup_config_id, setup_type, risk_label, stop/target from proposal.
                proposal_row = connection.execute(
                    _SELECT_PROPOSAL_SETUP_FIELDS, [row["proposal_id"]]
                ).fetchone()
                setup_config_id: str | None = None
                setup_type: str | None = None
                risk_label: str | None = None
                stop_price_raw: float | None = None
                target_price_raw: float | None = None
                if proposal_row is not None:
                    setup_config_id = proposal_row[0]
                    setup_type = proposal_row[1]
                    risk_label = proposal_row[2]
                    stop_price_raw = _f(proposal_row[3])
                    target_price_raw = _f(proposal_row[4])

                # Horizon returns.
                returns: dict[int, float | None] = {5: None, 10: None, 20: None, 40: None}
                eval_close_adj: dict[int, float | None] = {}
                for n in constants.OUTCOME_HORIZONS_BD:
                    if n > horizon_bd:
                        continue
                    eval_n = cal.add_trading_days(entry_date, n)
                    close_n = _f(self._scalar(connection, _SELECT_CLOSE_ADJ, [ticker, eval_n]))
                    eval_close_adj[n] = close_n
                    returns[n] = (
                        None if close_n is None else close_n / entry_price_sim - 1.0
                    )

                # 40bd MFE/MAE + stop_hit/target_hit (only for horizon_bd == 40).
                mfe_40 = mae_40 = None
                stop_hit: bool | None = None
                target_hit: bool | None = None
                if horizon_bd == 40:
                    mfe_40, mae_40, stop_hit, target_hit = self._window_stats(
                        connection, cal, ticker, entry_date, row["eval_date"],
                        entry_price_sim, stop_price_raw, target_price_raw,
                    )

                exit_close = eval_close_adj.get(horizon_bd)
                realized_r = self._realized_r(exit_close, entry_price_sim, stop_price_raw)

                earnings_flag = self._earnings_within_window(
                    connection, ticker, entry_date, row["eval_date"]
                )

                required = [
                    returns[n] for n in constants.OUTCOME_HORIZONS_BD if n <= horizon_bd
                ]
                status = (
                    OUTCOME_COMPLETE if all(v is not None for v in required) else OUTCOME_PARTIAL
                )

                outcomes.append({
                    "outcome_id": _outcome_id_for(row["proposal_id"], horizon_bd),
                    "proposal_id": row["proposal_id"],
                    "ticker": ticker,
                    "setup_config_id": setup_config_id,
                    "setup_type": setup_type,
                    "risk_label": risk_label,
                    "signal_date": row["signal_date"],
                    "entry_date": entry_date,
                    "entry_price_raw": entry_price_raw,
                    "entry_price_sim": entry_price_sim,
                    "stop_price_raw": stop_price_raw,
                    "target_price_raw": target_price_raw,
                    "return_5bd_pct": returns[5],
                    "return_10bd_pct": returns[10],
                    "return_20bd_pct": returns[20],
                    "return_40bd_pct": returns[40],
                    "mfe_40bd_pct": mfe_40,
                    "mae_40bd_pct": mae_40,
                    "stop_hit": stop_hit,
                    "target_hit": target_hit,
                    "realized_r_multiple": realized_r,
                    "earnings_within_window": earnings_flag,
                    "outcome_status": status,
                })
                queue_done.append(row["tracking_id"])
        finally:
            connection.close()

        return {
            "queue_rows_read": len(queue_rows),
            "outcomes": outcomes,
            "queue_done": queue_done,
            "queue_repair": queue_repair,
            "unresolvable_count": unresolvable_count,
            "repair_incremented_count": repair_incremented_count,
        }

    @staticmethod
    def _scalar(connection: Any, sql: str, params: list[Any]) -> Any:
        row = connection.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def _window_stats(
        self,
        connection: Any,
        cal: Any,
        ticker: str,
        entry_date: date,
        eval_date: date,
        entry_price_sim: float,
        stop_price_raw: float | None,
        target_price_raw: float | None,
    ) -> tuple[float | None, float | None, bool | None, bool | None]:
        """Compute (mfe, mae, stop_hit, target_hit) over [entry_date, eval_date].

        MFE/MAE use adjusted highs/lows. stop_hit/target_hit use raw highs/lows
        (FORMULAS §64). Returns (None, None, None, None) on any missing candle.
        """
        expected = cal.trading_days_between(entry_date, eval_date)
        candles = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in connection.execute(
                _SELECT_WINDOW_CANDLES, [ticker, entry_date, eval_date]
            ).fetchall()
        }
        highs_adj: list[float] = []
        lows_adj: list[float] = []
        stop_triggered = False
        target_triggered = False
        for day in expected:
            cell = candles.get(day)
            if cell is None:
                return None, None, None, None
            high_adj = _f(cell[0])
            low_adj = _f(cell[1])
            high_raw = _f(cell[2])
            low_raw = _f(cell[3])
            if high_adj is None or low_adj is None:
                return None, None, None, None
            highs_adj.append(high_adj)
            lows_adj.append(low_adj)
            # stop_hit: low_raw <= stop_price_raw (FORMULAS §64)
            if stop_price_raw is not None and low_raw is not None and not stop_triggered:
                if low_raw <= stop_price_raw:
                    stop_triggered = True
            # target_hit: high_raw >= target_price_raw (FORMULAS §64)
            if target_price_raw is not None and high_raw is not None and not target_triggered:
                if high_raw >= target_price_raw:
                    target_triggered = True
        if not highs_adj:
            return None, None, None, None
        mfe = max(highs_adj) / entry_price_sim - 1.0
        mae = min(lows_adj) / entry_price_sim - 1.0
        sh = stop_triggered if stop_price_raw is not None else None
        th = target_triggered if target_price_raw is not None else None
        return mfe, mae, sh, th

    @staticmethod
    def _realized_r(
        exit_close_adj: float | None,
        entry_price_sim: float,
        stop_price_raw: float | None,
    ) -> float | None:
        if exit_close_adj is None or stop_price_raw is None:
            return None
        denom = entry_price_sim - stop_price_raw
        if denom <= 0:
            return None
        return (exit_close_adj - entry_price_sim) / denom

    def _earnings_within_window(
        self, connection: Any, ticker: str, entry_date: date, eval_date: date,
    ) -> bool | None:
        row = connection.execute(
            _SELECT_EARNINGS, [entry_date, eval_date, ticker]
        ).fetchone()
        total = 0 if row is None or row[0] is None else int(row[0])
        in_window = 0 if row is None or row[1] is None else int(row[1])
        if total == 0:
            return None
        return in_window > 0

    def _write_plan(self, db_role: str, plan: dict[str, Any]) -> None:
        outcomes = plan["outcomes"]
        queue_done = plan["queue_done"]
        queue_repair = plan["queue_repair"]
        if not outcomes and not queue_done and not queue_repair:
            return
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for o in outcomes:
                    connection.execute(
                        _UPSERT_OUTCOME,
                        [
                            o["outcome_id"], o["proposal_id"], o["ticker"],
                            o["setup_config_id"], o["setup_type"], o["risk_label"],
                            o["signal_date"], o["entry_date"],
                            o["entry_price_raw"], o["entry_price_sim"],
                            o["stop_price_raw"], o["target_price_raw"],
                            o["return_5bd_pct"], o["return_10bd_pct"],
                            o["return_20bd_pct"], o["return_40bd_pct"],
                            o["mfe_40bd_pct"], o["mae_40bd_pct"],
                            o["stop_hit"], o["target_hit"],
                            o["realized_r_multiple"], o["earnings_within_window"],
                            o["outcome_status"],
                        ],
                    )
                for tracking_id in queue_done:
                    connection.execute(_UPDATE_QUEUE_DONE, [tracking_id])
                for repair in queue_repair:
                    sql = (
                        _UPDATE_QUEUE_REPAIR_UNRESOLVABLE
                        if repair["unresolvable"]
                        else _UPDATE_QUEUE_REPAIR_PENDING
                    )
                    connection.execute(sql, [repair["repair_attempts"], repair["tracking_id"]])
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        finally:
            connection.close()

    def _process_success(
        self, run_id: str, db_role: str, run_iso: str, *,
        queue_rows_read: int, outcomes_written: int,
        unresolvable_count: int, repair_incremented_count: int,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=outcomes_written,
            metadata=self._process_metadata(
                db_role=db_role, run_date=run_iso, run_id=run_id,
                queue_rows_read=queue_rows_read, outcomes_written=outcomes_written,
                unresolvable_count=unresolvable_count,
                repair_incremented_count=repair_incremented_count,
            ),
        )

    def _process_failed(
        self, run_id: str, db_role: str, run_iso: str, message: str, *,
        queue_rows_read: int = 0,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._process_metadata(
                db_role=db_role, run_date=run_iso, run_id=run_id,
                queue_rows_read=queue_rows_read, outcomes_written=0,
                unresolvable_count=0, repair_incremented_count=0,
            ),
        )

    @staticmethod
    def _process_metadata(
        *, db_role: str, run_date: str, run_id: str,
        queue_rows_read: int, outcomes_written: int,
        unresolvable_count: int, repair_incremented_count: int,
    ) -> dict[str, Any]:
        return {
            "db_role": db_role,
            "run_date": run_date,
            "run_id": run_id,
            "queue_rows_read": queue_rows_read,
            "outcomes_written": outcomes_written,
            "unresolvable_count": unresolvable_count,
            "repair_incremented_count": repair_incremented_count,
        }


__all__ = [
    "OutcomeQueueCreator",
    "OutcomeQueueProcessor",
    "ENQUEUE_METADATA_KEYS",
    "PROCESS_METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "MAX_REPAIR_ATTEMPTS",
]
