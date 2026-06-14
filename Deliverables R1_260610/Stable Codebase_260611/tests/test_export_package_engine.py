"""Tests for Module 18 — Export Package Engine.

All tests run fully offline. A ``FakeDbManager`` hands out real DuckDB
connections to ``tmp_path`` databases that have the real Module 03 schema
applied; no real prod/debug/simulation DB file is ever touched, and no network
or provider is involved. ``settings.EXPORTS_DIR`` is redirected into
``tmp_path`` so every ZIP lands under the temp exports directory.
"""

from __future__ import annotations

import ast
import json
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import pytest

from app.config import settings
from app.database import schema_manager as sm
from app.services.export import export_package_engine as epe
from app.services.export.export_package_engine import ExportPackageEngine
from app.utils import service_result

CONFIG_ID = "cfg-1"
SIGNAL_DATE = date(2024, 3, 1)
MODULE_PATH = Path(epe.__file__)

TICKER_KEYS = frozenset(epe.TICKER_METADATA_KEYS)
SIM_KEYS = frozenset(epe.SIM_METADATA_KEYS)


# --------------------------------------------------------------------------- #
# Fake injected DB managers.
# --------------------------------------------------------------------------- #
class FakeDbManager:
    """Hands out real DuckDB connections to tmp_path role databases."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = paths

    def connect(self, db_role: str, read_only: bool = False):
        return duckdb.connect(database=str(self._paths[db_role]), read_only=read_only)


class _FailWriteConn:
    """Proxy around a real DuckDB connection that raises on any INSERT."""

    def __init__(self, real: duckdb.DuckDBPyConnection) -> None:
        self._c = real

    def execute(self, sql: str, params=None):
        if sql.strip().upper().startswith("INSERT"):
            raise RuntimeError("simulated review-row write failure")
        if params is not None:
            self._c.execute(sql, params)
        else:
            self._c.execute(sql)
        return self

    @property
    def description(self):
        return self._c.description

    def fetchall(self):
        return self._c.fetchall()

    def fetchone(self):
        return self._c.fetchone()

    def close(self):
        self._c.close()


class FailingWriteDbManager:
    """Read-only connections pass through; write connections wrap with _FailWriteConn."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self._paths = paths

    def connect(self, db_role: str, read_only: bool = False):
        conn = duckdb.connect(database=str(self._paths[db_role]), read_only=read_only)
        return conn if read_only else _FailWriteConn(conn)


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    duckdb_dir = tmp_path / "duckdb"
    paths = {
        "prod":       duckdb_dir / "prod.duckdb",
        "debug":      duckdb_dir / "debug.duckdb",
        "simulation": duckdb_dir / "simulation.duckdb",
    }
    exports = tmp_path / "exports"

    monkeypatch.setattr(settings, "DUCKDB_DIR",         duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH",       paths["prod"],       raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH",      paths["debug"],      raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", paths["simulation"], raising=True)
    monkeypatch.setattr(settings, "EXPORTS_DIR",        exports,             raising=True)

    assert sm.apply_prod_schema().status       == service_result.STATUS_SUCCESS
    assert sm.apply_simulation_schema().status == service_result.STATUS_SUCCESS

    return {"paths": paths, "exports": exports, "db": FakeDbManager(paths)}


def _conn(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(database=str(path))


# --------------------------------------------------------------------------- #
# Ticker seeding helpers.
# --------------------------------------------------------------------------- #
def _seed_proposal(conn, pid: str, ticker: str, cfg: str, sig: date, score: float = 88.0):
    conn.execute(
        "INSERT INTO step5_proposals "
        "(proposal_id, run_id, strategy_config_id, ticker, signal_date, "
        " proposal_score_raw, proposal_score_final, raw_rank, diversified_rank, "
        " in_raw_top_n, in_diversified_top_n, rank_position, "
        " mechanical_explanation, created_at) "
        "VALUES (?, 'run', ?, ?, ?, ?, ?, 1, 1, TRUE, TRUE, 1, ?, "
        " CAST(now() AS TIMESTAMP))",
        [pid, cfg, ticker, sig, score, score,
         json.dumps({"reason": f"{ticker} breakout", "rr": 2.5})],
    )
    conn.execute(
        "INSERT INTO step3_candidates "
        "(candidate_id, run_id, strategy_config_id, ticker, signal_date, "
        " screening_score, passed_hard_filters, created_at) "
        "VALUES (?, 'run', ?, ?, ?, ?, TRUE, CAST(now() AS TIMESTAMP))",
        [f"c-{pid}", cfg, ticker, sig, score],
    )
    conn.execute(
        "INSERT INTO step4_analysis "
        "(analysis_id, candidate_id, run_id, strategy_config_id, ticker, "
        " signal_date, setup_type, setup_score, estimated_rr, "
        " stop_price_raw, target_price_raw, created_at) "
        "VALUES (?, ?, 'run', ?, ?, ?, 'breakout', ?, 2.5, 9.0, 14.0, "
        " CAST(now() AS TIMESTAMP))",
        [f"a-{pid}", f"c-{pid}", cfg, ticker, sig, score],
    )
    conn.execute(
        "INSERT INTO daily_features "
        "(ticker, feature_date, feature_cutoff_date, "
        " feature_schema_version, feature_ready, rsi14, calculated_at) "
        "VALUES (?, ?, ?, 'v1', TRUE, 55.0, CAST(now() AS TIMESTAMP))",
        [ticker, sig, sig],
    )
    for offset in range(-7, 6):
        d = date.fromordinal(sig.toordinal() + offset)
        conn.execute(
            "INSERT OR IGNORE INTO daily_prices "
            "(ticker, date, open_raw, high_raw, low_raw, close_raw, "
            " volume_raw, close_adj, source_provider, "
            " data_quality_status, mutation_flag, created_at) "
            "VALUES (?, ?, 10, 11, 9, 10.5, 1000, 10.5, 'test', 'ok', "
            " FALSE, CAST(now() AS TIMESTAMP))",
            [ticker, d],
        )


def seed_ticker(paths: dict[str, Path]) -> list[str]:
    """Seed prod with two standard proposals."""
    conn = _conn(paths["prod"])
    try:
        _seed_proposal(conn, "p1", "AAA", CONFIG_ID, SIGNAL_DATE, 88.0)
        _seed_proposal(conn, "p2", "BBB", CONFIG_ID, SIGNAL_DATE, 72.0)
    finally:
        conn.close()
    return ["p1", "p2"]


# --------------------------------------------------------------------------- #
# Simulation seeding helpers.
# --------------------------------------------------------------------------- #
SIM_RUN_ID = "sim-run-0001"


def seed_simulation(paths: dict[str, Path], *, negative: bool = True) -> None:
    conn = _conn(paths["simulation"])
    try:
        conn.execute(
            "INSERT INTO sim_runs "
            "(sim_run_id, sim_name, mode, start_date, end_date, created_at, "
            " config_ids, status) "
            "VALUES (?, 'demo', 'walk_forward', ?, ?, CAST(now() AS TIMESTAMP), "
            " ?, 'success')",
            [SIM_RUN_ID, date(2024, 1, 1), date(2024, 6, 1),
             json.dumps([CONFIG_ID, "cfg-2"])],
        )
        for cid, exp, dd, resolved in (
            (CONFIG_ID, 0.4, -5.0,  90.0),
            ("cfg-2",   0.1, -12.0, 80.0),
        ):
            conn.execute(
                "INSERT INTO sim_config_comparisons "
                "(comparison_id, sim_run_id, config_id, horizon_bd, expectancy, "
                " win_rate, avg_win, avg_loss, profit_factor, max_drawdown_pct, "
                " resolved_outcomes_pct, list_type, created_at) "
                "VALUES (?, ?, ?, 20, ?, 0.55, 6.0, -3.0, 1.6, ?, ?, "
                " 'diversified', CAST(now() AS TIMESTAMP))",
                [f"cmp-{cid}", SIM_RUN_ID, cid, exp, dd, resolved],
            )
        for i, score in enumerate((15.0, 55.0, 95.0)):
            conn.execute(
                "INSERT INTO sim_step3_candidates "
                "(candidate_id, sim_run_id, strategy_config_id, ticker, "
                " signal_date, screening_score, passed_hard_filters, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, TRUE, CAST(now() AS TIMESTAMP))",
                [f"sc-{i}", SIM_RUN_ID, CONFIG_ID, f"T{i}", SIGNAL_DATE, score],
            )
        for i, score in enumerate((25.0, 65.0)):
            conn.execute(
                "INSERT INTO sim_step5_proposals "
                "(proposal_id, sim_run_id, strategy_config_id, ticker, "
                " signal_date, proposal_score_final, selected_top_n, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, TRUE, CAST(now() AS TIMESTAMP))",
                [f"sp-{i}", SIM_RUN_ID, CONFIG_ID, f"T{i}", SIGNAL_DATE, score],
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO sim_step4_analysis "
                "(analysis_id, candidate_id, sim_run_id, strategy_config_id, "
                " ticker, signal_date, setup_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'breakout', CAST(now() AS TIMESTAMP))",
                [f"sa-{i}", f"sc-{i}", SIM_RUN_ID, CONFIG_ID, f"T{i}", SIGNAL_DATE],
            )
        ret_seq = (-8.0 if negative else 8.0, 5.0)
        conn.execute(
            "INSERT INTO sim_signal_outcomes "
            "(outcome_id, sim_run_id, proposal_id, ticker, strategy_config_id, "
            " signal_date, entry_date, return_40bd_pct, list_membership, "
            " outcome_status) "
            "VALUES ('o-partial', ?, 'sp-x', 'TX', ?, ?, ?, NULL, 'both', 'partial')",
            [SIM_RUN_ID, CONFIG_ID, SIGNAL_DATE, SIGNAL_DATE],
        )
        for i, r40 in enumerate(ret_seq):
            d = date.fromordinal(SIGNAL_DATE.toordinal() + i)
            conn.execute(
                "INSERT INTO sim_signal_outcomes "
                "(outcome_id, sim_run_id, proposal_id, ticker, "
                " strategy_config_id, signal_date, entry_date, "
                " return_5bd_pct, return_40bd_pct, list_membership, outcome_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'both', 'complete')",
                [f"o-{i}", SIM_RUN_ID, f"sp-{i}", f"T{i}", CONFIG_ID, d, d,
                 r40 / 2.0, r40],
            )
    finally:
        conn.close()


# =========================================================================== #
# Ticker review — core tests.
# =========================================================================== #
def test_ticker_success_zip_and_review_row(env):
    proposal_ids = seed_ticker(env["paths"])
    engine = ExportPackageEngine(db_manager=env["db"])

    result = engine.export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, proposal_ids, db_role="prod", run_id="run-fixed"
    )

    assert result.status == service_result.STATUS_SUCCESS
    assert result.run_id == "run-fixed"
    assert frozenset(result.metadata) == TICKER_KEYS
    assert result.metadata["error"] is None
    assert result.metadata["review_table"] == "ai_reviews"

    zip_path = Path(result.metadata["zip_path"])
    assert zip_path.parent == env["exports"]
    assert zip_path.name == "ticker_review_2024-03-01_run-fixe.zip"
    assert zip_path.exists()

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert names == {
            "metadata.json", "prices.csv", "features.csv",
            "step3.csv", "step4.csv", "step5.csv", "explanation.txt",
        }
        for name in names:
            assert zf.read(name), f"{name} must be non-empty"
        meta = json.loads(zf.read("metadata.json"))
        # Fix C: exact order preserved, not just set equality.
        assert meta["proposal_ids"] == proposal_ids
        assert "AAA" in zf.read("explanation.txt").decode()

    conn = _conn(env["paths"]["prod"])
    try:
        rows = conn.execute(
            "SELECT review_type, proposal_id, provider, model, "
            "prompt_version, prompt_text, selected_tickers_json FROM ai_reviews"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    review_type, proposal_id, provider, model, pv, prompt_text, tickers_json = rows[0]
    assert review_type == "ticker_review"
    # proposal_id must be the FIRST EXPORTED (step5 sorted by proposal_id).
    assert proposal_id == "p1"
    assert provider == "manual"
    assert model == "none"
    assert pv == "v1"
    assert "TICKER REVIEW" in prompt_text
    assert set(json.loads(tickers_json)) == {"AAA", "BBB"}


def test_ticker_run_id_minted_when_none(env):
    proposal_ids = seed_ticker(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, proposal_ids
    )
    assert result.status == service_result.STATUS_SUCCESS
    assert result.run_id
    assert result.run_id == result.metadata["run_id"]


def test_ticker_run_id_preserved_when_supplied(env):
    proposal_ids = seed_ticker(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, proposal_ids, run_id="my-run-id"
    )
    assert result.run_id == "my-run-id"
    assert result.metadata["run_id"] == "my-run-id"


# =========================================================================== #
# Fix A — requested order is preserved end-to-end.
# =========================================================================== #
def test_ticker_requested_order_preserved(env):
    """export_ticker_review(..., proposal_ids=["p2","p1"]) → p2 is first everywhere."""
    seed_ticker(env["paths"])  # seeds p1/AAA and p2/BBB
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["p2", "p1"], db_role="prod"
    )
    assert result.status == service_result.STATUS_SUCCESS

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        meta = json.loads(zf.read("metadata.json"))
        step5_lines = zf.read("step5.csv").decode().strip().splitlines()
        explanation = zf.read("explanation.txt").decode()

    # metadata.json: exact requested order
    assert meta["proposal_ids"] == ["p2", "p1"]

    # step5.csv: first data row is p2
    assert step5_lines[1].startswith("p2,"), step5_lines[1]

    # explanation.txt: first block is for p2/BBB
    first_block_header = explanation.strip().splitlines()[0]
    assert "p2" in first_block_header and "BBB" in first_block_header, first_block_header

    # ai_reviews: proposal_id is the first *requested* (p2), not lexicographic (p1)
    conn = _conn(env["paths"]["prod"])
    try:
        row = conn.execute(
            "SELECT proposal_id, selected_tickers_json FROM ai_reviews"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "p2"
    # selected_tickers_json follows exported (requested) order
    tickers = json.loads(row[1])
    assert tickers == ["BBB", "AAA"]


# =========================================================================== #
# Fix B — duplicate proposal_ids rejected before DB access.
# =========================================================================== #
def test_ticker_duplicate_proposal_ids_fails(env):
    """["p1", "p1"] must fail before any DB access; no ZIP, no ai_reviews row."""
    seed_ticker(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["p1", "p1"], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS
    assert result.metadata["zip_path"] is None
    assert not list(env["exports"].glob("*.zip")) if env["exports"].exists() else True
    assert "duplicate" in result.metadata["error"].lower()

    conn = _conn(env["paths"]["prod"])
    try:
        count = conn.execute("SELECT COUNT(*) FROM ai_reviews").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# =========================================================================== #
# Ticker review — pre-DB validation.
# =========================================================================== #
def test_ticker_invalid_role_fails_before_db(env):
    engine = ExportPackageEngine(db_manager=env["db"])
    result = engine.export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["p1"], db_role="simulation"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS
    assert result.metadata["zip_path"] is None
    assert result.metadata["error"]
    # No ZIP produced.
    assert not list(env["exports"].glob("*.zip")) if env["exports"].exists() else True


def test_ticker_empty_proposal_ids_fails(env):
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, [], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS
    assert "proposal_ids" in result.metadata["error"]


def test_ticker_empty_strategy_config_id_fails(env):
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, "", ["p1"], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert "strategy_config_id" in result.metadata["error"]


# =========================================================================== #
# Fix A — missing proposal_id.
# =========================================================================== #
def test_ticker_missing_proposal_id_fails(env):
    """Request a proposal_id that does not exist → failed, no ZIP, no review row."""
    seed_ticker(env["paths"])  # seeds p1, p2 — but we request "missing"
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["missing"], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS
    assert result.metadata["zip_path"] is None
    assert not list(env["exports"].glob("*.zip")) if env["exports"].exists() else True

    conn = _conn(env["paths"]["prod"])
    try:
        count = conn.execute("SELECT COUNT(*) FROM ai_reviews").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# =========================================================================== #
# Fix B — partial missing proposal_ids.
# =========================================================================== #
def test_ticker_partial_missing_proposal_ids_fails(env):
    """Request ["missing", "p1"]; p1 exists but 'missing' does not → full failure."""
    seed_ticker(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["missing", "p1"], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS
    assert result.metadata["zip_path"] is None

    conn = _conn(env["paths"]["prod"])
    try:
        count = conn.execute("SELECT COUNT(*) FROM ai_reviews").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# =========================================================================== #
# Fix C — wrong config/date.
# =========================================================================== #
def test_ticker_wrong_strategy_config_id_fails(env):
    """Proposal exists under OTHER_CONFIG; exporting under CONFIG_ID → failed."""
    conn = _conn(env["paths"]["prod"])
    try:
        _seed_proposal(conn, "p-other", "ZZZ", "OTHER_CONFIG", SIGNAL_DATE, 70.0)
    finally:
        conn.close()

    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["p-other"], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS
    assert result.metadata["zip_path"] is None


def test_ticker_wrong_signal_date_fails(env):
    """Proposal exists on a different signal_date → failed."""
    other_date = date(2024, 1, 10)
    conn = _conn(env["paths"]["prod"])
    try:
        _seed_proposal(conn, "p-old", "YYY", CONFIG_ID, other_date, 70.0)
    finally:
        conn.close()

    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["p-old"], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS


# =========================================================================== #
# Fix D — empty-ticker leakage regression.
# =========================================================================== #
def test_ticker_no_leakage_when_proposal_missing(env):
    """step3/step4 exist but proposal is nonexistent → no ZIP, no row."""
    seed_ticker(env["paths"])  # real step3/4 rows for CONFIG_ID + SIGNAL_DATE

    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, ["nonexistent-id"], db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["zip_path"] is None
    # No ZIP containing any data.
    zips = list(env["exports"].glob("*.zip")) if env["exports"].exists() else []
    assert not zips


# =========================================================================== #
# Fix E — proposal_ids=None.
# =========================================================================== #
def test_ticker_proposal_ids_none_fails_gracefully(env):
    """Passing None instead of a list must return failed without raising."""
    result = ExportPackageEngine(db_manager=env["db"]).export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, None, db_role="prod"  # type: ignore[arg-type]
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == TICKER_KEYS
    assert result.metadata["zip_path"] is None
    assert result.metadata["error"]


# =========================================================================== #
# Ticker review — DB write failure.
# =========================================================================== #
def test_ticker_review_write_failure_returns_failed_zip_remains(env):
    proposal_ids = seed_ticker(env["paths"])
    engine = ExportPackageEngine(db_manager=FailingWriteDbManager(env["paths"]))
    result = engine.export_ticker_review(
        SIGNAL_DATE, CONFIG_ID, proposal_ids, db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["zip_path"] is not None
    assert Path(result.metadata["zip_path"]).exists()  # G-ZIP-CLEANUP: ZIP retained
    assert "review write failed" in result.metadata["error"]


# =========================================================================== #
# Simulation review — core tests.
# =========================================================================== #
def test_simulation_success_zip_and_review_row(env):
    seed_simulation(env["paths"], negative=True)
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(
        SIM_RUN_ID, run_id="sim-fixed"
    )

    assert result.status == service_result.STATUS_SUCCESS
    assert result.run_id == "sim-fixed"
    assert frozenset(result.metadata) == SIM_KEYS
    assert result.metadata["review_table"] == "sim_ai_reviews"

    zip_path = Path(result.metadata["zip_path"])
    assert zip_path.parent == env["exports"]
    expected_name = f"simulation_review_{SIM_RUN_ID[:8]}_sim-fixe.zip"
    assert zip_path.name == expected_name

    with zipfile.ZipFile(zip_path) as zf:
        assert set(zf.namelist()) == {
            "configs.json", "performance_metrics.csv", "score_buckets.csv",
            "setup_performance.csv", "regime_performance.csv",
            "drawdowns.csv", "unresolved_outcomes.csv",
        }
        configs = json.loads(zf.read("configs.json"))
        assert configs["config_ids"] == [CONFIG_ID, "cfg-2"]

    conn = _conn(env["paths"]["simulation"])
    try:
        rows = conn.execute(
            "SELECT sim_run_id, provider, model, prompt_version, prompt_text "
            "FROM sim_ai_reviews"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == SIM_RUN_ID
    assert rows[0][1] == "manual"
    assert rows[0][2] == "none"
    assert "SIMULATION REVIEW" in rows[0][4]


def test_simulation_run_id_minted_when_none(env):
    seed_simulation(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(SIM_RUN_ID)
    assert result.status == service_result.STATUS_SUCCESS
    assert result.run_id == result.metadata["run_id"]


def test_simulation_run_id_preserved_when_supplied(env):
    seed_simulation(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(
        SIM_RUN_ID, run_id="kept-id"
    )
    assert result.run_id == "kept-id"


# =========================================================================== #
# Simulation — pre-DB validation.
# =========================================================================== #
def test_simulation_invalid_role_fails_before_db(env):
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(
        SIM_RUN_ID, db_role="prod"
    )
    assert result.status == service_result.STATUS_FAILED
    assert frozenset(result.metadata) == SIM_KEYS
    assert result.metadata["zip_path"] is None


def test_simulation_empty_sim_run_id_fails(env):
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(
        "", db_role="simulation"
    )
    assert result.status == service_result.STATUS_FAILED
    assert "sim_run_id" in result.metadata["error"]


# =========================================================================== #
# Fix F — score bucket boundary 100.0 → bucket (90, 100).
# =========================================================================== #
def test_score_bucket_boundary_100(env):
    conn = _conn(env["paths"]["simulation"])
    try:
        conn.execute(
            "INSERT INTO sim_runs "
            "(sim_run_id, mode, start_date, end_date, created_at, "
            " config_ids, status) "
            "VALUES ('r-100', 'walk_forward', ?, ?, CAST(now() AS TIMESTAMP), "
            " '[]', 'success')",
            [date(2024, 1, 1), date(2024, 6, 1)],
        )
        conn.execute(
            "INSERT INTO sim_step3_candidates "
            "(candidate_id, sim_run_id, strategy_config_id, ticker, signal_date, "
            " screening_score, passed_hard_filters, created_at) "
            "VALUES ('sc-100', 'r-100', 'cfg-x', 'TT', ?, 100.0, TRUE, "
            " CAST(now() AS TIMESTAMP))",
            [SIGNAL_DATE],
        )
        conn.execute(
            "INSERT INTO sim_step5_proposals "
            "(proposal_id, sim_run_id, strategy_config_id, ticker, signal_date, "
            " proposal_score_final, selected_top_n, created_at) "
            "VALUES ('sp-100', 'r-100', 'cfg-x', 'TT', ?, 100.0, TRUE, "
            " CAST(now() AS TIMESTAMP))",
            [SIGNAL_DATE],
        )
    finally:
        conn.close()

    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review("r-100")
    assert result.status == service_result.STATUS_SUCCESS

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        lines = zf.read("score_buckets.csv").decode().strip().splitlines()
    data_rows = lines[1:]
    # Both step3 and step5 score 100.0 → both in bucket 90-100.
    assert any("step3,90,100" in row for row in data_rows), data_rows
    assert any("step5,90,100" in row for row in data_rows), data_rows


# =========================================================================== #
# Fix G — drawdowns for multiple configs computed independently.
# =========================================================================== #
def test_drawdowns_multiple_configs(env):
    conn = _conn(env["paths"]["simulation"])
    try:
        conn.execute(
            "INSERT INTO sim_runs "
            "(sim_run_id, mode, start_date, end_date, created_at, "
            " config_ids, status) "
            "VALUES ('r-multi', 'walk_forward', ?, ?, CAST(now() AS TIMESTAMP), "
            " '[]', 'success')",
            [date(2024, 1, 1), date(2024, 6, 1)],
        )
        d0 = date(2024, 3, 1)
        d1 = date(2024, 3, 4)
        for i, (cid, r40) in enumerate((("cfg-A", -20.0), ("cfg-B", -15.0))):
            conn.execute(
                "INSERT INTO sim_signal_outcomes "
                "(outcome_id, sim_run_id, proposal_id, ticker, "
                " strategy_config_id, signal_date, entry_date, "
                " return_40bd_pct, list_membership, outcome_status) "
                "VALUES (?, 'r-multi', ?, ?, ?, ?, ?, ?, 'both', 'complete')",
                [f"o-{i}-0", f"p-{i}", f"T{i}", cid, d0, d0, r40],
            )
            conn.execute(
                "INSERT INTO sim_signal_outcomes "
                "(outcome_id, sim_run_id, proposal_id, ticker, "
                " strategy_config_id, signal_date, entry_date, "
                " return_40bd_pct, list_membership, outcome_status) "
                "VALUES (?, 'r-multi', ?, ?, ?, ?, ?, ?, 'both', 'complete')",
                [f"o-{i}-1", f"p-{i}b", f"T{i}b", cid, d1, d1, r40],
            )
    finally:
        conn.close()

    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review("r-multi")
    assert result.status == service_result.STATUS_SUCCESS

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        lines = zf.read("drawdowns.csv").decode().strip().splitlines()

    data_rows = lines[1:]
    # Both configs must appear independently.
    assert any("cfg-A" in row for row in data_rows), data_rows
    assert any("cfg-B" in row for row in data_rows), data_rows
    assert len(data_rows) == 2


# =========================================================================== #
# Fix H — drawdown recovery: +20, -10, +5, -30.
# =========================================================================== #
def test_drawdown_recovery_path(env):
    """Equity curve: +20, -10, +5, -30. Peak at d0, trough at d3, dd ≈ -33.85%."""
    conn = _conn(env["paths"]["simulation"])
    returns = [20.0, -10.0, 5.0, -30.0]
    base = date(2024, 3, 1)
    try:
        conn.execute(
            "INSERT INTO sim_runs "
            "(sim_run_id, mode, start_date, end_date, created_at, "
            " config_ids, status) "
            "VALUES ('r-recov', 'walk_forward', ?, ?, CAST(now() AS TIMESTAMP), "
            " '[]', 'success')",
            [date(2024, 1, 1), date(2024, 6, 1)],
        )
        for i, r in enumerate(returns):
            d = date.fromordinal(base.toordinal() + i)
            conn.execute(
                "INSERT INTO sim_signal_outcomes "
                "(outcome_id, sim_run_id, proposal_id, ticker, "
                " strategy_config_id, signal_date, entry_date, "
                " return_40bd_pct, list_membership, outcome_status) "
                "VALUES (?, 'r-recov', ?, ?, 'cfg-r', ?, ?, ?, 'both', 'complete')",
                [f"o-r-{i}", f"p-{i}", f"T{i}", d, d, r],
            )
    finally:
        conn.close()

    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review("r-recov")
    assert result.status == service_result.STATUS_SUCCESS

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        lines = zf.read("drawdowns.csv").decode().strip().splitlines()

    assert len(lines) == 2, f"expected header + 1 data row, got: {lines}"
    parts = lines[1].split(",")
    cfg, peak_date_str, trough_date_str, dd_pct = parts[0], parts[1], parts[2], float(parts[3])

    assert cfg == "cfg-r"
    assert peak_date_str == str(base)          # peak established at d0 (+20%)
    assert trough_date_str == str(date.fromordinal(base.toordinal() + 3))  # d3 (-30%)
    assert dd_pct < 0
    # Expected: 1.2 * 0.9 * 1.05 * 0.7 = 0.7938; dd = 0.7938/1.2 - 1 ≈ -0.3385
    assert abs(dd_pct - (-33.85)) < 0.01, f"unexpected dd_pct={dd_pct}"


# =========================================================================== #
# Fix I — worst drawdown prompt identifies most negative.
# =========================================================================== #
def test_worst_drawdown_prompt_most_negative(env):
    """cfg-1=-5, cfg-2=-12; prompt must identify cfg-2 as worst (most negative)."""
    seed_simulation(env["paths"], negative=True)  # seeds cfg-1=-5, cfg-2=-12
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(SIM_RUN_ID)
    assert result.status == service_result.STATUS_SUCCESS

    conn = _conn(env["paths"]["simulation"])
    try:
        row = conn.execute("SELECT prompt_text FROM sim_ai_reviews").fetchone()
    finally:
        conn.close()

    prompt = row[0]
    # "Worst max drawdown" line must name cfg-2 (dd=-12 is more negative than -5).
    worst_line = next(l for l in prompt.splitlines() if "Worst max drawdown" in l)
    assert "cfg-2" in worst_line, f"expected cfg-2 in worst line: {worst_line!r}"


# =========================================================================== #
# Fix J — simulation DB-write-after-ZIP failure.
# =========================================================================== #
def test_simulation_review_write_failure_returns_failed_zip_remains(env):
    seed_simulation(env["paths"])
    engine = ExportPackageEngine(db_manager=FailingWriteDbManager(env["paths"]))
    result = engine.export_simulation_review(SIM_RUN_ID)
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["zip_path"] is not None
    assert Path(result.metadata["zip_path"]).exists()  # G-ZIP-CLEANUP: ZIP retained
    assert "review write failed" in result.metadata["error"]


# =========================================================================== #
# Other CSV correctness tests.
# =========================================================================== #
def test_score_buckets_every_populated_bucket(env):
    seed_simulation(env["paths"])  # step3 scores: 15,55,95 → buckets 10,50,90
                                   # step5 scores: 25,65     → buckets 20,60
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(SIM_RUN_ID)

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        lines = zf.read("score_buckets.csv").decode().strip().splitlines()

    data_rows = lines[1:]
    assert len(data_rows) == 5  # 3 step3 + 2 step5 distinct buckets
    assert any(r.startswith("step3,10,20") for r in data_rows)
    assert any(r.startswith("step3,50,60") for r in data_rows)
    assert any(r.startswith("step3,90,100") for r in data_rows)
    assert any(r.startswith("step5,20,30") for r in data_rows)
    assert any(r.startswith("step5,60,70") for r in data_rows)


def test_drawdowns_header_only_for_all_positive(env):
    seed_simulation(env["paths"], negative=False)  # returns +8, +5 → no drawdown
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(SIM_RUN_ID)

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        lines = zf.read("drawdowns.csv").decode().strip().splitlines()
    assert len(lines) == 1  # header-only: all-positive curve has no drawdown


def test_regime_performance_header_only(env):
    seed_simulation(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(SIM_RUN_ID)

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        lines = zf.read("regime_performance.csv").decode().strip().splitlines()
    assert lines == ["market_regime,horizon_bd,mean_return_pct,n"]  # G-REGIME-SOURCE


def test_setup_performance_has_horizon_rows(env):
    seed_simulation(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(SIM_RUN_ID)

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        lines = zf.read("setup_performance.csv").decode().strip().splitlines()
    assert lines[0] == "setup_type,horizon_bd,mean_return_pct,n"
    assert any(l.startswith("breakout,") for l in lines[1:])


def test_unresolved_outcomes_contains_partial(env):
    seed_simulation(env["paths"])
    result = ExportPackageEngine(db_manager=env["db"]).export_simulation_review(SIM_RUN_ID)

    with zipfile.ZipFile(Path(result.metadata["zip_path"])) as zf:
        text = zf.read("unresolved_outcomes.csv").decode()
    assert "partial" in text


# =========================================================================== #
# Static boundary scans.
# =========================================================================== #
def _execute_sql_strings(tree: ast.AST) -> list[str]:
    """Collect string-literal SQL passed to any ``.execute(...)`` call."""
    sql: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                sql.append(first.value)
    return sql


def test_no_forbidden_constructs_in_module():
    src = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "duckdb"
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root != "duckdb"
            assert "providers" not in (node.module or "")
        # No real print() calls (docstrings may mention the word).
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"

    for sql in _execute_sql_strings(tree):
        upper = sql.upper()
        for token in ("CREATE TABLE", "CREATE VIEW", "CREATE INDEX",
                      "DROP ", "ALTER ", "ATTACH "):
            assert token not in upper, f"forbidden SQL token in execute(): {token}"


def test_only_allowed_tables_mutated():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    insert_targets = []
    for sql in _execute_sql_strings(tree):
        upper = sql.upper()
        if "INSERT INTO" in upper:
            insert_targets.append(
                upper.split("INSERT INTO", 1)[1].strip().split()[0]
            )
    assert insert_targets, "expected at least one INSERT in the module"
    assert set(insert_targets) <= {"AI_REVIEWS", "SIM_AI_REVIEWS"}
