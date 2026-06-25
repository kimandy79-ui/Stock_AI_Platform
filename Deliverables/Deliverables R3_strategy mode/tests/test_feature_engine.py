"""Tests for Module 11 — Feature Engine.

All tests run fully offline (no network, no provider) and never touch the real
prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects every
DuckDB settings path into pytest ``tmp_path`` and applies the real Module 03
schema there (mirroring ``tests/test_mutation_detector.py``). Price rows are
seeded directly into ``daily_prices``; Module 11 reads them read-only and writes
only ``daily_features`` via upsert.

Deterministic formula checks construct controlled series and recompute the
expected value with the same vectorised method the implementation uses (EMA /
Wilder RSI / Wilder ATR via recursive EWM with ``adjust=False``; closed forms
for ROC20 / RVOL20), asserting near-equality with a tight tolerance.
"""

from __future__ import annotations

import ast
import inspect
import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.config import constants
from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.features import feature_engine as femod
from app.services.features.feature_engine import FeatureEngine
from app.utils import service_result

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "db_role",
        "start_date",
        "end_date",
        "tickers_requested",
        "tickers_processed",
        "tickers_skipped_no_data",
        "rows_read",
        "feature_rows_written",
        "feature_rows_updated",
        "feature_ready_count",
        "feature_not_ready_count",
    }
)

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
# not Module 11 — Module 11 itself never inserts price rows).
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

_INSERT_TICKER_MASTER = (
    "INSERT INTO ticker_master "
    "(ticker, symbol_type, sector, active_flag, delisted_flag) "
    "VALUES (?, ?, ?, TRUE, FALSE)"
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


def _seed_series(
    db_path: Path,
    ticker: str,
    days: list[date],
    closes: list[float],
    volumes: list[int] | None = None,
    *,
    status: str = "ok",
    high_offset: float = 0.5,
    low_offset: float = 0.5,
    role_conn=None,
) -> None:
    """Seed a price series. high_adj/low_adj bracket close_adj; raw == adj."""
    import duckdb

    vols = volumes if volumes is not None else [1_000_000] * len(days)
    conn = duckdb.connect(str(db_path))
    try:
        for d, c, v in zip(days, closes, vols):
            high = c + high_offset
            low = c - low_offset
            conn.execute(
                _INSERT_PRICE,
                [
                    ticker, d,
                    c, high, low, c, v,        # raw OHLCV (raw == adj here)
                    c, high, low, c, None,     # adj OHLC + volume_adj NULL
                    SOURCE, status,
                ],
            )
    finally:
        conn.close()


def _seed_ticker_master(db_path: Path, ticker: str, sector: str | None, symbol_type: str = "stock") -> None:
    import duckdb

    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(_INSERT_TICKER_MASTER, [ticker, symbol_type, sector])
    finally:
        conn.close()


def _count_features(db_path: Path) -> int:
    import duckdb

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM daily_features").fetchone()[0]
    finally:
        conn.close()


def _fetch_feature(db_path: Path, ticker: str) -> dict | None:
    import duckdb

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = conn.execute(
            "SELECT * FROM daily_features WHERE ticker = ?", [ticker]
        ).fetchall()
        cols = [d[0] for d in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'daily_features' ORDER BY ordinal_position"
        ).fetchall()]
    finally:
        conn.close()
    if not rows:
        return None
    return dict(zip(cols, rows[0]))


# Reference recursive EWM (adjust=False) matching the implementation.
def _ewm(values: list[float], alpha: float) -> list[float]:
    out: list[float] = []
    prev = None
    for v in values:
        prev = v if prev is None else (1 - alpha) * prev + alpha * v
        out.append(prev)
    return out


def _ema(values: list[float], span: int) -> list[float]:
    return _ewm(values, 2.0 / (span + 1.0))


# --------------------------------------------------------------------------- #
# 1. Public API / signature / metadata / rows_processed invariant
# --------------------------------------------------------------------------- #
def test_calculate_signature_exact() -> None:
    sig = inspect.signature(FeatureEngine.calculate)
    params = list(sig.parameters)
    assert params == ["self", "start_date", "end_date", "tickers", "db_role", "run_id"]
    assert sig.parameters["tickers"].default is None
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["run_id"].default is None


def test_run_id_minted_when_none(tmp_db_paths: dict[str, Path]) -> None:
    res = FeatureEngine().calculate(date(2024, 1, 2), date(2024, 1, 31))
    assert isinstance(res.run_id, str) and len(res.run_id) >= 32


def test_run_id_preserved_when_supplied(tmp_db_paths: dict[str, Path]) -> None:
    res = FeatureEngine().calculate(date(2024, 1, 2), date(2024, 1, 31), run_id="fixed-id")
    assert res.run_id == "fixed-id"


def test_metadata_keys_exact_on_success(tmp_db_paths: dict[str, Path]) -> None:
    res = FeatureEngine().calculate(date(2024, 1, 2), date(2024, 1, 31))
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_exact_on_guard_failure(tmp_db_paths: dict[str, Path]) -> None:
    res = FeatureEngine().calculate(date(2024, 1, 2), date(2024, 1, 31), db_role="simulation")
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


def test_rows_processed_equals_tickers_processed(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2023, 1, 2), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])
    res = FeatureEngine().calculate(days[-1], days[-1])
    assert res.rows_processed == res.metadata["tickers_processed"] == 1


# --------------------------------------------------------------------------- #
# 2. Guards (no DB access before guard failure)
# --------------------------------------------------------------------------- #
class _ExplodingDb:
    """DB manager stub that raises if any connection is attempted."""

    def connect(self, db_role: str, read_only: bool = False):  # noqa: D401
        raise AssertionError("DB must not be accessed before guard passes")


@pytest.mark.parametrize("bad_role", ["simulation", "PROD", "", "other"])
def test_invalid_db_role_fails_without_db_access(bad_role: str) -> None:
    res = FeatureEngine(db_manager=_ExplodingDb()).calculate(
        date(2024, 1, 2), date(2024, 1, 31), db_role=bad_role
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == 0
    assert res.metadata["tickers_processed"] == 0


def test_invalid_date_range_fails_without_db_access() -> None:
    res = FeatureEngine(db_manager=_ExplodingDb()).calculate(
        date(2024, 2, 1), date(2024, 1, 1), db_role="prod"
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == 0


def test_tickers_requested_reported_on_guard_failure() -> None:
    res = FeatureEngine(db_manager=_ExplodingDb()).calculate(
        date(2024, 2, 1), date(2024, 1, 1), tickers=["AAA", "BBB"], db_role="prod"
    )
    assert res.metadata["tickers_requested"] == 2


# --------------------------------------------------------------------------- #
# 3. Ticker selection
# --------------------------------------------------------------------------- #
def test_tickers_none_processes_all_eligible(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2023, 1, 2), 300)
    for t in ("AAA", "BBB", "CCC"):
        _seed_series(prod, t, days, [50.0 + i * 0.05 for i in range(len(days))])
    res = FeatureEngine().calculate(days[-1], days[-1])
    assert res.metadata["tickers_requested"] == 0
    assert res.metadata["tickers_processed"] == 3
    assert res.metadata["tickers_skipped_no_data"] == 0


def test_duplicate_tickers_deduplicated_in_requested_count(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [50.0 + i * 0.05 for i in range(len(days))])
    res = FeatureEngine().calculate(days[-1], days[-1], tickers=["AAA", "AAA", "ZZZ"])
    assert res.metadata["tickers_requested"] == 2  # unique: AAA, ZZZ
    assert res.metadata["tickers_processed"] == 1  # only AAA has data
    assert res.metadata["tickers_skipped_no_data"] == 1  # ZZZ skipped


def test_explicit_list_skips_no_data(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2023, 1, 2), 300)
    _seed_series(prod, "AAA", days, [50.0 + i * 0.05 for i in range(len(days))])
    res = FeatureEngine().calculate(days[-1], days[-1], tickers=["AAA", "ZZZ"])
    assert res.metadata["tickers_requested"] == 2
    assert res.metadata["tickers_processed"] == 1
    assert res.metadata["tickers_skipped_no_data"] == 1


# --------------------------------------------------------------------------- #
# 4. Data-quality filter, warmup, no-lookahead, range write boundary
# --------------------------------------------------------------------------- #
def test_non_ok_rows_excluded(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2023, 1, 2), 300)
    _seed_series(prod, "AAA", days, [50.0 + i * 0.05 for i in range(len(days))], status="failed")
    res = FeatureEngine().calculate(days[-1], days[-1])
    assert res.metadata["tickers_processed"] == 0
    assert _count_features(prod) == 0


def test_feature_date_is_cutoff_within_range(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2023, 1, 2), 320)
    _seed_series(prod, "AAA", days, [50.0 + i * 0.05 for i in range(len(days))])
    # Request a window whose latest eligible date is days[300].
    start, end = days[290], days[300]
    res = FeatureEngine().calculate(start, end)
    assert res.metadata["tickers_processed"] == 1
    row = _fetch_feature(prod, "AAA")
    assert row["feature_date"] == days[300]
    assert row["feature_cutoff_date"] == days[300]
    # No row written outside the requested range.
    assert start <= row["feature_date"] <= end


def test_warmup_read_exceeds_range_rows(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2023, 1, 2), 300)
    _seed_series(prod, "AAA", days, [50.0 + i * 0.05 for i in range(len(days))])
    # Single-day range, but warmup must read prior history too.
    res = FeatureEngine().calculate(days[-1], days[-1])
    assert res.metadata["rows_read"] > 1
    assert res.metadata["tickers_processed"] == 1


def test_no_lookahead_cutoff_value(tmp_db_paths: dict[str, Path]) -> None:
    """A spike strictly after the cutoff must not change the cutoff-day EMA."""
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2023, 1, 2), 320)
    closes = [100.0] * len(days)
    closes[305] = 999.0  # spike after the cutoff (days[300])
    _seed_series(prod, "AAA", days, closes)
    FeatureEngine().calculate(days[300], days[300])
    row = _fetch_feature(prod, "AAA")
    # On a flat 100 series up to the cutoff, EMA20 must be ~100, not pulled up.
    assert row["ema20"] == pytest.approx(100.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# 5. Readiness (sufficient vs insufficient history)
# --------------------------------------------------------------------------- #
def test_feature_ready_with_full_history(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + math.sin(i / 9.0) for i in range(len(days))])
    res = FeatureEngine().calculate(days[-1], days[-1])
    assert res.metadata["feature_ready_count"] == 1
    assert res.metadata["feature_not_ready_count"] == 0
    row = _fetch_feature(prod, "AAA")
    assert row["feature_ready"] is True
    for col in femod.REQUIRED_FEATURE_COLUMNS:
        assert row[col] is not None


def test_feature_not_ready_with_short_history(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2024, 1, 2), 30)  # < 252 bars
    _seed_series(prod, "AAA", days, [100.0 + i for i in range(len(days))])
    res = FeatureEngine().calculate(days[-1], days[-1])
    assert res.metadata["tickers_processed"] == 1
    assert res.metadata["feature_ready_count"] == 0
    assert res.metadata["feature_not_ready_count"] == 1
    row = _fetch_feature(prod, "AAA")
    assert row["feature_ready"] is False
    assert row["distance_from_52w_high_pct"] is None  # 252-window not filled


# --------------------------------------------------------------------------- #
# 6. Deterministic formula checks
# --------------------------------------------------------------------------- #
def test_ema_roc_rvol_exact(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    closes = [100.0 + (i % 7) * 0.5 + i * 0.01 for i in range(len(days))]
    vols = [1_000_000 + (i % 5) * 10_000 for i in range(len(days))]
    _seed_series(prod, "AAA", days, closes, volumes=vols)
    FeatureEngine().calculate(days[-1], days[-1])
    row = _fetch_feature(prod, "AAA")

    expected_ema20 = _ema(closes, 20)[-1]
    expected_ema50 = _ema(closes, 50)[-1]
    expected_ema200 = _ema(closes, 200)[-1]
    assert row["ema20"] == pytest.approx(expected_ema20, rel=1e-9)
    assert row["ema50"] == pytest.approx(expected_ema50, rel=1e-9)
    assert row["ema200"] == pytest.approx(expected_ema200, rel=1e-9)

    expected_roc20 = closes[-1] / closes[-21] - 1.0
    assert row["roc20"] == pytest.approx(expected_roc20, rel=1e-12)

    avg_vol_20 = sum(vols[-21:-1]) / 20.0  # prior 20: t-20..t-1
    assert row["avg_volume_20d"] == pytest.approx(avg_vol_20, rel=1e-12)
    assert row["rvol20"] == pytest.approx(vols[-1] / avg_vol_20, rel=1e-12)

    expected_dollar = sum(closes[i] * vols[i] for i in range(len(days) - 21, len(days) - 1)) / 20.0
    assert row["avg_dollar_volume_20d"] == pytest.approx(expected_dollar, rel=1e-12)


def test_rsi_atr_wilder(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    closes = [100.0 + 5.0 * math.sin(i / 6.0) for i in range(len(days))]
    _seed_series(prod, "AAA", days, closes, high_offset=0.7, low_offset=0.3)
    FeatureEngine().calculate(days[-1], days[-1])
    row = _fetch_feature(prod, "AAA")

    # RSI14 via recursive EWM on gains/losses (matching the implementation).
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = _ewm(gains, 1 / 14)[-1]
    avg_loss = _ewm(losses, 1 / 14)[-1]
    expected_rsi = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    assert row["rsi14"] == pytest.approx(expected_rsi, rel=1e-9)

    # ATR14 via recursive EWM on true range (adjusted OHLC).
    highs = [c + 0.7 for c in closes]
    lows = [c - 0.3 for c in closes]
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        )
    expected_atr = _ewm(trs, 1 / 14)[-1]
    assert row["atr14"] == pytest.approx(expected_atr, rel=1e-9)
    assert row["atr_pct"] == pytest.approx(expected_atr / closes[-1], rel=1e-9)


def test_52w_high_and_pullback_and_breakout(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    closes = [100.0 + math.sin(i / 11.0) * 3.0 for i in range(len(days))]
    _seed_series(prod, "AAA", days, closes)
    FeatureEngine().calculate(days[-1], days[-1])
    row = _fetch_feature(prod, "AAA")

    high252 = max(closes[-252:])
    high20 = max(closes[-20:])
    assert row["distance_from_52w_high_pct"] == pytest.approx(closes[-1] / high252 - 1.0, rel=1e-9)
    assert row["pullback_from_recent_high_pct"] == pytest.approx(closes[-1] / high20 - 1.0, rel=1e-9)
    expected_breakout = (closes[-1] - high20) / row["atr14"]
    assert row["breakout_proximity"] == pytest.approx(expected_breakout, rel=1e-9)


def test_consolidation_score_clamped(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + math.sin(i / 8.0) for i in range(len(days))])
    FeatureEngine().calculate(days[-1], days[-1])
    row = _fetch_feature(prod, "AAA")
    assert row["consolidation_score"] is not None
    assert 0.0 <= row["consolidation_score"] <= 100.0


def test_ema_alignment_score_values(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    # Strong steady uptrend -> EMA20 > EMA50 > EMA200 -> alignment 100.
    _seed_series(prod, "AAA", days, [50.0 + i * 0.5 for i in range(len(days))])
    FeatureEngine().calculate(days[-1], days[-1])
    row = _fetch_feature(prod, "AAA")
    assert row["ema_alignment_score"] == pytest.approx(100.0)
    assert row["ema20"] > row["ema50"] > row["ema200"]


# --------------------------------------------------------------------------- #
# 7. Sector relative strength
# --------------------------------------------------------------------------- #
def test_sector_relative_strength_when_data_present(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    etf = constants.SECTOR_ETF_MAP["Technology"]  # XLK
    stock_closes = [100.0 + i * 0.20 for i in range(len(days))]
    etf_closes = [80.0 + i * 0.10 for i in range(len(days))]
    _seed_series(prod, "AAA", days, stock_closes)
    _seed_series(prod, etf, days, etf_closes)
    _seed_ticker_master(prod, "AAA", "Technology")
    _seed_ticker_master(prod, etf, None, symbol_type="etf")

    FeatureEngine().calculate(days[-1], days[-1], tickers=["AAA"])
    row = _fetch_feature(prod, "AAA")
    stock_roc = stock_closes[-1] / stock_closes[-21] - 1.0
    etf_roc = etf_closes[-1] / etf_closes[-21] - 1.0
    assert row["sector_relative_strength"] == pytest.approx(stock_roc - etf_roc, rel=1e-9)


def test_sector_relative_strength_null_when_unmapped(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.2 for i in range(len(days))])
    _seed_ticker_master(prod, "AAA", "Nonexistent Sector")
    FeatureEngine().calculate(days[-1], days[-1], tickers=["AAA"])
    row = _fetch_feature(prod, "AAA")
    assert row["sector_relative_strength"] is None


# --------------------------------------------------------------------------- #
# 8. Open gaps: market regime + earnings/macro fallback
# --------------------------------------------------------------------------- #
def test_market_regime_null_open_gap(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])
    FeatureEngine().calculate(days[-1], days[-1])
    row = _fetch_feature(prod, "AAA")
    assert row["market_regime"] is None


def test_earnings_macro_fallback_defaults(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])
    FeatureEngine().calculate(days[-1], days[-1])
    row = _fetch_feature(prod, "AAA")
    assert row["days_to_earnings_bd"] is None
    assert row["earnings_confidence"] is None
    assert row["macro_event_risk_flag"] is False


# --------------------------------------------------------------------------- #
# 9. Upsert idempotency + calculated_at refresh
# --------------------------------------------------------------------------- #
def test_upsert_idempotent(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])

    r1 = FeatureEngine().calculate(days[-1], days[-1])
    assert r1.metadata["feature_rows_written"] == 1
    assert r1.metadata["feature_rows_updated"] == 0

    r2 = FeatureEngine().calculate(days[-1], days[-1])
    assert r2.metadata["feature_rows_written"] == 0
    assert r2.metadata["feature_rows_updated"] == 1

    assert _count_features(prod) == 1  # no duplicate


def test_calculated_at_refreshes(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])
    FeatureEngine().calculate(days[-1], days[-1])
    first = _fetch_feature(prod, "AAA")["calculated_at"]
    FeatureEngine().calculate(days[-1], days[-1])
    second = _fetch_feature(prod, "AAA")["calculated_at"]
    assert second >= first


# --------------------------------------------------------------------------- #
# 10. Write ownership: only daily_features changes
# --------------------------------------------------------------------------- #
def test_only_daily_features_written(tmp_db_paths: dict[str, Path]) -> None:
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])

    forbidden = (
        "daily_prices", "ticker_master", "ticker_universe_snapshot",
        "sector_etf_map", "data_repair_queue", "feature_rebuild_log",
        "step3_candidates", "step5_proposals", "signal_outcomes",
        "outcome_tracking_queue",
    )

    def snapshot() -> dict[str, int]:
        conn = duckdb.connect(str(prod), read_only=True)
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in forbidden}
        finally:
            conn.close()

    before = snapshot()
    FeatureEngine().calculate(days[-1], days[-1])
    after = snapshot()
    assert before == after
    assert _count_features(prod) == 1


# --------------------------------------------------------------------------- #
# 11. Rollback leaves no partial feature rows
# --------------------------------------------------------------------------- #
class _WriteFailDb:
    """Wraps the real manager but makes the upsert raise mid-transaction."""

    def __init__(self, real, fail_on: str) -> None:
        self._real = real
        self._fail_on = fail_on

    def connect(self, db_role: str, read_only: bool = False):
        conn = self._real.connect(db_role, read_only=read_only)
        if read_only:
            return conn
        return _FailingConn(conn, self._fail_on)


class _FailingConn:
    def __init__(self, conn, fail_on: str) -> None:
        self._conn = conn
        self._fail_on = fail_on

    def execute(self, sql: str, *args, **kwargs):
        if self._fail_on in sql:
            raise RuntimeError("boom")
        return self._conn.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._conn.close()


def test_write_failure_rolls_back(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    _seed_series(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])

    failing = _WriteFailDb(dbm, fail_on="INSERT INTO daily_features")
    res = FeatureEngine(db_manager=failing).calculate(days[-1], days[-1])

    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["feature_rows_written"] == 0
    assert res.metadata["feature_rows_updated"] == 0
    # read/compute counts may remain accurate
    assert res.metadata["tickers_processed"] == 1
    assert res.rows_processed == 1
    assert _count_features(prod) == 0  # rolled back, no partial rows


# --------------------------------------------------------------------------- #
# 12. Static source scans (no forbidden patterns)
# --------------------------------------------------------------------------- #
def _engine_source() -> str:
    return Path(femod.__file__).read_text(encoding="utf-8")


def _imported_module_names(src: str) -> set[str]:
    """Return the set of top-level module names imported by ``src``."""
    names: set[str] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _non_docstring_strings(src: str) -> list[str]:
    """Return every string-literal constant in ``src`` except docstrings.

    Docstrings legitimately *describe* forbidden operations ("never uses
    ATTACH"); only executed SQL / code strings should be scanned for real DDL.
    """
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
    # The engine routes all DB access through app.database.duckdb_manager and
    # must never import the duckdb package directly.
    assert "duckdb" not in _imported_module_names(src)
    code_strings = _non_docstring_strings(src)
    for s in code_strings:
        upper = s.upper()
        assert "ATTACH" not in upper
        assert "CREATE TABLE" not in upper
        assert "ALTER TABLE" not in upper
        assert "DROP TABLE" not in upper


def test_no_print_in_engine() -> None:
    tree = ast.parse(_engine_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_provider_imports() -> None:
    src = _engine_source()
    # No provider package import.
    imported = _imported_module_names(src)
    assert "yfinance" not in imported
    assert not any(m == "providers" or m.startswith("providers") for m in imported)
    # No provider package path referenced from non-docstring code.
    for s in _non_docstring_strings(src):
        low = s.lower()
        assert "yfinance" not in low
        assert "providers" not in low
