"""Tests for the SEC-EDGAR-native insider_trade_flag wiring in
`pipeline_orchestrator.py`.

Originally (2026-07-18 coder note) `insider_trade_flag` was computed inline
inside `fundamentals_refresh`'s per-ticker loop, sharing ONE `_SecHttpClient`
with `EdgarFundamentalsProvider`'s own `companyfacts` fetches. The 2026-07-20
step-decoupling coder note moved it to its own `insider_flag_refresh` step
(measured ~82min at Step-4 scale, too expensive to keep paying inline every
`fundamentals_refresh` run) -- this file now proves: the `compute_insider_flag`
kill-switch still gates the lookup (now at the new step, not the provider
construction), `risk_label_config.fundamentals` thresholds are still read
correctly with a safe fallback, `_resolve_fundamentals_provider` no longer
wires ANY `insider_lookup` into the fundamentals-refresh-time provider, and
the new `_step_insider_flag` step correctly gates, iterates, updates, and
isolates per-ticker failures.

Fully offline.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app.providers import edgar_insider_provider as eip
from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
from app.utils import service_result as sr
from app.utils.service_result import ServiceResult

RUN_DATE = date(2026, 7, 15)


def _log():
    return logging.getLogger("test_pipeline_fundamentals_insider_wiring")


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=None):
        self._db.executed.append((sql, params))
        return _FakeCursor([])

    def close(self):
        pass


class _FakeDb:
    def __init__(self):
        self.executed: list[tuple] = []

    def connect(self, db_role, read_only=False):
        return _FakeConnection(self)


class _FakeConfigService:
    def __init__(self, pipeline_cfg=None, risk_cfg=None):
        self.pipeline_cfg = pipeline_cfg or {}
        self.risk_cfg = risk_cfg or {}

    def get_active_runtime_config(self, db_role, name):
        return ServiceResult(
            status=sr.STATUS_SUCCESS, run_id="r", rows_processed=1,
            metadata={"config_json": self.pipeline_cfg},
        )

    def get_active_risk_label_config(self, db_role):
        return ServiceResult(
            status=sr.STATUS_SUCCESS, run_id="r", rows_processed=1,
            metadata={"config_json": self.risk_cfg},
        )


def _orchestrator(db, **kw) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        db_manager=db,
        provider=object(),
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
        **kw,
    )


class _FakeHttpClient:
    def __init__(self):
        self.json_calls: list[str] = []
        self.text_calls: list[str] = []

    def get_json(self, url: str):
        self.json_calls.append(url)
        return {"insiderTransactionForIssuerExists": False}

    def get_text(self, url: str):
        self.text_calls.append(url)
        return "<ownershipDocument/>"


# --------------------------------------------------------------------------- #
# _pipeline_flags — compute_insider_flag default/override.
# --------------------------------------------------------------------------- #
class TestPipelineFlagsIncludeComputeInsiderFlag:
    def test_defaults_to_true_when_config_unreadable(self):
        class _Broken:
            def get_active_runtime_config(self, db_role, name):
                raise RuntimeError("boom")

        orch = _orchestrator(_FakeDb(), config_service=_Broken())
        flags = orch._pipeline_flags("prod", _log())
        assert flags["compute_insider_flag"] is True

    def test_reads_explicit_false_override(self):
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": False})
        orch = _orchestrator(_FakeDb(), config_service=cfg_service)
        flags = orch._pipeline_flags("prod", _log())
        assert flags["compute_insider_flag"] is False


# --------------------------------------------------------------------------- #
# _insider_flag_config — risk_label_config threshold reads.
# --------------------------------------------------------------------------- #
class TestInsiderFlagConfig:
    def test_reads_thresholds_from_risk_label_config(self):
        cfg_service = _FakeConfigService(
            risk_cfg={
                "fundamentals": {
                    "insider_purchase_lookback_days": 30,
                    "min_insider_transaction_value_usd": 5000.0,
                    "exclude_10b5_1": False,
                }
            }
        )
        orch = _orchestrator(_FakeDb(), config_service=cfg_service)
        cfg = orch._insider_flag_config("prod", _log())
        assert cfg == {
            "insider_purchase_lookback_days": 30,
            "min_insider_transaction_value_usd": 5000.0,
            "exclude_10b5_1": False,
        }

    def test_falls_back_to_module_defaults_on_config_read_failure(self):
        class _BrokenConfigService:
            def get_active_risk_label_config(self, db_role):
                raise RuntimeError("boom")

        orch = _orchestrator(_FakeDb(), config_service=_BrokenConfigService())
        cfg = orch._insider_flag_config("prod", _log())
        assert cfg["insider_purchase_lookback_days"] == eip.DEFAULT_LOOKBACK_DAYS
        assert cfg["min_insider_transaction_value_usd"] == eip.DEFAULT_MIN_TRANSACTION_VALUE_USD
        assert cfg["exclude_10b5_1"] == eip.DEFAULT_EXCLUDE_10B5_1


# --------------------------------------------------------------------------- #
# _build_insider_lookup — the kill-switch and the closure it builds.
# --------------------------------------------------------------------------- #
class TestBuildInsiderLookup:
    def test_disabled_returns_none(self):
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": False})
        orch = _orchestrator(_FakeDb(), config_service=cfg_service)
        result = orch._build_insider_lookup("prod", _log(), _FakeHttpClient())
        assert result is None

    def test_enabled_returns_a_callable_using_the_shared_http_client(self):
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        orch = _orchestrator(_FakeDb(), config_service=cfg_service)
        http_client = _FakeHttpClient()

        lookup = orch._build_insider_lookup("prod", _log(), http_client)
        assert callable(lookup)

        result = lookup("GME", RUN_DATE, "0001326380")
        assert result is False  # insiderTransactionForIssuerExists=False from the fake
        # Confirms the closure actually reached through to the SAME shared
        # client instance, not a second independent one.
        assert http_client.json_calls == [
            "https://data.sec.gov/submissions/CIK0001326380.json"
        ]

    def test_enabled_lookup_uses_configured_thresholds(self, monkeypatch: pytest.MonkeyPatch):
        cfg_service = _FakeConfigService(
            pipeline_cfg={"compute_insider_flag": True},
            risk_cfg={
                "fundamentals": {
                    "insider_purchase_lookback_days": 45,
                    "min_insider_transaction_value_usd": 2500.0,
                    "exclude_10b5_1": False,
                }
            },
        )
        orch = _orchestrator(_FakeDb(), config_service=cfg_service)
        http_client = _FakeHttpClient()

        captured_kwargs = {}
        original = eip.fetch_insider_purchase_flag

        def _spy(ticker, as_of_date, cik, fetch_json, fetch_filing_xml, **kwargs):
            captured_kwargs.update(kwargs)
            return original(ticker, as_of_date, cik, fetch_json, fetch_filing_xml, **kwargs)

        monkeypatch.setattr(
            "app.providers.edgar_insider_provider.fetch_insider_purchase_flag", _spy
        )
        lookup = orch._build_insider_lookup("prod", _log(), http_client)
        lookup("GME", RUN_DATE, "0001326380")

        assert captured_kwargs["lookback_days"] == 45
        assert captured_kwargs["min_transaction_value_usd"] == 2500.0
        assert captured_kwargs["exclude_10b5_1"] is False


# --------------------------------------------------------------------------- #
# _resolve_fundamentals_provider — 2026-07-20: never wires insider_lookup
# anymore, regardless of compute_insider_flag. That's now exclusively
# _step_insider_flag's job (see TestStepInsiderFlagRefresh below).
# --------------------------------------------------------------------------- #
class TestResolveFundamentalsProviderNoLongerWiresInsiderLookup:
    def test_injected_provider_is_used_verbatim(self):
        sentinel = object()
        orch = _orchestrator(_FakeDb(), fundamentals_provider=sentinel)
        assert orch._resolve_fundamentals_provider("prod", RUN_DATE, _log()) is sentinel

    def test_default_provider_has_fetch_json_but_no_insider_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        fake_client = _FakeHttpClient()
        monkeypatch.setattr(
            "app.providers.edgar_provider.build_sec_http_client", lambda **kw: fake_client
        )
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        orch = _orchestrator(_FakeDb(), config_service=cfg_service)

        provider = orch._resolve_fundamentals_provider("prod", RUN_DATE, _log())

        assert provider._fetch_json == fake_client.get_json
        # Even with compute_insider_flag=True, fundamentals_refresh-time
        # construction never wires a live insider_lookup anymore -- no SEC
        # submissions/filing-XML requests are reachable from this provider.
        assert provider._insider_lookup is None

    def test_compute_insider_flag_disabled_also_leaves_insider_lookup_none(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        fake_client = _FakeHttpClient()
        monkeypatch.setattr(
            "app.providers.edgar_provider.build_sec_http_client", lambda **kw: fake_client
        )
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": False})
        orch = _orchestrator(_FakeDb(), config_service=cfg_service)

        provider = orch._resolve_fundamentals_provider("prod", RUN_DATE, _log())
        assert provider._insider_lookup is None


# --------------------------------------------------------------------------- #
# _step_insider_flag — the new decoupled step (2026-07-20).
# --------------------------------------------------------------------------- #
_TICKER_CIK_MAP = {
    "0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."},
    "1": {"ticker": "GME", "cik_str": 1326380, "title": "GameStop Corp."},
}


class _StepDbCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _StepDbConnection:
    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=None):
        self._db.executed.append((sql, params))
        sql_upper = sql.upper()
        if sql_upper.startswith("SELECT") and "TICKER_MASTER" in sql_upper:
            return _StepDbCursor([(t,) for t in self._db.tickers])
        if sql_upper.startswith("SELECT") and "TICKER_FUNDAMENTALS" in sql_upper:
            return _StepDbCursor([(t,) for t in self._db.existing_rows])
        return _StepDbCursor([])

    def close(self):
        pass


class _StepDb:
    """Ticker universe (``ticker_master``) vs. existing ``ticker_fundamentals``
    rows are independently controllable, so tests can exercise a ticker that's
    active but has no fundamentals row yet (the 0-row-UPDATE case)."""

    def __init__(self, tickers=(), existing_rows=None):
        self.tickers = list(tickers)
        self.existing_rows = list(tickers) if existing_rows is None else list(existing_rows)
        self.executed: list[tuple] = []

    def connect(self, db_role, read_only=False):
        return _StepDbConnection(self)


class _CikAwareHttpClient:
    """Serves both company_tickers.json (CIK resolution) and
    submissions.json (insider lookup) from one fake client, mirroring the
    one-client-per-step design of the real ``_step_insider_flag``."""

    def __init__(self, insider_exists=False, cik_map=None):
        self.json_calls: list[str] = []
        self.text_calls: list[str] = []
        self._insider_exists = insider_exists
        self._cik_map = cik_map if cik_map is not None else _TICKER_CIK_MAP

    def get_json(self, url: str):
        self.json_calls.append(url)
        if "company_tickers.json" in url:
            return self._cik_map
        return {"insiderTransactionForIssuerExists": self._insider_exists}

    def get_text(self, url: str):
        self.text_calls.append(url)
        return "<ownershipDocument/>"


def _patch_client(monkeypatch: pytest.MonkeyPatch, http_client) -> None:
    monkeypatch.setattr(
        "app.providers.edgar_provider.build_sec_http_client", lambda **kw: http_client
    )


def _isolate_cik_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Redirect the on-disk company_tickers.json cache off the real
    data/cache/ directory (same isolation convention as test_edgar_provider.py
    passing an explicit cache_path -- here the cache-owning provider is built
    *inside* _step_insider_flag, so settings.CACHE_DIR is what's redirected)."""
    from app.config import settings

    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path, raising=False)


class TestStepInsiderFlagRegisteredCorrectly:
    def test_in_step_names_between_fundamentals_and_price(self):
        from app.services.pipeline.pipeline_orchestrator import STEP_NAMES

        f_idx = STEP_NAMES.index("fundamentals_refresh")
        i_idx = STEP_NAMES.index("insider_flag_refresh")
        p_idx = STEP_NAMES.index("price_ingestion")
        assert f_idx < i_idx < p_idx

    def test_recoverable_not_critical(self):
        from app.services.pipeline.pipeline_orchestrator import (
            CRITICAL_STEPS,
            RECOVERABLE_STEPS,
        )

        assert "insider_flag_refresh" in RECOVERABLE_STEPS
        assert "insider_flag_refresh" not in CRITICAL_STEPS


class TestStepInsiderFlagRefresh:
    def test_disabled_via_compute_insider_flag_skips_everything(self):
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": False})
        db = _StepDb(tickers=["AAPL"])
        orch = _orchestrator(db, config_service=cfg_service)

        result = orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        assert result.status == sr.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 0
        assert any("disabled via compute_insider_flag=False" in w for w in result.warnings)
        # Gate fires before any DB read -- no ticker_master/ticker_fundamentals query at all.
        assert db.executed == []

    def test_no_active_tickers_warns_without_failing(self):
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        db = _StepDb(tickers=[])
        orch = _orchestrator(db, config_service=cfg_service)

        result = orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        assert result.status == sr.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 0
        assert any("no active stock tickers" in w for w in result.warnings)

    def test_happy_path_updates_ticker_fundamentals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        _isolate_cik_cache(monkeypatch, tmp_path)
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        db = _StepDb(tickers=["GME"])
        http_client = _CikAwareHttpClient(insider_exists=False)
        _patch_client(monkeypatch, http_client)
        orch = _orchestrator(db, config_service=cfg_service)

        result = orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        assert result.status == sr.STATUS_SUCCESS
        assert result.rows_processed == 1
        updates = [e for e in db.executed if e[0].upper().startswith("UPDATE")]
        assert len(updates) == 1
        assert updates[0][1] == [False, "GME", RUN_DATE]
        # CIK resolution and the insider submissions check both went through
        # the one client this step built for itself.
        assert any("company_tickers.json" in u for u in http_client.json_calls)
        assert any("CIK0001326380" in u for u in http_client.json_calls)

    def test_ticker_with_no_fundamentals_row_is_skipped_and_warned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        _isolate_cik_cache(monkeypatch, tmp_path)
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        # AAPL is active but has no ticker_fundamentals row yet (its
        # fundamentals_refresh fetch failed that day) -- GME does.
        db = _StepDb(tickers=["AAPL", "GME"], existing_rows=["GME"])
        http_client = _CikAwareHttpClient()
        _patch_client(monkeypatch, http_client)
        orch = _orchestrator(db, config_service=cfg_service)

        result = orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        assert result.rows_processed == 1  # GME only
        assert any(
            "1/2" in w and "no ticker_fundamentals row" in w for w in result.warnings
        )
        # AAPL never triggers an SEC EDGAR insider lookup -- nothing to write.
        submission_calls = [u for u in http_client.json_calls if "submissions" in u]
        assert len(submission_calls) == 1

    def test_unresolvable_cik_is_a_warning_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        _isolate_cik_cache(monkeypatch, tmp_path)
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        db = _StepDb(tickers=["UNKNOWNTICKER"])
        http_client = _CikAwareHttpClient()
        _patch_client(monkeypatch, http_client)
        orch = _orchestrator(db, config_service=cfg_service)

        result = orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        assert result.status == sr.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 0
        assert any("no SEC CIK mapping found" in w for w in result.warnings)

    def test_per_ticker_exception_does_not_abort_the_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        _isolate_cik_cache(monkeypatch, tmp_path)
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        db = _StepDb(tickers=["GME", "AAPL"])
        http_client = _CikAwareHttpClient()
        _patch_client(monkeypatch, http_client)
        orch = _orchestrator(db, config_service=cfg_service)

        real_build = orch._build_insider_lookup

        def _flaky_build(db_role, log, client):
            inner = real_build(db_role, log, client)

            def _wrapped(ticker, as_of_date, cik):
                if ticker == "AAPL":
                    raise RuntimeError("boom")
                return inner(ticker, as_of_date, cik)

            return _wrapped

        orch._build_insider_lookup = _flaky_build

        result = orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        assert result.status == sr.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 1  # GME landed; AAPL didn't
        assert any("AAPL" in w and "raised" in w for w in result.warnings)

    def test_all_tickers_missing_rows_returns_success_with_warnings_zero_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        _isolate_cik_cache(monkeypatch, tmp_path)
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        db = _StepDb(tickers=["AAPL"], existing_rows=[])
        http_client = _CikAwareHttpClient()
        _patch_client(monkeypatch, http_client)
        orch = _orchestrator(db, config_service=cfg_service)

        result = orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        assert result.status == sr.STATUS_SUCCESS_WITH_WARNINGS
        assert result.rows_processed == 0
        # Never even attempted an SEC EDGAR request for AAPL.
        assert http_client.json_calls == []

    def test_update_writes_only_insider_trade_flag_column(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """Pins the UPDATE's shape -- must not touch the other 5 EDGAR fields
        fundamentals_refresh already wrote for this ticker/date."""
        _isolate_cik_cache(monkeypatch, tmp_path)
        cfg_service = _FakeConfigService(pipeline_cfg={"compute_insider_flag": True})
        db = _StepDb(tickers=["GME"])
        http_client = _CikAwareHttpClient(insider_exists=False)
        _patch_client(monkeypatch, http_client)
        orch = _orchestrator(db, config_service=cfg_service)

        orch._step_insider_flag(RUN_DATE, "prod", "r1", _log())

        updates = [e for e in db.executed if e[0].upper().startswith("UPDATE")]
        assert len(updates) == 1
        sql = updates[0][0]
        assert "SET insider_trade_flag = ?" in sql
        assert "eps_growth_trend" not in sql
        assert "WHERE ticker = ? AND as_of_date = ?" in sql
