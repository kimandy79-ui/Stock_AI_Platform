"""Funnel Diagnostics Service.

Read-only analysis of the Step 3 → Step 4 → Step 5 candidate funnel.
Purpose: explain why Normal/Conservative strategies produce zero selected stocks
by showing pass/fail counts at each gate.

No INSERT / UPDATE / DELETE / CREATE is performed. All reads use read-only
DuckDB connections obtained via the injected db_manager.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.utils.service_result import ServiceResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid roles and constants
# ---------------------------------------------------------------------------
_VALID_DB_ROLES: frozenset[str] = frozenset({"prod", "debug"})

_STEP3_HARD_FILTER_LABELS: list[str] = [
    "feature_not_ready",
    "not_stock",
    "price_below_min",
    "avg_dollar_volume_below_min",
    "rvol_below_min",
    "data_quality_not_ok",
]


# ---------------------------------------------------------------------------
# Internal data models
# ---------------------------------------------------------------------------
@dataclass
class FunnelStage:
    stage_order: int
    stage_key: str
    label: str
    input_count: int | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    pass_rate: float | None = None
    threshold: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_order": self.stage_order,
            "stage_key": self.stage_key,
            "label": self.label,
            "input_count": self.input_count,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "pass_rate": self.pass_rate,
            "threshold": self.threshold,
        }


@dataclass
class StrategyFunnelResult:
    signal_date: str
    db_role: str
    strategy_config_id: str
    strategy_name: str
    stages: list[FunnelStage] = field(default_factory=list)
    step4_observed: dict[str, Any] = field(default_factory=dict)
    step4_projected: dict[str, Any] = field(default_factory=dict)
    step5_counts: dict[str, Any] = field(default_factory=dict)
    bottlenecks: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_date": self.signal_date,
            "db_role": self.db_role,
            "strategy_config_id": self.strategy_config_id,
            "strategy_name": self.strategy_name,
            "stages": [s.to_dict() for s in self.stages],
            "step4_observed": self.step4_observed,
            "step4_projected": self.step4_projected,
            "step5_counts": self.step5_counts,
            "bottlenecks": self.bottlenecks,
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class FunnelDiagnosticsService:
    """Read-only funnel diagnostics for strategy candidate yield analysis.

    Usage::

        svc = FunnelDiagnosticsService(db_manager=mgr)
        result = svc.run(signal_date=date(2026, 6, 15), db_role="prod")
    """

    def __init__(self, db_manager: Any) -> None:
        self._db = db_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(
        self,
        signal_date: date,
        db_role: str = "prod",
        strategy_config_id: str | None = None,
        run_id: str | None = None,
        include_projected_step4: bool = True,
    ) -> ServiceResult:
        """Run funnel diagnostics for the given signal_date.

        Parameters
        ----------
        signal_date:
            The date to analyse.
        db_role:
            'prod' or 'debug'.
        strategy_config_id:
            If None, all active strategies are analysed. Otherwise only the
            specified config is analysed.
        run_id:
            Optional run_id to narrow results to a specific pipeline run.
            Because step-major execution may assign different run_ids to
            different steps, this is applied as an additional filter, not the
            primary join key. Primary join is always (strategy_config_id,
            signal_date).
        include_projected_step4:
            If True, attempt a read-only projection of Step 4 gate pass/fail
            counts for candidates that were never written to step4_analysis.
        """
        diag_run_id = run_id or str(uuid.uuid4())

        # --- guard: db_role ---
        if db_role not in _VALID_DB_ROLES:
            return ServiceResult(
                status="failed",
                run_id=diag_run_id,
                errors=[f"Invalid db_role {db_role!r}. Must be one of {sorted(_VALID_DB_ROLES)}."],
                metadata={
                    "db_role": db_role,
                    "signal_date": signal_date.isoformat(),
                    "strategy_config_id": strategy_config_id,
                    "run_id": run_id,
                    "strategies_analysed": [],
                },
            )

        logger.info(
            "FunnelDiagnostics starting | signal_date=%s db_role=%s strategy=%s run_id=%s",
            signal_date,
            db_role,
            strategy_config_id or "all",
            diag_run_id,
        )

        try:
            # Load active strategy configs
            configs = self._load_strategy_configs(db_role, strategy_config_id)
            if not configs:
                return ServiceResult(
                    status="success",
                    run_id=diag_run_id,
                    rows_processed=0,
                    warnings=["No active strategy configs found."],
                    metadata={
                        "db_role": db_role,
                        "signal_date": signal_date.isoformat(),
                        "strategy_config_id": strategy_config_id,
                        "run_id": run_id,
                        "strategies_analysed": [],
                        "funnel_results": [],
                    },
                )

            funnel_results: list[dict[str, Any]] = []
            warnings: list[str] = []

            for cfg_id, cfg_name, cfg_json in configs:
                try:
                    result = self._analyse_strategy(
                        signal_date=signal_date,
                        db_role=db_role,
                        config_id=cfg_id,
                        config_name=cfg_name,
                        config_json=cfg_json,
                        pipeline_run_id=run_id,
                        include_projected_step4=include_projected_step4,
                    )
                    funnel_results.append(result.to_dict())
                except Exception as exc:  # noqa: BLE001
                    msg = f"Strategy {cfg_id!r} ({cfg_name}) failed: {exc}"
                    logger.warning(msg)
                    warnings.append(msg)

            status = "success" if not warnings else "success_with_warnings"
            return ServiceResult(
                status=status,
                run_id=diag_run_id,
                rows_processed=len(funnel_results),
                warnings=warnings,
                metadata={
                    "db_role": db_role,
                    "signal_date": signal_date.isoformat(),
                    "strategy_config_id": strategy_config_id,
                    "run_id": run_id,
                    "strategies_analysed": [r["strategy_config_id"] for r in funnel_results],
                    "funnel_results": funnel_results,
                },
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("FunnelDiagnostics fatal error: %s", exc)
            return ServiceResult(
                status="failed",
                run_id=diag_run_id,
                errors=[str(exc)],
                metadata={
                    "db_role": db_role,
                    "signal_date": signal_date.isoformat(),
                    "strategy_config_id": strategy_config_id,
                    "run_id": run_id,
                    "strategies_analysed": [],
                },
            )

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------
    def _load_strategy_configs(
        self,
        db_role: str,
        strategy_config_id: str | None,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Return list of (config_id, strategy_name, config_dict) for active configs."""
        conn = self._db.connect(db_role, read_only=True)
        try:
            if strategy_config_id is not None:
                rows = conn.execute(
                    "SELECT config_id, strategy_name, config_json FROM strategy_configs "
                    "WHERE config_id = ? AND active_flag = TRUE",
                    [strategy_config_id],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT config_id, strategy_name, config_json FROM strategy_configs "
                    "WHERE active_flag = TRUE ORDER BY strategy_name"
                ).fetchall()
        finally:
            conn.close()

        result = []
        for row in rows:
            cfg_id, cfg_name, cfg_raw = row
            if isinstance(cfg_raw, str):
                cfg_dict = json.loads(cfg_raw)
            else:
                cfg_dict = cfg_raw if cfg_raw is not None else {}
            result.append((cfg_id, cfg_name, cfg_dict))
        return result

    # ------------------------------------------------------------------
    # Per-strategy analysis
    # ------------------------------------------------------------------
    def _analyse_strategy(
        self,
        signal_date: date,
        db_role: str,
        config_id: str,
        config_name: str,
        config_json: dict[str, Any],
        pipeline_run_id: str | None,
        include_projected_step4: bool,
    ) -> StrategyFunnelResult:
        result = StrategyFunnelResult(
            signal_date=signal_date.isoformat(),
            db_role=db_role,
            strategy_config_id=config_id,
            strategy_name=config_name,
        )

        # Thresholds from config
        min_screening_score: float = (
            config_json.get("screening", {}).get("min_screening_score", 0.0) or 0.0
        )
        min_step3_setup_score: float = (
            config_json.get("screening", {}).get("min_step3_setup_score", 0.0) or 0.0
        )

        # ---------- Step 3 ----------
        s3_rows = self._load_step3_rows(db_role, config_id, signal_date, pipeline_run_id)
        stages = self._build_step3_stages(s3_rows, min_screening_score, min_step3_setup_score)
        result.stages = stages

        # ---------- Step 4 observed ----------
        s4_rows = self._load_step4_rows(db_role, config_id, signal_date, pipeline_run_id)
        result.step4_observed = self._build_step4_observed(s4_rows)

        # ---------- Step 4 projected ----------
        if include_projected_step4:
            result.step4_projected = self._build_step4_projected(
                s3_rows, config_json, min_screening_score, min_step3_setup_score
            )
        else:
            result.step4_projected = {"projected_step4_available": False, "reason": "skipped by caller"}

        # ---------- Step 5 ----------
        s5_rows = self._load_step5_rows(db_role, config_id, signal_date, pipeline_run_id)
        result.step5_counts = self._build_step5_counts(s5_rows)

        # ---------- Bottlenecks ----------
        result.bottlenecks = self._detect_bottlenecks(result.stages, result.step4_observed, result.step5_counts)

        return result

    # ------------------------------------------------------------------
    # Step 3 data loading
    # ------------------------------------------------------------------
    def _load_step3_rows(
        self,
        db_role: str,
        config_id: str,
        signal_date: date,
        run_id: str | None,
    ) -> list[dict[str, Any]]:
        """Load all step3_candidates rows for (config_id, signal_date)."""
        conn = self._db.connect(db_role, read_only=True)
        try:
            sql = (
                "SELECT candidate_id, passed_hard_filters, hard_filter_fail_reasons, "
                "       screening_score, soft_score_components "
                "FROM step3_candidates "
                "WHERE strategy_config_id = ? AND signal_date = ?"
            )
            params: list[Any] = [config_id, signal_date.isoformat()]
            if run_id:
                sql += " AND run_id = ?"
                params.append(run_id)
            rows = conn.execute(sql, params).fetchall()
            cols = ["candidate_id", "passed_hard_filters", "hard_filter_fail_reasons",
                    "screening_score", "soft_score_components"]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def _build_step3_stages(
        self,
        rows: list[dict[str, Any]],
        min_screening_score: float,
        min_step3_setup_score: float,
    ) -> list[FunnelStage]:
        stages: list[FunnelStage] = []
        total = len(rows)
        order = 1

        # Stage 1: total evaluated
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="step3_evaluated",
            label="Step 3 evaluated",
            input_count=None,
            pass_count=total,
            fail_count=None,
            pass_rate=None,
            threshold=None,
        ))
        order += 1

        if total == 0:
            return stages

        # Parse JSON fields
        parsed: list[dict[str, Any]] = []
        for r in rows:
            fail_reasons: list[str] = []
            raw_fail = r.get("hard_filter_fail_reasons")
            if raw_fail:
                if isinstance(raw_fail, str):
                    try:
                        fail_reasons = json.loads(raw_fail) or []
                    except (json.JSONDecodeError, TypeError):
                        fail_reasons = []
                elif isinstance(raw_fail, list):
                    fail_reasons = raw_fail

            soft_components: dict[str, Any] = {}
            raw_soft = r.get("soft_score_components")
            if raw_soft:
                if isinstance(raw_soft, str):
                    try:
                        soft_components = json.loads(raw_soft) or {}
                    except (json.JSONDecodeError, TypeError):
                        soft_components = {}
                elif isinstance(raw_soft, dict):
                    soft_components = raw_soft

            parsed.append({
                "passed_hard_filters": bool(r.get("passed_hard_filters")),
                "fail_reasons": fail_reasons,
                "screening_score": r.get("screening_score"),
                "setup_score": soft_components.get("setup_score"),
            })

        # Stage 2: feature_ready pass
        feature_ready_pass = sum(1 for p in parsed if "feature_not_ready" not in p["fail_reasons"])
        feature_ready_fail = total - feature_ready_pass
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="feature_ready_pass",
            label="Feature ready",
            input_count=total,
            pass_count=feature_ready_pass,
            fail_count=feature_ready_fail,
            pass_rate=round(feature_ready_pass / total, 4) if total else None,
        ))
        order += 1

        # Stage 3: symbol_type stock pass
        stock_pass = sum(1 for p in parsed if "not_stock" not in p["fail_reasons"])
        stock_fail = total - stock_pass
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="symbol_type_stock_pass",
            label="Symbol type: stock",
            input_count=total,
            pass_count=stock_pass,
            fail_count=stock_fail,
            pass_rate=round(stock_pass / total, 4) if total else None,
        ))
        order += 1

        # Stage 4: price gate
        price_pass = sum(1 for p in parsed if "price_below_min" not in p["fail_reasons"])
        price_fail = total - price_pass
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="price_gate_pass",
            label="Price >= min_price",
            input_count=total,
            pass_count=price_pass,
            fail_count=price_fail,
            pass_rate=round(price_pass / total, 4) if total else None,
        ))
        order += 1

        # Stage 5: avg dollar volume gate
        adv_pass = sum(1 for p in parsed if "avg_dollar_volume_below_min" not in p["fail_reasons"])
        adv_fail = total - adv_pass
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="avg_dollar_volume_gate_pass",
            label="Avg dollar volume >= min",
            input_count=total,
            pass_count=adv_pass,
            fail_count=adv_fail,
            pass_rate=round(adv_pass / total, 4) if total else None,
        ))
        order += 1

        # Stage 6: rvol gate
        rvol_pass = sum(1 for p in parsed if "rvol_below_min" not in p["fail_reasons"])
        rvol_fail = total - rvol_pass
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="rvol_gate_pass",
            label="RVOL >= min_rvol",
            input_count=total,
            pass_count=rvol_pass,
            fail_count=rvol_fail,
            pass_rate=round(rvol_pass / total, 4) if total else None,
        ))
        order += 1

        # Stage 7: data quality gate
        dq_pass = sum(1 for p in parsed if "data_quality_not_ok" not in p["fail_reasons"])
        dq_fail = total - dq_pass
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="data_quality_pass",
            label="Data quality OK",
            input_count=total,
            pass_count=dq_pass,
            fail_count=dq_fail,
            pass_rate=round(dq_pass / total, 4) if total else None,
        ))
        order += 1

        # Stage 8: all hard filters passed
        hf_pass = sum(1 for p in parsed if p["passed_hard_filters"])
        hf_fail = total - hf_pass
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="step3_hard_filters_pass",
            label="Step 3 hard filters: all pass",
            input_count=total,
            pass_count=hf_pass,
            fail_count=hf_fail,
            pass_rate=round(hf_pass / total, 4) if total else None,
        ))
        order += 1

        # Stage 9: hard filters fail (informational inverse)
        stages.append(FunnelStage(
            stage_order=order,
            stage_key="step3_hard_filters_fail",
            label="Step 3 hard filters: any fail",
            input_count=total,
            pass_count=hf_fail,
            fail_count=hf_pass,
            pass_rate=round(hf_fail / total, 4) if total else None,
        ))
        order += 1

        # --- Score gates (only on hard-filter-passed candidates) ---
        passed_rows = [p for p in parsed if p["passed_hard_filters"]]
        n_passed = len(passed_rows)

        # Stage 10: min_screening_score gate
        if min_screening_score > 0 and n_passed > 0:
            ss_pass = sum(
                1 for p in passed_rows
                if p["screening_score"] is not None and p["screening_score"] >= min_screening_score
            )
            ss_fail = n_passed - ss_pass
            stages.append(FunnelStage(
                stage_order=order,
                stage_key="min_screening_score_pass",
                label=f"screening_score >= {min_screening_score}",
                input_count=n_passed,
                pass_count=ss_pass,
                fail_count=ss_fail,
                pass_rate=round(ss_pass / n_passed, 4) if n_passed else None,
                threshold=min_screening_score,
            ))
            order += 1

        # Stage 11: min_step3_setup_score gate
        if min_step3_setup_score > 0 and n_passed > 0:
            sus_pass = sum(
                1 for p in passed_rows
                if p["setup_score"] is not None and p["setup_score"] >= min_step3_setup_score
            )
            sus_fail = n_passed - sus_pass
            stages.append(FunnelStage(
                stage_order=order,
                stage_key="min_step3_setup_score_pass",
                label=f"setup_score (step3) >= {min_step3_setup_score}",
                input_count=n_passed,
                pass_count=sus_pass,
                fail_count=sus_fail,
                pass_rate=round(sus_pass / n_passed, 4) if n_passed else None,
                threshold=min_step3_setup_score,
            ))
            order += 1

        # Summary of per-reason counts (appended as a special stage with threshold=reason_counts)
        reason_counts: dict[str, int] = {label: 0 for label in _STEP3_HARD_FILTER_LABELS}
        for p in parsed:
            for reason in p["fail_reasons"]:
                if reason in reason_counts:
                    reason_counts[reason] += 1
                else:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1

        stages.append(FunnelStage(
            stage_order=order,
            stage_key="step3_fail_reason_counts",
            label="Step 3 hard filter: fail reason breakdown",
            input_count=total,
            pass_count=None,
            fail_count=hf_fail,
            pass_rate=None,
            threshold=reason_counts,
        ))

        return stages

    # ------------------------------------------------------------------
    # Step 4 data loading
    # ------------------------------------------------------------------
    def _load_step4_rows(
        self,
        db_role: str,
        config_id: str,
        signal_date: date,
        run_id: str | None,
    ) -> list[dict[str, Any]]:
        conn = self._db.connect(db_role, read_only=True)
        try:
            sql = (
                "SELECT analysis_id, setup_type, setup_score, estimated_rr, "
                "       explanation_json, earnings_penalty, macro_penalty "
                "FROM step4_analysis "
                "WHERE strategy_config_id = ? AND signal_date = ?"
            )
            params: list[Any] = [config_id, signal_date.isoformat()]
            if run_id:
                sql += " AND run_id = ?"
                params.append(run_id)
            rows = conn.execute(sql, params).fetchall()
            cols = ["analysis_id", "setup_type", "setup_score", "estimated_rr",
                    "explanation_json", "earnings_penalty", "macro_penalty"]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def _build_step4_observed(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        if n == 0:
            return {
                "step4_analysis_rows": 0,
                "setup_type_counts": {},
                "setup_score_min": None,
                "setup_score_mean": None,
                "setup_score_max": None,
                "estimated_rr_min": None,
                "estimated_rr_mean": None,
                "estimated_rr_max": None,
                "earnings_penalty_nonzero_count": 0,
                "macro_penalty_nonzero_count": 0,
            }

        setup_type_counts: dict[str, int] = {}
        setup_scores: list[float] = []
        rr_values: list[float] = []
        earnings_nonzero = 0
        macro_nonzero = 0

        for r in rows:
            st = r.get("setup_type") or "unknown"
            setup_type_counts[st] = setup_type_counts.get(st, 0) + 1
            if r.get("setup_score") is not None:
                setup_scores.append(float(r["setup_score"]))
            if r.get("estimated_rr") is not None:
                rr_values.append(float(r["estimated_rr"]))
            ep = r.get("earnings_penalty")
            if ep is not None and float(ep) != 0.0:
                earnings_nonzero += 1
            mp = r.get("macro_penalty")
            if mp is not None and float(mp) != 0.0:
                macro_nonzero += 1

        def _safe_mean(lst: list[float]) -> float | None:
            return round(sum(lst) / len(lst), 4) if lst else None

        return {
            "step4_analysis_rows": n,
            "setup_type_counts": setup_type_counts,
            "setup_score_min": round(min(setup_scores), 4) if setup_scores else None,
            "setup_score_mean": _safe_mean(setup_scores),
            "setup_score_max": round(max(setup_scores), 4) if setup_scores else None,
            "estimated_rr_min": round(min(rr_values), 4) if rr_values else None,
            "estimated_rr_mean": _safe_mean(rr_values),
            "estimated_rr_max": round(max(rr_values), 4) if rr_values else None,
            "earnings_penalty_nonzero_count": earnings_nonzero,
            "macro_penalty_nonzero_count": macro_nonzero,
        }

    # ------------------------------------------------------------------
    # Step 4 projected dry-run
    # ------------------------------------------------------------------
    def _build_step4_projected(
        self,
        s3_rows: list[dict[str, Any]],
        config_json: dict[str, Any],
        min_screening_score: float,
        min_step3_setup_score: float,
    ) -> dict[str, Any]:
        """Project Step 4 gate pass/fail counts from Step 3 candidate data.

        Gates that can be computed from step3_candidates.feature_snapshot_json
        and soft_score_components are applied. Gates requiring full Step 4
        scoring (min_step4_setup_score, min_estimated_rr) are explicitly noted
        as unavailable.
        """
        # Only hard-filter-passed candidates enter Step 4
        passed: list[dict[str, Any]] = []
        for r in s3_rows:
            if not r.get("passed_hard_filters"):
                continue

            soft_raw = r.get("soft_score_components")
            if isinstance(soft_raw, str):
                try:
                    soft = json.loads(soft_raw) or {}
                except (json.JSONDecodeError, TypeError):
                    soft = {}
            elif isinstance(soft_raw, dict):
                soft = soft_raw
            else:
                soft = {}

            passed.append({
                "screening_score": r.get("screening_score"),
                "setup_score": soft.get("setup_score"),
            })

        n_input = len(passed)
        if n_input == 0:
            return {
                "projected_step4_available": True,
                "input_candidates": 0,
                "gates": [],
                "notes": ["No candidates passed Step 3 hard filters; projection not applicable."],
            }

        gates: list[dict[str, Any]] = []
        current_pool = list(passed)
        gate_order = 1

        # Gate 1: min_screening_score (applied in Step 4 as a repeat guard)
        if min_screening_score > 0:
            after_ss = [
                c for c in current_pool
                if c["screening_score"] is not None and c["screening_score"] >= min_screening_score
            ]
            gates.append({
                "gate_order": gate_order,
                "gate_key": "min_screening_score",
                "label": f"screening_score >= {min_screening_score}",
                "input_count": len(current_pool),
                "pass_count": len(after_ss),
                "fail_count": len(current_pool) - len(after_ss),
                "threshold": min_screening_score,
                "source": "step3_candidates.screening_score",
            })
            current_pool = after_ss
            gate_order += 1

        # Gate 2: min_step3_setup_score
        if min_step3_setup_score > 0:
            after_sus = [
                c for c in current_pool
                if c["setup_score"] is not None and c["setup_score"] >= min_step3_setup_score
            ]
            gates.append({
                "gate_order": gate_order,
                "gate_key": "min_step3_setup_score",
                "label": f"setup_score >= {min_step3_setup_score}",
                "input_count": len(current_pool),
                "pass_count": len(after_sus),
                "fail_count": len(current_pool) - len(after_sus),
                "threshold": min_step3_setup_score,
                "source": "step3_candidates.soft_score_components.setup_score",
            })
            current_pool = after_sus
            gate_order += 1

        # Remaining gates: not reliably projectable without Step 4 re-execution
        notes = [
            "Gates 'min_step4_setup_score' and 'min_estimated_rr' require Step 4 scoring "
            "re-execution and are not projected here. step3_candidates.feature_snapshot_json "
            "does not contain the full input set (e.g. recent_20d_low_raw, close_raw for stop "
            "calculation) needed to reconstruct setup_score and estimated_rr for candidates "
            "that never reached step4_analysis.",
            "Gates 'atr_pct', 'ema50_extension', 'stop_distance', 'setup_type_allowed', "
            "'earnings_status' are also not projected: they require re-running Step 4 "
            "classification logic against feature_snapshot_json fields that may be incomplete.",
        ]

        return {
            "projected_step4_available": True,
            "input_candidates": n_input,
            "candidates_after_projection": len(current_pool),
            "gates": gates,
            "gates_not_projected": [
                "atr_pct_gate",
                "ema50_extension_gate",
                "stop_distance_gate",
                "setup_type_allowed_gate",
                "earnings_status_gate",
                "min_step4_setup_score_gate",
                "min_estimated_rr_gate",
            ],
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # Step 5 data loading
    # ------------------------------------------------------------------
    def _load_step5_rows(
        self,
        db_role: str,
        config_id: str,
        signal_date: date,
        run_id: str | None,
    ) -> list[dict[str, Any]]:
        conn = self._db.connect(db_role, read_only=True)
        try:
            sql = (
                "SELECT proposal_id, selected_flag, in_raw_top_n, in_diversified_top_n, "
                "       raw_rank, diversified_rank, rejection_reason, mechanical_explanation "
                "FROM step5_proposals "
                "WHERE strategy_config_id = ? AND signal_date = ?"
            )
            params: list[Any] = [config_id, signal_date.isoformat()]
            if run_id:
                sql += " AND run_id = ?"
                params.append(run_id)
            rows = conn.execute(sql, params).fetchall()
            cols = ["proposal_id", "selected_flag", "in_raw_top_n", "in_diversified_top_n",
                    "raw_rank", "diversified_rank", "rejection_reason", "mechanical_explanation"]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def _build_step5_counts(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rows)
        if n == 0:
            return {
                "step5_proposals_written": 0,
                "selected_flag_true": 0,
                "raw_rank_count": 0,
                "diversified_rank_count": 0,
                "diversification_rejections": 0,
                "rejection_reason_breakdown": {},
                "disposition_breakdown": {},
            }

        selected_true = sum(1 for r in rows if r.get("selected_flag"))
        raw_top_n = sum(1 for r in rows if r.get("in_raw_top_n"))
        div_top_n = sum(1 for r in rows if r.get("in_diversified_top_n"))
        div_rejections = sum(1 for r in rows if r.get("rejection_reason") is not None)

        # Rejection reason breakdown
        rejection_counts: dict[str, int] = {}
        for r in rows:
            rr = r.get("rejection_reason")
            if rr:
                rejection_counts[rr] = rejection_counts.get(rr, 0) + 1

        # Parse mechanical_explanation for disposition info
        disposition_counts: dict[str, int] = {}
        for r in rows:
            me_raw = r.get("mechanical_explanation")
            if not me_raw:
                continue
            if isinstance(me_raw, str):
                try:
                    me = json.loads(me_raw)
                except (json.JSONDecodeError, TypeError):
                    continue
            elif isinstance(me_raw, dict):
                me = me_raw
            else:
                continue
            disposition = me.get("final_disposition") or me.get("disposition")
            if disposition:
                disposition_counts[disposition] = disposition_counts.get(disposition, 0) + 1

        return {
            "step5_proposals_written": n,
            "selected_flag_true": selected_true,
            "raw_rank_count": raw_top_n,
            "diversified_rank_count": div_top_n,
            "diversification_rejections": div_rejections,
            "rejection_reason_breakdown": rejection_counts,
            "disposition_breakdown": disposition_counts,
        }

    # ------------------------------------------------------------------
    # Bottleneck detection
    # ------------------------------------------------------------------
    def _detect_bottlenecks(
        self,
        stages: list[FunnelStage],
        step4_observed: dict[str, Any],
        step5_counts: dict[str, Any],
    ) -> dict[str, str | None]:
        """Identify top 3 largest drop-off points by absolute count."""
        # Build a simple linear sequence of (label, pass_count) checkpoints
        checkpoints: list[tuple[str, int]] = []

        for stage in stages:
            key = stage.stage_key
            # Skip informational/duplicate stages
            if key in ("step3_hard_filters_fail", "step3_fail_reason_counts", "step3_evaluated"):
                continue
            if stage.pass_count is not None:
                checkpoints.append((stage.label, stage.pass_count))

        # Add Step 4 and Step 5 checkpoints
        s4_rows = step4_observed.get("step4_analysis_rows", 0) or 0
        checkpoints.append(("Step 4 analysis rows", s4_rows))

        s5_written = step5_counts.get("step5_proposals_written", 0) or 0
        checkpoints.append(("Step 5 proposals written", s5_written))

        s5_selected = step5_counts.get("selected_flag_true", 0) or 0
        checkpoints.append(("Step 5 selected", s5_selected))

        # Compute drop between consecutive checkpoints
        drops: list[tuple[int, str]] = []
        for i in range(1, len(checkpoints)):
            prev_label, prev_count = checkpoints[i - 1]
            cur_label, cur_count = checkpoints[i]
            drop = prev_count - cur_count
            if drop > 0:
                drops.append((drop, f"{prev_label} → {cur_label} (drop: {drop})"))

        drops.sort(key=lambda x: x[0], reverse=True)

        return {
            "main": drops[0][1] if len(drops) > 0 else None,
            "second": drops[1][1] if len(drops) > 1 else None,
            "third": drops[2][1] if len(drops) > 2 else None,
        }

    # ------------------------------------------------------------------
    # Read-only row count safety check
    # ------------------------------------------------------------------
    def row_counts(self, db_role: str) -> dict[str, int]:
        """Return current row counts for the six key tables.

        Used by callers to verify no mutation occurred.
        """
        tables = [
            "step3_candidates",
            "step4_analysis",
            "step5_proposals",
            "daily_features",
            "daily_prices",
            "strategy_configs",
        ]
        conn = self._db.connect(db_role, read_only=True)
        try:
            counts: dict[str, int] = {}
            for tbl in tables:
                row = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()  # noqa: S608
                counts[tbl] = int(row[0]) if row else 0
            return counts
        finally:
            conn.close()
