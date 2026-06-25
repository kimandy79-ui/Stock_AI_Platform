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

        conn = self._db.connect(db_role, read_only=True)
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
        conn = self._db.connect(db_role, read_only=True)
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
