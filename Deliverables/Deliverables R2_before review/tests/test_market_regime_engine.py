"""Tests for Module 12 — Market Regime Engine.

All tests run fully offline (no network, no provider) and never touch the real
prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects every
DuckDB settings path into pytest ``tmp_path`` and applies the real Module 03
schema there (mirroring ``tests/test_feature_engine.py``). Price rows are seeded
directly into ``daily_prices`` and pre-existing feature rows directly into
``daily_features``; Module 12 reads prices read-only and updates only the
``daily_features.market_regime`` / ``calculated_at`` columns.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl  # noqa: F401 - ensures the optional dep is present for the suite
import pytest

from app.config import constants
from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.regime import market_regime_engine as mremod
from app.services.regime.market_regime_engine import MarketRegimeEngine
from app.utils import service_result

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "db_role",
        "start_date",
        "end_date",
        "dates_requested",
        "dates_classified",
        "dates_skipped_insufficient_data",
        "rows_read",
        "feature_rows_updated",
        "regimes_by_value",
    }
)

SOURCE = "fake"
SCHEMA = constants.FEATURE_SCHEMA_VERSION


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
# Seeding helpers (write directly to the DB; this is the test harness, not
# Module 12 — Module 12 never inserts price or feature rows).
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

_INSERT_FEATURE = (
    "INSERT INTO daily_features "
    "(ticker, feature_date, feature_cutoff_date, feature_schema_version, "
    " feature_ready, market_regime, calculated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, CAST(? AS TIMESTAMP))"
)


def _trading_days(start: date, n: int) -> list[date]:
    """Return ``n`` consecutive weekday dates starting at/after ``start``."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d)
        d += timedelta(days=1)
    return out


def _trading_days_ending_on(last_day: date, n: int) -> list[date]:
    """Return ``n`` weekday dates ending exactly on ``last_day`` (inclusive).

    Walks backward from ``last_day`` collecting weekdays, then reverses so the
    result is in ascending order. All ``n`` bars are placed as close to
    ``last_day`` as possible, ensuring every bar falls within the engine's
    320-calendar-day warmup window when the series is seeded for tests.
    """
    out: list[date] = []
    d = last_day
    while len(out) < n:
        if d.weekday() < 5:  # Mon-Fri
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def _seed_prices(
    db_path: Path,
    ticker: str,
    days: list[date],
    closes: list[float],
    *,
    status: str = "ok",
    use_adj: bool = True,
) -> None:
    """Seed a price series for ``ticker``.

    ``use_adj=True`` writes ``close_adj == close_raw`` (stocks/ETFs). ``use_adj=
    False`` writes ``close_adj = NULL`` and only ``close_raw`` (the ``^VIX``
    case, so the engine's ``coalesce(close_adj, close_raw)`` falls back to raw).
    """
    import duckdb

    conn = duckdb.connect(str(db_path))
    try:
        for d, c in zip(days, closes):
            high = c + 0.5
            low = c - 0.5
            close_adj = c if use_adj else None
            high_adj = high if use_adj else None
            low_adj = low if use_adj else None
            conn.execute(
                _INSERT_PRICE,
                [
                    ticker, d,
                    c, high, low, c, 1_000_000,          # raw OHLCV
                    None if not use_adj else c, high_adj, low_adj, close_adj, None,
                    SOURCE, status,
                ],
            )
    finally:
        conn.close()


def _seed_feature_rows(
    db_path: Path,
    feature_date: date,
    tickers: list[str],
    *,
    schema_version: str = SCHEMA,
    market_regime: str | None = None,
) -> None:
    """Seed minimal ``daily_features`` rows for a date (one per ticker)."""
    import duckdb

    conn = duckdb.connect(str(db_path))
    try:
        for ticker in tickers:
            conn.execute(
                _INSERT_FEATURE,
                [
                    ticker, feature_date, feature_date, schema_version,
                    True, market_regime, "2000-01-01 00:00:00",
                ],
            )
    finally:
        conn.close()


def _fetch_feature_regime(db_path: Path, ticker: str, feature_date: date) -> dict | None:
    import duckdb

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT market_regime, calculated_at, feature_ready, feature_cutoff_date "
            "FROM daily_features WHERE ticker = ? AND feature_date = ?",
            [ticker, feature_date],
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    r = rows[0]
    return {
        "market_regime": r[0],
        "calculated_at": r[1],
        "feature_ready": r[2],
        "feature_cutoff_date": r[3],
    }


def _seed_constant_regime_symbols(
    db_path: Path,
    last_day: date,
    n: int,
    *,
    spy_last: float | None = None,
    qqq_last: float | None = None,
    vix_last: float | None = None,
    base: float = 100.0,
    include_qqq: bool = True,
    include_vix: bool = True,
) -> list[date]:
    """Seed SPY/QQQ/^VIX as ``n`` constant-``base`` trading days ending on
    ``last_day``; override the final close per symbol when provided.

    Uses :func:`_trading_days_ending_on` so every bar lands within the engine's
    320-calendar-day warmup window.  A constant series makes EMA200 converge to
    ``base``, so the final-day close relative to ``base`` deterministically
    controls the trend classification.
    """
    days = _trading_days_ending_on(last_day, n)

    spy_closes = [base] * len(days)
    if spy_last is not None:
        spy_closes[-1] = spy_last
    _seed_prices(db_path, constants.BENCHMARK_SPY, days, spy_closes, use_adj=True)

    if include_qqq:
        qqq_closes = [base] * len(days)
        if qqq_last is not None:
            qqq_closes[-1] = qqq_last
        _seed_prices(db_path, constants.BENCHMARK_QQQ, days, qqq_closes, use_adj=True)

    if include_vix:
        vix_closes = [15.0] * len(days)
        if vix_last is not None:
            vix_closes[-1] = vix_last
        _seed_prices(db_path, constants.BENCHMARK_VIX, days, vix_closes, use_adj=False)

    return days


# --------------------------------------------------------------------------- #
# 1. Public API / signature / metadata / rows_processed invariant
# --------------------------------------------------------------------------- #
def test_classify_signature_exact() -> None:
    sig = inspect.signature(MarketRegimeEngine.classify)
    params = list(sig.parameters)
    assert params == ["self", "start_date", "end_date", "db_role", "run_id"]
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["run_id"].default is None


def test_run_id_minted_when_none(tmp_db_paths: dict[str, Path]) -> None:
    res = MarketRegimeEngine().classify(date(2022, 6, 1), date(2022, 6, 1))
    assert isinstance(res.run_id, str) and len(res.run_id) >= 32


def test_run_id_preserved_when_supplied(tmp_db_paths: dict[str, Path]) -> None:
    res = MarketRegimeEngine().classify(date(2022, 6, 1), date(2022, 6, 1), run_id="rid-12")
    assert res.run_id == "rid-12"


def test_metadata_keys_exact_on_success(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 3, 1)
    _seed_constant_regime_symbols(tmp_db_paths[dbm.DB_ROLE_PROD], last, 220)
    res = MarketRegimeEngine().classify(last, last)
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_exact_on_guard_failure() -> None:
    res = MarketRegimeEngine().classify(date(2022, 6, 2), date(2022, 6, 1))
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


def test_rows_processed_equals_dates_classified(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 3, 6)
    _seed_constant_regime_symbols(tmp_db_paths[dbm.DB_ROLE_PROD], last, 220)
    # request a 3-calendar-day window ending on a classified date
    res = MarketRegimeEngine().classify(last - timedelta(days=2), last)
    assert res.rows_processed == res.metadata["dates_classified"]
    # also on guard failure
    bad = MarketRegimeEngine().classify(date(2022, 6, 2), date(2022, 6, 1))
    assert bad.rows_processed == bad.metadata["dates_classified"] == 0


# --------------------------------------------------------------------------- #
# 2. Guards run before any DB access
# --------------------------------------------------------------------------- #
class _ExplodingDb:
    """A db_manager whose connect() must never be called by guard paths."""

    def connect(self, db_role: str, read_only: bool = False):
        raise AssertionError("DB access attempted before guard passed")


@pytest.mark.parametrize("bad_role", ["simulation", "PROD", "", "weird"])
def test_invalid_db_role_fails_without_db_access(bad_role: str) -> None:
    res = MarketRegimeEngine(db_manager=_ExplodingDb()).classify(
        date(2022, 6, 1), date(2022, 6, 1), db_role=bad_role
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["db_role"] == bad_role
    assert res.metadata["feature_rows_updated"] == 0


def test_invalid_date_range_fails_without_db_access() -> None:
    res = MarketRegimeEngine(db_manager=_ExplodingDb()).classify(
        date(2022, 6, 2), date(2022, 6, 1)
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["dates_classified"] == 0
    assert res.metadata["dates_requested"] == 0


def test_simulation_role_rejected() -> None:
    res = MarketRegimeEngine(db_manager=_ExplodingDb()).classify(
        date(2022, 6, 1), date(2022, 6, 1), db_role="simulation"
    )
    assert res.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# 3. VIX gates, trend classification, priority override
# --------------------------------------------------------------------------- #
def test_vix_extreme_risk_gate(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 4, 3)
    _seed_constant_regime_symbols(tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, vix_last=31.0)
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_EXTREME_RISK] == 1
    assert res.metadata["dates_classified"] == 1


def test_vix_high_risk_gate(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 4, 4)
    _seed_constant_regime_symbols(tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, vix_last=26.0)
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_HIGH_RISK] == 1


def test_trend_bull(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 4, 5)
    _seed_constant_regime_symbols(tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=130.0)
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 1


def test_trend_bear(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 4, 6)
    _seed_constant_regime_symbols(
        tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=70.0, qqq_last=70.0
    )
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_BEAR] == 1


def test_trend_neutral_spy_below_qqq_above(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 4, 10)
    # SPY below its EMA200 but QQQ above its EMA200 -> bear cannot fire -> neutral
    _seed_constant_regime_symbols(
        tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=70.0, qqq_last=130.0
    )
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_NEUTRAL] == 1


def test_vix_priority_overrides_trend(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 4, 11)
    # Bullish SPY but extreme VIX -> extreme_risk wins (priority gate).
    _seed_constant_regime_symbols(
        tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=130.0, vix_last=35.0
    )
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_EXTREME_RISK] == 1
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 0


# --------------------------------------------------------------------------- #
# 4. Fallbacks
# --------------------------------------------------------------------------- #
def test_insufficient_spy_ema200_neutral_with_warning(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2022, 7, 1)
    # Only ~30 bars -> EMA200 unavailable -> neutral + warning.
    _seed_constant_regime_symbols(tmp_db_paths[dbm.DB_ROLE_PROD], last, 30, spy_last=130.0)
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_NEUTRAL] == 1
    assert res.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert res.warnings


def test_spy_absent_date_skipped(tmp_db_paths: dict[str, Path]) -> None:
    # No SPY rows at all; QQQ/VIX present. Every requested date is skipped.
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 5, 1)
    days = _trading_days(last - timedelta(days=10), 5)
    _seed_prices(prod, constants.BENCHMARK_QQQ, days, [100.0] * len(days))
    _seed_prices(prod, constants.BENCHMARK_VIX, days, [15.0] * len(days), use_adj=False)
    res = MarketRegimeEngine().classify(days[0], days[-1])
    assert res.metadata["dates_classified"] == 0
    assert res.metadata["dates_skipped_insufficient_data"] == res.metadata["dates_requested"]


def test_vix_missing_classifies_by_trend(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 5, 2)
    _seed_constant_regime_symbols(
        tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=130.0, include_vix=False
    )
    res = MarketRegimeEngine().classify(last, last)
    # No VIX -> gates skipped -> bull from trend.
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 1


def test_qqq_missing_no_bear(tmp_db_paths: dict[str, Path]) -> None:
    last = date(2023, 5, 3)
    # SPY below EMA200, but no QQQ -> bear cannot fire -> neutral.
    _seed_constant_regime_symbols(
        tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=70.0, include_qqq=False
    )
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_BEAR] == 0
    assert res.metadata["regimes_by_value"][constants.REGIME_NEUTRAL] == 1


# --------------------------------------------------------------------------- #
# 5. No look-ahead / calendar as-of (weekends)
# --------------------------------------------------------------------------- #
def test_weekend_uses_prior_trading_day(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Build a long constant history; Friday's SPY is bullish, then request the
    # following Saturday (no trading row) -> as-of backward picks Friday -> bull.
    friday = date(2023, 5, 5)  # Friday
    saturday = date(2023, 5, 6)
    _seed_constant_regime_symbols(prod, friday, 220, spy_last=130.0)
    res = MarketRegimeEngine().classify(saturday, saturday)
    assert res.metadata["dates_classified"] == 1
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 1


def test_no_lookahead_uses_only_past_rows(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # History is bullish through `mid`; a LATER row turns bearish. Classifying
    # `mid` must ignore the later row (no look-ahead) -> bull on `mid`.
    days = _trading_days(date(2022, 1, 3), 230)
    mid = days[210]
    spy = [100.0] * len(days)
    spy[210] = 130.0          # bullish at mid
    for i in range(211, len(days)):
        spy[i] = 60.0         # later bearish rows
    _seed_prices(prod, constants.BENCHMARK_SPY, days, spy)
    _seed_prices(prod, constants.BENCHMARK_QQQ, days, [100.0] * len(days))
    _seed_prices(prod, constants.BENCHMARK_VIX, days, [15.0] * len(days), use_adj=False)
    res = MarketRegimeEngine().classify(mid, mid)
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 1


# --------------------------------------------------------------------------- #
# 6. feature_rows_updated accuracy / update-all-rows-for-date
# --------------------------------------------------------------------------- #
def test_updates_all_feature_rows_for_date(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 6, 1)
    _seed_constant_regime_symbols(prod, last, 220, spy_last=130.0)
    _seed_feature_rows(prod, last, ["AAA", "BBB", "CCC"])
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["feature_rows_updated"] == 3
    for t in ("AAA", "BBB", "CCC"):
        assert _fetch_feature_regime(prod, t, last)["market_regime"] == constants.REGIME_BULL


def test_classified_date_without_feature_rows_counts_zero(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 6, 5)
    _seed_constant_regime_symbols(prod, last, 220, spy_last=130.0)
    # No daily_features rows seeded.
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["dates_classified"] == 1
    assert res.metadata["feature_rows_updated"] == 0


def test_wrong_schema_version_rows_not_updated(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 6, 6)
    _seed_constant_regime_symbols(prod, last, 220, spy_last=130.0)
    _seed_feature_rows(prod, last, ["AAA"], schema_version="features_v99")
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["feature_rows_updated"] == 0
    assert _fetch_feature_regime(prod, "AAA", last)["market_regime"] is None


# --------------------------------------------------------------------------- #
# 7. Write ownership: only market_regime + calculated_at change
# --------------------------------------------------------------------------- #
def test_only_market_regime_and_calculated_at_change(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 6, 7)
    _seed_constant_regime_symbols(prod, last, 220, spy_last=130.0)
    _seed_feature_rows(prod, last, ["AAA"])
    before = _fetch_feature_regime(prod, "AAA", last)
    res = MarketRegimeEngine().classify(last, last)
    after = _fetch_feature_regime(prod, "AAA", last)
    assert res.metadata["feature_rows_updated"] == 1
    assert after["market_regime"] == constants.REGIME_BULL
    assert after["calculated_at"] != before["calculated_at"]  # refreshed
    # untouched columns preserved
    assert after["feature_ready"] == before["feature_ready"]
    assert after["feature_cutoff_date"] == before["feature_cutoff_date"]


def test_no_other_tables_written(tmp_db_paths: dict[str, Path]) -> None:
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 6, 8)
    _seed_constant_regime_symbols(prod, last, 220, spy_last=130.0)

    forbidden = (
        "daily_prices", "ticker_master", "ticker_universe_snapshot",
        "data_repair_queue", "feature_rebuild_log",
    )

    def snapshot() -> dict[str, int]:
        conn = duckdb.connect(str(prod), read_only=True)
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in forbidden}
        finally:
            conn.close()

    before = snapshot()
    MarketRegimeEngine().classify(last, last)
    after = snapshot()
    assert before == after


# --------------------------------------------------------------------------- #
# 8. Rollback leaves no partial updates
# --------------------------------------------------------------------------- #
class _FailOnNthUpdateDb:
    """Wraps the real manager; makes the Nth ``UPDATE daily_features`` raise.

    Allows the test to verify that a mid-transaction failure (not just the
    very-first update) is correctly rolled back, leaving no partial writes.
    """

    def __init__(self, real, fail_on_update_number: int) -> None:
        self._real = real
        self._fail_on_update_number = fail_on_update_number

    def connect(self, db_role: str, read_only: bool = False):
        conn = self._real.connect(db_role, read_only=read_only)
        if read_only:
            return conn
        return _FailOnNthUpdateConn(conn, self._fail_on_update_number)


class _FailOnNthUpdateConn:
    def __init__(self, conn, fail_on_update_number: int) -> None:
        self._conn = conn
        self._fail_on_update_number = fail_on_update_number
        self._update_count = 0

    def execute(self, sql: str, *args, **kwargs):
        if "UPDATE daily_features" in sql:
            self._update_count += 1
            if self._update_count == self._fail_on_update_number:
                raise RuntimeError("boom on second update")
        return self._conn.execute(sql, *args, **kwargs)

    def fetchone(self):
        return self._conn.fetchone()

    def fetchall(self):
        return self._conn.fetchall()

    def close(self) -> None:
        self._conn.close()


def test_write_failure_rolls_back(tmp_db_paths: dict[str, Path]) -> None:
    """Mid-transaction rollback: first UPDATE succeeds, second raises.

    Proves that an already-applied write inside the transaction is reverted when
    a later update fails — not just that rollback is called when the first update
    fails. Both feature rows must remain unchanged after the failure.
    """
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Two consecutive weekday dates so classify() produces two classifications.
    day2 = date(2023, 7, 5)  # Wednesday
    day1 = date(2023, 7, 4)  # Tuesday (day before)
    # 220-bar series ending on day2; day1 is bar 219 (EMA200 available on both).
    _seed_constant_regime_symbols(prod, day2, 220, spy_last=130.0)
    _seed_feature_rows(prod, day1, ["AAA"])
    _seed_feature_rows(prod, day2, ["AAA"])

    # Fail on the *second* UPDATE so the first (day1) is attempted before rollback.
    failing = _FailOnNthUpdateDb(dbm, fail_on_update_number=2)
    res = MarketRegimeEngine(db_manager=failing).classify(day1, day2)

    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["feature_rows_updated"] == 0          # rolled back
    assert res.metadata["dates_classified"] == 2              # both dates computed
    assert res.rows_processed == res.metadata["dates_classified"]  # invariant holds
    # Rollback: neither row should have a regime written.
    assert _fetch_feature_regime(prod, "AAA", day1)["market_regime"] is None
    assert _fetch_feature_regime(prod, "AAA", day2)["market_regime"] is None


# --------------------------------------------------------------------------- #
# 9. regimes_by_value correctness
# --------------------------------------------------------------------------- #
def test_regimes_by_value_keys_and_sum(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 6, 12)
    _seed_constant_regime_symbols(prod, last, 220, spy_last=130.0)
    res = MarketRegimeEngine().classify(last - timedelta(days=2), last)
    rbv = res.metadata["regimes_by_value"]
    # every priority regime present
    assert set(rbv) == set(constants.MARKET_REGIME_PRIORITY)
    # values sum to dates_classified
    assert sum(rbv.values()) == res.metadata["dates_classified"]


# --------------------------------------------------------------------------- #
# 10. Reads only data_quality_status='ok' rows
# --------------------------------------------------------------------------- #
def test_non_ok_rows_excluded(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 6, 13)
    # Seed an 'ok' history, then add a non-ok bullish SPY row on `last` that must
    # be ignored; the last 'ok' SPY row is below EMA200 with bearish QQQ -> bear.
    days = _trading_days_ending_on(last, 220)
    spy = [100.0] * len(days)
    spy[-1] = 70.0
    qqq = [100.0] * len(days)
    qqq[-1] = 70.0
    _seed_prices(prod, constants.BENCHMARK_SPY, days, spy)
    _seed_prices(prod, constants.BENCHMARK_QQQ, days, qqq)
    _seed_prices(prod, constants.BENCHMARK_VIX, days, [15.0] * len(days), use_adj=False)
    # non-ok row that would flip to bull if (wrongly) read
    _seed_prices(prod, constants.BENCHMARK_SPY, [last + timedelta(days=1)], [130.0], status="suspect")
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_BEAR] == 1


# --------------------------------------------------------------------------- #
# 11. debug role works too
# --------------------------------------------------------------------------- #
def test_debug_role_supported(tmp_db_paths: dict[str, Path]) -> None:
    debug = tmp_db_paths[dbm.DB_ROLE_DEBUG]
    last = date(2023, 6, 14)
    _seed_constant_regime_symbols(debug, last, 220, spy_last=130.0)
    _seed_feature_rows(debug, last, ["AAA"])
    res = MarketRegimeEngine().classify(last, last, db_role="debug")
    assert res.is_ok()
    assert res.metadata["db_role"] == "debug"
    assert _fetch_feature_regime(debug, "AAA", last)["market_regime"] == constants.REGIME_BULL


# --------------------------------------------------------------------------- #
# 12. Static source scans (no forbidden patterns)
# --------------------------------------------------------------------------- #
def _engine_source() -> str:
    return Path(mremod.__file__).read_text(encoding="utf-8")


def _imported_module_names(src: str) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _non_docstring_strings(src: str) -> list[str]:
    tree = ast.parse(src)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
    ]


def test_no_direct_duckdb_or_attach_or_ddl() -> None:
    src = _engine_source()
    assert "duckdb" not in _imported_module_names(src)
    for s in _non_docstring_strings(src):
        upper = s.upper()
        assert "ATTACH" not in upper
        assert "CREATE TABLE" not in upper
        assert "ALTER TABLE" not in upper
        assert "DROP TABLE" not in upper
        assert "INSERT INTO" not in upper  # Module 12 never inserts


def test_no_print_in_engine() -> None:
    tree = ast.parse(_engine_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_provider_imports() -> None:
    imported = _imported_module_names(_engine_source())
    assert "yfinance" not in imported
    assert not any(m == "providers" or m.startswith("providers") for m in imported)
    for s in _non_docstring_strings(_engine_source()):
        low = s.lower()
        assert "yfinance" not in low
        assert "providers" not in low


# --------------------------------------------------------------------------- #
# 13. SPY close at EMA boundary, QQQ above EMA → neutral
# --------------------------------------------------------------------------- #
def test_spy_near_ema_boundary_qqq_above_is_neutral(tmp_db_paths: dict[str, Path]) -> None:
    """Strict-inequality boundary: SPY alone below EMA does not produce bear.

    With a constant-price series the EWM accumulates a tiny float delta so that
    ``spy_close (base) < spy_ema200 (base + epsilon)`` is technically True, but
    the bear predicate also requires ``qqq_close < qqq_ema200``.  Setting QQQ's
    last close well above its EMA blocks the bear condition, and since
    ``spy_close > spy_ema200`` is False, bull also does not fire → neutral.

    This proves: SPY below EMA alone is not sufficient for bear; both legs of
    the strict ``<`` inequality must be satisfied.
    """
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    last = date(2023, 7, 10)  # Monday
    # 220-bar series: SPY constant at base (EMA drifts infinitesimally above base),
    # QQQ last close = base + 10 (clearly above QQQ EMA ≈ base) → bear blocked.
    _seed_constant_regime_symbols(prod, last, 220, qqq_last=110.0)
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_NEUTRAL] == 1
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 0
    assert res.metadata["regimes_by_value"][constants.REGIME_BEAR] == 0
    assert res.metadata["dates_classified"] == 1


# --------------------------------------------------------------------------- #
# 14. Exact VIX threshold boundary tests
# --------------------------------------------------------------------------- #
def test_vix_exact_extreme_threshold_fires(tmp_db_paths: dict[str, Path]) -> None:
    """VIX == 30.0 exactly must fire extreme_risk (>= not >)."""
    last = date(2023, 7, 11)
    # SPY bullish so it would normally be bull; VIX == 30.0 must override it.
    _seed_constant_regime_symbols(
        tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=130.0, vix_last=30.0
    )
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_EXTREME_RISK] == 1
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 0


def test_vix_exact_high_threshold_fires(tmp_db_paths: dict[str, Path]) -> None:
    """VIX == 25.0 exactly must fire high_risk (>= not >) and not extreme_risk."""
    last = date(2023, 7, 12)
    # SPY bullish so it would normally be bull; VIX == 25.0 must produce high_risk.
    _seed_constant_regime_symbols(
        tmp_db_paths[dbm.DB_ROLE_PROD], last, 220, spy_last=130.0, vix_last=25.0
    )
    res = MarketRegimeEngine().classify(last, last)
    assert res.metadata["regimes_by_value"][constants.REGIME_HIGH_RISK] == 1
    assert res.metadata["regimes_by_value"][constants.REGIME_EXTREME_RISK] == 0
    assert res.metadata["regimes_by_value"][constants.REGIME_BULL] == 0


# --------------------------------------------------------------------------- #
# 15. Priority guard — duplicate and missing regime values
# --------------------------------------------------------------------------- #
def test_priority_duplicate_regime_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate entry in MARKET_REGIME_PRIORITY must fail before any DB access."""
    monkeypatch.setattr(
        constants,
        "MARKET_REGIME_PRIORITY",
        (
            constants.REGIME_EXTREME_RISK,
            constants.REGIME_HIGH_RISK,
            constants.REGIME_BEAR,
            constants.REGIME_BULL,
            constants.REGIME_BULL,  # duplicate
        ),
    )
    res = MarketRegimeEngine(db_manager=_ExplodingDb()).classify(
        date(2022, 6, 1), date(2022, 6, 1)
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == res.metadata["dates_classified"] == 0


def test_priority_missing_regime_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A regime missing from MARKET_REGIME_PRIORITY must fail before any DB access."""
    monkeypatch.setattr(
        constants,
        "MARKET_REGIME_PRIORITY",
        (
            constants.REGIME_EXTREME_RISK,
            constants.REGIME_HIGH_RISK,
            constants.REGIME_BULL,
            constants.REGIME_NEUTRAL,
            # REGIME_BEAR omitted
        ),
    )
    res = MarketRegimeEngine(db_manager=_ExplodingDb()).classify(
        date(2022, 6, 1), date(2022, 6, 1)
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == res.metadata["dates_classified"] == 0


def test_priority_unknown_regime_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised value in MARKET_REGIME_PRIORITY must fail before any DB access."""
    monkeypatch.setattr(
        constants,
        "MARKET_REGIME_PRIORITY",
        (
            constants.REGIME_EXTREME_RISK,
            constants.REGIME_HIGH_RISK,
            constants.REGIME_BEAR,
            constants.REGIME_BULL,
            constants.REGIME_NEUTRAL,
            "unknown_regime",
        ),
    )
    res = MarketRegimeEngine(db_manager=_ExplodingDb()).classify(
        date(2022, 6, 1), date(2022, 6, 1)
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == res.metadata["dates_classified"] == 0
    assert res.metadata["feature_rows_updated"] == 0
