"""Tests for Module 20's Phase 4 ``_step_fundamentals`` (fundamentals_refresh).

Fully offline: DB access through a small fake matching the ``DuckDBManager``
surface (``connect`` -> object with ``execute``/``close``); the fundamentals
provider is an injected fake, never a real ``EdgarFundamentalsProvider``.
Mirrors the structure and behavior contract of the pre-existing
``_step_earnings`` (same already-refreshed-today guard, same per-ticker
warning-not-failure semantics, same one-transaction batch upsert).
"""

from __future__ import annotations

from datetime import date

from app.providers.provider_interface import FundamentalSnapshot
from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
from app.utils import service_result
from app.utils.service_result import ServiceResult

RUN_DATE = date(2026, 6, 15)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=None):
        self._db.executed.append((sql, params))
        sql_upper = sql.upper()
        if "TICKER_FUNDAMENTALS" in sql_upper and "COUNT" in sql_upper:
            return _FakeCursor([(1 if self._db.already_refreshed else 0,)])
        if "TICKER_MASTER" in sql_upper and "SELECT" in sql_upper:
            return _FakeCursor([(t,) for t in self._db.tickers])
        return _FakeCursor([])

    def close(self):
        pass


class _FakeDb:
    def __init__(self, tickers=(), already_refreshed=False):
        self.tickers = tickers
        self.already_refreshed = already_refreshed
        self.executed: list[tuple] = []

    def connect(self, db_role, read_only=False):
        return _FakeConnection(self)


def _snapshot(ticker: str) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        ticker=ticker,
        as_of_date=RUN_DATE,
        eps_growth_trend=0.1,
        leverage_ratio=0.2,
        valuation_band="fair",
        piotroski_f_score=7,
        altman_z_score=2.5,
        insider_trade_flag=None,
        institutional_ownership_delta=None,
        source_provider="sec_edgar",
    )


class _FakeFundamentalsProvider:
    def __init__(self, snapshots=None, fail_tickers=(), raise_tickers=()):
        self._snapshots = snapshots or {}
        self._fail_tickers = set(fail_tickers)
        self._raise_tickers = set(raise_tickers)
        self.calls: list[str] = []

    def get_fundamentals(self, ticker: str, as_of_date: date) -> ServiceResult:
        self.calls.append(ticker)
        if ticker in self._raise_tickers:
            raise RuntimeError("simulated transport failure")
        if ticker in self._fail_tickers:
            return ServiceResult(
                status=service_result.STATUS_FAILED, run_id="p", errors=["boom"]
            )
        snapshot = self._snapshots.get(ticker) or _snapshot(ticker)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id="p",
            rows_processed=1,
            metadata={"fundamentals": snapshot},
        )


def _build(db, fundamentals_provider):
    return PipelineOrchestrator(
        db_manager=db,
        provider=object(),
        fundamentals_provider=fundamentals_provider,
    )


class TestStepFundamentals:
    def test_skips_when_already_refreshed_today(self) -> None:
        db = _FakeDb(tickers=["AAPL"], already_refreshed=True)
        provider = _FakeFundamentalsProvider()
        orch = _build(db, provider)
        result = orch._step_fundamentals(RUN_DATE, "prod", "r1", _NullLog())
        assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 0
        assert provider.calls == []

    def test_warns_when_no_active_tickers(self) -> None:
        db = _FakeDb(tickers=[])
        provider = _FakeFundamentalsProvider()
        orch = _build(db, provider)
        result = orch._step_fundamentals(RUN_DATE, "prod", "r1", _NullLog())
        assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 0
        assert provider.calls == []

    def test_happy_path_upserts_all_tickers(self) -> None:
        db = _FakeDb(tickers=["AAPL", "MSFT"])
        provider = _FakeFundamentalsProvider()
        orch = _build(db, provider)
        result = orch._step_fundamentals(RUN_DATE, "prod", "r1", _NullLog())
        assert result.status == service_result.STATUS_SUCCESS
        assert result.rows_processed == 2
        assert sorted(provider.calls) == ["AAPL", "MSFT"]
        upsert_calls = [e for e in db.executed if "INSERT INTO ticker_fundamentals" in e[0]]
        assert len(upsert_calls) == 2

    def test_per_ticker_failure_is_a_warning_not_a_hard_failure(self) -> None:
        db = _FakeDb(tickers=["AAPL", "BADTICKER"])
        provider = _FakeFundamentalsProvider(fail_tickers=["BADTICKER"])
        orch = _build(db, provider)
        result = orch._step_fundamentals(RUN_DATE, "prod", "r1", _NullLog())
        assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 1
        assert any("BADTICKER" in w for w in result.warnings)

    def test_provider_exception_is_a_warning_not_a_hard_failure(self) -> None:
        db = _FakeDb(tickers=["AAPL", "EXPLODES"])
        provider = _FakeFundamentalsProvider(raise_tickers=["EXPLODES"])
        orch = _build(db, provider)
        result = orch._step_fundamentals(RUN_DATE, "prod", "r1", _NullLog())
        assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 1
        assert any("EXPLODES" in w for w in result.warnings)

    def test_all_tickers_fail_returns_success_with_warnings_zero_rows(self) -> None:
        db = _FakeDb(tickers=["BADTICKER"])
        provider = _FakeFundamentalsProvider(fail_tickers=["BADTICKER"])
        orch = _build(db, provider)
        result = orch._step_fundamentals(RUN_DATE, "prod", "r1", _NullLog())
        assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 0

    def test_default_fundamentals_provider_is_edgar(self) -> None:
        from app.providers.edgar_provider import EdgarFundamentalsProvider

        db = _FakeDb(tickers=[])
        orch = PipelineOrchestrator(db_manager=db, provider=object())
        assert isinstance(orch._fundamentals_provider, EdgarFundamentalsProvider)

    def test_step_registered_in_step_names_and_recoverable(self) -> None:
        from app.services.pipeline.pipeline_orchestrator import (
            RECOVERABLE_STEPS,
            STEP_NAMES,
        )

        assert "fundamentals_refresh" in STEP_NAMES
        assert "fundamentals_refresh" in RECOVERABLE_STEPS
        earnings_idx = STEP_NAMES.index("earnings_calendar_refresh")
        fundamentals_idx = STEP_NAMES.index("fundamentals_refresh")
        price_idx = STEP_NAMES.index("price_ingestion")
        assert earnings_idx < fundamentals_idx < price_idx


class _NullLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass
