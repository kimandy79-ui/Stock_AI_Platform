"""Phase 6 tests — Setup-mode funnel diagnostics (M20/M22).

Tests verify:
  - SetupModeFunnelDiagnosticsService writes the required metric rows
  - Metric names use setup names (breakout/pullback/trend_continuation/consolidation_base)
  - 'conservative_consolidation' never appears in metric names
  - All required Phase 6 diagnostic metrics are present
  - db_role validation
  - Empty pipeline data returns success (no metrics = warning)
  - read() helper returns structured rows
  - Old strategy-mode terms not in diagnostic output
  - Legacy FunnelDiagnosticsService alias resolves to setup-mode class
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from app.services.diagnostics.funnel_diagnostics import (
    SetupModeFunnelDiagnosticsService,
    FunnelDiagnosticsService,
    ACTIVE_SETUP_TYPES,
    _normalize_validation_reason,
)
from app.utils.service_result import ServiceResult

SIG_DATE = date(2026, 6, 15)
RUN_ID = "test-run-001"

# --------------------------------------------------------------------------- #
# Minimal schema DDL needed for diagnostics tests
# --------------------------------------------------------------------------- #
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS step3_candidates (
    candidate_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    eligibility_score DOUBLE,
    passed_eligibility BOOLEAN NOT NULL,
    routing_status VARCHAR NOT NULL,
    routing_fail_reason VARCHAR,
    eligibility_fail_reasons JSON,
    routed_setup_types JSON,
    feature_snapshot_json JSON,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS step4_analysis (
    analysis_id VARCHAR PRIMARY KEY,
    candidate_id VARCHAR NOT NULL,
    run_id VARCHAR NOT NULL,
    setup_config_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    setup_type VARCHAR NOT NULL,
    setup_score DOUBLE,
    setup_passed BOOLEAN NOT NULL,
    setup_reasons JSON,
    setup_fail_reason VARCHAR,
    entry_price_raw DOUBLE,
    stop_price_raw DOUBLE,
    target_price_raw DOUBLE,
    estimated_rr DOUBLE,
    target_is_structural BOOLEAN,
    stop_distance_pct DOUBLE,
    support_level DOUBLE,
    resistance_level DOUBLE,
    next_resistance_level DOUBLE,
    atr_pct DOUBLE,
    distance_to_ema20_pct DOUBLE,
    distance_to_ema50_pct DOUBLE,
    rvol DOUBLE,
    earnings_days INTEGER,
    market_regime VARCHAR,
    earnings_penalty DOUBLE,
    macro_penalty DOUBLE,
    explanation_json JSON,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS step5_proposals (
    proposal_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    setup_config_id VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    setup_type VARCHAR NOT NULL,
    setup_score DOUBLE,
    risk_score DOUBLE,
    risk_label VARCHAR,
    risk_reasons JSON,
    disposition VARCHAR NOT NULL,
    entry_price_raw DOUBLE,
    stop_price_raw DOUBLE,
    target_price_raw DOUBLE,
    estimated_rr DOUBLE,
    target_is_structural BOOLEAN,
    support_level DOUBLE,
    resistance_level DOUBLE,
    next_resistance_level DOUBLE,
    earnings_days INTEGER,
    market_regime VARCHAR,
    proposal_score_raw DOUBLE,
    diversity_penalty DOUBLE,
    proposal_score_final DOUBLE,
    raw_rank INTEGER,
    diversified_rank INTEGER,
    in_raw_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    in_diversified_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    diversification_applied BOOLEAN NOT NULL DEFAULT TRUE,
    selected_top_n BOOLEAN NOT NULL DEFAULT FALSE,
    selected_flag BOOLEAN NOT NULL DEFAULT FALSE,
    ai_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    executed_flag BOOLEAN NOT NULL DEFAULT FALSE,
    rejection_reason VARCHAR,
    setup_reasons JSON,
    mechanical_explanation TEXT,
    sector_count_at_selection INTEGER,
    industry_count_at_selection INTEGER,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_run_diagnostics (
    diag_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    signal_date DATE NOT NULL,
    db_role VARCHAR NOT NULL,
    step_name VARCHAR NOT NULL,
    setup_type VARCHAR,
    metric_name VARCHAR NOT NULL,
    metric_value DOUBLE,
    reason VARCHAR,
    metadata_json JSON,
    created_at TIMESTAMP NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# FakeDbManager backed by an in-process DuckDB
# --------------------------------------------------------------------------- #
class FakeDbManager:
    def __init__(self, path: str = ":memory:"):
        self._conn = duckdb.connect(path)
        self._conn.execute(_SCHEMA_DDL)

    def connect(self, db_role: str, read_only: bool = False):
        return _FakeConn(self._conn)

    def close(self):
        self._conn.close()


class _FakeConn:
    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, params=None):
        if params is None:
            return self._c.execute(sql)
        return self._c.execute(sql, params)

    def close(self):
        pass  # shared in-process connection — don't close


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _insert_s3(db: FakeDbManager, ticker: str, passed: bool, routing_status: str,
               routed_types: list[str] | None = None, fail_reasons: list[str] | None = None,
               feature_ready: bool = True):
    db._conn.execute(
        "INSERT INTO step3_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            str(uuid.uuid4()), RUN_ID, ticker, SIG_DATE.isoformat(),
            0.5, passed, routing_status, None,
            json.dumps(fail_reasons or []),
            json.dumps(routed_types or []),
            json.dumps({"feature_ready": feature_ready}),
            datetime.now().isoformat(),
        ],
    )


def _insert_s4(db: FakeDbManager, ticker: str, setup_type: str, passed: bool,
               fail_reason: str | None = None):
    db._conn.execute(
        "INSERT INTO step4_analysis (analysis_id, candidate_id, run_id, setup_config_id, "
        "ticker, signal_date, setup_type, setup_score, setup_passed, setup_fail_reason, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            str(uuid.uuid4()), str(uuid.uuid4()), RUN_ID, f"setup_{setup_type}_v1",
            ticker, SIG_DATE.isoformat(), setup_type, 70.0, passed,
            fail_reason, datetime.now().isoformat(),
        ],
    )


def _insert_s5(db: FakeDbManager, ticker: str, setup_type: str,
               risk_label: str, disposition: str):
    db._conn.execute(
        "INSERT INTO step5_proposals (proposal_id, run_id, setup_config_id, ticker, "
        "signal_date, setup_type, disposition, in_raw_top_n, in_diversified_top_n, "
        "diversification_applied, selected_top_n, selected_flag, ai_reviewed, "
        "executed_flag, risk_label, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            str(uuid.uuid4()), RUN_ID, f"setup_{setup_type}_v1", ticker,
            SIG_DATE.isoformat(), setup_type, disposition,
            True, True, True, True, True, False, False,
            risk_label, datetime.now().isoformat(),
        ],
    )


def build_svc(db: FakeDbManager) -> SetupModeFunnelDiagnosticsService:
    return SetupModeFunnelDiagnosticsService(db_manager=db)


# --------------------------------------------------------------------------- #
# Active setup types
# --------------------------------------------------------------------------- #
def test_active_setup_types_are_canonical():
    assert set(ACTIVE_SETUP_TYPES) == {
        "breakout", "pullback", "trend_continuation", "consolidation_base"
    }


def test_conservative_consolidation_not_in_active_types():
    assert "conservative_consolidation" not in ACTIVE_SETUP_TYPES


# --------------------------------------------------------------------------- #
# db_role validation
# --------------------------------------------------------------------------- #
def test_invalid_db_role_returns_failed():
    db = FakeDbManager()
    svc = build_svc(db)
    result = svc.run(signal_date=SIG_DATE, db_role="simulation", run_id=RUN_ID)
    assert result.status == "failed"
    assert result.errors


def test_valid_prod_role_accepted():
    db = FakeDbManager()
    svc = build_svc(db)
    result = svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    assert result.status in ("success", "success_with_warnings")


def test_valid_debug_role_accepted():
    db = FakeDbManager()
    svc = build_svc(db)
    result = svc.run(signal_date=SIG_DATE, db_role="debug", run_id=RUN_ID)
    assert result.status in ("success", "success_with_warnings")


# --------------------------------------------------------------------------- #
# Empty pipeline data
# --------------------------------------------------------------------------- #
def test_empty_pipeline_returns_success():
    """Empty pipeline (no rows for this run_id) still succeeds.
    Zero-value metrics (risk_label.low=0, etc.) are valid and written.
    """
    db = FakeDbManager()
    svc = build_svc(db)
    result = svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    assert result.status in ("success", "success_with_warnings")


# --------------------------------------------------------------------------- #
# Eligibility metrics
# --------------------------------------------------------------------------- #
def test_eligibility_total_input_metric():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    _insert_s3(db, "MSFT", False, "ineligible", fail_reasons=["price_below_min"])
    svc = build_svc(db)
    result = svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    assert result.status in ("success", "success_with_warnings")
    rows = svc.read(RUN_ID, SIG_DATE, db_role="prod")
    total_row = next((r for r in rows if r["metric_name"] == "eligibility.total_input"), None)
    assert total_row is not None, "eligibility.total_input must be present"
    assert total_row["metric_value"] == 2.0


def test_eligibility_passed_metric():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    _insert_s3(db, "MSFT", False, "ineligible", fail_reasons=["price_below_min"])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    passed_row = next((r for r in rows if r["metric_name"] == "eligibility.passed"), None)
    assert passed_row is not None
    assert passed_row["metric_value"] == 1.0


def test_eligibility_failed_metric():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    _insert_s3(db, "GOOG", False, "ineligible", fail_reasons=["price_below_min"])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    failed_row = next((r for r in rows if r["metric_name"] == "eligibility.failed"), None)
    assert failed_row is not None
    assert failed_row["metric_value"] == 1.0


def test_eligibility_rejection_reason_metric():
    db = FakeDbManager()
    _insert_s3(db, "T1", False, "ineligible", fail_reasons=["price_below_min"])
    _insert_s3(db, "T2", False, "ineligible", fail_reasons=["price_below_min", "data_quality_fail"])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    price_row = next(
        (r for r in rows if r["metric_name"] == "eligibility.rejection_reason.price_below_min"), None
    )
    assert price_row is not None
    assert price_row["metric_value"] == 2.0


# --------------------------------------------------------------------------- #
# Routing metrics
# --------------------------------------------------------------------------- #
def test_routing_not_routed_metric():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    _insert_s3(db, "MSFT", True, "no_route", [])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    not_routed = next((r for r in rows if r["metric_name"] == "routing.not_routed"), None)
    assert not_routed is not None
    assert not_routed["metric_value"] == 1.0


def test_routing_counts_by_setup_type():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    _insert_s3(db, "MSFT", True, "routed", ["breakout", "trend_continuation"])
    _insert_s3(db, "NVDA", True, "routed", ["pullback"])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    # Check breakout routing count
    bo_row = next(
        (r for r in rows if r["metric_name"] == "routing.routed" and r["setup_type"] == "breakout"), None
    )
    assert bo_row is not None
    assert bo_row["metric_value"] == 2.0


def test_routing_consolidation_base_named_correctly():
    """Must be 'consolidation_base' not 'conservative_consolidation'."""
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["consolidation_base"])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    cb_row = next(
        (r for r in rows if r["setup_type"] == "consolidation_base" and r["metric_name"] == "routing.routed"),
        None
    )
    assert cb_row is not None, "consolidation_base routing count must be present"
    # Must NOT have conservative_consolidation
    for r in rows:
        assert r["setup_type"] != "conservative_consolidation", \
            "conservative_consolidation must not appear in diagnostic output"


# --------------------------------------------------------------------------- #
# Validation metrics
# --------------------------------------------------------------------------- #
def test_validation_pass_fail_by_setup_type():
    db = FakeDbManager()
    _insert_s4(db, "AAPL", "breakout", True)
    _insert_s4(db, "MSFT", "breakout", False, fail_reason="rvol_too_low")
    _insert_s4(db, "NVDA", "pullback", True)
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)

    bo_pass = next(
        (r for r in rows if r["metric_name"] == "validation.passed" and r["setup_type"] == "breakout"), None
    )
    bo_fail = next(
        (r for r in rows if r["metric_name"] == "validation.failed" and r["setup_type"] == "breakout"), None
    )
    pu_pass = next(
        (r for r in rows if r["metric_name"] == "validation.passed" and r["setup_type"] == "pullback"), None
    )
    assert bo_pass is not None and bo_pass["metric_value"] == 1.0
    assert bo_fail is not None and bo_fail["metric_value"] == 1.0
    assert pu_pass is not None and pu_pass["metric_value"] == 1.0


def test_validation_failure_reasons_by_setup_type():
    db = FakeDbManager()
    _insert_s4(db, "AAPL", "breakout", False, "rvol_too_low")
    _insert_s4(db, "MSFT", "breakout", False, "rvol_too_low")
    _insert_s4(db, "NVDA", "breakout", False, "stop_distance_too_large")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    rvol_row = next(
        (r for r in rows
         if r["metric_name"] == "validation.failure_reason.rvol_too_low"
         and r["setup_type"] == "breakout"),
        None
    )
    assert rvol_row is not None
    assert rvol_row["metric_value"] == 2.0


# --------------------------------------------------------------------------- #
# Risk label and disposition metrics
# --------------------------------------------------------------------------- #
def test_risk_label_counts_present():
    db = FakeDbManager()
    _insert_s5(db, "AAPL", "breakout", "low", "BUY")
    _insert_s5(db, "MSFT", "pullback", "medium", "BUY")
    _insert_s5(db, "NVDA", "trend_continuation", "high", "WATCHLIST_ONLY")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)

    for label in ("low", "medium", "high"):
        rl_row = next((r for r in rows if r["metric_name"] == f"risk_label.{label}"), None)
        assert rl_row is not None, f"risk_label.{label} must be present"

    low = next(r for r in rows if r["metric_name"] == "risk_label.low")
    med = next(r for r in rows if r["metric_name"] == "risk_label.medium")
    high = next(r for r in rows if r["metric_name"] == "risk_label.high")
    assert low["metric_value"] == 1.0
    assert med["metric_value"] == 1.0
    assert high["metric_value"] == 1.0


def test_buy_eligible_count_metric():
    db = FakeDbManager()
    _insert_s5(db, "AAPL", "breakout", "low", "BUY")
    _insert_s5(db, "MSFT", "pullback", "medium", "BUY")
    _insert_s5(db, "NVDA", "trend_continuation", "high", "WATCHLIST_ONLY")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    buy_row = next((r for r in rows if r["metric_name"] == "proposal.buy_eligible"), None)
    assert buy_row is not None
    assert buy_row["metric_value"] == 2.0


def test_watchlist_count_metric():
    db = FakeDbManager()
    _insert_s5(db, "AAPL", "breakout", "low", "BUY")
    _insert_s5(db, "MSFT", "pullback", "high", "WATCHLIST_ONLY")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    wl_row = next((r for r in rows if r["metric_name"] == "proposal.watchlist"), None)
    assert wl_row is not None
    assert wl_row["metric_value"] == 1.0


def test_rejected_count_metric():
    db = FakeDbManager()
    _insert_s5(db, "AAPL", "breakout", "high", "REJECTED")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    rej_row = next((r for r in rows if r["metric_name"] == "proposal.rejected"), None)
    assert rej_row is not None
    assert rej_row["metric_value"] == 1.0


def test_final_proposal_count_metric():
    db = FakeDbManager()
    _insert_s5(db, "AAPL", "breakout", "low", "BUY")
    _insert_s5(db, "MSFT", "pullback", "medium", "WATCHLIST_ONLY")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    fc_row = next((r for r in rows if r["metric_name"] == "proposal.final_count"), None)
    assert fc_row is not None
    assert fc_row["metric_value"] == 2.0


# --------------------------------------------------------------------------- #
# Required minimum metrics checklist (Phase 6 spec)
# --------------------------------------------------------------------------- #
def test_all_required_diagnostic_metrics_present():
    """Verify every required metric from the Phase 6 prompt is present."""
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    _insert_s3(db, "MSFT", False, "ineligible", fail_reasons=["price_below_min"])
    _insert_s3(db, "GOOG", True, "no_route", [])
    _insert_s4(db, "AAPL", "breakout", True)
    _insert_s5(db, "AAPL", "breakout", "low", "BUY")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    metric_names = {r["metric_name"] for r in rows}

    required = {
        "eligibility.total_input",
        "eligibility.passed",
        "eligibility.failed",
        "routing.not_routed",
        "risk_label.low",
        "risk_label.medium",
        "risk_label.high",
        "proposal.buy_eligible",
        "proposal.watchlist",
        "proposal.rejected",
        "proposal.final_count",
    }
    # routing.routed must exist for at least one setup type
    has_routing_routed = any(n == "routing.routed" for n in metric_names)
    # validation.passed must exist for at least one setup type
    has_validation = any(n in ("validation.passed", "validation.failed") for n in metric_names)

    missing = required - metric_names
    assert not missing, f"Required diagnostic metrics missing: {missing}"
    assert has_routing_routed, "routing.routed (per setup_type) must be present"
    assert has_validation, "validation.passed or validation.failed must be present"


# --------------------------------------------------------------------------- #
# Metric rows structure
# --------------------------------------------------------------------------- #
def test_diagnostic_rows_have_required_columns():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    assert rows, "must have at least one row"
    required_keys = {"step_name", "setup_type", "metric_name", "metric_value", "reason", "metadata_json"}
    for r in rows:
        assert required_keys <= set(r.keys()), f"Row missing keys: {required_keys - set(r.keys())}"


def test_no_old_strategy_terms_in_metric_names():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    _insert_s4(db, "AAPL", "breakout", True)
    _insert_s5(db, "AAPL", "breakout", "low", "BUY")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    for r in rows:
        name = (r["metric_name"] or "").lower()
        stype = (r["setup_type"] or "").lower()
        for bad in ("aggressive", "normal", "conservative", "strategy_config_id"):
            assert bad not in name, f"Old strategy term '{bad}' in metric_name: {r['metric_name']}"
            assert bad not in stype, f"Old strategy term '{bad}' in setup_type: {r['setup_type']}"


# --------------------------------------------------------------------------- #
# Step4 validation reason normalization (issue 2 cleanup)
# --------------------------------------------------------------------------- #

def test_normalize_validation_reason_strips_numeric_suffix() -> None:
    """Numeric (value<threshold) suffix must be stripped from the reason key."""
    key, example = _normalize_validation_reason("rvol_below_hard_threshold(0.91<1.5)")
    assert key == "rvol_below_hard_threshold"
    assert example == "0.91<1.5"


def test_normalize_validation_reason_no_parens() -> None:
    """Plain reason without parens returns key=reason, example=None."""
    key, example = _normalize_validation_reason("stop_too_wide")
    assert key == "stop_too_wide"
    assert example is None


def test_normalize_validation_reason_none_input() -> None:
    key, example = _normalize_validation_reason(None)
    assert key == "unknown"
    assert example is None


def test_normalize_validation_reason_range_wide() -> None:
    key, example = _normalize_validation_reason("range_too_wide(53.0<60.0)")
    assert key == "range_too_wide"
    assert example == "53.0<60.0"


def test_step4_metric_name_contains_no_numerics() -> None:
    """validation.failure_reason.* metric_names must not contain digits after the dot."""
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    # Seed a step4 failure with a numeric-containing reason
    _insert_s4(db, "AAPL", "breakout", False,
               fail_reason="rvol_below_hard_threshold(0.91<1.5)")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    fail_rows = [r for r in rows if "validation.failure_reason" in (r["metric_name"] or "")]
    assert fail_rows, "expected at least one validation.failure_reason row"
    for r in fail_rows:
        name = r["metric_name"] or ""
        # Extract the part after the last dot
        suffix = name.split(".")[-1]
        assert not any(ch.isdigit() for ch in suffix), (
            f"Numeric digit in metric_name suffix: '{name}'"
        )


def test_step4_numeric_example_in_metadata_json() -> None:
    """Numeric values must appear in metadata_json.examples, not in metric_name."""
    db = FakeDbManager()
    _insert_s3(db, "MSFT", True, "routed", ["pullback"])
    _insert_s4(db, "MSFT", "pullback", False,
               fail_reason="range_too_wide(53.0<60.0)")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    fail_rows = [r for r in rows if "validation.failure_reason.range_too_wide" in (r["metric_name"] or "")]
    assert fail_rows, "expected range_too_wide row"
    meta_raw = fail_rows[0].get("metadata_json")
    import json as _json
    meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
    assert "examples" in meta, "metadata_json must contain 'examples' key"
    assert "53.0<60.0" in meta["examples"], "example value must appear in metadata_json.examples"


def test_step4_plain_reason_no_metadata() -> None:
    """A plain reason with no numeric suffix produces no metadata_json examples."""
    db = FakeDbManager()
    _insert_s3(db, "XOM", True, "routed", ["consolidation_base"])
    _insert_s4(db, "XOM", "consolidation_base", False, fail_reason="stop_too_wide")
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    fail_rows = [r for r in rows if "validation.failure_reason.stop_too_wide" in (r["metric_name"] or "")]
    assert fail_rows
    # metadata_json may be None or missing examples key when no numeric suffix
    meta_raw = fail_rows[0].get("metadata_json")
    if meta_raw:
        import json as _json
        meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        examples = meta.get("examples", [])
        assert examples == [], f"expected no examples for plain reason, got: {examples}"


def test_rows_processed_equals_metrics_written():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    svc = build_svc(db)
    result = svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    rows = svc.read(RUN_ID, SIG_DATE)
    assert result.rows_processed == len(rows)


# --------------------------------------------------------------------------- #
# Legacy alias
# --------------------------------------------------------------------------- #
def test_legacy_alias_resolves_to_setup_mode_class():
    assert FunnelDiagnosticsService is SetupModeFunnelDiagnosticsService


# --------------------------------------------------------------------------- #
# Schema: pipeline_run_diagnostics table columns
# --------------------------------------------------------------------------- #
def test_pipeline_run_diagnostics_schema():
    db = FakeDbManager()
    _insert_s3(db, "AAPL", True, "routed", ["breakout"])
    svc = build_svc(db)
    svc.run(signal_date=SIG_DATE, db_role="prod", run_id=RUN_ID)
    # Verify actual schema columns
    cols_result = db._conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'pipeline_run_diagnostics'"
    ).fetchall()
    col_names = {r[0] for r in cols_result}
    required = {
        "diag_id", "run_id", "signal_date", "db_role", "step_name",
        "setup_type", "metric_name", "metric_value", "reason", "metadata_json", "created_at"
    }
    assert required <= col_names, f"Missing schema columns: {required - col_names}"
