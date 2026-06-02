"""Module 15 — Step 5 Proposal Engine.

Reads the Step 4 analyses for one ``signal_date`` / ``strategy_config_id`` from
``step4_analysis``, joins each analysis to its Step 3 screening score
(``step3_candidates`` on ``candidate_id``) and its ticker's sector / industry
(``ticker_master`` on ``ticker``), computes a raw proposal score, assigns a raw
ranking, applies either *hard-cap* or *soft-penalty* diversification, and appends
one row per *analyzable* Step 4 analysis to ``step5_proposals`` in a single
transaction. It runs after Module 14 (Step 4 Setup Analysis) and before Module 16.

Contract source of truth: ``M15_STEP5_PROPOSAL_ENGINE_SPEC.md`` (derived from the
frozen split Project Files — ``01b_SCHEMA_AND_DATA.md`` for the
``step5_proposals`` / ``step4_analysis`` / ``step3_candidates`` / ``ticker_master``
schema, ``01c_FORMULAS_AND_CONFIGS.md`` for the Step 5 scoring / RR-tier / raw-rank
/ diversification formulas, ``02b_ARCHITECTURE_DECISIONS.md`` AD-22.11 / AD-22.12 /
AD-22.13 for the raw+diversified ranking and the hard-cap-implies-no-soft-penalty
rule, ``01d_MODULES_AND_PIPELINE.md`` for the pipeline position, and the frozen
Module 14 service for the ``db_role`` guard / config-validation / read→compute→
single-write transaction style).

**Config-key naming (canonical)**
The Module 15 engine accepts the following canonical internal key names under the
``diversification`` section:

- ``hard_cap_enabled``  (bool)
- ``top_n``             (int > 0)
- hard-cap: ``max_sector_count``, ``max_industry_count`` (int >= 1)
- soft-penalty: ``sector_penalty``, ``industry_penalty`` (float, 0 < x <= 1)

The example strategy-config blocks in ``01c_FORMULAS_AND_CONFIGS.md`` use the
*legacy* names ``sector_max_positions`` / ``industry_max_positions`` (hard-cap)
and ``sector_penalty_factor`` / ``industry_penalty_factor`` (soft-penalty).
:func:`_normalise_diversification_block` transparently maps all legacy names to
canonical names before validation, so *both* naming conventions are accepted.
Callers that already use canonical names are unaffected.

**G-UNKNOWN-BUCKET decision**
NULL ``sector`` or NULL ``industry`` from ``ticker_master`` are mapped to the
``__UNKNOWN_SECTOR__`` / ``__UNKNOWN_INDUSTRY__`` sentinel strings and participate
in hard-cap counts / soft-penalty multipliers **as a single shared bucket**. This
is intentional: unknown-sector tickers are genuinely uncategorised and should be
subject to the same concentration limits as any named bucket. For small caps
(e.g. ``max_sector_count = 1``) this means only one unknown-sector ticker can be
accepted; this is the desired conservative behaviour. If callers want to exclude
sector/industry concentration limits for truly unclassified tickers they must
ensure ``ticker_master`` is populated before running Step 5.

This module only ever *inserts* into ``step5_proposals``. It never updates or
deletes existing rows, never writes any other table, never runs DDL, never calls
providers, never imports ``duckdb`` directly, never uses ``ATTACH``, never
bypasses the DuckDB manager, and never uses ``print()``. Reruns are append-only:
a new ``run_id`` simply produces a fresh set of proposal rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Roles this module is allowed to target.
# --------------------------------------------------------------------------- #
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module 15
# never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are accepted;
# any other value yields a ``failed`` result with no reads/writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# Unknown-bucket sentinels for NULL sector / industry (gap G-UNKNOWN-BUCKET).
UNKNOWN_SECTOR: Final[str] = "__UNKNOWN_SECTOR__"
UNKNOWN_INDUSTRY: Final[str] = "__UNKNOWN_INDUSTRY__"

# Default timing_score when the Step 4 row's timing_score is NULL.
DEFAULT_TIMING_SCORE: Final[float] = 50.0

# Rejection-reason labels (hard-cap mode only).
REJECT_SECTOR_CAP: Final[str] = "sector_cap"
REJECT_INDUSTRY_CAP: Final[str] = "industry_cap"

# Raw proposal-score component weights (sum == 1.0).
_W_SETUP: Final[float] = 0.40
_W_SCREENING: Final[float] = 0.25
_W_RR: Final[float] = 0.20
_W_TIMING: Final[float] = 0.15

# The exact metadata key set returned on every return path.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "signal_date",
    "strategy_config_id",
    "run_id",
    "analyses_read",
    "proposals_written",
    "raw_top_n_count",
    "diversified_top_n_count",
    "hard_cap_rejections",
)


# --------------------------------------------------------------------------- #
# Config-key name normalisation (legacy -> canonical).
# --------------------------------------------------------------------------- #
# The example strategy-config blocks in ``01c_FORMULAS_AND_CONFIGS.md`` use
# ``sector_max_positions`` / ``industry_max_positions`` (hard-cap) and
# ``sector_penalty_factor`` / ``industry_penalty_factor`` (soft-penalty).
# The canonical internal names are the shorter forms used in the module prompt.
# This mapping is applied once in _normalise_diversification_block before any
# validation, so both naming conventions are silently accepted.
_LEGACY_KEY_MAP: Final[dict[str, str]] = {
    "sector_max_positions": "max_sector_count",
    "industry_max_positions": "max_industry_count",
    "sector_penalty_factor": "sector_penalty",
    "industry_penalty_factor": "industry_penalty",
}


def _normalise_diversification_block(block: dict) -> dict:
    """Return a copy of ``block`` with legacy key names rewritten to canonical.

    Only the four legacy names in :data:`_LEGACY_KEY_MAP` are renamed; all other
    keys are preserved unchanged. The original ``block`` is not mutated.

    Raises
    ------
    _ConfigError
        If a caller supplies **both** a legacy name and its canonical equivalent
        for the same key (e.g. ``sector_max_positions`` and ``max_sector_count``
        in the same config block). Accepting both silently would hide accidental
        copy-paste errors where the two values disagree.
    """
    # Detect ambiguous duplicate: a block that contains both the legacy and the
    # canonical name for the same underlying key is almost certainly a mistake.
    for legacy, canonical in _LEGACY_KEY_MAP.items():
        if legacy in block and canonical in block:
            raise _ConfigError(
                f"diversification block contains both legacy name {legacy!r} "
                f"and canonical name {canonical!r} for the same key; "
                f"remove one to avoid ambiguity"
            )
    out: dict = {}
    for k, v in block.items():
        out[_LEGACY_KEY_MAP.get(k, k)] = v
    return out


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class _ConfigError(ValueError):
    """Raised internally when ``strategy_config`` is missing / invalid a key."""


# --------------------------------------------------------------------------- #
# SQL (operates only on existing objects; no DDL).
# --------------------------------------------------------------------------- #
# All Step 4 analyses for this signal_date / strategy_config_id, joined to the
# Step 3 screening score (on candidate_id) and the ticker's sector / industry.
_SELECT_ANALYSES: Final[str] = (
    "SELECT "
    "  a.analysis_id AS analysis_id, "
    "  a.candidate_id AS candidate_id, "
    "  a.ticker AS ticker, "
    "  a.setup_score AS setup_score, "
    "  a.timing_score AS timing_score, "
    "  a.estimated_rr AS estimated_rr, "
    "  c.screening_score AS screening_score, "
    "  t.sector AS sector, "
    "  t.industry AS industry "
    "FROM step4_analysis a "
    "LEFT JOIN step3_candidates c ON c.candidate_id = a.candidate_id "
    "LEFT JOIN ticker_master t ON t.ticker = a.ticker "
    "WHERE a.signal_date = ? "
    "  AND a.strategy_config_id = ? "
    "ORDER BY a.ticker, a.analysis_id"
)

# Parameterized single-row INSERT. Each row gets its own execute() call inside
# one BEGIN TRANSACTION / COMMIT (per-row execute, not executemany).
_INSERT_PROPOSAL: Final[str] = (
    "INSERT INTO step5_proposals "
    "(proposal_id, run_id, strategy_config_id, ticker, signal_date, "
    " proposal_score_raw, diversity_penalty, proposal_score_final, "
    " rank_position, raw_rank, diversified_rank, in_raw_top_n, "
    " in_diversified_top_n, diversification_applied, selected_top_n, "
    " selected_flag, ai_reviewed, executed_flag, rejection_reason, "
    " mechanical_explanation, sector_count_at_selection, "
    " industry_count_at_selection, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, FALSE, ?, "
    " ?, ?, ?, CAST(now() AS TIMESTAMP))"
)


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #
def _json_dumps(payload: Any) -> str:
    """Serialise ``payload`` deterministically (sorted keys)."""
    return json.dumps(payload, sort_keys=True)


def _f(value: Any) -> float | None:
    """Coerce a DB cell to ``float`` or ``None`` (NaN -> ``None``)."""
    if value is None:
        return None
    fv = float(value)
    if fv != fv:  # NaN
        return None
    return fv


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def _rr_score(estimated_rr: float | None) -> float:
    """Map ``estimated_rr`` onto the Step 5 RR tier score (NULL -> 0)."""
    if estimated_rr is None:
        return 0.0
    if estimated_rr >= 3.0:
        return 100.0
    if 2.2 <= estimated_rr < 3.0:
        return 80.0
    if 1.8 <= estimated_rr < 2.2:
        return 60.0
    return 0.0


def _proposal_score_raw(
    setup_score: float,
    screening_score: float,
    rr_score: float,
    timing_score: float,
) -> float:
    """Weighted raw proposal score, clamped to ``[0, 100]``."""
    raw = (
        _W_SETUP * setup_score
        + _W_SCREENING * screening_score
        + _W_RR * rr_score
        + _W_TIMING * timing_score
    )
    return _clamp(raw)


# --------------------------------------------------------------------------- #
# Step 5 proposal engine.
# --------------------------------------------------------------------------- #
class Step5ProposalEngine:
    """Score + rank + diversify Step 4 analyses for one signal date / config.

    The engine is effectively stateless; the optional ``db_manager`` constructor
    argument exists only so tests can inject a fake/wrapping manager. When it is
    ``None`` the real :mod:`app.database.duckdb_manager` is used, which is the
    single approved DB entry point (no arbitrary paths, no ``ATTACH``).
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )

    # ------------------------------------------------------------------ #
    # Public API (EXACT signature — do not vary).
    # ------------------------------------------------------------------ #
    def propose(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Score / rank / diversify Step 4 analyses and write proposal rows.

        Parameters
        ----------
        signal_date:
            The signal date. Only ``step4_analysis`` rows with this
            ``signal_date`` (and ``strategy_config_id``) are processed.
        strategy_config:
            Parsed strategy-config JSON. Required keys live under
            ``diversification`` (see :meth:`_parse_config`).
        strategy_config_id:
            Opaque config id, copied to every written proposal row.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns ``failed`` before any DB read/write.
        run_id:
            A fresh ``uuid4`` is minted when ``None``; a supplied value is kept.

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``metadata["proposals_written"]`` on every
            return path. ``metadata`` carries exactly :data:`METADATA_KEYS`.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        signal_iso = signal_date.isoformat()

        log.info(
            "propose start db_role=%s signal_date=%s strategy_config_id=%s",
            db_role,
            signal_iso,
            strategy_config_id,
        )

        # --- db_role guard: prod/debug only, never simulation. No I/O. ----- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 15 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("propose failed: %s", message)
            return self._failed(run_id, db_role, signal_iso, strategy_config_id, message)

        # --- config guard: validate required keys before any DB access. ---- #
        try:
            cfg = self._parse_config(strategy_config)
        except _ConfigError as exc:
            log.error("propose failed: %s", exc)
            return self._failed(
                run_id, db_role, signal_iso, strategy_config_id, str(exc)
            )

        # --- read phase (read-only). --------------------------------------- #
        try:
            analyses = self._read(db_role, signal_date, strategy_config_id)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("propose failed: %s", message)
            return self._failed(
                run_id, db_role, signal_iso, strategy_config_id, message
            )

        analyses_read = len(analyses)

        # --- empty input: success, zero counts, no insert. ----------------- #
        if analyses_read == 0:
            log.info("propose done: no step4 analyses for %s", signal_iso)
            return self._success(
                run_id,
                db_role,
                signal_iso,
                strategy_config_id,
                analyses_read=0,
                rows=[],
            )

        # --- compute phase (pure Python, no DB). --------------------------- #
        rows = self._build_rows(
            analyses, cfg, run_id, strategy_config_id, signal_date
        )

        # --- write phase: per-row execute() inside one BEGIN/COMMIT. ------- #
        try:
            self._write(db_role, rows)
        except Exception as exc:  # noqa: BLE001 - surface as failed; rollback inside
            log.error(
                "propose failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            return self._failed(
                run_id,
                db_role,
                signal_iso,
                strategy_config_id,
                f"{type(exc).__name__}: {exc}",
                analyses_read=analyses_read,
            )

        log.info(
            "propose done status=success analyses_read=%d proposals_written=%d",
            analyses_read,
            len(rows),
        )
        return self._success(
            run_id,
            db_role,
            signal_iso,
            strategy_config_id,
            analyses_read=analyses_read,
            rows=rows,
        )

    # ------------------------------------------------------------------ #
    # Config parsing / validation.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_config(strategy_config: dict) -> dict[str, Any]:
        """Validate and extract the required config values before DB access.

        Required ``diversification`` paths (see the Module 15 prompt contract):

        - ``hard_cap_enabled`` — bool
        - ``top_n`` — int > 0
        - hard-cap mode (``hard_cap_enabled is True``):
          ``max_sector_count`` / ``max_industry_count`` — int >= 1
        - soft-penalty mode (``hard_cap_enabled is False``):
          ``sector_penalty`` / ``industry_penalty`` — float, ``0 < x <= 1``

        Raises
        ------
        _ConfigError
            If ``strategy_config`` is not a dict or any required key is missing /
            has the wrong type / falls outside the allowed range.
        """
        if not isinstance(strategy_config, dict):
            raise _ConfigError("strategy_config must be a dict")

        raw_block = strategy_config.get("diversification")
        if not isinstance(raw_block, dict):
            raise _ConfigError("missing config section diversification")
        # Normalise legacy key names (sector_max_positions -> max_sector_count, etc.)
        # before validation so both naming conventions are accepted transparently.
        block = _normalise_diversification_block(raw_block)

        if "hard_cap_enabled" not in block:
            raise _ConfigError("missing config key diversification.hard_cap_enabled")
        hard_cap_enabled = block["hard_cap_enabled"]
        if not isinstance(hard_cap_enabled, bool):
            raise _ConfigError(
                "config key diversification.hard_cap_enabled must be bool"
            )

        if "top_n" not in block:
            raise _ConfigError("missing config key diversification.top_n")
        top_n = block["top_n"]
        if isinstance(top_n, bool) or not isinstance(top_n, int):
            raise _ConfigError("config key diversification.top_n must be int")
        if not top_n > 0:
            raise _ConfigError("config key diversification.top_n must be > 0")

        cfg: dict[str, Any] = {
            "hard_cap_enabled": hard_cap_enabled,
            "top_n": top_n,
        }

        if hard_cap_enabled:
            for key in ("max_sector_count", "max_industry_count"):
                if key not in block:
                    raise _ConfigError(f"missing config key diversification.{key}")
                value = block[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise _ConfigError(
                        f"config key diversification.{key} must be int"
                    )
                if value < 1:
                    raise _ConfigError(
                        f"config key diversification.{key} must be >= 1"
                    )
                cfg[key] = value
        else:
            for key in ("sector_penalty", "industry_penalty"):
                if key not in block:
                    raise _ConfigError(f"missing config key diversification.{key}")
                value = block[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise _ConfigError(
                        f"config key diversification.{key} must be numeric"
                    )
                fvalue = float(value)
                if not (0.0 < fvalue <= 1.0):
                    raise _ConfigError(
                        f"config key diversification.{key} must be in (0, 1]"
                    )
                cfg[key] = fvalue

        return cfg

    # ------------------------------------------------------------------ #
    # Read phase.
    # ------------------------------------------------------------------ #
    def _read(
        self,
        db_role: str,
        signal_date: date,
        strategy_config_id: str,
    ) -> list[dict[str, Any]]:
        """Read all Step 4 analyses for ``signal_date`` (read-only).

        Returns one dict per ``step4_analysis`` row (joined to its Step 3
        screening score and the ticker's sector / industry). The read connection
        is closed before any computation.
        """
        cols = (
            "analysis_id",
            "candidate_id",
            "ticker",
            "setup_score",
            "timing_score",
            "estimated_rr",
            "screening_score",
            "sector",
            "industry",
        )
        connection = self._db.connect(db_role, read_only=True)
        try:
            raw_rows = connection.execute(
                _SELECT_ANALYSES, [signal_date, strategy_config_id]
            ).fetchall()
        finally:
            connection.close()

        return [dict(zip(cols, raw)) for raw in raw_rows]

    # ------------------------------------------------------------------ #
    # Compute phase.
    # ------------------------------------------------------------------ #
    def _build_rows(
        self,
        analyses: list[dict[str, Any]],
        cfg: dict[str, Any],
        run_id: str,
        strategy_config_id: str,
        signal_date: date,
    ) -> list[dict[str, Any]]:
        """Score, rank and diversify analyzable analyses into insert payloads.

        Steps: (1) filter to analyzable rows (NULL setup/screening -> skip);
        (2) compute raw scores; (3) raw ranking with tie-breaks; (4) hard-cap or
        soft-penalty diversification; (5) shared selected semantics. One payload
        is produced per analyzable Step 4 analysis (including hard-cap rejected
        rows). Non-analyzable rows produce no payload but still count in
        ``analyses_read`` (handled by the caller via ``len(analyses)``).
        """
        top_n = cfg["top_n"]

        # --- (1) + (2): analyzable filter and raw scoring. ---------------- #
        scored: list[dict[str, Any]] = []
        for a in analyses:
            setup_score = _f(a["setup_score"])
            screening_score = _f(a["screening_score"])
            if setup_score is None or screening_score is None:
                continue  # not analyzable -> counted in analyses_read, no row

            estimated_rr = _f(a["estimated_rr"])
            timing_raw = _f(a["timing_score"])
            timing_score = (
                DEFAULT_TIMING_SCORE if timing_raw is None else timing_raw
            )
            rr_score = _rr_score(estimated_rr)
            raw_score = _proposal_score_raw(
                setup_score, screening_score, rr_score, timing_score
            )

            sector = a["sector"] if a["sector"] is not None else UNKNOWN_SECTOR
            industry = (
                a["industry"] if a["industry"] is not None else UNKNOWN_INDUSTRY
            )

            scored.append(
                {
                    "analysis_id": a["analysis_id"],
                    "candidate_id": a["candidate_id"],
                    "ticker": a["ticker"],
                    "setup_score": setup_score,
                    "screening_score": screening_score,
                    "timing_score": timing_score,
                    "estimated_rr": estimated_rr,
                    "rr_score": rr_score,
                    "proposal_score_raw": raw_score,
                    "sector": sector,
                    "industry": industry,
                }
            )

        if not scored:
            return []

        # --- (3): raw ranking. proposal_score_raw DESC, estimated_rr DESC -- #
        #          (NULL lowest), ticker ASC. 1-based raw_rank.
        def _raw_sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
            rr = item["estimated_rr"]
            rr_for_sort = rr if rr is not None else float("-inf")
            return (-item["proposal_score_raw"], -rr_for_sort, item["ticker"])

        scored.sort(key=_raw_sort_key)
        for idx, item in enumerate(scored, start=1):
            item["raw_rank"] = idx
            item["in_raw_top_n"] = idx <= top_n

        # --- (4): diversification. ---------------------------------------- #
        if cfg["hard_cap_enabled"]:
            self._apply_hard_cap(scored, cfg)
        else:
            self._apply_soft_penalty(scored, cfg)

        # --- (5): shared selected semantics + final payloads. ------------- #
        rows: list[dict[str, Any]] = []
        for item in scored:
            div_rank = item["diversified_rank"]
            in_div_top_n = div_rank is not None and div_rank <= top_n
            selected_flag = in_div_top_n
            selected_top_n = item["in_raw_top_n"] or in_div_top_n
            raw_score = item["proposal_score_raw"]
            final_score = item["proposal_score_final"]
            diversity_penalty = raw_score - final_score

            explanation = {
                "analysis_id": item["analysis_id"],
                "candidate_id": item["candidate_id"],
                "ticker": item["ticker"],
                "sector": item["sector"],
                "industry": item["industry"],
                "setup_score": item["setup_score"],
                "screening_score": item["screening_score"],
                "timing_score": item["timing_score"],
                "estimated_rr": item["estimated_rr"],
                "rr_score": item["rr_score"],
                "proposal_score_raw": raw_score,
                "proposal_score_final": final_score,
                "raw_rank": item["raw_rank"],
                "diversified_rank": div_rank,
                "diversification_mode": (
                    "hard_cap" if cfg["hard_cap_enabled"] else "soft_penalty"
                ),
                "rejection_reason": item["rejection_reason"],
                "sector_count_at_selection": item["sector_count_at_selection"],
                "industry_count_at_selection": item["industry_count_at_selection"],
            }

            rows.append(
                {
                    "proposal_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "strategy_config_id": strategy_config_id,
                    "ticker": item["ticker"],
                    "signal_date": signal_date,
                    "proposal_score_raw": raw_score,
                    "diversity_penalty": diversity_penalty,
                    "proposal_score_final": final_score,
                    "rank_position": item["raw_rank"],
                    "raw_rank": item["raw_rank"],
                    "diversified_rank": div_rank,
                    "in_raw_top_n": item["in_raw_top_n"],
                    "in_diversified_top_n": in_div_top_n,
                    "diversification_applied": True,
                    "selected_top_n": selected_top_n,
                    "selected_flag": selected_flag,
                    "rejection_reason": item["rejection_reason"],
                    "mechanical_explanation": _json_dumps(explanation),
                    "sector_count_at_selection": item["sector_count_at_selection"],
                    "industry_count_at_selection": item[
                        "industry_count_at_selection"
                    ],
                }
            )
        return rows

    @staticmethod
    def _apply_hard_cap(scored: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
        """Assign diversified ranks under hard-cap mode (mutates ``scored``).

        Candidates are processed in ``raw_rank`` order. A candidate is accepted
        while its sector and industry are both below their caps; otherwise it is
        rejected (``diversified_rank = NULL``, ``rejection_reason`` set, no soft
        penalty). ``proposal_score_final`` always equals ``proposal_score_raw``
        (AD-22.12: no double punishment). Both caps failing -> ``sector_cap``.
        """
        max_sector = cfg["max_sector_count"]
        max_industry = cfg["max_industry_count"]
        accepted_sector: dict[str, int] = {}
        accepted_industry: dict[str, int] = {}
        next_div_rank = 1

        for item in scored:  # already in raw_rank order
            sector = item["sector"]
            industry = item["industry"]
            prior_sector = accepted_sector.get(sector, 0)
            prior_industry = accepted_industry.get(industry, 0)
            sector_full = prior_sector >= max_sector
            industry_full = prior_industry >= max_industry

            item["proposal_score_final"] = item["proposal_score_raw"]

            if sector_full or industry_full:
                # Reject: still inserted, but excluded from the diversified list.
                item["diversified_rank"] = None
                # Both caps failing -> sector_cap takes priority.
                item["rejection_reason"] = (
                    REJECT_SECTOR_CAP if sector_full else REJECT_INDUSTRY_CAP
                )
                item["sector_count_at_selection"] = prior_sector
                item["industry_count_at_selection"] = prior_industry
            else:
                item["diversified_rank"] = next_div_rank
                next_div_rank += 1
                item["rejection_reason"] = None
                accepted_sector[sector] = prior_sector + 1
                accepted_industry[industry] = prior_industry + 1
                item["sector_count_at_selection"] = accepted_sector[sector]
                item["industry_count_at_selection"] = accepted_industry[industry]

    @staticmethod
    def _apply_soft_penalty(
        scored: list[dict[str, Any]], cfg: dict[str, Any]
    ) -> None:
        """Assign diversified ranks under soft-penalty mode (mutates ``scored``).

        No hard rejection. Processing in ``raw_rank`` order, each candidate is
        penalised by ``penalty ** prior_count`` where ``prior_count`` is the
        number of earlier (lower ``raw_rank``) candidates sharing its sector /
        industry. Candidates are then ranked by ``proposal_score_final`` DESC,
        ``ticker`` ASC. ``rejection_reason`` is ``None`` for every row.
        """
        sector_penalty = cfg["sector_penalty"]
        industry_penalty = cfg["industry_penalty"]
        seen_sector: dict[str, int] = {}
        seen_industry: dict[str, int] = {}

        for item in scored:  # already in raw_rank order
            sector = item["sector"]
            industry = item["industry"]
            prior_sector = seen_sector.get(sector, 0)
            prior_industry = seen_industry.get(industry, 0)

            sector_multiplier = sector_penalty ** max(0, prior_sector)
            industry_multiplier = industry_penalty ** max(0, prior_industry)
            final_score = _clamp(
                item["proposal_score_raw"]
                * sector_multiplier
                * industry_multiplier
            )

            item["proposal_score_final"] = final_score
            item["rejection_reason"] = None
            item["sector_count_at_selection"] = prior_sector + 1
            item["industry_count_at_selection"] = prior_industry + 1

            seen_sector[sector] = prior_sector + 1
            seen_industry[industry] = prior_industry + 1

        # Re-rank by final score (DESC) then ticker (ASC).
        order = sorted(
            range(len(scored)),
            key=lambda i: (-scored[i]["proposal_score_final"], scored[i]["ticker"]),
        )
        for div_rank, pos in enumerate(order, start=1):
            scored[pos]["diversified_rank"] = div_rank

    # ------------------------------------------------------------------ #
    # Write phase.
    # ------------------------------------------------------------------ #
    def _write(self, db_role: str, rows: list[dict[str, Any]]) -> int:
        """Append all proposal rows inside a single ``BEGIN TRANSACTION / COMMIT``.

        Each row is written with its own ``execute()`` call (not ``executemany``)
        and the row count is returned. Any error triggers ``ROLLBACK`` so no
        partial Module 15 rows survive. An empty plan returns 0 immediately
        without opening a transaction.

        Rollback safety: if ``ROLLBACK`` itself raises, the original write / COMMIT
        exception is re-raised unchanged (the rollback error is suppressed via
        ``except Exception`` so it cannot mask the root cause).
        """
        if not rows:
            return 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for row in rows:
                    connection.execute(
                        _INSERT_PROPOSAL,
                        [
                            row["proposal_id"],
                            row["run_id"],
                            row["strategy_config_id"],
                            row["ticker"],
                            row["signal_date"],
                            row["proposal_score_raw"],
                            row["diversity_penalty"],
                            row["proposal_score_final"],
                            row["rank_position"],
                            row["raw_rank"],
                            row["diversified_rank"],
                            row["in_raw_top_n"],
                            row["in_diversified_top_n"],
                            row["diversification_applied"],
                            row["selected_top_n"],
                            row["selected_flag"],
                            row["rejection_reason"],
                            row["mechanical_explanation"],
                            row["sector_count_at_selection"],
                            row["industry_count_at_selection"],
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                # Attempt ROLLBACK but never let a rollback failure mask the
                # original write / COMMIT exception; re-raise the original.
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
        finally:
            connection.close()
        return len(rows)

    # ------------------------------------------------------------------ #
    # Result builders.
    # ------------------------------------------------------------------ #
    def _success(
        self,
        run_id: str,
        db_role: str,
        signal_iso: str,
        strategy_config_id: str,
        *,
        analyses_read: int,
        rows: list[dict[str, Any]],
    ) -> ServiceResult:
        """Build a ``success`` result from the written rows."""
        proposals_written = len(rows)
        raw_top_n_count = sum(1 for r in rows if r["in_raw_top_n"])
        diversified_top_n_count = sum(
            1 for r in rows if r["in_diversified_top_n"]
        )
        hard_cap_rejections = sum(
            1 for r in rows if r["rejection_reason"] is not None
        )

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=proposals_written,
            metadata=self._metadata(
                db_role=db_role,
                signal_date=signal_iso,
                strategy_config_id=strategy_config_id,
                run_id=run_id,
                analyses_read=analyses_read,
                proposals_written=proposals_written,
                raw_top_n_count=raw_top_n_count,
                diversified_top_n_count=diversified_top_n_count,
                hard_cap_rejections=hard_cap_rejections,
            ),
        )

    def _failed(
        self,
        run_id: str,
        db_role: str,
        signal_iso: str,
        strategy_config_id: str,
        message: str,
        *,
        analyses_read: int = 0,
    ) -> ServiceResult:
        """Build a ``failed`` result with zero write counts and exact keys.

        On guard / config failure (before DB access) ``analyses_read`` is ``0``;
        on a write failure the caller passes the real ``analyses_read`` while all
        write-derived counts stay ``0`` (rollback leaves no partial rows).
        """
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._metadata(
                db_role=db_role,
                signal_date=signal_iso,
                strategy_config_id=strategy_config_id,
                run_id=run_id,
                analyses_read=analyses_read,
                proposals_written=0,
                raw_top_n_count=0,
                diversified_top_n_count=0,
                hard_cap_rejections=0,
            ),
        )

    @staticmethod
    def _metadata(
        *,
        db_role: str,
        signal_date: str,
        strategy_config_id: str,
        run_id: str,
        analyses_read: int,
        proposals_written: int,
        raw_top_n_count: int,
        diversified_top_n_count: int,
        hard_cap_rejections: int,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "db_role": db_role,
            "signal_date": signal_date,
            "strategy_config_id": strategy_config_id,
            "run_id": run_id,
            "analyses_read": analyses_read,
            "proposals_written": proposals_written,
            "raw_top_n_count": raw_top_n_count,
            "diversified_top_n_count": diversified_top_n_count,
            "hard_cap_rejections": hard_cap_rejections,
        }


__all__ = [
    "Step5ProposalEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "UNKNOWN_SECTOR",
    "UNKNOWN_INDUSTRY",
    "DEFAULT_TIMING_SCORE",
    "REJECT_SECTOR_CAP",
    "REJECT_INDUSTRY_CAP",
]
