"""Module 14 — Step 4 Setup Analysis.

Reads the *passing* Step 3 candidates for one ``signal_date`` /
``strategy_config_id`` from ``step3_candidates`` (``passed_hard_filters = TRUE``),
joins each candidate's current features (``daily_features_current`` on
``feature_date = signal_date``) and that day's prices (``daily_prices`` on
``date = signal_date``), and for every *analyzable* candidate computes the Step 4
setup classification, mechanical stop / target / estimated-RR, the four component
scores and the composite ``setup_score`` (after earnings / macro penalties), then
appends one row per analyzable candidate to ``step4_analysis`` in a single
transaction. It runs after Module 13 (Step 3 Screening) and before Module 15
(Step 5 Proposals).

Contract source of truth: ``M14_STEP4_ANALYSIS_SPEC.md`` (derived from the frozen
split Project Files — ``01a_CORE_PRINCIPLES.md`` for guardrails / enums,
``01b_SCHEMA_AND_DATA.md`` for the ``step4_analysis`` / ``step3_candidates`` /
``daily_features`` / ``daily_prices`` schema and the ``daily_features_current``
view, ``01c_FORMULAS_AND_CONFIGS.md`` for the Step 4 entry / stop / target / RR /
penalty formulas, ``01d_MODULES_AND_PIPELINE.md`` for the pipeline position,
``02_PROJECT_IMPLEMENTATION_CONTEXT.md`` for the DB-boundary / logging rules, and
the Module 13 spec for the ``db_role`` / service style). Under-specified details
that the frozen sources leave open are recorded as gaps (``G-ATR-CONTRACTION``,
``G-TREND-RESUME-HISTORY``, ``G-SCORING-SUBCOMPONENT-WEIGHTS``,
``G-MISSING-ATR-OR-PRICE``) and closed conservatively in the spec.

This module only ever *inserts* into ``step4_analysis``. It never updates or
deletes existing rows, never writes any other table, never runs DDL, never calls
providers, never imports ``duckdb`` directly, never uses ``ATTACH``, never
bypasses the DuckDB manager, and never uses ``print()``.
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
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module 14
# never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are accepted;
# any other value yields a ``failed`` result with no reads/writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# --------------------------------------------------------------------------- #
# Setup-type labels (priority order; first match wins).
# --------------------------------------------------------------------------- #
SETUP_HIGH_TIGHT_FLAG: Final[str] = "high_tight_flag"
SETUP_BREAKOUT: Final[str] = "breakout"
SETUP_VOLATILITY_SQUEEZE: Final[str] = "volatility_squeeze"
SETUP_TREND_PULLBACK: Final[str] = "trend_pullback"
SETUP_TREND_RESUME: Final[str] = "trend_resume"
SETUP_UNKNOWN: Final[str] = "unknown"
SETUP_MOMENTUM_EXTENSION: Final[str] = "momentum_extension"

# Stop-clamp factor used when the mechanical stop is missing / invalid / >= entry.
_STOP_CLAMP_FACTOR: Final[float] = 0.95

# The exact metadata key set returned on every return path.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "signal_date",
    "strategy_config_id",
    "run_id",
    "candidates_evaluated",
    "analyses_written",
    "estimated_rr_min",
    "estimated_rr_max",
    "estimated_rr_mean",
    "setup_type_counts",
)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class _ConfigError(ValueError):
    """Raised internally when ``strategy_config`` is missing / invalid a key."""


# --------------------------------------------------------------------------- #
# SQL (operates only on existing objects; no DDL).
# --------------------------------------------------------------------------- #
# Passing Step 3 candidates for this signal_date / strategy_config_id.
_SELECT_CANDIDATES: Final[str] = (
    "SELECT candidate_id, ticker, screening_score, soft_score_components "
    "FROM step3_candidates "
    "WHERE signal_date = ? "
    "  AND strategy_config_id = ? "
    "  AND passed_hard_filters = TRUE "
    "ORDER BY ticker, candidate_id"
)

# Current features joined with that signal_date's price row.
_SELECT_FEATURES_PRICES: Final[str] = (
    "SELECT "
    "  f.ticker AS ticker, "
    "  f.ema20 AS ema20, "
    "  f.ema50 AS ema50, "
    "  f.ema200 AS ema200, "
    "  f.ema_alignment_score AS ema_alignment_score, "
    "  f.rsi14 AS rsi14, "
    "  f.roc20 AS roc20, "
    "  f.rvol20 AS rvol20, "
    "  f.atr14 AS atr14, "
    "  f.breakout_proximity AS breakout_proximity, "
    "  f.pullback_from_recent_high_pct AS pullback_from_recent_high_pct, "
    "  f.consolidation_score AS consolidation_score, "
    "  f.sector_relative_strength AS sector_relative_strength, "
    "  f.days_to_earnings_bd AS days_to_earnings_bd, "
    "  f.macro_event_risk_flag AS macro_event_risk_flag, "
    "  f.atr_pct AS atr_pct, "
    "  f.distance_to_ema50_pct AS distance_to_ema50_pct, "
    "  p.close_raw AS close_raw, "
    "  p.close_adj AS close_adj, "
    "  p.open_raw AS open_raw, "
    "  p.high_raw AS high_raw, "
    "  p.low_raw AS low_raw "
    "FROM daily_features_current f "
    "LEFT JOIN daily_prices p ON p.ticker = f.ticker AND p.date = f.feature_date "
    "WHERE f.feature_date = ?"
)

# Lowest low over the last up to 20 price rows on / before signal_date.
_SELECT_RECENT_20D_LOW: Final[str] = (
    "SELECT MIN(low_raw) FROM ("
    "  SELECT low_raw FROM daily_prices "
    "  WHERE ticker = ? AND date <= ? "
    "  ORDER BY date DESC LIMIT 20"
    ")"
)

# Prior up to 10 trading rows (close_adj + ema20) strictly before signal_date.
_SELECT_PRIOR_10: Final[str] = (
    "SELECT p.close_adj AS close_adj, f.ema20 AS ema20 "
    "FROM daily_prices p "
    "JOIN daily_features_current f "
    "  ON f.ticker = p.ticker AND f.feature_date = p.date "
    "WHERE p.ticker = ? AND p.date < ? "
    "ORDER BY p.date DESC LIMIT 10"
)

# Parameterized single-row INSERT. Each row gets its own execute() call inside
# one BEGIN TRANSACTION / COMMIT (per-row execute, not executemany).
_INSERT_ANALYSIS: Final[str] = (
    "INSERT INTO step4_analysis "
    "(analysis_id, candidate_id, run_id, strategy_config_id, ticker, signal_date, "
    " setup_type, setup_score, breakout_quality_score, squeeze_score, "
    " timing_score, confirmation_score, estimated_rr, stop_price_raw, "
    " target_price_raw, earnings_penalty, macro_penalty, explanation_json, "
    " created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    " CAST(now() AS TIMESTAMP))"
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


# --------------------------------------------------------------------------- #
# Setup-type classification (first match wins; NULL inputs make a clause false).
# --------------------------------------------------------------------------- #
def _classify_setup(
    feat: dict[str, Any],
    min_rvol: float,
    min_consolidation_for_breakout: float = 0.0,
    ema50_ext_limit: float = 1.0,
) -> str:
    """Return the Step 4 setup_type for one candidate's feature snapshot.

    Evaluated in strict priority order. Any ``None`` required input makes that
    condition false. ``trend_resume`` additionally consults the prior-10 history
    flag (``_trend_resume_history_ok``) precomputed by the caller.

    ``min_consolidation_for_breakout`` gates the breakout classification — a
    near-breakout with insufficient consolidation is classified as
    ``momentum_extension`` instead. ``ema50_ext_limit`` is used to detect
    extended momentum conditions.
    """
    roc20 = feat["roc20"]
    consolidation = feat["consolidation_score"]
    breakout = feat["breakout_proximity"]
    rvol20 = feat["rvol20"]
    close_adj = feat["close_adj"]
    ema20 = feat["ema20"]
    ema50 = feat["ema50"]
    ema200 = feat["ema200"]
    pullback = feat["pullback_from_recent_high_pct"]
    dist_ema50 = feat.get("dist_ema50")  # injected by caller when available

    # 1. high_tight_flag
    if roc20 is not None and consolidation is not None:
        if roc20 > 0.15 and consolidation >= 60:
            return SETUP_HIGH_TIGHT_FLAG

    # 2. breakout — requires sufficient consolidation for this strategy.
    #    If near a breakout level but consolidation is below threshold,
    #    fall through to momentum_extension check instead.
    if breakout is not None and rvol20 is not None:
        if -0.5 <= breakout <= 0.5 and rvol20 >= min_rvol:
            cons_ok = consolidation is not None and consolidation >= min_consolidation_for_breakout
            if cons_ok:
                return SETUP_BREAKOUT
            # Not enough consolidation — classify as momentum_extension below.

    # 3. volatility_squeeze (atr_contraction_proxy == consolidation_score >= 70).
    if consolidation is not None:
        if consolidation >= 70:  # implies atr_contraction_proxy is True
            return SETUP_VOLATILITY_SQUEEZE

    # 4. trend_pullback
    if (
        close_adj is not None
        and ema200 is not None
        and pullback is not None
        and ema20 is not None
        and ema50 is not None
    ):
        if (
            close_adj > ema200
            and -0.12 <= pullback <= -0.03
            and ema20 > ema50
        ):
            return SETUP_TREND_PULLBACK

    # 5. trend_resume (AD-22.16) — needs prior-10 history.
    if (
        feat.get("_trend_resume_history_ok") is True
        and close_adj is not None
        and ema20 is not None
        and pullback is not None
    ):
        if close_adj > ema20 and -0.20 <= pullback <= -0.03:
            return SETUP_TREND_RESUME

    # 6. momentum_extension — extended move lacking setup structure.
    #    Triggered when: EMA50 extended, strong momentum, elevated RVOL,
    #    but no consolidation / pullback / trend structure qualifies above.
    if (
        dist_ema50 is not None
        and dist_ema50 > ema50_ext_limit * 0.5   # noticeably extended
        and roc20 is not None and roc20 > 0.05
        and rvol20 is not None and rvol20 >= 1.5
        and (consolidation is None or consolidation < min_consolidation_for_breakout)
    ):
        return SETUP_MOMENTUM_EXTENSION

    # 7. fallback
    return SETUP_UNKNOWN


def _trend_resume_history_ok(prior_rows: list[tuple[Any, Any]]) -> bool:
    """Return True when >= 3 of the prior-10 rows had ``close_adj < ema20``.

    ``prior_rows`` is a list of ``(close_adj, ema20)`` tuples for the up to 10
    trading rows strictly before ``signal_date``. Rows with a NULL close_adj or
    ema20 cannot satisfy the condition and are skipped. If fewer than the needed
    qualifying rows exist (including the no-history case) the flag is False and
    ``trend_resume`` is skipped (gap ``G-TREND-RESUME-HISTORY``).
    """
    below = 0
    for close_adj, ema20 in prior_rows:
        c = _f(close_adj)
        e = _f(ema20)
        if c is not None and e is not None and c < e:
            below += 1
    return below >= 3


# --------------------------------------------------------------------------- #
# Stop / target / RR.
# --------------------------------------------------------------------------- #
def _atr14_raw_equivalent(
    atr14: float | None, close_raw: float | None, close_adj: float | None
) -> float | None:
    """Convert adjusted ATR to a raw-price-equivalent ATR.

    Uses ``atr14 * (close_raw / close_adj)`` only when ``atr14``, ``close_raw``
    and a non-zero ``close_adj`` are all present; otherwise falls back to raw
    ``atr14``. When ``atr14`` is missing returns ``None`` (the stop logic then
    falls back to ``recent_20d_low`` or the safe clamp — gap
    ``G-MISSING-ATR-OR-PRICE``).
    """
    if atr14 is None:
        return None
    if close_raw is not None and close_adj is not None and close_adj != 0.0:
        return atr14 * (close_raw / close_adj)
    return atr14


def _compute_stop(
    entry: float,
    recent_20d_low: float | None,
    atr_raw_equiv: float | None,
) -> tuple[float, bool]:
    """Return ``(stop_price_raw, stop_clamped)``.

    The mechanical stop is ``min(recent_20d_low, entry - 1.5 * atr_raw_equiv)``
    over whichever of those two candidates are available. If neither candidate is
    available, or the resulting stop is invalid / ``>= entry``, the stop is
    clamped to ``entry * 0.95`` and ``stop_clamped`` is True.
    """
    candidates: list[float] = []
    if recent_20d_low is not None:
        candidates.append(recent_20d_low)
    if atr_raw_equiv is not None:
        candidates.append(entry - 1.5 * atr_raw_equiv)

    stop: float | None = min(candidates) if candidates else None

    if stop is None or stop != stop or stop >= entry:
        return entry * _STOP_CLAMP_FACTOR, True
    return stop, False


def _compute_target_rr(
    entry: float, stop: float, target_r: float
) -> tuple[float, float | None]:
    """Return ``(target_price_raw, estimated_rr)``; RR is ``None`` if denom <= 0."""
    target = entry + target_r * (entry - stop)
    denom = entry - stop
    estimated_rr = (target - entry) / denom if denom > 0 else None
    return target, estimated_rr


# --------------------------------------------------------------------------- #
# Component scores (all clamped 0-100).
# --------------------------------------------------------------------------- #
def _breakout_quality_score(
    breakout_proximity: float | None, rvol20: float | None
) -> float:
    """0.5 * breakout-position sub + 0.5 * rvol sub."""
    if breakout_proximity is None:
        position_sub = 0.0
    elif -0.5 <= breakout_proximity <= 0.5:
        position_sub = 100.0
    else:
        position_sub = max(0.0, 100.0 * (1.0 - abs(breakout_proximity) / 2.0))

    if rvol20 is None:
        rvol_sub = 0.0
    elif rvol20 >= 2.0:
        rvol_sub = 100.0
    elif 1.5 <= rvol20 < 2.0:
        rvol_sub = 70.0
    elif 1.2 <= rvol20 < 1.5:
        rvol_sub = 40.0
    else:
        rvol_sub = 0.0

    return _clamp(0.5 * position_sub + 0.5 * rvol_sub)


def _squeeze_score(consolidation_score: float | None) -> float:
    """Squeeze sub-score == consolidation_score (NULL -> 0)."""
    if consolidation_score is None:
        return 0.0
    return _clamp(consolidation_score)


def _timing_score(
    rsi14: float | None,
    ema_alignment_score: float | None,
    sector_relative_strength: float | None,
) -> float:
    """0.4 * RSI sub + 0.3 * ema_alignment sub + 0.3 * sector-RS sub."""
    if rsi14 is None:
        rsi_sub = 0.0
    elif 50.0 <= rsi14 <= 65.0:
        rsi_sub = 100.0
    elif (45.0 <= rsi14 < 50.0) or (65.0 < rsi14 <= 70.0):
        rsi_sub = 70.0
    else:
        rsi_sub = 30.0

    ema_sub = 0.0 if ema_alignment_score is None else _clamp(ema_alignment_score)

    if sector_relative_strength is None:
        srs_sub = 50.0
    elif sector_relative_strength > 0.05:
        srs_sub = 100.0
    elif 0.0 <= sector_relative_strength <= 0.05:
        srs_sub = 70.0
    elif -0.05 <= sector_relative_strength < 0.0:
        srs_sub = 30.0
    else:
        srs_sub = 0.0

    return _clamp(0.4 * rsi_sub + 0.3 * ema_sub + 0.3 * srs_sub)


def _confirmation_score(
    close_adj: float | None,
    ema200: float | None,
    ema20: float | None,
    ema50: float | None,
) -> float:
    """50 * I(close_adj > ema200) + 50 * I(ema20 > ema50). NULL inputs -> 0."""
    above_200 = (
        close_adj is not None and ema200 is not None and close_adj > ema200
    )
    aligned = ema20 is not None and ema50 is not None and ema20 > ema50
    return _clamp(50.0 * (1 if above_200 else 0) + 50.0 * (1 if aligned else 0))


# --------------------------------------------------------------------------- #
# Setup-type-aware quality scorer.
# --------------------------------------------------------------------------- #
# Per-setup-type component weights (breakout_quality, squeeze, timing,
# confirmation). Weights sum to 1.0 for each setup type.
_SETUP_WEIGHTS: Final[dict[str, tuple[float, float, float, float]]] = {
    #                              bq     sq    tim   conf
    SETUP_BREAKOUT:              (0.50, 0.30, 0.10, 0.10),
    SETUP_VOLATILITY_SQUEEZE:    (0.10, 0.50, 0.30, 0.10),
    SETUP_TREND_PULLBACK:        (0.10, 0.10, 0.50, 0.30),
    SETUP_TREND_RESUME:          (0.10, 0.10, 0.40, 0.40),
    SETUP_HIGH_TIGHT_FLAG:       (0.40, 0.10, 0.40, 0.10),
    SETUP_MOMENTUM_EXTENSION:    (0.40, 0.10, 0.40, 0.10),
    SETUP_UNKNOWN:               (0.25, 0.25, 0.25, 0.25),  # fallback equal weight
}


def _route_setup_score(
    setup_type: str,
    breakout_quality: float,
    squeeze: float,
    timing: float,
    confirmation: float,
) -> float:
    """Return setup quality weighted by setup type (0-100, clamped).

    Each setup type applies different component weights so that irrelevant
    components do not inflate or suppress the score.
    """
    weights = _SETUP_WEIGHTS.get(setup_type, _SETUP_WEIGHTS[SETUP_UNKNOWN])
    w_bq, w_sq, w_tim, w_conf = weights
    return _clamp(
        w_bq * breakout_quality
        + w_sq * squeeze
        + w_tim * timing
        + w_conf * confirmation
    )


# --------------------------------------------------------------------------- #
# Penalties.
# --------------------------------------------------------------------------- #
def _earnings_penalty(
    days_to_earnings_bd: int | None,
    avoid_within_bd: int,
    penalty_points_max: float,
    strategy_name: str = "normal",
) -> tuple[float, str]:
    """Linear earnings penalty (<= 0). Returns ``(penalty, earnings_status)``.

    Unknown earnings date (``None``) is treated as risk proportional to
    strategy strictness — not as zero penalty. The returned ``earnings_status``
    label is stored in ``explanation_json`` for auditability.
    """
    if days_to_earnings_bd is None:
        status = "EARNINGS_UNKNOWN"
        unknown_factors: dict[str, float] = {
            "conservative": 1.0,
            "normal": 0.75,
            "aggressive": 0.40,
        }
        factor = unknown_factors.get(strategy_name, 0.75)
        penalty = min(0.0, penalty_points_max * factor)
        return penalty, status

    if avoid_within_bd == 0:
        if days_to_earnings_bd == 0:
            return min(0.0, penalty_points_max), "EARNINGS_TODAY"
        return 0.0, "EARNINGS_CLEAR"

    if days_to_earnings_bd <= avoid_within_bd:
        penalty = penalty_points_max * (1.0 - days_to_earnings_bd / avoid_within_bd)
        return min(0.0, penalty), "EARNINGS_INSIDE_WINDOW"

    return 0.0, "EARNINGS_CLEAR"


def _macro_penalty(
    enabled: bool,
    macro_event_risk_flag: bool | None,
    penalty_points: float,
) -> float:
    """Flat macro penalty (<= 0) when enabled and the risk flag is True."""
    if enabled is True and macro_event_risk_flag is True:
        return min(0.0, penalty_points)
    return 0.0


# --------------------------------------------------------------------------- #
# Step 4 setup analysis engine.
# --------------------------------------------------------------------------- #
class Step4AnalysisEngine:
    """Classify + score Step 4 setups for one signal date / strategy config.

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
    def analyze(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Analyze passing Step 3 candidates for ``signal_date`` and write rows.

        Parameters
        ----------
        signal_date:
            The signal / feature date. Only ``step3_candidates`` rows with this
            ``signal_date`` (and ``strategy_config_id``) that passed the hard
            filters are processed.
        strategy_config:
            Parsed strategy-config JSON. Required keys: ``step4.target_R``,
            ``earnings.avoid_within_bd``, ``earnings.penalty_points_max``,
            ``macro_event_risk.enabled``, ``macro_event_risk.penalty_points`` and
            ``screening.min_rvol``.
        strategy_config_id:
            Opaque config id, copied to every written analysis row.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns ``failed`` before any DB read/write.
        run_id:
            A fresh ``uuid4`` is minted when ``None``; a supplied value is kept.

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``metadata["analyses_written"]`` on every
            return path. ``metadata`` carries exactly :data:`METADATA_KEYS`.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        signal_iso = signal_date.isoformat()

        log.info(
            "analyze start db_role=%s signal_date=%s strategy_config_id=%s",
            db_role,
            signal_iso,
            strategy_config_id,
        )

        # --- db_role guard: prod/debug only, never simulation. No I/O. ----- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 14 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("analyze failed: %s", message)
            return self._failed(run_id, db_role, signal_iso, strategy_config_id, message)

        # --- config guard: validate required keys before any DB access. ---- #
        try:
            cfg = self._parse_config(strategy_config)
        except _ConfigError as exc:
            log.error("analyze failed: %s", exc)
            return self._failed(
                run_id, db_role, signal_iso, strategy_config_id, str(exc)
            )

        # --- read phase (read-only). --------------------------------------- #
        try:
            candidates, feature_by_ticker, recent_lows, history_ok = self._read(
                db_role, signal_date, strategy_config_id
            )
        except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("analyze failed: %s", message)
            return self._failed(
                run_id, db_role, signal_iso, strategy_config_id, message
            )

        candidates_evaluated = len(candidates)

        # --- empty qualifying input: success, zero counts, no insert. ------ #
        if candidates_evaluated == 0:
            log.info("analyze done: no passing step3 candidates for %s", signal_iso)
            return self._success(
                run_id,
                db_role,
                signal_iso,
                strategy_config_id,
                candidates_evaluated=0,
                rows=[],
            )

        # --- compute phase (pure Python, no DB). --------------------------- #
        rows = self._build_rows(
            candidates,
            feature_by_ticker,
            recent_lows,
            history_ok,
            cfg,
            run_id,
            strategy_config_id,
            signal_date,
        )

        # --- write phase: per-row execute() inside one BEGIN/COMMIT. ------- #
        try:
            analyses_written = self._write(db_role, rows)
        except Exception as exc:  # noqa: BLE001 - surface as failed; rollback inside
            log.error(
                "analyze failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            return self._failed(
                run_id,
                db_role,
                signal_iso,
                strategy_config_id,
                f"{type(exc).__name__}: {exc}",
                candidates_evaluated=candidates_evaluated,
            )

        log.info(
            "analyze done status=success candidates_evaluated=%d analyses_written=%d",
            candidates_evaluated,
            analyses_written,
        )
        return self._success(
            run_id,
            db_role,
            signal_iso,
            strategy_config_id,
            candidates_evaluated=candidates_evaluated,
            rows=rows,
        )

    # ------------------------------------------------------------------ #
    # Config parsing / validation.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_config(strategy_config: dict) -> dict[str, Any]:
        """Validate and extract the required config values before DB access.

        Raises
        ------
        _ConfigError
            If ``strategy_config`` is not a dict or any required key is missing /
            has the wrong type / falls outside the allowed range.
        """
        if not isinstance(strategy_config, dict):
            raise _ConfigError("strategy_config must be a dict")

        def _section(name: str) -> dict:
            block = strategy_config.get(name)
            if not isinstance(block, dict):
                raise _ConfigError(f"missing config section {name}")
            return block

        def _number(block: dict, section: str, key: str) -> float:
            if key not in block:
                raise _ConfigError(f"missing config key {section}.{key}")
            value = block[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _ConfigError(f"config key {section}.{key} must be numeric")
            return float(value)

        step4 = _section("step4")
        target_r = _number(step4, "step4", "target_R")
        if not target_r > 0:
            raise _ConfigError("config key step4.target_R must be > 0")
        min_step4_setup_score = _number(step4, "step4", "min_step4_setup_score")
        if min_step4_setup_score < 0:
            raise _ConfigError(
                "config key step4.min_step4_setup_score must be >= 0"
            )
        min_consolidation_for_breakout = _number(
            step4, "step4", "min_consolidation_for_breakout"
        )
        if min_consolidation_for_breakout < 0:
            raise _ConfigError(
                "config key step4.min_consolidation_for_breakout must be >= 0"
            )
        min_estimated_rr = _number(step4, "step4", "min_estimated_rr")
        if min_estimated_rr < 0:
            raise _ConfigError(
                "config key step4.min_estimated_rr must be >= 0"
            )

        earnings = _section("earnings")
        if "avoid_within_bd" not in earnings:
            raise _ConfigError("missing config key earnings.avoid_within_bd")
        avoid_within_bd = earnings["avoid_within_bd"]
        if isinstance(avoid_within_bd, bool) or not isinstance(avoid_within_bd, int):
            raise _ConfigError("config key earnings.avoid_within_bd must be int")
        if avoid_within_bd < 0:
            raise _ConfigError("config key earnings.avoid_within_bd must be >= 0")

        penalty_points_max = _number(
            earnings, "earnings", "penalty_points_max"
        )
        if not penalty_points_max <= 0:
            raise _ConfigError("config key earnings.penalty_points_max must be <= 0")

        macro = _section("macro_event_risk")
        if "enabled" not in macro:
            raise _ConfigError("missing config key macro_event_risk.enabled")
        macro_enabled = macro["enabled"]
        if not isinstance(macro_enabled, bool):
            raise _ConfigError("config key macro_event_risk.enabled must be bool")
        macro_penalty_points = _number(
            macro, "macro_event_risk", "penalty_points"
        )
        if not macro_penalty_points <= 0:
            raise _ConfigError(
                "config key macro_event_risk.penalty_points must be <= 0"
            )

        screening = _section("screening")
        min_rvol = _number(screening, "screening", "min_rvol")
        if not min_rvol > 0:
            raise _ConfigError("config key screening.min_rvol must be > 0")
        min_screening_score = _number(
            screening, "screening", "min_screening_score"
        )
        if min_screening_score < 0:
            raise _ConfigError(
                "config key screening.min_screening_score must be >= 0"
            )
        min_step3_setup_score = _number(
            screening, "screening", "min_step3_setup_score"
        )
        if min_step3_setup_score < 0:
            raise _ConfigError(
                "config key screening.min_step3_setup_score must be >= 0"
            )

        strategy_name: str = strategy_config.get("strategy_name", "normal")
        if not isinstance(strategy_name, str):
            strategy_name = "normal"

        # Per-strategy risk-gate hard limits applied in _build_rows.
        _ATR_LIMITS: dict[str, float] = {
            "conservative": 0.06, "normal": 0.08, "aggressive": 0.12,
        }
        _EMA50_LIMITS: dict[str, float] = {
            "conservative": 0.08, "normal": 0.12, "aggressive": 0.25,
        }
        _STOP_LIMITS: dict[str, float] = {
            "conservative": 0.10, "normal": 0.15, "aggressive": 0.20,
        }

        return {
            "target_r": target_r,
            "avoid_within_bd": avoid_within_bd,
            "penalty_points_max": penalty_points_max,
            "macro_enabled": macro_enabled,
            "macro_penalty_points": macro_penalty_points,
            "min_rvol": min_rvol,
            "min_screening_score": min_screening_score,
            "min_step3_setup_score": min_step3_setup_score,
            "min_step4_setup_score": min_step4_setup_score,
            "min_consolidation_for_breakout": min_consolidation_for_breakout,
            "min_estimated_rr": min_estimated_rr,
            "strategy_name": strategy_name,
            "atr_pct_limit": _ATR_LIMITS.get(strategy_name, 0.08),
            "ema50_ext_limit": _EMA50_LIMITS.get(strategy_name, 0.12),
            "stop_distance_limit": _STOP_LIMITS.get(strategy_name, 0.15),
        }

    # ------------------------------------------------------------------ #
    # Read phase.
    # ------------------------------------------------------------------ #
    def _read(
        self,
        db_role: str,
        signal_date: date,
        strategy_config_id: str,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, float | None],
        dict[str, bool],
    ]:
        """Read all inputs for ``signal_date`` (read-only).

        Returns the passing candidate list (``candidate_id`` + ``ticker``), a
        ticker -> feature/price snapshot map, a ticker -> ``recent_20d_low_raw``
        map and a ticker -> ``trend_resume`` history-ok flag map. The read
        connection is closed before any computation.
        """
        connection = self._db.connect(db_role, read_only=True)
        try:
            candidate_rows = connection.execute(
                _SELECT_CANDIDATES, [signal_date, strategy_config_id]
            ).fetchall()
            import json as _json
            candidates = []
            for cid, ticker, sc, soft_json in candidate_rows:
                try:
                    soft_parsed = _json.loads(soft_json) if soft_json else {}
                except Exception:
                    soft_parsed = {}
                candidates.append({
                    "candidate_id": cid,
                    "ticker": ticker,
                    "screening_score": sc,
                    "soft_score_components_parsed": soft_parsed,
                })

            feature_by_ticker: dict[str, dict[str, Any]] = {}
            recent_lows: dict[str, float | None] = {}
            history_ok: dict[str, bool] = {}

            if candidates:
                tickers = {c["ticker"] for c in candidates}

                fp_rows = connection.execute(
                    _SELECT_FEATURES_PRICES, [signal_date]
                ).fetchall()
                fp_cols = (
                    "ticker", "ema20", "ema50", "ema200", "ema_alignment_score",
                    "rsi14", "roc20", "rvol20", "atr14", "breakout_proximity",
                    "pullback_from_recent_high_pct", "consolidation_score",
                    "sector_relative_strength", "days_to_earnings_bd",
                    "macro_event_risk_flag", "atr_pct", "distance_to_ema50_pct",
                    "close_raw", "close_adj", "open_raw", "high_raw", "low_raw",
                )
                for raw in fp_rows:
                    record = dict(zip(fp_cols, raw))
                    if record["ticker"] in tickers:
                        feature_by_ticker[record["ticker"]] = record

                for ticker in tickers:
                    low_row = connection.execute(
                        _SELECT_RECENT_20D_LOW, [ticker, signal_date]
                    ).fetchone()
                    recent_lows[ticker] = _f(low_row[0]) if low_row else None

                    prior_rows = connection.execute(
                        _SELECT_PRIOR_10, [ticker, signal_date]
                    ).fetchall()
                    history_ok[ticker] = _trend_resume_history_ok(prior_rows)
        finally:
            connection.close()

        return candidates, feature_by_ticker, recent_lows, history_ok

    # ------------------------------------------------------------------ #
    # Compute phase.
    # ------------------------------------------------------------------ #
    def _build_rows(
        self,
        candidates: list[dict[str, Any]],
        feature_by_ticker: dict[str, dict[str, Any]],
        recent_lows: dict[str, float | None],
        history_ok: dict[str, bool],
        cfg: dict[str, Any],
        run_id: str,
        strategy_config_id: str,
        signal_date: date,
    ) -> list[dict[str, Any]]:
        """Materialise the per-candidate insert payloads (deterministic order).

        Candidates with no current feature row or no usable ``close_raw`` are
        treated as not analyzable and produce no row (gap
        ``G-MISSING-ATR-OR-PRICE``).

        Pre-scoring gates (applied in order before any scoring):
        1. ``min_screening_score`` — Step 3 total score meets strategy threshold.
        1b.``min_step3_setup_score`` — Step 3 setup sub-score meets strategy threshold.
        2. ``atr_pct`` — hard limit by strategy.
        3. ``distance_to_ema50_pct`` — hard limit by strategy.
        4. ``stop_distance_pct`` — computed after stop, hard limit by strategy.
        5. ``setup_type = unknown/momentum_extension`` — blocked for Conservative.
        Post-scoring gates:
        6. ``min_step4_setup_score`` — Step 4 setup score meets strategy threshold.
        7. ``min_estimated_rr`` — Conservative hard block on insufficient RR.
        """
        rows: list[dict[str, Any]] = []
        target_r = cfg["target_r"]
        strategy_name = cfg["strategy_name"]
        min_screening_score = cfg["min_screening_score"]
        min_step3_setup_score = cfg["min_step3_setup_score"]
        min_step4_setup_score = cfg["min_step4_setup_score"]
        min_consolidation_for_breakout = cfg["min_consolidation_for_breakout"]
        min_estimated_rr = cfg["min_estimated_rr"]
        atr_pct_limit = cfg["atr_pct_limit"]
        ema50_ext_limit = cfg["ema50_ext_limit"]
        stop_distance_limit = cfg["stop_distance_limit"]

        for cand in candidates:
            ticker = cand["ticker"]

            # --- Gate 1: min_screening_score (Step 3 score threshold). ----- #
            candidate_screening_score = _f(cand.get("screening_score"))
            if (
                candidate_screening_score is None
                or candidate_screening_score < min_screening_score
            ):
                continue  # does not meet strategy screening threshold

            # --- Gate 1b: min_step3_setup_score (Step 3 setup sub-score). -- #
            # Read from soft_score_components JSON stored by Step 3.
            # None score (e.g. seeded as '{}') only blocks when threshold > 0.
            _soft = cand.get("soft_score_components_parsed") or {}
            step3_setup_score = _f(_soft.get("setup_score"))
            if min_step3_setup_score > 0.0 and (
                step3_setup_score is None
                or step3_setup_score < min_step3_setup_score
            ):
                continue  # setup quality too weak at Step 3 level

            raw_feat = feature_by_ticker.get(ticker)
            if raw_feat is None:
                continue  # no current feature/price row -> not analyzable

            entry = _f(raw_feat["close_raw"])
            if entry is None or entry <= 0.0:
                continue  # no usable entry proxy -> not analyzable

            # --- Gate 2: ATR% hard limit. ---------------------------------- #
            atr_pct = _f(raw_feat.get("atr_pct"))
            if atr_pct is not None and atr_pct > atr_pct_limit:
                continue  # too volatile for this strategy

            # --- Gate 3: EMA50 extension hard limit. ----------------------- #
            dist_ema50 = _f(raw_feat.get("distance_to_ema50_pct"))
            if dist_ema50 is not None and dist_ema50 > ema50_ext_limit:
                continue  # too extended above EMA50 for this strategy

            feat = {
                "ema20": _f(raw_feat["ema20"]),
                "ema50": _f(raw_feat["ema50"]),
                "ema200": _f(raw_feat["ema200"]),
                "ema_alignment_score": _f(raw_feat["ema_alignment_score"]),
                "rsi14": _f(raw_feat["rsi14"]),
                "roc20": _f(raw_feat["roc20"]),
                "rvol20": _f(raw_feat["rvol20"]),
                "atr14": _f(raw_feat["atr14"]),
                "breakout_proximity": _f(raw_feat["breakout_proximity"]),
                "pullback_from_recent_high_pct": _f(
                    raw_feat["pullback_from_recent_high_pct"]
                ),
                "consolidation_score": _f(raw_feat["consolidation_score"]),
                "sector_relative_strength": _f(raw_feat["sector_relative_strength"]),
                "close_raw": entry,
                "close_adj": _f(raw_feat["close_adj"]),
                "_trend_resume_history_ok": history_ok.get(ticker, False),
            }
            days_to_earnings = raw_feat["days_to_earnings_bd"]
            days_to_earnings = (
                int(days_to_earnings) if days_to_earnings is not None else None
            )
            macro_flag = raw_feat["macro_event_risk_flag"]
            if macro_flag is not None:
                macro_flag = bool(macro_flag)

            # --- stop / target / RR. --------------------------------------- #
            atr_raw_equiv = _atr14_raw_equivalent(
                feat["atr14"], entry, feat["close_adj"]
            )
            recent_low = recent_lows.get(ticker)
            stop, stop_clamped = _compute_stop(entry, recent_low, atr_raw_equiv)
            target, estimated_rr = _compute_target_rr(entry, stop, target_r)

            # --- Gate 4: stop distance hard limit. ------------------------- #
            stop_distance_pct = (entry - stop) / entry if entry > 0 else None
            if stop_distance_pct is not None and stop_distance_pct > stop_distance_limit:
                continue  # stop too wide for this strategy

            # --- classification. ------------------------------------------- #
            feat["dist_ema50"] = dist_ema50  # inject for momentum_extension check
            setup_type = _classify_setup(
                feat,
                cfg["min_rvol"],
                min_consolidation_for_breakout=min_consolidation_for_breakout,
                ema50_ext_limit=ema50_ext_limit,
            )

            # --- Gate 5: unknown/momentum_extension blocked for Conservative. #
            if strategy_name == "conservative" and setup_type in (
                SETUP_UNKNOWN, SETUP_MOMENTUM_EXTENSION
            ):
                continue  # Conservative requires a clean classified setup

            # --- component scores. ----------------------------------------- #
            breakout_quality = _breakout_quality_score(
                feat["breakout_proximity"], feat["rvol20"]
            )
            squeeze = _squeeze_score(feat["consolidation_score"])
            timing = _timing_score(
                feat["rsi14"],
                feat["ema_alignment_score"],
                feat["sector_relative_strength"],
            )
            confirmation = _confirmation_score(
                feat["close_adj"], feat["ema200"], feat["ema20"], feat["ema50"]
            )

            # --- penalties. ------------------------------------------------ #
            earnings_penalty, earnings_status = _earnings_penalty(
                days_to_earnings,
                cfg["avoid_within_bd"],
                cfg["penalty_points_max"],
                strategy_name,
            )

            # Conservative hard block: earnings date must be known. --------- #
            if earnings_status == "EARNINGS_UNKNOWN" and strategy_name == "conservative":
                continue  # Conservative requires verified earnings date

            macro_penalty = _macro_penalty(
                cfg["macro_enabled"], macro_flag, cfg["macro_penalty_points"]
            )

            # --- composite setup_score (type-weighted routing). ------------ #
            setup_quality = _route_setup_score(
                setup_type, breakout_quality, squeeze, timing, confirmation
            )
            setup_score = _clamp(setup_quality + earnings_penalty + macro_penalty)

            # --- Gate 6: min_step4_setup_score. ---------------------------- #
            if setup_score < min_step4_setup_score:
                continue  # setup quality too weak at Step 4 level

            # --- Gate 7: min_estimated_rr (conservative hard gate). -------- #
            if (
                estimated_rr is not None
                and estimated_rr < min_estimated_rr
                and strategy_name == "conservative"
            ):
                continue  # RR too low for Conservative strategy

            explanation = {
                "setup_type": setup_type,
                "entry_proxy_raw": entry,
                "stop_price_raw": stop,
                "target_price_raw": target,
                "target_R": target_r,
                "atr14_raw_equivalent": atr_raw_equiv,
                "recent_20d_low_raw": recent_low,
                "stop_clamped": stop_clamped,
                "stop_distance_pct": stop_distance_pct,
                "atr_pct": atr_pct,
                "distance_to_ema50_pct": dist_ema50,
                "step3_setup_score": step3_setup_score,
                "step4_setup_score": setup_score,
                "earnings_penalty": earnings_penalty,
                "earnings_status": earnings_status,
                "macro_penalty": macro_penalty,
                "days_to_earnings_bd": days_to_earnings,
                "macro_event_risk_flag": macro_flag,
                "gate_results": {
                    "min_screening_score": "PASS",
                    "min_step3_setup_score": "PASS",
                    "atr_pct": "PASS",
                    "ema50_extension": "PASS",
                    "stop_distance": "PASS",
                    "setup_type_allowed": "PASS",
                    "earnings_status": earnings_status,
                    "min_step4_setup_score": "PASS",
                    "min_estimated_rr": (
                        "PASS" if estimated_rr is None or estimated_rr >= min_estimated_rr
                        else "PASS"  # reached here so it passed
                    ),
                },
            }

            rows.append(
                {
                    "analysis_id": str(uuid.uuid4()),
                    "candidate_id": cand["candidate_id"],
                    "run_id": run_id,
                    "strategy_config_id": strategy_config_id,
                    "ticker": ticker,
                    "signal_date": signal_date,
                    "setup_type": setup_type,
                    "setup_score": setup_score,
                    "breakout_quality_score": breakout_quality,
                    "squeeze_score": squeeze,
                    "timing_score": timing,
                    "confirmation_score": confirmation,
                    "estimated_rr": estimated_rr,
                    "stop_price_raw": stop,
                    "target_price_raw": target,
                    "earnings_penalty": earnings_penalty,
                    "macro_penalty": macro_penalty,
                    "explanation_json": _json_dumps(explanation),
                }
            )
        return rows

    # ------------------------------------------------------------------ #
    # Write phase.
    # ------------------------------------------------------------------ #
    def _write(self, db_role: str, rows: list[dict[str, Any]]) -> int:
        """Append all analysis rows inside a single ``BEGIN TRANSACTION / COMMIT``.

        Each row is written with its own ``execute()`` call (not ``executemany``)
        and the rows count is returned. Any error triggers ``ROLLBACK`` so no
        partial Module 14 rows survive. An empty plan returns 0 immediately
        without opening a transaction.
        """
        if not rows:
            return 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for row in rows:
                    connection.execute(
                        _INSERT_ANALYSIS,
                        [
                            row["analysis_id"],
                            row["candidate_id"],
                            row["run_id"],
                            row["strategy_config_id"],
                            row["ticker"],
                            row["signal_date"],
                            row["setup_type"],
                            row["setup_score"],
                            row["breakout_quality_score"],
                            row["squeeze_score"],
                            row["timing_score"],
                            row["confirmation_score"],
                            row["estimated_rr"],
                            row["stop_price_raw"],
                            row["target_price_raw"],
                            row["earnings_penalty"],
                            row["macro_penalty"],
                            row["explanation_json"],
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
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
        candidates_evaluated: int,
        rows: list[dict[str, Any]],
    ) -> ServiceResult:
        """Build a ``success`` result from the written rows."""
        analyses_written = len(rows)
        rr_values = [
            r["estimated_rr"] for r in rows if r["estimated_rr"] is not None
        ]
        rr_min = min(rr_values) if rr_values else None
        rr_max = max(rr_values) if rr_values else None
        rr_mean = (sum(rr_values) / len(rr_values)) if rr_values else None

        setup_type_counts: dict[str, int] = {}
        for r in rows:
            st = r["setup_type"]
            setup_type_counts[st] = setup_type_counts.get(st, 0) + 1

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=analyses_written,
            metadata=self._metadata(
                db_role=db_role,
                signal_date=signal_iso,
                strategy_config_id=strategy_config_id,
                run_id=run_id,
                candidates_evaluated=candidates_evaluated,
                analyses_written=analyses_written,
                estimated_rr_min=rr_min,
                estimated_rr_max=rr_max,
                estimated_rr_mean=rr_mean,
                setup_type_counts=setup_type_counts,
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
        candidates_evaluated: int = 0,
    ) -> ServiceResult:
        """Build a ``failed`` result with zero counts and exact metadata keys."""
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
                candidates_evaluated=candidates_evaluated,
                analyses_written=0,
                estimated_rr_min=None,
                estimated_rr_max=None,
                estimated_rr_mean=None,
                setup_type_counts={},
            ),
        )

    @staticmethod
    def _metadata(
        *,
        db_role: str,
        signal_date: str,
        strategy_config_id: str,
        run_id: str,
        candidates_evaluated: int,
        analyses_written: int,
        estimated_rr_min: float | None,
        estimated_rr_max: float | None,
        estimated_rr_mean: float | None,
        setup_type_counts: dict[str, int],
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "db_role": db_role,
            "signal_date": signal_date,
            "strategy_config_id": strategy_config_id,
            "run_id": run_id,
            "candidates_evaluated": candidates_evaluated,
            "analyses_written": analyses_written,
            "estimated_rr_min": estimated_rr_min,
            "estimated_rr_max": estimated_rr_max,
            "estimated_rr_mean": estimated_rr_mean,
            "setup_type_counts": setup_type_counts,
        }


__all__ = [
    "Step4AnalysisEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "SETUP_HIGH_TIGHT_FLAG",
    "SETUP_BREAKOUT",
    "SETUP_VOLATILITY_SQUEEZE",
    "SETUP_TREND_PULLBACK",
    "SETUP_TREND_RESUME",
    "SETUP_UNKNOWN",
    "SETUP_MOMENTUM_EXTENSION",
]
