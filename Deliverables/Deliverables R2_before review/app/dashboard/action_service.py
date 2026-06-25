"""Dashboard action service (M21 V2 button wiring).

This module is the **only** bridge between the Streamlit UI and the backend
service layer.  Every mutation or heavy operation originating from a dashboard
button flows through here.  ``app.py`` must not import or instantiate service
classes directly — it calls :class:`DashboardActionService` instead.

Contracts
---------
- No Streamlit import.
- No direct ``duckdb`` import; all DB access goes through the injected
  ``db_manager`` (or the real manager loaded lazily at call time).
- Returns :class:`~app.utils.service_result.ServiceResult` on every path.
- Validates ``db_role`` **before** constructing any service object that would
  touch the DB.
- Never calls provider APIs or recomputes upstream module logic.

Actions
-------
run_pipeline(db_role, run_date, run_type) -> ServiceResult
    Delegates to ``PipelineOrchestrator.run()``.

export_ticker_review(db_role, signal_date, strategy_config_id, proposal_ids)
    Delegates to ``ExportPackageEngine.export_ticker_review()``.
    Produces a reviewer-facing ZIP and records one ``ai_reviews`` row.

send_ticker_review(db_role, ai_review_id) -> ServiceResult
    Delegates to ``AiReviewEngine.send_ticker_review()``.

record_human_action(db_role, ai_review_id, human_action) -> ServiceResult
    Delegates to ``AiReviewEngine.record_human_action()``.

activate_strategy_config(db_role, config_id, activated_by, reason)
    Delegates to ``ConfigService.activate_strategy_config()``.

clone_strategy_config(db_role, strategy_name, config_json, version, ...)
    Delegates to ``ConfigService.create_strategy_config_version()``.

export_strategy_config_csv(db_role, config_id) -> ServiceResult
    Reads one strategy config and returns CSV bytes in metadata.

export_proposals_csv(db_role, signal_date, strategy_config_id, proposal_ids)
    Reads live DB rows for the supplied proposal ids and returns CSV bytes.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date
from typing import Any, Protocol

from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

_LOG = logging_config.get_logger(__name__)

# Allowed roles for write actions.
_WRITE_ROLES = frozenset({"prod", "debug"})

# ------------------------------------------------------------------ #
# Protocol stubs for test injection.
# ------------------------------------------------------------------ #


class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class _PipelineOrchestratorLike(Protocol):
    def run(
        self,
        run_date: date,
        run_type: str,
        db_role: str,
        force_rerun: bool,
        strategy_configs: dict | None,
        run_id: str | None,
    ) -> ServiceResult: ...


class _ExportEngineLike(Protocol):
    def export_ticker_review(
        self,
        signal_date: date,
        strategy_config_id: str,
        proposal_ids: list[str],
        db_role: str,
        run_id: str | None,
    ) -> ServiceResult: ...


class _AiReviewEngineLike(Protocol):
    def send_ticker_review(
        self, ai_review_id: str, db_role: str, run_id: str | None
    ) -> ServiceResult: ...

    def record_human_action(
        self,
        ai_review_id: str,
        human_action: str,
        db_role: str,
        run_id: str | None,
    ) -> ServiceResult: ...


class _ConfigServiceLike(Protocol):
    def activate_strategy_config(
        self,
        config_id: str,
        db_role: str,
        activated_by: str | None,
        reason: str | None,
    ) -> ServiceResult: ...

    def create_strategy_config_version(
        self,
        db_role: str,
        strategy_name: str,
        config_json: dict[str, Any],
        version: str | None,
        parent_config_id: str | None,
        created_by: str | None,
        notes: str | None,
        activate: bool,
    ) -> ServiceResult: ...

    def get_strategy_config(
        self, config_id: str, db_role: str
    ) -> ServiceResult: ...

    def list_strategy_configs(
        self, db_role: str, strategy_name: str | None
    ) -> ServiceResult: ...


# ------------------------------------------------------------------ #
# CSV helpers (no Streamlit, no DB).
# ------------------------------------------------------------------ #

_STRATEGY_CSV_COLS = [
    "config_id",
    "strategy_name",
    "version",
    "active_flag",
    "config_hash",
    "created_at",
    "created_by",
    "notes",
]

_PROPOSALS_CSV_COLS = [
    "proposal_id",
    "signal_date",
    "ticker",
    "strategy_config_id",
    "raw_rank",
    "diversified_rank",
    "proposal_score_raw",
    "proposal_score_final",
    "in_raw_top_n",
    "in_diversified_top_n",
    "setup_type",
    "estimated_rr",
    "sector",
    "industry",
    "mechanical_explanation",
]


def _dict_to_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    """Serialize *rows* to UTF-8 CSV bytes, emitting only *columns* in order."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=columns,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _config_row_to_csv_dict(cfg: dict[str, Any]) -> dict[str, Any]:
    """Flatten a config dict for CSV export (config_json excluded)."""
    out: dict[str, Any] = {}
    for col in _STRATEGY_CSV_COLS:
        out[col] = cfg.get(col, "")
    return out


# ------------------------------------------------------------------ #
# Action service.
# ------------------------------------------------------------------ #


class DashboardActionService:
    """Adapter layer between Streamlit button handlers and backend services.

    All constructor arguments are optional; when ``None`` the corresponding
    service is lazily instantiated from the real production modules.  Inject
    fakes for offline testing.

    Parameters
    ----------
    db_manager:
        Injected for tests only.  When ``None``, the real
        ``app.database.duckdb_manager`` is used (imported lazily).
    pipeline_orchestrator:
        Injected for tests only.
    export_engine:
        Injected for tests only.
    ai_review_engine:
        Injected for tests only.
    config_service:
        Injected for tests only.
    """

    def __init__(
        self,
        db_manager: _DbManagerLike | None = None,
        pipeline_orchestrator: _PipelineOrchestratorLike | None = None,
        export_engine: _ExportEngineLike | None = None,
        ai_review_engine: _AiReviewEngineLike | None = None,
        config_service: _ConfigServiceLike | None = None,
    ) -> None:
        self._db = db_manager  # may remain None; used only for proposals CSV
        self._pipeline = pipeline_orchestrator
        self._export = export_engine
        self._ai_review = ai_review_engine
        self._config = config_service

    # ------------------------------------------------------------------ #
    # Lazy service constructors (production path only).
    # ------------------------------------------------------------------ #

    def _get_pipeline(self) -> _PipelineOrchestratorLike:
        if self._pipeline is None:
            from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator
            from app.providers.yahoo_provider import YahooProvider
            from app.providers.provider_interface import TickerInfo

            symbol_source: list[TickerInfo] = []
            try:
                db = self._get_db()
                conn = db.connect("prod", read_only=True)
                try:
                    rows = conn.execute(
                        "SELECT ticker, yahoo_symbol, company_name, exchange, "
                        "sector, industry, security_type, symbol_type "
                        "FROM ticker_master "
                        "WHERE active_flag = true AND delisted_flag = false"
                    ).fetchall()
                finally:
                    conn.close()
                symbol_source = [
                    TickerInfo(
                        ticker=r[0],
                        company_name=r[2] or None,
                        exchange=r[3] or None,
                        sector=r[4] or None,
                        industry=r[5] or None,
                        security_type=r[6] or None,
                        symbol_type=r[7] or "stock",
                    )
                    for r in rows
                ]
                _LOG.info("symbol_source loaded: %d tickers", len(symbol_source))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("could not load symbol_source: %s", exc)

            provider = YahooProvider(symbol_source=symbol_source if symbol_source else None)
            self._pipeline = PipelineOrchestrator(provider=provider)
        return self._pipeline

    def _get_export(self) -> _ExportEngineLike:
        if self._export is None:
            from app.services.export.export_package_engine import (
                ExportPackageEngine,
            )

            self._export = ExportPackageEngine(db_manager=None)
        return self._export

    def _get_ai_review(self) -> _AiReviewEngineLike:
        if self._ai_review is None:
            from app.services.ai_review.ai_review_engine import AiReviewEngine

            self._ai_review = AiReviewEngine(db_manager=None)
        return self._ai_review

    def _get_config(self) -> _ConfigServiceLike:
        if self._config is None:
            from app.services.config.config_service import ConfigService

            self._config = ConfigService(db_manager=None)
        return self._config

    def _get_db(self) -> Any:
        if self._db is None:
            from app.database import duckdb_manager

            self._db = duckdb_manager
        return self._db

    # ------------------------------------------------------------------ #
    # Internal helpers.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _failed(run_id: str, error: str, meta: dict[str, Any]) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            errors=[error],
            metadata={**meta, "error": error},
        )

    @staticmethod
    def _validate_write_role(db_role: str, run_id: str) -> str | None:
        """Return an error string if role is not allowed, else ``None``."""
        if db_role not in _WRITE_ROLES:
            return f"db_role {db_role!r} not allowed for write actions; must be one of {sorted(_WRITE_ROLES)}"
        return None

    # ------------------------------------------------------------------ #
    # Public actions.
    # ------------------------------------------------------------------ #

    def run_pipeline(
        self,
        db_role: str = "prod",
        run_date: date | None = None,
        run_type: str = "manual",
        force_rerun: bool = False,
        strategy_configs: dict | None = None,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Trigger a pipeline run.

        Delegates to :class:`~app.services.pipeline.pipeline_orchestrator.PipelineOrchestrator`.
        Returns its ``ServiceResult`` verbatim so the caller sees step-level
        error detail.
        """
        rid = run_id or str(uuid.uuid4())
        role_err = self._validate_write_role(db_role, rid)
        if role_err:
            return self._failed(rid, role_err, {"db_role": db_role, "action": "run_pipeline"})

        effective_date = run_date or date.today()
        _LOG.info("dashboard action: run_pipeline date=%s role=%s rid=%s", effective_date, db_role, rid)
        try:
            result = self._get_pipeline().run(
                run_date=effective_date,
                run_type=run_type,
                db_role=db_role,
                force_rerun=force_rerun,
                strategy_configs=strategy_configs,
                run_id=rid,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "run_pipeline"})
        return result

    def export_ticker_review(
        self,
        signal_date: date,
        strategy_config_id: str,
        proposal_ids: list[str],
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Create a ticker-review ZIP via :class:`~app.services.export.export_package_engine.ExportPackageEngine`.

        Validates inputs here so the Streamlit layer never calls the engine
        with empty lists.
        """
        rid = run_id or str(uuid.uuid4())
        role_err = self._validate_write_role(db_role, rid)
        if role_err:
            return self._failed(rid, role_err, {"db_role": db_role, "action": "export_ticker_review"})
        if not strategy_config_id:
            return self._failed(rid, "strategy_config_id must not be empty", {"db_role": db_role, "action": "export_ticker_review"})
        if not proposal_ids:
            return self._failed(rid, "proposal_ids must not be empty; select at least one row", {"db_role": db_role, "action": "export_ticker_review"})

        _LOG.info(
            "dashboard action: export_ticker_review date=%s config=%s proposals=%d role=%s rid=%s",
            signal_date,
            strategy_config_id,
            len(proposal_ids),
            db_role,
            rid,
        )
        try:
            result = self._get_export().export_ticker_review(
                signal_date=signal_date,
                strategy_config_id=strategy_config_id,
                proposal_ids=proposal_ids,
                db_role=db_role,
                run_id=rid,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "export_ticker_review"})
        return result

    def send_ticker_review(
        self,
        ai_review_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Send an existing ``ai_reviews`` row to the AI provider."""
        rid = run_id or str(uuid.uuid4())
        role_err = self._validate_write_role(db_role, rid)
        if role_err:
            return self._failed(rid, role_err, {"db_role": db_role, "action": "send_ticker_review"})
        if not ai_review_id:
            return self._failed(rid, "ai_review_id must not be empty", {"db_role": db_role, "action": "send_ticker_review"})

        _LOG.info("dashboard action: send_ticker_review id=%s role=%s rid=%s", ai_review_id, db_role, rid)
        try:
            result = self._get_ai_review().send_ticker_review(
                ai_review_id=ai_review_id,
                db_role=db_role,
                run_id=rid,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "send_ticker_review"})
        return result

    def record_human_action(
        self,
        ai_review_id: str,
        human_action: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Record the reviewer's qualitative decision on an AI review row."""
        rid = run_id or str(uuid.uuid4())
        role_err = self._validate_write_role(db_role, rid)
        if role_err:
            return self._failed(rid, role_err, {"db_role": db_role, "action": "record_human_action"})
        if not ai_review_id:
            return self._failed(rid, "ai_review_id must not be empty", {"db_role": db_role, "action": "record_human_action"})

        _LOG.info(
            "dashboard action: record_human_action id=%s action=%s role=%s rid=%s",
            ai_review_id,
            human_action,
            db_role,
            rid,
        )
        try:
            result = self._get_ai_review().record_human_action(
                ai_review_id=ai_review_id,
                human_action=human_action,
                db_role=db_role,
                run_id=rid,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "record_human_action"})
        return result

    def activate_strategy_config(
        self,
        config_id: str,
        db_role: str,
        activated_by: str | None = None,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Activate a strategy config version; deactivates same-name siblings."""
        rid = run_id or str(uuid.uuid4())
        role_err = self._validate_write_role(db_role, rid)
        if role_err:
            return self._failed(rid, role_err, {"db_role": db_role, "action": "activate_strategy_config"})
        if not config_id:
            return self._failed(rid, "config_id must not be empty", {"db_role": db_role, "action": "activate_strategy_config"})

        _LOG.info("dashboard action: activate_strategy_config id=%s role=%s rid=%s", config_id, db_role, rid)
        try:
            result = self._get_config().activate_strategy_config(
                config_id=config_id,
                db_role=db_role,
                activated_by=activated_by,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "activate_strategy_config"})
        return result

    def clone_strategy_config(
        self,
        db_role: str,
        strategy_name: str,
        config_json: dict[str, Any],
        version: str | None = None,
        parent_config_id: str | None = None,
        created_by: str | None = None,
        notes: str | None = None,
        activate: bool = False,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Create a new strategy config version (clone / save-as).

        ``activate=False`` by default; the caller must call
        :meth:`activate_strategy_config` separately to make the new version
        active, or pass ``activate=True`` to do it atomically.
        """
        rid = run_id or str(uuid.uuid4())
        role_err = self._validate_write_role(db_role, rid)
        if role_err:
            return self._failed(rid, role_err, {"db_role": db_role, "action": "clone_strategy_config"})
        if not strategy_name:
            return self._failed(rid, "strategy_name must not be empty", {"db_role": db_role, "action": "clone_strategy_config"})
        if not config_json:
            return self._failed(rid, "config_json must not be empty", {"db_role": db_role, "action": "clone_strategy_config"})

        _LOG.info(
            "dashboard action: clone_strategy_config name=%s role=%s activate=%s rid=%s",
            strategy_name,
            db_role,
            activate,
            rid,
        )
        try:
            result = self._get_config().create_strategy_config_version(
                db_role=db_role,
                strategy_name=strategy_name,
                config_json=config_json,
                version=version,
                parent_config_id=parent_config_id,
                created_by=created_by,
                notes=notes,
                activate=activate,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "clone_strategy_config"})
        return result

    def export_strategy_config_csv(
        self,
        config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Return CSV bytes for a single strategy config in ``metadata['csv_bytes']``.

        The CSV covers config metadata only (``config_json`` is omitted to keep
        the download human-readable; it is available separately via
        :meth:`get_strategy_config`).
        """
        rid = run_id or str(uuid.uuid4())
        # Read-only; prod and debug are both fine.
        if db_role not in _WRITE_ROLES:
            return self._failed(rid, f"db_role {db_role!r} not in {sorted(_WRITE_ROLES)}", {"db_role": db_role, "action": "export_strategy_config_csv"})
        if not config_id:
            return self._failed(rid, "config_id must not be empty", {"db_role": db_role, "action": "export_strategy_config_csv"})

        _LOG.info("dashboard action: export_strategy_config_csv id=%s role=%s rid=%s", config_id, db_role, rid)
        try:
            sr = self._get_config().get_strategy_config(config_id=config_id, db_role=db_role)
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "export_strategy_config_csv"})

        if sr.status == service_result.STATUS_FAILED:
            return sr

        cfg = sr.metadata.get("config", {})
        csv_bytes = _dict_to_csv([_config_row_to_csv_dict(cfg)], _STRATEGY_CSV_COLS)
        strategy_name = cfg.get("strategy_name", "unknown")
        version = cfg.get("version", "v?")
        filename = f"strategy_{strategy_name}_{version}.csv".replace(" ", "_")
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=rid,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_id": config_id,
                "strategy_name": strategy_name,
                "version": version,
                "csv_bytes": csv_bytes,
                "filename": filename,
            },
        )

    def export_proposals_csv(
        self,
        signal_date: date,
        proposal_ids: list[str],
        db_role: str = "prod",
        strategy_config_id: str | None = None,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Query live DB rows for *proposal_ids* and return CSV bytes.

        Uses a **read-only** connection so this is safe to call from the
        dashboard's read path.  Returns ``metadata['csv_bytes']`` and
        ``metadata['filename']``.
        """
        rid = run_id or str(uuid.uuid4())
        if db_role not in _WRITE_ROLES:
            return self._failed(rid, f"db_role {db_role!r} not in {sorted(_WRITE_ROLES)}", {"db_role": db_role, "action": "export_proposals_csv"})
        if not proposal_ids:
            return self._failed(rid, "proposal_ids must not be empty; select at least one row", {"db_role": db_role, "action": "export_proposals_csv"})

        _LOG.info(
            "dashboard action: export_proposals_csv date=%s proposals=%d role=%s rid=%s",
            signal_date,
            len(proposal_ids),
            db_role,
            rid,
        )
        try:
            rows = self._fetch_proposals_by_ids(signal_date, proposal_ids, db_role)
        except Exception as exc:  # noqa: BLE001
            return self._failed(rid, f"{type(exc).__name__}: {exc}", {"db_role": db_role, "action": "export_proposals_csv"})

        csv_bytes = _dict_to_csv(rows, _PROPOSALS_CSV_COLS)
        filename = f"proposals_{signal_date}_{db_role}.csv"
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=rid,
            rows_processed=len(rows),
            metadata={
                "db_role": db_role,
                "signal_date": str(signal_date),
                "csv_bytes": csv_bytes,
                "filename": filename,
                "rows": len(rows),
            },
        )

    # ------------------------------------------------------------------ #
    # Internal DB helpers (read-only; used only for proposals CSV).
    # ------------------------------------------------------------------ #

    def _fetch_proposals_by_ids(
        self,
        signal_date: date,
        proposal_ids: list[str],
        db_role: str,
    ) -> list[dict[str, Any]]:
        """Read step5_proposals rows joined to step4/ticker_master for given ids."""
        if not proposal_ids:
            return []

        db = self._get_db()
        placeholders = ", ".join(["?"] * len(proposal_ids))
        sql = (
            "SELECT "
            "p.proposal_id, p.signal_date, p.ticker, p.strategy_config_id, "
            "p.raw_rank, p.diversified_rank, "
            "p.proposal_score_raw, p.proposal_score_final, "
            "p.in_raw_top_n, p.in_diversified_top_n, "
            "a.setup_type, a.estimated_rr, "
            "m.sector, m.industry, "
            "p.mechanical_explanation "
            "FROM step5_proposals p "
            "LEFT JOIN step4_analysis a "
            "  ON a.run_id = p.run_id AND a.ticker = p.ticker "
            "  AND a.signal_date = p.signal_date "
            "  AND a.strategy_config_id = p.strategy_config_id "
            "LEFT JOIN ticker_master m ON m.ticker = p.ticker "
            f"WHERE p.proposal_id IN ({placeholders}) "
            "AND p.signal_date = ? "
            "ORDER BY p.raw_rank ASC NULLS LAST, p.ticker ASC"
        )
        params = list(proposal_ids) + [signal_date]
        conn = db.connect(db_role, read_only=True)
        try:
            cursor = conn.execute(sql, params)
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
