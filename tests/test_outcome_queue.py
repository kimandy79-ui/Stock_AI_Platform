"""Tests for Module 16 — Outcome Queue.

All tests run fully offline (no network, no provider, no real trading calendar)
and never touch the real prod / debug / simulation DB files. The ``tmp_db_paths``
fixture redirects every DuckDB settings path into pytest ``tmp_path`` and applies
the real Module 03 schema there (mirroring ``tests/test_step5_proposal_engine``).
A :class:`FakeCalendar` is injected via ``monkeypatch`` over
``outcome_queue._default_calendar`` so ``pandas_market_calendars`` is never
imported during the suite.

Seeds (harness only, not Module 16): ``step5_proposals`` (eligibility +
strategy_config_id), ``step4_analysis`` (``stop_price_raw``), ``daily_prices``
(OHLC), ``earnings_calendar``, and ``outcome_tracking_queue`` rows for processor
scenarios. Module 16 reads everything read-only and only writes
``outcome_tracking_queue`` / ``signal_outcomes``.
"""

from __future__ import annotations

import ast
import math
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.config import constants
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.outcomes import outcome_queue as oq
from app.services.outcomes.outcome_queue import (
    OutcomeQueueCreator,
    OutcomeQueueProcessor,
)
from app.utils import service_result

CONFIG_ID = "cfg-1"

ENQUEUE_KEYS = frozenset(
    {
        "db_role",
        "signal_date",
        "strategy_config_id",
        "run_id",
        "proposals_read",
        "rows_enqueued",
    }
)
PROCESS_KEYS = frozenset(
    {
        "db_role",
        "run_date",
        "run_id",
        "queue_rows_read",
        "outcomes_written",
        "unresolvable_count",
        "repair_incremented_count",
    }
)


# --------------------------------------------------------------------------- #
# Fake NYSE trading calendar (Mon-Fri sessions; no holidays needed for tests).
# --------------------------------------------------------------------------- #
def _weekday_sessions(start: date, count: int) -> list[date]:
    sessions: list[date] = []
    day = start
    while len(sessions) < count:
        if day.weekday() < 5:  # Mon-Fri
            sessions.append(day)
        day += timedelta(days=1)
    return sessions


SESSIONS: list[date] = _weekday_sessions(date(2024, 1, 2), 200)
SIGNAL_DATE: date = SESSIONS[0]
ENTRY_DATE: date = SESSIONS[1]  # next_trading_day(signal_date)


class FakeCalendar:
    """Minimal NYSE-like calendar over an explicit ordered session list."""

    def __init__(self, sessions: list[date]) -> None:
        self._sessions = sorted(sessions)
        self._index = {d: i for i, d in enumerate(self._sessions)}

    def next_trading_day(self, day: date) -> date:
        for s in self._sessions:
            if s > day:
                return s
        raise AssertionError("session list exhausted")

    def add_trading_days(self, day: date, n: int) -> date:
        idx = self._index[day]
        return self._sessions[idx + n]

    def trading_days_between(self, start: date, end: date) -> list[date]:
        return [s for s in self._sessions if start <= s <= end]


def eval_date_for(horizon_bd: int) -> date:
    return SESSIONS[1 + horizon_bd]  # add_trading_days(ENTRY_DATE, horizon)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"

    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)

    assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS

    return {
        dbm.DB_ROLE_PROD: prod,
        dbm.DB_ROLE_DEBUG: debug,
        dbm.DB_ROLE_SIMULATION: simulation,
    }


@pytest.fixture(autouse=True)
def fake_calendar(monkeypatch: pytest.MonkeyPatch) -> FakeCalendar:
    cal = FakeCalendar(SESSIONS)
    monkeypatch.setattr(oq, "_default_calendar", lambda: cal)
    return cal


def config(slippage_bps: float = 10) -> dict:
    return {"simulation": {"slippage_bps": slippage_bps}}


# --------------------------------------------------------------------------- #
# Seeding helpers (harness only; write directly to the DB).
# --------------------------------------------------------------------------- #
def _connect(db_path: Path, read_only: bool = False):
    import duckdb

    return duckdb.connect(database=str(db_path), read_only=read_only)


_INSERT_PROPOSAL = (
    "INSERT INTO step5_proposals "
    "(proposal_id, run_id, strategy_config_id, ticker, signal_date, "
    " in_raw_top_n, in_diversified_top_n, diversification_applied, "
    " selected_top_n, selected_flag, ai_reviewed, executed_flag, created_at) "
    "VALUES (?, 'run', ?, ?, ?, ?, ?, TRUE, ?, ?, FALSE, FALSE, "
    " CAST(now() AS TIMESTAMP))"
)

_INSERT_ANALYSIS = (
    "INSERT INTO step4_analysis "
    "(analysis_id, candidate_id, run_id, strategy_config_id, ticker, "
    " signal_date, stop_price_raw, created_at) "
    "VALUES (?, ?, 'run', ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

_INSERT_PRICE = (
    "INSERT INTO daily_prices "
    "(ticker, date, open_raw, high_adj, low_adj, close_adj, source_provider, "
    " data_quality_status, mutation_flag, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, 'test', 'ok', FALSE, CAST(now() AS TIMESTAMP))"
)

_INSERT_EARNINGS = (
    "INSERT INTO earnings_calendar "
    "(ticker, earnings_date, session, source, confidence, updated_at) "
    "VALUES (?, ?, 'unknown', 'test', 'high', CAST(now() AS TIMESTAMP))"
)

_INSERT_QUEUE = (
    "INSERT INTO outcome_tracking_queue "
    "(tracking_id, proposal_id, ticker, signal_date, entry_date, eval_date, "
    " horizon_bd, status, repair_attempts, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)


def seed_proposal(
    db_path: Path,
    proposal_id: str,
    ticker: str,
    *,
    in_raw: bool = True,
    in_div: bool = True,
    config_id: str = CONFIG_ID,
    signal_date: date = SIGNAL_DATE,
    stop_price_raw: float | None = 90.0,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            _INSERT_PROPOSAL,
            [
                proposal_id,
                config_id,
                ticker,
                signal_date,
                in_raw,
                in_div,
                in_raw or in_div,
                in_div,
            ],
        )
        conn.execute(
            _INSERT_ANALYSIS,
            [
                f"an-{proposal_id}",
                f"cand-{proposal_id}",
                config_id,
                ticker,
                signal_date,
                stop_price_raw,
            ],
        )
    finally:
        conn.close()


def seed_price(
    db_path: Path,
    ticker: str,
    day: date,
    *,
    open_raw: float | None = 100.0,
    high_adj: float | None = 110.0,
    low_adj: float | None = 95.0,
    close_adj: float | None = 105.0,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            _INSERT_PRICE, [ticker, day, open_raw, high_adj, low_adj, close_adj]
        )
    finally:
        conn.close()


def seed_window(db_path: Path, ticker: str, horizon_bd: int) -> None:
    """Seed entry + every eval candle + full window for a horizon."""
    for n in range(0, horizon_bd + 1):
        day = SESSIONS[1 + n]
        seed_price(db_path, ticker, day)


def seed_earnings(db_path: Path, ticker: str, day: date) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(_INSERT_EARNINGS, [ticker, day])
    finally:
        conn.close()


def seed_queue_row(
    db_path: Path,
    proposal_id: str,
    horizon_bd: int,
    *,
    ticker: str = "AAA",
    status: str = "pending",
    repair_attempts: int = 0,
    entry_date: date = ENTRY_DATE,
) -> str:
    tracking_id = oq._tracking_id_for(proposal_id, horizon_bd)
    conn = _connect(db_path)
    try:
        conn.execute(
            _INSERT_QUEUE,
            [
                tracking_id,
                proposal_id,
                ticker,
                SIGNAL_DATE,
                entry_date,
                eval_date_for(horizon_bd),
                horizon_bd,
                status,
                repair_attempts,
            ],
        )
    finally:
        conn.close()
    return tracking_id


def fetch_queue(db_path: Path, tracking_id: str) -> dict:
    conn = _connect(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT status, repair_attempts, last_repair_attempt, completed_at "
            "FROM outcome_tracking_queue WHERE tracking_id = ?",
            [tracking_id],
        ).fetchone()
    finally:
        conn.close()
    return {
        "status": row[0],
        "repair_attempts": row[1],
        "last_repair_attempt": row[2],
        "completed_at": row[3],
    }


def fetch_outcome(db_path: Path, outcome_id: str) -> dict | None:
    conn = _connect(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT entry_price_raw, entry_price_sim, return_5bd_pct, "
            "return_10bd_pct, return_20bd_pct, return_40bd_pct, mfe_40bd_pct, "
            "mae_40bd_pct, realized_r_multiple, earnings_within_window, "
            "outcome_status, strategy_config_id FROM signal_outcomes "
            "WHERE outcome_id = ?",
            [outcome_id],
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cols = [
        "entry_price_raw",
        "entry_price_sim",
        "return_5bd_pct",
        "return_10bd_pct",
        "return_20bd_pct",
        "return_40bd_pct",
        "mfe_40bd_pct",
        "mae_40bd_pct",
        "realized_r_multiple",
        "earnings_within_window",
        "outcome_status",
        "strategy_config_id",
    ]
    return dict(zip(cols, row))


def count_outcomes(db_path: Path) -> int:
    conn = _connect(db_path, read_only=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Creator — public API / metadata / guards.
# --------------------------------------------------------------------------- #
def test_enqueue_public_api_and_generated_run_id(tmp_db_paths: dict[str, Path]) -> None:
    seed_proposal(tmp_db_paths[dbm.DB_ROLE_PROD], "p1", "AAA")
    res = OutcomeQueueCreator().enqueue(SIGNAL_DATE, CONFIG_ID, config())
    assert res.status == service_result.STATUS_SUCCESS
    assert uuid.UUID(res.run_id)  # minted
    assert frozenset(res.metadata) == ENQUEUE_KEYS
    assert res.metadata["run_id"] == res.run_id
    assert res.rows_processed == res.metadata["rows_enqueued"]


def test_enqueue_provided_run_id_is_kept(tmp_db_paths: dict[str, Path]) -> None:
    seed_proposal(tmp_db_paths[dbm.DB_ROLE_PROD], "p1", "AAA")
    res = OutcomeQueueCreator().enqueue(
        SIGNAL_DATE, CONFIG_ID, config(), run_id="fixed-run"
    )
    assert res.run_id == "fixed-run"


def test_enqueue_rejects_simulation_before_db_access() -> None:
    res = OutcomeQueueCreator().enqueue(
        SIGNAL_DATE, CONFIG_ID, config(), db_role="simulation"
    )
    assert res.status == service_result.STATUS_FAILED
    assert frozenset(res.metadata) == ENQUEUE_KEYS
    assert res.metadata["rows_enqueued"] == 0
    assert res.rows_processed == 0


def test_enqueue_config_validated_before_db_access() -> None:
    res = OutcomeQueueCreator().enqueue(
        SIGNAL_DATE, CONFIG_ID, {"simulation": {"slippage_bps": -1}}
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["rows_enqueued"] == 0


def test_enqueue_missing_slippage_fails() -> None:
    res = OutcomeQueueCreator().enqueue(SIGNAL_DATE, CONFIG_ID, {"simulation": {}})
    assert res.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# Creator — eligibility / horizons / dates / idempotency.
# --------------------------------------------------------------------------- #
def test_enqueue_eligibility_filter(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "raw", "AAA", in_raw=True, in_div=False)
    seed_proposal(prod, "div", "BBB", in_raw=False, in_div=True)
    seed_proposal(prod, "both", "CCC", in_raw=True, in_div=True)
    seed_proposal(prod, "none", "DDD", in_raw=False, in_div=False)

    res = OutcomeQueueCreator().enqueue(SIGNAL_DATE, CONFIG_ID, config())
    assert res.metadata["proposals_read"] == 3
    assert res.metadata["rows_enqueued"] == 3 * len(constants.OUTCOME_HORIZONS_BD)


def test_enqueue_one_row_per_horizon_with_deterministic_ids_and_dates(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    OutcomeQueueCreator().enqueue(SIGNAL_DATE, CONFIG_ID, config())

    conn = _connect(prod, read_only=True)
    try:
        rows = conn.execute(
            "SELECT tracking_id, horizon_bd, entry_date, eval_date, status "
            "FROM outcome_tracking_queue ORDER BY horizon_bd"
        ).fetchall()
    finally:
        conn.close()

    assert [r[1] for r in rows] == list(constants.OUTCOME_HORIZONS_BD)
    for tracking_id, horizon, entry_date, eval_date, status in rows:
        assert tracking_id == oq._tracking_id_for("p1", horizon)
        assert entry_date == ENTRY_DATE
        assert eval_date == eval_date_for(horizon)
        assert status == "pending"


def test_enqueue_idempotent_second_run(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    first = OutcomeQueueCreator().enqueue(SIGNAL_DATE, CONFIG_ID, config())
    second = OutcomeQueueCreator().enqueue(SIGNAL_DATE, CONFIG_ID, config())

    assert first.metadata["rows_enqueued"] == len(constants.OUTCOME_HORIZONS_BD)
    assert second.metadata["rows_enqueued"] == 0  # no new rows
    assert second.status == service_result.STATUS_SUCCESS

    conn = _connect(prod, read_only=True)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM outcome_tracking_queue"
        ).fetchone()[0]
    finally:
        conn.close()
    assert total == len(constants.OUTCOME_HORIZONS_BD)


def test_enqueue_empty_eligible_input_succeeds_zero_counts(
    tmp_db_paths: dict[str, Path],
) -> None:
    res = OutcomeQueueCreator().enqueue(SIGNAL_DATE, CONFIG_ID, config())
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["proposals_read"] == 0
    assert res.metadata["rows_enqueued"] == 0


def test_enqueue_write_failure_rolls_back(tmp_db_paths: dict[str, Path]) -> None:
    seed_proposal(tmp_db_paths[dbm.DB_ROLE_PROD], "p1", "AAA")

    class BoomConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            if sql.strip().startswith("INSERT INTO outcome_tracking_queue"):
                raise RuntimeError("boom")
            return self._inner.execute(sql, params) if params else self._inner.execute(sql)

        def close(self):
            self._inner.close()

    class BoomManager:
        def connect(self, db_role, read_only=False):
            real = dbm.connect(db_role, read_only=read_only)
            return real if read_only else BoomConn(real)

    res = OutcomeQueueCreator(db_manager=BoomManager()).enqueue(
        SIGNAL_DATE, CONFIG_ID, config()
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["rows_enqueued"] == 0
    conn = _connect(tmp_db_paths[dbm.DB_ROLE_PROD], read_only=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM outcome_tracking_queue"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Processor — public API / metadata / guards / empty / future rows.
# --------------------------------------------------------------------------- #
def test_process_public_api_and_metadata_keys(tmp_db_paths: dict[str, Path]) -> None:
    res = OutcomeQueueProcessor().process(SESSIONS[60], config())
    assert res.status == service_result.STATUS_SUCCESS
    assert frozenset(res.metadata) == PROCESS_KEYS
    assert uuid.UUID(res.run_id)
    assert res.rows_processed == res.metadata["outcomes_written"] == 0


def test_process_rejects_simulation_before_db_access() -> None:
    res = OutcomeQueueProcessor().process(
        SESSIONS[60], config(), db_role="simulation"
    )
    assert res.status == service_result.STATUS_FAILED
    assert frozenset(res.metadata) == PROCESS_KEYS


def test_process_config_validated_before_db_access() -> None:
    res = OutcomeQueueProcessor().process(SESSIONS[60], {"simulation": {}})
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["outcomes_written"] == 0


def test_process_provided_run_id_kept(tmp_db_paths: dict[str, Path]) -> None:
    res = OutcomeQueueProcessor().process(SESSIONS[60], config(), run_id="rid")
    assert res.run_id == "rid"


def test_process_ignores_future_eval_rows(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    seed_window(prod, "AAA", 40)
    seed_queue_row(prod, "p1", 40)  # eval_date == SESSIONS[41]
    # run_date earlier than eval_date -> ignored.
    res = OutcomeQueueProcessor().process(SESSIONS[10], config())
    assert res.metadata["queue_rows_read"] == 0
    assert count_outcomes(prod) == 0


# --------------------------------------------------------------------------- #
# Processor — repair / unresolvable.
# --------------------------------------------------------------------------- #
def test_process_missing_entry_increments_repair_no_outcome(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    tid = seed_queue_row(prod, "p1", 5)  # no entry-date price seeded
    res = OutcomeQueueProcessor().process(SESSIONS[60], config())

    assert res.metadata["repair_incremented_count"] == 1
    assert res.metadata["unresolvable_count"] == 0
    assert res.metadata["outcomes_written"] == 0
    q = fetch_queue(prod, tid)
    assert q["status"] == "pending"
    assert q["repair_attempts"] == 1
    assert q["last_repair_attempt"] is not None
    assert count_outcomes(prod) == 0


def test_process_third_attempt_marks_unresolvable(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    tid = seed_queue_row(prod, "p1", 5, repair_attempts=2)
    res = OutcomeQueueProcessor().process(SESSIONS[60], config())

    assert res.metadata["repair_incremented_count"] == 1
    assert res.metadata["unresolvable_count"] == 1
    q = fetch_queue(prod, tid)
    assert q["status"] == "unresolvable"
    assert q["repair_attempts"] == 3
    assert count_outcomes(prod) == 0


# --------------------------------------------------------------------------- #
# Processor — formulas / NULL handling.
# --------------------------------------------------------------------------- #
def test_process_return_formulas_and_entry_prices(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA", stop_price_raw=90.0)
    # entry open_raw = 100; slippage 10bps -> sim = 100.1
    seed_price(prod, "AAA", ENTRY_DATE, open_raw=100.0)
    seed_price(prod, "AAA", eval_date_for(5), close_adj=120.0)
    seed_queue_row(prod, "p1", 5)

    res = OutcomeQueueProcessor().process(SESSIONS[60], config(10))
    assert res.metadata["outcomes_written"] == 1
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 5))
    assert out is not None
    assert math.isclose(out["entry_price_sim"], 100.1, rel_tol=1e-9)
    assert math.isclose(out["return_5bd_pct"], 120.0 / 100.1 - 1, rel_tol=1e-9)
    # horizons > 5 are NULL for a 5bd row.
    assert out["return_10bd_pct"] is None
    assert out["outcome_status"] == "complete"
    assert out["strategy_config_id"] == CONFIG_ID


def test_process_missing_horizon_candle_is_partial(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    seed_price(prod, "AAA", ENTRY_DATE, open_raw=100.0)
    # eval-date close candle deliberately missing.
    seed_queue_row(prod, "p1", 5)
    res = OutcomeQueueProcessor().process(SESSIONS[60], config())
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 5))
    assert out["return_5bd_pct"] is None
    assert out["outcome_status"] == "partial"
    assert res.metadata["outcomes_written"] == 1


def test_process_mfe_mae_full_window(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    # full 0..40 window candles (high_adj=110, low_adj=95, close_adj=105)
    seed_window(prod, "AAA", 40)
    # bump one candle's high/low to known extremes.
    seed_queue_row(prod, "p1", 40)
    res = OutcomeQueueProcessor().process(SESSIONS[60], config(0))
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 40))
    assert out["mfe_40bd_pct"] is not None
    assert out["mae_40bd_pct"] is not None
    # sim == entry (slippage 0) == 100; max high 110 -> 0.10; min low 95 -> -0.05
    assert math.isclose(out["mfe_40bd_pct"], 110.0 / 100.0 - 1, rel_tol=1e-9)
    assert math.isclose(out["mae_40bd_pct"], 95.0 / 100.0 - 1, rel_tol=1e-9)


def test_process_mfe_mae_null_when_window_incomplete(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA")
    # seed only part of the window (0..20), leaving 21..40 missing.
    for n in range(0, 21):
        seed_price(prod, "AAA", SESSIONS[1 + n])
    seed_queue_row(prod, "p1", 40)
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 40))
    assert out["mfe_40bd_pct"] is None
    assert out["mae_40bd_pct"] is None


def test_process_realized_r_multiple(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_proposal(prod, "p1", "AAA", stop_price_raw=90.0)
    seed_price(prod, "AAA", ENTRY_DATE, open_raw=100.0)
    seed_price(prod, "AAA", eval_date_for(5), close_adj=130.0)
    seed_queue_row(prod, "p1", 5)
    OutcomeQueueProcessor().process(SESSIONS[60], config(0))
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 5))
    # (130 - 100) / (100 - 90) = 3.0
    assert math.isclose(out["realized_r_multiple"], 3.0, rel_tol=1e-9)


def test_process_realized_r_null_when_denominator_non_positive(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # stop above entry -> denom <= 0 -> NULL
    seed_proposal(prod, "p1", "AAA", stop_price_raw=120.0)
    seed_price(prod, "AAA", ENTRY_DATE, open_raw=100.0)
    seed_price(prod, "AAA", eval_date_for(5), close_adj=130.0)
    seed_queue_row(prod, "p1", 5)
    OutcomeQueueProcessor().process(SESSIONS[60], config(0))
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 5))
    assert out["realized_r_multiple"] is None


# --------------------------------------------------------------------------- #
# Processor — earnings tri-state.
# --------------------------------------------------------------------------- #
def _seed_basic_5bd(prod: Path, ticker: str, proposal_id: str) -> None:
    seed_proposal(prod, proposal_id, ticker)
    seed_price(prod, ticker, ENTRY_DATE, open_raw=100.0)
    seed_price(prod, ticker, eval_date_for(5), close_adj=105.0)
    seed_queue_row(prod, proposal_id, 5)


def test_process_earnings_in_window_true(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    _seed_basic_5bd(prod, "AAA", "p1")
    seed_earnings(prod, "AAA", SESSIONS[3])  # within (entry, eval]
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 5))
    assert out["earnings_within_window"] is True


def test_process_earnings_out_of_window_false(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    _seed_basic_5bd(prod, "AAA", "p1")
    seed_earnings(prod, "AAA", SESSIONS[80])  # far outside window
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 5))
    assert out["earnings_within_window"] is False


def test_process_earnings_no_rows_null(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    _seed_basic_5bd(prod, "AAA", "p1")
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    out = fetch_outcome(prod, oq._outcome_id_for("p1", 5))
    assert out["earnings_within_window"] is None


# --------------------------------------------------------------------------- #
# Processor — deterministic id / idempotent reprocess / done status.
# --------------------------------------------------------------------------- #
def test_process_sets_queue_done_and_completed_at(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    _seed_basic_5bd(prod, "AAA", "p1")
    tid = oq._tracking_id_for("p1", 5)
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    q = fetch_queue(prod, tid)
    assert q["status"] == "done"
    assert q["completed_at"] is not None


def test_process_reprocess_is_idempotent_upsert(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    _seed_basic_5bd(prod, "AAA", "p1")
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    # Re-enqueue the same (now-done) row back to pending and reprocess.
    conn = _connect(prod)
    try:
        conn.execute(
            "UPDATE outcome_tracking_queue SET status='pending', "
            "completed_at=NULL WHERE tracking_id = ?",
            [oq._tracking_id_for("p1", 5)],
        )
    finally:
        conn.close()
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    assert count_outcomes(prod) == 1  # single upserted row, not duplicated


def test_process_deterministic_outcome_id(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    _seed_basic_5bd(prod, "AAA", "p1")
    OutcomeQueueProcessor().process(SESSIONS[60], config())
    assert fetch_outcome(prod, oq._outcome_id_for("p1", 5)) is not None


def test_process_write_failure_rolls_back(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    _seed_basic_5bd(prod, "AAA", "p1")

    class BoomConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            if sql.strip().startswith("INSERT INTO signal_outcomes"):
                raise RuntimeError("boom")
            return self._inner.execute(sql, params) if params else self._inner.execute(sql)

        def close(self):
            self._inner.close()

    class BoomManager:
        def connect(self, db_role, read_only=False):
            real = dbm.connect(db_role, read_only=read_only)
            return real if read_only else BoomConn(real)

    res = OutcomeQueueProcessor(db_manager=BoomManager()).process(
        SESSIONS[60], config()
    )
    assert res.status == service_result.STATUS_FAILED
    assert count_outcomes(prod) == 0
    # queue row must remain pending (rollback).
    q = fetch_queue(prod, oq._tracking_id_for("p1", 5))
    assert q["status"] == "pending"


def test_process_debug_role(tmp_db_paths: dict[str, Path], monkeypatch) -> None:
    debug = tmp_db_paths[dbm.DB_ROLE_DEBUG]
    assert sm.apply_debug_schema().status == service_result.STATUS_SUCCESS
    _seed_basic_5bd(debug, "AAA", "p1")
    res = OutcomeQueueProcessor().process(SESSIONS[60], config(), db_role="debug")
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["outcomes_written"] == 1


# --------------------------------------------------------------------------- #
# Static guardrail scans (run offline; read source by path, no import needed).
# --------------------------------------------------------------------------- #
_SRC_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "services"
    / "outcomes"
    / "outcome_queue.py"
)


def _source() -> str:
    return _SRC_PATH.read_text(encoding="utf-8")


def _imported_modules(src: str) -> set[str]:
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _sql_strings(src: str) -> str:
    """All string constants except docstrings, upper-cased and concatenated."""
    tree = ast.parse(src)
    skip = _docstring_ids(tree)
    parts = [
        n.value.upper()
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in skip
    ]
    return "\n".join(parts)


def test_no_direct_duckdb_import() -> None:
    assert not any(
        m == "duckdb" or m.startswith("duckdb.")
        for m in _imported_modules(_source())
    )


def test_no_provider_imports() -> None:
    # Only import statements are scanned; the module docstring may legitimately
    # mention that the service "calls providers" (it must not).
    assert not any(
        "provider" in m.lower() for m in _imported_modules(_source())
    )


def test_no_print_calls() -> None:
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_ddl_or_attach() -> None:
    sql = _sql_strings(_source())
    for token in ("ATTACH", "CREATE TABLE", "DROP TABLE", "ALTER TABLE"):
        assert token not in sql


def test_only_allowed_write_targets() -> None:
    """No INSERT/UPDATE/DELETE against tables other than the two allowed."""
    sql = _sql_strings(_source())
    for verb in ("INSERT INTO ", "UPDATE ", "DELETE FROM "):
        idx = 0
        while True:
            idx = sql.find(verb, idx)
            if idx == -1:
                break
            # Skip the upsert "ON CONFLICT ... DO UPDATE SET" clause.
            if verb == "UPDATE " and sql[max(0, idx - 3):idx] == "DO ":
                idx += len(verb)
                continue
            tail = sql[idx + len(verb):].lstrip()
            assert tail.startswith("OUTCOME_TRACKING_QUEUE") or tail.startswith(
                "SIGNAL_OUTCOMES"
            ), f"unexpected write target after {verb!r}: {tail[:40]}"
            idx += len(verb)
