"""Tests for Module 10 — Mutation Detector.

All tests run fully offline (no network, no provider) and never touch the real
prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects every
DuckDB settings path into pytest ``tmp_path`` and applies the real Module 03
schema there (mirroring ``tests/test_data_validator.py``). Rows are seeded
directly into ``daily_prices`` per test; Module 10 reads them read-only and
writes only ``daily_prices.mutation_flag`` / ``adjustment_factor`` (+ the row
``updated_at`` audit column) and inserts into ``data_repair_queue`` /
``feature_rebuild_log``.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path

import pytest

from app.config import constants
from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.mutation import mutation_detector as mdmod
from app.services.mutation.mutation_detector import MutationDetector
from app.utils import service_result
from app.utils.service_result import ServiceResult

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
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
    }
)

START = date(2024, 1, 2)
END = date(2024, 1, 31)
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
# not Module 10 — Module 10 itself never inserts price rows).
# --------------------------------------------------------------------------- #
_INSERT_PRICE = (
    "INSERT INTO daily_prices "
    "(ticker, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
    " open_adj, high_adj, low_adj, close_adj, volume_adj, "
    " dividend_amount, split_ratio, adjustment_factor, source_provider, "
    " data_quality_status, mutation_flag, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, "
    " CAST(now() AS TIMESTAMP), NULL)"
)


def _price_defaults() -> dict[str, object]:
    return dict(
        open_raw=10.0,
        high_raw=11.0,
        low_raw=9.0,
        close_raw=10.0,
        volume_raw=1000,
        open_adj=10.0,
        high_adj=11.0,
        low_adj=9.0,
        close_adj=10.0,
        volume_adj=None,
        split_ratio=1.0,
        adjustment_factor=None,
        data_quality_status="ok",
        mutation_flag=False,
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
                vals["split_ratio"],
                vals["adjustment_factor"],
                SOURCE,
                vals["data_quality_status"],
                vals["mutation_flag"],
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


def _rebuild_rows(role: str = "prod") -> list[tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT rebuild_id, ticker, reason, affected_start_date, "
            "affected_end_date, feature_schema_version, triggered_at, status "
            "FROM feature_rebuild_log ORDER BY ticker, affected_start_date"
        ).fetchall()
    finally:
        conn.close()
    return rows


(
    B_ID,
    B_TICKER,
    B_REASON,
    B_START,
    B_END,
    B_VERSION,
    B_TRIGGERED,
    B_STATUS,
) = range(8)


# --------------------------------------------------------------------------- #
# 1. Public API: import, exact signature, run_id propagation
# --------------------------------------------------------------------------- #
def test_public_api_exact_signature() -> None:
    sig = inspect.signature(MutationDetector.detect)
    params = list(sig.parameters)
    assert params == ["self", "start_date", "end_date", "db_role", "run_id"]
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["run_id"].default is None


def test_run_id_propagation_minted_when_none(tmp_db_paths: dict[str, Path]) -> None:
    result = MutationDetector().detect(START, END, db_role="prod")
    assert isinstance(result, ServiceResult)
    assert result.run_id  # non-empty uuid4


def test_run_id_propagation_passthrough(tmp_db_paths: dict[str, Path]) -> None:
    result = MutationDetector().detect(START, END, db_role="prod", run_id="rid-123")
    assert result.run_id == "rid-123"


def test_only_one_public_method() -> None:
    public = [
        name
        for name, _ in inspect.getmembers(MutationDetector, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public == ["detect"]


def test_metadata_keys_exact_success(tmp_db_paths: dict[str, Path]) -> None:
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.status == service_result.STATUS_SUCCESS
    assert frozenset(result.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_exact_on_guard_failure() -> None:
    result = MutationDetector().detect(START, END, db_role="simulation")
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == REQUIRED_METADATA_KEYS


# --------------------------------------------------------------------------- #
# 2. adjustment_factor derivation
# --------------------------------------------------------------------------- #
def test_adjustment_factor_derived_and_written(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("AAA", START, close_raw=10.0, close_adj=8.0, adjustment_factor=None)
    result = MutationDetector().detect(START, END, db_role="prod")
    rows = _price_rows()
    assert rows[("AAA", START)][P_ADJ_FACTOR] == pytest.approx(0.8)
    assert result.metadata["adjustment_factors_written"] == 1
    assert result.metadata["rows_processed"] == 1
    # No split -> no mutation, no repair/rebuild.
    assert result.metadata["mutation_rows_detected"] == 0
    assert result.metadata["repair_queue_enqueued"] == 0
    assert result.metadata["rebuild_logs_enqueued"] == 0


def test_adjustment_factor_null_when_underivable(tmp_db_paths: dict[str, Path]) -> None:
    # close_raw zero, close_raw None, close_adj None -> all underivable (NULL).
    _seed_price("ZRO", date(2024, 1, 2), close_raw=0.0, close_adj=5.0)
    _seed_price("RNN", date(2024, 1, 3), close_raw=None, close_adj=5.0)
    _seed_price("ANN", date(2024, 1, 4), close_raw=10.0, close_adj=None)
    MutationDetector().detect(START, END, db_role="prod")
    rows = _price_rows()
    assert rows[("ZRO", date(2024, 1, 2))][P_ADJ_FACTOR] is None
    assert rows[("RNN", date(2024, 1, 3))][P_ADJ_FACTOR] is None
    assert rows[("ANN", date(2024, 1, 4))][P_ADJ_FACTOR] is None


def test_adjustment_factor_cleared_to_null_counts(tmp_db_paths: dict[str, Path]) -> None:
    # Stored non-null factor but now underivable -> cleared to NULL, counted.
    _seed_price("CLR", START, close_raw=None, close_adj=None, adjustment_factor=1.23)
    result = MutationDetector().detect(START, END, db_role="prod")
    assert _price_rows()[("CLR", START)][P_ADJ_FACTOR] is None
    assert result.metadata["adjustment_factors_written"] == 1


def test_adjustment_factor_unchanged_not_rewritten(tmp_db_paths: dict[str, Path]) -> None:
    # Stored factor already equals close_adj/close_raw -> no write.
    _seed_price("SAME", START, close_raw=10.0, close_adj=8.0, adjustment_factor=0.8)
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.metadata["adjustment_factors_written"] == 0


# --------------------------------------------------------------------------- #
# 3. data-quality boundary: non-ok rows skipped / untouched
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_status", ["failed", "suspect", "warning", "quarantined"])
def test_non_ok_rows_skipped_and_untouched(
    tmp_db_paths: dict[str, Path], bad_status: str
) -> None:
    _seed_price(
        "BAD",
        START,
        data_quality_status=bad_status,
        close_raw=10.0,
        close_adj=8.0,
        split_ratio=2.0,  # would be a mutation if eligible
        adjustment_factor=None,
        mutation_flag=False,
    )
    result = MutationDetector().detect(START, END, db_role="prod")
    row = _price_rows()[("BAD", START)]
    assert row[P_ADJ_FACTOR] is None  # not derived
    assert row[P_MUT] is False  # not flagged
    assert result.metadata["rows_read"] == 1
    assert result.metadata["rows_processed"] == 0
    assert result.metadata["rows_skipped_non_ok"] == 1
    assert result.metadata["mutation_rows_detected"] == 0
    assert _repair_rows() == []
    assert _rebuild_rows() == []


# --------------------------------------------------------------------------- #
# 4. explicit split detection -> flag, repair, rebuild
# --------------------------------------------------------------------------- #
def test_explicit_split_flags_and_enqueues(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("SPL", date(2024, 1, 10), split_ratio=2.0, close_raw=10.0, close_adj=5.0)
    result = MutationDetector().detect(START, END, db_role="prod")

    row = _price_rows()[("SPL", date(2024, 1, 10))]
    assert row[P_MUT] is True
    assert row[P_ADJ_FACTOR] == pytest.approx(0.5)

    assert result.metadata["mutation_rows_detected"] == 1
    assert result.metadata["mutation_flags_written"] == 1
    assert result.metadata["tickers_with_mutation"] == 1
    assert result.metadata["repair_queue_enqueued"] == 1
    assert result.metadata["rebuild_logs_enqueued"] == 1

    repairs = _repair_rows()
    assert len(repairs) == 1
    assert repairs[0][R_TICKER] == "SPL"
    assert repairs[0][R_REASON] == "mutation"
    assert repairs[0][R_DATE] == date(2024, 1, 10)
    assert repairs[0][R_STATUS] == "pending"
    assert repairs[0][R_ATTEMPTS] == 0
    assert repairs[0][R_MAX] == 3

    rebuilds = _rebuild_rows()
    assert len(rebuilds) == 1
    assert rebuilds[0][B_TICKER] == "SPL"
    assert rebuilds[0][B_REASON] == "mutation"
    assert rebuilds[0][B_START] == date(2024, 1, 10)
    assert rebuilds[0][B_END] is None
    assert rebuilds[0][B_VERSION] == constants.FEATURE_SCHEMA_VERSION
    assert rebuilds[0][B_STATUS] == "pending"


def test_split_ratio_one_and_null_not_detected(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("ONE", date(2024, 1, 5), split_ratio=1.0)
    _seed_price("NUL", date(2024, 1, 6), split_ratio=None)
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.metadata["mutation_rows_detected"] == 0
    assert _price_rows()[("ONE", date(2024, 1, 5))][P_MUT] is False
    assert _price_rows()[("NUL", date(2024, 1, 6))][P_MUT] is False
    assert _repair_rows() == []


def test_first_mutation_date_used_for_repair_and_rebuild(
    tmp_db_paths: dict[str, Path],
) -> None:
    # Two split rows for one ticker; earliest date keys repair/rebuild.
    _seed_price("MUL", date(2024, 1, 20), split_ratio=2.0)
    _seed_price("MUL", date(2024, 1, 8), split_ratio=3.0)
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.metadata["mutation_rows_detected"] == 2
    assert result.metadata["tickers_with_mutation"] == 1
    assert result.metadata["repair_queue_enqueued"] == 1
    assert result.metadata["rebuild_logs_enqueued"] == 1
    assert _repair_rows()[0][R_DATE] == date(2024, 1, 8)
    assert _rebuild_rows()[0][B_START] == date(2024, 1, 8)


# --------------------------------------------------------------------------- #
# 5. clean rows unchanged; no-downgrade on re-run
# --------------------------------------------------------------------------- #
def test_clean_rows_unchanged(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("CLN", START, close_raw=10.0, close_adj=10.0, adjustment_factor=1.0)
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.metadata["adjustment_factors_written"] == 0
    assert result.metadata["mutation_rows_detected"] == 0
    assert result.metadata["mutation_flags_written"] == 0
    assert _price_rows()[("CLN", START)][P_MUT] is False


def test_no_downgrade_existing_true_flag_preserved(
    tmp_db_paths: dict[str, Path],
) -> None:
    # Row already mutation_flag TRUE and a split -> detected but flag write 0.
    _seed_price("PRE", START, split_ratio=2.0, mutation_flag=True)
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.metadata["mutation_rows_detected"] == 1
    assert result.metadata["mutation_flags_written"] == 0  # already TRUE
    assert _price_rows()[("PRE", START)][P_MUT] is True


def test_rerun_stable_no_duplicates(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("RUN", date(2024, 1, 10), split_ratio=2.0, close_raw=10.0, close_adj=5.0)

    first = MutationDetector().detect(START, END, db_role="prod")
    assert first.metadata["mutation_flags_written"] == 1
    assert first.metadata["adjustment_factors_written"] == 1
    assert first.metadata["repair_queue_enqueued"] == 1
    assert first.metadata["rebuild_logs_enqueued"] == 1

    second = MutationDetector().detect(START, END, db_role="prod")
    assert second.metadata["mutation_rows_detected"] == 1  # still detected
    assert second.metadata["mutation_flags_written"] == 0  # already TRUE
    assert second.metadata["adjustment_factors_written"] == 0  # unchanged
    assert second.metadata["repair_queue_enqueued"] == 0  # dedup
    assert second.metadata["rebuild_logs_enqueued"] == 0  # dedup

    assert len(_repair_rows()) == 1
    assert len(_rebuild_rows()) == 1


# --------------------------------------------------------------------------- #
# 6. deterministic ids
# --------------------------------------------------------------------------- #
def test_deterministic_repair_id_matches_logical_key() -> None:
    rid = mdmod._repair_id_for("XYZ", date(2024, 1, 10), "mutation")
    assert rid == mdmod._repair_id_for("XYZ", date(2024, 1, 10), "mutation")
    assert rid != mdmod._repair_id_for("XYZ", date(2024, 1, 11), "mutation")
    assert rid != mdmod._repair_id_for("ABC", date(2024, 1, 10), "mutation")


def test_deterministic_rebuild_id_matches_logical_key() -> None:
    bid = mdmod._rebuild_id_for("XYZ", date(2024, 1, 10), "mutation")
    assert bid == mdmod._rebuild_id_for("XYZ", date(2024, 1, 10), "mutation")
    assert bid != mdmod._rebuild_id_for("XYZ", date(2024, 1, 11), "mutation")
    # Repair and rebuild ids for the same key differ (distinct namespaces).
    assert bid != mdmod._repair_id_for("XYZ", date(2024, 1, 10), "mutation")


def test_repair_id_written_matches_helper(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("IDM", date(2024, 1, 9), split_ratio=2.0)
    MutationDetector().detect(START, END, db_role="prod")
    expected = mdmod._repair_id_for("IDM", date(2024, 1, 9), "mutation")
    assert _repair_rows()[0][R_ID] == expected
    expected_b = mdmod._rebuild_id_for("IDM", date(2024, 1, 9), "mutation")
    assert _rebuild_rows()[0][B_ID] == expected_b


# --------------------------------------------------------------------------- #
# 7. write ownership: only mutation_flag / adjustment_factor / updated_at change
# --------------------------------------------------------------------------- #
def test_protected_columns_unchanged(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price(
        "OWN",
        START,
        close_raw=10.0,
        close_adj=5.0,
        split_ratio=2.0,
        dividend_amount=0.0,
    )
    before = _price_rows()[("OWN", START)]
    MutationDetector().detect(START, END, db_role="prod")
    after = _price_rows()[("OWN", START)]

    # Allowed to change: mutation_flag, adjustment_factor, updated_at.
    assert after[P_MUT] != before[P_MUT]  # FALSE -> TRUE
    assert after[P_ADJ_FACTOR] is not None and before[P_ADJ_FACTOR] is None
    assert after[P_UPDATED] is not None

    # Everything else must be byte-identical.
    for idx in (
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
        P_SOURCE,
        P_DQ,
        P_CREATED,
    ):
        assert after[idx] == before[idx], idx


def test_forbidden_tables_untouched(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("FRB", START, split_ratio=2.0)
    MutationDetector().detect(START, END, db_role="prod")
    conn = dbm.connect("prod", read_only=True)
    try:
        for table in (
            "ticker_master",
            "ticker_universe_snapshot",
            "sector_etf_map",
            "daily_features",
            "step5_proposals",
            "signal_outcomes",
        ):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 8. no writes to simulation DB; simulation role refused
# --------------------------------------------------------------------------- #
def test_simulation_db_not_created_or_written(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("SIM", START, split_ratio=2.0)
    MutationDetector().detect(START, END, db_role="prod")
    assert not tmp_db_paths[dbm.DB_ROLE_SIMULATION].exists()


def test_simulation_role_never_opens_simulation_db(
    tmp_db_paths: dict[str, Path],
) -> None:
    result = MutationDetector().detect(START, END, db_role="simulation")
    assert result.status == service_result.STATUS_FAILED
    assert not tmp_db_paths[dbm.DB_ROLE_SIMULATION].exists()


# --------------------------------------------------------------------------- #
# 9. guards fail before any DB access
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_role", ["simulation", "prod_typo", "", "PROD"])
def test_invalid_db_role_fails_before_writes(bad_role: str) -> None:
    # No tmp_db_paths fixture: any DB access would hit real settings paths and
    # raise. A clean ``failed`` proves the guard short-circuits before I/O.
    result = MutationDetector().detect(START, END, db_role=bad_role)
    assert result.status == service_result.STATUS_FAILED
    assert result.rows_processed == 0
    assert all(result.metadata[k] == 0 for k in (
        "rows_read",
        "rows_processed",
        "rows_skipped_non_ok",
        "adjustment_factors_written",
        "mutation_rows_detected",
        "mutation_flags_written",
        "tickers_with_mutation",
        "repair_queue_enqueued",
        "rebuild_logs_enqueued",
    ))


def test_invalid_date_range_fails_before_writes() -> None:
    result = MutationDetector().detect(END, START, db_role="prod")
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["db_role"] == "prod"
    assert result.metadata["rows_read"] == 0


# --------------------------------------------------------------------------- #
# 10. transaction rollback leaves no partial writes
# --------------------------------------------------------------------------- #
class _FailingConnection:
    """Wraps a real connection but raises on the repair/rebuild INSERT."""

    def __init__(self, real, fail_sql_fragment: str) -> None:
        self._real = real
        self._fail = fail_sql_fragment

    def execute(self, sql, *args, **kwargs):
        if self._fail in sql:
            raise RuntimeError("boom")
        return self._real.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._real.close()


class _FailingManager:
    """DB manager wrapper that fails write-connection INSERTs into rebuild log."""

    def __init__(self, fail_sql_fragment: str) -> None:
        self._fail = fail_sql_fragment

    def connect(self, db_role: str, read_only: bool = False):
        real = dbm.connect(db_role, read_only=read_only)
        if read_only:
            return real  # read phase untouched
        return _FailingConnection(real, self._fail)


def test_transaction_rollback_leaves_no_partial_writes(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_price("RBK", date(2024, 1, 10), split_ratio=2.0, close_raw=10.0, close_adj=5.0)
    # Fail on the rebuild-log insert, AFTER adj-factor + flag + repair writes
    # in the same transaction; all must roll back.
    engine = MutationDetector(db_manager=_FailingManager("feature_rebuild_log"))
    result = engine.detect(START, END, db_role="prod")

    assert result.status == service_result.STATUS_FAILED
    # rows_processed must equal metadata["rows_processed"] on write failure (spec §4).
    assert result.rows_processed == result.metadata["rows_processed"]
    assert result.rows_processed == 1  # one eligible ok row was read and planned
    # Durable write counts are zero on failure (rollback mandatory).
    assert result.metadata["adjustment_factors_written"] == 0
    assert result.metadata["mutation_flags_written"] == 0
    assert result.metadata["repair_queue_enqueued"] == 0
    assert result.metadata["rebuild_logs_enqueued"] == 0
    # Read/compute counts still reflected (not zeroed on write failure).
    assert result.metadata["rows_read"] == 1
    assert result.metadata["rows_processed"] == 1
    assert result.metadata["rows_skipped_non_ok"] == 0
    assert result.metadata["mutation_rows_detected"] == 1
    assert result.metadata["tickers_with_mutation"] == 1

    # Nothing durably written.
    row = _price_rows()[("RBK", date(2024, 1, 10))]
    assert row[P_MUT] is False
    assert row[P_ADJ_FACTOR] is None
    assert _repair_rows() == []
    assert _rebuild_rows() == []


def test_read_failure_returns_failed_no_writes(tmp_db_paths: dict[str, Path]) -> None:
    class _ReadFailManager:
        def connect(self, db_role: str, read_only: bool = False):
            if read_only:
                raise RuntimeError("read boom")
            return dbm.connect(db_role, read_only=read_only)

    result = MutationDetector(db_manager=_ReadFailManager()).detect(
        START, END, db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["rows_read"] == 0
    assert _repair_rows() == []


# --------------------------------------------------------------------------- #
# 11. debug role works; rows outside range ignored
# --------------------------------------------------------------------------- #
def test_debug_role_detects(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("DBG", date(2024, 1, 10), role="debug", split_ratio=2.0)
    result = MutationDetector().detect(START, END, db_role="debug")
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["db_role"] == "debug"
    assert result.metadata["mutation_flags_written"] == 1
    assert _price_rows("debug")[("DBG", date(2024, 1, 10))][P_MUT] is True


def test_rows_outside_range_ignored(tmp_db_paths: dict[str, Path]) -> None:
    _seed_price("OUT", date(2023, 12, 31), split_ratio=2.0)  # before range
    _seed_price("OUT", date(2024, 2, 1), split_ratio=2.0)  # after range
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.metadata["rows_read"] == 0
    assert result.metadata["mutation_rows_detected"] == 0
    assert _price_rows()[("OUT", date(2023, 12, 31))][P_MUT] is False
    assert _price_rows()[("OUT", date(2024, 2, 1))][P_MUT] is False


# --------------------------------------------------------------------------- #
# 12. ratio-discontinuity gap G1 documented (not invented)
# --------------------------------------------------------------------------- #
def test_spec_documents_open_gap_g1() -> None:
    spec = Path(__file__).resolve().parents[1] / "M10_MUTATION_DETECTOR_SPEC.md"
    text = spec.read_text(encoding="utf-8")
    assert "G1" in text
    # The detector must not invent a discontinuity threshold.
    code = _module_code_only()
    assert "discontinuity" not in code.lower()


def test_no_close_ratio_discontinuity_detection(tmp_db_paths: dict[str, Path]) -> None:
    # Two consecutive rows with a large close_raw/close_adj ratio jump but
    # split_ratio == 1: with G1 unimplemented, neither is flagged a mutation.
    _seed_price("JMP", date(2024, 1, 8), close_raw=10.0, close_adj=10.0, split_ratio=1.0)
    _seed_price("JMP", date(2024, 1, 9), close_raw=10.0, close_adj=5.0, split_ratio=1.0)
    result = MutationDetector().detect(START, END, db_role="prod")
    assert result.metadata["mutation_rows_detected"] == 0
    assert _repair_rows() == []


# --------------------------------------------------------------------------- #
# 13. static source scans
# --------------------------------------------------------------------------- #
def _module_raw_source() -> str:
    return Path(mdmod.__file__).read_text(encoding="utf-8")


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
        for k, v in vars(mdmod).items()
        if isinstance(v, str) and not k.startswith("__")
    )


def _daily_prices_set_clauses() -> str:
    """Return only the SET portions of UPDATE statements on daily_prices."""
    sql = _sql_literals()
    pieces: list[str] = []
    for stmt in re.split(r"(?i)UPDATE daily_prices", sql)[1:]:
        m = re.search(r"(?is)\bSET\b(.*?)\bWHERE\b", stmt)
        if m:
            pieces.append(m.group(1))
    return " ".join(pieces)


def test_no_direct_duckdb_import_or_connect() -> None:
    code = _module_code_only()
    assert not re.search(r"\bimport\s+duckdb\b", code)
    assert not re.search(r"\bfrom\s+duckdb\b", code)
    assert "duckdb.connect(" not in _module_raw_source()
    assert not hasattr(mdmod, "duckdb")


def test_no_vendor_or_provider_import() -> None:
    code = _module_code_only()
    for forbidden in ("yfinance", "provider", "requests", "urllib", "httpx"):
        assert forbidden not in code, forbidden


def test_no_attach_or_ddl_in_source() -> None:
    code = _module_code_only().upper()
    sql = _sql_literals().upper()
    for forbidden in ("ATTACH", "ALTER TABLE", "CREATE TABLE", "CREATE TYPE", "DROP "):
        assert forbidden not in sql, forbidden
        assert forbidden not in code, forbidden


def test_no_forbidden_table_writes_in_source() -> None:
    sql = _sql_literals().upper()
    # Only daily_prices, data_repair_queue, feature_rebuild_log may be written.
    for table in (
        "TICKER_MASTER",
        "TICKER_UNIVERSE_SNAPSHOT",
        "SECTOR_ETF_MAP",
        "DAILY_FEATURES",
        "STEP3_CANDIDATES",
        "STEP5_PROPOSALS",
        "SIGNAL_OUTCOMES",
        "OUTCOME_TRACKING_QUEUE",
    ):
        for verb in ("INSERT INTO " + table, "UPDATE " + table, "DELETE FROM " + table):
            assert verb not in sql, verb


def test_daily_prices_only_allowed_columns_written() -> None:
    set_clauses = _daily_prices_set_clauses()
    assert set_clauses, "expected at least one UPDATE daily_prices SET clause"
    # Forbidden assignments anywhere in the SET clauses.
    for forbidden_col in (
        "open_raw =",
        "high_raw =",
        "low_raw =",
        "close_raw =",
        "open_adj =",
        "close_adj =",
        "volume_raw =",
        "volume_adj =",
        "dividend_amount =",
        "split_ratio =",
        "data_quality_status =",
        "created_at =",
    ):
        assert forbidden_col not in set_clauses, forbidden_col
    # Allowed assignments present across the statements.
    assert "mutation_flag = TRUE" in set_clauses
    assert "adjustment_factor =" in set_clauses
    assert "updated_at =" in set_clauses


def test_existing_repair_and_rebuild_rows_not_processed() -> None:
    sql = _sql_literals().upper()
    assert "UPDATE DATA_REPAIR_QUEUE" not in sql
    assert "DELETE FROM DATA_REPAIR_QUEUE" not in sql
    assert "UPDATE FEATURE_REBUILD_LOG" not in sql
    assert "DELETE FROM FEATURE_REBUILD_LOG" not in sql


def test_no_print_in_source() -> None:
    assert "print(" not in _module_code_only()
