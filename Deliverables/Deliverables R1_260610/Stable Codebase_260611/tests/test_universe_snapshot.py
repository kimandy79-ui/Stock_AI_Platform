"""Tests for Module 06 — Universe Snapshot Engine.

All tests run fully offline (no network, no live provider) and never touch the
real prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects
every DuckDB settings path into pytest ``tmp_path`` and applies the real
Module 03 schema there (mirroring ``tests/test_schema_manager.py`` and
``tests/test_duckdb_manager.py``). Input is fed as in-test ``TickerInfo`` lists.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.providers.provider_interface import TickerInfo
from app.services.universe import universe_snapshot as us
from app.services.universe.universe_snapshot import UniverseSnapshotEngine
from app.utils import service_result
from app.utils.service_result import ServiceResult

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
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
    }
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect all DuckDB settings paths into ``tmp_path`` and apply schema.

    Mirrors the isolation discipline of ``tests/test_schema_manager.py``: no
    test ever touches the real ``data/duckdb/`` tree. The real Module 03 schema
    is applied to the temp prod and debug databases so Module 06 has the
    ``ticker_master`` / ``ticker_universe_snapshot`` / ``sector_etf_map`` tables
    to work against.
    """
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


def _info(ticker: str, **kw: object) -> TickerInfo:
    kw.setdefault("symbol_type", constants.SYMBOL_TYPE_STOCK)
    return TickerInfo(ticker=ticker, **kw)  # type: ignore[arg-type]


# --- DB read helpers (via the approved manager, read-only) ------------------ #
def _master_rows(role: str = "prod") -> dict[str, tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT ticker, yahoo_symbol, company_name, exchange, sector, "
            "industry, security_type, symbol_type, active_flag, delisted_flag, "
            "first_seen, last_seen FROM ticker_master"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r for r in rows}


def _snapshot_rows(role: str = "prod") -> list[tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT snapshot_month, ticker, exchange, sector, industry, "
            "market_cap_bucket, active_flag, source FROM ticker_universe_snapshot "
            "ORDER BY snapshot_month, ticker"
        ).fetchall()
    finally:
        conn.close()
    return rows


def _sector_etf_rows(role: str = "prod") -> list[tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute("SELECT * FROM sector_etf_map").fetchall()
    finally:
        conn.close()
    return rows


# Master row tuple indexes (match the SELECT in _master_rows).
M_TICKER, M_YHOO, M_NAME, M_EXCH, M_SECTOR, M_IND, M_SECTYPE, M_SYMTYPE = range(8)
M_ACTIVE, M_DELISTED, M_FIRST_SEEN, M_LAST_SEEN = 8, 9, 10, 11
# Snapshot row tuple indexes.
S_MONTH, S_TICKER, S_EXCH, S_SECTOR, S_IND, S_BUCKET, S_ACTIVE, S_SOURCE = range(8)


# --------------------------------------------------------------------------- #
# 1. Import smoke / exact signature
# --------------------------------------------------------------------------- #
def test_import_and_signature() -> None:
    assert hasattr(us, "UniverseSnapshotEngine")
    sig = inspect.signature(UniverseSnapshotEngine.apply_snapshot)
    params = list(sig.parameters)
    assert params == ["self", "entries", "as_of_date", "db_role", "source", "run_id"]
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["source"].default == "manual"
    assert sig.parameters["run_id"].default is None
    # No second public method beyond apply_snapshot.
    public = [
        n
        for n in vars(UniverseSnapshotEngine)
        if not n.startswith("_") and callable(getattr(UniverseSnapshotEngine, n))
    ]
    assert public == ["apply_snapshot"]


# --------------------------------------------------------------------------- #
# 2. Fresh insert
# --------------------------------------------------------------------------- #
def test_fresh_insert(tmp_db_paths: dict[str, Path]) -> None:
    entries = [
        _info("AAPL", company_name="Apple", exchange="NASDAQ", sector="Technology"),
        _info("MSFT", company_name="Microsoft", exchange="NASDAQ", sector="Technology"),
        _info("XOM", company_name="Exxon", exchange="NYSE", sector="Energy"),
    ]
    result = UniverseSnapshotEngine().apply_snapshot(entries, date(2024, 3, 10))

    assert result.status == service_result.STATUS_SUCCESS
    assert result.is_ok() and not result.errors
    assert result.metadata["tickers_inserted"] == 3
    assert result.metadata["tickers_updated"] == 0
    assert result.metadata["snapshot_rows"] == 3
    assert result.rows_processed == 3

    master = _master_rows()
    assert set(master) == {"AAPL", "MSFT", "XOM"}
    for row in master.values():
        assert row[M_ACTIVE] is True
        assert row[M_DELISTED] is False
        assert row[M_FIRST_SEEN] == date(2024, 3, 1)
        assert row[M_LAST_SEEN] == date(2024, 3, 1)

    snaps = _snapshot_rows()
    assert len(snaps) == 3
    assert {s[S_TICKER] for s in snaps} == {"AAPL", "MSFT", "XOM"}


# --------------------------------------------------------------------------- #
# 3. Re-run idempotency
# --------------------------------------------------------------------------- #
def test_rerun_idempotency(tmp_db_paths: dict[str, Path]) -> None:
    entries = [_info("AAPL"), _info("MSFT")]
    engine = UniverseSnapshotEngine()
    engine.apply_snapshot(entries, date(2024, 3, 10))
    second = engine.apply_snapshot(entries, date(2024, 3, 28))

    assert second.status == service_result.STATUS_SUCCESS
    snaps = _snapshot_rows()
    # No duplicate (snapshot_month, ticker) rows for March.
    assert len(snaps) == 2
    assert sorted(s[S_TICKER] for s in snaps) == ["AAPL", "MSFT"]
    # Master not duplicated either.
    assert set(_master_rows()) == {"AAPL", "MSFT"}


# --------------------------------------------------------------------------- #
# 4. Update path
# --------------------------------------------------------------------------- #
def test_update_path(tmp_db_paths: dict[str, Path]) -> None:
    engine = UniverseSnapshotEngine()
    engine.apply_snapshot([_info("AAPL", company_name="Apple")], date(2024, 1, 15))
    result = engine.apply_snapshot(
        [_info("AAPL", company_name="Apple Inc.", sector="Technology")],
        date(2024, 2, 15),
    )

    assert result.metadata["tickers_updated"] == 1
    assert result.metadata["tickers_inserted"] == 0

    row = _master_rows()["AAPL"]
    assert row[M_FIRST_SEEN] == date(2024, 1, 1)  # unchanged
    assert row[M_LAST_SEEN] == date(2024, 2, 1)  # advanced
    assert row[M_ACTIVE] is True
    assert row[M_NAME] == "Apple Inc."  # mutable metadata refreshed
    assert row[M_SECTOR] == "Technology"


# --------------------------------------------------------------------------- #
# 5. Absent-ticker lifecycle
# --------------------------------------------------------------------------- #
def test_absent_ticker_marked_inactive(tmp_db_paths: dict[str, Path]) -> None:
    engine = UniverseSnapshotEngine()
    engine.apply_snapshot([_info("AAPL"), _info("MSFT")], date(2024, 1, 15))
    result = engine.apply_snapshot([_info("AAPL")], date(2024, 2, 15))

    assert result.metadata["tickers_marked_inactive"] == 1
    msft = _master_rows()["MSFT"]
    assert msft[M_ACTIVE] is False
    assert msft[M_DELISTED] is False  # absence is NOT delisting
    assert msft[M_LAST_SEEN] == date(2024, 1, 1)  # unchanged
    # The still-present ticker stays active.
    assert _master_rows()["AAPL"][M_ACTIVE] is True


# --------------------------------------------------------------------------- #
# 6. snapshot_month normalization
# --------------------------------------------------------------------------- #
def test_snapshot_month_normalization(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot([_info("AAPL")], date(2024, 7, 23))
    assert result.metadata["snapshot_month"] == "2024-07-01"
    assert _snapshot_rows()[0][S_MONTH] == date(2024, 7, 1)


# --------------------------------------------------------------------------- #
# 7. yahoo_symbol identity
# --------------------------------------------------------------------------- #
def test_yahoo_symbol_identity(tmp_db_paths: dict[str, Path]) -> None:
    UniverseSnapshotEngine().apply_snapshot([_info("AAPL")], date(2024, 3, 1))
    row = _master_rows()["AAPL"]
    assert row[M_YHOO] == "AAPL" == row[M_TICKER]


# --------------------------------------------------------------------------- #
# 8. market_cap_bucket NULL
# --------------------------------------------------------------------------- #
def test_market_cap_bucket_null(tmp_db_paths: dict[str, Path]) -> None:
    UniverseSnapshotEngine().apply_snapshot(
        [_info("AAPL"), _info("MSFT")], date(2024, 3, 1)
    )
    for row in _snapshot_rows():
        assert row[S_BUCKET] is None


# --------------------------------------------------------------------------- #
# 9. source propagation
# --------------------------------------------------------------------------- #
def test_source_propagation(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot(
        [_info("AAPL")], date(2024, 3, 1), source="yahoo"
    )
    assert result.metadata["source"] == "yahoo"
    assert _snapshot_rows()[0][S_SOURCE] == "yahoo"


# --------------------------------------------------------------------------- #
# 10. Invalid input (bad type + duplicates)
# --------------------------------------------------------------------------- #
def test_invalid_and_duplicate_input(tmp_db_paths: dict[str, Path]) -> None:
    entries = [
        _info("AAPL"),
        "NOT_A_TICKERINFO",  # bad type -> skipped
        _info("AAPL", company_name="dup"),  # duplicate -> earlier dropped, last kept
        _info("MSFT"),
    ]
    result = UniverseSnapshotEngine().apply_snapshot(entries, date(2024, 3, 1))

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.warnings
    assert result.metadata["input_rows"] == 4
    assert result.metadata["valid_rows"] == 2  # AAPL, MSFT distinct
    assert result.metadata["skipped_rows"] == 2  # bad type + 1 duplicate
    # Last AAPL occurrence wins.
    assert _master_rows()["AAPL"][M_NAME] == "dup"
    assert len(_snapshot_rows()) == 2


def test_entirely_unusable_input(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot(
        [123, None, "x"], date(2024, 3, 1)
    )
    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.metadata["valid_rows"] == 0
    assert result.metadata["snapshot_rows"] == 0
    assert _snapshot_rows() == []


# --------------------------------------------------------------------------- #
# 11. Empty input
# --------------------------------------------------------------------------- #
def test_empty_input(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot([], date(2024, 3, 1))
    assert result.status == service_result.STATUS_SUCCESS
    assert not result.warnings
    assert result.metadata["snapshot_rows"] == 0
    assert result.rows_processed == 0
    assert _snapshot_rows() == []


def test_empty_input_clears_month(tmp_db_paths: dict[str, Path]) -> None:
    engine = UniverseSnapshotEngine()
    engine.apply_snapshot([_info("AAPL")], date(2024, 3, 1))
    engine.apply_snapshot([], date(2024, 3, 20))  # same month, now empty
    assert _snapshot_rows() == []  # delete-then-insert empties the month


# --------------------------------------------------------------------------- #
# 12. db_role guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_role", ["simulation", "prod_x", "", "PROD"])
def test_db_role_guard(tmp_db_paths: dict[str, Path], bad_role: str) -> None:
    result = UniverseSnapshotEngine().apply_snapshot(
        [_info("AAPL")], date(2024, 3, 1), db_role=bad_role
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    assert not result.is_ok()
    # No writes to prod or debug.
    assert _master_rows("prod") == {}
    assert _snapshot_rows("prod") == []
    assert _master_rows("debug") == {}


def test_debug_role_allowed(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot(
        [_info("AAPL")], date(2024, 3, 1), db_role="debug"
    )
    assert result.status == service_result.STATUS_SUCCESS
    assert set(_master_rows("debug")) == {"AAPL"}
    # prod untouched.
    assert _master_rows("prod") == {}


# --------------------------------------------------------------------------- #
# 13. Transaction rollback
# --------------------------------------------------------------------------- #
class _FailingConn:
    """Wrap a real connection and raise on the first snapshot INSERT.

    Lets us force a mid-transaction failure after BEGIN/DELETE/master-writes so
    the rollback behavior can be verified against a real DuckDB transaction.
    """

    def __init__(self, real: object) -> None:
        self._real = real

    def execute(self, sql: str, params: list | None = None):  # type: ignore[no-untyped-def]
        if sql.startswith("INSERT INTO ticker_universe_snapshot"):
            raise RuntimeError("forced failure")
        if params is None:
            return self._real.execute(sql)  # type: ignore[attr-defined]
        return self._real.execute(sql, params)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._real.close()  # type: ignore[attr-defined]


class _FailingManager:
    def connect(self, db_role: str, read_only: bool = False):  # type: ignore[no-untyped-def]
        return _FailingConn(dbm.connect(db_role, read_only=read_only))


def test_transaction_rollback_leaves_no_partial_rows(
    tmp_db_paths: dict[str, Path],
) -> None:
    # Seed one existing ticker so the failing run also attempts a master write.
    UniverseSnapshotEngine().apply_snapshot([_info("AAPL")], date(2024, 1, 1))

    result = UniverseSnapshotEngine(db_manager=_FailingManager()).apply_snapshot(
        [_info("AAPL"), _info("MSFT")], date(2024, 2, 1)
    )

    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    # February snapshot rows must not exist (rolled back); the new MSFT master
    # row must not have been committed; AAPL's first_seen stays at January.
    feb = [s for s in _snapshot_rows() if s[S_MONTH] == date(2024, 2, 1)]
    assert feb == []
    assert "MSFT" not in _master_rows()
    assert _master_rows()["AAPL"][M_FIRST_SEEN] == date(2024, 1, 1)


# --------------------------------------------------------------------------- #
# 14. No sector_etf_map write
# --------------------------------------------------------------------------- #
def test_sector_etf_map_untouched(tmp_db_paths: dict[str, Path]) -> None:
    before = _sector_etf_rows()
    UniverseSnapshotEngine().apply_snapshot(
        [_info("AAPL", sector="Technology")], date(2024, 3, 1)
    )
    assert _sector_etf_rows() == before == []


# --------------------------------------------------------------------------- #
# 15. DB isolation / static scan
# --------------------------------------------------------------------------- #
def _module_raw_source() -> str:
    return Path(us.__file__).read_text(encoding="utf-8")


def _module_code_only() -> str:
    """Executable source with comments and string literals removed.

    Mirrors the literal-vs-prose separation in ``tests/test_schema_manager.py``
    and ``tests/test_provider_interface.py`` so that honest documentation (the
    module docstring describes what it must not do) cannot trip the scan, while
    a real import or call still does.
    """
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


def test_no_direct_duckdb_import_or_connect() -> None:
    import re

    code = _module_code_only()
    # Importing ``duckdb_manager`` is required and allowed; importing the bare
    # ``duckdb`` package is not. Word boundaries distinguish the two.
    assert not re.search(r"\bimport\s+duckdb\b", code)
    assert not re.search(r"\bfrom\s+duckdb\b", code)
    assert "duckdb.connect(" not in _module_raw_source()
    # All DB access is via the manager; the module holds no bare duckdb handle.
    assert not hasattr(us, "duckdb")


def test_no_yfinance_import() -> None:
    code = _module_code_only()
    assert "yfinance" not in code
    assert not hasattr(us, "yfinance")


def test_no_attach_or_ddl_in_source() -> None:
    code = _module_code_only().upper()
    # No module-level SQL string constant performs an ATTACH or any DDL.
    sql_literals = " ".join(
        v
        for k, v in vars(us).items()
        if isinstance(v, str) and not k.startswith("__")
    ).upper()
    for forbidden in ("ATTACH", "ALTER TABLE", "CREATE TABLE", "CREATE TYPE"):
        # Checked in the executed SQL literals (ground truth) and in executable
        # code; the module docstring may legitimately *mention* these as things
        # Module 06 must not do, so prose/strings are excluded from the code scan.
        assert forbidden not in sql_literals, forbidden
        assert forbidden not in code, forbidden


def test_no_print_in_source() -> None:
    assert "print(" not in _module_code_only()


# --------------------------------------------------------------------------- #
# 16. ServiceResult contract
# --------------------------------------------------------------------------- #
def test_service_result_contract(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot([_info("AAPL")], date(2024, 3, 1))
    assert isinstance(result, ServiceResult)
    assert result.has_valid_status()
    assert result.run_id
    assert set(result.metadata.keys()) == set(REQUIRED_METADATA_KEYS)


def test_metadata_keys_exact_on_failure(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot(
        [_info("AAPL")], date(2024, 3, 1), db_role="simulation"
    )
    # Exact key set even on the guard-failure path.
    assert set(result.metadata.keys()) == set(REQUIRED_METADATA_KEYS)


def test_run_id_is_propagated(tmp_db_paths: dict[str, Path]) -> None:
    result = UniverseSnapshotEngine().apply_snapshot(
        [_info("AAPL")], date(2024, 3, 1), run_id="fixed-run-id"
    )
    assert result.run_id == "fixed-run-id"
