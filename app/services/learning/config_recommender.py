"""Module 23 — Config Recommender (learning layer).

Aggregates realized outcomes from prod ``signal_outcomes`` and simulation
``sim_signal_outcomes``, grouped by ``(setup_type, regime, config_id)``, and
proposes config changes for human review when a candidate config beats the
currently-active ("incumbent") config for its setup_type by enough margin,
with enough samples, to be worth a look. Writes ``config_recommendations``
rows with ``status = 'pending'`` — nothing in this module ever activates a
config.

Contract source of truth: ``M23_CONFIG_RECOMMENDER_SPEC.md``.

Design notes
------------
- **Metric reuse.** ``expectancy`` / ``win_rate`` / ``avg_win`` / ``avg_loss``
  / ``profit_factor`` / ``max_drawdown_pct`` / ``resolved_outcomes_pct`` reuse
  :func:`app.services.simulation.simulation_engine.compute_metrics` verbatim
  (the same formulas already used for ``sim_config_comparisons``) — this
  module does not reimplement them. ``target_hit_rate`` / ``stop_hit_rate``
  are a simple mean-of-boolean computed locally (``compute_metrics`` doesn't
  produce them; they're a fraction, not a "formula" worth reusing machinery
  for).
- **Guardrails (the one genuinely new piece of math here).** A cell can have
  more than one candidate config (Phase 1.5 seeds 1-2 presets per setup_type
  plus the incumbent), which is a multiple-comparison setting — an unguarded
  "highest expectancy wins" comparator would systematically promote noise.
  Two guardrails, both required, applied *pairwise* against the incumbent for
  every candidate independently (never "max across all candidates" as a
  single pooled comparison):
    1. **Sample floor** — both the candidate's and the incumbent's cell must
       have >= ``sample_floor`` (default 30) *resolved* realized-R
       observations. This is a flat floor, not a scaled Minimum Track Record
       Length / Minimum Backtest Length (Bailey & Lopez de Prado) treatment —
       that fuller approach was judged out of scope for a first cut per the
       coder note; flagging here in case a future pass wants it.
    2. **Improvement margin** — the candidate's expectancy must exceed the
       incumbent's by at least ``margin_k`` (default 1.0) pooled standard
       errors of the difference in sample means:
           pooled_se = sqrt(var_candidate/n_candidate + var_incumbent/n_incumbent)
           margin_required = margin_k * pooled_se
       This is a simple, explicitly-stated one-sided margin rule — not a
       formal hypothesis test at a declared alpha, and not a full Deflated
       Sharpe Ratio / Harvey-Liu multiple-testing haircut. It exists so a
       candidate must clear "higher AND not just noise-sized higher," which
       is the minimum bar the coder note asked for.
  When more than one candidate qualifies in a cell, the one with the largest
  margin achieved is written as the recommendation; every evaluated
  candidate's stats (qualified or not) are kept in ``evidence_json`` for
  human-reviewer transparency.
- **No auto-activation.** This module never imports
  ``app.services.config.config_service`` and never references
  ``activate_setup_config`` anywhere in its source — reads of
  ``setup_configs`` go through this module's own local SQL, exactly like
  every other Step 3/4/5 service already reads its own configs directly
  rather than through ``ConfigService``. Enforced by a static source scan in
  the test suite.
- **Tri-DB discipline.** Reads prod ``signal_outcomes``/``step5_proposals``,
  reads simulation ``sim_signal_outcomes``/``sim_step5_proposals``, writes
  ``config_recommendations`` to prod only (``ALLOWED_DB_ROLES`` is
  intentionally just ``("prod",)`` for the write path — the coder note says
  "writes recommendations to prod only", not prod-or-debug). A simulation
  read failure (e.g. no sim runs yet) degrades to a warning + empty sim data
  rather than failing the whole run — prod-only history can still be useful
  for setup types with a longer production track record.
"""

from __future__ import annotations

import json
import math
import statistics
import uuid
from typing import Any, Final, Protocol

from app.database import duckdb_manager
from app.services.simulation.simulation_engine import compute_metrics
from app.utils import logging_config
from app.utils import service_result as sr
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_SIMULATION: Final[str] = duckdb_manager.DB_ROLE_SIMULATION
# Write path is prod-only by design (see module docstring).
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD,)

# --------------------------------------------------------------------------- #
# Guardrail defaults (overridable per run(), never hardcoded into the math)
# --------------------------------------------------------------------------- #
DEFAULT_SAMPLE_FLOOR: Final[int] = 30
DEFAULT_MARGIN_K: Final[float] = 1.0

# --------------------------------------------------------------------------- #
# config_recommendations.status vocabulary
# --------------------------------------------------------------------------- #
STATUS_PENDING: Final[str] = "pending"
STATUS_APPROVED: Final[str] = "approved"
STATUS_REJECTED: Final[str] = "rejected"
ALLOWED_STATUSES: Final[tuple[str, ...]] = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

# --------------------------------------------------------------------------- #
# Metadata key contract
# --------------------------------------------------------------------------- #
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "run_id",
    "sample_floor",
    "margin_k",
    "prod_outcomes_read",
    "sim_outcomes_read",
    "cells_evaluated",
    "cells_skipped_no_incumbent_data",
    "candidates_evaluated",
    "recommendations_written",
)


class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL — reads (own local queries; never routed through ConfigService, so
# there is no import path in this module that could reach
# activate_setup_config).
# --------------------------------------------------------------------------- #
_SELECT_PROD_OUTCOMES: Final[str] = (
    "SELECT so.setup_type, so.setup_config_id, sp.market_regime, "
    "  so.signal_date, so.realized_r_multiple, so.stop_hit, so.target_hit "
    "FROM signal_outcomes so "
    "LEFT JOIN step5_proposals sp ON sp.proposal_id = so.proposal_id"
)

_SELECT_SIM_OUTCOMES: Final[str] = (
    "SELECT o.setup_type, o.setup_config_id, p.market_regime, "
    "  o.signal_date, o.realized_r_multiple, o.stop_hit, o.target_hit "
    "FROM sim_signal_outcomes o "
    "LEFT JOIN sim_step5_proposals p ON p.proposal_id = o.proposal_id "
    "WHERE o.cross_fold_outcome = FALSE"
)

_OUTCOME_ROW_COLS: Final[tuple[str, ...]] = (
    "setup_type", "config_id", "regime", "signal_date",
    "realized_r_multiple", "stop_hit", "target_hit",
)

_SELECT_ACTIVE_CONFIGS: Final[str] = (
    "SELECT setup_type, config_id FROM setup_configs WHERE active_flag = TRUE"
)

_SELECT_CONFIG_JSON_BY_IDS: Final[str] = (
    "SELECT config_id, config_json FROM setup_configs WHERE config_id = ANY(?)"
)

# --------------------------------------------------------------------------- #
# SQL — writes
# --------------------------------------------------------------------------- #
_INSERT_RECOMMENDATION: Final[str] = (
    "INSERT INTO config_recommendations "
    "(recommendation_id, run_id, setup_type, regime, incumbent_config_id, "
    " candidate_config_id, proposal_json, evidence_json, status, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

_SELECT_PENDING: Final[str] = (
    "SELECT recommendation_id, run_id, setup_type, regime, incumbent_config_id, "
    " candidate_config_id, proposal_json, evidence_json, status, created_at "
    "FROM config_recommendations WHERE status = 'pending'"
)

_PENDING_COLS: Final[tuple[str, ...]] = (
    "recommendation_id", "run_id", "setup_type", "regime", "incumbent_config_id",
    "candidate_config_id", "proposal_json", "evidence_json", "status", "created_at",
)


# --------------------------------------------------------------------------- #
# Pure helpers — statistics (no I/O; directly unit-testable).
# --------------------------------------------------------------------------- #
def _hit_rate(flags: list[bool | None]) -> float | None:
    """Fraction of non-NULL flags that are True; None if none resolved."""
    resolved = [f for f in flags if f is not None]
    if not resolved:
        return None
    return sum(1 for f in resolved if f) / len(resolved)


def evaluate_candidate(
    candidate_resolved: list[float],
    incumbent_resolved: list[float],
    candidate_expectancy: float,
    incumbent_expectancy: float,
    *,
    sample_floor: int = DEFAULT_SAMPLE_FLOOR,
    margin_k: float = DEFAULT_MARGIN_K,
) -> dict[str, Any]:
    """Pairwise statistical guardrail: does ``candidate`` beat ``incumbent``
    by enough margin, with enough samples, to be worth recommending?

    Both guardrails are required to qualify:

    1. **Sample floor** — ``len(candidate_resolved) >= sample_floor`` and
       ``len(incumbent_resolved) >= sample_floor``.
    2. **Improvement margin** — ``candidate_expectancy - incumbent_expectancy
       >= margin_k * pooled_se``, where ``pooled_se`` is the standard error of
       the difference in sample means (pooled two-sample SE using each
       group's own sample variance). See module docstring for why this
       simple margin rule was chosen over a fuller multiple-testing
       correction.

    ``candidate_expectancy`` / ``incumbent_expectancy`` are passed in (not
    recomputed here) so callers reuse
    :func:`app.services.simulation.simulation_engine.compute_metrics`'s
    ``"expectancy"`` output rather than this function silently duplicating
    that formula.
    """
    n_candidate = len(candidate_resolved)
    n_incumbent = len(incumbent_resolved)
    meets_floor = n_candidate >= sample_floor and n_incumbent >= sample_floor

    out: dict[str, Any] = {
        "n_candidate": n_candidate,
        "n_incumbent": n_incumbent,
        "sample_floor": sample_floor,
        "margin_k": margin_k,
        "meets_sample_floor": meets_floor,
        "candidate_expectancy": candidate_expectancy,
        "incumbent_expectancy": incumbent_expectancy,
    }
    if not meets_floor:
        out.update({"margin_required": None, "margin_achieved": None, "qualified": False})
        return out

    candidate_var = statistics.variance(candidate_resolved) if n_candidate >= 2 else 0.0
    incumbent_var = statistics.variance(incumbent_resolved) if n_incumbent >= 2 else 0.0
    pooled_se = math.sqrt(candidate_var / n_candidate + incumbent_var / n_incumbent)
    margin_required = margin_k * pooled_se
    margin_achieved = candidate_expectancy - incumbent_expectancy

    out.update({
        "margin_required": margin_required,
        "margin_achieved": margin_achieved,
        "qualified": margin_achieved >= margin_required,
    })
    return out


def build_parameter_diff(
    incumbent_config_json: dict[str, Any] | None,
    candidate_config_json: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Diff two setup_config ``validation`` blocks into a proposal_json list.

    Returns ``[{"parameter": key, "current_value": ..., "proposed_value": ...}, ...]``
    for every key in the candidate's validation block that differs from the
    incumbent's (missing config_json on either side degrades to an empty
    comparison base rather than raising).
    """
    inc_val = (incumbent_config_json or {}).get("validation", {}) or {}
    cand_val = (candidate_config_json or {}).get("validation", {}) or {}
    diffs: list[dict[str, Any]] = []
    for key in sorted(cand_val):
        current = inc_val.get(key)
        proposed = cand_val[key]
        if current != proposed:
            diffs.append({"parameter": key, "current_value": current, "proposed_value": proposed})
    return diffs


def _group_outcome_rows(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str | None], dict[str, list[dict[str, Any]]]]:
    """Group outcome rows by (setup_type, regime) -> config_id -> rows."""
    cells: dict[tuple[str, str | None], dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        key = (row["setup_type"], row["regime"])
        cells.setdefault(key, {}).setdefault(row["config_id"], []).append(row)
    return cells


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class ConfigRecommenderService:
    """Aggregates outcomes and proposes config changes for human review.

    Never activates a config. The optional ``db_manager`` argument exists
    only for test injection; when ``None`` the approved
    :mod:`app.database.duckdb_manager` is used.
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = db_manager if db_manager is not None else duckdb_manager

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def run(
        self,
        db_role: str = DB_ROLE_PROD,
        sample_floor: int = DEFAULT_SAMPLE_FLOOR,
        margin_k: float = DEFAULT_MARGIN_K,
        run_id: str | None = None,
    ) -> ServiceResult:
        """Aggregate outcomes, evaluate guardrails, write pending proposals."""
        run_id = run_id or str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        warnings: list[str] = []

        if db_role not in ALLOWED_DB_ROLES:
            msg = (
                f"Unsupported db_role {db_role!r}. Config recommender writes "
                f"only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("ConfigRecommender failed: %s", msg)
            return self._failed(run_id, msg, self._empty_metadata(db_role, sample_floor, margin_k))

        try:
            prod_rows = self._read_prod_outcomes()
        except Exception as exc:  # noqa: BLE001
            msg = f"prod outcome read failed: {type(exc).__name__}: {exc}"
            log.error("ConfigRecommender failed: %s", msg)
            return self._failed(run_id, msg, self._empty_metadata(db_role, sample_floor, margin_k))

        try:
            sim_rows = self._read_sim_outcomes()
        except Exception as exc:  # noqa: BLE001
            # Degrade gracefully -- prod-only history can still be useful.
            warnings.append(f"sim outcome read failed (continuing prod-only): {type(exc).__name__}: {exc}")
            sim_rows = []

        try:
            active_configs = self._read_active_configs()
        except Exception as exc:  # noqa: BLE001
            msg = f"active config read failed: {type(exc).__name__}: {exc}"
            log.error("ConfigRecommender failed: %s", msg)
            return self._failed(run_id, msg, self._empty_metadata(db_role, sample_floor, margin_k))

        all_rows = prod_rows + sim_rows
        cells = _group_outcome_rows(all_rows)

        cells_evaluated = 0
        cells_skipped = 0
        candidates_evaluated = 0
        proposals: list[dict[str, Any]] = []

        for (setup_type, regime), by_config in cells.items():
            incumbent_id = active_configs.get(setup_type)
            if incumbent_id is None or incumbent_id not in by_config:
                cells_skipped += 1
                continue
            cells_evaluated += 1

            incumbent_series = sorted(by_config[incumbent_id], key=lambda r: r["signal_date"])
            incumbent_returns = [r["realized_r_multiple"] for r in incumbent_series]
            incumbent_resolved = [r for r in incumbent_returns if r is not None]
            incumbent_metrics = compute_metrics(incumbent_returns)
            incumbent_expectancy = incumbent_metrics["expectancy"] or 0.0

            candidate_evals: list[dict[str, Any]] = []
            for config_id, series in by_config.items():
                if config_id == incumbent_id:
                    continue
                candidates_evaluated += 1
                series_sorted = sorted(series, key=lambda r: r["signal_date"])
                cand_returns = [r["realized_r_multiple"] for r in series_sorted]
                cand_resolved = [r for r in cand_returns if r is not None]
                cand_metrics = compute_metrics(cand_returns)
                cand_expectancy = cand_metrics["expectancy"] or 0.0

                evaluation = evaluate_candidate(
                    cand_resolved, incumbent_resolved,
                    cand_expectancy, incumbent_expectancy,
                    sample_floor=sample_floor, margin_k=margin_k,
                )
                evaluation["config_id"] = config_id
                evaluation["metrics"] = cand_metrics
                evaluation["target_hit_rate"] = _hit_rate([r["target_hit"] for r in series_sorted])
                evaluation["stop_hit_rate"] = _hit_rate([r["stop_hit"] for r in series_sorted])
                candidate_evals.append(evaluation)

            qualified = [c for c in candidate_evals if c["qualified"]]
            if not qualified:
                continue
            winner = max(qualified, key=lambda c: c["margin_achieved"])

            proposals.append({
                "setup_type": setup_type,
                "regime": regime,
                "incumbent_config_id": incumbent_id,
                "candidate_config_id": winner["config_id"],
                "incumbent_n": len(incumbent_resolved),
                "incumbent_metrics": incumbent_metrics,
                "incumbent_target_hit_rate": _hit_rate([r["target_hit"] for r in incumbent_series]),
                "incumbent_stop_hit_rate": _hit_rate([r["stop_hit"] for r in incumbent_series]),
                "all_candidates": candidate_evals,
            })

        # Fetch config_json for every incumbent/candidate id referenced, to
        # build the human-readable parameter diff.
        config_ids_needed = {p["incumbent_config_id"] for p in proposals} | {
            p["candidate_config_id"] for p in proposals
        }
        try:
            config_jsons = self._read_config_jsons(config_ids_needed) if config_ids_needed else {}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"config_json read failed (diffs will be empty): {type(exc).__name__}: {exc}")
            config_jsons = {}

        rows_to_write: list[list[Any]] = []
        for p in proposals:
            incumbent_cfg = config_jsons.get(p["incumbent_config_id"])
            candidate_cfg = config_jsons.get(p["candidate_config_id"])
            proposal_diff = build_parameter_diff(incumbent_cfg, candidate_cfg)
            evidence = {
                "sample_floor": sample_floor,
                "margin_k": margin_k,
                "incumbent": {
                    "config_id": p["incumbent_config_id"],
                    "n": p["incumbent_n"],
                    "expectancy": p["incumbent_metrics"]["expectancy"],
                    "win_rate": p["incumbent_metrics"]["win_rate"],
                    "profit_factor": p["incumbent_metrics"]["profit_factor"],
                    "max_drawdown_pct": p["incumbent_metrics"]["max_drawdown_pct"],
                    "resolved_outcomes_pct": p["incumbent_metrics"]["resolved_outcomes_pct"],
                    "target_hit_rate": p["incumbent_target_hit_rate"],
                    "stop_hit_rate": p["incumbent_stop_hit_rate"],
                },
                "candidates": [
                    {
                        "config_id": c["config_id"],
                        "n_candidate": c["n_candidate"],
                        "n_incumbent": c["n_incumbent"],
                        "meets_sample_floor": c["meets_sample_floor"],
                        "candidate_expectancy": c["candidate_expectancy"],
                        "incumbent_expectancy": c["incumbent_expectancy"],
                        "margin_required": c["margin_required"],
                        "margin_achieved": c["margin_achieved"],
                        "qualified": c["qualified"],
                        "win_rate": c["metrics"]["win_rate"],
                        "profit_factor": c["metrics"]["profit_factor"],
                        "max_drawdown_pct": c["metrics"]["max_drawdown_pct"],
                        "resolved_outcomes_pct": c["metrics"]["resolved_outcomes_pct"],
                        "target_hit_rate": c["target_hit_rate"],
                        "stop_hit_rate": c["stop_hit_rate"],
                    }
                    for c in p["all_candidates"]
                ],
                "winner_config_id": p["candidate_config_id"],
            }
            rows_to_write.append([
                str(uuid.uuid4()), run_id, p["setup_type"], p["regime"],
                p["incumbent_config_id"], p["candidate_config_id"],
                json.dumps(proposal_diff, default=str),
                json.dumps(evidence, default=str),
                STATUS_PENDING,
            ])

        try:
            written = self._write_recommendations(db_role, rows_to_write)
        except Exception as exc:  # noqa: BLE001
            msg = f"write failed (rolled back): {type(exc).__name__}: {exc}"
            log.error("ConfigRecommender failed: %s", msg)
            return self._failed(run_id, msg, self._empty_metadata(db_role, sample_floor, margin_k))

        metadata = {
            "db_role": db_role,
            "run_id": run_id,
            "sample_floor": sample_floor,
            "margin_k": margin_k,
            "prod_outcomes_read": len(prod_rows),
            "sim_outcomes_read": len(sim_rows),
            "cells_evaluated": cells_evaluated,
            "cells_skipped_no_incumbent_data": cells_skipped,
            "candidates_evaluated": candidates_evaluated,
            "recommendations_written": written,
        }
        log.info(
            "ConfigRecommender done cells=%d skipped=%d candidates=%d written=%d",
            cells_evaluated, cells_skipped, candidates_evaluated, written,
        )
        status = sr.STATUS_SUCCESS_WITH_WARNINGS if warnings else sr.STATUS_SUCCESS
        return ServiceResult(
            status=status, run_id=run_id, rows_processed=written,
            warnings=warnings, metadata=metadata,
        )

    def get_pending_recommendations(
        self, db_role: str = DB_ROLE_PROD, setup_type: str | None = None,
    ) -> ServiceResult:
        """Read-only accessor for pending recommendations (for a future
        dashboard/M21 read — this module does not touch dashboard code)."""
        run_id = str(uuid.uuid4())
        if db_role not in ALLOWED_DB_ROLES:
            msg = f"Unsupported db_role {db_role!r}."
            return self._failed(run_id, msg, {"db_role": db_role})

        connection = self._db.connect(db_role, read_only=True)
        try:
            rows = connection.execute(_SELECT_PENDING).fetchall()
        finally:
            connection.close()

        results = [dict(zip(_PENDING_COLS, r)) for r in rows]
        for r in results:
            r["proposal_json"] = _loads(r["proposal_json"])
            r["evidence_json"] = _loads(r["evidence_json"])
        if setup_type is not None:
            results = [r for r in results if r["setup_type"] == setup_type]

        return ServiceResult(
            status=sr.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(results),
            metadata={"db_role": db_role, "setup_type": setup_type, "recommendations": results},
        )

    # ------------------------------------------------------------------ #
    # Read helpers
    # ------------------------------------------------------------------ #
    def _read_prod_outcomes(self) -> list[dict[str, Any]]:
        connection = self._db.connect(DB_ROLE_PROD, read_only=True)
        try:
            rows = connection.execute(_SELECT_PROD_OUTCOMES).fetchall()
        finally:
            connection.close()
        return [dict(zip(_OUTCOME_ROW_COLS, r)) for r in rows]

    def _read_sim_outcomes(self) -> list[dict[str, Any]]:
        connection = self._db.connect(DB_ROLE_SIMULATION, read_only=True)
        try:
            rows = connection.execute(_SELECT_SIM_OUTCOMES).fetchall()
        finally:
            connection.close()
        return [dict(zip(_OUTCOME_ROW_COLS, r)) for r in rows]

    def _read_active_configs(self) -> dict[str, str]:
        connection = self._db.connect(DB_ROLE_PROD, read_only=True)
        try:
            rows = connection.execute(_SELECT_ACTIVE_CONFIGS).fetchall()
        finally:
            connection.close()
        return {setup_type: config_id for setup_type, config_id in rows}

    def _read_config_jsons(self, config_ids: set[str]) -> dict[str, dict[str, Any]]:
        """Merge config_json lookups from prod and simulation (prod wins on
        overlap) -- presets may have been seeded into either DB."""
        ids_list = list(config_ids)
        result: dict[str, dict[str, Any]] = {}

        sim_conn = self._db.connect(DB_ROLE_SIMULATION, read_only=True)
        try:
            for config_id, config_json in sim_conn.execute(
                _SELECT_CONFIG_JSON_BY_IDS, [ids_list]
            ).fetchall():
                result[config_id] = _loads(config_json)
        except Exception:  # noqa: BLE001 - sim DB may not exist yet; prod may still cover it
            pass
        finally:
            sim_conn.close()

        prod_conn = self._db.connect(DB_ROLE_PROD, read_only=True)
        try:
            for config_id, config_json in prod_conn.execute(
                _SELECT_CONFIG_JSON_BY_IDS, [ids_list]
            ).fetchall():
                result[config_id] = _loads(config_json)  # prod overrides sim on overlap
        finally:
            prod_conn.close()

        return result

    # ------------------------------------------------------------------ #
    # Write helper
    # ------------------------------------------------------------------ #
    def _write_recommendations(self, db_role: str, rows: list[list[Any]]) -> int:
        if not rows:
            return 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for params in rows:
                    connection.execute(_INSERT_RECOMMENDATION, params)
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            connection.close()
        return len(rows)

    # ------------------------------------------------------------------ #
    # Result builders
    # ------------------------------------------------------------------ #
    @staticmethod
    def _empty_metadata(db_role: str, sample_floor: int, margin_k: float) -> dict[str, Any]:
        return {
            "db_role": db_role,
            "run_id": None,
            "sample_floor": sample_floor,
            "margin_k": margin_k,
            "prod_outcomes_read": 0,
            "sim_outcomes_read": 0,
            "cells_evaluated": 0,
            "cells_skipped_no_incumbent_data": 0,
            "candidates_evaluated": 0,
            "recommendations_written": 0,
        }

    @staticmethod
    def _failed(run_id: str, message: str, metadata: dict[str, Any]) -> ServiceResult:
        metadata = dict(metadata)
        metadata["run_id"] = run_id
        return ServiceResult(
            status=sr.STATUS_FAILED, run_id=run_id, rows_processed=0,
            errors=[message], metadata=metadata,
        )


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


__all__ = [
    "ConfigRecommenderService",
    "ALLOWED_DB_ROLES",
    "METADATA_KEYS",
    "DEFAULT_SAMPLE_FLOOR",
    "DEFAULT_MARGIN_K",
    "STATUS_PENDING",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "ALLOWED_STATUSES",
    "evaluate_candidate",
    "build_parameter_diff",
]
