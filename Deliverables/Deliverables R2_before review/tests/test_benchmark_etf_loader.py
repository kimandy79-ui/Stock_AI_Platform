"""Tests for Module 07 — Benchmark / Sector ETF Loader.

All tests run fully offline (no network, no live provider) and never touch the
real prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects
every DuckDB settings path into pytest ``tmp_path`` and applies the real
Module 03 schema there (mirroring ``tests/test_universe_snapshot.py``). Price
bars are supplied by an in-test :class:`MarketDataProvider` fake; the provider
interface is never bypassed and no vendor library is imported.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.providers.provider_interface import (
    MarketDataProvider,
    PriceBar,
    PriceHistoryRequest,
    ProviderCapabilities,
    ProviderErrorDetail,
)
from app.services.benchmarks import benchmark_etf_loader as bel
from app.services.benchmarks.benchmark_etf_loader import BenchmarkEtfLoader
from app.utils import service_result
from app.utils.service_result import ServiceResult

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "db_role",
        "start_date",
        "end_date",
        "symbols_requested",
        "symbols_loaded",
        "symbols_skipped",
        "price_rows_written",
        "ticker_master_upserted",
        "sector_etf_map_seeded",
    }
)

PROVIDER_NAME = "fake"
START = date(2024, 1, 2)
END = date(2024, 1, 3)
N_SYMBOLS = len(constants.REQUIRED_BENCHMARK_SYMBOLS)
N_SECTORS = len(constants.SECTOR_ETF_MAP)


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
# In-test provider fake (honors the Module 04 contract; no network).
# --------------------------------------------------------------------------- #
def _bar(ticker: str, d: date, **overrides: object) -> PriceBar:
    base: dict[str, object] = dict(
        open_raw=10.0,
        high_raw=11.0,
        low_raw=9.0,
        close_raw=10.5,
        volume_raw=1000,
        open_adj=10.0,
        high_adj=11.0,
        low_adj=9.0,
        close_adj=10.5,
        dividend_amount=None,
        split_ratio=None,
        source_provider=PROVIDER_NAME,
    )
    base.update(overrides)
    return PriceBar(ticker=ticker, date=d, **base)  # type: ignore[arg-type]


class _FakeProvider(MarketDataProvider):
    """Deterministic provider fake.

    ``responses`` overrides the bars returned for a ticker. ``fail`` forces a
    ``failed`` ServiceResult; ``raise_for`` forces an exception; ``empty`` forces
    a success with zero bars. Any ticker not configured gets two default bars.
    """

    def __init__(
        self,
        responses: dict[str, list[PriceBar]] | None = None,
        fail: set[str] | None = None,
        empty: set[str] | None = None,
        raise_for: set[str] | None = None,
        warnings_for: dict[str, list[str]] | None = None,
        missing_bars: set[str] | None = None,
        fail_all: bool = False,
        empty_all: bool = False,
    ) -> None:
        self._responses = responses or {}
        self._fail = fail or set()
        self._empty = empty or set()
        self._raise = raise_for or set()
        self._warnings_for = warnings_for or {}
        self._missing_bars = missing_bars or set()
        self._fail_all = fail_all
        self._empty_all = empty_all
        self.requested: list[tuple[str, str]] = []

    def get_capabilities(self) -> ServiceResult:
        caps = ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            supports_daily_prices=True,
            supports_ticker_listing=False,
            supports_earnings=False,
            supports_adjusted_prices=True,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id="fake",
            rows_processed=1,
            metadata={"capabilities": caps, "provider_name": PROVIDER_NAME},
        )

    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult:
        self.requested.append((request.ticker, request.symbol_type))
        if request.ticker in self._raise:
            raise RuntimeError(f"boom for {request.ticker}")
        if self._fail_all or request.ticker in self._fail:
            return ServiceResult(
                status=service_result.STATUS_FAILED,
                run_id="fake",
                rows_processed=0,
                errors=[f"unsupported {request.ticker}"],
                metadata={
                    "bars": [],
                    "provider_name": PROVIDER_NAME,
                    "error_detail": ProviderErrorDetail(
                        kind="unsupported_symbol",
                        message="nope",
                        symbol=request.ticker,
                    ),
                },
            )
        if self._empty_all or request.ticker in self._empty:
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id="fake",
                rows_processed=0,
                metadata={"bars": [], "provider_name": PROVIDER_NAME},
            )
        if request.ticker in self._missing_bars:
            # Deliberate provider-contract violation: success with no 'bars' key.
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id="fake",
                rows_processed=0,
                metadata={"provider_name": PROVIDER_NAME},
            )
        bars = self._responses.get(
            request.ticker,
            [_bar(request.ticker, START), _bar(request.ticker, END)],
        )
        if request.ticker in self._warnings_for:
            return ServiceResult(
                status=service_result.STATUS_SUCCESS_WITH_WARNINGS,
                run_id="fake",
                rows_processed=len(bars),
                warnings=list(self._warnings_for[request.ticker]),
                metadata={"bars": bars, "provider_name": PROVIDER_NAME},
            )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id="fake",
            rows_processed=len(bars),
            metadata={"bars": bars, "provider_name": PROVIDER_NAME},
        )

    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id="fake",
            rows_processed=0,
            metadata={"symbols": [], "provider_name": PROVIDER_NAME},
        )

    def get_earnings(self, ticker: str) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id="fake",
            rows_processed=0,
            metadata={"events": [], "provider_name": PROVIDER_NAME},
        )


# --- DB read helpers (via the approved manager, read-only) ------------------ #
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


# Price row tuple indexes (match the SELECT in _price_rows).
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


def _master_rows(role: str = "prod") -> dict[str, tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT ticker, yahoo_symbol, company_name, exchange, sector, "
            "industry, security_type, symbol_type, active_flag, delisted_flag, "
            "first_seen, last_seen, last_updated FROM ticker_master"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r for r in rows}


M_TICKER, M_YHOO, M_NAME, M_EXCH, M_SECTOR, M_IND, M_SECTYPE, M_SYMTYPE = range(8)
M_ACTIVE, M_DELISTED, M_FIRST_SEEN, M_LAST_SEEN, M_LAST_UPDATED = 8, 9, 10, 11, 12


def _sector_etf_rows(role: str = "prod") -> dict[str, tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT sector, etf_ticker, active_flag, created_at FROM sector_etf_map"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r for r in rows}


# --------------------------------------------------------------------------- #
# 1. Import smoke / exact signature
# --------------------------------------------------------------------------- #
def test_import_and_signature() -> None:
    assert hasattr(bel, "BenchmarkEtfLoader")
    sig = inspect.signature(BenchmarkEtfLoader.load)
    params = list(sig.parameters)
    assert params == [
        "self",
        "provider",
        "start_date",
        "end_date",
        "db_role",
        "run_id",
    ]
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["run_id"].default is None
    public = [
        n
        for n in vars(BenchmarkEtfLoader)
        if not n.startswith("_") and callable(getattr(BenchmarkEtfLoader, n))
    ]
    assert public == ["load"]


# --------------------------------------------------------------------------- #
# 2. Fresh load
# --------------------------------------------------------------------------- #
def test_fresh_load(tmp_db_paths: dict[str, Path]) -> None:
    result = BenchmarkEtfLoader().load(_FakeProvider(), START, END)

    assert result.status == service_result.STATUS_SUCCESS
    assert result.is_ok() and not result.errors and not result.warnings
    assert result.metadata["symbols_requested"] == N_SYMBOLS
    assert result.metadata["symbols_loaded"] == N_SYMBOLS
    assert result.metadata["symbols_skipped"] == 0
    assert result.metadata["price_rows_written"] == N_SYMBOLS * 2
    assert result.metadata["ticker_master_upserted"] == N_SYMBOLS
    assert result.metadata["sector_etf_map_seeded"] == N_SECTORS
    assert result.rows_processed == N_SYMBOLS * 2

    prices = _price_rows()
    assert len(prices) == N_SYMBOLS * 2
    assert set(constants.REQUIRED_BENCHMARK_SYMBOLS) == {k[0] for k in prices}
    assert set(_master_rows()) == set(constants.REQUIRED_BENCHMARK_SYMBOLS)


# --------------------------------------------------------------------------- #
# 3. Idempotency
# --------------------------------------------------------------------------- #
def test_idempotency(tmp_db_paths: dict[str, Path]) -> None:
    loader = BenchmarkEtfLoader()
    loader.load(_FakeProvider(), START, END)
    first = _price_rows()[("SPY", START)]
    assert first[P_UPDATED] is None  # fresh insert leaves updated_at NULL

    second = loader.load(_FakeProvider(), START, END)
    assert second.status == service_result.STATUS_SUCCESS
    assert second.metadata["sector_etf_map_seeded"] == 0  # already seeded
    assert second.metadata["ticker_master_upserted"] == N_SYMBOLS

    prices = _price_rows()
    assert len(prices) == N_SYMBOLS * 2  # no duplicate (ticker, date)
    row = prices[("SPY", START)]
    assert row[P_CREATED] == first[P_CREATED]  # created_at preserved
    assert row[P_UPDATED] is not None  # updated_at set on conflict


# --------------------------------------------------------------------------- #
# 4. ^VIX handling
# --------------------------------------------------------------------------- #
def test_vix_handling(tmp_db_paths: dict[str, Path]) -> None:
    # Provider returns distinct close_raw / close_adj and a non-null volume to
    # prove the loader overrides them per the locked rule.
    vix_bar = _bar(
        "^VIX", START, close_raw=999.0, close_adj=15.0, volume_raw=12345
    )
    provider = _FakeProvider(responses={"^VIX": [vix_bar]})
    BenchmarkEtfLoader().load(provider, START, END)

    row = _price_rows()[("^VIX", START)]
    assert row[P_CLOSE_RAW] == 15.0  # close_raw forced to close_adj
    assert row[P_CLOSE_ADJ] == 15.0
    assert row[P_VOLUME_RAW] is None  # volume_raw NULL
    assert row[P_VOLUME_ADJ] is None
    assert row[P_OPEN_RAW] == 10.0  # provider OHL preserved
    assert _master_rows()["^VIX"][M_SYMTYPE] == constants.SYMBOL_TYPE_INDEX


# --------------------------------------------------------------------------- #
# 5. Symbol-type assignment
# --------------------------------------------------------------------------- #
def test_symbol_type_assignment(tmp_db_paths: dict[str, Path]) -> None:
    BenchmarkEtfLoader().load(_FakeProvider(), START, END)
    master = _master_rows()
    assert master["SPY"][M_SYMTYPE] == constants.SYMBOL_TYPE_BENCHMARK
    assert master["QQQ"][M_SYMTYPE] == constants.SYMBOL_TYPE_BENCHMARK
    assert master["^VIX"][M_SYMTYPE] == constants.SYMBOL_TYPE_INDEX
    for etf in constants.SECTOR_ETFS:
        assert master[etf][M_SYMTYPE] == constants.SYMBOL_TYPE_ETF
    # ticker_master identity rules.
    for ticker, row in master.items():
        assert row[M_YHOO] == ticker
        assert row[M_ACTIVE] is True
        assert row[M_DELISTED] is False
        assert row[M_LAST_UPDATED] is not None


# --------------------------------------------------------------------------- #
# 6. Per-symbol failure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["fail", "empty", "raise"])
def test_per_symbol_failure_skips_one(
    tmp_db_paths: dict[str, Path], mode: str
) -> None:
    kw = {
        "fail": {"QQQ"} if mode == "fail" else None,
        "empty": {"QQQ"} if mode == "empty" else None,
        "raise_for": {"QQQ"} if mode == "raise" else None,
    }
    provider = _FakeProvider(**kw)  # type: ignore[arg-type]
    result = BenchmarkEtfLoader().load(provider, START, END)

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.warnings
    assert result.metadata["symbols_loaded"] == N_SYMBOLS - 1
    assert result.metadata["symbols_skipped"] == 1
    prices = _price_rows()
    assert ("QQQ", START) not in prices  # skipped symbol not written
    assert "QQQ" not in _master_rows()  # nor upserted into master
    # Other symbols still loaded + sector map seeded.
    assert ("SPY", START) in prices
    assert result.metadata["sector_etf_map_seeded"] == N_SECTORS


# --------------------------------------------------------------------------- #
# 7. All-symbol failure / empty data
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kw", [{"fail_all": True}, {"empty_all": True}])
def test_all_symbols_fail_or_empty(
    tmp_db_paths: dict[str, Path], kw: dict[str, bool]
) -> None:
    result = BenchmarkEtfLoader().load(_FakeProvider(**kw), START, END)

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.metadata["symbols_loaded"] == 0
    assert result.metadata["symbols_skipped"] == N_SYMBOLS
    assert result.metadata["price_rows_written"] == 0
    assert result.rows_processed == 0
    assert _price_rows() == {}
    assert _master_rows() == {}
    # sector_etf_map seeding is constant-driven and still happens.
    assert result.metadata["sector_etf_map_seeded"] == N_SECTORS
    assert set(_sector_etf_rows()) == set(constants.SECTOR_ETF_MAP)


# --------------------------------------------------------------------------- #
# 8. db_role guard (incl. simulation)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_role", ["simulation", "prod_x", "", "PROD"])
def test_db_role_guard(tmp_db_paths: dict[str, Path], bad_role: str) -> None:
    result = BenchmarkEtfLoader().load(
        _FakeProvider(), START, END, db_role=bad_role
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.errors and not result.is_ok()
    # No writes to prod or debug (including no sector_etf_map seeding).
    assert _price_rows("prod") == {}
    assert _master_rows("prod") == {}
    assert _sector_etf_rows("prod") == {}
    assert _price_rows("debug") == {}


def test_debug_role_allowed(tmp_db_paths: dict[str, Path]) -> None:
    result = BenchmarkEtfLoader().load(
        _FakeProvider(), START, END, db_role="debug"
    )
    assert result.status == service_result.STATUS_SUCCESS
    assert set(_master_rows("debug")) == set(constants.REQUIRED_BENCHMARK_SYMBOLS)
    assert _price_rows("prod") == {}  # prod untouched


# --------------------------------------------------------------------------- #
# 9. sector_etf_map content + idempotency + no-update
# --------------------------------------------------------------------------- #
def test_sector_etf_map_content_and_idempotency(
    tmp_db_paths: dict[str, Path],
) -> None:
    loader = BenchmarkEtfLoader()
    loader.load(_FakeProvider(), START, END)
    rows = _sector_etf_rows()
    assert {s: r[1] for s, r in rows.items()} == dict(constants.SECTOR_ETF_MAP)
    for r in rows.values():
        assert r[2] is True  # active_flag
        assert r[3] is not None  # created_at

    second = loader.load(_FakeProvider(), START, END)
    assert second.metadata["sector_etf_map_seeded"] == 0
    assert _sector_etf_rows() == rows  # unchanged on re-run


def test_sector_etf_map_does_not_update_existing(
    tmp_db_paths: dict[str, Path],
) -> None:
    # Pre-seed one sector with a deliberately different etf; Module 07 must not
    # overwrite it (insert-or-ignore).
    conn = dbm.connect("prod")
    try:
        conn.execute(
            "INSERT INTO sector_etf_map (sector, etf_ticker, active_flag, created_at) "
            "VALUES ('Technology', 'WRONG', FALSE, CAST(now() AS TIMESTAMP))"
        )
    finally:
        conn.close()

    result = BenchmarkEtfLoader().load(_FakeProvider(), START, END)
    rows = _sector_etf_rows()
    assert rows["Technology"][1] == "WRONG"  # preserved, not overwritten
    assert rows["Technology"][2] is False  # active_flag preserved
    assert result.metadata["sector_etf_map_seeded"] == N_SECTORS - 1


# --------------------------------------------------------------------------- #
# 10. ticker_master non-clobbering
# --------------------------------------------------------------------------- #
def test_ticker_master_non_clobbering(tmp_db_paths: dict[str, Path]) -> None:
    # Pre-insert SPY as a Module-06-style row with descriptive + lifecycle data.
    conn = dbm.connect("prod")
    try:
        conn.execute(
            "INSERT INTO ticker_master "
            "(ticker, yahoo_symbol, company_name, exchange, sector, industry, "
            " security_type, symbol_type, active_flag, delisted_flag, "
            " first_seen, last_seen, last_updated) "
            "VALUES ('SPY', 'SPY', 'SPDR S&P 500', 'NYSE', 'Index', 'Index Fund', "
            " 'ETF', 'stock', FALSE, TRUE, DATE '2020-01-01', DATE '2020-02-01', "
            " CAST(now() AS TIMESTAMP))"
        )
    finally:
        conn.close()

    BenchmarkEtfLoader().load(_FakeProvider(), START, END)
    row = _master_rows()["SPY"]
    # Module-06-owned fields preserved.
    assert row[M_NAME] == "SPDR S&P 500"
    assert row[M_EXCH] == "NYSE"
    assert row[M_SECTOR] == "Index"
    assert row[M_IND] == "Index Fund"
    assert row[M_SECTYPE] == "ETF"
    assert row[M_DELISTED] is True
    assert row[M_FIRST_SEEN] == date(2020, 1, 1)
    assert row[M_LAST_SEEN] == date(2020, 2, 1)
    # Module-07-owned fields refreshed.
    assert row[M_SYMTYPE] == constants.SYMBOL_TYPE_BENCHMARK
    assert row[M_ACTIVE] is True
    assert row[M_YHOO] == "SPY"


# --------------------------------------------------------------------------- #
# 11. Transaction rollback
# --------------------------------------------------------------------------- #
class _FailingConn:
    """Wrap a real connection and raise on the first daily_prices INSERT."""

    def __init__(self, real: object) -> None:
        self._real = real

    def execute(self, sql: str, params: list | None = None):  # type: ignore[no-untyped-def]
        if sql.startswith("INSERT INTO daily_prices"):
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
    result = BenchmarkEtfLoader(db_manager=_FailingManager()).load(
        _FakeProvider(), START, END
    )

    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    # All writes in the single transaction are rolled back: no prices, no
    # master rows, and no sector_etf_map seeding survive.
    assert _price_rows() == {}
    assert _master_rows() == {}
    assert _sector_etf_rows() == {}


# --------------------------------------------------------------------------- #
# 12-14. Locked daily_prices defaults
# --------------------------------------------------------------------------- #
def test_locked_daily_price_defaults(tmp_db_paths: dict[str, Path]) -> None:
    BenchmarkEtfLoader().load(_FakeProvider(), START, END)
    row = _price_rows()[("XLK", START)]
    assert row[P_ADJ_FACTOR] is None  # Module 10 owns adjustment_factor
    assert row[P_VOLUME_ADJ] is None
    assert row[P_DQ] == "ok"  # Module 09 owns real validation
    assert row[P_MUT] is False
    assert row[P_SOURCE] == PROVIDER_NAME
    assert row[P_CREATED] is not None


def test_dividend_and_split_defaults(tmp_db_paths: dict[str, Path]) -> None:
    # Missing dividend/split -> 0 / 1; provided values pass through.
    provided = _bar("XLF", START, dividend_amount=0.5, split_ratio=2.0)
    missing = _bar("XLF", END)  # dividend_amount/split_ratio = None
    provider = _FakeProvider(responses={"XLF": [provided, missing]})
    BenchmarkEtfLoader().load(provider, START, END)

    prices = _price_rows()
    assert prices[("XLF", START)][P_DIVIDEND] == 0.5
    assert prices[("XLF", START)][P_SPLIT] == 2.0
    assert prices[("XLF", END)][P_DIVIDEND] == 0
    assert prices[("XLF", END)][P_SPLIT] == 1


# --------------------------------------------------------------------------- #
# 15. Static scan: forbidden imports / direct DB / DDL / print
# --------------------------------------------------------------------------- #
def _module_raw_source() -> str:
    return Path(bel.__file__).read_text(encoding="utf-8")


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


def test_no_direct_duckdb_import_or_connect() -> None:
    import re

    code = _module_code_only()
    assert not re.search(r"\bimport\s+duckdb\b", code)
    assert not re.search(r"\bfrom\s+duckdb\b", code)
    assert "duckdb.connect(" not in _module_raw_source()
    assert not hasattr(bel, "duckdb")


def test_no_vendor_or_provider_impl_import() -> None:
    code = _module_code_only()
    assert "yfinance" not in code
    assert "yahoo_provider" not in code  # uses the abstract interface only
    assert not hasattr(bel, "yfinance")


def test_no_attach_or_ddl_in_source() -> None:
    code = _module_code_only().upper()
    sql_literals = " ".join(
        v
        for k, v in vars(bel).items()
        if isinstance(v, str) and not k.startswith("__")
    ).upper()
    for forbidden in ("ATTACH", "ALTER TABLE", "CREATE TABLE", "CREATE TYPE"):
        assert forbidden not in sql_literals, forbidden
        assert forbidden not in code, forbidden


def test_no_snapshot_write_in_source() -> None:
    # Module 07 must not write ticker_universe_snapshot (Module 06 territory).
    sql_literals = " ".join(
        v
        for k, v in vars(bel).items()
        if isinstance(v, str) and not k.startswith("__")
    )
    assert "ticker_universe_snapshot" not in sql_literals


def test_no_print_in_source() -> None:
    assert "print(" not in _module_code_only()


# --------------------------------------------------------------------------- #
# 16. ServiceResult metadata keys (exact, every path)
# --------------------------------------------------------------------------- #
def test_metadata_keys_exact_success(tmp_db_paths: dict[str, Path]) -> None:
    result = BenchmarkEtfLoader().load(_FakeProvider(), START, END)
    assert isinstance(result, ServiceResult)
    assert result.has_valid_status() and result.run_id
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)
    assert result.metadata["start_date"] == START.isoformat()
    assert result.metadata["end_date"] == END.isoformat()


def test_metadata_keys_exact_on_guard_failure(
    tmp_db_paths: dict[str, Path],
) -> None:
    result = BenchmarkEtfLoader().load(
        _FakeProvider(), START, END, db_role="simulation"
    )
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


def test_metadata_keys_exact_on_write_failure(
    tmp_db_paths: dict[str, Path],
) -> None:
    result = BenchmarkEtfLoader(db_manager=_FailingManager()).load(
        _FakeProvider(), START, END
    )
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


def test_run_id_is_propagated(tmp_db_paths: dict[str, Path]) -> None:
    result = BenchmarkEtfLoader().load(
        _FakeProvider(), START, END, run_id="fixed-run-id"
    )
    assert result.run_id == "fixed-run-id"


def test_provider_requests_use_classified_symbol_type(
    tmp_db_paths: dict[str, Path],
) -> None:
    provider = _FakeProvider()
    BenchmarkEtfLoader().load(provider, START, END)
    seen = dict(provider.requested)
    assert seen["SPY"] == constants.SYMBOL_TYPE_BENCHMARK
    assert seen["QQQ"] == constants.SYMBOL_TYPE_BENCHMARK
    assert seen["^VIX"] == constants.SYMBOL_TYPE_INDEX
    assert seen["XLK"] == constants.SYMBOL_TYPE_ETF


# --------------------------------------------------------------------------- #
# 17. Provider success_with_warnings propagation
# --------------------------------------------------------------------------- #
def test_provider_warnings_are_propagated_with_ticker_context(
    tmp_db_paths: dict[str, Path],
) -> None:
    provider = _FakeProvider(
        warnings_for={"SPY": ["bar gap detected", "stale Adj Close"]}
    )
    result = BenchmarkEtfLoader().load(provider, START, END)

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    # SPY still loaded — provider returned valid bars alongside the warnings.
    assert result.metadata["symbols_loaded"] == N_SYMBOLS
    assert result.metadata["symbols_skipped"] == 0
    assert ("SPY", START) in _price_rows()
    # Both provider warnings surface with ticker context.
    joined = " || ".join(result.warnings)
    assert "SPY: bar gap detected" in joined
    assert "SPY: stale Adj Close" in joined


# --------------------------------------------------------------------------- #
# 18. Invalid date range fails before any provider call / DB write
# --------------------------------------------------------------------------- #
def test_invalid_date_range_fails_early(tmp_db_paths: dict[str, Path]) -> None:
    provider = _FakeProvider()
    # start_date > end_date: should fail before fetch or any DB activity.
    result = BenchmarkEtfLoader().load(provider, END, START)

    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    assert not result.is_ok()
    # No provider calls.
    assert provider.requested == []
    # No DB mutations: no price rows, no master rows, no sector seeding.
    assert _price_rows() == {}
    assert _master_rows() == {}
    assert _sector_etf_rows() == {}
    # Metadata key set still exact on this early-fail path.
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)
    assert result.metadata["symbols_loaded"] == 0
    assert result.metadata["price_rows_written"] == 0
    assert result.metadata["sector_etf_map_seeded"] == 0


# --------------------------------------------------------------------------- #
# 19. Missing 'bars' key in provider metadata is a contract issue, not zero bars
# --------------------------------------------------------------------------- #
def test_missing_bars_metadata_is_warned_and_skipped(
    tmp_db_paths: dict[str, Path],
) -> None:
    provider = _FakeProvider(missing_bars={"QQQ"})
    result = BenchmarkEtfLoader().load(provider, START, END)

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.metadata["symbols_loaded"] == N_SYMBOLS - 1
    assert result.metadata["symbols_skipped"] == 1
    # The specific contract-violation message must appear with ticker context.
    assert any(
        "QQQ" in w and "missing metadata['bars']" in w for w in result.warnings
    )
    # QQQ not loaded; other symbols still loaded.
    assert ("QQQ", START) not in _price_rows()
    assert "QQQ" not in _master_rows()
    assert ("SPY", START) in _price_rows()


# --------------------------------------------------------------------------- #
# 20. sector_etf_map seeding uses SQL-level INSERT ... ON CONFLICT DO NOTHING
# --------------------------------------------------------------------------- #
def test_sector_etf_map_seed_sql_uses_sql_level_insert_or_ignore() -> None:
    sql = bel._INSERT_SECTOR_ETF.upper()
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    # ``RETURNING`` is how the loader counts actual inserts; require it so the
    # SQL-level contract cannot regress to a Python-side pre-check.
    assert "RETURNING" in sql


def test_sector_etf_map_sql_conflict_preserves_existing_rows(
    tmp_db_paths: dict[str, Path],
) -> None:
    # Pre-seed one of the SECTOR_ETF_MAP sectors with a deliberately wrong
    # etf_ticker. The SQL-level ON CONFLICT DO NOTHING clause must leave the
    # existing row untouched and report it as not-seeded this run.
    conn = dbm.connect("prod")
    try:
        conn.execute(
            "INSERT INTO sector_etf_map (sector, etf_ticker, active_flag, created_at) "
            "VALUES ('Energy', 'WRONG', FALSE, CAST(now() AS TIMESTAMP))"
        )
    finally:
        conn.close()

    result = BenchmarkEtfLoader().load(_FakeProvider(), START, END)
    rows = _sector_etf_rows()
    assert rows["Energy"][1] == "WRONG"  # preserved by SQL-level no-op
    assert rows["Energy"][2] is False  # active_flag preserved
    assert result.metadata["sector_etf_map_seeded"] == N_SECTORS - 1
    # Every other sector seeded with the canonical mapping.
    for sector, etf in constants.SECTOR_ETF_MAP.items():
        if sector == "Energy":
            continue
        assert rows[sector][1] == etf
