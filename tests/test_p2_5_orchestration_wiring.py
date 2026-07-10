"""P2.5 — M20 step5 orchestration wiring (fundamentals on, AI review provisioned off).

Four contracts, per the coder note:

1. **Golden byte-identity.** With every seeded config as it ships, turning
   ``auto_invoke_fundamentals`` on changes no proposal score, because
   ``risk_label_config.fundamentals.score_weight`` is seeded ``0.0``. The score
   is computed and threaded end-to-end, but contributes exactly zero.
2. **Double-credit guard (runtime).** When a setup_config opts M14 into its own
   fundamentals adjustment, Step 5 must not also apply its term — exactly one of
   the two paths may contribute.
3. **Double-credit guard (authoring).** ``validate_setup_config`` rejects the
   conflicting combination, naming it, rather than silently relying on (2).
4. **AI review flag.** Default ``False`` => M19 is never constructed or called
   and output is unchanged; explicitly ``True`` => the provider is invoked and
   Step 5 receives the scores.

Fully offline. No real DB file, no provider, no network, no API key.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services.config import default_configs
from app.services.config.config_service import ConfigService
from app.services.fundamentals import fundamentals_quality as fq
from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
from app.services.proposal.step5_proposal_engine import (
    _m14_owns_fundamentals,
    _parse_risk_label_config,
    _proposal_score_raw,
)
from app.services.screening.m14_setup_validators import _compute_fundamentals_adjustment
from app.utils import service_result
from app.utils.service_result import ServiceResult

RUN_DATE = date(2026, 6, 15)

# A ticker with strong fundamentals across all five fields -> high quality score.
_STRONG = {
    "piotroski_f_score": 9,
    "altman_z_score": 5.0,
    "valuation_band": "cheap",
    "eps_growth_trend": 0.5,
    "leverage_ratio": 0.0,
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, db: "_FakeDb") -> None:
        self._db = db

    def execute(self, sql: str, params: Any = None):
        self._db.executed.append((sql, params))
        if "ticker_fundamentals" in sql.lower():
            return _FakeCursor(self._db.fundamentals_rows)
        return _FakeCursor([])

    def close(self) -> None:
        pass


class _FakeDb:
    def __init__(self, fundamentals_rows: list[tuple] | None = None) -> None:
        self.fundamentals_rows = fundamentals_rows or []
        self.executed: list[tuple] = []

    def connect(self, db_role: str, read_only: bool = False):
        return _FakeConnection(self)


class _RecordingProposalEngine:
    """Captures every propose() call so we can assert what Step 5 received."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def propose(self, **kwargs: Any) -> ServiceResult:
        self.calls.append(kwargs)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS, run_id="r", rows_processed=1, metadata={}
        )


class _FakeConfigService:
    """Serves runtime + risk configs straight from the seeded defaults."""

    def __init__(self, pipeline_overrides: dict[str, Any] | None = None) -> None:
        self._pipeline_overrides = pipeline_overrides or {}

    def get_active_runtime_config(self, db_role: str, config_type: str) -> ServiceResult:
        cfg = dict(default_configs.DEFAULT_RUNTIME_CONFIGS[config_type])
        cfg.update(self._pipeline_overrides)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS, run_id="r", rows_processed=1,
            metadata={"config_json": cfg},
        )

    def get_active_risk_label_config(self, db_role: str) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS, run_id="r", rows_processed=1,
            metadata={"config_json": dict(default_configs.DEFAULT_RISK_LABEL_CONFIG)},
        )


def _orchestrator(db: _FakeDb, engine: Any, **kwargs: Any) -> PipelineOrchestrator:
    """Build M20 with every collaborator faked except the ones under test."""
    pipeline_overrides = kwargs.pop("pipeline_overrides", None)
    return PipelineOrchestrator(
        db_manager=db,
        proposal_engine=engine,
        config_service=_FakeConfigService(pipeline_overrides),
        provider=object(),
        fundamentals_provider=object(),
        benchmark_loader=object(),
        universe_engine=object(),
        ingestion_engine=object(),
        validation_engine=object(),
        mutation_engine=object(),
        feature_engine=object(),
        regime_engine=object(),
        eligibility_engine=object(),
        setup_validation_engine=object(),
        outcome_creator=object(),
        outcome_processor=object(),
        diagnostics_service=object(),
        **kwargs,
    )


def _log():
    import logging

    return logging.getLogger("test_p2_5")


# --------------------------------------------------------------------------- #
# 1. Golden byte-identity: seeded weight is 0.0, so the term is inert.
# --------------------------------------------------------------------------- #
class TestSeededConfigIsByteIdentical:
    def test_seeded_risk_config_has_inert_fundamentals_weight(self):
        parsed = _parse_risk_label_config(
            dict(default_configs.DEFAULT_RISK_LABEL_CONFIG)
        )
        assert parsed["fundamentals_score_weight"] == 0.0

    def test_v2_risk_config_inherits_the_inert_weight(self):
        parsed = _parse_risk_label_config(
            dict(default_configs.DEFAULT_RISK_LABEL_CONFIG_V2)
        )
        assert parsed["fundamentals_score_weight"] == 0.0

    def test_no_seeded_setup_config_enables_m14_fundamentals(self):
        for setup_type, cfg in default_configs.DEFAULT_SETUP_CONFIGS.items():
            assert not _m14_owns_fundamentals(cfg), setup_type

    @pytest.mark.parametrize("quality", [0.0, 25.0, 50.0, 75.0, 100.0])
    def test_scoring_under_seeded_weight_is_byte_identical(self, quality):
        """The whole point of P2.5: feeding a score changes nothing while inert."""
        weight = _parse_risk_label_config(
            dict(default_configs.DEFAULT_RISK_LABEL_CONFIG)
        )["fundamentals_score_weight"]
        without = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        with_score = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            fundamentals_quality_score=quality,
            fundamentals_score_weight=weight,
        )
        assert with_score == without


# --------------------------------------------------------------------------- #
# 2. Double-credit guard at scoring time.
# --------------------------------------------------------------------------- #
class TestDoubleCreditRuntimeGuard:
    _M14_ON = {"fundamentals": {"enabled": True, "weight": 20.0}}

    def test_predicate_detects_m14_ownership(self):
        assert _m14_owns_fundamentals(self._M14_ON)
        assert not _m14_owns_fundamentals({})
        assert not _m14_owns_fundamentals({"fundamentals": {"enabled": False, "weight": 20.0}})
        # enabled but zero weight contributes nothing in M14, so Step 5 may score.
        assert not _m14_owns_fundamentals({"fundamentals": {"enabled": True, "weight": 0.0}})

    def test_m14_precondition_owns_the_signal(self):
        """M14 really does move setup_score for this config (guards the guard)."""
        m14_adj, _ = _compute_fundamentals_adjustment(
            _STRONG, self._M14_ON["fundamentals"]
        )
        assert m14_adj > 0.0

    def test_double_credit_would_occur_without_suppression(self):
        """The formula does add a second contribution when both scores are live."""
        quality = fq.compute_fundamentals_quality(_STRONG)
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        unguarded = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            fundamentals_quality_score=quality,
            fundamentals_score_weight=0.10,
        )
        assert unguarded > base

    def test_m14_disabled_lets_step5_own_the_signal(self):
        cfg = {"fundamentals": {"enabled": False, "weight": 20.0}}
        m14_adj, _ = _compute_fundamentals_adjustment(_STRONG, cfg["fundamentals"])
        assert m14_adj == 0.0
        assert not _m14_owns_fundamentals(cfg)

    def test_both_paths_agree_on_the_quality_number(self):
        """Shared helper: M14's evidence and Step 5's input are the same value."""
        _, evidence = _compute_fundamentals_adjustment(
            _STRONG, {"enabled": True, "weight": 20.0}
        )
        assert evidence["avg_quality"] == fq.compute_fundamentals_quality(_STRONG)


# --------------------------------------------------------------------------- #
# 3. Double-credit guard at authoring time.
# --------------------------------------------------------------------------- #
class TestDoubleCreditConfigValidation:
    def _conflicting_setup_config(self) -> dict[str, Any]:
        cfg = dict(default_configs.DEFAULT_SETUP_CONFIGS["breakout"])
        cfg["fundamentals"] = {"enabled": True, "weight": 20.0}
        return cfg

    def test_rejected_when_step5_term_is_active(self):
        cs = ConfigService(db_manager=_FakeDb())
        result = cs.validate_setup_config(
            self._conflicting_setup_config(),
            risk_label_config={"fundamentals": {"score_weight": 0.10}},
        )
        assert result.status == service_result.STATUS_FAILED
        errors = " ".join(result.metadata["errors"])
        assert "scored twice" in errors
        assert "fundamentals.enabled" in errors
        assert "score_weight" in errors

    def test_allowed_when_step5_term_is_inert(self):
        """Seeded weight is 0.0 => Step 5 adds nothing => no double credit."""
        cs = ConfigService(db_manager=_FakeDb())
        result = cs.validate_setup_config(self._conflicting_setup_config())
        assert result.status == service_result.STATUS_SUCCESS

    def test_seeded_configs_all_validate_against_seeded_risk_config(self):
        cs = ConfigService(db_manager=_FakeDb())
        for setup_type, cfg in default_configs.DEFAULT_SETUP_CONFIGS.items():
            result = cs.validate_setup_config(
                cfg, risk_label_config=default_configs.DEFAULT_RISK_LABEL_CONFIG
            )
            assert result.status == service_result.STATUS_SUCCESS, setup_type


# --------------------------------------------------------------------------- #
# 4. M20 wiring: fundamentals auto-invoked; AI review provisioned but off.
# --------------------------------------------------------------------------- #
class TestOrchestratorStep5Wiring:
    def _fundamentals_rows(self) -> list[tuple]:
        # Column order must match fq.FUNDAMENTALS_COLS.
        return [("AAPL", 0.5, 0.0, "cheap", 9, 5.0)]

    def test_fundamentals_scores_are_built_and_passed_to_step5(self):
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()
        orch = _orchestrator(db, engine)

        orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        assert len(engine.calls) == 1
        scores = engine.calls[0]["fundamentals_scores"]
        assert scores == {"AAPL": pytest.approx(fq.compute_fundamentals_quality(_STRONG))}

    def test_fundamentals_read_is_point_in_time(self):
        db = _FakeDb(self._fundamentals_rows())
        orch = _orchestrator(db, _RecordingProposalEngine())
        orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        reads = [c for c in db.executed if "ticker_fundamentals" in c[0].lower()]
        assert reads, "expected a ticker_fundamentals read"
        sql, params = reads[0]
        assert "as_of_date <= ?" in sql
        assert params == [RUN_DATE.isoformat()]

    def test_flag_off_means_no_fundamentals_read_and_none_passed(self):
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()
        orch = _orchestrator(
            db, engine, pipeline_overrides={"auto_invoke_fundamentals": False}
        )

        orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        assert engine.calls[0]["fundamentals_scores"] is None
        assert not [c for c in db.executed if "ticker_fundamentals" in c[0].lower()]

    def test_fundamentals_read_failure_degrades_to_none(self):
        class _ExplodingDb(_FakeDb):
            def connect(self, db_role: str, read_only: bool = False):
                raise RuntimeError("table not found")

        engine = _RecordingProposalEngine()
        orch = _orchestrator(_ExplodingDb(), engine)
        orch._step_step5(RUN_DATE, "prod", "run-1", _log())
        assert engine.calls[0]["fundamentals_scores"] is None

    # -- AI review: off by default ----------------------------------------- #
    def test_ai_review_is_off_in_the_seeded_pipeline_config(self):
        assert default_configs.DEFAULT_RUNTIME_CONFIGS["pipeline"][
            "auto_invoke_ai_review"
        ] is False

    def test_default_run_invokes_step5_once_and_never_touches_ai_review(self):
        """Flag False => one propose() call, no ai_review_scores, zero API cost."""
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()

        def _must_not_run(*args, **kwargs):
            raise AssertionError("AI review provider invoked while flag is False")

        orch = _orchestrator(db, engine, ai_review_scores_provider=_must_not_run)
        orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        assert len(engine.calls) == 1
        assert "ai_review_scores" not in engine.calls[0]
        assert not [c for c in db.executed if "delete" in c[0].lower()]

    def test_enabled_flag_without_a_provider_keeps_first_pass(self):
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()
        orch = _orchestrator(
            db, engine, pipeline_overrides={"auto_invoke_ai_review": True}
        )
        result = orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        assert result.status == service_result.STATUS_SUCCESS
        assert len(engine.calls) == 1

    # -- AI review: explicitly on ------------------------------------------ #
    def test_enabled_flag_invokes_provider_and_step5_receives_scores(self):
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()
        seen: list[tuple] = []

        def _provider(signal_date, db_role, run_id, log):
            seen.append((signal_date, db_role, run_id))
            return {"AAPL": {"contrarian_risk_score": 20.0, "audit_consistency_score": 90.0}}

        orch = _orchestrator(
            db, engine,
            pipeline_overrides={"auto_invoke_ai_review": True},
            ai_review_scores_provider=_provider,
        )
        orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        assert seen == [(RUN_DATE, "prod", "run-1")]
        # Two passes: proposals must exist before they can be reviewed.
        assert len(engine.calls) == 2
        assert "ai_review_scores" not in engine.calls[0]
        second = engine.calls[1]
        assert second["ai_review_scores"]["AAPL"]["contrarian_risk_score"] == 20.0
        # Fundamentals still carried through on the rescore.
        assert second["fundamentals_scores"] == engine.calls[0]["fundamentals_scores"]

    def test_rescore_deletes_first_pass_rows_before_reinserting(self):
        """M15._write is INSERT-only; without the delete the date would double up."""
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()
        orch = _orchestrator(
            db, engine,
            pipeline_overrides={"auto_invoke_ai_review": True},
            ai_review_scores_provider=lambda *a: {"AAPL": {"contrarian_risk_score": 1.0}},
        )
        orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        deletes = [c for c in db.executed if "delete from step5_proposals" in c[0].lower()]
        assert len(deletes) == 1
        assert deletes[0][1] == [RUN_DATE]

    def test_provider_failure_keeps_first_pass_proposals(self):
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()

        def _boom(*args, **kwargs):
            raise RuntimeError("vendor 500")

        orch = _orchestrator(
            db, engine,
            pipeline_overrides={"auto_invoke_ai_review": True},
            ai_review_scores_provider=_boom,
        )
        result = orch._step_step5(RUN_DATE, "prod", "run-1", _log())

        assert result.status == service_result.STATUS_SUCCESS
        assert len(engine.calls) == 1
        assert not [c for c in db.executed if "delete from step5_proposals" in c[0].lower()]

    def test_empty_scores_do_not_trigger_a_rescore(self):
        db = _FakeDb(self._fundamentals_rows())
        engine = _RecordingProposalEngine()
        orch = _orchestrator(
            db, engine,
            pipeline_overrides={"auto_invoke_ai_review": True},
            ai_review_scores_provider=lambda *a: {},
        )
        orch._step_step5(RUN_DATE, "prod", "run-1", _log())
        assert len(engine.calls) == 1


# --------------------------------------------------------------------------- #
# Golden-dataset diff, through the real propose() path against a real DuckDB.
#
# The formula-level test above proves the term is inert. This proves the whole
# Step 5 path is: scores, ranking, disposition, risk labels -- every written
# column -- must be byte-identical whether or not M20 feeds fundamentals_scores,
# so long as the seeded (0.0) weight is in force.
# --------------------------------------------------------------------------- #
from tests.test_step5_proposal_engine import (  # noqa: E402
    DEFAULT_SETUP_CONFIGS,
    SIGNAL_DATE,
    _make_engine,
    _read_proposals,
    _seed_features,
    _seed_price,
    _seed_step4,
    _seed_ticker,
    tmp_db_paths,  # noqa: F401  (pytest fixture, used by name)
)
from app.database import duckdb_manager as dbm  # noqa: E402
from app.utils import service_result as sr  # noqa: E402


class TestGoldenDatasetByteIdentity:
    _TICKERS = ("ALPHA", "BRAVO", "CHARLIE", "DELTA")

    def _seed_scenario(self, db: str) -> None:
        # Varied setup_scores so ranking/top_n ordering is actually exercised.
        for i, ticker in enumerate(self._TICKERS):
            _seed_ticker(db, ticker)
            _seed_price(db, ticker, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, ticker, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(
                db, ticker, SIGNAL_DATE, setup_type="breakout",
                setup_config_id="setup_breakout_v1", setup_passed=True,
                setup_score=60.0 + i * 8.0, entry_price_raw=100.0,
                market_regime="bull", earnings_days=30,
            )

    def _propose(self, db: str, fundamentals_scores):
        result = _make_engine().propose(
            SIGNAL_DATE,
            risk_label_config=dict(default_configs.DEFAULT_RISK_LABEL_CONFIG),
            setup_configs=DEFAULT_SETUP_CONFIGS,
            db_role=db,
            fundamentals_scores=fundamentals_scores,
        )
        assert result.status == sr.STATUS_SUCCESS, result.errors
        return {p["ticker"]: p for p in _read_proposals(db, SIGNAL_DATE)}

    def _clear(self, db: str) -> None:
        conn = dbm.connect(db)
        try:
            conn.execute("DELETE FROM step5_proposals WHERE signal_date = ?", [SIGNAL_DATE])
        finally:
            conn.close()

    # Columns that legitimately differ per run (identity/timestamps).
    _VOLATILE = {"proposal_id", "run_id", "created_at"}

    def test_auto_invoked_fundamentals_change_nothing_under_seeded_weight(
        self, tmp_db_paths
    ):
        db = dbm.DB_ROLE_PROD
        self._seed_scenario(db)

        # "Before": pre-P2.5 behavior -- M20 fed nothing.
        before = self._propose(db, None)
        self._clear(db)

        # "After": M20 auto-invokes fundamentals for every ticker, including a
        # maximally-strong and a maximally-weak one, which at a live weight would
        # move both ranking and disposition in opposite directions.
        scores = {
            "ALPHA": 100.0, "BRAVO": 0.0, "CHARLIE": 75.0, "DELTA": 25.0,
        }
        after = self._propose(db, scores)

        assert set(before) == set(after)
        for ticker in before:
            b = {k: v for k, v in before[ticker].items() if k not in self._VOLATILE}
            a = {k: v for k, v in after[ticker].items() if k not in self._VOLATILE}
            assert a == b, f"{ticker} changed under an inert fundamentals weight"

    def test_step5_term_suppressed_end_to_end_when_m14_owns_fundamentals(
        self, tmp_db_paths
    ):
        """Engine-level double-credit guard: drives the real propose()/_build_rows.

        Unlike a predicate unit test, deleting the guard from _build_rows makes
        this fail. Both runs use a LIVE score_weight (0.10) and the same
        fundamentals score; they differ only in whether the setup_config hands
        ownership of the signal to M14.
        """
        db = dbm.DB_ROLE_PROD
        self._seed_scenario(db)

        activated = dict(default_configs.DEFAULT_RISK_LABEL_CONFIG)
        activated["fundamentals"] = {"score_weight": 0.10}
        scores = {t: 100.0 for t in self._TICKERS}

        m14_off = dict(DEFAULT_SETUP_CONFIGS)
        m14_on = dict(DEFAULT_SETUP_CONFIGS)
        m14_on["breakout"] = {
            **DEFAULT_SETUP_CONFIGS["breakout"],
            "fundamentals": {"enabled": True, "weight": 20.0},
        }

        def _run(setup_configs):
            result = _make_engine().propose(
                SIGNAL_DATE, risk_label_config=activated,
                setup_configs=setup_configs, db_role=db,
                fundamentals_scores=scores,
            )
            assert result.status == sr.STATUS_SUCCESS, result.errors
            return {p["ticker"]: p for p in _read_proposals(db, SIGNAL_DATE)}

        # Baseline: no fundamentals fed at all -> Step 5 term contributes nothing.
        no_fundamentals = self._propose(db, None)
        self._clear(db)

        # M14 does NOT own it -> Step 5 applies its term -> score rises.
        step5_owns = _run(m14_off)
        self._clear(db)

        # M14 DOES own it -> Step 5 must suppress its term -> back to baseline.
        m14_owns = _run(m14_on)

        for ticker in self._TICKERS:
            assert (
                step5_owns[ticker]["proposal_score_raw"]
                > no_fundamentals[ticker]["proposal_score_raw"]
            ), f"{ticker}: precondition, Step 5 term must be live here"
            assert (
                m14_owns[ticker]["proposal_score_raw"]
                == no_fundamentals[ticker]["proposal_score_raw"]
            ), f"{ticker}: Step 5 double-counted a signal M14 already owns"

    def test_same_scenario_does_move_once_the_weight_is_activated(self, tmp_db_paths):
        """Control: the golden test above passes because the weight is 0.0,
        not because fundamentals_scores is silently dropped on the floor."""
        db = dbm.DB_ROLE_PROD
        self._seed_scenario(db)
        before = self._propose(db, None)
        self._clear(db)

        activated = dict(default_configs.DEFAULT_RISK_LABEL_CONFIG)
        activated["fundamentals"] = {"score_weight": 0.10}
        result = _make_engine().propose(
            SIGNAL_DATE, risk_label_config=activated,
            setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db,
            fundamentals_scores={"ALPHA": 100.0, "BRAVO": 0.0},
        )
        assert result.status == sr.STATUS_SUCCESS, result.errors
        after = {p["ticker"]: p for p in _read_proposals(db, SIGNAL_DATE)}

        assert after["ALPHA"]["proposal_score_raw"] > before["ALPHA"]["proposal_score_raw"]
        assert after["BRAVO"]["proposal_score_raw"] < before["BRAVO"]["proposal_score_raw"]


# --------------------------------------------------------------------------- #
# Shared helper: single source of truth for the 0-100 formula.
# --------------------------------------------------------------------------- #
class TestSharedQualityHelper:
    def test_no_coverage_returns_none_not_zero(self):
        assert fq.compute_fundamentals_quality({}) is None

    def test_absent_fields_are_excluded_not_scored_as_zero(self):
        only_piotroski = fq.compute_fundamentals_quality({"piotroski_f_score": 9})
        assert only_piotroski == pytest.approx(100.0)

    def test_unknown_valuation_band_is_excluded(self):
        assert fq.compute_fundamentals_quality({"valuation_band": "unknown"}) is None

    def test_build_scores_omits_uncovered_tickers(self):
        scores = fq.build_fundamentals_scores({"A": _STRONG, "B": {}})
        assert "B" not in scores
        assert scores["A"] == pytest.approx(fq.compute_fundamentals_quality(_STRONG))
