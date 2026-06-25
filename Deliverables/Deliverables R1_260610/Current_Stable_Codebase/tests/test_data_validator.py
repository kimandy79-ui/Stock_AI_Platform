"""Tests for Module 09 — Data Validator.

All tests run fully offline (no network, no provider) and never touch the real
prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects every
DuckDB settings path into pytest ``tmp_path`` and applies the real Module 03
schema there (mirroring ``tests/test_daily_price_ingestion.py``). Rows are
seeded directly into ``daily_prices`` per test; Module 09 reads them read-only
and writes only ``data_quality_status`` (+ the row ``updated_at`` audit column)
and inserts into ``data_repair_queue``.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from pathlib import Path

import pytest

from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.validation import data_validator as dvmod
from app.services.validation.data_validator import DataValidator
from app.utils import service_result
from app.utils.service_result import ServiceResult

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "db_role",
        "start_date",
        "end_date",
        "rows_validated",
        "rows_ok",
        "rows_failed",
        "status_updates_written",
        "repair_queue_enqueued",
    }
)

START = date(2024, 1, 2)
END = date(2024, 1, 5)
SOURCE = "fake"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect all DuckDB settings paths into ``tmp_path`` and apply schema."""
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"

    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)

    assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
    assert sm.apply_debug_schema().status == service_result.STATUS_SUCCESS

    return {
        dbm.DB_ROLE_PROD: prod,
        dbm.DB_ROLE_DEBUG: debug,
        dbm.DB_ROLE_SIMULATION: simulation,
    }


# --------------------------------------------------------------------------- #
# Seeding helpers (write directly to daily_prices; this is the test harness,
# not Module 09 — Module 09 itself never inserts price rows).
# --------------------------------------------------------------------------- #
_INSERT_PRICE = (
    "INSERT INTO daily_prices "
    "(ticker, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
    " open_adj, high_adj, low_adj, close_adj, volume_adj, "
    " dividend_amount, split_ratio, adjustment_factor, source_provider, "
    " data_quality_status, mutation_flag, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, NULL, ?, ?, FALSE, "
    " CAST(now() AS TIMESTAMP), NULL)"
)


def _price_defaults() -> dict[str, object]:
    return dict(
        open_raw=10.0,
        high_raw=11.0,
        low_raw=9.0,
        close_raw=10.5,
        volume_raw=1000,
        open_adj=10.0,
        high_adj=11.0,
        low_adj=9.0,
        close_adj=10.5,
        volume_adj=None,
        data_quality_status="ok",
    )


def _seed_price(
    ticker: str,
    d: date,
    role: str = "prod",
    **overrides: object,
) -> None:
    """Insert one ``daily_prices`` row with valid defaults unless overridden."""
    vals = _price_defaults()
    vals.update(overrides)
    conn = dbm.connect(role)
    try:
        conn.execute(
            _INSERT_PRICE,
            [
                ticker,
                d,
                vals["open_raw"],
                vals["high_raw"],
                vals["low_raw"],
                vals["close_raw"],
                vals["volume_raw"],
                vals["open_adj"],
                vals["high_adj"],
                vals["low_adj"],
                vals["close_adj"],
                vals["volume_adj"],
                SOURCE,
                vals["data_quality_status"],
            ],
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Readback helpers
# --------------------------------------------------------------------------- #
def _price_rows(role: str = "prod") -> dict[tuple[str, date], tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT ticker, date, open_raw, high_raw, low_raw, close_raw, "
            "volume_raw, open_adj, high_adj, low_adj, close_adj, volume_adj, "
            "dividend_amount, split_ratio, adjustment_factor, source_provider, "
            "data_quality_status, mutation_flag, created_at, updated_at "
            "FROM daily_prices"
        ).fetchall()
    finally:
        conn.close()
    return {(r[0], r[1]): r for r in rows}


(
    P_TICKER,
    P_DATE,
    P_OPEN_RAW,
    P_HIGH_RAW,
    P_LOW_RAW,
    P_CLOSE_RAW,
    P_VOLUME_RAW,
    P_OPEN_ADJ,
    P_HIGH_ADJ,
    P_LOW_ADJ,
    P_CLOSE_ADJ,
    P_VOLUME_ADJ,
    P_DIVIDEND,
    P_SPLIT,
    P_ADJ_FACTOR,
    P_SOURCE,
    P_DQ,
    P_MUT,
    P_CREATED,
    P_UPDATED,
) = range(20)


def _status_of(ticker: str, d: date, role: str = "prod") -> str:
    return _price_rows(role)[(ticker, d)][P_DQ]


def _repair_rows(role: str = "prod") -> list[tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT repair_id, ticker, repair_date, repair_reason, attempts, "
            "max_attempts, last_attempt, status, created_at, updated_at "
            "FROM data_repair_queue ORDER BY ticker, repair_date"
        ).fetchall()
    finally:
        conn.close()
    return rows


(
    R_ID,
    R_TICKER,
    R_DATE,
    R_REASON,
    R_ATTEMPTS,
    R_MAX,
    R_LAST,
    R_STATUS,
    R_CREATED,
    R_UPDATED,
) = range(10)


# --------------------------------------------------------------------------- #
# 1. Public API: import, exact signature, run_id propagation
# --------------------------------------------------------------------------- #
def test_public_api_exact_signature() -> None:
    sig = inspect.signature(DataValidator.validate)
    params = list(sig.parameters)
    assert params == ["self", "start_date", "end_date", "db_role", "run_id"]
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["run_id"].default is None


def test_run_id_propagation_minted_when_none(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("AAPL", START)
    result = DataValidator().validate(START, END)
    assert result.run_id  # a uuid4 was minted
    assert result.status == service_result.STATUS_SUCCESS


def test_run_id_propagation_passthrough(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("AAPL", START)
    result = DataValidator().validate(START, END, run_id="run-123")
    assert result.run_id == "run-123"


def test_only_one_public_method() -> None:
    public = [
        name
        for name, _ in inspect.getmembers(DataValidator, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public == ["validate"]


# --------------------------------------------------------------------------- #
# 2. Valid rows remain ok
# --------------------------------------------------------------------------- #
def test_valid_rows_remain_ok(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("AAPL", START)
    _seed_price("MSFT", START)
    _seed_price("AAPL", date(2024, 1, 3))
    result = DataValidator().validate(START, END)

    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["rows_validated"] == 3
    assert result.metadata["rows_ok"] == 3
    assert result.metadata["rows_failed"] == 0
    assert result.metadata["status_updates_written"] == 0
    assert result.metadata["repair_queue_enqueued"] == 0
    assert all(r[P_DQ] == "ok" for r in _price_rows().values())
    assert _repair_rows() == []


def test_empty_range_clean_success(tmp_db_paths: dict[str, Path]) -> None:
    # No rows seeded in range: clean success, zero counts.
    result = DataValidator().validate(START, END)
    assert result.status == service_result.STATUS_SUCCESS
    assert result.rows_processed == 0
    assert result.metadata["rows_validated"] == 0
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


# --------------------------------------------------------------------------- #
# 3. OHLC rules (1–6): each escalates to failed AND enqueues one bad_ohlc repair
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "label,overrides",
    [
        ("null_open_raw", {"open_raw": None}),
        ("null_close_adj", {"close_adj": None}),
        ("high_lt_low_raw", {"high_raw": 8.0}),  # high 8 < low 9
        ("high_lt_low_adj", {"high_adj": 8.0}),
        ("open_above_high_raw", {"open_raw": 12.0}),
        ("close_below_low_raw", {"close_raw": 8.5}),
        ("open_out_of_range_adj", {"open_adj": 99.0}),
        ("close_out_of_range_adj", {"close_adj": 1.0}),
        ("non_positive_price", {"low_raw": 0.0, "open_raw": 0.0,
                                "close_raw": 0.0, "high_raw": 0.0}),
        ("negative_price", {"low_raw": -1.0}),
    ],
)
def test_ohlc_rule_flags_failed_and_enqueues_repair(
    tmp_db_paths: dict[str, Path], label: str, overrides: dict
) -> None:
    """Rules 1–6: OHLC structural failures escalate status and enqueue bad_ohlc."""
    _seed_price("BAD", START, **overrides)
    result = DataValidator().validate(START, END)

    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["rows_validated"] == 1
    assert result.metadata["rows_failed"] == 1
    assert result.metadata["rows_ok"] == 0
    assert result.metadata["status_updates_written"] == 1
    assert result.metadata["repair_queue_enqueued"] == 1

    assert _status_of("BAD", START) == "failed"

    repairs = _repair_rows()
    assert len(repairs) == 1
    r = repairs[0]
    assert r[R_TICKER] == "BAD"
    assert r[R_DATE] == START
    assert r[R_REASON] == "bad_ohlc"
    assert r[R_ATTEMPTS] == 0
    assert r[R_MAX] == 3
    assert r[R_LAST] is None
    assert r[R_STATUS] == "pending"
    assert r[R_UPDATED] is None
    assert r[R_CREATED] is not None


# Rule 7 (negative volume): escalates status but does NOT enqueue a repair.
# No suitable repair_reason in frozen enum — open spec gap G6.
@pytest.mark.parametrize(
    "label,overrides",
    [
        ("negative_volume_raw", {"volume_raw": -5}),
        ("negative_volume_adj", {"volume_adj": -5}),
    ],
)
def test_negative_volume_flags_failed_no_repair_enqueue(
    tmp_db_paths: dict[str, Path], label: str, overrides: dict
) -> None:
    """Rule 7: negative volume escalates status to failed but enqueues NO repair.

    The frozen repair_reason enum has no volume-specific value (gap G6).
    Force-mapping to bad_ohlc would be incorrect per the prompt contract, so
    the repair row is intentionally omitted.
    """
    _seed_price("VOL", START, **overrides)
    result = DataValidator().validate(START, END)

    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["rows_validated"] == 1
    assert result.metadata["rows_failed"] == 1
    assert result.metadata["rows_ok"] == 0
    assert result.metadata["status_updates_written"] == 1
    # Key assertion: no repair enqueued (gap G6).
    assert result.metadata["repair_queue_enqueued"] == 0

    assert _status_of("VOL", START) == "failed"
    assert _repair_rows() == []


def test_zero_volume_and_null_volume_adj_not_flagged(
    tmp_db_paths: dict[str, Path],
) -> None:
    # Zero volume and NULL volume_adj are legitimate (spec A4): not flagged.
    _seed_price("ZV", START, volume_raw=0, volume_adj=None)
    result = DataValidator().validate(START, END)
    assert result.metadata["rows_failed"] == 0
    assert _status_of("ZV", START) == "ok"
    assert _repair_rows() == []


def test_boundary_open_equals_low_is_ok(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("BND", START, open_raw=9.0, close_raw=11.0)  # == low / == high
    result = DataValidator().validate(START, END)
    assert result.metadata["rows_failed"] == 0
    assert _status_of("BND", START) == "ok"


def test_mixed_valid_and_invalid_counts(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("GOODA", START)
    _seed_price("GOODB", date(2024, 1, 3))
    _seed_price("BADA", START, high_raw=1.0)  # high < low
    _seed_price("BADB", date(2024, 1, 4), open_raw=None)
    result = DataValidator().validate(START, END)

    assert result.metadata["rows_validated"] == 4
    assert result.metadata["rows_ok"] == 2
    assert result.metadata["rows_failed"] == 2
    assert result.metadata["status_updates_written"] == 2
    assert result.metadata["repair_queue_enqueued"] == 2
    assert _status_of("GOODA", START) == "ok"
    assert _status_of("BADA", START) == "failed"
    assert _status_of("BADB", date(2024, 1, 4)) == "failed"


# --------------------------------------------------------------------------- #
# 4. Status precedence / no-downgrade
# --------------------------------------------------------------------------- #
def test_no_downgrade_quarantined_preserved(tmp_db_paths: dict[str, Path]) -> None:
    # A bad row already quarantined must NOT be lowered to failed.
    _seed_price("Q", START, high_raw=1.0, data_quality_status="quarantined")
    result = DataValidator().validate(START, END)

    assert result.metadata["rows_failed"] == 1  # still counted as failing data
    assert result.metadata["status_updates_written"] == 0  # but no status change
    assert _status_of("Q", START) == "quarantined"
    # A repair is still enqueued (dedup is on the logical key; harmless).
    assert result.metadata["repair_queue_enqueued"] == 1


def test_escalates_from_ok_and_warning_and_suspect(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("FO", START, high_raw=1.0, data_quality_status="ok")
    _seed_price("FW", START, high_raw=1.0, data_quality_status="warning")
    _seed_price("FS", START, high_raw=1.0, data_quality_status="suspect")
    result = DataValidator().validate(START, END)

    assert result.metadata["status_updates_written"] == 3
    assert _status_of("FO", START) == "failed"
    assert _status_of("FW", START) == "failed"
    assert _status_of("FS", START) == "failed"


def test_good_row_does_not_downgrade_worse_status(
    tmp_db_paths: dict[str, Path],
) -> None:
    # A row with valid data but a pre-existing worse status: Module 09 computes
    # "ok" (severity 0), which never overwrites a stored worse status.
    _seed_price("GW", START, data_quality_status="suspect")  # data is valid
    result = DataValidator().validate(START, END)

    assert result.metadata["rows_ok"] == 1
    assert result.metadata["rows_failed"] == 0
    assert result.metadata["status_updates_written"] == 0
    assert _status_of("GW", START) == "suspect"  # preserved, not lowered to ok
    assert _repair_rows() == []


# --------------------------------------------------------------------------- #
# 5. Idempotency / deterministic repair id
# --------------------------------------------------------------------------- #
def test_rerun_stable_no_duplicate_repairs_no_extra_updates(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("BAD", START, high_raw=1.0)
    first = DataValidator().validate(START, END)
    assert first.metadata["status_updates_written"] == 1
    assert first.metadata["repair_queue_enqueued"] == 1

    second = DataValidator().validate(START, END)
    # Status already failed -> no further change; repair dedup -> no new rows.
    assert second.metadata["rows_failed"] == 1
    assert second.metadata["status_updates_written"] == 0
    assert second.metadata["repair_queue_enqueued"] == 0
    assert len(_repair_rows()) == 1


def test_deterministic_repair_id_matches_logical_key(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("BAD", START, high_raw=1.0)
    DataValidator().validate(START, END)
    expected = dvmod._repair_id_for("BAD", START, "bad_ohlc")
    assert _repair_rows()[0][R_ID] == expected


def test_repair_id_helper_properties() -> None:
    a = dvmod._repair_id_for("AAPL", START, "bad_ohlc")
    b = dvmod._repair_id_for("AAPL", START, "bad_ohlc")
    c = dvmod._repair_id_for("MSFT", START, "bad_ohlc")
    d = dvmod._repair_id_for("AAPL", date(2024, 1, 3), "bad_ohlc")
    assert a == b
    assert a != c
    assert a != d


# --------------------------------------------------------------------------- #
# 6. Price values / forbidden columns are never modified
# --------------------------------------------------------------------------- #
def test_price_and_forbidden_columns_unchanged(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("BAD", START, high_raw=1.0, low_raw=9.0)  # invalid: high<low
    before = _price_rows()[("BAD", START)]
    DataValidator().validate(START, END)
    after = _price_rows()[("BAD", START)]

    # Every column except data_quality_status and updated_at is byte-identical.
    for idx in range(len(before)):
        if idx in (P_DQ, P_UPDATED):
            continue
        assert before[idx] == after[idx], f"column index {idx} changed"

    # The only legitimate mutations: status escalated, updated_at set.
    assert before[P_DQ] == "ok" and after[P_DQ] == "failed"
    assert before[P_UPDATED] is None and after[P_UPDATED] is not None
    # mutation_flag explicitly untouched.
    assert after[P_MUT] is False
    assert after[P_ADJ_FACTOR] is None


# --------------------------------------------------------------------------- #
# 7. No writes to forbidden tables / simulation DB
# --------------------------------------------------------------------------- #
def test_forbidden_tables_untouched(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("BAD", START, high_raw=1.0)
    DataValidator().validate(START, END)
    conn = dbm.connect("prod", read_only=True)
    try:
        for table in (
            "ticker_master",
            "ticker_universe_snapshot",
            "sector_etf_map",
            "daily_features",
        ):
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert count == 0, table
    finally:
        conn.close()


def test_simulation_db_not_created_or_written(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("BAD", START, high_raw=1.0)
    DataValidator().validate(START, END)
    # Module 09 only targets prod/debug; the simulation file is never opened.
    assert not tmp_db_paths[dbm.DB_ROLE_SIMULATION].exists()


# --------------------------------------------------------------------------- #
# 8. Existing repair rows are not processed / updated / deleted
# --------------------------------------------------------------------------- #
def test_existing_repair_rows_not_modified(tmp_db_paths: dict[str, Path]) -> None:
    # Pre-seed an unrelated pending repair row; Module 09 must not touch it.
    conn = dbm.connect("prod")
    try:
        conn.execute(
            "INSERT INTO data_repair_queue "
            "(repair_id, ticker, repair_date, repair_reason, attempts, "
            " max_attempts, last_attempt, status, created_at, updated_at) "
            "VALUES ('preexisting', 'OLD', ?, 'missing_price', 1, 3, NULL, "
            " 'pending', CAST(now() AS TIMESTAMP), NULL)",
            [START],
        )
    finally:
        conn.close()

    _seed_price("BAD", START, high_raw=1.0)
    DataValidator().validate(START, END)

    rows = {r[R_ID]: r for r in _repair_rows()}
    # The pre-existing row is unchanged.
    old = rows["preexisting"]
    assert old[R_TICKER] == "OLD"
    assert old[R_REASON] == "missing_price"
    assert old[R_ATTEMPTS] == 1
    assert old[R_STATUS] == "pending"
    # And a new bad_ohlc row was added for BAD.
    assert any(r[R_TICKER] == "BAD" and r[R_REASON] == "bad_ohlc" for r in rows.values())


# --------------------------------------------------------------------------- #
# 9. Guards fail before any DB write
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_role", ["simulation", "prod_typo", "", "PROD"])
def test_invalid_db_role_fails_before_writes(
    tmp_db_paths: dict[str, Path], bad_role: str
) -> None:
    _seed_price("AAPL", START)  # would be ok; must not be touched
    result = DataValidator().validate(START, END, db_role=bad_role)
    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)
    assert result.metadata["rows_validated"] == 0
    # No status change, no repairs.
    assert _repair_rows() == []
    assert _status_of("AAPL", START) == "ok"


def test_invalid_date_range_fails_before_writes(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("AAPL", START)
    result = DataValidator().validate(END, START)  # start > end
    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)
    assert _repair_rows() == []
    assert _status_of("AAPL", START) == "ok"


def test_simulation_role_never_opens_simulation_db(
    tmp_db_paths: dict[str, Path],
) -> None:
    result = DataValidator().validate(START, END, db_role="simulation")
    assert result.status == service_result.STATUS_FAILED
    assert not tmp_db_paths[dbm.DB_ROLE_SIMULATION].exists()


# --------------------------------------------------------------------------- #
# 10. Transaction rollback leaves no partial status updates or repair rows
# --------------------------------------------------------------------------- #
class _FailingConn:
    """Wrap a real connection and raise on the first status UPDATE."""

    def __init__(self, real: object) -> None:
        self._real = real

    def execute(self, sql: str, params: list | None = None):  # type: ignore[no-untyped-def]
        if sql.startswith("UPDATE daily_prices"):
            raise RuntimeError("forced failure")
        if params is None:
            return self._real.execute(sql)  # type: ignore[attr-defined]
        return self._real.execute(sql, params)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._real.close()  # type: ignore[attr-defined]


class _FailingManager:
    """Read-only connections pass through; write connections fail on UPDATE."""

    def connect(self, db_role: str, read_only: bool = False):  # type: ignore[no-untyped-def]
        real = dbm.connect(db_role, read_only=read_only)
        if read_only:
            return real
        return _FailingConn(real)


def test_transaction_rollback_leaves_no_partial_rows(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("BAD", START, high_raw=1.0)
    result = DataValidator(db_manager=_FailingManager()).validate(START, END)

    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    # Status not escalated and no repair row survived the rollback.
    assert _status_of("BAD", START) == "ok"
    assert _repair_rows() == []
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


def test_read_failure_returns_failed_no_writes(
    tmp_db_paths: dict[str, Path],
) -> None:
    class _ReadFailManager:
        def connect(self, db_role: str, read_only: bool = False):  # type: ignore[no-untyped-def]
            if read_only:
                raise RuntimeError("read boom")
            return dbm.connect(db_role, read_only=read_only)

    _seed_price("AAPL", START)
    result = DataValidator(db_manager=_ReadFailManager()).validate(START, END)
    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    assert _repair_rows() == []
    assert _status_of("AAPL", START) == "ok"


# --------------------------------------------------------------------------- #
# 11. debug role works the same
# --------------------------------------------------------------------------- #
def test_debug_role_validates(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("BAD", START, high_raw=1.0, role="debug")
    result = DataValidator().validate(START, END, db_role="debug")
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["db_role"] == "debug"
    assert _status_of("BAD", START, role="debug") == "failed"
    assert len(_repair_rows(role="debug")) == 1
    # prod untouched.
    assert _repair_rows(role="prod") == []


# --------------------------------------------------------------------------- #
# 12. Range filtering: rows outside [start, end] are not validated
# --------------------------------------------------------------------------- #
def test_rows_outside_range_ignored(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("IN", date(2024, 1, 3), high_raw=1.0)  # invalid, in range
    _seed_price("OUT", date(2024, 2, 1), high_raw=1.0)  # invalid, out of range
    result = DataValidator().validate(START, END)
    assert result.metadata["rows_validated"] == 1
    assert result.metadata["rows_failed"] == 1
    assert _status_of("IN", date(2024, 1, 3)) == "failed"
    assert _status_of("OUT", date(2024, 2, 1)) == "ok"  # untouched
    repairs = _repair_rows()
    assert len(repairs) == 1 and repairs[0][R_TICKER] == "IN"


# --------------------------------------------------------------------------- #
# 13. ServiceResult metadata keys (exact, every path) + invariants
# --------------------------------------------------------------------------- #
def test_metadata_keys_exact_success(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("AAPL", START)
    result = DataValidator().validate(START, END)
    assert isinstance(result, ServiceResult)
    assert result.has_valid_status() and result.run_id
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)
    assert result.metadata["start_date"] == START.isoformat()
    assert result.metadata["end_date"] == END.isoformat()


def test_metadata_counts_invariant(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("A", START)
    _seed_price("B", START, high_raw=1.0)
    result = DataValidator().validate(START, END)
    m = result.metadata
    assert m["rows_ok"] + m["rows_failed"] == m["rows_validated"]
    assert result.rows_processed == m["rows_validated"]


def test_metadata_keys_exact_on_guard_failure(
    tmp_db_paths: dict[str, Path],
) -> None:
    result = DataValidator().validate(START, END, db_role="simulation")
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


# --------------------------------------------------------------------------- #
# 13b. Open spec gaps are documented and not invented
# --------------------------------------------------------------------------- #
def test_spec_documents_open_gaps_not_invented() -> None:
    """Assert that the module spec explicitly documents every ambiguous/missing
    rule as an open gap or N/A, and does not invent behavior for it.

    This test reads M09_DATA_VALIDATOR_SPEC.md and checks that the known
    open gaps are present by keyword, confirming they are documented rather
    than silently omitted or incorrectly implemented.
    """
    import pathlib
    import conftest  # conftest.ROOT is the project root; always correct regardless
                     # of where the zip is extracted or pytest is invoked from.

    spec_path = pathlib.Path(conftest.ROOT) / "M09_DATA_VALIDATOR_SPEC.md"
    assert spec_path.exists(), (
        f"M09_DATA_VALIDATOR_SPEC.md not found at project root {spec_path}. "
        "Ensure the spec file is placed alongside conftest.py."
    )
    spec = spec_path.read_text(encoding="utf-8")

    # G1: missing rows / missing expected trading days
    assert "missing" in spec.lower() and ("trading day" in spec.lower() or "G1" in spec)
    # G2: duplicate rows (N/A via PK)
    assert "duplicate" in spec.lower() and ("G2" in spec or "PRIMARY KEY" in spec or "N/A" in spec)
    # G3: large price jumps / outliers (out of scope, Module 10)
    assert "jump" in spec.lower() or "outlier" in spec.lower() or "G3" in spec
    # G4: stale / incomplete ticker coverage
    assert "stale" in spec.lower() or "G4" in spec
    # G6: negative volume has no suitable repair_reason in frozen enum
    assert "G6" in spec
    assert "bad_volume" in spec or "no suitable" in spec.lower()
    # The spec must NOT claim negative volume maps to bad_ohlc (force-map removed).
    # It may mention bad_ohlc in other rules, but the volume row must say "no" or gap.
    # We check the rule-7 table row does not say "yes | bad_ohlc".
    assert "volume_raw < 0" in spec or "negative_volume" in spec
    # Confirm the rule-7 line does not claim repair_queue_enqueued for volume.
    rule7_line = [l for l in spec.splitlines() if "negative_volume" in l and "|" in l]
    assert rule7_line, "rule 7 table row not found in spec"
    assert "bad_ohlc" not in rule7_line[0] or "no" in rule7_line[0].lower()


# --------------------------------------------------------------------------- #
# 14. Static scan: forbidden imports / direct DB / DDL / print / forbidden cols
# --------------------------------------------------------------------------- #
def _module_raw_source() -> str:
    return Path(dvmod.__file__).read_text(encoding="utf-8")


def _module_code_only() -> str:
    """Executable source with comments and string literals removed."""
    import io
    import token
    import tokenize

    pieces: list[str] = []
    readline = io.StringIO(_module_raw_source()).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type in (token.COMMENT, token.STRING):
            continue
        if tok.type == getattr(token, "FSTRING_MIDDLE", -1):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)


def _sql_literals() -> str:
    return " ".join(
        v
        for k, v in vars(dvmod).items()
        if isinstance(v, str) and not k.startswith("__")
    )


def test_no_direct_duckdb_import_or_connect() -> None:
    import re

    code = _module_code_only()
    assert not re.search(r"\bimport\s+duckdb\b", code)
    assert not re.search(r"\bfrom\s+duckdb\b", code)
    assert "duckdb.connect(" not in _module_raw_source()
    assert not hasattr(dvmod, "duckdb")


def test_no_vendor_or_provider_import() -> None:
    code = _module_code_only()
    assert "yfinance" not in code
    assert "yahoo_provider" not in code
    assert "provider_interface" not in code  # M09 never calls providers
    assert "get_price_history" not in code
    assert not hasattr(dvmod, "yfinance")


def test_no_attach_or_ddl_in_source() -> None:
    code = _module_code_only().upper()
    sql = _sql_literals().upper()
    for forbidden in ("ATTACH", "ALTER TABLE", "CREATE TABLE", "CREATE TYPE", "DROP "):
        assert forbidden not in sql, forbidden
        assert forbidden not in code, forbidden


def test_no_forbidden_table_writes_in_source() -> None:
    sql = _sql_literals().upper()
    # Only daily_prices (status) and data_repair_queue may be written.
    for table in (
        "TICKER_MASTER",
        "TICKER_UNIVERSE_SNAPSHOT",
        "SECTOR_ETF_MAP",
        "DAILY_FEATURES",
    ):
        for verb in ("INSERT INTO " + table, "UPDATE " + table, "DELETE FROM " + table):
            assert verb not in sql, verb


def test_no_price_or_protected_column_writes_in_source() -> None:
    # The UPDATE against daily_prices must only set data_quality_status and
    # updated_at — never price/volume/adjustment/mutation columns.
    sql = _sql_literals()
    update_stmts = [
        s for s in sql.split(";") if "UPDATE daily_prices" in s
    ] or [dvmod._ESCALATE_STATUS]
    joined = " ".join(update_stmts)
    for forbidden_col in (
        "open_raw =",
        "high_raw =",
        "low_raw =",
        "close_raw =",
        "open_adj =",
        "close_adj =",
        "volume_raw =",
        "dividend_amount =",
        "split_ratio =",
        "adjustment_factor =",
        "mutation_flag =",
    ):
        assert forbidden_col not in joined, forbidden_col
    assert "data_quality_status =" in joined


def test_repair_queue_not_processed_in_source() -> None:
    # Module 09 only INSERTs into data_repair_queue; it never updates/deletes it.
    sql = _sql_literals().upper()
    assert "UPDATE DATA_REPAIR_QUEUE" not in sql
    assert "DELETE FROM DATA_REPAIR_QUEUE" not in sql


def test_no_print_in_source() -> None:
    assert "print(" not in _module_code_only()
