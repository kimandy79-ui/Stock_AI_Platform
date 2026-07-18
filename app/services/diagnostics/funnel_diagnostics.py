"""Setup-mode Funnel Diagnostics Service (Phase 6 — M20/M22).

Reads post-run pipeline tables and persists structured metric rows to
``pipeline_run_diagnostics``.

Diagnostic categories
---------------------
Pipeline-level (setup_type = NULL):
  eligibility.total_input / .passed / .failed / .feature_ready / .feature_missing
  eligibility.rejection_reason.<reason>
  routing.not_routed / .ineligible
  risk_label.low / .medium / .high
  proposal.buy_eligible / .watchlist / .rejected / .final_count
  proposal.rejection_reason.<reason>          ← Phase 6 addition
  risk.failure_reason.<reason>                ← Phase 6 addition
  timing.step_duration_sec.<step_name>        ← Phase 6 addition
  config.snapshot (metadata_json per setup_type + risk_label_config) ← Phase 6 addition

Per-setup (setup_type = breakout|pullback|trend_continuation|consolidation_base):
  routing.routed
  validation.passed / .failed
  validation.failure_reason.<reason>

All writes go into ``pipeline_run_diagnostics`` (Phase 6 schema addition).
Read connections are read-only; the single insert batch uses one write connection.

Rules: no DDL, no ATTACH, no direct duckdb import, no print().
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from typing import Any, Final

from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result as sr
from app.utils.service_result import ServiceResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_VALID_DB_ROLES: Final[frozenset[str]] = frozenset({"prod", "debug"})

ACTIVE_SETUP_TYPES: Final[tuple[str, ...]] = (
    "breakout",
    "pullback",
    "trend_continuation",
    "consolidation_base",
)

_STEP_ELIGIBILITY: Final[str] = "step3_universal_eligibility"
_STEP_ROUTING: Final[str] = "step3_routing"
_STEP_VALIDATION: Final[str] = "step4_setup_validation"
_STEP_RISK: Final[str] = "step5_risk_label"
_STEP_PROPOSALS: Final[str] = "step5_proposals"
_STEP_CONFIG: Final[str] = "config_snapshot"
_STEP_TIMING: Final[str] = "pipeline_timing"

# ---------------------------------------------------------------------------
# Read SQL
# ---------------------------------------------------------------------------
_SQL_S3_SUMMARY: Final[str] = """
SELECT
    COUNT(*) AS total,
    SUM(CASE WHEN passed_eligibility = TRUE  THEN 1 ELSE 0 END) AS passed,
    SUM(CASE WHEN passed_eligibility = FALSE THEN 1 ELSE 0 END) AS failed,
    SUM(CASE WHEN routing_status = 'routed'    THEN 1 ELSE 0 END) AS routed,
    SUM(CASE WHEN routing_status = 'no_route'  THEN 1 ELSE 0 END) AS not_routed,
    SUM(CASE WHEN routing_status = 'ineligible' THEN 1 ELSE 0 END) AS ineligible
FROM step3_candidates
WHERE run_id = ? AND signal_date = ?
"""

_SQL_S3_FEATURE_READY: Final[str] = """
SELECT
    SUM(CASE WHEN feature_snapshot_json IS NOT NULL
             AND json_extract(feature_snapshot_json, '$.feature_ready') = TRUE
             THEN 1 ELSE 0 END) AS feature_ready
FROM step3_candidates
WHERE run_id = ? AND signal_date = ?
"""

_SQL_S3_ELIGIBILITY_REASONS: Final[str] = """
SELECT eligibility_fail_reasons
FROM step3_candidates
WHERE run_id = ? AND signal_date = ? AND passed_eligibility = FALSE
"""

_SQL_S3_ROUTED_TYPES: Final[str] = """
SELECT routed_setup_types
FROM step3_candidates
WHERE run_id = ? AND signal_date = ? AND routing_status = 'routed'
"""

_SQL_S4_SUMMARY: Final[str] = """
SELECT
    setup_type,
    SUM(CASE WHEN setup_passed = TRUE  THEN 1 ELSE 0 END) AS passed,
    SUM(CASE WHEN setup_passed = FALSE THEN 1 ELSE 0 END) AS failed
FROM step4_analysis
WHERE run_id = ? AND signal_date = ?
GROUP BY setup_type
"""

_SQL_S4_FAIL_REASONS: Final[str] = """
SELECT setup_type, setup_fail_reason, COUNT(*) AS cnt
FROM step4_analysis
WHERE run_id = ? AND signal_date = ? AND setup_passed = FALSE
GROUP BY setup_type, setup_fail_reason
ORDER BY setup_type, cnt DESC
"""

_SQL_S5_SUMMARY: Final[str] = """
SELECT risk_label, disposition, COUNT(*) AS cnt
FROM step5_proposals
WHERE run_id = ? AND signal_date = ?
GROUP BY risk_label, disposition
"""

_SQL_S5_TOTAL: Final[str] = """
SELECT COUNT(*) FROM step5_proposals WHERE run_id = ? AND signal_date = ?
"""

_SQL_S5_REJECTION_REASONS: Final[str] = """
SELECT rejection_reason, COUNT(*) AS cnt
FROM step5_proposals
WHERE run_id = ? AND signal_date = ?
  AND rejection_reason IS NOT NULL
GROUP BY rejection_reason
ORDER BY cnt DESC
"""

_SQL_S5_RISK_REASONS: Final[str] = """
SELECT risk_reasons
FROM step5_proposals
WHERE run_id = ? AND signal_date = ?
  AND risk_reasons IS NOT NULL
"""

_SQL_SETUP_CONFIGS: Final[str] = """
SELECT config_id, setup_type, version, config_hash
FROM setup_configs
WHERE active_flag = TRUE
ORDER BY setup_type
"""

_SQL_RISK_CONFIG: Final[str] = """
SELECT config_id, version, config_hash
FROM risk_label_config
WHERE active_flag = TRUE
LIMIT 1
"""

# ---------------------------------------------------------------------------
# Read-report SQL (used by build_report — read-only, no persist)
# ---------------------------------------------------------------------------
_SQL_RESOLVE_RUN_ID: Final[str] = (
    "SELECT run_id FROM step3_candidates "
    "WHERE signal_date = ? ORDER BY created_at DESC LIMIT 1"
)

_SQL_S3_REPORT: Final[str] = (
    "SELECT ticker, routing_status, routed_setup_types, "
    "passed_eligibility, eligibility_fail_reasons "
    "FROM step3_candidates WHERE run_id = ? AND signal_date = ?"
)

_SQL_S4_REPORT: Final[str] = (
    "SELECT ticker, setup_type, setup_score, setup_passed, setup_fail_reason, "
    "rvol, atr_pct, distance_to_ema20_pct, distance_to_ema50_pct, "
    "explanation_json "
    "FROM step4_analysis WHERE run_id = ? AND signal_date = ?"
)

_SQL_S5_REPORT: Final[str] = (
    "SELECT ticker, setup_type, setup_score, estimated_rr, "
    "entry_price_raw, stop_price_raw, "
    "disposition, selected_flag, rejection_reason, mechanical_explanation "
    "FROM step5_proposals WHERE run_id = ? AND signal_date = ?"
)

# ---------------------------------------------------------------------------
# Insert SQL
# ---------------------------------------------------------------------------
_SQL_INSERT_DIAG: Final[str] = (
    "INSERT INTO pipeline_run_diagnostics "
    "(diag_id, run_id, signal_date, db_role, step_name, setup_type, "
    "metric_name, metric_value, reason, metadata_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------
def _row(
    run_id: str,
    signal_date: date,
    db_role: str,
    step_name: str,
    metric_name: str,
    metric_value: float | int | None,
    setup_type: str | None = None,
    reason: str | None = None,
    metadata_json: dict | None = None,
) -> list[Any]:
    return [
        str(uuid.uuid4()),
        run_id,
        signal_date.isoformat(),
        db_role,
        step_name,
        setup_type,
        metric_name,
        float(metric_value) if metric_value is not None else None,
        reason,
        json.dumps(metadata_json) if metadata_json else None,
    ]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class SetupModeFunnelDiagnosticsService:
    """Compute and persist setup-mode funnel diagnostics.

    Called by PipelineOrchestrator after Step 5 completes.

    Usage::

        svc = SetupModeFunnelDiagnosticsService(db_manager=mgr)
        result = svc.run(
            signal_date=date(2026, 6, 15),
            db_role="prod",
            run_id=run_id,
            step_timings={"step3_universal_eligibility": 4.2, ...},
        )
    """

    def __init__(self, db_manager: Any | None = None) -> None:
        self._db = db_manager if db_manager is not None else duckdb_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(
        self,
        signal_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
        step_timings: dict[str, float] | None = None,
    ) -> ServiceResult:
        """Compute funnel diagnostics and persist to pipeline_run_diagnostics.

        Parameters
        ----------
        signal_date:
            The pipeline signal date.
        db_role:
            ``'prod'`` or ``'debug'``.
        run_id:
            Pipeline run_id; a new UUID is minted when None.
        step_timings:
            Optional dict of ``{step_name: duration_seconds}`` from the
            orchestrator.  Each entry produces a
            ``timing.step_duration_sec.<step_name>`` row.
        """
        run_id = run_id or str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        if db_role not in _VALID_DB_ROLES:
            msg = f"invalid db_role {db_role!r}; must be one of {sorted(_VALID_DB_ROLES)}"
            return ServiceResult(
                status=sr.STATUS_FAILED, run_id=run_id,
                errors=[msg],
                metadata={"db_role": db_role, "signal_date": signal_date.isoformat()},
            )

        log.info(
            "SetupModeFunnelDiagnostics start signal_date=%s db_role=%s",
            signal_date, db_role,
        )

        try:
            rows = self._collect_metrics(
                signal_date, db_role, run_id, log, step_timings or {}
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("diagnostics collection failed: %s", exc)
            return ServiceResult(
                status=sr.STATUS_FAILED, run_id=run_id,
                errors=[f"diagnostics collection failed: {exc}"],
                metadata={"db_role": db_role, "signal_date": signal_date.isoformat()},
            )

        if not rows:
            log.warning(
                "no diagnostic metrics collected for run_id=%s signal_date=%s",
                run_id, signal_date,
            )
            return ServiceResult(
                status=sr.STATUS_SUCCESS, run_id=run_id, rows_processed=0,
                warnings=["no diagnostic metrics collected; pipeline data may be empty"],
                metadata={
                    "db_role": db_role,
                    "signal_date": signal_date.isoformat(),
                    "metrics_written": 0,
                },
            )

        try:
            self._persist(rows, db_role, log)
        except Exception as exc:  # noqa: BLE001
            log.exception("diagnostics persist failed: %s", exc)
            return ServiceResult(
                status=sr.STATUS_FAILED, run_id=run_id,
                errors=[f"diagnostics persist failed: {exc}"],
                metadata={
                    "db_role": db_role,
                    "signal_date": signal_date.isoformat(),
                    "metrics_collected": len(rows),
                    "metrics_written": 0,
                },
            )

        log.info("SetupModeFunnelDiagnostics complete: %d rows written", len(rows))
        return ServiceResult(
            status=sr.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(rows),
            metadata={
                "db_role": db_role,
                "signal_date": signal_date.isoformat(),
                "metrics_written": len(rows),
            },
        )

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    def _collect_metrics(
        self,
        signal_date: date,
        db_role: str,
        run_id: str,
        log: Any,
        step_timings: dict[str, float],
    ) -> list[list[Any]]:
        rows: list[list[Any]] = []
        sd_iso = signal_date.isoformat()
        params = [run_id, sd_iso]

        conn = self._db.connect(db_role)
        try:
            total = 0

            # ---- Step 3 eligibility summary --------------------------------
            s3 = conn.execute(_SQL_S3_SUMMARY, params).fetchone()
            if s3 is not None:
                total, passed, failed, _routed, not_routed, ineligible = (
                    int(v or 0) for v in s3
                )
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_ELIGIBILITY, "eligibility.total_input", total))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_ELIGIBILITY, "eligibility.passed", passed))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_ELIGIBILITY, "eligibility.failed", failed))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_ROUTING, "routing.not_routed", not_routed))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_ROUTING, "routing.ineligible", ineligible))

            # ---- Feature-ready count ----------------------------------------
            try:
                fr = conn.execute(_SQL_S3_FEATURE_READY, params).fetchone()
                feature_ready = int(fr[0]) if fr and fr[0] is not None else 0
                feature_missing = total - feature_ready
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_ELIGIBILITY, "eligibility.feature_ready", feature_ready))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_ELIGIBILITY, "eligibility.feature_missing", feature_missing))
            except Exception:  # noqa: BLE001
                pass

            # ---- Eligibility rejection reasons ------------------------------
            try:
                reason_counts: dict[str, int] = {}
                for (raw,) in conn.execute(_SQL_S3_ELIGIBILITY_REASONS, params).fetchall():
                    if raw is None:
                        continue
                    reasons = raw if isinstance(raw, list) else (
                        json.loads(raw) if isinstance(raw, str) else []
                    )
                    for r in (reasons or []):
                        if r:
                            reason_counts[str(r)] = reason_counts.get(str(r), 0) + 1
                for reason, cnt in reason_counts.items():
                    rows.append(_row(
                        run_id, signal_date, db_role,
                        _STEP_ELIGIBILITY,
                        f"eligibility.rejection_reason.{reason}",
                        cnt, reason=reason,
                    ))
            except Exception:  # noqa: BLE001
                pass

            # ---- Routing counts by setup_type --------------------------------
            try:
                type_counts: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
                for (raw,) in conn.execute(_SQL_S3_ROUTED_TYPES, params).fetchall():
                    types = raw if isinstance(raw, list) else (
                        json.loads(raw) if isinstance(raw, str) else []
                    )
                    for st in (types or []):
                        if st in type_counts:
                            type_counts[st] += 1
                for st, cnt in type_counts.items():
                    rows.append(_row(
                        run_id, signal_date, db_role,
                        _STEP_ROUTING, "routing.routed", cnt, setup_type=st,
                    ))
            except Exception:  # noqa: BLE001
                pass

            # ---- Step 4 pass/fail by setup_type -----------------------------
            try:
                for setup_type, p, f in conn.execute(_SQL_S4_SUMMARY, params).fetchall():
                    rows.append(_row(run_id, signal_date, db_role,
                                     _STEP_VALIDATION, "validation.passed",
                                     int(p or 0), setup_type=setup_type))
                    rows.append(_row(run_id, signal_date, db_role,
                                     _STEP_VALIDATION, "validation.failed",
                                     int(f or 0), setup_type=setup_type))
            except Exception:  # noqa: BLE001
                pass

            # ---- Step 4 failure reasons by setup_type -----------------------
            # Reasons may contain numeric suffixes like "rvol_below_hard_threshold(0.91<1.5)".
            # metric_name must use stable category key only; numeric examples go in metadata_json.
            try:
                example_map: dict[tuple[str, str], list[str]] = {}
                count_map: dict[tuple[str, str], int] = {}
                for setup_type, raw_reason, cnt in conn.execute(_SQL_S4_FAIL_REASONS, params).fetchall():
                    reason_key, example = _normalize_validation_reason(raw_reason)
                    k = (setup_type or "", reason_key)
                    count_map[k] = count_map.get(k, 0) + int(cnt or 0)
                    if example:
                        example_map.setdefault(k, []).append(example)
                for (setup_type, reason_key), cnt in count_map.items():
                    examples = example_map.get((setup_type, reason_key), [])
                    meta: dict | None = {"examples": examples[:5]} if examples else None
                    rows.append(_row(
                        run_id, signal_date, db_role,
                        _STEP_VALIDATION,
                        f"validation.failure_reason.{reason_key}",
                        cnt, setup_type=setup_type or None, reason=reason_key,
                        metadata_json=meta,
                    ))
            except Exception:  # noqa: BLE001
                pass

            # ---- Step 5 risk labels + disposition ---------------------------
            try:
                risk_counts: dict[str, int] = {}
                buy_count = watchlist_count = rejected_count = 0
                for risk_label, disposition, cnt in conn.execute(_SQL_S5_SUMMARY, params).fetchall():
                    cnt = int(cnt or 0)
                    if risk_label:
                        risk_counts[risk_label] = risk_counts.get(risk_label, 0) + cnt
                    if disposition == "BUY":
                        buy_count += cnt
                    elif disposition == "WATCHLIST_ONLY":
                        watchlist_count += cnt
                    elif disposition == "REJECTED":
                        rejected_count += cnt

                for label in ("low", "medium", "high"):
                    rows.append(_row(run_id, signal_date, db_role,
                                     _STEP_RISK, f"risk_label.{label}",
                                     risk_counts.get(label, 0)))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_PROPOSALS, "proposal.buy_eligible", buy_count))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_PROPOSALS, "proposal.watchlist", watchlist_count))
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_PROPOSALS, "proposal.rejected", rejected_count))
            except Exception:  # noqa: BLE001
                pass

            # ---- Final proposal count ----------------------------------------
            try:
                fc = conn.execute(_SQL_S5_TOTAL, params).fetchone()
                rows.append(_row(run_id, signal_date, db_role,
                                 _STEP_PROPOSALS, "proposal.final_count",
                                 int(fc[0]) if fc else 0))
            except Exception:  # noqa: BLE001
                pass

            # ---- Proposal rejection reasons (Phase 6) -----------------------
            try:
                for reason, cnt in conn.execute(_SQL_S5_REJECTION_REASONS, params).fetchall():
                    label = reason or "unknown"
                    rows.append(_row(
                        run_id, signal_date, db_role,
                        _STEP_PROPOSALS,
                        f"proposal.rejection_reason.{label}",
                        int(cnt or 0), reason=reason,
                    ))
            except Exception:  # noqa: BLE001
                pass

            # ---- Stop / target / RR / risk failure breakdown (Phase 6) ------
            # risk_reasons is a JSON array of strings like
            # ["stop_distance_score=45.2", "atr_score=72.1", ...]
            # (keys use _score suffix since Phase 7 cleanup).
            # metric_name must NEVER contain numeric values — the score is
            # stripped by _classify_risk_reason and stored in metadata_json.
            try:
                risk_reason_counts: dict[str, int] = {}
                risk_reason_scores: dict[str, list[float]] = {}
                for (raw,) in conn.execute(_SQL_S5_RISK_REASONS, params).fetchall():
                    if raw is None:
                        continue
                    reasons = raw if isinstance(raw, list) else (
                        json.loads(raw) if isinstance(raw, str) else []
                    )
                    for r in (reasons or []):
                        r_str = str(r)
                        key = _classify_risk_reason(r_str)
                        risk_reason_counts[key] = risk_reason_counts.get(key, 0) + 1
                        parts = r_str.split("=", 1)
                        if len(parts) == 2:
                            try:
                                score = float(parts[1])
                                risk_reason_scores.setdefault(key, []).append(score)
                            except ValueError:
                                pass

                for key, cnt in risk_reason_counts.items():
                    score_examples = risk_reason_scores.get(key, [])
                    meta: dict | None = (
                        {"score_examples": score_examples[:5]} if score_examples else None
                    )
                    rows.append(_row(
                        run_id, signal_date, db_role,
                        _STEP_RISK, key, cnt, reason=key,
                        metadata_json=meta,
                    ))
            except Exception:  # noqa: BLE001
                pass

            # ---- Config snapshot (Phase 6) ----------------------------------
            try:
                setup_cfg_rows = conn.execute(_SQL_SETUP_CONFIGS, []).fetchall()
                for config_id, setup_type, version, config_hash in setup_cfg_rows:
                    rows.append(_row(
                        run_id, signal_date, db_role,
                        _STEP_CONFIG, "config.setup_config",
                        None, setup_type=setup_type,
                        metadata_json={
                            "config_id": config_id,
                            "version": version,
                            "config_hash": config_hash,
                            "setup_type": setup_type,
                        },
                    ))
            except Exception:  # noqa: BLE001
                pass

            try:
                rl_row = conn.execute(_SQL_RISK_CONFIG, []).fetchone()
                if rl_row:
                    config_id, version, config_hash = rl_row
                    rows.append(_row(
                        run_id, signal_date, db_role,
                        _STEP_CONFIG, "config.risk_label_config",
                        None, setup_type=None,
                        metadata_json={
                            "config_id": config_id,
                            "version": version,
                            "config_hash": config_hash,
                        },
                    ))
            except Exception:  # noqa: BLE001
                pass

        finally:
            conn.close()

        # ---- Timing rows (Phase 6) -------------------------------------------
        for step_name, duration in (step_timings or {}).items():
            rows.append(_row(
                run_id, signal_date, db_role,
                _STEP_TIMING,
                f"timing.step_duration_sec.{step_name}",
                duration, reason=step_name,
            ))

        return rows

    # ------------------------------------------------------------------
    # Persist
    # ------------------------------------------------------------------
    def _persist(self, rows: list[list[Any]], db_role: str, log: Any) -> None:
        conn = self._db.connect(db_role, read_only=False)
        try:
            conn.execute("BEGIN")
            for row_params in rows:
                conn.execute(_SQL_INSERT_DIAG, row_params)
            conn.execute("COMMIT")
            log.debug("diagnostics: inserted %d rows", len(rows))
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read-back helper
    # ------------------------------------------------------------------
    def read(
        self,
        run_id: str,
        signal_date: date,
        db_role: str = "prod",
    ) -> list[dict[str, Any]]:
        """Return all diagnostic rows for a given run_id / signal_date."""
        conn = self._db.connect(db_role)
        try:
            rows = conn.execute(
                "SELECT step_name, setup_type, metric_name, metric_value, reason, metadata_json "
                "FROM pipeline_run_diagnostics "
                "WHERE run_id = ? AND signal_date = ? "
                "ORDER BY step_name, setup_type, metric_name",
                [run_id, signal_date.isoformat()],
            ).fetchall()
        finally:
            conn.close()
        cols = ["step_name", "setup_type", "metric_name", "metric_value", "reason", "metadata_json"]
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------------
    # Read-report (CLI / ad-hoc analysis; does not persist anything)
    # ------------------------------------------------------------------
    def build_report(
        self,
        signal_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
        top_n_borderline: int = 10,
        setup_type_filter: str | None = None,
    ) -> ServiceResult:
        """Query pipeline tables and return a rich diagnostic report.

        Does NOT write to ``pipeline_run_diagnostics``.  The report dict is
        returned in ``result.metadata["report"]``.
        """
        if db_role not in _VALID_DB_ROLES:
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id or "",
                errors=[f"invalid db_role {db_role!r}"],
                metadata={"db_role": db_role, "signal_date": signal_date.isoformat()},
            )

        conn = self._db.connect(db_role)
        try:
            if run_id is None:
                row = conn.execute(
                    _SQL_RESOLVE_RUN_ID, [signal_date.isoformat()]
                ).fetchone()
                if not row:
                    return ServiceResult(
                        status=sr.STATUS_FAILED,
                        run_id="",
                        errors=[f"no pipeline data found for signal_date={signal_date}"],
                        metadata={"db_role": db_role, "signal_date": signal_date.isoformat()},
                    )
                run_id = row[0]

            params = [run_id, signal_date.isoformat()]
            s3_raw = conn.execute(_SQL_S3_REPORT, params).fetchall()
            s4_raw = conn.execute(_SQL_S4_REPORT, params).fetchall()
            s5_raw = conn.execute(_SQL_S5_REPORT, params).fetchall()
        finally:
            conn.close()

        _S3C = ("ticker", "routing_status", "routed_setup_types",
                "passed_eligibility", "eligibility_fail_reasons")
        _S4C = ("ticker", "setup_type", "setup_score", "setup_passed", "setup_fail_reason",
                "rvol", "atr_pct", "distance_to_ema20_pct", "distance_to_ema50_pct",
                "explanation_json")
        _S5C = ("ticker", "setup_type", "setup_score", "estimated_rr",
                "entry_price_raw", "stop_price_raw",
                "disposition", "selected_flag", "rejection_reason", "mechanical_explanation")

        s3 = [dict(zip(_S3C, r)) for r in s3_raw]
        s4 = [dict(zip(_S4C, r)) for r in s4_raw]
        s5 = [dict(zip(_S5C, r)) for r in s5_raw]

        for r in s3:
            r["routed_setup_types"]       = _parse_json_list(r["routed_setup_types"])
            r["eligibility_fail_reasons"] = _parse_json_list(r["eligibility_fail_reasons"])
        for r in s4:
            r["explanation_json"] = _parse_json_dict(r["explanation_json"])
        for r in s5:
            r["mechanical_explanation"] = _parse_json_dict(r["mechanical_explanation"])
            ep = _flt(r.get("entry_price_raw"))
            sp = _flt(r.get("stop_price_raw"))
            if r.get("stop_distance_pct") is None and ep and sp and ep > 0:
                r["stop_distance_pct"] = (ep - sp) / ep

        report: dict[str, Any] = {
            "signal_date": signal_date.isoformat(),
            "db_role": db_role,
            "run_id": run_id,
            "total_s3": len(s3),
            "total_s4": len(s4),
            "total_s5": len(s5),
        }
        report["routing_summary"]        = _rpt_routing(s3)
        report["eligibility_rejection_reasons"] = _rpt_eligibility_rejection_reasons(s3)
        report["setup_funnel"]           = _rpt_setup_funnel(s3, s4, s5, setup_type_filter)
        report["failure_reasons"]        = _rpt_failure_reasons(s4, setup_type_filter)
        report["co_occurring_failure_reasons"] = _rpt_co_occurring_failure_reasons(s4, setup_type_filter)
        report["s5_rejection_reasons"]   = _rpt_s5_rejection_reasons(s5)
        report["evidence_summaries"]     = _rpt_evidence(s4, s5, setup_type_filter)
        report["borderline_failures"]    = _rpt_borderline(s4, top_n_borderline, setup_type_filter)
        report["failure_layers"]         = _rpt_layers(s3, s4, s5)
        report["final_trade_decisions"]  = _rpt_ftd(s5)
        report["diagnostic_warnings"]   = _rpt_warnings(s3, s4, s5, setup_type_filter)

        return ServiceResult(
            status=sr.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(s3) + len(s4) + len(s5),
            metadata={"report": report},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_STOP_PREFIXES = ("stop_distance", "stop_price", "stop_basis")
_TARGET_PREFIXES = ("target_room", "target_price", "target_is_structural")
_RR_PREFIXES = ("estimated_rr", "rr_", "min_rr")


def _classify_risk_reason(raw: str) -> str:
    """Map a raw risk_reason string to a metric key in ``<category>.failure_reason.<label>`` form.

    Input format (from step5 risk_reasons): ``"<label>=<score>"`` where
    ``<label>`` is a ``_score``-suffixed key (e.g. ``"stop_distance_score=45.2"``)
    and ``<score>`` is a 0-100 component score value.

    The numeric ``=<score>`` part is always stripped before building the
    ``metric_name``.  Numeric values must never appear in ``metric_name``
    (they belong in ``metadata_json`` or ``metric_value``).

    Category mapping:
    - stop.*     → ``stop.failure_reason.<label>``
    - target.*   → ``target.failure_reason.<label>``
    - rr.*       → ``rr.failure_reason.<label>``
    - everything else → ``risk.failure_reason.<label>``
    """
    # Strip the numeric score value; keep the category label only.
    label = raw.split("=")[0].strip().lower()
    if any(label.startswith(p) for p in _STOP_PREFIXES):
        return f"stop.failure_reason.{label}"
    if any(label.startswith(p) for p in _TARGET_PREFIXES):
        return f"target.failure_reason.{label}"
    if any(label.startswith(p) for p in _RR_PREFIXES):
        return f"rr.failure_reason.{label}"
    return f"risk.failure_reason.{label}"




def _parse_json_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if v is None:
        return []
    try:
        r = json.loads(v)
        return r if isinstance(r, list) else []
    except Exception:  # noqa: BLE001
        return []


def _parse_json_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if v is None:
        return {}
    try:
        r = json.loads(v)
        return r if isinstance(r, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _flt(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _stats_of(values: list[Any]) -> dict[str, Any]:
    vals = [v for v in (_flt(x) for x in values) if v is not None]
    if not vals:
        return {"n": 0, "min": None, "mean": None, "max": None,
                "p25": None, "p50": None, "p75": None}
    n = len(vals)
    s = sorted(vals)

    def _q(frac: float) -> float:
        return round(s[max(0, min(n - 1, int(n * frac)))], 4)

    return {
        "n": n,
        "min": round(s[0], 4),
        "mean": round(sum(vals) / n, 4),
        "max": round(s[-1], 4),
        "p25": _q(0.25),
        "p50": _q(0.50),
        "p75": _q(0.75),
    }


def _pct_f(n: int, d: int) -> float:
    return round(n / d, 4) if d > 0 else 0.0


def _collect(d: dict[str, list], key: str, val: Any) -> None:
    v = _flt(val)
    if v is not None:
        d.setdefault(key, []).append(v)


def _extract_numeric(s: str) -> str:
    """From 'close=12.43' return '12.43'; from '57.0' return '57.0'."""
    s = s.strip()
    return s.split("=", 1)[1].strip() if "=" in s else s


def _parse_example(example: str | None) -> tuple[str | None, str | None, str | None]:
    """Split 'actual<threshold' → ('actual', 'threshold', '<').

    Handles plain numerics ('0.91<1.5') and labelled pairs
    ('close=12.43>base_high=12.42'). Ignores '=' as a separator so that
    labelled-pair format is not mis-split.

    Returns (actual_value, threshold_value, direction) where direction is
    '<' or '>' (None when unparseable).
    """
    if not example:
        return None, None, None
    for sep in ("<", ">"):
        if sep in example:
            left, right = example.split(sep, 1)
            return _extract_numeric(left), _extract_numeric(right), sep
    return example.strip(), None, None


def _rpt_routing(s3: list[dict]) -> dict:
    total = len(s3)
    passed = sum(1 for r in s3 if r.get("passed_eligibility"))
    routed = sum(1 for r in s3 if r["routing_status"] == "routed")
    not_routed = sum(
        1 for r in s3
        if r["routing_status"] == "no_route" and r.get("passed_eligibility")
    )
    ineligible = total - passed
    multi = sum(1 for r in s3 if len(r["routed_setup_types"]) > 1)
    by_setup: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    for r in s3:
        for st in r["routed_setup_types"]:
            if st in by_setup:
                by_setup[st] += 1
    return {
        "total_universe": total,
        "passed_eligibility": passed,
        "failed_eligibility": ineligible,
        "routed": routed,
        "not_routed": not_routed,
        "ineligible_no_route": ineligible,
        "multi_routed": multi,
        "by_setup": by_setup,
    }


def _rpt_eligibility_rejection_reasons(s3: list[dict]) -> list[dict]:
    """Aggregate step3 eligibility fail reasons over ineligible candidates.

    Item C (CODER_NOTE 2026-07-13): surfaces the ``eligibility_fail_reasons``
    already loaded per step3 row (equivalent to the persisted
    ``eligibility.rejection_reason.<reason>`` metrics) so the report can show
    which step3 gates — e.g. the M13 merger filter (``merger_pending``) — are
    firing.  Returns ``{reason, count, pct_of_ineligible}`` sorted by count desc.
    """
    ineligible = [r for r in s3 if not r.get("passed_eligibility")]
    total_ineligible = len(ineligible)
    counts: dict[str, int] = {}
    for r in ineligible:
        for reason in (r.get("eligibility_fail_reasons") or []):
            if reason:
                counts[str(reason)] = counts.get(str(reason), 0) + 1

    result = []
    for reason, cnt in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        result.append({
            "reason": reason,
            "count": cnt,
            "pct_of_ineligible": _pct_f(cnt, total_ineligible),
        })
    return result


def _rpt_setup_funnel(
    s3: list[dict],
    s4: list[dict],
    s5: list[dict],
    setup_type_filter: str | None,
) -> list[dict]:
    routed: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    for r in s3:
        for st in r["routed_setup_types"]:
            if st in routed:
                routed[st] += 1

    s4_pass: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    s4_fail: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    for r in s4:
        st = r.get("setup_type") or ""
        if st in s4_pass:
            if r["setup_passed"]:
                s4_pass[st] += 1
            else:
                s4_fail[st] += 1

    s5_cnt: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    s5_sel: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    for r in s5:
        st = r.get("setup_type") or ""
        if st in s5_cnt:
            s5_cnt[st] += 1
            if r["selected_flag"]:
                s5_sel[st] += 1

    result = []
    for st in ACTIVE_SETUP_TYPES:
        if setup_type_filter and st != setup_type_filter:
            continue
        pass_c = s4_pass[st]
        fail_c = s4_fail[st]
        total_v = pass_c + fail_c
        result.append({
            "setup_type": st,
            "routed_count": routed[st],
            "validator_pass_count": pass_c,
            "validator_fail_count": fail_c,
            "pass_rate": _pct_f(pass_c, total_v),
            "step4_count": total_v,
            "step5_count": s5_cnt[st],
            "selected_count": s5_sel[st],
        })
    return result


def _rpt_failure_reasons(s4: list[dict], setup_type_filter: str | None) -> list[dict]:
    fail_total: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    counts: dict[tuple[str, str], int] = {}
    examples: dict[tuple[str, str], list[tuple[str | None, str | None, str]]] = {}
    for r in s4:
        st = r.get("setup_type") or ""
        if st not in ACTIVE_SETUP_TYPES:
            continue
        if setup_type_filter and st != setup_type_filter:
            continue
        if not r["setup_passed"]:
            fail_total[st] += 1
            key, example = _normalize_validation_reason(r.get("setup_fail_reason"))
            k = (st, key)
            counts[k] = counts.get(k, 0) + 1
            ex_list = examples.setdefault(k, [])
            if len(ex_list) < 3:
                actual, threshold, direction = _parse_example(example)
                ex_list.append((actual, threshold, direction, r.get("ticker") or "?"))

    result = []
    for (st, reason), cnt in sorted(counts.items(), key=lambda x: (x[0][0], -x[1])):
        ex = examples.get((st, reason), [])
        result.append({
            "setup_type": st,
            "failure_reason": reason,
            "count": cnt,
            "pct_of_setup_failures": _pct_f(cnt, fail_total[st]),
            "actual_value": ex[0][0] if ex else None,
            "threshold": ex[0][1] if ex else None,
            "direction": ex[0][2] if ex else None,
            "sample_tickers": [e[3] for e in ex],
        })
    return result


def _rpt_co_occurring_failure_reasons(
    s4: list[dict], setup_type_filter: str | None
) -> list[dict]:
    """P2-G secondary failure-reason breakdown: for each setup_type's failing
    population, group by the FIRST ``hard_fails`` reason (the same value
    reported as ``setup_fail_reason`` / surfaced by ``_rpt_failure_reasons``),
    then report which OTHER reasons appear elsewhere in that row's full
    ``hard_fails`` list.

    Source: ``explanation_json["hard_fails"]`` — already fetched by
    ``_SQL_S4_REPORT`` and already parsed into a dict at the ``build_report``
    call site (``r["explanation_json"] = _parse_json_dict(...)``). No new
    query, no schema change; ``m14_setup_validators.py`` writes this same
    list into every validator's ``evidence_json["hard_fails"]``, which
    ``step4_setup_validation_engine.py`` persists verbatim as
    ``explanation_json`` (see P2-G investigation,
    ``reports/P2_G_breakout_gate_ordering_investigation_2026-07-18.md``).

    Generalizes across all four setup types: whichever setup's ``hard_fails``
    happens to have more than one entry for a given row contributes to that
    setup's breakdown — nothing breakout-specific here.

    Percentages are against the first-reason cohort size (how many rows share
    that same first reason), not the setup's total failure count — a
    candidate whose first reason is X answers "given X, how often does Y also
    apply", which needs the X-cohort as the denominator.
    """
    cohort_size: dict[tuple[str, str], int] = {}
    co_counts: dict[tuple[str, str, str], int] = {}

    for r in s4:
        st = r.get("setup_type") or ""
        if st not in ACTIVE_SETUP_TYPES:
            continue
        if setup_type_filter and st != setup_type_filter:
            continue
        if r["setup_passed"]:
            continue
        expl = r.get("explanation_json") or {}
        hard_fails = expl.get("hard_fails") or []
        if not hard_fails:
            continue  # soft-score-only fail (no hard_fails entries) — nothing to break down
        first_key, _ = _normalize_validation_reason(hard_fails[0])
        cohort_k = (st, first_key)
        cohort_size[cohort_k] = cohort_size.get(cohort_k, 0) + 1
        seen_others: set[str] = set()
        for hf in hard_fails[1:]:
            other_key, _ = _normalize_validation_reason(hf)
            if other_key in seen_others:
                continue  # count each co-occurring category once per row
            seen_others.add(other_key)
            k = (st, first_key, other_key)
            co_counts[k] = co_counts.get(k, 0) + 1

    result = []
    for (st, first_key, other_key), cnt in sorted(
        co_counts.items(), key=lambda x: (x[0][0], x[0][1], -x[1])
    ):
        cohort_n = cohort_size.get((st, first_key), 0)
        result.append({
            "setup_type": st,
            "first_reason": first_key,
            "co_occurring_reason": other_key,
            "count": cnt,
            "cohort_size": cohort_n,
            "pct_of_first_reason_cohort": _pct_f(cnt, cohort_n),
        })
    return result


def _rpt_evidence(
    s4: list[dict],
    s5: list[dict],
    setup_type_filter: str | None,
) -> dict[str, dict]:
    """Summarise step4/step5 evidence fields per setup_type.

    Item B (CODER_NOTE 2026-07-13): the evidence output is split into two
    explicit populations per setup so that no field silently mixes counts:

    - ``routed``    — statistics over **all** step4 rows for that setup_type
      (pass + fail).  ``setup_score`` belongs here: every routed candidate has
      a score regardless of whether it passed validation.
    - ``validated`` — statistics over only ``setup_passed == True`` step4 rows
      (the gate-input fields ``rvol``/``atr_pct``/… only make sense for
      candidates that cleared the validator).  The step5-derived ``*_s5`` fields
      also live here: a step5 proposal only exists for a validated candidate.

    Each setup entry also carries the population **row** counts ``routed_n`` and
    ``validated_n`` (distinct from any single field's non-null ``n``), so the
    report can label ``breakout — routed (n=713)`` vs
    ``breakout — validated (n=14)`` unambiguously.
    """
    routed_data: dict[str, dict[str, list]] = {st: {} for st in ACTIVE_SETUP_TYPES}
    validated_data: dict[str, dict[str, list]] = {st: {} for st in ACTIVE_SETUP_TYPES}
    routed_n: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}
    validated_n: dict[str, int] = {st: 0 for st in ACTIVE_SETUP_TYPES}

    for r in s4:
        st = r.get("setup_type") or ""
        if st not in routed_data or (setup_type_filter and st != setup_type_filter):
            continue
        routed_n[st] += 1
        # routed population — score exists for every step4 row (pass or fail)
        _collect(routed_data[st], "setup_score", r.get("setup_score"))
        if r["setup_passed"]:
            validated_n[st] += 1
            d = validated_data[st]
            _collect(d, "rvol", r.get("rvol"))
            _collect(d, "atr_pct", r.get("atr_pct"))
            _collect(d, "ema20_distance_pct", r.get("distance_to_ema20_pct"))
            _collect(d, "ema50_distance_pct", r.get("distance_to_ema50_pct"))
            _collect(d, "estimated_rr", r.get("estimated_rr"))
            _collect(d, "stop_distance_pct", r.get("stop_distance_pct"))
            expl = r.get("explanation_json") or {}
            for field in ("range_width_pct", "price_position_in_range"):
                _collect(d, field, expl.get(field))
            _collect(d, "days_in_range",
                     expl.get("days_in_range") or expl.get("range_duration"))
            if st == "consolidation_base":
                _collect(d, "support_found", 1 if expl.get("support_raw") is not None else 0)
                _collect(d, "resistance_found", 1 if expl.get("resistance_raw") is not None else 0)
            if st == "breakout":
                # P1.3 (CODER_NOTE P1 batch, 2026-07-08): instrumentation only —
                # anticipatory ([-1.0,-0.05)) vs confirmed ([-0.05,0.5]) breakout
                # semantics decision stays open pending more data; this field was
                # previously absent from evidence_summaries entirely.
                _collect(d, "breakout_proximity", expl.get("breakout_proximity"))

    for r in s5:
        st = r.get("setup_type") or ""
        if st not in validated_data or (setup_type_filter and st != setup_type_filter):
            continue
        d = validated_data[st]
        _collect(d, "estimated_rr_s5", r.get("estimated_rr"))
        _collect(d, "stop_distance_pct_s5", r.get("stop_distance_pct"))

    result: dict[str, dict] = {}
    for st in ACTIVE_SETUP_TYPES:
        if setup_type_filter and st != setup_type_filter:
            continue
        result[st] = {
            "routed_n": routed_n[st],
            "validated_n": validated_n[st],
            "routed": {k: _stats_of(v) for k, v in routed_data[st].items()},
            "validated": {k: _stats_of(v) for k, v in validated_data[st].items()},
        }
    return result


def _rpt_borderline(
    s4: list[dict],
    top_n: int,
    setup_type_filter: str | None,
) -> dict[str, list[dict]]:
    by_setup: dict[str, list] = {}
    for r in s4:
        st = r.get("setup_type") or ""
        if st not in ACTIVE_SETUP_TYPES:
            continue
        if setup_type_filter and st != setup_type_filter:
            continue
        if not r["setup_passed"]:
            by_setup.setdefault(st, []).append(r)

    result: dict[str, list[dict]] = {}
    for st, rows in by_setup.items():
        rows.sort(key=lambda x: -(float(x.get("setup_score") or 0)))
        top = []
        for r in rows[:top_n]:
            key, example = _normalize_validation_reason(r.get("setup_fail_reason"))
            actual, threshold, direction = _parse_example(example)
            top.append({
                "ticker": r["ticker"],
                "setup_score": round(float(r.get("setup_score") or 0), 4),
                "failed_rule": key,
                "actual_value": actual,
                "threshold": threshold,
                "direction": direction,
            })
        result[st] = top
    return result


def _borderline_proximity(row: dict) -> float | None:
    """Direction-aware normalised distance from a borderline row to its threshold.

    Item B (CODER_NOTE 2026-07-13). Smaller = nearer the threshold.

    - "below min" rules (direction ``'<'``): ``(threshold - actual) / threshold``
    - "above max" rules (direction ``'>'``): ``(actual - threshold) / threshold``

    Returns ``None`` when the comparison is unparseable or the threshold is 0.
    """
    actual = _flt(row.get("actual_value"))
    threshold = _flt(row.get("threshold"))
    direction = row.get("direction")
    if actual is None or threshold is None or threshold == 0:
        return None
    if direction == "<":
        return (threshold - actual) / threshold
    if direction == ">":
        return (actual - threshold) / threshold
    return None


def _sort_borderline_by_proximity(rows: list[dict]) -> list[dict]:
    """Return ``rows`` sorted ascending by :func:`_borderline_proximity`.

    Rows whose distance is unparseable (``None``) sort last, preserving their
    original relative order (stable sort).
    """
    def _key(r: dict) -> tuple[bool, float]:
        d = _borderline_proximity(r)
        return (d is None, d if d is not None else 0.0)

    return sorted(rows, key=_key)


def _rpt_layers(
    s3: list[dict],
    s4: list[dict],
    s5: list[dict],
) -> dict[str, int]:
    ineligible = sum(1 for r in s3 if not r.get("passed_eligibility"))
    not_routed = sum(
        1 for r in s3
        if r["routing_status"] == "no_route" and r.get("passed_eligibility")
    )
    routed_tickers = {r["ticker"] for r in s3 if r["routing_status"] == "routed"}
    s4_any_pass    = {r["ticker"] for r in s4 if r["setup_passed"]}
    s4_all_fail    = routed_tickers - s4_any_pass
    s5_tickers     = {r["ticker"] for r in s5}
    s5_selected    = {r["ticker"] for r in s5 if r["selected_flag"]}
    s5_watchlist   = {r["ticker"] for r in s5 if r["disposition"] == "WATCHLIST_ONLY"}
    s5_rejected_n  = sum(1 for r in s5 if r["disposition"] == "REJECTED")
    div_rejected   = {
        r["ticker"] for r in s5
        if r.get("rejection_reason") in ("sector_cap", "industry_cap")
    }
    buy_not_sel = {
        r["ticker"] for r in s5
        if r["disposition"] == "BUY" and not r["selected_flag"]
    }
    validator_pass_no_s5 = s4_any_pass - s5_tickers

    return {
        "ineligible": ineligible,
        "not_routed": not_routed,
        "routed_all_validators_failed": len(s4_all_fail),
        "validator_passed_no_step5": len(validator_pass_no_s5),
        "step5_rejected": s5_rejected_n,
        "step5_watchlist_not_selected": len(s5_watchlist - s5_selected),
        "step5_buy_diversity_rejected": len(buy_not_sel & div_rejected),
        "selected": len(s5_selected),
    }


def _rpt_ftd(s5: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in s5:
        mech = r.get("mechanical_explanation") or {}
        ftd = mech.get("final_trade_decision") if isinstance(mech, dict) else None
        if not ftd:
            # Rows written before P0 deployment lack the key — infer from disposition
            disp = r.get("disposition") or ""
            ftd = ("REJECTED" if disp == "REJECTED"
                   else "WATCHLIST_ONLY" if disp == "WATCHLIST_ONLY"
                   else "unknown")
        counts[str(ftd)] = counts.get(str(ftd), 0) + 1
    return counts


_DOMINANCE_THRESHOLD: Final[float] = 0.60   # share of selected = dominance warning
_CB_PASS_RATE_WARN: Final[float] = 0.05     # consolidation_base pass rate below this = warning

# Item B (CODER_NOTE 2026-07-13): step5 diversity-cap rejections are applied
# *after* ranking as a diversity trim, not as a validation gate. Relabel them in
# the human-readable report so they are not misread as gate failures. Both caps
# (see step5_proposal_engine REJECT_SECTOR_CAP / REJECT_INDUSTRY_CAP) are the
# same class of post-ranking trim. The raw DB values are unchanged; this is a
# display-surface mapping only.
_REJECTION_DISPLAY_LABELS: Final[dict[str, str]] = {
    "industry_cap": "diversity_trim_industry_cap",
    "sector_cap": "diversity_trim_sector_cap",
}


def _display_rejection_label(key: str) -> str:
    """Map a raw rejection key to its human-readable report label (identity by default)."""
    return _REJECTION_DISPLAY_LABELS.get(key, key)


def _rpt_s5_rejection_reasons(s5: list[dict]) -> list[dict]:
    """Aggregate step5 rejection_reason by normalised key across all dispositions."""
    total_with_reason = 0
    counts: dict[str, int] = {}
    examples: dict[str, list[tuple[str | None, str | None, str | None, str]]] = {}

    for r in s5:
        raw = r.get("rejection_reason")
        if not raw:
            continue
        total_with_reason += 1
        key, example = _normalize_validation_reason(raw)
        counts[key] = counts.get(key, 0) + 1
        ex_list = examples.setdefault(key, [])
        if len(ex_list) < 3:
            actual, threshold, direction = _parse_example(example)
            ex_list.append((actual, threshold, direction, r.get("ticker") or "?"))

    result = []
    for key, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        ex = examples.get(key, [])
        result.append({
            "reason": _display_rejection_label(key),
            "count": cnt,
            "pct_of_total": _pct_f(cnt, total_with_reason),
            "actual_value": ex[0][0] if ex else None,
            "threshold": ex[0][1] if ex else None,
            "direction": ex[0][2] if ex else None,
            "sample_tickers": [e[3] for e in ex],
        })
    return result


def _rpt_warnings(
    s3: list[dict],
    s4: list[dict],
    s5: list[dict],
    setup_type_filter: str | None,  # noqa: ARG001 — reserved for future use
) -> list[dict]:
    """Produce structured diagnostic warnings from pipeline data."""
    warnings: list[dict] = []

    # 1. Setup dominance in final selection
    total_sel = sum(1 for r in s5 if r.get("selected_flag"))
    sel_by_setup: dict[str, int] = {}
    for r in s5:
        if r.get("selected_flag"):
            st = r.get("setup_type") or "unknown"
            sel_by_setup[st] = sel_by_setup.get(st, 0) + 1
    if total_sel > 0:
        for st, cnt in sorted(sel_by_setup.items(), key=lambda x: -x[1]):
            share = cnt / total_sel
            if share >= _DOMINANCE_THRESHOLD:
                warnings.append({
                    "code": f"setup_dominance.{st}_selected_share_high",
                    "severity": "warn",
                    "message": (
                        f"{st} selected {cnt}/{total_sel} = "
                        f"{share * 100:.0f}% of final list"
                    ),
                    "detail": {
                        "setup_type": st,
                        "selected_count": cnt,
                        "total_selected": total_sel,
                        "share": round(share, 4),
                    },
                })

    # 2. Consolidation health: pass rate < 5%
    cb_routed = sum(
        1 for r in s3
        if "consolidation_base" in (r.get("routed_setup_types") or [])
    )
    cb_s4 = [r for r in s4 if r.get("setup_type") == "consolidation_base"]
    cb_pass = sum(1 for r in cb_s4 if r.get("setup_passed"))
    cb_total = len(cb_s4)
    if cb_routed > 0 and cb_total > 0:
        cb_rate = cb_pass / cb_total
        if cb_rate < _CB_PASS_RATE_WARN:
            warnings.append({
                "code": "consolidation_validator_too_strict_or_misconfigured",
                "severity": "warn",
                "message": (
                    f"consolidation_base: routed={cb_routed}, "
                    f"passed={cb_pass}/{cb_total}, "
                    f"pass_rate={cb_rate * 100:.1f}%"
                ),
                "detail": {
                    "routed": cb_routed,
                    "passed": cb_pass,
                    "total_validated": cb_total,
                    "pass_rate": round(cb_rate, 4),
                },
            })

    # 3. Consolidation evidence integrity: passed but no support_raw or resistance_raw
    cb_passed_rows = [r for r in cb_s4 if r.get("setup_passed")]
    no_sr_count = 0
    for r in cb_passed_rows:
        expl = r.get("explanation_json") or {}
        if expl.get("support_raw") is None and expl.get("resistance_raw") is None:
            no_sr_count += 1
    if no_sr_count > 0:
        warnings.append({
            "code": "consolidation_passed_without_support_resistance_evidence",
            "severity": "warn",
            "message": (
                f"{no_sr_count}/{len(cb_passed_rows)} passed consolidation_base "
                "candidates have no support_raw or resistance_raw in evidence"
            ),
            "detail": {
                "affected_count": no_sr_count,
                "total_passed": len(cb_passed_rows),
            },
        })

    return warnings


def _normalize_validation_reason(raw: str | None) -> tuple[str, str | None]:
    """Split a raw step4 ``setup_fail_reason`` into a stable category key and
    an optional numeric example string.

    Step4 records reasons like::

        "rvol_below_hard_threshold(0.91<1.5)"
        "range_too_wide(53.0<60.0)"
        "stop_too_wide(0.12>0.10)"

    The ``(…)`` suffix contains the actual vs threshold values and must NOT
    appear in ``metric_name`` (which must be a stable category key).

    Returns:
        ``(reason_key, example_or_none)``

        - ``reason_key`` — the stable category name, e.g.
          ``"rvol_below_hard_threshold"``.  Used in ``metric_name`` and
          ``reason`` fields.
        - ``example_or_none`` — the raw numeric string inside the parentheses,
          e.g. ``"0.91<1.5"``, stored in ``metadata_json["examples"]``.
          ``None`` when no parentheses are present.
    """
    if not raw:
        return ("unknown", None)
    m = re.match(r"^([^(]+?)\s*(?:\((.+)\))?$", raw.strip())
    if m:
        key = m.group(1).strip()
        example = m.group(2)  # None when no parentheses
        return (key, example)
    return (raw.strip(), None)

# ---------------------------------------------------------------------------
# Legacy alias
# ---------------------------------------------------------------------------
FunnelDiagnosticsService = SetupModeFunnelDiagnosticsService
