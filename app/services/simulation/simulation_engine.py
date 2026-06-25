"""Module 17 — Simulation Engine.

`SimulationEngine.run` replays the frozen Step 3 / 4 / 5 pipeline and realized
outcomes over a historical date range into the ``sim_*`` tables of
``simulation.duckdb``. Production data is read **only** through a read-only prod
attach (``duckdb_manager.connect_simulation_with_prod``); the engine never opens
the prod path directly, never writes to prod/debug tables, never calls
providers, never imports ``duckdb`` directly, never runs DDL, and never uses
``print()``.

Three modes are supported:

``research``
    Replay every requested config over the whole ``[start_date, end_date]``
    window (``fold_id = NULL``) and aggregate per-config performance.
``config_comparison``
    Same replay as ``research``; the deliverable is the
    ``sim_config_comparisons`` table (raw + diversified per horizon).
``walk_forward``
    **V1 — Option B (replay-all).** Expanding train window with
    calendar-quarter test folds (12-month minimum initial train). V1 replays
    *every* requested config across the full window. Per fold it selects the
    best config by maximum train-window expectancy subject to
    ``resolved_outcomes_pct >= 0.85`` and ``max_drawdown_pct <= 25``, and
    records that selection in ``sim_folds.selected_config_id`` for
    transparency. V1 does **not** restrict test-fold signal generation to the
    selected config — true selected-config execution is deferred to a future
    module. Outcomes whose 40bd evaluation extends beyond a fold's test period
    are flagged ``cross_fold_outcome = TRUE`` and excluded from that fold's
    training metrics and from run-level ``sim_config_comparisons``.

Step 3 / 4 / 5 replay reuses the **frozen** Module 13–15 scoring code directly
(their pure scoring expressions, classification, stop/target, ranking and
diversification helpers) so simulated scores are identical to production with no
formula divergence; the engine owns only the no-look-ahead sim-scoped reads and
the ``sim_*`` writes. Outcome math reuses the Module 16 entry / return / MFE /
MAE / realized-R rules (FORMULAS §64), adapted to simulation tables with
``entry_price_sim`` as the denominator and a ``list_membership`` label.

Contract source of truth: ``M17_SIMULATION_ENGINE_SPEC.md`` (derived from
``01b_SCHEMA_AND_DATA.md`` / ``M02_SCHEMA_SPEC.md`` §4 for the ``sim_*`` shapes,
``01d_MODULES_AND_PIPELINE.md`` ``SIMULATION/80``/``81``/``82`` for the
simulation flow, no-look-ahead and prod-attach rules,
``01c_FORMULAS_AND_CONFIGS.md`` §61–64 plus frozen Modules 13–16 for the
formulas, ``01a_CORE_PRINCIPLES.md`` for enums, and the frozen Module 16 service
for the ``db_role`` guard, validate-before-IO, read→compute→single-write
transaction style and metadata discipline).
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.config import constants
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

# NOTE: ``polars`` / ``duckdb`` and the frozen Step 3/4/5 engines are imported
# lazily inside the methods that need them (see ``_engines`` / ``_replay_date``).
# Keeping the module top-level import-light lets the pure helpers
# (``plan_walk_forward_folds`` / ``compute_metrics`` / ``select_config_for_fold``)
# and pre-DB validation be unit-tested offline without those heavy dependencies.


# --------------------------------------------------------------------------- #
# Modes / roles.
# --------------------------------------------------------------------------- #
MODE_RESEARCH: Final[str] = "research"
MODE_WALK_FORWARD: Final[str] = "walk_forward"
MODE_CONFIG_COMPARISON: Final[str] = "config_comparison"
ALLOWED_MODES: Final[tuple[str, ...]] = (
    MODE_RESEARCH,
    MODE_WALK_FORWARD,
    MODE_CONFIG_COMPARISON,
)

# Hardcoded to avoid importing ``duckdb_manager`` (and thus ``duckdb``) at module
# import time; the values mirror ``duckdb_manager.DB_ROLE_SIMULATION`` /
# ``DEFAULT_PROD_ALIAS`` and are asserted to match in the test suite.
DB_ROLE_SIMULATION: Final[str] = "simulation"
PROD_ALIAS: Final[str] = "prod"

# sim_runs status vocabulary.
RUN_PENDING: Final[str] = "pending"
RUN_RUNNING: Final[str] = "running"
RUN_SUCCESS: Final[str] = "success"
RUN_FAILED: Final[str] = "failed"

# outcome_status vocabulary (mirrors Module 16).
OUTCOME_COMPLETE: Final[str] = "complete"
OUTCOME_PARTIAL: Final[str] = "partial"

# list_membership labels (M02 §4.7 / PATCH 08).
LIST_RAW_ONLY: Final[str] = "raw_only"
LIST_DIVERSIFIED_ONLY: Final[str] = "diversified_only"
LIST_BOTH: Final[str] = "both"

# list_type labels for sim_config_comparisons (M02 §4.8 / PATCH 08).
LIST_TYPE_RAW: Final[str] = "raw"
LIST_TYPE_DIVERSIFIED: Final[str] = "diversified"

# Walk-forward protocol constants (01d SIMULATION/81; 01c config thresholds).
MIN_TRAIN_MONTHS: Final[int] = 12
MIN_RESOLVED_OUTCOMES_PCT: Final[float] = 0.85
MAX_DRAWDOWN_PCT: Final[float] = 25.0
# Horizon used to evaluate / select configs in walk-forward (longest horizon so a
# fold's cross-fold spill is measured against the full 40bd realization window).
SELECTION_HORIZON_BD: Final[int] = 40

# Exact metadata key set returned on every path.
RUN_METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "mode",
    "sim_name",
    "run_id",
    "start_date",
    "end_date",
    "config_ids",
    "sim_dates",
    "folds",
    "step3_rows",
    "step4_rows",
    "step5_rows",
    "outcomes_written",
    "comparisons_written",
)


# --------------------------------------------------------------------------- #
# Injection hooks.
# --------------------------------------------------------------------------- #
def _default_calendar() -> Any:
    """Return the project NYSE trading-calendar utility.

    Imported lazily and resolved through this module-level function so tests can
    ``monkeypatch`` it with a fake calendar without importing
    ``pandas_market_calendars``.
    """
    from app.utils import trading_calendar

    return trading_calendar


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for injection)."""

    def connect_simulation_with_prod(
        self, read_only: bool = ..., prod_alias: str = ...
    ) -> Any: ...


class _ValidationError(ValueError):
    """Raised internally for pre-DB validation failures."""


# --------------------------------------------------------------------------- #
# SQL — production reads are qualified with the read-only prod alias and enforce
# no-look-ahead in SQL (feature_cutoff_date <= sim_date, prices.date <= sim_date).
# Writes target unqualified ``sim_*`` tables in simulation.duckdb (no DDL).
# --------------------------------------------------------------------------- #
_SELECT_FEATURE_VERSION: Final[str] = (
    f"SELECT MAX(feature_schema_version) FROM {PROD_ALIAS}.daily_features"
)

# Step 3 screening input for one sim_date (column order == step3._INPUT_SCHEMA).
# No-look-ahead: feature_cutoff_date <= sim_date and the joined price row's
# date <= sim_date (enforced in the JOIN so left-join semantics are preserved).
_SELECT_SCREENING_INPUT: Final[str] = (
    "SELECT "
    "  f.ticker AS ticker, "
    "  f.feature_date AS feature_date, "
    "  f.feature_ready AS feature_ready, "
    "  f.ema20, f.ema50, f.ema200, f.ema_alignment_score, "
    "  f.distance_to_ema50_pct, f.rsi14, f.roc20, f.rvol20, "
    "  f.avg_dollar_volume_20d, f.breakout_proximity, "
    "  f.pullback_from_recent_high_pct, f.consolidation_score, "
    "  f.sector_relative_strength, f.market_regime, "
    "  tm.symbol_type, p.close_raw, p.close_adj, p.data_quality_status "
    f"FROM {PROD_ALIAS}.daily_features f "
    f"LEFT JOIN {PROD_ALIAS}.ticker_master tm ON tm.ticker = f.ticker "
    f"LEFT JOIN {PROD_ALIAS}.daily_prices p "
    "  ON p.ticker = f.ticker AND p.date = f.feature_date AND p.date <= ? "
    "WHERE f.feature_date = ? "
    "  AND f.feature_cutoff_date <= ? "
    "  AND f.feature_schema_version = ? "
    "ORDER BY f.ticker"
)

# Step 4 features + that-day prices for one sim_date (column order == frozen
# step4 fp_cols). Same no-look-ahead guards.
_SELECT_FEATURES_PRICES: Final[str] = (
    "SELECT "
    "  f.ticker AS ticker, f.ema20, f.ema50, f.ema200, f.ema_alignment_score, "
    "  f.rsi14, f.roc20, f.rvol20, f.atr14, f.breakout_proximity, "
    "  f.pullback_from_recent_high_pct, f.consolidation_score, "
    "  f.sector_relative_strength, f.days_to_earnings_bd, "
    "  f.macro_event_risk_flag, p.close_raw, p.close_adj, p.open_raw, "
    "  p.high_raw, p.low_raw "
    f"FROM {PROD_ALIAS}.daily_features f "
    f"LEFT JOIN {PROD_ALIAS}.daily_prices p "
    "  ON p.ticker = f.ticker AND p.date = f.feature_date AND p.date <= ? "
    "WHERE f.feature_date = ? "
    "  AND f.feature_cutoff_date <= ? "
    "  AND f.feature_schema_version = ?"
)

_SELECT_RECENT_20D_LOW: Final[str] = (
    "SELECT MIN(low_raw) FROM ("
    f"  SELECT low_raw FROM {PROD_ALIAS}.daily_prices "
    "  WHERE ticker = ? AND date <= ? "
    "  ORDER BY date DESC LIMIT 20"
    ")"
)

_SELECT_PRIOR_10: Final[str] = (
    "SELECT p.close_adj AS close_adj, f.ema20 AS ema20 "
    f"FROM {PROD_ALIAS}.daily_prices p "
    f"JOIN {PROD_ALIAS}.daily_features f "
    "  ON f.ticker = p.ticker AND f.feature_date = p.date "
    "  AND f.feature_cutoff_date <= ? AND f.feature_schema_version = ? "
    "WHERE p.ticker = ? AND p.date < ? "
    "ORDER BY p.date DESC LIMIT 10"
)

_SELECT_SECTOR_INDUSTRY: Final[str] = (
    f"SELECT ticker, sector, industry FROM {PROD_ALIAS}.ticker_master"
)

# Outcome reads (forward-looking by design — NOT subject to the sim_date ceiling).
_SELECT_OPEN_RAW: Final[str] = (
    f"SELECT open_raw FROM {PROD_ALIAS}.daily_prices WHERE ticker = ? AND date = ?"
)
_SELECT_CLOSE_ADJ: Final[str] = (
    f"SELECT close_adj FROM {PROD_ALIAS}.daily_prices WHERE ticker = ? AND date = ?"
)
_SELECT_WINDOW_CANDLES: Final[str] = (
    f"SELECT date, high_adj, low_adj FROM {PROD_ALIAS}.daily_prices "
    "WHERE ticker = ? AND date BETWEEN ? AND ?"
)

# --- sim_* writes (no DDL; operate on existing tables only). --------------- #
_INSERT_SIM_RUN: Final[str] = (
    "INSERT INTO sim_runs "
    "(sim_run_id, sim_name, mode, start_date, end_date, created_at, "
    " config_ids, status, notes) "
    "VALUES (?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP), ?, ?, NULL)"
)
_UPDATE_SIM_RUN_STATUS: Final[str] = (
    "UPDATE sim_runs SET status = ? WHERE sim_run_id = ?"
)
_UPDATE_SIM_RUN_FAILED: Final[str] = (
    "UPDATE sim_runs SET status = 'failed', notes = ? WHERE sim_run_id = ?"
)

_INSERT_SIM_FOLD: Final[str] = (
    "INSERT INTO sim_folds "
    "(fold_id, sim_run_id, fold_number, train_start, train_end, test_start, "
    " test_end, selected_config_id, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

_INSERT_SIM_STEP3: Final[str] = (
    "INSERT INTO sim_step3_candidates "
    "(candidate_id, sim_run_id, fold_id, ticker, signal_date, "
    " eligibility_score, passed_eligibility, routing_status, "
    " routing_fail_reason, eligibility_fail_reasons, routed_setup_types, "
    " created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

_INSERT_SIM_STEP4: Final[str] = (
    "INSERT INTO sim_step4_analysis "
    "(analysis_id, candidate_id, sim_run_id, fold_id, setup_config_id, "
    " ticker, signal_date, setup_type, setup_score, setup_passed, "
    " estimated_rr, target_is_structural, stop_price_raw, target_price_raw, "
    " created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

_INSERT_SIM_STEP5: Final[str] = (
    "INSERT INTO sim_step5_proposals "
    "(proposal_id, sim_run_id, fold_id, setup_config_id, ticker, "
    " signal_date, setup_type, setup_score, risk_label, disposition, "
    " proposal_score_raw, diversity_penalty, proposal_score_final, "
    " raw_rank, diversified_rank, in_raw_top_n, in_diversified_top_n, "
    " diversification_applied, selected_top_n, selected_flag, "
    " rejection_reason, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    " CAST(now() AS TIMESTAMP))"
)

_INSERT_SIM_OUTCOME: Final[str] = (
    "INSERT INTO sim_signal_outcomes "
    "(outcome_id, sim_run_id, fold_id, proposal_id, ticker, "
    " setup_config_id, setup_type, risk_label, signal_date, entry_date, "
    " entry_price_raw, entry_price_sim, stop_price_raw, target_price_raw, "
    " list_membership, return_5bd_pct, return_10bd_pct, return_20bd_pct, "
    " return_40bd_pct, mfe_40bd_pct, mae_40bd_pct, realized_r_multiple, "
    " cross_fold_outcome, outcome_status, calculated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    " ?, ?, CAST(now() AS TIMESTAMP))"
)

_INSERT_SIM_COMPARISON: Final[str] = (
    "INSERT INTO sim_config_comparisons "
    "(comparison_id, sim_run_id, config_id, setup_type, risk_label, "
    " horizon_bd, expectancy, win_rate, avg_win, avg_loss, profit_factor, "
    " max_drawdown_pct, resolved_outcomes_pct, list_type, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #
def _f(value: Any) -> float | None:
    """Coerce a DB cell to ``float`` or ``None`` (NaN -> ``None``)."""
    if value is None:
        return None
    fv = float(value)
    if fv != fv:  # NaN
        return None
    return fv


def _list_membership(in_raw: bool, in_div: bool) -> str | None:
    """Return the ``list_membership`` label for raw/diversified Top-N flags."""
    if in_raw and in_div:
        return LIST_BOTH
    if in_raw:
        return LIST_RAW_ONLY
    if in_div:
        return LIST_DIVERSIFIED_ONLY
    return None  # caller never creates outcomes for non-list proposals


def _add_months(day: date, months: int) -> date:
    """Return ``day`` advanced by ``months`` calendar months (clamped day-of-month)."""
    total = (day.year * 12 + (day.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    # Clamp day to the last valid day of the target month.
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    last_day = (next_first - _dt.timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _quarter_start(day: date) -> date:
    """First calendar day of ``day``'s calendar quarter."""
    q_month = ((day.month - 1) // 3) * 3 + 1
    return date(day.year, q_month, 1)


def _next_quarter_start(day: date) -> date:
    """First calendar day of the quarter after ``day``'s quarter."""
    qs = _quarter_start(day)
    return _add_months(qs, 3)


def _quarter_end(quarter_start: date) -> date:
    """Last calendar day of the quarter beginning at ``quarter_start``."""
    return _add_months(quarter_start, 3) - _dt.timedelta(days=1)


def plan_walk_forward_folds(
    start_date: date, end_date: date
) -> list[dict[str, date | int]]:
    """Plan expanding-window walk-forward folds (pure, deterministic).

    The first test fold is the calendar quarter that begins at least
    :data:`MIN_TRAIN_MONTHS` months after ``start_date``; subsequent quarters up
    to ``end_date`` are additional test folds. Each fold's train window is
    ``[start_date, test_start - 1 day]`` (expanding). Returns an ordered list of
    fold dicts with ``fold_number`` / ``train_start`` / ``train_end`` /
    ``test_start`` / ``test_end`` (``test_end`` clamped to ``end_date``).
    """
    folds: list[dict[str, date | int]] = []
    earliest_test_start = _next_quarter_start(_add_months(start_date, MIN_TRAIN_MONTHS))
    test_start = earliest_test_start
    fold_number = 1
    while test_start <= end_date:
        test_end = min(_quarter_end(test_start), end_date)
        folds.append(
            {
                "fold_number": fold_number,
                "train_start": start_date,
                "train_end": test_start - _dt.timedelta(days=1),
                "test_start": test_start,
                "test_end": test_end,
            }
        )
        fold_number += 1
        test_start = _add_months(test_start, 3)
    return folds


def compute_metrics(ordered_returns: list[float | None]) -> dict[str, float | None]:
    """Compute the config-comparison metrics for one group.

    ``ordered_returns`` is the per-outcome horizon return (decimal) for the
    group, ordered by ``signal_date`` (``None`` for unresolved outcomes). Metrics
    use only resolved outcomes; ``resolved_outcomes_pct`` is the resolved /
    total fraction (the one figure that counts unresolved rows in its
    denominator).

    ``expectancy`` is the mean realized return over resolved outcomes (gap
    G-EXPECTANCY: Project Files give thresholds but no closed-form expectancy, so
    mean realized return per trade is used). ``max_drawdown_pct`` is the
    peak-to-trough drawdown of the compounded equity curve, in percent (0–100).
    """
    total = len(ordered_returns)
    resolved = [r for r in ordered_returns if r is not None]
    n_resolved = len(resolved)
    resolved_pct = (n_resolved / total) if total else None

    if n_resolved == 0:
        return {
            "expectancy": None,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "profit_factor": None,
            "max_drawdown_pct": None,
            "resolved_outcomes_pct": resolved_pct,
        }

    wins = [r for r in resolved if r > 0]
    losses = [r for r in resolved if r < 0]
    win_rate = len(wins) / n_resolved
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    expectancy = sum(resolved) / n_resolved
    gross_loss = abs(sum(losses))
    profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else None

    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in resolved:
        equity *= 1.0 + r
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)

    return {
        "expectancy": expectancy,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd * 100.0,
        "resolved_outcomes_pct": resolved_pct,
    }


def select_config_for_fold(
    metrics_by_config: dict[str, dict[str, float | None]],
) -> str | None:
    """Select the best config per the walk-forward rule (pure).

    Eligible configs have ``resolved_outcomes_pct >= 0.85`` and
    ``max_drawdown_pct <= 25``. Among eligible configs the one with the highest
    ``expectancy`` wins (ties broken by ``config_id`` ascending). Returns
    ``None`` when no config is eligible.
    """
    eligible: list[tuple[float, str]] = []
    for config_id in sorted(metrics_by_config):
        m = metrics_by_config[config_id]
        resolved = m.get("resolved_outcomes_pct")
        max_dd = m.get("max_drawdown_pct")
        expectancy = m.get("expectancy")
        if resolved is None or expectancy is None or max_dd is None:
            continue
        if resolved >= MIN_RESOLVED_OUTCOMES_PCT and max_dd <= MAX_DRAWDOWN_PCT:
            eligible.append((expectancy, config_id))
    if not eligible:
        return None
    # Highest expectancy wins; ties broken by config_id ascending.
    eligible.sort(key=lambda pair: (-pair[0], pair[1]))
    return eligible[0][1]


# --------------------------------------------------------------------------- #
# Simulation engine.
# --------------------------------------------------------------------------- #
# Sentinel returned by _run_with_connection when replay is not yet supported.
_REPLAY_UNSUPPORTED: Final[str] = (
    "UNSUPPORTED: setup-mode simulation replay (step3_universal_eligibility / "
    "step4_setup_validation_engine) is not yet implemented in SimulationEngine. "
    "The engine accepts mode/config validation and writes sim_runs metadata, "
    "but returns failed before any replay. "
    "Legacy step3_screening / step4_analysis_engine are NOT executed."
)


class SimulationEngine:
    """Setup-mode simulation engine (replay phase: pending).

    Validation, walk-forward fold planning, metric computation, and config
    comparison helpers are fully functional.  The replay phase
    (``_replay_date`` / ``_run_with_connection`` past DB init) is guarded and
    returns a clear ``failed`` ServiceResult rather than executing the legacy
    ``step3_screening`` / ``step4_analysis_engine`` path.

    The optional ``db_manager`` argument exists only for test injection; when
    ``None`` the approved :mod:`app.database.duckdb_manager` is used.
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db_override: _DbManagerLike | None = db_manager

    def _db(self) -> Any:
        """Resolve the DB manager (injected override or the approved manager)."""
        if self._db_override is not None:
            return self._db_override
        from app.database import duckdb_manager

        return duckdb_manager

    def _slippage_bps(self, config: dict) -> float:
        """Extract and validate slippage_bps from a setup config dict."""
        from app.services.outcomes.outcome_queue import _validate_config, _ConfigError
        try:
            return _validate_config(config)
        except _ConfigError as exc:
            raise _ValidationError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Public API.
    # ------------------------------------------------------------------ #
    def run(
        self,
        sim_name: str,
        mode: str,
        start_date: date,
        end_date: date,
        config_ids: list[str],
        setup_configs: dict[str, dict],
        db_role: str = "simulation",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Run a simulation and return a :class:`ServiceResult`.

        All validation happens before any DB access. ``run_id`` is minted
        (``uuid4``) when ``None`` and otherwise preserved. ``metadata`` carries
        exactly :data:`RUN_METADATA_KEYS` on every return path.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()
        log.info(
            "simulation run start mode=%s sim_name=%s start=%s end=%s configs=%s",
            mode,
            sim_name,
            start_iso,
            end_iso,
            config_ids,
        )

        # --- pre-DB validation (no I/O). ---------------------------------- #
        try:
            self._validate(mode, start_date, end_date, config_ids, setup_configs, db_role)
        except _ValidationError as exc:
            log.error("simulation run failed validation: %s", exc)
            return self._failed_no_run(run_id, mode, sim_name, start_iso, end_iso, config_ids, str(exc))

        # --- open the single simulation connection (prod attached RO). ---- #
        try:
            connection = self._db().connect_simulation_with_prod()
        except Exception as exc:  # noqa: BLE001 - surface connect failure
            message = f"connect failed: {type(exc).__name__}: {exc}"
            log.error("simulation run failed: %s", message)
            return self._failed_no_run(
                run_id, mode, sim_name, start_iso, end_iso, config_ids, message
            )

        try:
            return self._run_with_connection(
                connection,
                log,
                run_id=run_id,
                sim_name=sim_name,
                mode=mode,
                start_date=start_date,
                end_date=end_date,
                config_ids=config_ids,
                setup_configs=setup_configs,
            )
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # Validation.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate(
        mode: str,
        start_date: date,
        end_date: date,
        config_ids: list[str],
        setup_configs: dict[str, dict],
        db_role: str,
    ) -> None:
        """Validate every pre-DB precondition, raising :class:`_ValidationError`."""
        if db_role != DB_ROLE_SIMULATION:
            raise _ValidationError(
                f"Unsupported db_role {db_role!r}. Module 17 writes only to "
                f"{DB_ROLE_SIMULATION!r}."
            )
        if mode not in ALLOWED_MODES:
            raise _ValidationError(
                f"Unknown mode {mode!r}. Valid modes: {list(ALLOWED_MODES)}."
            )
        if not config_ids:
            raise _ValidationError("config_ids must be non-empty")
        if not isinstance(setup_configs, dict):
            raise _ValidationError("setup_configs must be a dict")
        for cid in config_ids:
            if cid not in setup_configs:
                raise _ValidationError(
                    f"config_id {cid!r} has no entry in setup_configs"
                )
        if start_date > end_date:
            raise _ValidationError(
                f"start_date {start_date.isoformat()} is after end_date "
                f"{end_date.isoformat()}"
            )

    # ------------------------------------------------------------------ #
    # Orchestration with an open connection.
    # ------------------------------------------------------------------ #
    def _run_with_connection(
        self,
        connection: Any,
        log: Any,
        *,
        run_id: str,
        sim_name: str,
        mode: str,
        start_date: date,
        end_date: date,
        config_ids: list[str],
        setup_configs: dict[str, dict],
    ) -> ServiceResult:
        start_iso = start_date.isoformat()
        end_iso = end_date.isoformat()

        # Track lifecycle state so the except clause knows what to clean up.
        run_row_created = False
        tx_started = False
        sim_dates_count = 0
        folds_count = 0

        try:
            # --- sim_runs: pending (autocommit). ----------------------------- #
            connection.execute(
                _INSERT_SIM_RUN,
                [run_id, sim_name, mode, start_date, end_date, json.dumps(config_ids), RUN_PENDING],
            )
            run_row_created = True

            # --- sim_runs: running (autocommit). ----------------------------- #
            connection.execute(_UPDATE_SIM_RUN_STATUS, [RUN_RUNNING, run_id])

            cal = _default_calendar()
            feature_version = self._feature_version(connection)
            sim_dates = cal.trading_days_between(start_date, end_date)
            sim_dates_count = len(sim_dates)

            folds = (
                plan_walk_forward_folds(start_date, end_date)
                if mode == MODE_WALK_FORWARD
                else []
            )
            folds_count = len(folds)
            fold_ids = {f["fold_number"]: str(uuid.uuid4()) for f in folds}

            # ── Replay guard (setup-mode engines not yet wired) ────────────── #
            # Legacy step3_screening / step4_analysis_engine must NOT execute.
            # Return failed before any replay work; sim_run row status → failed.
            connection.execute(_UPDATE_SIM_RUN_FAILED, [_REPLAY_UNSUPPORTED, run_id])
            raise _ValidationError(_REPLAY_UNSUPPORTED)

            # --- all replay / write work in one transaction. ----------------- #  # noqa: unreachable
            connection.execute("BEGIN TRANSACTION")
            tx_started = True

            counters = {
                "step3_rows": 0,
                "step4_rows": 0,
                "step5_rows": 0,
                "outcomes_written": 0,
            }
            all_outcomes: list[dict[str, Any]] = []

            for config_id in config_ids:
                config = setup_configs[config_id]
                slippage_bps = self._slippage_bps(config)
                sector_industry = self._sector_industry_map(connection)

                for sim_date in sim_dates:
                    fold = self._fold_for_date(folds, sim_date)
                    fold_id = fold_ids.get(fold["fold_number"]) if fold else None

                    replay = self._replay_date(
                        connection,
                        run_id=run_id,
                        fold_id=fold_id,
                        config_id=config_id,
                        config=config,
                        parsed={},
                        sim_date=sim_date,
                        feature_version=feature_version,
                        sector_industry=sector_industry,
                    )
                    counters["step3_rows"] += replay["step3_rows"]
                    counters["step4_rows"] += replay["step4_rows"]
                    counters["step5_rows"] += replay["step5_rows"]

                    outcomes = self._build_outcomes(
                        connection,
                        cal,
                        run_id=run_id,
                        fold=fold,
                        fold_id=fold_id,
                        config_id=config_id,
                        sim_date=sim_date,
                        slippage_bps=slippage_bps,
                        proposals=replay["proposals"],
                        stop_by_ticker=replay["stop_by_ticker"],
                    )
                    for o in outcomes:
                        connection.execute(_INSERT_SIM_OUTCOME, self._outcome_params(o))
                        counters["outcomes_written"] += 1
                        all_outcomes.append(o)

            if mode == MODE_WALK_FORWARD:
                self._write_folds(connection, run_id, folds, fold_ids, all_outcomes)

            comparisons_written = self._write_comparisons(
                connection, run_id, mode, config_ids, all_outcomes
            )

            connection.execute(_UPDATE_SIM_RUN_STATUS, [RUN_SUCCESS, run_id])
            connection.execute("COMMIT")
            tx_started = False

        except Exception as exc:  # noqa: BLE001 - surface as failed with notes
            note = f"{type(exc).__name__}: {exc}"
            log.error("simulation run failed: %s", note)

            if tx_started:
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001 - never mask original error
                    pass

            if run_row_created:
                try:
                    connection.execute(_UPDATE_SIM_RUN_FAILED, [note, run_id])
                except Exception:  # noqa: BLE001 - best-effort status write
                    pass

            return self._failed_with_run(
                run_id, mode, sim_name, start_iso, end_iso, config_ids,
                note, sim_dates=sim_dates_count, folds=folds_count,
            )

        log.info(
            "simulation run done status=success step3=%d step4=%d step5=%d "
            "outcomes=%d comparisons=%d",
            counters["step3_rows"],
            counters["step4_rows"],
            counters["step5_rows"],
            counters["outcomes_written"],
            comparisons_written,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=counters["outcomes_written"],
            metadata=self._metadata(
                mode=mode,
                sim_name=sim_name,
                run_id=run_id,
                start_iso=start_iso,
                end_iso=end_iso,
                config_ids=config_ids,
                sim_dates=sim_dates_count,
                folds=folds_count,
                step3_rows=counters["step3_rows"],
                step4_rows=counters["step4_rows"],
                step5_rows=counters["step5_rows"],
                outcomes_written=counters["outcomes_written"],
                comparisons_written=comparisons_written,
            ),
        )

    # ------------------------------------------------------------------ #
    # Config validation helpers.
    # ------------------------------------------------------------------ #
    # _parse_all_configs removed: it imported legacy step3_screening /
    # step4_analysis_engine engines which must not run in setup-mode.
    # Config-level validation is now done by _slippage_bps() only.

    # ------------------------------------------------------------------ #
    # Read helpers.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _feature_version(connection: Any) -> str | None:
        row = connection.execute(_SELECT_FEATURE_VERSION).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _sector_industry_map(connection: Any) -> dict[str, tuple[Any, Any]]:
        rows = connection.execute(_SELECT_SECTOR_INDUSTRY).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    @staticmethod
    def _fold_for_date(
        folds: list[dict[str, Any]], sim_date: date
    ) -> dict[str, Any] | None:
        """Return the test fold whose test period contains ``sim_date`` (or None)."""
        for fold in folds:
            if fold["test_start"] <= sim_date <= fold["test_end"]:
                return fold
        return None

    # ------------------------------------------------------------------ #
    # Per-date Step 3/4/5 replay — UNSUPPORTED in current release.
    # ------------------------------------------------------------------ #
    def _replay_date(
        self,
        connection: Any,
        *,
        run_id: str,
        fold_id: str | None,
        config_id: str,
        config: dict,
        parsed: dict[str, Any],
        sim_date: date,
        feature_version: str | None,
        sector_industry: dict[str, tuple[Any, Any]],
    ) -> dict[str, Any]:
        """Stub — setup-mode replay not yet implemented.

        Legacy ``step3_screening`` / ``step4_analysis_engine`` must not be
        called.  The replay guard in ``_run_with_connection`` prevents this
        method from being reached; this stub exists only for static analysis
        and test inspection.
        """
        raise _ValidationError(_REPLAY_UNSUPPORTED)

        # _read_step4_inputs removed: it called legacy step4_analysis_engine.

        # ------------------------------------------------------------------ #
    # Outcomes (Module 16 §64 rules adapted to simulation tables).
    # ------------------------------------------------------------------ #
    def _build_outcomes(
        self,
        connection: Any,
        cal: Any,
        *,
        run_id: str,
        fold: dict[str, Any] | None,
        fold_id: str | None,
        config_id: str,
        sim_date: date,
        slippage_bps: float,
        proposals: list[dict[str, Any]],
        stop_by_ticker: dict[str, float | None],
    ) -> list[dict[str, Any]]:
        """Build one ``sim_signal_outcomes`` payload per raw/diversified proposal."""
        outcomes: list[dict[str, Any]] = []
        entry_date = cal.next_trading_day(sim_date)

        for p in proposals:
            in_raw = bool(p["in_raw_top_n"])
            in_div = bool(p["in_diversified_top_n"])
            if not (in_raw or in_div):
                continue  # outcomes only for raw OR diversified list members
            membership = _list_membership(in_raw, in_div)
            ticker = p["ticker"]

            entry_open = _f(self._scalar(connection, _SELECT_OPEN_RAW, [ticker, entry_date]))
            if entry_open is None:
                continue  # no entry candle -> no outcome row (V1: skip)

            entry_price_raw = entry_open
            entry_price_sim = entry_price_raw * (1.0 + slippage_bps / 10000.0)

            returns: dict[int, float | None] = {5: None, 10: None, 20: None, 40: None}
            eval_close: dict[int, float | None] = {}
            for n in constants.OUTCOME_HORIZONS_BD:
                eval_n = cal.add_trading_days(entry_date, n)
                close_n = _f(self._scalar(connection, _SELECT_CLOSE_ADJ, [ticker, eval_n]))
                eval_close[n] = close_n
                returns[n] = None if close_n is None else close_n / entry_price_sim - 1.0

            eval_40 = cal.add_trading_days(entry_date, SELECTION_HORIZON_BD)
            mfe_40, mae_40 = self._window_mfe_mae(
                connection, cal, ticker, entry_date, eval_40, entry_price_sim
            )

            stop = stop_by_ticker.get(ticker)
            realized_r = self._realized_r(eval_close.get(40), entry_price_sim, stop)

            status = (
                OUTCOME_COMPLETE
                if all(returns[n] is not None for n in constants.OUTCOME_HORIZONS_BD)
                else OUTCOME_PARTIAL
            )

            cross_fold = False
            if fold is not None and eval_40 > fold["test_end"]:
                cross_fold = True

            outcomes.append(
                {
                    "outcome_id": str(uuid.uuid4()),
                    "sim_run_id": run_id,
                    "fold_id": fold_id,
                    "proposal_id": p["proposal_id"],
                    "ticker": ticker,
                    "setup_config_id": config_id,
                    "setup_type": p.get("setup_type"),
                    "risk_label": p.get("risk_label"),
                    "signal_date": sim_date,
                    "entry_date": entry_date,
                    "entry_price_raw": entry_price_raw,
                    "entry_price_sim": entry_price_sim,
                    "stop_price_raw": stop_by_ticker.get(ticker),
                    "target_price_raw": p.get("target_price_raw"),
                    "list_membership": membership,
                    "return_5bd_pct": returns[5],
                    "return_10bd_pct": returns[10],
                    "return_20bd_pct": returns[20],
                    "return_40bd_pct": returns[40],
                    "mfe_40bd_pct": mfe_40,
                    "mae_40bd_pct": mae_40,
                    "realized_r_multiple": realized_r,
                    "cross_fold_outcome": cross_fold,
                    "outcome_status": status,
                }
            )
        return outcomes

    @staticmethod
    def _outcome_params(o: dict[str, Any]) -> list[Any]:
        return [
            o["outcome_id"], o["sim_run_id"], o["fold_id"], o["proposal_id"],
            o["ticker"], o["setup_config_id"], o["setup_type"], o["risk_label"],
            o["signal_date"], o["entry_date"],
            o["entry_price_raw"], o["entry_price_sim"],
            o["stop_price_raw"], o["target_price_raw"],
            o["list_membership"],
            o["return_5bd_pct"], o["return_10bd_pct"],
            o["return_20bd_pct"], o["return_40bd_pct"],
            o["mfe_40bd_pct"], o["mae_40bd_pct"], o["realized_r_multiple"],
            o["cross_fold_outcome"], o["outcome_status"],
        ]

    @staticmethod
    def _scalar(connection: Any, sql: str, params: list[Any]) -> Any:
        row = connection.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def _window_mfe_mae(
        self,
        connection: Any,
        cal: Any,
        ticker: str,
        entry_date: date,
        eval_date: date,
        entry_price_sim: float,
    ) -> tuple[float | None, float | None]:
        """40bd MFE/MAE over ``[entry_date, eval_date]`` or ``(None, None)``.

        Every expected NYSE session in the inclusive window must have a non-NULL
        ``high_adj`` / ``low_adj`` candle, mirroring FORMULAS §64 / Module 16.
        """
        expected = cal.trading_days_between(entry_date, eval_date)
        candles = {
            r[0]: (r[1], r[2])
            for r in connection.execute(
                _SELECT_WINDOW_CANDLES, [ticker, entry_date, eval_date]
            ).fetchall()
        }
        highs: list[float] = []
        lows: list[float] = []
        for day in expected:
            cell = candles.get(day)
            if cell is None:
                return None, None
            high = _f(cell[0])
            low = _f(cell[1])
            if high is None or low is None:
                return None, None
            highs.append(high)
            lows.append(low)
        if not highs:
            return None, None
        return max(highs) / entry_price_sim - 1.0, min(lows) / entry_price_sim - 1.0

    @staticmethod
    def _realized_r(
        exit_close_adj: float | None,
        entry_price_sim: float,
        stop_price_raw: float | None,
    ) -> float | None:
        if exit_close_adj is None or stop_price_raw is None:
            return None
        denom = entry_price_sim - stop_price_raw
        if denom <= 0:
            return None
        return (exit_close_adj - entry_price_sim) / denom

    # ------------------------------------------------------------------ #
    # Walk-forward folds.
    # ------------------------------------------------------------------ #
    def _write_folds(
        self,
        connection: Any,
        run_id: str,
        folds: list[dict[str, Any]],
        fold_ids: dict[int, str],
        all_outcomes: list[dict[str, Any]],
    ) -> None:
        """Select the best config per fold and insert ``sim_folds`` rows."""
        for fold in folds:
            metrics_by_config = self._fold_train_metrics(fold, all_outcomes)
            selected = select_config_for_fold(metrics_by_config)
            connection.execute(
                _INSERT_SIM_FOLD,
                [
                    fold_ids[fold["fold_number"]], run_id, fold["fold_number"],
                    fold["train_start"], fold["train_end"], fold["test_start"],
                    fold["test_end"], selected,
                ],
            )

    @staticmethod
    def _fold_train_metrics(
        fold: dict[str, Any], all_outcomes: list[dict[str, Any]]
    ) -> dict[str, dict[str, float | None]]:
        """Per-config training metrics for ``fold`` (diversified list, 40bd).

        Train outcomes are those whose ``signal_date`` falls in the fold's train
        window, excluding ``cross_fold_outcome = TRUE`` rows. Ordered by
        ``signal_date`` so the drawdown curve is chronological.
        """
        by_config: dict[str, list[tuple[date, float | None]]] = {}
        for o in all_outcomes:
            if o["cross_fold_outcome"]:
                continue
            if not (fold["train_start"] <= o["signal_date"] <= fold["train_end"]):
                continue
            if o["list_membership"] not in (LIST_DIVERSIFIED_ONLY, LIST_BOTH):
                continue
            by_config.setdefault(o["setup_config_id"], []).append(
                (o["signal_date"], o["return_40bd_pct"])
            )
        metrics: dict[str, dict[str, float | None]] = {}
        for cid, rows in by_config.items():
            rows.sort(key=lambda pair: pair[0])
            metrics[cid] = compute_metrics([r for _, r in rows])
        return metrics

    # ------------------------------------------------------------------ #
    # Config comparisons.
    # ------------------------------------------------------------------ #
    def _write_comparisons(
        self,
        connection: Any,
        run_id: str,
        mode: str,
        config_ids: list[str],
        all_outcomes: list[dict[str, Any]],
    ) -> int:
        """Insert one ``sim_config_comparisons`` row per (config, horizon, list_type).

        For ``walk_forward`` only non-cross-fold outcomes are aggregated; for the
        other modes every outcome counts. Metrics are computed per list type
        (``raw`` includes ``raw_only`` + ``both``; ``diversified`` includes
        ``diversified_only`` + ``both``).
        """
        relevant = [
            o for o in all_outcomes
            if not (mode == MODE_WALK_FORWARD and o["cross_fold_outcome"])
        ]
        written = 0
        list_types = (
            (LIST_TYPE_RAW, (LIST_RAW_ONLY, LIST_BOTH)),
            (LIST_TYPE_DIVERSIFIED, (LIST_DIVERSIFIED_ONLY, LIST_BOTH)),
        )
        return_key = {5: "return_5bd_pct", 10: "return_10bd_pct",
                      20: "return_20bd_pct", 40: "return_40bd_pct"}
        for config_id in config_ids:
            for list_type, memberships in list_types:
                group = sorted(
                    (
                        o for o in relevant
                        if o["setup_config_id"] == config_id
                        and o["list_membership"] in memberships
                    ),
                    key=lambda o: o["signal_date"],
                )
                for horizon in constants.OUTCOME_HORIZONS_BD:
                    ordered = [o[return_key[horizon]] for o in group]
                    m = compute_metrics(ordered)
                    connection.execute(
                        _INSERT_SIM_COMPARISON,
                        [
                            str(uuid.uuid4()), run_id, config_id,
                            None, None,  # setup_type, risk_label: aggregate comparison row
                            horizon,
                            m["expectancy"], m["win_rate"], m["avg_win"],
                            m["avg_loss"], m["profit_factor"], m["max_drawdown_pct"],
                            m["resolved_outcomes_pct"], list_type,
                        ],
                    )
                    written += 1
        return written

    # ------------------------------------------------------------------ #
    # Result builders.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _metadata(
        *,
        mode: str,
        sim_name: str,
        run_id: str,
        start_iso: str,
        end_iso: str,
        config_ids: list[str],
        sim_dates: int,
        folds: int,
        step3_rows: int,
        step4_rows: int,
        step5_rows: int,
        outcomes_written: int,
        comparisons_written: int,
    ) -> dict[str, Any]:
        return {
            "db_role": DB_ROLE_SIMULATION,
            "mode": mode,
            "sim_name": sim_name,
            "run_id": run_id,
            "start_date": start_iso,
            "end_date": end_iso,
            "config_ids": list(config_ids),
            "sim_dates": sim_dates,
            "folds": folds,
            "step3_rows": step3_rows,
            "step4_rows": step4_rows,
            "step5_rows": step5_rows,
            "outcomes_written": outcomes_written,
            "comparisons_written": comparisons_written,
        }

    def _failed_no_run(
        self,
        run_id: str,
        mode: str,
        sim_name: str,
        start_iso: str,
        end_iso: str,
        config_ids: list[str],
        message: str,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._metadata(
                mode=mode, sim_name=sim_name, run_id=run_id, start_iso=start_iso,
                end_iso=end_iso, config_ids=config_ids, sim_dates=0, folds=0,
                step3_rows=0, step4_rows=0, step5_rows=0, outcomes_written=0,
                comparisons_written=0,
            ),
        )

    def _failed_with_run(
        self,
        run_id: str,
        mode: str,
        sim_name: str,
        start_iso: str,
        end_iso: str,
        config_ids: list[str],
        message: str,
        *,
        sim_dates: int,
        folds: int,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._metadata(
                mode=mode, sim_name=sim_name, run_id=run_id, start_iso=start_iso,
                end_iso=end_iso, config_ids=config_ids, sim_dates=sim_dates,
                folds=folds, step3_rows=0, step4_rows=0, step5_rows=0,
                outcomes_written=0, comparisons_written=0,
            ),
        )


__all__ = [
    "SimulationEngine",
    "ALLOWED_MODES",
    "RUN_METADATA_KEYS",
    "plan_walk_forward_folds",
    "compute_metrics",
    "select_config_for_fold",
]
