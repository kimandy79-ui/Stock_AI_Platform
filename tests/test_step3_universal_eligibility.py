"""Tests for Module 13 — Step 3 Universal Eligibility + Setup Routing.

Covers all acceptance criteria from the Phase 3 review:
  1. consolidation_base canonical naming everywhere.
  2. No constants.py / ServiceResult overwrite — uses accepted Phase 1 versions.
  3. Schema-level DB integration test via tmp_db_paths + schema_manager.
  4. Active tickers with missing price/feature rows appear as ineligible.
  5. feature_snapshot_json populated with eligibility + routing inputs.
  6. Transaction / idempotency: duplicate run_id allowed (different candidate_ids);
     rollback on write error leaves no partial rows.
  7. No unused SQL artefacts (verified by source scan).

Unit tests run fully offline (FakeDB, no duckdb import needed).
Integration tests open a real in-memory DuckDB via accepted duckdb_manager.

All DB paths redirected into tmp_path; no real data/duckdb/ files touched.
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.screening.step3_universal_eligibility import (
    ALLOWED_DB_ROLES,
    METADATA_KEYS,
    ROUTING_INELIGIBLE,
    ROUTING_NO_ROUTE,
    ROUTING_ROUTED,
    ConfigParityError,
    MissingConfigError,
    Step3UniversalEligibilityEngine,
    _assert_universe_parity,
    _build_snapshot,
    _check_eligibility,
    _evaluate_routing,
)
from app.utils.service_result import ServiceResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SIGNAL_DATE = date(2024, 3, 15)
RUN_ID = str(uuid.uuid4())

ALL_SETUP_CONFIGS = [
    {
        "config_id": "setup_breakout_v1",
        "setup_type": "breakout",
        "universe": {
            "min_price": 5.0,
            "min_avg_dollar_volume_20d": 10_000_000.0,
            "allowed_symbol_types": ["stock"],
        },
    },
    {
        "config_id": "setup_pullback_v1",
        "setup_type": "pullback",
        "universe": {
            "min_price": 5.0,
            "min_avg_dollar_volume_20d": 10_000_000.0,
            "allowed_symbol_types": ["stock"],
        },
    },
    {
        "config_id": "setup_trend_continuation_v1",
        "setup_type": "trend_continuation",
        "universe": {
            "min_price": 5.0,
            "min_avg_dollar_volume_20d": 10_000_000.0,
            "allowed_symbol_types": ["stock"],
        },
    },
    {
        "config_id": "setup_consolidation_base_v1",
        "setup_type": "consolidation_base",
        "universe": {
            "min_price": 5.0,
            "min_avg_dollar_volume_20d": 10_000_000.0,
            "allowed_symbol_types": ["stock"],
        },
    },
]

_UNIVERSE = ALL_SETUP_CONFIGS[0]["universe"]
_MIN_PRICE: float = _UNIVERSE["min_price"]
_MIN_ADV: float = _UNIVERSE["min_avg_dollar_volume_20d"]
_ALLOWED_TYPES: list[str] = _UNIVERSE["allowed_symbol_types"]


def _row(**overrides) -> dict[str, Any]:
    """Build a fully eligible, non-routing ticker row with sensible defaults."""
    defaults: dict[str, Any] = dict(
        ticker="AAPL",
        symbol_type="stock",
        open_raw=148.0,
        high_raw=152.0,
        low_raw=147.0,
        close_raw=150.0,
        close_adj=150.0,
        volume_raw=5_000_000,
        data_quality_status="ok",
        feature_ready=True,
        avg_dollar_volume_20d=50_000_000.0,
        breakout_proximity=None,
        range_duration=None,
        ema200=None,
        pullback_from_recent_high_pct=None,
        ema20=None,
        ema50=None,
        ema_alignment_score=None,
        ema50_slope=None,
        range_tightness_score=None,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Fake DB (offline, no duckdb)
# ---------------------------------------------------------------------------
class _FakeDB:
    def __init__(
        self,
        ticker_rows: list[dict[str, Any]],
        db_role: str = "debug",
        raise_on_write: bool = False,
    ) -> None:
        self._rows = ticker_rows
        self._role = db_role
        self._raise_on_write = raise_on_write
        self.inserted: list[tuple[Any, ...]] = []

    def connect(self, db_role: str, read_only: bool = False) -> "_FakeConn":
        return _FakeConn(self, read_only=read_only, raise_on_write=self._raise_on_write)

    @property
    def db_role(self) -> str:
        return self._role


class _FakeConn:
    def __init__(self, db: _FakeDB, read_only: bool, raise_on_write: bool) -> None:
        self._db = db
        self._read_only = read_only
        self._raise = raise_on_write
        self._in_tx = False

    def execute(self, sql: str, params: list[Any] | None = None) -> "_FakeConn":
        if sql.strip().upper().startswith("BEGIN"):
            self._in_tx = True
            return self
        if sql.strip().upper().startswith("COMMIT"):
            self._in_tx = False
            return self
        if sql.strip().upper().startswith("ROLLBACK"):
            self._in_tx = False
            return self
        if "INSERT INTO step3_candidates" in sql and not self._read_only:
            if self._raise:
                raise RuntimeError("simulated write failure")
            self._db.inserted.append(tuple(params or []))
            return self
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Return rows in the column order expected by _SQL_READ_UNIVERSE."""
        result = []
        for row in self._db._rows:
            result.append((
                row["ticker"],
                row["symbol_type"],
                row["open_raw"],
                row["high_raw"],
                row["low_raw"],
                row["close_raw"],
                row.get("close_adj"),
                row["volume_raw"],
                row["data_quality_status"],
                row["feature_ready"],
                row["avg_dollar_volume_20d"],
                row["breakout_proximity"],
                row["range_duration"],
                row["ema200"],
                row["pullback_from_recent_high_pct"],
                row["ema20"],
                row["ema50"],
                row["ema_alignment_score"],
                row["ema50_slope"],
                row["range_tightness_score"],
            ))
        return result

    def close(self) -> None:
        pass


def _engine(rows: list[dict[str, Any]], raise_on_write: bool = False) -> tuple[Step3UniversalEligibilityEngine, _FakeDB]:
    fake = _FakeDB(rows, raise_on_write=raise_on_write)
    engine = Step3UniversalEligibilityEngine(db_manager=fake)
    return engine, fake


def _run(rows: list[dict[str, Any]], **kwargs) -> tuple[ServiceResult, _FakeDB]:
    eng, fake = _engine(rows)
    result = eng.run(
        signal_date=SIGNAL_DATE,
        db_role="debug",
        run_id=RUN_ID,
        setup_configs=ALL_SETUP_CONFIGS,
        **kwargs,
    )
    return result, fake


# ===========================================================================
# 1. Canonical naming
# ===========================================================================
class TestCanonicalNaming:
    def test_consolidation_base_in_setup_types(self):
        """consolidation_base must be in ALLOWED_SETUP_TYPES (canonical name, not conservative_consolidation)."""
        assert "consolidation_base" in constants.ALLOWED_SETUP_TYPES

    def test_all_four_setup_types_present(self):
        assert set(constants.ALLOWED_SETUP_TYPES) == {
            "breakout", "pullback", "trend_continuation", "consolidation_base"
        }

    def test_setup_configs_use_canonical_names(self):
        for cfg in ALL_SETUP_CONFIGS:
            assert cfg["setup_type"] in constants.ALLOWED_SETUP_TYPES

    def test_module_does_not_reference_conservative_consolidation(self):
        """Source scan: no 'conservative_consolidation' in module source."""
        import app.services.screening.step3_universal_eligibility as mod
        src = inspect.getsource(mod)
        assert "conservative_consolidation" not in src


# ===========================================================================
# 2. ServiceResult / constants compatibility
# ===========================================================================
class TestContractCompatibility:
    def test_service_result_is_ok_method(self):
        """Uses accepted ServiceResult.is_ok() not a custom .ok property."""
        result, _ = _run([_row()])
        assert result.is_ok()

    def test_service_result_status_constants(self):
        from app.utils import service_result as sr_mod
        result, _ = _run([_row()])
        assert result.status == sr_mod.STATUS_SUCCESS

    def test_constants_feature_schema_version(self):
        assert constants.FEATURE_SCHEMA_VERSION == "features_v02"

    def test_allowed_setup_types_from_constants(self):
        for cfg in ALL_SETUP_CONFIGS:
            assert cfg["setup_type"] in constants.ALLOWED_SETUP_TYPES


# ===========================================================================
# 3. Eligibility checks (unit)
# ===========================================================================
class TestEligibilityUnit:
    def test_fully_eligible(self):
        assert _check_eligibility(_row(), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES) == []

    def test_feature_not_ready(self):
        reasons = _check_eligibility(_row(feature_ready=False), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "feature_not_ready" in reasons

    def test_not_stock_etf(self):
        reasons = _check_eligibility(_row(symbol_type="etf"), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "not_stock" in reasons

    def test_not_stock_benchmark(self):
        reasons = _check_eligibility(_row(symbol_type="benchmark"), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "not_stock" in reasons

    def test_price_below_min(self):
        reasons = _check_eligibility(_row(close_raw=4.99), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "price_below_min" in reasons

    def test_price_at_min_passes(self):
        reasons = _check_eligibility(_row(close_raw=5.0), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "price_below_min" not in reasons

    def test_price_none(self):
        r = _row(close_raw=None, data_quality_status="ok")
        reasons = _check_eligibility(r, _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "price_below_min" in reasons

    def test_liquidity_below_min(self):
        reasons = _check_eligibility(_row(avg_dollar_volume_20d=9_999_999.0), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "liquidity_below_min" in reasons

    def test_liquidity_none(self):
        reasons = _check_eligibility(_row(avg_dollar_volume_20d=None), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "liquidity_below_min" in reasons

    def test_data_quality_fail(self):
        reasons = _check_eligibility(_row(data_quality_status="suspect"), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "data_quality_fail" in reasons

    def test_data_quality_warning_fails(self):
        reasons = _check_eligibility(_row(data_quality_status="warning"), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "data_quality_fail" in reasons

    def test_ohlcv_high_below_low(self):
        reasons = _check_eligibility(_row(high_raw=100.0, low_raw=110.0), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "ohlcv_anomaly" in reasons

    def test_ohlcv_close_zero(self):
        reasons = _check_eligibility(_row(close_raw=0.0, open_raw=50.0), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "ohlcv_anomaly" in reasons

    def test_ohlcv_open_negative(self):
        reasons = _check_eligibility(_row(open_raw=-1.0), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "ohlcv_anomaly" in reasons

    def test_ohlcv_volume_negative(self):
        reasons = _check_eligibility(_row(volume_raw=-100), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "ohlcv_anomaly" in reasons

    def test_multiple_rejection_reasons(self):
        reasons = _check_eligibility(
            _row(feature_ready=False, close_raw=1.0, avg_dollar_volume_20d=100.0),
            _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES,
        )
        assert len(reasons) >= 3

    # Fix #4: missing price row → ineligible with no_price_row reason
    def test_no_price_row_ineligible(self):
        r = _row(close_raw=None, data_quality_status=None, open_raw=None,
                 high_raw=None, low_raw=None, volume_raw=None)
        reasons = _check_eligibility(r, _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert "no_price_row" in reasons

    # AD-22.23: RVOL must NOT be a universal hard gate
    def test_rvol_not_in_eligibility_checks(self):
        """RVOL is absent from _check_eligibility — no rvol field on eligibility path."""
        row = _row()  # no rvol field at all
        reasons = _check_eligibility(row, _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert reasons == []

    def test_no_setup_score_gate(self):
        reasons = _check_eligibility(_row(), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert reasons == []

    def test_no_atr_pct_gate(self):
        reasons = _check_eligibility(_row(), _MIN_PRICE, _MIN_ADV, _ALLOWED_TYPES)
        assert reasons == []


# ===========================================================================
# 4. Routing predicates (unit)
# ===========================================================================
class TestRoutingUnit:
    def test_breakout_routed(self):
        routes = _evaluate_routing(_row(breakout_proximity=0.0, range_duration=15))
        assert "breakout" in routes

    def test_breakout_not_routed_below_proximity(self):
        routes = _evaluate_routing(_row(breakout_proximity=-1.5, range_duration=15))
        assert "breakout" not in routes

    def test_breakout_not_routed_short_base(self):
        routes = _evaluate_routing(_row(breakout_proximity=0.0, range_duration=5))
        assert "breakout" not in routes

    def test_pullback_routed(self):
        routes = _evaluate_routing(_row(
            close_adj=155.0, ema200=100.0, pullback_from_recent_high_pct=-0.08,
            ema20=120.0, ema50=110.0,
        ))
        assert "pullback" in routes

    def test_pullback_not_routed_below_ema200(self):
        routes = _evaluate_routing(_row(
            close_adj=90.0, ema200=100.0, pullback_from_recent_high_pct=-0.08,
            ema20=95.0, ema50=92.0,
        ))
        assert "pullback" not in routes

    def test_pullback_not_routed_too_shallow(self):
        routes = _evaluate_routing(_row(
            close_adj=155.0, ema200=100.0, pullback_from_recent_high_pct=-0.01,
            ema20=120.0, ema50=110.0,
        ))
        assert "pullback" not in routes

    def test_pullback_not_routed_too_deep(self):
        routes = _evaluate_routing(_row(
            close_adj=155.0, ema200=100.0, pullback_from_recent_high_pct=-0.25,
            ema20=120.0, ema50=110.0,
        ))
        assert "pullback" not in routes

    def test_pullback_ema20_below_ema50_not_routed(self):
        routes = _evaluate_routing(_row(
            close_adj=155.0, ema200=100.0, pullback_from_recent_high_pct=-0.08,
            ema20=105.0, ema50=110.0,
        ))
        assert "pullback" not in routes

    def test_trend_continuation_routed(self):
        routes = _evaluate_routing(_row(
            ema_alignment_score=100.0, ema50_slope=0.01, close_adj=155.0, ema50=120.0,
        ))
        assert "trend_continuation" in routes

    def test_trend_continuation_low_alignment(self):
        routes = _evaluate_routing(_row(
            ema_alignment_score=30.0, ema50_slope=0.01, close_adj=155.0, ema50=120.0,
        ))
        assert "trend_continuation" not in routes

    def test_trend_continuation_negative_slope(self):
        routes = _evaluate_routing(_row(
            ema_alignment_score=100.0, ema50_slope=-0.01, close_adj=155.0, ema50=120.0,
        ))
        assert "trend_continuation" not in routes

    def test_trend_continuation_price_below_ema50(self):
        routes = _evaluate_routing(_row(
            ema_alignment_score=100.0, ema50_slope=0.01, close_adj=110.0, ema50=120.0,
        ))
        assert "trend_continuation" not in routes

    def test_trend_continuation_slope_zero_not_routed(self):
        routes = _evaluate_routing(_row(
            ema_alignment_score=100.0, ema50_slope=0.0, close_adj=155.0, ema50=120.0,
        ))
        assert "trend_continuation" not in routes

    def test_consolidation_base_routed(self):
        routes = _evaluate_routing(_row(range_tightness_score=70.0, range_duration=20))
        assert "consolidation_base" in routes

    def test_consolidation_base_low_tightness(self):
        routes = _evaluate_routing(_row(range_tightness_score=30.0, range_duration=20))
        assert "consolidation_base" not in routes

    def test_consolidation_base_short_range(self):
        routes = _evaluate_routing(_row(range_tightness_score=70.0, range_duration=5))
        assert "consolidation_base" not in routes

    def test_multi_route(self):
        """A ticker can route to multiple setup types simultaneously."""
        routes = _evaluate_routing(_row(
            breakout_proximity=0.0, range_duration=20,
            ema_alignment_score=100.0, ema50_slope=0.01, close_adj=155.0, ema50=120.0,
            range_tightness_score=70.0,
        ))
        assert "breakout" in routes
        assert "trend_continuation" in routes
        assert "consolidation_base" in routes

    def test_no_route_all_predicates_fail(self):
        routes = _evaluate_routing(_row(
            breakout_proximity=-5.0, range_duration=2,
            close_adj=90.0, ema200=100.0, pullback_from_recent_high_pct=-0.5,
            ema20=85.0, ema50=88.0, ema_alignment_score=10.0, ema50_slope=-0.05,
            range_tightness_score=10.0,
        ))
        assert routes == []

    def test_none_fields_do_not_raise(self):
        routes = _evaluate_routing(_row())  # all routing fields None
        assert isinstance(routes, list)

    def test_each_setup_type_routed_independently(self):
        cases = [
            (dict(breakout_proximity=0.0, range_duration=15), "breakout"),
            (dict(close_adj=155.0, ema200=100.0, pullback_from_recent_high_pct=-0.08,
                  ema20=120.0, ema50=110.0), "pullback"),
            (dict(ema_alignment_score=100.0, ema50_slope=0.01, close_adj=155.0, ema50=120.0),
             "trend_continuation"),
            (dict(range_tightness_score=70.0, range_duration=20), "consolidation_base"),
        ]
        for kwargs, expected in cases:
            routes = _evaluate_routing(_row(**kwargs))
            assert expected in routes, f"Expected {expected} in {routes}"


# ===========================================================================
# 5. Feature snapshot (fix #5)
# ===========================================================================
class TestFeatureSnapshot:
    def test_snapshot_contains_eligibility_fields(self):
        snap = _build_snapshot(_row())
        for field in ("close_raw", "feature_ready", "symbol_type", "data_quality_status",
                      "avg_dollar_volume_20d", "open_raw", "high_raw", "low_raw", "volume_raw"):
            assert field in snap, f"Missing eligibility field: {field}"

    def test_snapshot_contains_routing_fields(self):
        snap = _build_snapshot(_row())
        for field in ("close_adj", "ema200", "ema20", "ema50", "ema_alignment_score",
                      "ema50_slope", "pullback_from_recent_high_pct",
                      "breakout_proximity", "range_duration", "range_tightness_score"):
            assert field in snap, f"Missing routing field: {field}"

    def test_snapshot_json_serialisable(self):
        snap = _build_snapshot(_row(
            breakout_proximity=0.5, range_duration=15, ema50_slope=0.01,
        ))
        json.dumps(snap)  # must not raise

    def test_snapshot_in_inserted_rows(self):
        row = _row(breakout_proximity=0.5, range_duration=15)
        result, fake = _run([row])
        assert result.is_ok()
        inserted = fake.inserted[0]
        # feature_snapshot_json is param index 10 (0-based)
        snap = json.loads(inserted[10])
        assert "close_raw" in snap
        assert "breakout_proximity" in snap


# ===========================================================================
# 6. Engine integration (offline)
# ===========================================================================
class TestEngineOffline:
    def test_happy_path_all_inserted(self):
        rows = [_row(ticker="AAPL"), _row(ticker="MSFT")]
        result, fake = _run(rows)
        assert result.is_ok()
        assert result.rows_processed == 2
        tickers = {r[2] for r in fake.inserted}
        assert tickers == {"AAPL", "MSFT"}

    def test_ineligible_persisted(self):
        rows = [_row(ticker="BAD", feature_ready=False)]
        result, fake = _run(rows)
        assert result.is_ok()
        inserted = fake.inserted[0]
        routing_status = inserted[6]
        assert routing_status == ROUTING_INELIGIBLE

    def test_no_route_persisted(self):
        rows = [_row(ticker="FLAT")]  # all routing fields None
        result, fake = _run(rows)
        assert result.is_ok()
        inserted = fake.inserted[0]
        routing_status = inserted[6]
        assert routing_status == ROUTING_NO_ROUTE

    def test_routed_persisted(self):
        rows = [_row(ticker="BRKT", breakout_proximity=0.0, range_duration=15)]
        result, fake = _run(rows)
        assert result.is_ok()
        inserted = fake.inserted[0]
        routing_status = inserted[6]
        routed_setups = json.loads(inserted[9])
        assert routing_status == ROUTING_ROUTED
        assert "breakout" in routed_setups

    def test_multi_route_persisted(self):
        rows = [_row(
            ticker="MULTI",
            breakout_proximity=0.0, range_duration=20,
            range_tightness_score=70.0,
            ema_alignment_score=100.0, ema50_slope=0.01, close_adj=155.0, ema50=120.0,
        )]
        result, fake = _run(rows)
        assert result.is_ok()
        routed_setups = json.loads(fake.inserted[0][9])
        assert len(routed_setups) >= 2

    def test_empty_universe_success_with_warnings(self):
        result, _ = _run([])
        assert result.status == "success_with_warnings"
        assert result.rows_processed == 0

    def test_simulation_role_returns_failed(self):
        rows = [_row()]
        eng, _ = _engine(rows)
        result = eng.run(
            signal_date=SIGNAL_DATE, db_role="simulation",
            run_id=RUN_ID, setup_configs=ALL_SETUP_CONFIGS,
        )
        assert result.status == "failed"

    def test_config_parity_error_returns_failed(self):
        divergent = [
            ALL_SETUP_CONFIGS[0],
            {**ALL_SETUP_CONFIGS[1], "universe": {**ALL_SETUP_CONFIGS[1]["universe"], "min_price": 99.0}},
        ]
        eng, _ = _engine([_row()])
        result = eng.run(
            signal_date=SIGNAL_DATE, db_role="debug",
            run_id=RUN_ID, setup_configs=divergent,
        )
        assert result.status == "failed"

    def test_metadata_keys_on_success(self):
        result, _ = _run([_row()])
        for key in METADATA_KEYS:
            assert key in result.metadata, f"Missing metadata key: {key}"

    def test_metadata_keys_on_failed(self):
        eng, _ = _engine([_row()])
        result = eng.run(
            signal_date=SIGNAL_DATE, db_role="simulation",
            run_id=RUN_ID, setup_configs=ALL_SETUP_CONFIGS,
        )
        for key in METADATA_KEYS:
            assert key in result.metadata, f"Missing metadata key on failed: {key}"

    def test_diagnostics_counts(self):
        rows = [
            _row(ticker="A", breakout_proximity=0.0, range_duration=15),
            _row(ticker="B", feature_ready=False),
            _row(ticker="C"),  # no_route
        ]
        result, _ = _run(rows)
        m = result.metadata
        assert m["total_evaluated"] == 3
        assert m["ineligible_count"] == 1
        assert m["no_route_count"] == 1
        assert m["routed_count"] == 1
        assert m["routed_by_setup_type"]["breakout"] == 1
        assert m["candidates_written"] == 3

    def test_no_strategy_config_dependency(self):
        for cfg in ALL_SETUP_CONFIGS:
            assert "strategy_config_id" not in cfg
            assert "strategy_name" not in cfg

    def test_old_rvol_gate_not_controlling_routing(self):
        """Ticker with zero rvol (not even a field on TickerRow) still routes."""
        rows = [_row(ticker="LOWRVOL", breakout_proximity=0.0, range_duration=15)]
        result, fake = _run(rows)
        assert result.is_ok()
        inserted = fake.inserted[0]
        assert inserted[6] == ROUTING_ROUTED

    # Fix #4: missing price row
    def test_no_price_row_ticker_is_ineligible(self):
        """Active ticker with no daily_prices row on signal_date → ineligible."""
        rows = [_row(
            ticker="NOPRICE",
            close_raw=None, open_raw=None, high_raw=None, low_raw=None,
            volume_raw=None, data_quality_status=None,
        )]
        result, fake = _run(rows)
        assert result.is_ok()
        inserted = fake.inserted[0]
        assert inserted[6] == ROUTING_INELIGIBLE
        fail_reasons = json.loads(inserted[8])
        assert "no_price_row" in fail_reasons

    # Fix #6: rollback on write error
    def test_write_error_returns_failed_no_partial_rows(self):
        eng, fake = _engine([_row()], raise_on_write=True)
        result = eng.run(
            signal_date=SIGNAL_DATE, db_role="debug",
            run_id=RUN_ID, setup_configs=ALL_SETUP_CONFIGS,
        )
        assert result.status == "failed"
        assert len(fake.inserted) == 0


# ===========================================================================
# 7. Universe config parity (unit)
# ===========================================================================
class TestUniverseParity:
    def test_identical_configs_pass(self):
        ub = _assert_universe_parity(ALL_SETUP_CONFIGS)
        assert ub["min_price"] == 5.0

    def test_divergent_min_price_raises(self):
        bad = {**ALL_SETUP_CONFIGS[1], "universe": {**ALL_SETUP_CONFIGS[1]["universe"], "min_price": 99.0}}
        with pytest.raises(ConfigParityError):
            _assert_universe_parity([ALL_SETUP_CONFIGS[0], bad])

    def test_missing_universe_block_raises(self):
        bad = {"config_id": "x", "setup_type": "breakout"}
        with pytest.raises(MissingConfigError):
            _assert_universe_parity([bad])

    def test_empty_configs_raises(self):
        with pytest.raises(MissingConfigError):
            _assert_universe_parity([])


# ===========================================================================
# 8. Source scan (fix #7)
# ===========================================================================
class TestSourceScan:
    def _src(self) -> str:
        import app.services.screening.step3_universal_eligibility as mod
        return inspect.getsource(mod)

    def test_no_direct_duckdb_import(self):
        src = self._src()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
                module = getattr(node, "module", "") or ""
                assert "duckdb" not in names, "Direct duckdb import found"
                assert module != "duckdb", "Direct duckdb import found"

    def test_no_print_statements(self):
        src = self._src()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "print":
                    pytest.fail("print() found in module source")

    def test_no_ddl(self):
        src = self._src().upper()
        for ddl in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE INDEX"):
            assert ddl not in src, f"DDL keyword '{ddl}' found in module source"

    def test_no_conservative_consolidation(self):
        assert "conservative_consolidation" not in self._src()

    def test_no_strategy_config_id_references(self):
        """No legacy strategy_config_id column used in insert SQL."""
        src = self._src()
        assert "strategy_config_id" not in src


# ===========================================================================
# 9. Schema-level DB integration test (fix #3)
#    Uses accepted duckdb_manager + schema_manager + tmp_path fixtures.
# ===========================================================================
@pytest.fixture()
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect all DB paths into tmp_path (mirrors test_schema_manager.py pattern)."""
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    return {dbm.DB_ROLE_PROD: prod, dbm.DB_ROLE_DEBUG: debug, dbm.DB_ROLE_SIMULATION: simulation}


def _seed_setup_configs(conn: Any, configs: list[dict[str, Any]]) -> None:
    """Seed setup_configs rows for integration test."""
    import hashlib
    for cfg in configs:
        cj = json.dumps(cfg)
        ch = hashlib.md5(cj.encode()).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO setup_configs "
            "(config_id, setup_type, version, config_json, config_hash, active_flag, created_at) "
            "VALUES (?, ?, ?, ?, ?, TRUE, now())",
            [cfg["config_id"], cfg["setup_type"], "v1", cj, ch],
        )


def _seed_ticker(conn: Any, ticker: str, symbol_type: str = "stock") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ticker_master "
        "(ticker, symbol_type, active_flag, delisted_flag) VALUES (?, ?, TRUE, FALSE)",
        [ticker, symbol_type],
    )


def _seed_price(conn: Any, ticker: str, signal_date: date, **overrides) -> None:
    defaults = dict(
        open_raw=148.0, high_raw=152.0, low_raw=147.0, close_raw=150.0,
        close_adj=150.0, volume_raw=5_000_000, data_quality_status="ok",
        source_provider="fake",
    )
    defaults.update(overrides)
    conn.execute(
        "INSERT OR IGNORE INTO daily_prices "
        "(ticker, date, open_raw, high_raw, low_raw, close_raw, close_adj, "
        " volume_raw, data_quality_status, source_provider, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        [
            ticker, signal_date.isoformat(),
            defaults["open_raw"], defaults["high_raw"], defaults["low_raw"],
            defaults["close_raw"], defaults["close_adj"], defaults["volume_raw"],
            defaults["data_quality_status"], defaults["source_provider"],
        ],
    )


def _seed_feature(conn: Any, ticker: str, signal_date: date, **overrides) -> None:
    defaults = dict(
        feature_cutoff_date=signal_date,
        feature_schema_version=constants.FEATURE_SCHEMA_VERSION,
        feature_ready=True,
        avg_dollar_volume_20d=50_000_000.0,
        breakout_proximity=None, range_duration=None, ema200=None,
        pullback_from_recent_high_pct=None, ema20=None, ema50=None,
        ema_alignment_score=None, ema50_slope=None, range_tightness_score=None,
    )
    defaults.update(overrides)
    conn.execute(
        "INSERT OR IGNORE INTO daily_features "
        "(ticker, feature_date, feature_cutoff_date, feature_schema_version, "
        " feature_ready, avg_dollar_volume_20d, "
        " breakout_proximity, range_duration, ema200, pullback_from_recent_high_pct, "
        " ema20, ema50, ema_alignment_score, ema50_slope, range_tightness_score, "
        " calculated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        [
            ticker, signal_date.isoformat(),
            defaults["feature_cutoff_date"].isoformat(),
            defaults["feature_schema_version"],
            defaults["feature_ready"],
            defaults["avg_dollar_volume_20d"],
            defaults["breakout_proximity"], defaults["range_duration"],
            defaults["ema200"], defaults["pullback_from_recent_high_pct"],
            defaults["ema20"], defaults["ema50"],
            defaults["ema_alignment_score"], defaults["ema50_slope"],
            defaults["range_tightness_score"],
        ],
    )


class TestSchemaIntegration:
    """
    Integration tests using real DuckDB via duckdb_manager + schema_manager.
    Uses the accepted tmp_db_paths fixture pattern from test_schema_manager.py.
    """

    @pytest.fixture(autouse=True)
    def _setup_db(self, tmp_db_paths: dict[str, Path]) -> None:
        """Apply schema to debug DB before each integration test."""
        sm.apply_schema("debug")
        self._role = "debug"

    def _conn(self) -> Any:
        return dbm.connect(self._role)

    def test_schema_has_step3_candidates_table(self) -> None:
        conn = dbm.connect(self._role, read_only=True)
        try:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = 'step3_candidates'"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "step3_candidates table not found"

    def test_step3_candidates_columns(self) -> None:
        conn = dbm.connect(self._role, read_only=True)
        try:
            cols = {
                r[0] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'step3_candidates'"
                ).fetchall()
            }
        finally:
            conn.close()
        required = {
            "candidate_id", "run_id", "ticker", "signal_date",
            "eligibility_score", "passed_eligibility", "routing_status",
            "routing_fail_reason", "eligibility_fail_reasons",
            "routed_setup_types", "feature_snapshot_json", "created_at",
        }
        assert required <= cols

    def test_engine_inserts_into_real_schema(self, tmp_db_paths: dict[str, Path]) -> None:
        """Happy path: eligible + routed ticker inserted into real DuckDB."""
        conn = self._conn()
        try:
            _seed_setup_configs(conn, ALL_SETUP_CONFIGS)
            _seed_ticker(conn, "AAPL")
            _seed_price(conn, "AAPL", SIGNAL_DATE)
            _seed_feature(conn, "AAPL", SIGNAL_DATE,
                          breakout_proximity=0.0, range_duration=15)
            conn.commit()
        finally:
            conn.close()

        engine = Step3UniversalEligibilityEngine()
        result = engine.run(
            signal_date=SIGNAL_DATE,
            db_role=self._role,
            run_id=RUN_ID,
            setup_configs=ALL_SETUP_CONFIGS,
        )

        assert result.is_ok(), result.errors
        assert result.rows_processed == 1

        conn = dbm.connect(self._role, read_only=True)
        try:
            rows = conn.execute(
                "SELECT ticker, routing_status, passed_eligibility, "
                "routed_setup_types, feature_snapshot_json "
                "FROM step3_candidates WHERE run_id = ?",
                [RUN_ID],
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        ticker, status, passed, routes_raw, snap_raw = rows[0]
        assert ticker == "AAPL"
        assert status == ROUTING_ROUTED
        assert passed is True
        routes = json.loads(routes_raw)
        assert "breakout" in routes
        snap = json.loads(snap_raw)
        assert "close_raw" in snap
        assert "breakout_proximity" in snap

    def test_ineligible_ticker_inserted(self, tmp_db_paths: dict[str, Path]) -> None:
        """Ticker failing eligibility is persisted as ineligible."""
        conn = self._conn()
        try:
            _seed_setup_configs(conn, ALL_SETUP_CONFIGS)
            _seed_ticker(conn, "BAD")
            _seed_price(conn, "BAD", SIGNAL_DATE, data_quality_status="suspect")
            _seed_feature(conn, "BAD", SIGNAL_DATE, feature_ready=False)
            conn.commit()
        finally:
            conn.close()

        engine = Step3UniversalEligibilityEngine()
        result = engine.run(
            signal_date=SIGNAL_DATE, db_role=self._role,
            run_id=RUN_ID, setup_configs=ALL_SETUP_CONFIGS,
        )
        assert result.is_ok()

        conn = dbm.connect(self._role, read_only=True)
        try:
            rows = conn.execute(
                "SELECT routing_status, passed_eligibility, eligibility_fail_reasons "
                "FROM step3_candidates WHERE ticker = 'BAD' AND run_id = ?",
                [RUN_ID],
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        status, passed, fail_raw = rows[0]
        assert status == ROUTING_INELIGIBLE
        assert passed is False
        fail_reasons = json.loads(fail_raw)
        assert len(fail_reasons) > 0

    def test_no_price_row_ticker_ineligible_in_real_db(self, tmp_db_paths: dict[str, Path]) -> None:
        """Fix #4: Active ticker with no price row on signal_date → ineligible."""
        conn = self._conn()
        try:
            _seed_setup_configs(conn, ALL_SETUP_CONFIGS)
            _seed_ticker(conn, "NOPRICE")
            # Intentionally NO daily_prices row seeded for NOPRICE
            _seed_feature(conn, "NOPRICE", SIGNAL_DATE)
            conn.commit()
        finally:
            conn.close()

        engine = Step3UniversalEligibilityEngine()
        result = engine.run(
            signal_date=SIGNAL_DATE, db_role=self._role,
            run_id=RUN_ID, setup_configs=ALL_SETUP_CONFIGS,
        )
        assert result.is_ok()

        conn = dbm.connect(self._role, read_only=True)
        try:
            rows = conn.execute(
                "SELECT routing_status, passed_eligibility, eligibility_fail_reasons "
                "FROM step3_candidates WHERE ticker = 'NOPRICE' AND run_id = ?",
                [RUN_ID],
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        status, passed, fail_raw = rows[0]
        assert status == ROUTING_INELIGIBLE
        assert passed is False
        reasons = json.loads(fail_raw)
        assert "no_price_row" in reasons

    def test_idempotency_second_run_inserts_new_candidates(self, tmp_db_paths: dict[str, Path]) -> None:
        """
        Fix #6: Re-running Step 3 for the same signal_date+ticker inserts a
        second candidate row (different candidate_id / run_id). Step 3 never
        deletes or updates existing rows — the pipeline orchestrator guards
        against double-runs via pipeline_runs; Step 3 itself is append-only.
        """
        conn = self._conn()
        try:
            _seed_setup_configs(conn, ALL_SETUP_CONFIGS)
            _seed_ticker(conn, "AAPL")
            _seed_price(conn, "AAPL", SIGNAL_DATE)
            _seed_feature(conn, "AAPL", SIGNAL_DATE,
                          breakout_proximity=0.0, range_duration=15)
            conn.commit()
        finally:
            conn.close()

        engine = Step3UniversalEligibilityEngine()
        run_id_1 = str(uuid.uuid4())
        run_id_2 = str(uuid.uuid4())

        result1 = engine.run(
            signal_date=SIGNAL_DATE, db_role=self._role,
            run_id=run_id_1, setup_configs=ALL_SETUP_CONFIGS,
        )
        result2 = engine.run(
            signal_date=SIGNAL_DATE, db_role=self._role,
            run_id=run_id_2, setup_configs=ALL_SETUP_CONFIGS,
        )

        assert result1.is_ok()
        assert result2.is_ok()

        conn = dbm.connect(self._role, read_only=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM step3_candidates WHERE ticker = 'AAPL'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert count == 2, "Each run should insert its own candidate row"

    def test_routing_status_not_null_constraint(self, tmp_db_paths: dict[str, Path]) -> None:
        """routing_status NOT NULL is enforced — verify column exists as NOT NULL."""
        conn = dbm.connect(self._role, read_only=True)
        try:
            rows = conn.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'step3_candidates' AND column_name = 'routing_status'"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "routing_status column not found"
        is_nullable = rows[0][0]
        assert is_nullable in ("NO", "no", False, 0), (
            f"routing_status should be NOT NULL, got is_nullable={is_nullable!r}"
        )

    def test_multiple_tickers_all_persisted(self, tmp_db_paths: dict[str, Path]) -> None:
        """Three tickers, three different outcomes — all persisted correctly."""
        conn = self._conn()
        try:
            _seed_setup_configs(conn, ALL_SETUP_CONFIGS)
            # Routed
            _seed_ticker(conn, "BRKT")
            _seed_price(conn, "BRKT", SIGNAL_DATE)
            _seed_feature(conn, "BRKT", SIGNAL_DATE, breakout_proximity=0.0, range_duration=15)
            # No-route
            _seed_ticker(conn, "FLAT")
            _seed_price(conn, "FLAT", SIGNAL_DATE)
            _seed_feature(conn, "FLAT", SIGNAL_DATE)
            # Ineligible
            _seed_ticker(conn, "BAD")
            _seed_price(conn, "BAD", SIGNAL_DATE, close_raw=1.0)
            _seed_feature(conn, "BAD", SIGNAL_DATE)
            conn.commit()
        finally:
            conn.close()

        engine = Step3UniversalEligibilityEngine()
        result = engine.run(
            signal_date=SIGNAL_DATE, db_role=self._role,
            run_id=RUN_ID, setup_configs=ALL_SETUP_CONFIGS,
        )
        assert result.is_ok()
        assert result.rows_processed == 3
        assert result.metadata["routed_count"] == 1
        assert result.metadata["no_route_count"] == 1
        assert result.metadata["ineligible_count"] == 1
