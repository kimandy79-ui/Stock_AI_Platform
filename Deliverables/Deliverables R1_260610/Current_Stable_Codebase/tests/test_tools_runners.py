"""Tests for the operator runner scripts under ``tools/``.

Fully offline. Two layers of coverage:

* **Logic** — each runner's success/failure -> exit-code mapping and its
  delegation are verified by monkeypatching the runner's build/apply seam with
  an in-process fake (no real DuckDB, engines, provider, or network).
* **Integration (init only)** — ``init_prod_db`` is run against a temp prod DB
  (the ``tmp_db_paths`` pattern from the Module 07 suite) to prove it really
  applies the schema via the Module 03 manager, without touching the real
  ``data/duckdb/prod.duckdb``.
* **Isolation** — static-source checks prove the debug runner never targets
  ``prod`` and never reaches for the orchestrator directly, so it cannot write
  to ``prod.duckdb``.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.utils import service_result
from app.utils.service_result import ServiceResult

from tools import (
    init_debug_db,
    init_prod_db,
    run_debug_pipeline,
    run_prod_pipeline,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _ok(metadata: dict | None = None, warnings: list[str] | None = None) -> ServiceResult:
    status = (
        service_result.STATUS_SUCCESS_WITH_WARNINGS
        if warnings
        else service_result.STATUS_SUCCESS
    )
    return ServiceResult(
        status=status,
        run_id="rid-test",
        rows_processed=1,
        warnings=warnings or [],
        errors=[],
        metadata=metadata or {},
    )


def _failed(errors: list[str] | None = None, metadata: dict | None = None) -> ServiceResult:
    return ServiceResult(
        status=service_result.STATUS_FAILED,
        run_id="rid-test",
        rows_processed=0,
        warnings=[],
        errors=errors or ["boom"],
        metadata=metadata or {},
    )


class _RecordingOrchestrator:
    def __init__(self, result: ServiceResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def run(self, **kwargs) -> ServiceResult:  # noqa: ANN003
        self.calls.append(kwargs)
        return self.result


class _RecordingController:
    def __init__(self, result: ServiceResult) -> None:
        self.result = result
        self.preset_calls: list[tuple] = []

    def run_preset(self, preset_name, run_date, **kwargs) -> ServiceResult:  # noqa: ANN001, ANN003
        self.preset_calls.append((preset_name, run_date, kwargs))
        return self.result

    # If the runner ever tried to drive the orchestrator directly, this would
    # be called — it must not be.
    def run(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("debug runner must not call controller.run() directly")


@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect DuckDB settings paths into ``tmp_path`` (no real DB touched)."""
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    return {"prod": prod, "debug": debug, "simulation": simulation}


# --------------------------------------------------------------------------- #
# init_prod_db
# --------------------------------------------------------------------------- #
def test_init_prod_db_success_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        init_prod_db,
        "_apply_prod_schema",
        lambda: _ok({"tables_created": ["a", "b"], "schema_version": "schema_v01"}),
    )
    assert init_prod_db.main([]) == 0


def test_init_prod_db_failure_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_prod_db, "_apply_prod_schema", lambda: _failed(["bad"]))
    assert init_prod_db.main([]) == 1


def test_init_prod_db_exception_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> ServiceResult:
        raise RuntimeError("disk full")

    monkeypatch.setattr(init_prod_db, "_apply_prod_schema", _boom)
    assert init_prod_db.main([]) == 1


def test_init_prod_db_real_schema_on_tmp_db(tmp_db_paths: dict[str, Path]) -> None:
    """Integration: runner applies the real schema to a temp prod DB."""
    assert init_prod_db.main([]) == 0
    assert tmp_db_paths["prod"].exists()
    # Idempotent second call still succeeds (M02 §6).
    assert init_prod_db.main([]) == 0
    # Schema actually present.
    conn = dbm.connect("prod", read_only=True)
    try:
        names = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    finally:
        conn.close()
    assert "pipeline_runs" in names and "schema_versions" in names


# --------------------------------------------------------------------------- #
# run_prod_pipeline
# --------------------------------------------------------------------------- #
def test_run_prod_success_and_db_role(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _RecordingOrchestrator(_ok({"run_id": "x", "steps_completed": [1, 2]}))
    monkeypatch.setattr(run_prod_pipeline, "_build_orchestrator", lambda: orch)
    assert run_prod_pipeline.main(["--date", "2025-06-02"]) == 0
    assert orch.calls[0]["db_role"] == "prod"
    assert orch.calls[0]["run_date"] == date(2025, 6, 2)
    assert orch.calls[0]["run_type"] == "manual"


def test_run_prod_warnings_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _RecordingOrchestrator(_ok({"steps_completed": []}, warnings=["w"]))
    monkeypatch.setattr(run_prod_pipeline, "_build_orchestrator", lambda: orch)
    assert run_prod_pipeline.main([]) == 0


def test_run_prod_failure_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _RecordingOrchestrator(_failed(["lock held"], {"failed_step": "validation"}))
    monkeypatch.setattr(run_prod_pipeline, "_build_orchestrator", lambda: orch)
    assert run_prod_pipeline.main([]) == 1


def test_run_prod_forwards_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _RecordingOrchestrator(_ok())
    monkeypatch.setattr(run_prod_pipeline, "_build_orchestrator", lambda: orch)
    run_prod_pipeline.main(
        ["--run-type", "force_rerun", "--force-rerun", "--resume-from", "validation"]
    )
    call = orch.calls[0]
    assert call["run_type"] == "force_rerun"
    assert call["force_rerun"] is True
    assert call["resume_from"] == "validation"


# --------------------------------------------------------------------------- #
# run_debug_pipeline
# --------------------------------------------------------------------------- #
def test_run_debug_success_uses_run_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    ctrl = _RecordingController(_ok({"debug": {"preset": "fast_smoke_test"}}))
    monkeypatch.setattr(run_debug_pipeline, "_build_controller", lambda: ctrl)
    monkeypatch.setattr(run_debug_pipeline, "_ensure_debug_db", lambda: None)
    assert run_debug_pipeline.main(["--preset", "fast_smoke_test"]) == 0
    assert ctrl.preset_calls[0][0] == "fast_smoke_test"


def test_run_debug_forwards_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    ctrl = _RecordingController(_ok({"debug": {}}))
    monkeypatch.setattr(run_debug_pipeline, "_build_controller", lambda: ctrl)
    monkeypatch.setattr(run_debug_pipeline, "_ensure_debug_db", lambda: None)
    run_debug_pipeline.main(
        ["--date", "2025-06-02", "--sample-count", "12", "--strategies", "normal", "aggressive"]
    )
    _, run_dt, kwargs = ctrl.preset_calls[0]
    assert run_dt == date(2025, 6, 2)
    assert kwargs["sample_count"] == 12
    assert kwargs["strategy_names"] == ["normal", "aggressive"]


def test_run_debug_failure_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    ctrl = _RecordingController(_failed(["nope"]))
    monkeypatch.setattr(run_debug_pipeline, "_build_controller", lambda: ctrl)
    monkeypatch.setattr(run_debug_pipeline, "_ensure_debug_db", lambda: None)
    assert run_debug_pipeline.main([]) == 1


def test_run_debug_init_failure_aborts_before_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If debug-DB init fails, the controller is never built and exit is 1."""
    monkeypatch.setattr(run_debug_pipeline, "_ensure_debug_db", lambda: "init boom")

    def _no_build() -> object:
        raise AssertionError("controller must not be built when init fails")

    monkeypatch.setattr(run_debug_pipeline, "_build_controller", _no_build)
    assert run_debug_pipeline.main([]) == 1


def test_run_debug_help_smoke() -> None:
    """`run_debug_pipeline.py --help` exits 0 (script-path smoke test)."""
    with pytest.raises(SystemExit) as exc:
        run_debug_pipeline.main(["--help"])
    assert exc.value.code == 0


def test_run_debug_never_targets_prod_source() -> None:
    """Static guard: debug runner has no 'prod' db_role and no orchestrator import."""
    src = inspect.getsource(run_debug_pipeline)
    # No prod/simulation db_role literal is ever passed.
    assert '"prod"' not in src and "'prod'" not in src
    assert '"simulation"' not in src and "'simulation'" not in src
    # The runner never reaches for the orchestrator directly; the controller
    # (which is debug-only) is the sole entry point.
    assert "PipelineOrchestrator" not in src


# --------------------------------------------------------------------------- #
# Debug DB auto-initialization (the bug fix)
# --------------------------------------------------------------------------- #
def test_ensure_debug_db_creates_when_missing(tmp_db_paths: dict[str, Path]) -> None:
    """`_ensure_debug_db` applies the real debug schema when the file is absent."""
    assert not tmp_db_paths["debug"].exists()
    assert run_debug_pipeline._ensure_debug_db() is None
    assert tmp_db_paths["debug"].exists()
    # Idempotent: second call is a no-op (file already present), still None.
    assert run_debug_pipeline._ensure_debug_db() is None
    # Debug-only: prod DB must never be created by this path.
    assert not tmp_db_paths["prod"].exists()


def test_first_time_debug_run_starts_without_existing_db(
    tmp_db_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the bug: a first-ever debug run (no debug.duckdb) can start.

    The real ``_ensure_debug_db`` runs against the temp paths and creates the
    schema; the controller is faked so the heavy pipeline is not executed, but
    the proof is that initialization happens *before* ``run_preset`` and the
    debug DB now exists.
    """
    assert not tmp_db_paths["debug"].exists()
    ctrl = _RecordingController(_ok({"debug": {"preset": "pipeline_sanity"}}))
    monkeypatch.setattr(run_debug_pipeline, "_build_controller", lambda: ctrl)

    rc = run_debug_pipeline.main(["--preset", "pipeline_sanity", "--sample-count", "50"])

    assert rc == 0
    assert tmp_db_paths["debug"].exists()  # created before the controller ran
    assert ctrl.preset_calls and ctrl.preset_calls[0][0] == "pipeline_sanity"
    assert not tmp_db_paths["prod"].exists()  # prod untouched


def test_init_debug_db_real_schema_on_tmp_db(tmp_db_paths: dict[str, Path]) -> None:
    assert not tmp_db_paths["debug"].exists()
    assert init_debug_db.main([]) == 0
    assert tmp_db_paths["debug"].exists()
    assert init_debug_db.main([]) == 0  # idempotent
    assert not tmp_db_paths["prod"].exists()


def test_init_debug_db_failure_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_debug_db, "_apply_debug_schema", lambda: _failed(["bad"]))
    assert init_debug_db.main([]) == 1
