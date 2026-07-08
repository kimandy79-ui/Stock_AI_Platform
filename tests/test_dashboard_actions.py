"""Tests for app.dashboard.action_service (DashboardActionService).

All tests run fully offline: injected fakes replace every service and no
real DB file, Streamlit, or network call is made.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date
from typing import Any

import pytest

from app.utils.service_result import ServiceResult
from app.utils import service_result as sr_mod


# ------------------------------------------------------------------ #
# Fake helpers.
# ------------------------------------------------------------------ #

def _ok(run_id: str | None = None, **meta: Any) -> ServiceResult:
    return ServiceResult(
        status=sr_mod.STATUS_SUCCESS,
        run_id=run_id or str(uuid.uuid4()),
        rows_processed=1,
        metadata=meta,
    )


def _fail(run_id: str | None = None, error: str = "boom") -> ServiceResult:
    return ServiceResult(
        status=sr_mod.STATUS_FAILED,
        run_id=run_id or str(uuid.uuid4()),
        errors=[error],
        metadata={"error": error},
    )


class FakePipeline:
    def __init__(self, result: ServiceResult | None = None) -> None:
        self.called: list[dict] = []
        self._result = result

    def run(self, **kwargs: Any) -> ServiceResult:
        self.called.append(kwargs)
        # Echo the supplied run_id so preserve-tests work correctly.
        base = self._result or _ok()
        return ServiceResult(
            status=base.status,
            run_id=kwargs.get("run_id") or base.run_id,
            rows_processed=base.rows_processed,
            warnings=base.warnings,
            errors=base.errors,
            metadata=base.metadata,
        )


class FakeExport:
    def __init__(self, result: ServiceResult | None = None) -> None:
        self.called: list[dict] = []
        self._result = result or _ok(zip_path="/tmp/fake.zip", zip_filename="fake.zip")

    def export_ticker_review(self, **kwargs: Any) -> ServiceResult:
        self.called.append(kwargs)
        return self._result


class FakeAiReview:
    def __init__(
        self,
        send_result: ServiceResult | None = None,
        action_result: ServiceResult | None = None,
    ) -> None:
        self.send_calls: list[dict] = []
        self.action_calls: list[dict] = []
        self._send_result = send_result or _ok()
        self._action_result = action_result or _ok()

    def send_ticker_review(self, **kwargs: Any) -> ServiceResult:
        self.send_calls.append(kwargs)
        return self._send_result

    def record_human_action(self, **kwargs: Any) -> ServiceResult:
        self.action_calls.append(kwargs)
        return self._action_result


class FakeConfig:
    def __init__(
        self,
        activate_result: ServiceResult | None = None,
        create_result: ServiceResult | None = None,
        get_result: ServiceResult | None = None,
        list_result: ServiceResult | None = None,
    ) -> None:
        self.activate_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self._activate_result = activate_result or _ok(config_id="cfg1", setup_name="Normal")
        self._create_result = create_result or _ok(config_id="cfg2", setup_name="Normal", active_flag=False)
        self._get_result = get_result or _ok(config={"config_id": "cfg1", "setup_name": "Normal", "version": "v1", "active_flag": True, "config_hash": "abc", "created_at": "2026-01-01", "created_by": None, "notes": None})
        self._list_result = list_result or _ok(versions=[])

    def activate_setup_config(
        self,
        config_id: str,
        db_role: str,
        activated_by: Any = None,
        reason: Any = None,
        setup_type: Any = None,
    ) -> ServiceResult:
        # setup_type is accepted here (but not by the old activated_by/reason
        # tests below) because it mirrors ConfigService's *real* signature --
        # approve_config_recommendation calls it that way. See action_service.py's
        # comment on the pre-existing activated_by/reason drift.
        self.activate_calls.append({
            "config_id": config_id, "db_role": db_role,
            "activated_by": activated_by, "reason": reason, "setup_type": setup_type,
        })
        return self._activate_result

    def create_setup_config_version(self, **kwargs: Any) -> ServiceResult:
        self.create_calls.append(kwargs)
        return self._create_result

    def get_setup_config(self, config_id: str, db_role: str) -> ServiceResult:
        self.get_calls.append({"config_id": config_id, "db_role": db_role})
        return self._get_result

    def list_setup_configs(self, db_role: str, setup_name: Any = None) -> ServiceResult:
        return self._list_result


class FakeConfigRecommender:
    def __init__(self, status_result: ServiceResult | None = None) -> None:
        self.status_calls: list[dict] = []
        self._status_result = status_result or _ok(recommendation_id="rec1", status="approved")

    def set_recommendation_status(self, recommendation_id: str, status: str, db_role: str) -> ServiceResult:
        self.status_calls.append({"recommendation_id": recommendation_id, "status": status, "db_role": db_role})
        return self._status_result


class FakeDbConn:
    def __init__(self, rows: list[tuple], columns: list[str]) -> None:
        self._rows = rows
        self._columns = columns

    class _Cursor:
        def __init__(self, rows: list[tuple], columns: list[str]) -> None:
            self._rows = rows
            self.description = [(c,) for c in columns]

        def fetchall(self) -> list[tuple]:
            return self._rows

    def execute(self, sql: str, params: Any = None) -> "_Cursor":
        return self._Cursor(self._rows, self._columns)

    def close(self) -> None:
        pass


class FakeDbManager:
    def __init__(self, rows: list[tuple] | None = None, columns: list[str] | None = None) -> None:
        self._rows = rows or []
        self._columns = columns or []

    def connect(self, db_role: str, read_only: bool = False) -> FakeDbConn:
        return FakeDbConn(self._rows, self._columns)


def _svc(**kwargs: Any) -> "DashboardActionService":  # noqa: F821
    from app.dashboard.action_service import DashboardActionService
    return DashboardActionService(**kwargs)


# ------------------------------------------------------------------ #
# run_pipeline.
# ------------------------------------------------------------------ #

class TestRunPipeline:
    def test_success_delegates_to_pipeline(self) -> None:
        fake = FakePipeline()
        svc = _svc(pipeline_orchestrator=fake)
        result = svc.run_pipeline(db_role="prod", run_date=date(2026, 6, 5))
        assert result.status == sr_mod.STATUS_SUCCESS
        assert len(fake.called) == 1
        assert fake.called[0]["db_role"] == "prod"
        assert fake.called[0]["run_date"] == date(2026, 6, 5)

    def test_invalid_role_rejected_before_pipeline(self) -> None:
        fake = FakePipeline()
        svc = _svc(pipeline_orchestrator=fake)
        result = svc.run_pipeline(db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED
        assert len(fake.called) == 0  # no pipeline call

    def test_pipeline_failure_propagated(self) -> None:
        fake = FakePipeline(result=_fail(error="step 5 crash"))
        svc = _svc(pipeline_orchestrator=fake)
        result = svc.run_pipeline(db_role="debug")
        assert result.status == sr_mod.STATUS_FAILED

    def test_run_id_supplied_preserved(self) -> None:
        fixed_id = str(uuid.uuid4())
        fake = FakePipeline()
        svc = _svc(pipeline_orchestrator=fake)
        result = svc.run_pipeline(db_role="prod", run_id=fixed_id)
        assert result.run_id == fixed_id

    def test_run_id_minted_when_none(self) -> None:
        fake = FakePipeline()
        svc = _svc(pipeline_orchestrator=fake)
        result = svc.run_pipeline(db_role="prod")
        assert result.run_id  # non-empty
        assert len(result.run_id) == 36  # uuid4 string

    def test_invalid_ticker_source_rejected(self) -> None:
        fake = FakePipeline()
        svc = _svc(pipeline_orchestrator=fake)
        result = svc.run_pipeline(db_role="prod", ticker_source="ftp")
        assert result.status == sr_mod.STATUS_FAILED
        assert len(fake.called) == 0  # rejected before ever touching the pipeline

    def test_injected_pipeline_bypasses_ticker_loading_entirely(self) -> None:
        """When a fake orchestrator is injected (the existing test pattern),
        ticker_source is accepted but never exercises the real csv/db
        loading branch -- ticker_count is simply None."""
        fake = FakePipeline()
        svc = _svc(pipeline_orchestrator=fake)
        result = svc.run_pipeline(db_role="prod", ticker_source="csv")
        assert result.status == sr_mod.STATUS_SUCCESS
        assert result.metadata["ticker_source"] == "csv"
        assert result.metadata["ticker_count"] is None


# ------------------------------------------------------------------ #
# run_pipeline ticker_source — real _get_pipeline branching (no injected
# fake orchestrator, so the csv/db loading logic actually runs).
# ------------------------------------------------------------------ #

class TestGetPipelineTickerSource:
    def test_csv_source_loads_real_file(self, tmp_path) -> None:
        csv_path = tmp_path / "tickers.csv"
        csv_path.write_text("ticker\nAAPL\nMSFT\nGOOGL\n", encoding="utf-8")
        svc = _svc()
        pipeline, ticker_count = svc._get_pipeline("csv", csv_path)
        assert ticker_count == 3
        assert pipeline is not None

    def test_csv_source_missing_file_raises_value_error(self, tmp_path) -> None:
        svc = _svc()
        missing = tmp_path / "does_not_exist.csv"
        with pytest.raises(ValueError, match="cannot read"):
            svc._get_pipeline("csv", missing)

    def test_csv_source_empty_file_raises_value_error(self, tmp_path) -> None:
        csv_path = tmp_path / "empty.csv"
        csv_path.write_text("", encoding="utf-8")
        svc = _svc()
        with pytest.raises(ValueError, match="empty"):
            svc._get_pipeline("csv", csv_path)

    def test_csv_source_default_path_is_project_csv(self) -> None:
        """No explicit tickers_csv_path -> falls back to
        data/input/backfill_tickers_common_only.csv, which exists in this repo."""
        svc = _svc()
        pipeline, ticker_count = svc._get_pipeline("csv", None)
        assert ticker_count is not None and ticker_count > 0

    def test_db_source_loads_from_ticker_master(self) -> None:
        rows = [
            ("AAPL", "AAPL", "Apple Inc.", "NASDAQ", "Technology", "Hardware", "common_stock", "stock"),
            ("MSFT", "MSFT", "Microsoft Corp.", "NASDAQ", "Technology", "Software", "common_stock", "stock"),
        ]
        columns = ["ticker", "yahoo_symbol", "company_name", "exchange",
                   "sector", "industry", "security_type", "symbol_type"]
        svc = _svc(db_manager=FakeDbManager(rows=rows, columns=columns))
        pipeline, ticker_count = svc._get_pipeline("db", None)
        assert ticker_count == 2

    def test_db_source_is_the_default_when_unspecified(self) -> None:
        rows = [("AAPL", "AAPL", "Apple Inc.", "NASDAQ", "Technology", "Hardware", "common_stock", "stock")]
        columns = ["ticker", "yahoo_symbol", "company_name", "exchange",
                   "sector", "industry", "security_type", "symbol_type"]
        svc = _svc(db_manager=FakeDbManager(rows=rows, columns=columns))
        pipeline, ticker_count = svc._get_pipeline()
        assert ticker_count == 1


# ------------------------------------------------------------------ #
# export_ticker_review.
# ------------------------------------------------------------------ #

class TestExportTickerReview:
    _DATE = date(2026, 6, 5)

    def test_success(self) -> None:
        fake = FakeExport()
        svc = _svc(export_engine=fake)
        result = svc.export_ticker_review(
            signal_date=self._DATE,
            setup_config_id="cfg1",
            proposal_ids=["p1", "p2"],
            db_role="prod",
        )
        assert result.status == sr_mod.STATUS_SUCCESS
        assert len(fake.called) == 1

    def test_invalid_role_no_engine_call(self) -> None:
        fake = FakeExport()
        svc = _svc(export_engine=fake)
        result = svc.export_ticker_review(signal_date=self._DATE, setup_config_id="cfg1", proposal_ids=["p1"], db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED
        assert len(fake.called) == 0

    def test_empty_setup_config_id_rejected(self) -> None:
        fake = FakeExport()
        svc = _svc(export_engine=fake)
        result = svc.export_ticker_review(signal_date=self._DATE, setup_config_id="", proposal_ids=["p1"], db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED
        assert len(fake.called) == 0

    def test_empty_proposal_ids_rejected(self) -> None:
        fake = FakeExport()
        svc = _svc(export_engine=fake)
        result = svc.export_ticker_review(signal_date=self._DATE, setup_config_id="cfg1", proposal_ids=[], db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED
        assert len(fake.called) == 0

    def test_debug_role_allowed(self) -> None:
        fake = FakeExport()
        svc = _svc(export_engine=fake)
        result = svc.export_ticker_review(signal_date=self._DATE, setup_config_id="cfg1", proposal_ids=["p1"], db_role="debug")
        assert result.status == sr_mod.STATUS_SUCCESS


# ------------------------------------------------------------------ #
# send_ticker_review.
# ------------------------------------------------------------------ #

class TestSendTickerReview:
    def test_success(self) -> None:
        fake = FakeAiReview()
        svc = _svc(ai_review_engine=fake)
        result = svc.send_ticker_review(ai_review_id="rev1", db_role="prod")
        assert result.status == sr_mod.STATUS_SUCCESS
        assert len(fake.send_calls) == 1

    def test_invalid_role(self) -> None:
        fake = FakeAiReview()
        svc = _svc(ai_review_engine=fake)
        result = svc.send_ticker_review(ai_review_id="rev1", db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED
        assert not fake.send_calls

    def test_empty_ai_review_id(self) -> None:
        fake = FakeAiReview()
        svc = _svc(ai_review_engine=fake)
        result = svc.send_ticker_review(ai_review_id="", db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED
        assert not fake.send_calls


# ------------------------------------------------------------------ #
# record_human_action.
# ------------------------------------------------------------------ #

class TestRecordHumanAction:
    def test_success(self) -> None:
        fake = FakeAiReview()
        svc = _svc(ai_review_engine=fake)
        result = svc.record_human_action(ai_review_id="rev1", human_action="accepted", db_role="prod")
        assert result.status == sr_mod.STATUS_SUCCESS
        assert len(fake.action_calls) == 1
        assert fake.action_calls[0]["human_action"] == "accepted"

    def test_invalid_role(self) -> None:
        fake = FakeAiReview()
        svc = _svc(ai_review_engine=fake)
        result = svc.record_human_action(ai_review_id="rev1", human_action="accepted", db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED
        assert not fake.action_calls

    def test_empty_id(self) -> None:
        fake = FakeAiReview()
        svc = _svc(ai_review_engine=fake)
        result = svc.record_human_action(ai_review_id="", human_action="ignored", db_role="debug")
        assert result.status == sr_mod.STATUS_FAILED


# ------------------------------------------------------------------ #
# activate_setup_config.
# ------------------------------------------------------------------ #

class TestActivateStrategyConfig:
    def test_success(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.activate_setup_config(config_id="cfg1", db_role="prod")
        assert result.status == sr_mod.STATUS_SUCCESS
        assert len(fake.activate_calls) == 1
        assert fake.activate_calls[0]["config_id"] == "cfg1"

    def test_invalid_role(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.activate_setup_config(config_id="cfg1", db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED
        assert not fake.activate_calls

    def test_empty_config_id(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.activate_setup_config(config_id="", db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED

    def test_activated_by_and_reason_forwarded(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        svc.activate_setup_config(config_id="cfg1", db_role="debug", activated_by="alice", reason="test reason")
        assert fake.activate_calls[0]["activated_by"] == "alice"
        assert fake.activate_calls[0]["reason"] == "test reason"


# ------------------------------------------------------------------ #
# approve_config_recommendation.
# ------------------------------------------------------------------ #

class TestApproveConfigRecommendation:
    def test_success_activates_then_marks_approved(self) -> None:
        config = FakeConfig()
        recommender = FakeConfigRecommender()
        svc = _svc(config_service=config, config_recommender=recommender)
        result = svc.approve_config_recommendation(
            recommendation_id="rec1", candidate_config_id="cfg1",
            setup_type="breakout", db_role="prod",
        )
        assert result.status == sr_mod.STATUS_SUCCESS
        assert len(config.activate_calls) == 1
        assert config.activate_calls[0]["config_id"] == "cfg1"
        assert config.activate_calls[0]["setup_type"] == "breakout"
        assert len(recommender.status_calls) == 1
        assert recommender.status_calls[0] == {
            "recommendation_id": "rec1", "status": "approved", "db_role": "prod",
        }

    def test_invalid_role_rejected_before_any_call(self) -> None:
        config = FakeConfig()
        recommender = FakeConfigRecommender()
        svc = _svc(config_service=config, config_recommender=recommender)
        result = svc.approve_config_recommendation(
            recommendation_id="rec1", candidate_config_id="cfg1",
            setup_type="breakout", db_role="simulation",
        )
        assert result.status == sr_mod.STATUS_FAILED
        assert not config.activate_calls
        assert not recommender.status_calls

    def test_empty_recommendation_id_rejected(self) -> None:
        svc = _svc(config_service=FakeConfig(), config_recommender=FakeConfigRecommender())
        result = svc.approve_config_recommendation(
            recommendation_id="", candidate_config_id="cfg1", setup_type="breakout", db_role="prod",
        )
        assert result.status == sr_mod.STATUS_FAILED

    def test_empty_candidate_config_id_rejected(self) -> None:
        svc = _svc(config_service=FakeConfig(), config_recommender=FakeConfigRecommender())
        result = svc.approve_config_recommendation(
            recommendation_id="rec1", candidate_config_id="", setup_type="breakout", db_role="prod",
        )
        assert result.status == sr_mod.STATUS_FAILED

    def test_activation_failure_short_circuits_before_marking_approved(self) -> None:
        config = FakeConfig(activate_result=_fail(error="activation boom"))
        recommender = FakeConfigRecommender()
        svc = _svc(config_service=config, config_recommender=recommender)
        result = svc.approve_config_recommendation(
            recommendation_id="rec1", candidate_config_id="cfg1",
            setup_type="breakout", db_role="prod",
        )
        assert result.status == sr_mod.STATUS_FAILED
        assert len(config.activate_calls) == 1
        assert not recommender.status_calls  # never marked approved after a failed activation

    def test_recommendation_status_failure_degrades_to_warning_not_failure(self) -> None:
        config = FakeConfig()
        recommender = FakeConfigRecommender(status_result=_fail(error="status update boom"))
        svc = _svc(config_service=config, config_recommender=recommender)
        result = svc.approve_config_recommendation(
            recommendation_id="rec1", candidate_config_id="cfg1",
            setup_type="breakout", db_role="prod",
        )
        # Config is already active -- degrade to warnings, not a hard failure.
        assert result.status == sr_mod.STATUS_SUCCESS_WITH_WARNINGS
        assert any("approved" in w for w in result.warnings)


# ------------------------------------------------------------------ #
# reject_config_recommendation.
# ------------------------------------------------------------------ #

class TestRejectConfigRecommendation:
    def test_success(self) -> None:
        recommender = FakeConfigRecommender(status_result=_ok(recommendation_id="rec1", status="rejected"))
        svc = _svc(config_recommender=recommender)
        result = svc.reject_config_recommendation(recommendation_id="rec1", db_role="prod")
        assert result.status == sr_mod.STATUS_SUCCESS
        assert recommender.status_calls == [{"recommendation_id": "rec1", "status": "rejected", "db_role": "prod"}]

    def test_invalid_role_rejected(self) -> None:
        recommender = FakeConfigRecommender()
        svc = _svc(config_recommender=recommender)
        result = svc.reject_config_recommendation(recommendation_id="rec1", db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED
        assert not recommender.status_calls

    def test_empty_recommendation_id_rejected(self) -> None:
        svc = _svc(config_recommender=FakeConfigRecommender())
        result = svc.reject_config_recommendation(recommendation_id="", db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED

    def test_does_not_touch_config_service(self) -> None:
        config = FakeConfig()
        recommender = FakeConfigRecommender()
        svc = _svc(config_service=config, config_recommender=recommender)
        svc.reject_config_recommendation(recommendation_id="rec1", db_role="prod")
        assert not config.activate_calls


# ------------------------------------------------------------------ #
# clone_setup_config.
# ------------------------------------------------------------------ #

class TestCloneStrategyConfig:
    def test_success(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.clone_setup_config(
            db_role="prod",
            setup_name="Normal",
            config_json={"min_price": 5.0},
        )
        assert result.status == sr_mod.STATUS_SUCCESS
        assert len(fake.create_calls) == 1

    def test_invalid_role(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.clone_setup_config(db_role="simulation", setup_name="Normal", config_json={"x": 1})
        assert result.status == sr_mod.STATUS_FAILED
        assert not fake.create_calls

    def test_empty_setup_name_rejected(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.clone_setup_config(db_role="prod", setup_name="", config_json={"x": 1})
        assert result.status == sr_mod.STATUS_FAILED

    def test_empty_config_json_rejected(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.clone_setup_config(db_role="prod", setup_name="Normal", config_json={})
        assert result.status == sr_mod.STATUS_FAILED

    def test_activate_flag_forwarded(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        svc.clone_setup_config(db_role="prod", setup_name="Normal", config_json={"x": 1}, activate=True)
        assert fake.create_calls[0]["activate"] is True


# ------------------------------------------------------------------ #
# export_setup_config_csv.
# ------------------------------------------------------------------ #

class TestExportStrategyConfigCsv:
    def test_csv_bytes_returned(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.export_setup_config_csv(config_id="cfg1", db_role="prod")
        assert result.status == sr_mod.STATUS_SUCCESS
        csv_bytes = result.metadata["csv_bytes"]
        assert isinstance(csv_bytes, bytes)
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
        assert len(rows) == 1
        assert rows[0]["config_id"] == "cfg1"
        assert rows[0]["setup_name"] == "Normal"

    def test_filename_contains_strategy_and_version(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.export_setup_config_csv(config_id="cfg1", db_role="prod")
        assert "Normal" in result.metadata["filename"]
        assert "v1" in result.metadata["filename"]

    def test_invalid_role(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.export_setup_config_csv(config_id="cfg1", db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED

    def test_empty_config_id(self) -> None:
        fake = FakeConfig()
        svc = _svc(config_service=fake)
        result = svc.export_setup_config_csv(config_id="", db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED

    def test_upstream_failure_propagated(self) -> None:
        fake = FakeConfig(get_result=_fail(error="not found"))
        svc = _svc(config_service=fake)
        result = svc.export_setup_config_csv(config_id="cfg1", db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED


# ------------------------------------------------------------------ #
# export_proposals_csv.
# ------------------------------------------------------------------ #

class TestExportProposalsCsv:
    _DATE = date(2026, 6, 5)

    def _fake_db_with_rows(self) -> FakeDbManager:
        rows = [
            ("pid1", date(2026, 6, 5), "NVDA", "cfg1", 1, 1, 92.4, 91.0, True, True, "Trend Resume", 3.12, "Technology", "Semiconductors", "EMA breakout"),
        ]
        columns = ["proposal_id", "signal_date", "ticker", "setup_config_id", "raw_rank", "diversified_rank", "proposal_score_raw", "proposal_score_final", "in_raw_top_n", "in_diversified_top_n", "setup_type", "estimated_rr", "sector", "industry", "mechanical_explanation"]
        return FakeDbManager(rows=rows, columns=columns)

    def test_success_returns_csv_bytes(self) -> None:
        svc = _svc(db_manager=self._fake_db_with_rows())
        result = svc.export_proposals_csv(signal_date=self._DATE, proposal_ids=["pid1"], db_role="prod")
        assert result.status == sr_mod.STATUS_SUCCESS
        csv_bytes = result.metadata["csv_bytes"]
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
        assert len(rows) == 1
        assert rows[0]["ticker"] == "NVDA"

    def test_csv_header_contains_expected_columns(self) -> None:
        svc = _svc(db_manager=self._fake_db_with_rows())
        result = svc.export_proposals_csv(signal_date=self._DATE, proposal_ids=["pid1"], db_role="prod")
        csv_text = result.metadata["csv_bytes"].decode("utf-8")
        header = csv_text.split("\n")[0]
        for col in ["proposal_id", "ticker", "raw_rank", "sector"]:
            assert col in header

    def test_empty_proposal_ids_rejected(self) -> None:
        svc = _svc(db_manager=FakeDbManager())
        result = svc.export_proposals_csv(signal_date=self._DATE, proposal_ids=[], db_role="prod")
        assert result.status == sr_mod.STATUS_FAILED

    def test_invalid_role_rejected(self) -> None:
        svc = _svc(db_manager=FakeDbManager())
        result = svc.export_proposals_csv(signal_date=self._DATE, proposal_ids=["p1"], db_role="simulation")
        assert result.status == sr_mod.STATUS_FAILED

    def test_filename_contains_date_and_role(self) -> None:
        svc = _svc(db_manager=self._fake_db_with_rows())
        result = svc.export_proposals_csv(signal_date=self._DATE, proposal_ids=["pid1"], db_role="prod")
        assert "2026-06-05" in result.metadata["filename"]
        assert "prod" in result.metadata["filename"]

    def test_row_count_in_metadata(self) -> None:
        svc = _svc(db_manager=self._fake_db_with_rows())
        result = svc.export_proposals_csv(signal_date=self._DATE, proposal_ids=["pid1"], db_role="prod")
        assert result.metadata["rows"] == 1


# ------------------------------------------------------------------ #
# data_access: load_proposal_ids (new method).
# ------------------------------------------------------------------ #

class TestLoadProposalIds:
    _DATE = date(2026, 6, 5)

    def _loader_with_ids(self, ids: list[str]) -> Any:
        from app.dashboard.data_access import DashboardDataLoader

        class _FakeMgr:
            def __init__(self, rows: list[tuple]) -> None:
                self._rows = rows

            class _Conn:
                def __init__(self, rows: list[tuple]) -> None:
                    self._rows = rows

                class _Cursor:
                    def __init__(self, rows: list[tuple]) -> None:
                        self.description = [("proposal_id",)]
                        self._rows = rows

                    def fetchall(self) -> list[tuple]:
                        return self._rows

                def execute(self, sql: str, params: Any) -> "_Cursor":
                    return self._Cursor(self._rows)

                def close(self) -> None:
                    pass

            def connect(self, db_role: str, read_only: bool = False) -> "_Conn":
                return self._Conn(self._rows)

        rows = [(pid,) for pid in ids]
        return DashboardDataLoader(db_manager=_FakeMgr(rows), db_role="prod")

    def test_returns_proposal_ids(self) -> None:
        loader = self._loader_with_ids(["pid1", "pid2"])
        result = loader.load_proposal_ids(
            run_id="run1",
            signal_date=self._DATE,
            tickers=["NVDA", "MSFT"],
        )
        assert result == ["pid1", "pid2"]

    def test_empty_tickers_returns_empty(self) -> None:
        loader = self._loader_with_ids(["pid1"])
        result = loader.load_proposal_ids(run_id="run1", signal_date=self._DATE, tickers=[])
        assert result == []

    def test_empty_db_rows_returns_empty(self) -> None:
        loader = self._loader_with_ids([])
        result = loader.load_proposal_ids(run_id="run1", signal_date=self._DATE, tickers=["NVDA"])
        assert result == []
