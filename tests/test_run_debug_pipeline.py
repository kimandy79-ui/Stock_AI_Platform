"""Tests for ``tools/run_debug_pipeline.py``.

All tests run fully offline: ``_ensure_debug_db`` and ``_build_controller``
are monkeypatched so no DuckDB, file system, or network access occurs.

Covers:
- ``--preset`` / ``--sample-count`` / ``--setups`` argument parsing
- ``--setups`` maps to ``setup_types`` in controller.run_preset (not strategy_names)
- Legacy ``--strategies`` is NOT accepted (removed in setup-mode migration)
- ``pipeline_sanity --sample-count 50`` succeeds (the exact failing command)
- Exit code 0 on success, 1 on failure
- ``_SETUP_TYPE_CHOICES`` contains all four canonical setup types
- No reference to aggressive/normal/conservative in CLI source
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure repo root is on path for tool import.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tools.run_debug_pipeline as cli
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Fake controller.
# --------------------------------------------------------------------------- #
class _FakeController:
    """Records run_preset calls; returns a configurable result."""

    def __init__(self, result: ServiceResult | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result or ServiceResult(
            service_result.STATUS_SUCCESS,
            "debug-test",
            metadata={"debug": {"preset": "pipeline_sanity", "db_role": "debug",
                                "executed_steps": [], "setup_types": ["breakout"]}},
        )

    def run_preset(self, preset_name, run_date, *, sample_count=None,
                   setup_types=None, force_rerun=True, run_id=None, **kw):
        self.calls.append({
            "preset_name": preset_name,
            "run_date": run_date,
            "sample_count": sample_count,
            "setup_types": setup_types,
            "force_rerun": force_rerun,
        })
        return self._result


@pytest.fixture(autouse=True)
def _patch_io(monkeypatch):
    """Always skip real DB init and always use the fake controller factory."""
    monkeypatch.setattr(cli, "_ensure_debug_db", lambda: None)
    yield


def _make_run(controller: _FakeController, monkeypatch):
    monkeypatch.setattr(cli, "_build_controller", lambda: controller)


# --------------------------------------------------------------------------- #
# Argument parsing.
# --------------------------------------------------------------------------- #
class TestArgParsing:
    def test_default_preset_is_fast_smoke_test(self):
        args = cli._parse_args([])
        assert args.preset == "fast_smoke_test"

    def test_preset_pipeline_sanity(self):
        args = cli._parse_args(["--preset", "pipeline_sanity"])
        assert args.preset == "pipeline_sanity"

    def test_sample_count_forwarded(self):
        args = cli._parse_args(["--preset", "pipeline_sanity", "--sample-count", "50"])
        assert args.sample_count == 50

    def test_setups_forwarded(self):
        args = cli._parse_args(["--setups", "breakout", "pullback"])
        assert args.setups == ["breakout", "pullback"]

    def test_setups_default_is_none(self):
        args = cli._parse_args([])
        assert args.setups is None

    def test_invalid_preset_raises(self):
        with pytest.raises(SystemExit):
            cli._parse_args(["--preset", "nonexistent_preset"])

    def test_invalid_setup_type_raises(self):
        with pytest.raises(SystemExit):
            cli._parse_args(["--setups", "aggressive"])  # legacy name rejected

    def test_strategies_flag_not_accepted(self):
        """--strategies must not exist; only --setups is valid."""
        with pytest.raises(SystemExit):
            cli._parse_args(["--strategies", "normal"])


# --------------------------------------------------------------------------- #
# Canonical setup type choices.
# --------------------------------------------------------------------------- #
class TestSetupTypeChoices:
    def test_all_four_canonical_types_present(self):
        assert set(cli._SETUP_TYPE_CHOICES) == {
            "breakout", "pullback", "trend_continuation", "consolidation_base"
        }

    def test_no_legacy_names_in_choices(self):
        for legacy in ("normal", "aggressive", "conservative"):
            assert legacy not in cli._SETUP_TYPE_CHOICES

    def test_no_legacy_names_in_source(self):
        src = Path(cli.__file__).read_text(encoding="utf-8")
        for term in ("--strategies", "aggressive", "conservative",
                     "strategy_names", "strategy_configs"):
            assert term not in src, f"Found legacy term '{term}' in run_debug_pipeline.py"


# --------------------------------------------------------------------------- #
# The exact failing command: pipeline_sanity --sample-count 50
# --------------------------------------------------------------------------- #
class TestPipelineSanityCommand:
    def test_pipeline_sanity_sample_count_50_succeeds(self, monkeypatch):
        ctrl = _FakeController()
        _make_run(ctrl, monkeypatch)
        exit_code = cli.main(["--preset", "pipeline_sanity", "--sample-count", "50"])
        assert exit_code == 0
        assert len(ctrl.calls) == 1
        call = ctrl.calls[0]
        assert call["preset_name"] == "pipeline_sanity"
        assert call["sample_count"] == 50

    def test_pipeline_sanity_passes_setup_types_none_by_default(self, monkeypatch):
        """When --setups not given, setup_types=None so all four presets are used."""
        ctrl = _FakeController()
        _make_run(ctrl, monkeypatch)
        cli.main(["--preset", "pipeline_sanity", "--sample-count", "50"])
        assert ctrl.calls[0]["setup_types"] is None

    def test_pipeline_sanity_with_setups_filter(self, monkeypatch):
        ctrl = _FakeController()
        _make_run(ctrl, monkeypatch)
        cli.main([
            "--preset", "pipeline_sanity",
            "--sample-count", "50",
            "--setups", "breakout", "consolidation_base",
        ])
        assert ctrl.calls[0]["setup_types"] == ["breakout", "consolidation_base"]


# --------------------------------------------------------------------------- #
# Exit codes.
# --------------------------------------------------------------------------- #
class TestExitCodes:
    def test_success_returns_0(self, monkeypatch):
        _make_run(_FakeController(), monkeypatch)
        assert cli.main(["--preset", "fast_smoke_test"]) == 0

    def test_failure_returns_1(self, monkeypatch):
        ctrl = _FakeController(
            result=ServiceResult(service_result.STATUS_FAILED, "d", errors=["bang"])
        )
        _make_run(ctrl, monkeypatch)
        assert cli.main(["--preset", "fast_smoke_test"]) == 1

    def test_db_init_failure_returns_1(self, monkeypatch):
        monkeypatch.setattr(cli, "_ensure_debug_db", lambda: "schema failed")
        monkeypatch.setattr(cli, "_build_controller", lambda: _FakeController())
        assert cli.main([]) == 1

    def test_controller_exception_returns_1(self, monkeypatch):
        class _ExplodingController:
            def run_preset(self, *a, **kw):
                raise RuntimeError("boom")
        monkeypatch.setattr(cli, "_build_controller", lambda: _ExplodingController())
        assert cli.main([]) == 1

    def test_success_with_warnings_returns_0(self, monkeypatch):
        ctrl = _FakeController(
            result=ServiceResult(
                service_result.STATUS_SUCCESS_WITH_WARNINGS,
                "d",
                warnings=["minor issue"],
                metadata={"debug": {}},
            )
        )
        _make_run(ctrl, monkeypatch)
        assert cli.main([]) == 0


# --------------------------------------------------------------------------- #
# setup_types forwarding.
# --------------------------------------------------------------------------- #
class TestSetupTypesForwarding:
    def test_single_setup_type_forwarded(self, monkeypatch):
        ctrl = _FakeController()
        _make_run(ctrl, monkeypatch)
        cli.main(["--setups", "pullback"])
        assert ctrl.calls[0]["setup_types"] == ["pullback"]

    def test_all_four_setup_types_accepted(self, monkeypatch):
        ctrl = _FakeController()
        _make_run(ctrl, monkeypatch)
        cli.main([
            "--setups",
            "breakout", "pullback", "trend_continuation", "consolidation_base",
        ])
        assert set(ctrl.calls[0]["setup_types"]) == {
            "breakout", "pullback", "trend_continuation", "consolidation_base"
        }

    def test_consolidation_base_accepted(self, monkeypatch):
        """consolidation_base (not conservative_consolidation) must be accepted."""
        ctrl = _FakeController()
        _make_run(ctrl, monkeypatch)
        exit_code = cli.main(["--setups", "consolidation_base"])
        assert exit_code == 0
        assert ctrl.calls[0]["setup_types"] == ["consolidation_base"]


# --------------------------------------------------------------------------- #
# Orchestrator constructor compatibility
# --------------------------------------------------------------------------- #
class TestOrchestratorKwargCompatibility:
    """Debug mode must only pass kwargs that PipelineOrchestrator.__init__ accepts."""

    def test_no_legacy_kwargs_passed_to_orchestrator(self, monkeypatch):
        """engine_kwargs must not contain screening_engine or analysis_engine."""
        import inspect
        from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
        valid_params = set(inspect.signature(PipelineOrchestrator.__init__).parameters)

        captured_kwargs: dict = {}

        class _CapturingOrchestrator:
            def __init__(self, **kw):
                captured_kwargs.update(kw)
            def run(self, **kw):
                return ServiceResult(service_result.STATUS_SUCCESS, "r", metadata={})

        from app.services.debug.debug_mode import DebugModeController, DebugRunPlan, STEP_NAMES
        from app.utils.service_result import ServiceResult
        from app.utils import service_result as sr
        from datetime import date

        ctrl = DebugModeController(
            db_manager=object(),
            provider=None,  # not used for pipeline_sanity (no scoping needed)
            orchestrator_factory=_CapturingOrchestrator,
            setup_configs={
                "breakout": {}, "pullback": {}, "trend_continuation": {}, "consolidation_base": {}
            },
            scoped_feature_engine_factory=lambda db, t: object(),
            scoped_screening_engine_factory=lambda db, t: object(),
        )

        # Build engine kwargs for pipeline_sanity (start=STEP_NAMES[0], end=step5_proposals)
        from app.services.debug.debug_mode import DebugModeController, DebugRunPlan, STEP_NAMES
        plan = DebugRunPlan(
            run_date=date(2026, 6, 22),
            sample_count=50,
            start_step=STEP_NAMES[0],
            end_step="step5_proposals",
            setup_types=("breakout", "pullback", "trend_continuation", "consolidation_base"),
        )
        engine_kwargs = DebugModeController._build_engine_kwargs(plan, None, object(), lambda db, t: object(), lambda db, t: object())

        # Every key in engine_kwargs must be a valid PipelineOrchestrator param
        invalid = [k for k in engine_kwargs if k not in valid_params]
        assert not invalid, (
            f"engine_kwargs has keys not accepted by PipelineOrchestrator.__init__: {invalid}. "
            f"Valid params: {sorted(valid_params)}"
        )

        # Specifically forbidden legacy names
        assert "screening_engine" not in engine_kwargs
        assert "analysis_engine" not in engine_kwargs

    def test_eligibility_engine_kwarg_is_valid_orchestrator_param(self):
        import inspect
        from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
        valid = set(inspect.signature(PipelineOrchestrator.__init__).parameters)
        assert "eligibility_engine" in valid
        assert "setup_validation_engine" in valid
        assert "screening_engine" not in valid
        assert "analysis_engine" not in valid

    def test_pipeline_sanity_50_via_fake_controller(self, monkeypatch):
        """Exact reproduction of the failing command path."""
        ctrl = _FakeController()
        _make_run(ctrl, monkeypatch)
        exit_code = cli.main(["--preset", "pipeline_sanity", "--sample-count", "50"])
        assert exit_code == 0
        call = ctrl.calls[0]
        assert call["preset_name"] == "pipeline_sanity"
        assert call["sample_count"] == 50
        assert call["setup_types"] is None  # no --setups flag → None → all four types
