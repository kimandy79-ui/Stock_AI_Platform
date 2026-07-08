"""Tests for Module 23 — Config Recommender (learning layer).

Layered like the M17 suite: pure guardrail/diff/hit-rate helpers are tested
offline; full aggregation-through-write behavior is tested against real tmp
DuckDB prod + simulation databases (module 03 schema).
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.services.learning import config_recommender as cr
from app.services.learning.config_recommender import (
    ConfigRecommenderService,
    build_parameter_diff,
    evaluate_candidate,
)
from app.utils import service_result

MODULE_SRC = inspect.getsource(cr)

INCUMBENT = "setup_breakout_v1"
CANDIDATE = "setup_breakout_canonical"


# --------------------------------------------------------------------------- #
# Pure helpers (offline).
# --------------------------------------------------------------------------- #
def test_evaluate_candidate_below_sample_floor_never_qualifies() -> None:
    result = evaluate_candidate(
        candidate_resolved=[1.0] * 10,
        incumbent_resolved=[0.1] * 40,
        candidate_expectancy=1.0,
        incumbent_expectancy=0.1,
        sample_floor=30,
    )
    assert result["meets_sample_floor"] is False
    assert result["qualified"] is False
    assert result["margin_required"] is None


def test_evaluate_candidate_above_floor_below_margin_does_not_qualify() -> None:
    # Same mean, so margin_achieved == 0 -- can never clear a positive margin.
    values = [0.30, 0.31, 0.29, 0.30, 0.32, 0.28] * 6  # 36 values
    result = evaluate_candidate(
        candidate_resolved=values,
        incumbent_resolved=values,
        candidate_expectancy=sum(values) / len(values),
        incumbent_expectancy=sum(values) / len(values),
        sample_floor=30,
        margin_k=1.0,
    )
    assert result["meets_sample_floor"] is True
    assert result["margin_achieved"] == pytest.approx(0.0)
    assert result["qualified"] is False


def test_evaluate_candidate_above_floor_and_margin_qualifies() -> None:
    low_variance_high = [2.0] * 40
    low_variance_low = [0.2] * 40
    result = evaluate_candidate(
        candidate_resolved=low_variance_high,
        incumbent_resolved=low_variance_low,
        candidate_expectancy=2.0,
        incumbent_expectancy=0.2,
        sample_floor=30,
        margin_k=1.0,
    )
    assert result["meets_sample_floor"] is True
    assert result["qualified"] is True
    assert result["margin_achieved"] == pytest.approx(1.8)


def test_evaluate_candidate_is_pairwise_not_pooled() -> None:
    """Two independent candidate evaluations against the same incumbent must
    not influence each other (each call is a self-contained pairwise test).

    Uses data with realistic spread (not constants) -- a zero-variance series
    has pooled_se == 0, which would make any positive difference trivially
    "qualify" regardless of size and defeat the point of this test.
    """
    incumbent = ([0.05, 0.15, 0.08, 0.12] * 10)  # mean 0.10, real spread
    strong = ([2.8, 3.2, 2.9, 3.1] * 10)  # mean 3.0, clearly separated
    weak = ([0.052, 0.152, 0.082, 0.122] * 10)  # mean 0.102, diff 0.002 << pooled_se
    inc_mean = sum(incumbent) / len(incumbent)
    r_strong = evaluate_candidate(strong, incumbent, sum(strong) / len(strong), inc_mean, sample_floor=30)
    r_weak = evaluate_candidate(weak, incumbent, sum(weak) / len(weak), inc_mean, sample_floor=30)
    assert r_strong["qualified"] is True
    assert r_weak["qualified"] is False


def test_build_parameter_diff_only_differing_keys() -> None:
    incumbent_cfg = {"validation": {"min_rvol_breakout": 1.5, "min_base_duration": 10}}
    candidate_cfg = {"validation": {"min_rvol_breakout": 2.0, "min_base_duration": 10}}
    diff = build_parameter_diff(incumbent_cfg, candidate_cfg)
    assert diff == [{"parameter": "min_rvol_breakout", "current_value": 1.5, "proposed_value": 2.0}]


def test_build_parameter_diff_handles_missing_config_json() -> None:
    assert build_parameter_diff(None, {"validation": {"a": 1}}) == [
        {"parameter": "a", "current_value": None, "proposed_value": 1}
    ]
    assert build_parameter_diff({"validation": {"a": 1}}, None) == []


def test_hit_rate_excludes_none_and_handles_all_none() -> None:
    assert cr._hit_rate([True, True, False, None]) == pytest.approx(2 / 3)
    assert cr._hit_rate([None, None]) is None
    assert cr._hit_rate([]) is None


def test_group_outcome_rows_groups_by_setup_type_regime_config() -> None:
    rows = [
        {"setup_type": "breakout", "regime": "bull", "config_id": "a", "signal_date": date(2024, 1, 1)},
        {"setup_type": "breakout", "regime": "bull", "config_id": "b", "signal_date": date(2024, 1, 2)},
        {"setup_type": "breakout", "regime": "bear", "config_id": "a", "signal_date": date(2024, 1, 3)},
    ]
    cells = cr._group_outcome_rows(rows)
    assert set(cells) == {("breakout", "bull"), ("breakout", "bear")}
    assert set(cells[("breakout", "bull")]) == {"a", "b"}


# --------------------------------------------------------------------------- #
# Static source scans -- never auto-activates.
# --------------------------------------------------------------------------- #
def test_never_references_activate_setup_config() -> None:
    """AST-based (not a raw substring scan): the module's docstring legitimately
    *mentions* activate_setup_config in prose explaining why it's never called;
    what must actually be absent is any Name/Attribute node referencing it as
    code (an identifier, a call, an attribute access)."""
    tree = ast.parse(MODULE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "activate_setup_config"
        if isinstance(node, ast.Attribute):
            assert node.attr != "activate_setup_config"


def test_never_imports_config_service() -> None:
    tree = ast.parse(MODULE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "config_service" not in node.module
        if isinstance(node, ast.Import):
            assert all("config_service" not in a.name for a in node.names)


def test_no_print_calls() -> None:
    tree = ast.parse(MODULE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_reuses_compute_metrics_not_reimplemented() -> None:
    """Confirms the module imports (reuses) simulation_engine.compute_metrics
    rather than defining its own expectancy/win_rate/profit_factor formulas."""
    tree = ast.parse(MODULE_SRC)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.simulation.simulation_engine":
            imported_names.update(a.name for a in node.names)
    assert "compute_metrics" in imported_names
    # And no locally-defined function reimplements it under another name.
    assert "def compute_metrics" not in MODULE_SRC


# --------------------------------------------------------------------------- #
# Pre-DB validation.
# --------------------------------------------------------------------------- #
def test_run_rejects_non_prod_db_role() -> None:
    class Boom:
        def connect(self, *a, **k):
            raise AssertionError("DB accessed before validation")

    res = ConfigRecommenderService(Boom()).run(db_role="debug")
    assert res.status == service_result.STATUS_FAILED
    assert "prod" in res.errors[0].lower()
    assert frozenset(res.metadata) == frozenset(cr.METADATA_KEYS)


def test_get_pending_recommendations_rejects_non_prod_db_role() -> None:
    res = ConfigRecommenderService().get_pending_recommendations(db_role="simulation")
    assert res.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# Integration layer (real DuckDB, Module 03 schema).
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    from app.config import settings
    from app.database import schema_manager as sm

    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
    assert sm.apply_simulation_schema().status == service_result.STATUS_SUCCESS
    return {"prod": prod, "simulation": simulation}


def _conn(path: Path):
    import duckdb

    return duckdb.connect(database=str(path))


def _seed_setup_config(conn, config_id: str, setup_type: str, active: bool) -> None:
    conn.execute(
        "INSERT INTO setup_configs (config_id, setup_type, version, parent_config_id, "
        " config_json, config_hash, active_flag, created_at, notes) "
        "VALUES (?, ?, 'v1', NULL, ?, 'dummyhash', ?, CAST(now() AS TIMESTAMP), 'test')",
        [
            config_id, setup_type,
            json.dumps({"setup_type": setup_type, "validation": {"min_rvol_breakout": 1.5 if active else 2.0}}),
            active,
        ],
    )


def _seed_prod_outcome(
    conn, *, idx: int, config_id: str, setup_type: str, signal_date: date,
    r: float | None, stop_hit: bool | None, target_hit: bool | None, market_regime: str | None,
) -> None:
    proposal_id = f"prop-{config_id}-{idx}"
    outcome_id = f"outc-{config_id}-{idx}"
    conn.execute(
        "INSERT INTO step5_proposals (proposal_id, run_id, setup_config_id, ticker, "
        " signal_date, setup_type, disposition, market_regime, in_raw_top_n, "
        " in_diversified_top_n, diversification_applied, selected_top_n, "
        " selected_flag, ai_reviewed, executed_flag, created_at) "
        "VALUES (?, 'run1', ?, ?, ?, ?, 'BUY', ?, TRUE, TRUE, TRUE, TRUE, TRUE, "
        " FALSE, FALSE, CAST(now() AS TIMESTAMP))",
        [proposal_id, config_id, f"T{idx}", signal_date, setup_type, market_regime],
    )
    conn.execute(
        "INSERT INTO signal_outcomes (outcome_id, proposal_id, ticker, setup_config_id, "
        " setup_type, signal_date, entry_date, realized_r_multiple, stop_hit, "
        " target_hit, outcome_status, calculated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', CAST(now() AS TIMESTAMP))",
        [
            outcome_id, proposal_id, f"T{idx}", config_id, setup_type,
            signal_date, signal_date + timedelta(days=1), r, stop_hit, target_hit,
        ],
    )


def _seed_sim_outcome(
    conn, *, idx: int, config_id: str, setup_type: str, signal_date: date,
    r: float | None, stop_hit: bool | None, target_hit: bool | None, market_regime: str | None,
) -> None:
    proposal_id = f"sim-prop-{config_id}-{idx}"
    outcome_id = f"sim-outc-{config_id}-{idx}"
    conn.execute(
        "INSERT INTO sim_step5_proposals (proposal_id, sim_run_id, fold_id, "
        " setup_config_id, ticker, signal_date, setup_type, disposition, "
        " market_regime, in_raw_top_n, in_diversified_top_n, "
        " diversification_applied, selected_top_n, selected_flag, created_at) "
        "VALUES (?, 'simrun1', NULL, ?, ?, ?, ?, 'BUY', ?, TRUE, TRUE, TRUE, "
        " TRUE, TRUE, CAST(now() AS TIMESTAMP))",
        [proposal_id, config_id, f"S{idx}", signal_date, setup_type, market_regime],
    )
    conn.execute(
        "INSERT INTO sim_signal_outcomes (outcome_id, sim_run_id, fold_id, "
        " proposal_id, ticker, setup_config_id, setup_type, signal_date, "
        " entry_date, realized_r_multiple, stop_hit, target_hit, "
        " cross_fold_outcome, outcome_status, calculated_at) "
        "VALUES (?, 'simrun1', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, "
        " 'complete', CAST(now() AS TIMESTAMP))",
        [
            outcome_id, proposal_id, f"S{idx}", config_id, setup_type,
            signal_date, signal_date + timedelta(days=1), r, stop_hit, target_hit,
        ],
    )


def _seed_n(
    seeder, conn, *, n: int, config_id: str, setup_type: str, r: float, spread: float = 0.0,
    start: date = date(2024, 1, 2), market_regime: str = "bull",
) -> None:
    """Seed n outcome rows with mean ``r``. ``spread`` alternates +/- spread
    around the mean so the series has real (non-zero) sample variance --
    constant values give pooled_se == 0, which trivially "qualifies" any
    positive difference regardless of size and defeats guardrail tests."""
    for i in range(n):
        r_i = r + (spread if i % 2 == 0 else -spread)
        seeder(
            conn, idx=i, config_id=config_id, setup_type=setup_type,
            signal_date=start + timedelta(days=i), r=r_i,
            stop_hit=(r_i < 0), target_hit=(r_i > 0), market_regime=market_regime,
        )


def test_integration_recommends_clear_winner(tmp_db_paths) -> None:
    prod = _conn(tmp_db_paths["prod"])
    sim = _conn(tmp_db_paths["simulation"])
    try:
        _seed_setup_config(prod, INCUMBENT, "breakout", active=True)
        _seed_n(_seed_prod_outcome, prod, n=40, config_id=INCUMBENT, setup_type="breakout", r=0.2, spread=0.05)
        _seed_setup_config(sim, CANDIDATE, "breakout", active=False)
        _seed_n(_seed_sim_outcome, sim, n=40, config_id=CANDIDATE, setup_type="breakout", r=2.0, spread=0.05)
    finally:
        prod.close()
        sim.close()

    res = ConfigRecommenderService().run(db_role="prod", sample_floor=30, margin_k=1.0)
    assert res.status == service_result.STATUS_SUCCESS, res.errors
    assert res.metadata["recommendations_written"] == 1
    assert res.metadata["cells_evaluated"] == 1
    assert res.metadata["candidates_evaluated"] == 1

    conn = _conn(tmp_db_paths["prod"])
    try:
        row = conn.execute(
            "SELECT setup_type, regime, incumbent_config_id, candidate_config_id, "
            " proposal_json, evidence_json, status "
            "FROM config_recommendations"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    setup_type, regime, incumbent_id, candidate_id, proposal_json, evidence_json, status = row
    assert setup_type == "breakout"
    assert regime == "bull"
    assert incumbent_id == INCUMBENT
    assert candidate_id == CANDIDATE
    assert status == "pending"
    proposal = json.loads(proposal_json) if isinstance(proposal_json, str) else proposal_json
    assert any(p["parameter"] == "min_rvol_breakout" for p in proposal)
    evidence = json.loads(evidence_json) if isinstance(evidence_json, str) else evidence_json
    assert evidence["incumbent"]["config_id"] == INCUMBENT
    assert evidence["winner_config_id"] == CANDIDATE
    assert evidence["candidates"][0]["qualified"] is True


def test_integration_below_sample_floor_emits_no_recommendation(tmp_db_paths) -> None:
    prod = _conn(tmp_db_paths["prod"])
    sim = _conn(tmp_db_paths["simulation"])
    try:
        _seed_setup_config(prod, INCUMBENT, "breakout", active=True)
        _seed_n(_seed_prod_outcome, prod, n=40, config_id=INCUMBENT, setup_type="breakout", r=0.2, spread=0.05)
        _seed_setup_config(sim, CANDIDATE, "breakout", active=False)
        # Only 10 candidate samples -- below the 30-sample floor, despite a huge
        # expectancy edge (r=5.0 vs incumbent's 0.2).
        _seed_n(_seed_sim_outcome, sim, n=10, config_id=CANDIDATE, setup_type="breakout", r=5.0)
    finally:
        prod.close()
        sim.close()

    res = ConfigRecommenderService().run(db_role="prod")
    assert res.status == service_result.STATUS_SUCCESS, res.errors
    assert res.metadata["recommendations_written"] == 0
    assert res.metadata["candidates_evaluated"] == 1

    conn = _conn(tmp_db_paths["prod"])
    try:
        n = conn.execute("SELECT COUNT(*) FROM config_recommendations").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_integration_above_floor_below_margin_emits_no_recommendation(tmp_db_paths) -> None:
    prod = _conn(tmp_db_paths["prod"])
    sim = _conn(tmp_db_paths["simulation"])
    try:
        _seed_setup_config(prod, INCUMBENT, "breakout", active=True)
        _seed_n(_seed_prod_outcome, prod, n=40, config_id=INCUMBENT, setup_type="breakout", r=0.30, spread=0.15)
        _seed_setup_config(sim, CANDIDATE, "breakout", active=False)
        # Candidate is only trivially higher (0.31 vs 0.30) -- above the sample
        # floor but the improvement is noise-sized, not margin-sized.
        _seed_n(_seed_sim_outcome, sim, n=40, config_id=CANDIDATE, setup_type="breakout", r=0.31, spread=0.15)
    finally:
        prod.close()
        sim.close()

    res = ConfigRecommenderService().run(db_role="prod")
    assert res.status == service_result.STATUS_SUCCESS, res.errors
    assert res.metadata["recommendations_written"] == 0


def test_integration_no_incumbent_data_skips_cell(tmp_db_paths) -> None:
    """If the currently-active config has no realized outcomes in a cell,
    there is no baseline to compare against -- the cell is skipped, not
    treated as an automatic win for whichever candidate has data."""
    prod = _conn(tmp_db_paths["prod"])
    sim = _conn(tmp_db_paths["simulation"])
    try:
        _seed_setup_config(prod, INCUMBENT, "breakout", active=True)
        # No signal_outcomes rows for INCUMBENT at all.
        _seed_setup_config(sim, CANDIDATE, "breakout", active=False)
        _seed_n(_seed_sim_outcome, sim, n=40, config_id=CANDIDATE, setup_type="breakout", r=2.0, spread=0.05)
    finally:
        prod.close()
        sim.close()

    res = ConfigRecommenderService().run(db_role="prod")
    assert res.status == service_result.STATUS_SUCCESS, res.errors
    assert res.metadata["recommendations_written"] == 0
    assert res.metadata["cells_evaluated"] == 0
    assert res.metadata["cells_skipped_no_incumbent_data"] == 1


def test_integration_missing_simulation_db_degrades_to_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No simulation.duckdb at all (fresh install, no sim runs yet) must not
    hard-fail the whole run -- prod-only data can still be useful."""
    from app.config import settings
    from app.database import schema_manager as sm

    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"  # never created
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS

    conn = _conn(prod)
    try:
        _seed_setup_config(conn, INCUMBENT, "breakout", active=True)
        _seed_n(_seed_prod_outcome, conn, n=40, config_id=INCUMBENT, setup_type="breakout", r=0.2, spread=0.05)
    finally:
        conn.close()

    res = ConfigRecommenderService().run(db_role="prod")
    assert res.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert res.warnings
    assert res.metadata["sim_outcomes_read"] == 0
    assert res.metadata["prod_outcomes_read"] == 40


def test_get_pending_recommendations_returns_written_rows(tmp_db_paths) -> None:
    prod = _conn(tmp_db_paths["prod"])
    sim = _conn(tmp_db_paths["simulation"])
    try:
        _seed_setup_config(prod, INCUMBENT, "breakout", active=True)
        _seed_n(_seed_prod_outcome, prod, n=40, config_id=INCUMBENT, setup_type="breakout", r=0.2, spread=0.05)
        _seed_setup_config(sim, CANDIDATE, "breakout", active=False)
        _seed_n(_seed_sim_outcome, sim, n=40, config_id=CANDIDATE, setup_type="breakout", r=2.0, spread=0.05)
    finally:
        prod.close()
        sim.close()

    svc = ConfigRecommenderService()
    run_result = svc.run(db_role="prod")
    assert run_result.metadata["recommendations_written"] == 1

    read_result = svc.get_pending_recommendations(db_role="prod")
    assert read_result.status == service_result.STATUS_SUCCESS
    recs = read_result.metadata["recommendations"]
    assert len(recs) == 1
    assert recs[0]["status"] == "pending"
    assert recs[0]["candidate_config_id"] == CANDIDATE
    assert isinstance(recs[0]["proposal_json"], list)
    assert isinstance(recs[0]["evidence_json"], dict)

    filtered = svc.get_pending_recommendations(db_role="prod", setup_type="pullback")
    assert filtered.metadata["recommendations"] == []


# --------------------------------------------------------------------------- #
# set_recommendation_status
# --------------------------------------------------------------------------- #
def test_set_recommendation_status_rejects_non_prod_db_role() -> None:
    res = ConfigRecommenderService().set_recommendation_status(
        recommendation_id="rec1", status="approved", db_role="debug"
    )
    assert res.status == service_result.STATUS_FAILED
    assert "prod" in res.errors[0].lower()


def test_set_recommendation_status_rejects_invalid_status() -> None:
    class Boom:
        def connect(self, *a, **k):
            raise AssertionError("DB accessed before validation")

    res = ConfigRecommenderService(Boom()).set_recommendation_status(
        recommendation_id="rec1", status="pending", db_role="prod"
    )
    assert res.status == service_result.STATUS_FAILED
    assert "status must be one of" in res.errors[0]


def test_set_recommendation_status_rejects_empty_recommendation_id() -> None:
    class Boom:
        def connect(self, *a, **k):
            raise AssertionError("DB accessed before validation")

    res = ConfigRecommenderService(Boom()).set_recommendation_status(
        recommendation_id="", status="approved", db_role="prod"
    )
    assert res.status == service_result.STATUS_FAILED


def _seed_recommendation(conn, *, recommendation_id: str, setup_type: str = "breakout") -> None:
    conn.execute(
        "INSERT INTO config_recommendations "
        "(recommendation_id, run_id, setup_type, regime, incumbent_config_id, "
        " candidate_config_id, proposal_json, evidence_json, status, created_at) "
        "VALUES (?, 'run1', ?, 'bull', 'incumbent1', 'candidate1', '[]', '{}', "
        " 'pending', CAST(now() AS TIMESTAMP))",
        [recommendation_id, setup_type],
    )


def test_set_recommendation_status_approves_pending_row(tmp_db_paths) -> None:
    prod = _conn(tmp_db_paths["prod"])
    try:
        _seed_recommendation(prod, recommendation_id="rec1")
    finally:
        prod.close()

    svc = ConfigRecommenderService()
    res = svc.set_recommendation_status(recommendation_id="rec1", status="approved", db_role="prod")
    assert res.status == service_result.STATUS_SUCCESS
    assert res.rows_processed == 1

    pending = svc.get_pending_recommendations(db_role="prod")
    assert pending.metadata["recommendations"] == []  # no longer pending


def test_set_recommendation_status_rejects_pending_row(tmp_db_paths) -> None:
    prod = _conn(tmp_db_paths["prod"])
    try:
        _seed_recommendation(prod, recommendation_id="rec2")
    finally:
        prod.close()

    svc = ConfigRecommenderService()
    res = svc.set_recommendation_status(recommendation_id="rec2", status="rejected", db_role="prod")
    assert res.status == service_result.STATUS_SUCCESS

    conn = _conn(tmp_db_paths["prod"])
    try:
        row = conn.execute(
            "SELECT status FROM config_recommendations WHERE recommendation_id = ?", ["rec2"]
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "rejected"


def test_set_recommendation_status_unknown_id_fails(tmp_db_paths) -> None:
    svc = ConfigRecommenderService()
    res = svc.set_recommendation_status(recommendation_id="nope", status="approved", db_role="prod")
    assert res.status == service_result.STATUS_FAILED
    assert "not found" in res.errors[0]


def test_set_recommendation_status_double_apply_is_noop_with_warning(tmp_db_paths) -> None:
    prod = _conn(tmp_db_paths["prod"])
    try:
        _seed_recommendation(prod, recommendation_id="rec3")
    finally:
        prod.close()

    svc = ConfigRecommenderService()
    first = svc.set_recommendation_status(recommendation_id="rec3", status="approved", db_role="prod")
    assert first.status == service_result.STATUS_SUCCESS

    second = svc.set_recommendation_status(recommendation_id="rec3", status="rejected", db_role="prod")
    assert second.status == service_result.STATUS_SUCCESS_WITH_WARNINGS
    assert second.rows_processed == 0
    assert "already" in second.warnings[0]

    # Status must remain 'approved', not silently overwritten by the second call.
    conn = _conn(tmp_db_paths["prod"])
    try:
        row = conn.execute(
            "SELECT status FROM config_recommendations WHERE recommendation_id = ?", ["rec3"]
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "approved"
