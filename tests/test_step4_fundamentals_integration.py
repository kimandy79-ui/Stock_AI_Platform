"""Integration tests: Step4SetupValidationEngine reads ticker_fundamentals
(Phase 4) and merges it into the feat dict passed to validators, point-in-time
correct (only rows with as_of_date <= signal_date, most recent per ticker).

Real DuckDB via duckdb_manager + schema_manager; no fakes for the DB layer.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.analysis.step4_setup_validation_engine import (
    Step4SetupValidationEngine,
    _read_fundamentals,
)
from app.utils import service_result

SIGNAL_DATE = date(2024, 3, 15)


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
    assert sm.apply_debug_schema().status == service_result.STATUS_SUCCESS
    return {"debug": debug, "prod": prod, "simulation": simulation}


def _seed_ticker(conn: Any, ticker: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ticker_master "
        "(ticker, symbol_type, active_flag, delisted_flag) VALUES (?, 'stock', TRUE, FALSE)",
        [ticker],
    )


def _seed_fundamentals(
    conn: Any, ticker: str, as_of_date: date, piotroski: int, calculated_at: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO ticker_fundamentals "
        "(ticker, as_of_date, eps_growth_trend, leverage_ratio, valuation_band, "
        " piotroski_f_score, altman_z_score, insider_trade_flag, "
        " institutional_ownership_delta, source_provider, calculated_at) "
        "VALUES (?, ?, 0.1, 0.2, 'fair', ?, 2.5, NULL, NULL, 'sec_edgar', "
        + ("CAST(? AS TIMESTAMP)" if calculated_at else "now()") + ")",
        [ticker, as_of_date.isoformat(), piotroski] + ([calculated_at] if calculated_at else []),
    )


class TestReadFundamentalsPointInTime:
    def test_most_recent_row_at_or_before_signal_date_wins(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "AAPL")
            _seed_fundamentals(conn, "AAPL", date(2023, 6, 1), piotroski=5)
            _seed_fundamentals(conn, "AAPL", date(2024, 1, 1), piotroski=8)
        finally:
            conn.close()

        result = _read_fundamentals(dbm, "debug", SIGNAL_DATE)
        assert result["AAPL"]["piotroski_f_score"] == 8

    def test_future_as_of_date_excluded(self, tmp_db_paths: dict[str, Path]) -> None:
        """Point-in-time discipline: a row dated after signal_date must never
        be visible, mirroring the earnings_calendar leak-guard precedent."""
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "AAPL")
            _seed_fundamentals(conn, "AAPL", date(2023, 6, 1), piotroski=5)
            _seed_fundamentals(conn, "AAPL", date(2024, 12, 1), piotroski=9)  # future
        finally:
            conn.close()

        result = _read_fundamentals(dbm, "debug", SIGNAL_DATE)
        assert result["AAPL"]["piotroski_f_score"] == 5

    def test_ticker_with_no_fundamentals_row_absent_not_errored(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "NOFUND")
        finally:
            conn.close()
        result = _read_fundamentals(dbm, "debug", SIGNAL_DATE)
        assert "NOFUND" not in result

    def test_empty_table_returns_empty_dict(self, tmp_db_paths: dict[str, Path]) -> None:
        result = _read_fundamentals(dbm, "debug", SIGNAL_DATE)
        assert result == {}


class TestEngineMergesFundamentalsIntoFeat:
    def test_engine_run_does_not_fail_when_fundamentals_present(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        """End-to-end smoke test: seeding ticker_fundamentals must not break
        the existing Step4 pipeline (config opts out of scoring by default,
        so this only proves the new read/merge path is wired without error)."""
        run_id = str(uuid.uuid4())
        conn = dbm.connect("debug")
        try:
            _seed_ticker(conn, "AAPL")
            conn.execute(
                "INSERT OR IGNORE INTO daily_prices "
                "(ticker, date, open_raw, high_raw, low_raw, close_raw, close_adj, "
                " volume_raw, data_quality_status, source_provider, created_at) "
                "VALUES (?, ?, 153, 156.5, 152, 155, 155, 5000000, 'ok', 'fake', now())",
                ["AAPL", SIGNAL_DATE.isoformat()],
            )
            conn.execute(
                "INSERT OR IGNORE INTO daily_features "
                "(ticker, feature_date, feature_cutoff_date, feature_schema_version, "
                " feature_ready, breakout_proximity, range_duration, range_tightness_score, "
                " rvol20, resistance_level, next_resistance_level, support_level, "
                " atr_pct, distance_to_ema20_pct, distance_to_ema50_pct, "
                " market_regime, macro_event_risk_flag, calculated_at) "
                "VALUES (?, ?, ?, ?, TRUE, -0.2, 15, 70.0, 2.0, 160.0, 175.0, 148.0, "
                "        0.025, 0.02, 0.05, 'neutral', FALSE, now())",
                ["AAPL", SIGNAL_DATE.isoformat(), SIGNAL_DATE.isoformat(),
                 constants.FEATURE_SCHEMA_VERSION],
            )
            cand_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO step3_candidates "
                "(candidate_id, run_id, ticker, signal_date, eligibility_score, "
                " passed_eligibility, routing_status, routing_fail_reason, "
                " eligibility_fail_reasons, routed_setup_types, feature_snapshot_json, created_at) "
                "VALUES (?, ?, ?, ?, 80.0, TRUE, 'routed', NULL, '[]', ?, ?, now())",
                [cand_id, run_id, "AAPL", SIGNAL_DATE.isoformat(),
                 json.dumps([constants.SETUP_BREAKOUT]), json.dumps({})],
            )
            _seed_fundamentals(conn, "AAPL", date(2024, 1, 1), piotroski=8)
        finally:
            conn.close()

        setup_configs = {
            constants.SETUP_BREAKOUT: {
                "config_id": "setup_breakout_v1",
                "setup_type": "breakout",
                "validation": {"min_setup_score": 0},
                "scoring_weights": {
                    "resistance_clarity": 0.20, "breakout_confirmation": 0.25,
                    "volume_expansion": 0.20, "base_quality": 0.20, "target_room": 0.15,
                },
                "earnings": {},
                "macro_event_risk": {"enabled": False},
            }
        }
        engine = Step4SetupValidationEngine()
        result = engine.run(
            SIGNAL_DATE, db_role="debug", run_id=run_id, setup_configs=setup_configs
        )
        assert result.status in ("success", "success_with_warnings")
        assert result.rows_processed == 1
