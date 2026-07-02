"""Phase 0 — Point-in-Time / Look-Ahead Audit Tests.

Verifies that M11 (FeatureEngine) and M12 (MarketRegimeEngine) never use
information that was not knowable on the signal_date (feature_cutoff_date).

Audit Inventory
---------------
Join                    | Date field            | Verdict         | Fix
------------------------|----------------------|-----------------|------
earnings_calendar       | updated_at           | LEAK — fixed    | WHERE CAST(updated_at AS DATE) <= cutoff
ticker_master.sector    | last_updated (whole) | asof-blind¹     | Deferred (schema limitation)
daily_prices (benchmark)| date <= end_date     | asof-safe       | None needed
daily_prices (regime)   | date <= end_date     | asof-safe       | None needed

¹ ticker_master has no versioned sector history. last_updated covers the whole
  row, not sector specifically. A fix requires either sector_history table or
  migrating sector lookup to ticker_universe_snapshot (snapshot_month key).
  Risk is accepted as low — sector reassignments are extremely rare in practice.

Tests
-----
1. test_earnings_future_updated_at_excluded           — fails pre-fix, passes post-fix
2. test_earnings_revised_date_uses_pre_signal_record  — fails pre-fix, passes post-fix
3. test_earnings_known_before_signal_date_is_visible  — control; passes both
4. test_benchmark_forward_price_not_used_in_rs        — asof-safe; passes both
5. test_m12_forward_benchmark_not_used_in_regime      — asof-safe; passes both
6. test_sector_reassignment_is_asof_blind             — documents known limitation
"""

from __future__ import annotations

import duckdb
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.features.feature_engine import FeatureEngine
from app.services.regime.market_regime_engine import MarketRegimeEngine
from app.utils import service_result


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Wire isolated DB paths into tmp_path and apply real schema."""
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

    return {dbm.DB_ROLE_PROD: prod, dbm.DB_ROLE_DEBUG: debug}


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
_INSERT_PRICE = (
    "INSERT INTO daily_prices "
    "(ticker, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
    " open_adj, high_adj, low_adj, close_adj, volume_adj, "
    " dividend_amount, split_ratio, adjustment_factor, source_provider, "
    " data_quality_status, mutation_flag, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, NULL, 'fake', ?, FALSE, "
    " CAST(now() AS TIMESTAMP), NULL)"
)

_INSERT_TICKER_MASTER = (
    "INSERT INTO ticker_master "
    "(ticker, symbol_type, sector, active_flag, delisted_flag) "
    "VALUES (?, 'stock', ?, TRUE, FALSE)"
)

_INSERT_EARNINGS = (
    "INSERT INTO earnings_calendar "
    "(ticker, earnings_date, session, source, confidence, updated_at) "
    "VALUES (?, ?, NULL, 'test', ?, CAST(? AS TIMESTAMP))"
)


def _trading_days(start: date, n: int) -> list[date]:
    """Return n consecutive Mon-Fri dates starting at/after start."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed_prices(
    db_path: Path,
    ticker: str,
    days: list[date],
    closes: list[float],
    status: str = "ok",
) -> None:
    conn = duckdb.connect(str(db_path))
    try:
        for d, c in zip(days, closes):
            h = c + 1.0
            lo = max(0.01, c - 1.0)
            conn.execute(_INSERT_PRICE, [ticker, d, c, h, lo, c, 2_000_000, c, h, lo, c, None, status])
    finally:
        conn.close()


def _seed_ticker_master(db_path: Path, ticker: str, sector: str | None) -> None:
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(_INSERT_TICKER_MASTER, [ticker, sector])
    finally:
        conn.close()


def _seed_earnings(
    db_path: Path,
    ticker: str,
    earnings_date: date,
    confidence: str,
    updated_at: date,
) -> None:
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(_INSERT_EARNINGS, [ticker, earnings_date, confidence, f"{updated_at.isoformat()} 00:00:00"])
    finally:
        conn.close()


def _fetch_feature(db_path: Path, ticker: str) -> dict:
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        row = conn.execute(
            "SELECT * FROM daily_features WHERE ticker = ? ORDER BY feature_date DESC LIMIT 1",
            [ticker],
        ).fetchone()
        if row is None:
            return {}
        cols = [d[0] for d in conn.execute(
            "SELECT * FROM daily_features WHERE ticker = ? LIMIT 0", [ticker]
        ).description]
        return dict(zip(cols, row))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. Earnings calendar — look-ahead leak (confirmed, now fixed)
# --------------------------------------------------------------------------- #

def test_earnings_future_updated_at_excluded(tmp_db: dict[str, Path]) -> None:
    """Earnings record published after signal_date must not affect features.

    A record with updated_at = signal_date + 1 day represents information that
    was not available on signal_date. It must be excluded by the asof filter.
    Before the fix this test FAILED (days_to_earnings_bd was non-NULL).
    After the fix it passes (days_to_earnings_bd is NULL — record filtered out).
    """
    prod = tmp_db[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    signal_date = days[-1]

    _seed_prices(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])

    # Earnings published ONE day AFTER signal_date — must be invisible.
    future_earnings_date = signal_date + timedelta(days=20)
    future_updated_at = signal_date + timedelta(days=1)
    _seed_earnings(prod, "AAA", future_earnings_date, "high", future_updated_at)

    FeatureEngine().calculate(signal_date, signal_date, tickers=["AAA"])
    row = _fetch_feature(prod, "AAA")

    assert row["days_to_earnings_bd"] is None, (
        "Earnings published after signal_date leaked into feature computation"
    )
    assert row["earnings_confidence"] is None


def test_earnings_revised_date_uses_pre_signal_record_only(tmp_db: dict[str, Path]) -> None:
    """A revision published after signal_date must not override the pre-signal record.

    Scenario (two rows in earnings_calendar for the same ticker):
      Pre-signal:  earnings_date = signal_date + 45 days, confidence='low',
                   updated_at = signal_date - 5 days  → VISIBLE
      Post-signal: earnings_date = signal_date + 20 days, confidence='high',
                   updated_at = signal_date + 1 day   → must be INVISIBLE

    Without the fix, the engine picks the closer (revised) date and returns
    confidence='high'. With the fix, only the original date is visible and
    confidence='low'.
    """
    prod = tmp_db[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    signal_date = days[-1]

    _seed_prices(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])

    # Pre-signal: known 5 days before signal (should be visible).
    _seed_earnings(prod, "AAA", signal_date + timedelta(days=45), "low", signal_date - timedelta(days=5))
    # Post-signal: revised 1 day after signal (must NOT be visible).
    _seed_earnings(prod, "AAA", signal_date + timedelta(days=20), "high", signal_date + timedelta(days=1))

    FeatureEngine().calculate(signal_date, signal_date, tickers=["AAA"])
    row = _fetch_feature(prod, "AAA")

    # Only the pre-signal record is visible → confidence must be 'low', not 'high'.
    assert row["earnings_confidence"] == "low", (
        "Post-signal earnings revision leaked: expected confidence='low' "
        f"(pre-signal record only) but got {row['earnings_confidence']!r}"
    )
    # days_to_earnings_bd should reflect the original date (+45 days), not the revised (+20 days).
    assert row["days_to_earnings_bd"] is not None
    assert row["days_to_earnings_bd"] >= 30  # 45 calendar days ≈ 30+ trading days


def test_earnings_known_before_signal_date_is_visible(tmp_db: dict[str, Path]) -> None:
    """Control: earnings published before signal_date must be included (no over-filtering)."""
    prod = tmp_db[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    signal_date = days[-1]

    _seed_prices(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])

    # Earnings published on signal_date itself (edge case: same day).
    _seed_earnings(prod, "AAA", signal_date + timedelta(days=30), "medium", signal_date)

    FeatureEngine().calculate(signal_date, signal_date, tickers=["AAA"])
    row = _fetch_feature(prod, "AAA")

    assert row["days_to_earnings_bd"] is not None
    assert row["earnings_confidence"] == "medium"


# --------------------------------------------------------------------------- #
# 2. Benchmark price joins (M11) — asof-safe; no fix needed
# --------------------------------------------------------------------------- #

def test_benchmark_forward_price_not_used_in_rs(tmp_db: dict[str, Path]) -> None:
    """A benchmark (SPY) price dated after feature_cutoff_date must not affect RS.

    The daily_prices query is bounded by date <= end_date, so post-cutoff rows
    are excluded at the SQL level. We verify by planting an extreme anomalous
    SPY row on signal_date + 1 and confirming relative_strength_vs_spy is within
    a normal range.
    """
    prod = tmp_db[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    signal_date = days[-1]

    closes = [100.0 + i * 0.1 for i in range(len(days))]
    spy_closes = [400.0 + i * 0.05 for i in range(len(days))]

    _seed_prices(prod, "AAA", days, closes)
    _seed_prices(prod, constants.BENCHMARK_SPY, days, spy_closes)
    # No sector → sector_relative_strength NULL, which is fine; RS vs SPY still computed.

    # Anomalous future SPY row — if used, would make roc20 ~ 999999/400 - 1 ≈ 2499 → extreme.
    future_day = signal_date + timedelta(days=1)
    _seed_prices(prod, constants.BENCHMARK_SPY, [future_day], [999_999.0])

    FeatureEngine().calculate(signal_date, signal_date, tickers=["AAA"])
    row = _fetch_feature(prod, "AAA")

    assert row["relative_strength_vs_spy"] is not None
    # If the anomalous future price were used, the value would be wildly negative (huge SPY roc).
    # A sane result is in the range (-1, 1) for a flat-ish 300-day series.
    assert -1.0 < row["relative_strength_vs_spy"] < 1.0, (
        f"relative_strength_vs_spy={row['relative_strength_vs_spy']!r} "
        "is out of normal bounds — future SPY price may have been used"
    )


# --------------------------------------------------------------------------- #
# 3. Regime benchmark joins (M12) — asof-safe; no fix needed
# --------------------------------------------------------------------------- #

def test_m12_forward_benchmark_not_used_in_regime(tmp_db: dict[str, Path]) -> None:
    """A benchmark price dated after signal_date must not affect the regime classification.

    We plant an extreme VIX value (100.0, which triggers EXTREME_RISK) for
    signal_date + 1. The regime computed for signal_date must NOT be extreme_risk;
    that would only happen if the future VIX row leaked into the classification.
    """
    prod = tmp_db[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    signal_date = days[-1]

    # Normal market: SPY and QQQ both above their EMA200 (bull), VIX calm (~15).
    spy_closes = [400.0 + i * 0.2 for i in range(len(days))]
    qqq_closes = [300.0 + i * 0.15 for i in range(len(days))]
    vix_closes = [15.0] * len(days)

    _seed_prices(prod, constants.BENCHMARK_SPY, days, spy_closes)
    _seed_prices(prod, constants.BENCHMARK_QQQ, days, qqq_closes)
    _seed_prices(prod, constants.BENCHMARK_VIX, days, vix_closes)

    # Also need at least one stock in daily_features for the regime update to touch anything;
    # seed a dummy price series and run M11 first.
    _seed_prices(prod, "AAA", days, [50.0 + i * 0.05 for i in range(len(days))])
    FeatureEngine().calculate(signal_date, signal_date, tickers=["AAA"])

    # Future anomalous VIX row: extreme_risk threshold is 35+ (per constants).
    future_day = signal_date + timedelta(days=1)
    _seed_prices(prod, constants.BENCHMARK_VIX, [future_day], [100.0])

    result = MarketRegimeEngine().classify(signal_date, signal_date)
    assert result.status in (
        service_result.STATUS_SUCCESS,
        service_result.STATUS_SUCCESS_WITH_WARNINGS,
    )

    # Regime must NOT be extreme_risk — that would only happen if the future VIX leaked.
    from app.database import duckdb_manager as _dbm
    import duckdb as _ddb
    conn = _ddb.connect(str(prod), read_only=True)
    try:
        row = conn.execute(
            "SELECT market_regime FROM daily_features WHERE ticker = 'AAA' ORDER BY feature_date DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] != constants.REGIME_EXTREME_RISK, (
        f"market_regime={row[0]!r}: future extreme VIX appears to have leaked into regime classification"
    )


# --------------------------------------------------------------------------- #
# 4. Sector lookup (ticker_master) — known design limitation, documented
# --------------------------------------------------------------------------- #

def test_sector_reassignment_is_asof_blind(tmp_db: dict[str, Path]) -> None:
    """Documents that ticker_master sector lookup is asof-blind.

    ticker_master has no versioned sector history field. The last_updated column
    covers the whole row, not sector specifically, so a per-field as-of filter
    is not possible. If a sector is reassigned after a historical signal_date,
    re-running M11 features for that date will use the NEW sector.

    This test demonstrates the behavior. It is recorded here as a known design
    limitation (LOW RISK — sector reassignments are extremely rare in production).
    A fix requires migrating sector lookup to ticker_universe_snapshot (which has
    snapshot_month date keying) or adding a sector_history table. Both options
    are out of scope for this minimal audit fix.
    """
    prod = tmp_db[dbm.DB_ROLE_PROD]
    days = _trading_days(date(2022, 6, 1), 300)
    signal_date = days[-1]

    tech_etf = constants.SECTOR_ETF_MAP.get("Technology")  # XLK
    assert tech_etf is not None, "Technology ETF must be mapped in constants"

    _seed_prices(prod, "AAA", days, [100.0 + i * 0.1 for i in range(len(days))])
    _seed_prices(prod, tech_etf, days, [50.0 + i * 0.05 for i in range(len(days))])
    _seed_ticker_master(prod, "AAA", sector="Technology")

    # First run: sector = "Technology" → sector_relative_strength is non-NULL.
    FeatureEngine().calculate(signal_date, signal_date, tickers=["AAA"])
    row_before = _fetch_feature(prod, "AAA")
    assert row_before["sector_relative_strength"] is not None

    # Simulate a post-signal sector reassignment by updating ticker_master.
    conn = duckdb.connect(str(prod))
    try:
        conn.execute("UPDATE ticker_master SET sector = 'DoesNotExist' WHERE ticker = 'AAA'")
    finally:
        conn.close()

    # Re-run features for the SAME historical signal_date.
    FeatureEngine().calculate(signal_date, signal_date, tickers=["AAA"])
    row_after = _fetch_feature(prod, "AAA")

    # Because ticker_master is asof-blind, the new sector is now used.
    # A point-in-time correct implementation would still give non-NULL RS
    # (using the Technology sector that was valid on signal_date).
    # This assertion documents the current (leak-prone) behavior:
    assert row_after["sector_relative_strength"] is None, (
        "If this assertion fails, ticker_master is now asof-aware — remove this test."
    )
    # NOTE: no corrective assertion is added here because the fix requires schema
    # changes (out of scope). This test exists solely to document the limitation.
