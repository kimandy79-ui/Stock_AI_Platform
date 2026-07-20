"""Tests for Module 08 — Daily Price Ingestion.

All tests run fully offline (no network, no live provider) and never touch the
real prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects
every DuckDB settings path into pytest ``tmp_path`` and applies the real
Module 03 schema there (mirroring ``tests/test_benchmark_etf_loader.py``).
Price bars are supplied by an in-test :class:`MarketDataProvider` fake; the
provider interface is never bypassed and no vendor library is imported.

The active stock universe is seeded directly into ``ticker_master`` per test
(Module 08 reads it read-only and never writes it).
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
from app.services.ingestion import daily_price_ingestion as dpi
from app.services.ingestion.daily_price_ingestion import DailyPriceIngestionEngine
from app.utils import service_result
from app.utils.service_result import ServiceResult

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "db_role",
        "start_date",
        "end_date",
        "tickers_requested",
        "tickers_loaded",
        "tickers_skipped",
        "price_rows_written",
        "repair_queue_enqueued",
    }
)

PROVIDER_NAME = "fake"
START = date(2024, 1, 2)
END = date(2024, 1, 3)

# Default active-stock universe seeded into ticker_master for most tests.
ACTIVE_STOCKS = ["AAPL", "MSFT", "GOOG"]


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


def _seed_master(
    rows: list[tuple[str, str, bool]], role: str = "prod"
) -> None:
    """Seed ``ticker_master`` with ``(ticker, symbol_type, active_flag)`` rows."""
    conn = dbm.connect(role)
    try:
        for ticker, symbol_type, active in rows:
            conn.execute(
                "INSERT INTO ticker_master "
                "(ticker, yahoo_symbol, symbol_type, active_flag, delisted_flag, "
                " last_updated) "
                "VALUES (?, ?, ?, ?, FALSE, CAST(now() AS TIMESTAMP))",
                [ticker, ticker, symbol_type, active],
            )
    finally:
        conn.close()


def _seed_active_stocks(tickers: list[str], role: str = "prod") -> None:
    _seed_master(
        [(t, constants.SYMBOL_TYPE_STOCK, True) for t in tickers], role=role
    )


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
    a success with zero bars; ``missing_bars`` forces a success with no 'bars'
    key. Any ticker not configured gets two default bars.
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


def _repair_rows(role: str = "prod") -> list[tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT repair_id, ticker, repair_date, repair_reason, attempts, "
            "max_attempts, last_attempt, status, created_at, updated_at "
            "FROM data_repair_queue ORDER BY ticker"
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


def _master_rows(role: str = "prod") -> dict[str, tuple]:
    conn = dbm.connect(role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT ticker, symbol_type, active_flag, last_updated "
            "FROM ticker_master"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r for r in rows}


# --------------------------------------------------------------------------- #
# 1. Import smoke / exact signature
# --------------------------------------------------------------------------- #
def test_import_and_signature() -> None:
    assert hasattr(dpi, "DailyPriceIngestionEngine")
    sig = inspect.signature(DailyPriceIngestionEngine.ingest)
    params = list(sig.parameters)
    assert params == [
        "self",
        "provider",
        "start_date",
        "end_date",
        "db_role",
        "run_id",
        "tickers",   # optional scope for batched/backfill use; None = all active stocks
    ]
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["run_id"].default is None
    assert sig.parameters["tickers"].default is None  # None preserves existing full-universe behaviour
    public = [
        n
        for n in vars(DailyPriceIngestionEngine)
        if not n.startswith("_") and callable(getattr(DailyPriceIngestionEngine, n))
    ]
    assert public == ["ingest"]


def test_run_id_is_propagated(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    result = DailyPriceIngestionEngine().ingest(
        _FakeProvider(), START, END, run_id="fixed-run-id"
    )
    assert result.run_id == "fixed-run-id"


# --------------------------------------------------------------------------- #
# 2. Fresh ingest
# --------------------------------------------------------------------------- #
def test_fresh_ingest(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    result = DailyPriceIngestionEngine().ingest(_FakeProvider(), START, END)

    assert result.status == service_result.STATUS_SUCCESS
    assert result.is_ok() and not result.errors and not result.warnings
    assert result.metadata["tickers_requested"] == len(ACTIVE_STOCKS)
    assert result.metadata["tickers_loaded"] == len(ACTIVE_STOCKS)
    assert result.metadata["tickers_skipped"] == 0
    assert result.metadata["price_rows_written"] == len(ACTIVE_STOCKS) * 2
    assert result.metadata["repair_queue_enqueued"] == 0
    assert result.rows_processed == len(ACTIVE_STOCKS) * 2

    prices = _price_rows()
    assert len(prices) == len(ACTIVE_STOCKS) * 2
    assert {k[0] for k in prices} == set(ACTIVE_STOCKS)
    assert _repair_rows() == []


# --------------------------------------------------------------------------- #
# 3. Idempotency
# --------------------------------------------------------------------------- #
def test_idempotency(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    engine = DailyPriceIngestionEngine()
    engine.ingest(_FakeProvider(), START, END)
    first = _price_rows()[("AAPL", START)]
    assert first[P_UPDATED] is None  # fresh insert leaves updated_at NULL

    second = engine.ingest(_FakeProvider(), START, END)
    assert second.status == service_result.STATUS_SUCCESS
    assert second.metadata["price_rows_written"] == len(ACTIVE_STOCKS) * 2

    prices = _price_rows()
    assert len(prices) == len(ACTIVE_STOCKS) * 2  # no duplicate (ticker, date)
    row = prices[("AAPL", START)]
    assert row[P_CREATED] == first[P_CREATED]  # created_at preserved
    assert row[P_UPDATED] is not None  # updated_at set on conflict


# --------------------------------------------------------------------------- #
# 4. Ticker selection: only stock + active
# --------------------------------------------------------------------------- #
def test_ticker_selection_filters_type_and_active(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_master(
        [
            ("AAPL", constants.SYMBOL_TYPE_STOCK, True),
            ("MSFT", constants.SYMBOL_TYPE_STOCK, True),
            ("OLD", constants.SYMBOL_TYPE_STOCK, False),  # inactive
            ("SPY", constants.SYMBOL_TYPE_BENCHMARK, True),  # not stock
            ("XLK", constants.SYMBOL_TYPE_ETF, True),  # not stock
            ("^VIX", constants.SYMBOL_TYPE_INDEX, True),  # not stock
        ]
    )
    provider = _FakeProvider()
    result = DailyPriceIngestionEngine().ingest(provider, START, END)

    requested = {t for t, _ in provider.requested}
    assert requested == {"AAPL", "MSFT"}
    assert result.metadata["tickers_requested"] == 2
    assert {k[0] for k in _price_rows()} == {"AAPL", "MSFT"}
    # Every provider request used symbol_type="stock".
    assert all(st == constants.SYMBOL_TYPE_STOCK for _, st in provider.requested)


def test_no_active_stocks_is_clean_success(tmp_db_paths: dict[str, Path]) -> None:
    # Only non-stock / inactive rows present.
    _seed_master(
        [
            ("SPY", constants.SYMBOL_TYPE_BENCHMARK, True),
            ("OLD", constants.SYMBOL_TYPE_STOCK, False),
        ]
    )
    provider = _FakeProvider()
    result = DailyPriceIngestionEngine().ingest(provider, START, END)
    assert result.status == service_result.STATUS_SUCCESS
    assert provider.requested == []
    assert result.metadata["tickers_requested"] == 0
    assert result.metadata["price_rows_written"] == 0
    assert _price_rows() == {}
    assert _repair_rows() == []


# --------------------------------------------------------------------------- #
# 5. Per-ticker failures -> repair queue, skip, continue
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["fail", "empty", "raise", "missing_bars"])
def test_per_ticker_failure_enqueues_repair(
    tmp_db_paths: dict[str, Path], mode: str
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    kw = {
        "fail": {"MSFT"} if mode == "fail" else None,
        "empty": {"MSFT"} if mode == "empty" else None,
        "raise_for": {"MSFT"} if mode == "raise" else None,
        "missing_bars": {"MSFT"} if mode == "missing_bars" else None,
    }
    provider = _FakeProvider(**kw)  # type: ignore[arg-type]
    result = DailyPriceIngestionEngine().ingest(provider, START, END)

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.warnings
    assert result.metadata["tickers_loaded"] == len(ACTIVE_STOCKS) - 1
    assert result.metadata["tickers_skipped"] == 1
    assert result.metadata["repair_queue_enqueued"] == 1

    prices = _price_rows()
    assert ("MSFT", START) not in prices  # skipped ticker not written
    assert ("AAPL", START) in prices  # others still loaded

    repairs = _repair_rows()
    assert len(repairs) == 1
    r = repairs[0]
    assert r[R_TICKER] == "MSFT"
    assert r[R_DATE] == END  # repair_date = ingestion range end
    assert r[R_REASON] == "missing_price"
    assert r[R_ATTEMPTS] == 0
    assert r[R_MAX] == 3
    assert r[R_STATUS] == "pending"
    assert r[R_CREATED] is not None
    assert r[R_UPDATED] is None


# --------------------------------------------------------------------------- #
# 6. All-ticker failure / empty -> success_with_warnings, repairs still written
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kw", [{"fail_all": True}, {"empty_all": True}])
def test_all_tickers_fail_or_empty(
    tmp_db_paths: dict[str, Path], kw: dict[str, bool]
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    result = DailyPriceIngestionEngine().ingest(_FakeProvider(**kw), START, END)

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.metadata["tickers_loaded"] == 0
    assert result.metadata["tickers_skipped"] == len(ACTIVE_STOCKS)
    assert result.metadata["price_rows_written"] == 0
    assert result.metadata["repair_queue_enqueued"] == len(ACTIVE_STOCKS)
    assert result.rows_processed == 0
    assert _price_rows() == {}
    # Repair queue still written for every failed ticker.
    assert {r[R_TICKER] for r in _repair_rows()} == set(ACTIVE_STOCKS)


# --------------------------------------------------------------------------- #
# 7. Repair queue insert-or-ignore: no duplicate rows on re-run
# --------------------------------------------------------------------------- #
def test_repair_queue_insert_or_ignore_no_duplicates(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    engine = DailyPriceIngestionEngine()

    first = engine.ingest(_FakeProvider(fail={"MSFT"}), START, END)
    assert first.metadata["repair_queue_enqueued"] == 1
    assert len(_repair_rows()) == 1
    first_repair_id = _repair_rows()[0][R_ID]

    # Re-run with the same failing ticker / same end_date: must not duplicate.
    second = engine.ingest(_FakeProvider(fail={"MSFT"}), START, END)
    assert second.metadata["repair_queue_enqueued"] == 0  # ignored duplicate
    repairs = _repair_rows()
    assert len(repairs) == 1
    assert repairs[0][R_ID] == first_repair_id  # untouched existing row


def test_repair_queue_dedups_within_single_run(
    tmp_db_paths: dict[str, Path],
) -> None:
    # Pre-seed an existing repair for MSFT at END; in the same run MSFT fails
    # again -> still no duplicate, and enqueued count excludes the ignored row.
    _seed_active_stocks(["AAPL", "MSFT"])
    conn = dbm.connect("prod")
    try:
        conn.execute(
            "INSERT INTO data_repair_queue "
            "(repair_id, ticker, repair_date, repair_reason, attempts, "
            " max_attempts, status, created_at) "
            "VALUES ('pre', 'MSFT', ?, 'missing_price', 0, 3, 'pending', "
            " CAST(now() AS TIMESTAMP))",
            [END],
        )
    finally:
        conn.close()

    result = DailyPriceIngestionEngine().ingest(_FakeProvider(fail={"MSFT"}), START, END)
    assert result.metadata["repair_queue_enqueued"] == 0
    assert len(_repair_rows()) == 1  # only the pre-existing row


def test_repair_id_is_deterministic_for_logical_repair_key(
    tmp_db_paths: dict[str, Path],
) -> None:
    # repair_id must be a deterministic function of
    # (ticker, repair_date, repair_reason) so DB-level ON CONFLICT (repair_id)
    # DO NOTHING gives true insert-or-ignore semantics without a UNIQUE on the
    # triple — and so re-runs produce no duplicate row and an unchanged id.
    _seed_active_stocks(ACTIVE_STOCKS)
    engine = DailyPriceIngestionEngine()

    engine.ingest(_FakeProvider(fail={"MSFT"}), START, END)
    repairs = _repair_rows()
    assert len(repairs) == 1
    repair_id = repairs[0][R_ID]

    # The id matches the module's deterministic derivation for the logical key.
    expected = dpi._repair_id_for("MSFT", END, "missing_price")
    assert repair_id == expected

    # Re-run with the same logical failure: one row, same id, none enqueued.
    second = engine.ingest(_FakeProvider(fail={"MSFT"}), START, END)
    assert second.metadata["repair_queue_enqueued"] == 0
    repairs = _repair_rows()
    assert len(repairs) == 1
    assert repairs[0][R_ID] == repair_id  # id unchanged

    # Different logical keys derive different, stable ids.
    assert dpi._repair_id_for("MSFT", END, "missing_price") == repair_id
    assert dpi._repair_id_for("AAPL", END, "missing_price") != repair_id
    assert dpi._repair_id_for("MSFT", START, "missing_price") != repair_id


def test_repair_insert_sql_uses_conflict_repair_id_do_nothing() -> None:
    # Lock the DB-level dedup contract: insert targets the repair_id PRIMARY KEY
    # with DO NOTHING and RETURNING (one row per insert, zero per conflict).
    sql = dpi._INSERT_REPAIR.upper()
    assert "ON CONFLICT" in sql
    assert "REPAIR_ID" in sql
    assert "DO NOTHING" in sql
    assert "RETURNING" in sql


# --------------------------------------------------------------------------- #
# 8. db_role guard (incl. simulation) — no reads, no writes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_role", ["simulation", "prod_x", "", "PROD"])
def test_db_role_guard(tmp_db_paths: dict[str, Path], bad_role: str) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    provider = _FakeProvider()
    result = DailyPriceIngestionEngine().ingest(
        provider, START, END, db_role=bad_role
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.errors and not result.is_ok()
    assert provider.requested == []  # no provider calls
    assert _price_rows("prod") == {}
    assert _repair_rows("prod") == []
    assert _price_rows("debug") == {}
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


def test_debug_role_allowed(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS, role="debug")
    result = DailyPriceIngestionEngine().ingest(
        _FakeProvider(), START, END, db_role="debug"
    )
    assert result.status == service_result.STATUS_SUCCESS
    assert {k[0] for k in _price_rows("debug")} == set(ACTIVE_STOCKS)
    assert _price_rows("prod") == {}  # prod untouched


# --------------------------------------------------------------------------- #
# 9. Invalid date range fails before provider call / DB activity
# --------------------------------------------------------------------------- #
def test_invalid_date_range_fails_early(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    provider = _FakeProvider()
    result = DailyPriceIngestionEngine().ingest(provider, END, START)

    assert result.status == service_result.STATUS_FAILED
    assert result.errors and not result.is_ok()
    assert provider.requested == []  # no provider calls
    assert _price_rows() == {}
    assert _repair_rows() == []
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)
    assert result.metadata["tickers_requested"] == 0
    assert result.metadata["price_rows_written"] == 0
    assert result.metadata["repair_queue_enqueued"] == 0


# --------------------------------------------------------------------------- #
# 10. Provider success_with_warnings propagation (bars still loaded)
# --------------------------------------------------------------------------- #
def test_provider_warnings_propagated_with_ticker_context(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    provider = _FakeProvider(
        warnings_for={"AAPL": ["bar gap detected", "stale Adj Close"]}
    )
    result = DailyPriceIngestionEngine().ingest(provider, START, END)

    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert result.metadata["tickers_loaded"] == len(ACTIVE_STOCKS)
    assert result.metadata["tickers_skipped"] == 0
    assert result.metadata["repair_queue_enqueued"] == 0  # warnings != failure
    assert ("AAPL", START) in _price_rows()
    joined = " || ".join(result.warnings)
    assert "AAPL: bar gap detected" in joined
    assert "AAPL: stale Adj Close" in joined


# --------------------------------------------------------------------------- #
# 11. Locked daily_prices defaults
# --------------------------------------------------------------------------- #
def test_locked_daily_price_defaults(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    DailyPriceIngestionEngine().ingest(_FakeProvider(), START, END)
    row = _price_rows()[("AAPL", START)]
    assert row[P_ADJ_FACTOR] is None  # Module 10 owns adjustment_factor
    assert row[P_VOLUME_ADJ] is None
    assert row[P_DQ] == "ok"  # Module 09 owns real validation
    assert row[P_MUT] is False
    assert row[P_SOURCE] == PROVIDER_NAME
    assert row[P_CREATED] is not None
    # Stocks keep provider close_raw / volume_raw verbatim (no ^VIX rule).
    assert row[P_CLOSE_RAW] == 10.5
    assert row[P_VOLUME_RAW] == 1000


def test_dividend_and_split_defaults(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(["AAPL"])
    provided = _bar("AAPL", START, dividend_amount=0.5, split_ratio=2.0)
    missing = _bar("AAPL", END)  # dividend_amount/split_ratio = None
    provider = _FakeProvider(responses={"AAPL": [provided, missing]})
    DailyPriceIngestionEngine().ingest(provider, START, END)

    prices = _price_rows()
    assert prices[("AAPL", START)][P_DIVIDEND] == 0.5
    assert prices[("AAPL", START)][P_SPLIT] == 2.0
    assert prices[("AAPL", END)][P_DIVIDEND] == 0
    assert prices[("AAPL", END)][P_SPLIT] == 1


def test_real_yahoo_provider_split_zero_writes_default_one(
    tmp_db_paths: dict[str, Path],
) -> None:
    """Cross-module integration (split-ratio convention fix, 2026-07-18): a
    *real* ``YahooProvider`` (not the local ``_FakeProvider``), fed a frame
    with yfinance's ``Stock Splits: 0.0`` 'no split' sentinel, now correctly
    ends up writing ``split_ratio = 1.0`` to ``daily_prices`` -- proving the
    provider's ``0.0`` -> ``None`` translation and this module's existing
    ``None`` -> ``1`` default (``:566``) compose correctly end-to-end,
    instead of ``0.0`` being written verbatim as before the fix.
    """
    from tests.test_yahoo_provider import FakeYF, _price_frame
    from app.providers.yahoo_provider import YahooProvider

    _seed_active_stocks(["AAPL"])
    frame = _price_frame(
        rows=[
            {
                "Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 100.0,
                "Volume": 1_000_000, "Dividends": 0.0, "Stock Splits": 0.0,
                "Adj Close": 100.0,
            },
        ],
        dates=[START.isoformat()],
    )
    fake_yf = FakeYF()
    fake_yf.history_behavior["AAPL"] = frame
    real_provider = YahooProvider(yf_module=fake_yf)

    result = DailyPriceIngestionEngine().ingest(
        real_provider, START, START, tickers=["AAPL"]
    )
    assert result.status == service_result.STATUS_SUCCESS, result.errors

    prices = _price_rows()
    assert prices[("AAPL", START)][P_SPLIT] == 1.0


# --------------------------------------------------------------------------- #
# 12. ticker_master is never written by Module 08
# --------------------------------------------------------------------------- #
def test_ticker_master_not_mutated(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    before = _master_rows()
    DailyPriceIngestionEngine().ingest(_FakeProvider(), START, END)
    after = _master_rows()
    assert before == after  # read-only: identical rows, no new tickers


# --------------------------------------------------------------------------- #
# 13. Transaction rollback / no partial rows
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
    """Read-only connections pass through; write connections fail on price insert."""

    def connect(self, db_role: str, read_only: bool = False):  # type: ignore[no-untyped-def]
        real = dbm.connect(db_role, read_only=read_only)
        if read_only:
            return real
        return _FailingConn(real)


def test_transaction_rollback_leaves_no_partial_rows(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    # Force at least one repair too, to prove repairs also roll back.
    result = DailyPriceIngestionEngine(db_manager=_FailingManager()).ingest(
        _FakeProvider(fail={"GOOG"}), START, END
    )

    assert result.status == service_result.STATUS_FAILED
    assert result.errors
    assert _price_rows() == {}  # no partial prices
    assert _repair_rows() == []  # repair inserts rolled back too
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


# --------------------------------------------------------------------------- #
# 14. Static scan: forbidden imports / direct DB / DDL / print / forbidden tables
# --------------------------------------------------------------------------- #
def _module_raw_source() -> str:
    return Path(dpi.__file__).read_text(encoding="utf-8")


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
        for k, v in vars(dpi).items()
        if isinstance(v, str) and not k.startswith("__")
    )


def test_no_direct_duckdb_import_or_connect() -> None:
    import re

    code = _module_code_only()
    assert not re.search(r"\bimport\s+duckdb\b", code)
    assert not re.search(r"\bfrom\s+duckdb\b", code)
    assert "duckdb.connect(" not in _module_raw_source()
    assert not hasattr(dpi, "duckdb")


def test_no_vendor_or_provider_impl_import() -> None:
    code = _module_code_only()
    assert "yfinance" not in code
    assert "yahoo_provider" not in code  # uses the abstract interface only
    assert not hasattr(dpi, "yfinance")


def test_no_attach_or_ddl_in_source() -> None:
    code = _module_code_only().upper()
    sql = _sql_literals().upper()
    for forbidden in ("ATTACH", "ALTER TABLE", "CREATE TABLE", "CREATE TYPE"):
        assert forbidden not in sql, forbidden
        assert forbidden not in code, forbidden


def test_no_forbidden_table_writes_in_source() -> None:
    sql = _sql_literals()
    # Module 08 must not write these tables.
    assert "ticker_universe_snapshot" not in sql
    assert "sector_etf_map" not in sql
    # ticker_master is read-only: no INSERT/UPDATE/DELETE against it.
    upper = sql.upper()
    for verb in ("INSERT INTO TICKER_MASTER", "UPDATE TICKER_MASTER", "DELETE FROM TICKER_MASTER"):
        assert verb not in upper, verb


def test_no_print_in_source() -> None:
    assert "print(" not in _module_code_only()


# --------------------------------------------------------------------------- #
# 15. ServiceResult metadata keys (exact, every path)
# --------------------------------------------------------------------------- #
def test_metadata_keys_exact_success(tmp_db_paths: dict[str, Path]) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    result = DailyPriceIngestionEngine().ingest(_FakeProvider(), START, END)
    assert isinstance(result, ServiceResult)
    assert result.has_valid_status() and result.run_id
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)
    assert result.metadata["start_date"] == START.isoformat()
    assert result.metadata["end_date"] == END.isoformat()


def test_metadata_keys_exact_on_guard_failure(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    result = DailyPriceIngestionEngine().ingest(
        _FakeProvider(), START, END, db_role="simulation"
    )
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


def test_metadata_keys_exact_on_write_failure(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    result = DailyPriceIngestionEngine(db_manager=_FailingManager()).ingest(
        _FakeProvider(), START, END
    )
    assert result.status == service_result.STATUS_FAILED
    assert set(result.metadata) == set(REQUIRED_METADATA_KEYS)


def test_provider_requests_use_stock_symbol_type(
    tmp_db_paths: dict[str, Path],
) -> None:
    _seed_active_stocks(ACTIVE_STOCKS)
    provider = _FakeProvider()
    DailyPriceIngestionEngine().ingest(provider, START, END)
    assert provider.requested  # non-empty
    for _ticker, symbol_type in provider.requested:
        assert symbol_type == constants.SYMBOL_TYPE_STOCK
