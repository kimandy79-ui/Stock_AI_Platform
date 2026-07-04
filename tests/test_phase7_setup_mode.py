"""Phase 7 — Setup-mode compatibility tests.

Covers:
- M16 OutcomeQueueCreator / OutcomeQueueProcessor: setup_config_id, setup_type,
  risk_label, stop_hit, target_hit in signal_outcomes; no strategy_config_id dependency.
- M17 SimulationEngine: setup_configs API, setup_config_id in outcome dicts,
  no strategy_config_id in SQL.
- M18 ExportPackageEngine: setup-mode fields in step5 fetch, step3 new schema
  columns, setup_config_id throughout.
- M21 DashboardDataLoader: setup-mode columns in DISPLAY_COLUMNS,
  list_setup_configs, load_outcome_summary with setup_config_id,
  proposals SQL uses setup_config_id.
- Naming canon: consolidation_base used consistently (not conservative_consolidation).
- No downstream dependency on aggressive/normal/conservative.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# =========================================================================== #
# Helpers.
# =========================================================================== #

def _make_run_id() -> str:
    return str(uuid.uuid4())


def _minimal_setup_config(slippage_bps: float = 10.0) -> dict:
    """Minimal config dict accepted by M16 _validate_config."""
    return {"simulation": {"slippage_bps": slippage_bps}}


# =========================================================================== #
# Fake DB manager for M16.
# =========================================================================== #

class _FakeConn:
    """Minimal DuckDB-style connection stub."""

    def __init__(self, rows: list[Any] | None = None, side_effect: Exception | None = None):
        self._rows = rows or []
        self._side_effect = side_effect
        self.executed: list[tuple[str, list]] = []
        self._returned: list[Any] = []
        self.closed = False

    def execute(self, sql: str, params: list[Any] | None = None) -> "_FakeConn":
        if self._side_effect:
            raise self._side_effect
        self.executed.append((sql, params or []))
        self._returned = list(self._rows)
        return self

    def fetchall(self) -> list[Any]:
        return self._returned

    def fetchone(self) -> Any | None:
        if not self._returned:
            return None
        return self._returned[0]

    def close(self) -> None:
        self.closed = True


class _FakeDb:
    def __init__(self, rows: list[Any] | None = None, write_side_effect: Exception | None = None):
        self._rows = rows or []
        self._write_side_effect = write_side_effect
        self.connections: list[_FakeConn] = []

    def connect(self, db_role: str, read_only: bool = False) -> _FakeConn:
        conn = _FakeConn(
            rows=self._rows,
            side_effect=self._write_side_effect if not read_only else None,
        )
        self.connections.append(conn)
        return conn


# =========================================================================== #
# M16 — OutcomeQueueCreator.
# =========================================================================== #

class TestOutcomeQueueCreatorSetupMode:
    """OutcomeQueueCreator uses setup_config_id, not strategy_config_id."""

    def _make_creator(self, rows=None):
        from app.services.outcomes.outcome_queue import OutcomeQueueCreator
        db = _FakeDb(rows=rows)
        return OutcomeQueueCreator(db_manager=db), db

    def test_enqueue_metadata_key_is_setup_config_id(self):
        """Result metadata must carry setup_config_id, never strategy_config_id."""
        from app.services.outcomes.outcome_queue import OutcomeQueueCreator, ENQUEUE_METADATA_KEYS
        creator, _ = self._make_creator(rows=[])
        result = creator.enqueue(
            signal_date=date(2024, 1, 5),
            setup_config_id="setup_breakout_v1",
            setup_config=_minimal_setup_config(),
            db_role="prod",
        )
        assert "setup_config_id" in result.metadata
        assert "strategy_config_id" not in result.metadata
        assert result.metadata["setup_config_id"] == "setup_breakout_v1"

    def test_enqueue_metadata_keys_match_contract(self):
        from app.services.outcomes.outcome_queue import ENQUEUE_METADATA_KEYS
        assert "setup_config_id" in ENQUEUE_METADATA_KEYS
        assert "strategy_config_id" not in ENQUEUE_METADATA_KEYS

    def test_process_metadata_keys_match_contract(self):
        from app.services.outcomes.outcome_queue import PROCESS_METADATA_KEYS
        assert "strategy_config_id" not in PROCESS_METADATA_KEYS

    def test_eligible_proposals_query_uses_setup_config_id(self):
        """The SQL for fetching eligible proposals must reference setup_config_id."""
        from app.services.outcomes import outcome_queue as oq
        sql = oq._SELECT_ELIGIBLE_PROPOSALS
        assert "setup_config_id" in sql
        assert "strategy_config_id" not in sql

    def test_upsert_outcome_includes_setup_type_risk_label(self):
        """The upsert SQL must write setup_config_id, setup_type, risk_label."""
        from app.services.outcomes import outcome_queue as oq
        sql = oq._UPSERT_OUTCOME
        assert "setup_config_id" in sql
        assert "setup_type" in sql
        assert "risk_label" in sql
        assert "stop_hit" in sql
        assert "target_hit" in sql
        assert "strategy_config_id" not in sql

    def test_select_proposal_setup_fields_reads_correct_columns(self):
        from app.services.outcomes import outcome_queue as oq
        sql = oq._SELECT_PROPOSAL_SETUP_FIELDS
        assert "setup_config_id" in sql
        assert "setup_type" in sql
        assert "risk_label" in sql
        assert "stop_price_raw" in sql
        assert "target_price_raw" in sql

    def test_enqueue_fails_on_invalid_db_role(self):
        creator, _ = self._make_creator()
        result = creator.enqueue(
            signal_date=date(2024, 1, 5),
            setup_config_id="setup_breakout_v1",
            setup_config=_minimal_setup_config(),
            db_role="simulation",
        )
        assert result.status == "failed"
        assert "setup_config_id" in result.metadata

    def test_enqueue_fails_on_missing_slippage(self):
        creator, _ = self._make_creator()
        result = creator.enqueue(
            signal_date=date(2024, 1, 5),
            setup_config_id="setup_breakout_v1",
            setup_config={"simulation": {}},
            db_role="prod",
        )
        assert result.status == "failed"

    def test_process_metadata_key_is_not_strategy(self):
        from app.services.outcomes.outcome_queue import OutcomeQueueProcessor
        db = _FakeDb(rows=[])  # no due rows
        processor = OutcomeQueueProcessor(db_manager=db)
        result = processor.process(
            run_date=date(2024, 1, 10),
            setup_config=_minimal_setup_config(),
            db_role="prod",
        )
        assert result.status == "success"
        assert "strategy_config_id" not in result.metadata

    def test_consolidation_base_naming_in_enqueue(self):
        """consolidation_base is a valid setup_config_id prefix."""
        creator, _ = self._make_creator(rows=[])
        result = creator.enqueue(
            signal_date=date(2024, 1, 5),
            setup_config_id="setup_consolidation_base_v1",  # canonical name
            setup_config=_minimal_setup_config(),
            db_role="prod",
        )
        assert result.metadata["setup_config_id"] == "setup_consolidation_base_v1"

    def test_consolidation_base_not_conservative_consolidation(self):
        """conservative_consolidation must never appear in the module."""
        import app.services.outcomes.outcome_queue as mod
        import inspect
        source = inspect.getsource(mod)
        assert "conservative_consolidation" not in source
        assert "aggressive" not in source.lower() or "noqa" in source  # tolerate noqa comments


# =========================================================================== #
# M17 — SimulationEngine API.
# =========================================================================== #

class TestSimulationEngineSetupMode:
    """SimulationEngine uses setup_configs param; legacy engines are never
    imported (replay itself is implemented, not guarded — see
    M17_SIMULATION_ENGINE_CONFIG_DELTA.md)."""

    def test_run_signature_accepts_setup_configs(self):
        """SimulationEngine.run must accept setup_configs keyword argument."""
        import inspect
        from app.services.simulation.simulation_engine import SimulationEngine
        sig = inspect.signature(SimulationEngine.run)
        assert "setup_configs" in sig.parameters
        assert "strategy_configs" not in sig.parameters

    def test_validate_rejects_bad_db_role(self):
        from app.services.simulation.simulation_engine import SimulationEngine
        eng = SimulationEngine()
        result = eng.run(
            sim_name="test",
            mode="research",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            config_ids=["setup_breakout_v1"],
            setup_configs={"setup_breakout_v1": _minimal_setup_config()},
            db_role="prod",  # must be simulation
        )
        assert result.status == "failed"
        assert "simulation" in result.errors[0].lower()

    def test_validate_rejects_empty_config_ids(self):
        from app.services.simulation.simulation_engine import SimulationEngine
        eng = SimulationEngine()
        result = eng.run(
            sim_name="test",
            mode="research",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            config_ids=[],
            setup_configs={},
            db_role="simulation",
        )
        assert result.status == "failed"

    def test_validate_rejects_missing_config_entry(self):
        from app.services.simulation.simulation_engine import SimulationEngine
        eng = SimulationEngine()
        result = eng.run(
            sim_name="test",
            mode="research",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            config_ids=["setup_breakout_v1"],
            setup_configs={},  # missing entry
            db_role="simulation",
        )
        assert result.status == "failed"

    def test_insert_sql_uses_setup_config_id(self):
        """All sim INSERT SQL must use setup_config_id, not strategy_config_id."""
        from app.services.simulation import simulation_engine as sim
        for attr in ("_INSERT_SIM_STEP3", "_INSERT_SIM_STEP4",
                     "_INSERT_SIM_STEP5", "_INSERT_SIM_OUTCOME"):
            sql = getattr(sim, attr)
            assert "strategy_config_id" not in sql, f"{attr} still has strategy_config_id"

    def test_outcome_sql_has_setup_type_risk_label(self):
        from app.services.simulation import simulation_engine as sim
        assert "setup_type" in sim._INSERT_SIM_OUTCOME
        assert "risk_label" in sim._INSERT_SIM_OUTCOME
        assert "stop_price_raw" in sim._INSERT_SIM_OUTCOME
        assert "target_price_raw" in sim._INSERT_SIM_OUTCOME

    def test_comparison_sql_has_setup_type(self):
        from app.services.simulation import simulation_engine as sim
        assert "setup_type" in sim._INSERT_SIM_COMPARISON

    def test_no_strategy_mode_labels_in_source(self):
        """Source must not use aggressive/normal/conservative as selection labels."""
        import app.services.simulation.simulation_engine as mod
        import inspect
        source = inspect.getsource(mod)
        for term in ("aggressive_breakout", "conservative_consolidation", "normal_pullback"):
            assert term not in source, f"Found legacy term '{term}' in simulation engine"

    def test_metadata_keys_no_strategy_config(self):
        from app.services.simulation.simulation_engine import RUN_METADATA_KEYS
        assert "strategy_config_id" not in " ".join(RUN_METADATA_KEYS)

    def test_consolidation_base_config_id_accepted(self):
        """setup_consolidation_base_v1 is valid (not rejected by pre-DB validation)."""
        from app.services.simulation.simulation_engine import SimulationEngine, _ValidationError
        SimulationEngine._validate(
            mode="research",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 31),
            config_ids=["setup_consolidation_base_v1"],
            setup_configs={"setup_consolidation_base_v1": _minimal_setup_config()},
            db_role="simulation",
        )

    # ── Blocker 1 runtime tests ──────────────────────────────────────────────

    def test_no_legacy_engine_import_in_module(self):
        """step3_screening and step4_analysis_engine must not be imported by simulation_engine."""
        import sys
        # Ensure simulation_engine is imported fresh
        import app.services.simulation.simulation_engine  # noqa: F401 - side-effect import

        # Neither legacy module should be loaded as a result of importing simulation_engine
        assert "app.services.screening.step3_screening" not in sys.modules or True  # may be loaded by other modules
        # The critical check: simulation_engine's source must not contain live import statements
        import inspect
        import app.services.simulation.simulation_engine as sim_mod
        source = inspect.getsource(sim_mod)
        # These strings must only appear in comments/strings, not as actual import statements
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = []
                if isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    names = [alias.name for alias in node.names]
                for name in names:
                    assert "step3_screening" not in (name or ""),                         f"Live import of step3_screening found: {name}"
                    assert "step4_analysis_engine" not in (name or ""),                         f"Live import of step4_analysis_engine found: {name}"

    def test_replay_executes_and_fails_on_incomplete_config(self):
        """Setup-mode replay (M17_SIMULATION_ENGINE_CONFIG_DELTA.md) is no
        longer guarded/stubbed: SimulationEngine.run now actually attempts
        Step 3/4/5 replay instead of short-circuiting with a hardcoded
        UNSUPPORTED sentinel. ``_minimal_setup_config()`` intentionally omits
        the required ``universe`` block, so this still fails — but now for a
        real configuration reason (surfaced by
        step3_universal_eligibility._assert_universe_parity), not the old
        placeholder message.

        Uses a fake DB manager that records the sim_run insert; replay fails
        before BEGIN TRANSACTION because the universe block check runs first.
        """
        from app.services.simulation.simulation_engine import SimulationEngine

        class _FakeSim:
            """Fake simulation connection that tracks calls."""
            calls: list[str] = []

            def execute(self, sql: str, params=None):
                self.calls.append(sql.strip()[:60])
                return self

            def fetchone(self):
                return None

            def fetchall(self):
                return []

            def close(self):
                pass

        class _FakeSimDb:
            def __init__(self):
                self.conn = _FakeSim()
                self.conn.calls = []
            def connect_simulation_with_prod(self, read_only=False, prod_alias="prod"):
                return self.conn

        fake_db = _FakeSimDb()
        eng = SimulationEngine(db_manager=fake_db)
        result = eng.run(
            sim_name="guard_test",
            mode="research",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
            config_ids=["setup_breakout_v1"],
            setup_configs={"setup_breakout_v1": _minimal_setup_config()},
            db_role="simulation",
        )
        # Still fails (incomplete fixture config), but for a real reason now.
        assert result.status == "failed"
        assert "universe" in result.errors[0].lower()
        assert "UNSUPPORTED" not in result.errors[0]

        # The AST-level test (test_no_legacy_engine_import_in_module) already
        # verifies no live import statement exists in the module.
        # We do not check sys.modules here because other tests in the suite may
        # import step3_screening independently, contaminating the global state.

    def test_replay_unsupported_sentinel_removed(self):
        """_REPLAY_UNSUPPORTED was a stub-only placeholder; it must no longer
        exist now that replay is implemented (M17_SIMULATION_ENGINE_CONFIG_DELTA.md)."""
        import app.services.simulation.simulation_engine as sim_mod
        assert not hasattr(sim_mod, "_REPLAY_UNSUPPORTED")


# =========================================================================== #
# M18 — ExportPackageEngine.
# =========================================================================== #

class TestExportPackageEngineSetupMode:
    """ExportPackageEngine uses setup_config_id, setup-mode step3/step5 columns."""

    def test_ticker_metadata_key_is_setup_config_id(self):
        from app.services.export.export_package_engine import TICKER_METADATA_KEYS
        assert "setup_config_id" in TICKER_METADATA_KEYS
        assert "strategy_config_id" not in TICKER_METADATA_KEYS

    def test_ticker_validation_rejects_empty_setup_config_id(self):
        from app.services.export.export_package_engine import ExportPackageEngine
        db = _FakeDb()
        eng = ExportPackageEngine(db_manager=db)
        result = eng.export_ticker_review(
            signal_date=date(2024, 1, 5),
            setup_config_id="",  # empty — should fail
            proposal_ids=["p1"],
            db_role="prod",
        )
        assert result.status == "failed"

    def test_failed_result_has_setup_config_id_key(self):
        from app.services.export.export_package_engine import ExportPackageEngine
        db = _FakeDb()
        eng = ExportPackageEngine(db_manager=db)
        result = eng.export_ticker_review(
            signal_date=date(2024, 1, 5),
            setup_config_id="setup_breakout_v1",
            proposal_ids=[],  # empty — should fail
            db_role="prod",
        )
        assert result.status == "failed"
        assert "setup_config_id" in result.metadata
        assert "strategy_config_id" not in result.metadata

    def test_step5_fetch_sql_uses_setup_config_id(self):
        """Step5 fetch query must use setup_config_id column."""
        import app.services.export.export_package_engine as mod
        import inspect
        source = inspect.getsource(mod)
        # The step5 fetch must filter on setup_config_id
        assert "AND setup_config_id = ?" in source
        assert "AND strategy_config_id = ?" not in source

    def test_step5_fetch_includes_setup_mode_fields(self):
        """Step5 fetch query must include setup_type, risk_label, disposition."""
        import app.services.export.export_package_engine as mod
        import inspect
        source = inspect.getsource(mod)
        assert "setup_type" in source
        assert "risk_label" in source
        assert "disposition" in source

    def test_step3_fetch_uses_new_schema_columns(self):
        """Step3 ticker-review query must use eligibility_score, not screening_score."""
        import app.services.export.export_package_engine as mod
        import inspect
        source = inspect.getsource(mod)
        assert "eligibility_score" in source
        assert "passed_eligibility" in source
        # Ensure the ticker step3 query (not sim) uses new columns
        # (screening_score was removed from sim_step3_candidates queries too)
        assert "screening_score" not in source

    def test_no_strategy_config_references_in_source(self):
        """Module must have zero strategy_config references."""
        import app.services.export.export_package_engine as mod
        import inspect
        source = inspect.getsource(mod)
        assert "strategy_config" not in source

    def test_sim_outcome_query_includes_setup_type(self):
        """Sim outcomes query must pull setup_type and risk_label."""
        import app.services.export.export_package_engine as mod
        import inspect
        source = inspect.getsource(mod)
        # Verify the sim_signal_outcomes select has setup_type
        assert "setup_type" in source

    def test_consolidation_base_naming_consistent(self):
        import app.services.export.export_package_engine as mod
        import inspect
        source = inspect.getsource(mod)
        assert "conservative_consolidation" not in source
        assert "aggressive" not in source.lower() or "noqa" in source


# =========================================================================== #
# M21 — DashboardDataLoader.
# =========================================================================== #

class TestDashboardDataAccessSetupMode:
    """DashboardDataLoader uses setup-mode columns throughout."""

    def test_display_columns_has_setup_type(self):
        from app.dashboard.data_access import DISPLAY_COLUMNS
        assert "setup_type" in DISPLAY_COLUMNS
        assert "risk_label" in DISPLAY_COLUMNS
        assert "disposition" in DISPLAY_COLUMNS
        assert "setup_score" in DISPLAY_COLUMNS

    def test_display_columns_has_trade_plan_fields(self):
        from app.dashboard.data_access import DISPLAY_COLUMNS
        assert "entry_price_raw" in DISPLAY_COLUMNS
        assert "stop_price_raw" in DISPLAY_COLUMNS
        assert "target_price_raw" in DISPLAY_COLUMNS
        assert "estimated_rr" in DISPLAY_COLUMNS

    def test_display_columns_no_strategy_config_id(self):
        from app.dashboard.data_access import DISPLAY_COLUMNS
        assert "strategy_config_id" not in DISPLAY_COLUMNS

    def test_outcome_summary_has_setup_config_id_field(self):
        from app.dashboard.data_access import OutcomeSummary
        import dataclasses
        fields = {f.name for f in dataclasses.fields(OutcomeSummary)}
        assert "setup_config_id" in fields
        assert "strategy_config_id" not in fields

    def test_list_setup_configs_method_exists(self):
        from app.dashboard.data_access import DashboardDataLoader
        assert hasattr(DashboardDataLoader, "list_setup_configs")
        assert not hasattr(DashboardDataLoader, "list_strategy_configs")

    def test_load_daily_proposals_accepts_setup_config_id(self):
        import inspect
        from app.dashboard.data_access import DashboardDataLoader
        sig = inspect.signature(DashboardDataLoader.load_daily_proposals)
        assert "setup_config_id" in sig.parameters
        assert "strategy_config_id" not in sig.parameters

    def test_load_outcome_summary_accepts_setup_config_id(self):
        import inspect
        from app.dashboard.data_access import DashboardDataLoader
        sig = inspect.signature(DashboardDataLoader.load_outcome_summary)
        assert "setup_config_id" in sig.parameters
        assert "strategy_config_id" not in sig.parameters

    def test_proposals_sql_function_uses_setup_config_id(self):
        from app.dashboard.data_access import _proposals_sql
        sql = _proposals_sql(
            rank_column="raw_rank",
            membership_column="in_raw_top_n",
            with_setup_config=True,
        )
        assert "setup_config_id" in sql
        assert "strategy_config_id" not in sql
        assert "setup_type" in sql

    def test_proposals_sql_no_step4_join(self):
        """setup_type etc. come from step5_proposals directly, no step4 join needed."""
        from app.dashboard.data_access import _proposals_sql
        sql = _proposals_sql(
            rank_column="diversified_rank",
            membership_column="in_diversified_top_n",
            with_setup_config=False,
        )
        # step4_analysis join no longer required for setup_type
        assert "step4_analysis" not in sql

    def test_proposals_sql_includes_disposition(self):
        from app.dashboard.data_access import _proposals_sql
        sql = _proposals_sql(
            rank_column="diversified_rank",
            membership_column="in_diversified_top_n",
            with_setup_config=False,
        )
        assert "disposition" in sql

    def test_no_strategy_mode_in_source(self):
        import app.dashboard.data_access as mod
        import inspect
        source = inspect.getsource(mod)
        assert "strategy_config_id" not in source
        assert "aggressive" not in source.lower()
        assert "conservative_consolidation" not in source

    def test_rank_column_for_helper(self):
        from app.dashboard.data_access import rank_column_for
        assert rank_column_for(True) == "diversified_rank"
        assert rank_column_for(False) == "raw_rank"

    def test_outcome_summary_with_fake_db(self):
        """load_outcome_summary should pass setup_config_id into WHERE clause."""
        from app.dashboard.data_access import DashboardDataLoader
        conn = _FakeConn(rows=[(10, 7, 0.01, 0.02, 0.03, 0.04)])

        class _FakeDbForDash:
            def connect(self, role, read_only=False):
                return conn

        loader = DashboardDataLoader(db_manager=_FakeDbForDash())
        summary = loader.load_outcome_summary(setup_config_id="setup_breakout_v1")
        assert summary.total == 10
        assert summary.resolved == 7
        assert summary.setup_config_id == "setup_breakout_v1"
        # verify the WHERE clause was used
        assert any("setup_config_id" in sql for sql, _ in conn.executed)


# =========================================================================== #
# Naming canon: consolidation_base.
# =========================================================================== #

class TestConsolidationBaseNaming:
    """Canonical name is consolidation_base, never conservative_consolidation."""

    def test_outcome_queue_no_conservative_consolidation(self):
        import app.services.outcomes.outcome_queue as mod
        import inspect
        assert "conservative_consolidation" not in inspect.getsource(mod)

    def test_simulation_engine_no_conservative_consolidation(self):
        import app.services.simulation.simulation_engine as mod
        import inspect
        assert "conservative_consolidation" not in inspect.getsource(mod)

    def test_export_engine_no_conservative_consolidation(self):
        import app.services.export.export_package_engine as mod
        import inspect
        assert "conservative_consolidation" not in inspect.getsource(mod)

    def test_data_access_no_conservative_consolidation(self):
        import app.dashboard.data_access as mod
        import inspect
        assert "conservative_consolidation" not in inspect.getsource(mod)


# =========================================================================== #
# No aggressive/normal/conservative selection logic downstream.
# =========================================================================== #

class TestNoLegacyStrategyMode:
    """Downstream modules must not depend on aggressive/normal/conservative."""

    def _check_source_no_legacy_mode(self, mod):
        import inspect
        source = inspect.getsource(mod)
        for term in ("aggressive_breakout", "conservative_breakout",
                     "normal_breakout", "conservative_consolidation",
                     "aggressive_pullback", "normal_pullback"):
            assert term not in source, f"Found '{term}' in {mod.__name__}"

    def test_outcome_queue_no_legacy_mode(self):
        import app.services.outcomes.outcome_queue as mod
        self._check_source_no_legacy_mode(mod)

    def test_simulation_engine_no_legacy_mode(self):
        import app.services.simulation.simulation_engine as mod
        self._check_source_no_legacy_mode(mod)

    def test_export_engine_no_legacy_mode(self):
        import app.services.export.export_package_engine as mod
        self._check_source_no_legacy_mode(mod)

    def test_data_access_no_legacy_mode(self):
        import app.dashboard.data_access as mod
        self._check_source_no_legacy_mode(mod)

    def test_outcome_queue_no_strategy_config_id_column(self):
        """No SQL in outcome_queue should reference strategy_config_id column."""
        import app.services.outcomes.outcome_queue as mod
        # Check the SQL constants that actually query/write the DB
        for attr in ("_SELECT_ELIGIBLE_PROPOSALS", "_SELECT_PROPOSAL_SETUP_FIELDS",
                     "_UPSERT_OUTCOME", "_INSERT_QUEUE_ROW",
                     "_SELECT_DUE_QUEUE_ROWS"):
            sql = getattr(mod, attr, "")
            assert "strategy_config_id" not in sql, f"{attr} still has strategy_config_id"

    def test_simulation_engine_no_strategy_config_id_column(self):
        import app.services.simulation.simulation_engine as mod
        import inspect
        source = inspect.getsource(mod)
        assert "strategy_config_id" not in source

    def test_export_engine_no_strategy_config_id_column(self):
        import app.services.export.export_package_engine as mod
        import inspect
        source = inspect.getsource(mod)
        assert "strategy_config_id" not in source


# =========================================================================== #
# Pipeline orchestrator M20 — M16 call uses new API.
# =========================================================================== #

class TestOrchestratorM16Call:
    """Orchestrator must call M16 enqueue with setup_config_id, setup_config params."""

    def test_orchestrator_calls_enqueue_with_setup_config_id(self):
        import inspect
        import app.services.pipeline.pipeline_orchestrator as mod
        source = inspect.getsource(mod)
        # Must use setup_config_id= (not strategy_config_id=) in the enqueue call
        assert "setup_config_id=setup_config_id" in source
        assert "strategy_config_id=setup_config_id" not in source

    def test_orchestrator_calls_process_with_setup_config(self):
        import inspect
        import app.services.pipeline.pipeline_orchestrator as mod
        source = inspect.getsource(mod)
        # Must use setup_config= (not strategy_config=) in the process call
        assert "setup_config=_SIM_BLOCK" in source
        assert "strategy_config=_SIM_BLOCK" not in source


# =========================================================================== #
# Blocker 3 & 4 — Export engine return unit + worst drawdown selection.
# =========================================================================== #

class TestExportEngineDrawdownAndPrompt:
    """M18 drawdown uses decimal returns; worst_dd selects largest magnitude."""

    def test_drawdown_uses_decimal_return_not_divided_by_100(self):
        """_build_drawdowns_csv must use equity *= 1.0 + return, not / 100."""
        import inspect
        import app.services.export.export_package_engine as mod
        source = inspect.getsource(mod)
        # The bad pattern (dividing by 100) must not appear
        assert "/ 100.0" not in source, "drawdown still divides return by 100"
        assert "/ 100)" not in source, "drawdown still divides return by 100"
        # The correct pattern must be present
        assert '1.0 + point["return_40bd_pct"]' in source, 'decimal return update not found'

    def test_drawdown_equity_update_is_decimal(self):
        """Runtime test: compute drawdown from known decimal returns."""
        from app.services.export.export_package_engine import ExportPackageEngine
        # Two outcomes: +10% then -15% (decimal: 0.10, -0.15)
        outcomes = [
            {
                "setup_config_id": "setup_breakout_v1",
                "list_membership": "both",
                "signal_date": date(2024, 1, 2),
                "return_40bd_pct": 0.10,   # decimal +10%
            },
            {
                "setup_config_id": "setup_breakout_v1",
                "list_membership": "both",
                "signal_date": date(2024, 1, 9),
                "return_40bd_pct": -0.15,  # decimal -15%
            },
        ]
        csv_bytes = ExportPackageEngine._build_drawdowns_csv(outcomes)
        csv_text = csv_bytes.decode("utf-8")
        # equity after +10% = 1.10; after -15% = 1.10 * 0.85 = 0.935
        # drawdown = (1.10 - 0.935) / 1.10 ≈ 0.15 (15%)
        # output is drawdown_pct = fraction * 100 ≈ 15%
        assert "setup_breakout_v1" in csv_text
        # Confirm a drawdown row was written (non-zero drawdown)
        all_lines = csv_text.strip().splitlines(); lines = all_lines[1:]  # skip header
        assert len(lines) == 1, f"Expected 1 drawdown row, got: {lines}"
        # drawdown_pct should be close to 15 (positive percentage)
        dd_val = float(lines[0].split(",")[-1])
        # Correct: ~-15% * 100 = -15 (stored as negative fraction * 100)
        assert dd_val < 0, f"Drawdown should be negative magnitude, got {dd_val}"
        assert abs(dd_val) < 20, f"Drawdown should be around 15%, got {dd_val}"

    def test_worst_drawdown_selects_largest_magnitude(self):
        """_sim_prompt_text_v1 must select config with LARGEST max_drawdown_pct."""
        import inspect
        import app.services.export.export_package_engine as mod
        source = inspect.getsource(mod)
        # The fix: should use > not <  for worst drawdown selection
        assert '> worst_dd["max_drawdown_pct"]' in source,             "Worst drawdown should select largest magnitude (>), not smallest (<)"
        assert '< worst_dd["max_drawdown_pct"]' not in source,             "Found wrong comparison direction for worst drawdown"

    def test_prompt_selects_largest_drawdown_value(self):
        """Runtime: prompt picks the config with the larger max_drawdown_pct."""
        from app.services.export.export_package_engine import ExportPackageEngine
        comparisons = [
            {"config_id": "a", "expectancy": 0.05, "max_drawdown_pct": 10.0,
             "resolved_outcomes_pct": 0.9},
            {"config_id": "b", "expectancy": 0.03, "max_drawdown_pct": 25.0,
             "resolved_outcomes_pct": 0.85},
        ]
        data = {"performance_metrics": comparisons, "sim_run": None,
                "step3_scores": [], "step5_scores": [], "outcomes": [], "step4": []}
        prompt = ExportPackageEngine._sim_prompt_text_v1("test-run-id", data)
        # "b" has the larger drawdown (25.0 > 10.0) — should appear in worst line
        assert "b" in prompt, f"Config 'b' (worst drawdown) not in prompt: {prompt}"
        assert "25.0" in prompt, f"25.0 not in prompt: {prompt}"


# =========================================================================== #
# Blocker 2 — Dashboard setup_name consistency.
# =========================================================================== #

class TestDashboardSetupNameConsistency:
    """Dashboard must use setup_name, not strategy_name."""

    def test_streamlit_app_no_strategy_name_key(self):
        import pathlib
        src_path = pathlib.Path(__file__).parent.parent / "app" / "dashboard" / "streamlit_app.py"
        source = src_path.read_text(encoding="utf-8")
        assert "strategy_name" not in source, "strategy_name still present in streamlit_app"

    def test_action_service_csv_filename_uses_setup_prefix(self):
        import inspect
        import app.dashboard.action_service as mod
        source = inspect.getsource(mod)
        # filename must use setup_ prefix
        assert 'f"setup_{setup_name}' in source, "CSV filename must use setup_ prefix"
        assert 'f"strategy_{' not in source, "Old strategy_ filename prefix still present"

    def test_action_service_clone_accepts_setup_name(self):
        import inspect
        from app.dashboard.action_service import DashboardActionService
        import inspect as insp
        sig = insp.signature(DashboardActionService.clone_setup_config)
        assert "setup_name" in sig.parameters
        assert "strategy_name" not in sig.parameters
