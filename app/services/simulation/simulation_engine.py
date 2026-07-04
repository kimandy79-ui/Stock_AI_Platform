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
from typing import Any, Callable, Final, Protocol

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

# Embargo window (trading days) excluded from a fold's train metrics immediately
# before its test_start, so signals whose outcome realization overlaps the test
# period cannot leak forward-looking information into training metrics. Default
# matches SELECTION_HORIZON_BD; configurable per-engine (see
# ``SimulationEngine.__init__``), never hardcoded into the metric computation
# itself (``_fold_train_metrics`` takes it as a parameter, default 0 = off, so
# existing offline unit tests that don't pass it keep their pre-embargo behavior).
DEFAULT_EMBARGO_BD: Final[int] = 40

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

# Step 3 universe read for one sim_date — mirrors step3_universal_eligibility's
# _SQL_READ_UNIVERSE column-for-column (same column order as _STEP3_COL_NAMES
# below) so _check_eligibility / _evaluate_routing / _compute_eligibility_score
# can be reused verbatim. No-look-ahead: feature_cutoff_date <= sim_date and the
# joined price row's date <= sim_date (enforced in the JOIN so left-join
# semantics are preserved, matching prod's ticker_master LEFT JOIN pattern).
_SELECT_UNIVERSE_STEP3: Final[str] = (
    "SELECT "
    "  tm.ticker, tm.symbol_type, "
    "  dp.open_raw, dp.high_raw, dp.low_raw, dp.close_raw, dp.close_adj, "
    "  CAST(dp.volume_raw AS BIGINT) AS volume_raw, dp.data_quality_status, "
    "  COALESCE(df.feature_ready, FALSE) AS feature_ready, "
    "  df.avg_dollar_volume_20d, "
    "  df.breakout_proximity, df.range_duration, "
    "  df.ema200, df.pullback_from_recent_high_pct, "
    "  df.ema20, df.ema50, df.ema_alignment_score, df.ema50_slope, "
    "  df.range_tightness_score "
    f"FROM {PROD_ALIAS}.ticker_master tm "
    f"LEFT JOIN {PROD_ALIAS}.daily_prices dp "
    "  ON dp.ticker = tm.ticker AND dp.date = ? AND dp.date <= ? "
    f"LEFT JOIN {PROD_ALIAS}.daily_features df "
    "  ON df.ticker = tm.ticker AND df.feature_date = ? "
    "  AND df.feature_cutoff_date <= ? AND df.feature_schema_version = ? "
    "WHERE tm.active_flag = TRUE AND tm.delisted_flag = FALSE "
    "ORDER BY tm.ticker"
)

# Column order for _SELECT_UNIVERSE_STEP3 rows — identical to
# step3_universal_eligibility.Step3UniversalEligibilityEngine._read's col_names
# so _check_eligibility / _evaluate_routing receive the exact same row shape.
_STEP3_COL_NAMES: Final[tuple[str, ...]] = (
    "ticker", "symbol_type",
    "open_raw", "high_raw", "low_raw", "close_raw", "close_adj",
    "volume_raw", "data_quality_status",
    "feature_ready", "avg_dollar_volume_20d",
    "breakout_proximity", "range_duration",
    "ema200", "pullback_from_recent_high_pct",
    "ema20", "ema50", "ema_alignment_score", "ema50_slope",
    "range_tightness_score",
)

# Step 4/5 features + that-day prices for one sim_date — column order ==
# _STEP4_FEATURE_COLS below, matching step4_setup_validation_engine.py's
# _FEATURE_COLS so m14_setup_validators.validate_setup receives the exact same
# feat-dict shape it gets in prod. Same no-look-ahead guards as prod (feature
# cutoff + price date ceiling in the JOIN). This single per-date read is shared
# by every (setup_config_id, risk_label_config_id) variant — it is not
# re-queried per variant.
_SELECT_FEATURES_PRICES: Final[str] = (
    "SELECT "
    "  f.ticker, f.feature_schema_version, "
    "  f.ema20, f.ema50, f.ema200, f.ema_alignment_score, "
    "  f.ema20_slope, f.ema50_slope, "
    "  f.distance_to_ema20_pct, f.distance_to_ema50_pct, "
    "  f.rsi14, f.roc20, f.atr14, f.atr_pct, f.atr_compression_score, "
    "  f.rvol20, f.avg_dollar_volume_20d, "
    "  f.pullback_from_recent_high_pct, f.pullback_depth_pct, "
    "  f.breakout_proximity, f.consolidation_score, "
    "  f.swing_high, f.swing_low, "
    "  f.support_level, f.resistance_level, f.next_resistance_level, "
    "  f.base_high, f.base_low, "
    "  f.range_width_pct, f.range_duration, f.range_tightness_score, "
    "  f.volume_dry_up_score, f.volume_expansion_score, "
    "  f.relative_strength_vs_spy, f.sector_relative_strength, "
    "  f.market_regime, f.days_to_earnings_bd, f.macro_event_risk_flag, "
    "  p.open_raw, p.high_raw, p.low_raw, p.close_raw, p.close_adj "
    f"FROM {PROD_ALIAS}.daily_features f "
    f"LEFT JOIN {PROD_ALIAS}.daily_prices p "
    "  ON p.ticker = f.ticker AND p.date = f.feature_date AND p.date <= ? "
    "WHERE f.feature_date = ? "
    "  AND f.feature_cutoff_date <= ? "
    "  AND f.feature_schema_version = ?"
)

# Column order for _SELECT_FEATURES_PRICES rows — identical to
# step4_setup_validation_engine._FEATURE_COLS. Serves both Step 4
# (validate_setup) and Step 5 (_build_rows' features_map) — Step 5 only reads a
# subset of these keys via dict.get(), so one shared per-ticker dict covers both.
_STEP4_FEATURE_COLS: Final[tuple[str, ...]] = (
    "ticker", "feature_schema_version",
    "ema20", "ema50", "ema200", "ema_alignment_score",
    "ema20_slope", "ema50_slope",
    "distance_to_ema20_pct", "distance_to_ema50_pct",
    "rsi14", "roc20", "atr14", "atr_pct", "atr_compression_score",
    "rvol20", "avg_dollar_volume_20d",
    "pullback_from_recent_high_pct", "pullback_depth_pct",
    "breakout_proximity", "consolidation_score",
    "swing_high", "swing_low",
    "support_level", "resistance_level", "next_resistance_level",
    "base_high", "base_low",
    "range_width_pct", "range_duration", "range_tightness_score",
    "volume_dry_up_score", "volume_expansion_score",
    "relative_strength_vs_spy", "sector_relative_strength",
    "market_regime", "days_to_earnings_bd", "macro_event_risk_flag",
    "open_raw", "high_raw", "low_raw", "close_raw", "close_adj",
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
    " estimated_rr, target_is_structural, entry_price_raw, stop_price_raw, "
    " target_price_raw, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

# Full DDL column set (fix 8 parity — was missing risk_score/risk_reasons/
# price-level/support-resistance/market-context columns; those values are
# already produced by Step5ProposalEngine._build_rows so writing them is a
# pure plumbing fix, not a new computation).
_INSERT_SIM_STEP5: Final[str] = (
    "INSERT INTO sim_step5_proposals "
    "(proposal_id, sim_run_id, fold_id, setup_config_id, ticker, "
    " signal_date, setup_type, setup_score, risk_score, risk_label, "
    " risk_reasons, disposition, entry_price_raw, stop_price_raw, "
    " target_price_raw, estimated_rr, target_is_structural, "
    " support_level, resistance_level, next_resistance_level, "
    " market_regime, earnings_days, "
    " proposal_score_raw, diversity_penalty, proposal_score_final, "
    " raw_rank, diversified_rank, in_raw_top_n, in_diversified_top_n, "
    " diversification_applied, selected_top_n, selected_flag, "
    " rejection_reason, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
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
class SimulationEngine:
    """Setup-mode simulation engine.

    Replays Step 3 (``step3_universal_eligibility``) / Step 4
    (``m14_setup_validators``) / Step 5 (``step5_proposal_engine``) pure
    scoring functions against historical prod data, for one or more
    ``(setup_config_id, risk_label_config_id)`` variants (each ``config_id`` in
    ``run(config_ids=...)`` names one variant; its ``risk_label_config`` block
    lives nested in ``setup_configs[config_id]``). Step 3 runs once per
    ``sim_date`` regardless of variant count; Step 4/5 iterate per variant
    against that shared Step 3 output (see ``M17_SIMULATION_ENGINE_CONFIG_DELTA.md``).

    The optional ``db_manager`` argument exists only for test injection; when
    ``None`` the approved :mod:`app.database.duckdb_manager` is used.

    ``fold_planner`` and ``embargo_bd`` are the walk-forward extension seam:
    ``fold_planner`` defaults to :func:`plan_walk_forward_folds` (Option B,
    replay-all) but can be swapped for a different fold-generation strategy
    (e.g. CPCV) without touching any replay method — every replay/outcome/
    metric method receives an already-materialized ``folds: list[dict]``, it
    never calls a fold planner itself. ``embargo_bd`` (default
    :data:`DEFAULT_EMBARGO_BD`) is the number of trading days immediately
    before each fold's ``test_start`` excluded from that fold's train metrics,
    so a signal whose outcome-realization window overlaps the test period
    cannot leak forward-looking information into training metrics.
    """

    def __init__(
        self,
        db_manager: _DbManagerLike | None = None,
        fold_planner: Callable[[date, date], list[dict[str, Any]]] = plan_walk_forward_folds,
        embargo_bd: int = DEFAULT_EMBARGO_BD,
    ) -> None:
        self._db_override: _DbManagerLike | None = db_manager
        self._fold_planner = fold_planner
        self._embargo_bd = embargo_bd

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
                self._fold_planner(start_date, end_date)
                if mode == MODE_WALK_FORWARD
                else []
            )
            folds_count = len(folds)
            fold_ids = {f["fold_number"]: str(uuid.uuid4()) for f in folds}

            # Universe block must be identical across every variant replayed in
            # this run (mirrors step3_universal_eligibility's prod invariant) —
            # checked once for the whole run, not per sim_date.
            s3 = self._step3_module()
            universe_cfg = s3._parse_universe_config(
                s3._assert_universe_parity([setup_configs[cid] for cid in config_ids])
            )

            # --- all replay / write work in one transaction. ----------------- #
            connection.execute("BEGIN TRANSACTION")
            tx_started = True

            counters = {
                "step3_rows": 0,
                "step4_rows": 0,
                "step5_rows": 0,
                "outcomes_written": 0,
            }
            all_outcomes: list[dict[str, Any]] = []
            sector_industry = self._sector_industry_map(connection)
            slippage_by_config = {
                cid: self._slippage_bps(setup_configs[cid]) for cid in config_ids
            }

            for sim_date in sim_dates:
                fold = self._fold_for_date(folds, sim_date)
                fold_id = fold_ids.get(fold["fold_number"]) if fold else None

                # Step 3 — once per sim_date, shared across every variant.
                step3 = self._replay_step3(
                    connection,
                    run_id=run_id,
                    fold_id=fold_id,
                    sim_date=sim_date,
                    feature_version=feature_version,
                    universe_cfg=universe_cfg,
                )
                counters["step3_rows"] += step3["step3_rows"]

                # Feature+price frame — one read per sim_date, shared across
                # every variant's Step 4/5 evaluation (no re-query per variant).
                features_map = self._read_features_prices(
                    connection, sim_date, feature_version
                )

                for config_id in config_ids:
                    config = setup_configs[config_id]

                    replay = self._replay_date(
                        connection,
                        run_id=run_id,
                        fold_id=fold_id,
                        config_id=config_id,
                        config=config,
                        sim_date=sim_date,
                        routed_candidates=step3["candidates"],
                        features_map=features_map,
                        sector_industry=sector_industry,
                    )
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
                        slippage_bps=slippage_by_config[config_id],
                        proposals=replay["proposals"],
                        stop_by_ticker=replay["stop_by_ticker"],
                    )
                    for o in outcomes:
                        connection.execute(_INSERT_SIM_OUTCOME, self._outcome_params(o))
                        counters["outcomes_written"] += 1
                        all_outcomes.append(o)

            if mode == MODE_WALK_FORWARD:
                self._write_folds(
                    connection, run_id, folds, fold_ids, all_outcomes,
                    cal=cal, embargo_bd=self._embargo_bd,
                )

            comparisons_written = self._write_comparisons(
                connection, run_id, mode, config_ids, all_outcomes,
                setup_configs=setup_configs,
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
    # Frozen setup-mode engine imports — lazy so the pure helpers and
    # pre-DB validation stay unit-testable without duckdb/polars installed.
    # Per M17_SIMULATION_ENGINE_CONFIG_DELTA.md: only these pure functions are
    # called, never Step3UniversalEligibilityEngine.run() /
    # Step4SetupValidationEngine.run() (I/O orchestration wrappers that would
    # write prod/debug and break sim DB isolation).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _step3_module() -> Any:
        from app.services.screening import step3_universal_eligibility
        return step3_universal_eligibility

    @staticmethod
    def _m14_validators_module() -> Any:
        from app.services.screening import m14_setup_validators
        return m14_setup_validators

    @staticmethod
    def _step5_module() -> Any:
        from app.services.proposal import step5_proposal_engine
        return step5_proposal_engine

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

    def _read_features_prices(
        self, connection: Any, sim_date: date, feature_version: str | None
    ) -> dict[str, dict[str, Any]]:
        """Read one sim_date's feature+price frame, keyed by ticker.

        Shared by every variant's Step 4 (``validate_setup``) and Step 5
        (``_build_rows``) evaluation for this date — read once, not re-queried
        per ``(setup_config_id, risk_label_config_id)`` variant.
        """
        rows = connection.execute(
            _SELECT_FEATURES_PRICES, [sim_date, sim_date, sim_date, feature_version]
        ).fetchall()
        return {
            r[0]: dict(zip(_STEP4_FEATURE_COLS, r))
            for r in rows
            if r[0] is not None
        }

    # ------------------------------------------------------------------ #
    # Step 3 — once per sim_date, shared across every variant.
    # ------------------------------------------------------------------ #
    def _replay_step3(
        self,
        connection: Any,
        *,
        run_id: str,
        fold_id: str | None,
        sim_date: date,
        feature_version: str | None,
        universe_cfg: tuple[float, float, list[str], frozenset[str]],
    ) -> dict[str, Any]:
        """Replay Step 3 (universal eligibility + routing) for one sim_date.

        Writes ``sim_step3_candidates`` (config-independent — the table has no
        ``setup_config_id`` column) and returns the in-memory routed candidate
        list so Step 4 can filter by ``routed_setup_types`` without a re-read.
        """
        s3 = self._step3_module()
        min_price, min_adv, allowed_types, merger_watch_list = universe_cfg

        rows = connection.execute(
            _SELECT_UNIVERSE_STEP3,
            [sim_date, sim_date, sim_date, sim_date, feature_version],
        ).fetchall()
        raw_rows = [dict(zip(_STEP3_COL_NAMES, r)) for r in rows]

        eligible_dvols = [
            r["avg_dollar_volume_20d"] for r in raw_rows
            if r["avg_dollar_volume_20d"] is not None and r["close_raw"] is not None
        ]
        eligible_prices = [
            r["close_raw"] for r in raw_rows
            if r["close_raw"] is not None and r["avg_dollar_volume_20d"] is not None
        ]

        candidates: list[dict[str, Any]] = []
        for row in raw_rows:
            reasons = s3._check_eligibility(row, min_price, min_adv, allowed_types, merger_watch_list)
            passed = not reasons
            if passed:
                routed = s3._evaluate_routing(row)
                routing_status = s3.ROUTING_ROUTED if routed else s3.ROUTING_NO_ROUTE
                routing_fail_reason = None if routed else s3.ROUTING_FAIL_NO_ROUTE
                eligibility_score = s3._compute_eligibility_score(row, eligible_dvols, eligible_prices)
            else:
                routed = []
                routing_status = s3.ROUTING_INELIGIBLE
                routing_fail_reason = reasons[0] if reasons else None
                eligibility_score = None

            candidates.append({
                "candidate_id": str(uuid.uuid4()),
                "ticker": row["ticker"],
                "eligibility_score": eligibility_score,
                "passed_eligibility": passed,
                "routing_status": routing_status,
                "routing_fail_reason": routing_fail_reason,
                "eligibility_fail_reasons": reasons,
                "routed_setup_types": routed,
            })

        for c in candidates:
            connection.execute(
                _INSERT_SIM_STEP3,
                [
                    c["candidate_id"], run_id, fold_id, c["ticker"], sim_date,
                    c["eligibility_score"], c["passed_eligibility"],
                    c["routing_status"], c["routing_fail_reason"],
                    json.dumps(c["eligibility_fail_reasons"]),
                    json.dumps(c["routed_setup_types"]),
                ],
            )

        return {"step3_rows": len(candidates), "candidates": candidates}

    # ------------------------------------------------------------------ #
    # Step 4/5 — per variant, against the shared Step 3 output for this date.
    # ------------------------------------------------------------------ #
    def _replay_date(
        self,
        connection: Any,
        *,
        run_id: str,
        fold_id: str | None,
        config_id: str,
        config: dict,
        sim_date: date,
        routed_candidates: list[dict[str, Any]],
        features_map: dict[str, dict[str, Any]],
        sector_industry: dict[str, tuple[Any, Any]],
    ) -> dict[str, Any]:
        """Replay Step 4 (``validate_setup``) + Step 5 (``_build_rows``) for one
        ``(sim_date, config_id)`` variant, given the date's shared Step 3
        routing output and shared feature+price frame.

        Writes ``sim_step4_analysis`` / ``sim_step5_proposals`` rows and
        returns ``step4_rows`` / ``step5_rows`` counts plus ``proposals`` /
        ``stop_by_ticker`` for ``_build_outcomes``.
        """
        m14 = self._m14_validators_module()
        step5_mod = self._step5_module()
        sig_iso = sim_date.isoformat()

        setup_type = config.get("setup_type", "")
        risk_cfg_raw = config.get("risk_label_config")
        if not isinstance(risk_cfg_raw, dict):
            raise _ValidationError(
                f"config_id {config_id!r} is missing a 'risk_label_config' block "
                "(each sim variant pairs one setup_config with one risk_label_config)."
            )
        parsed_risk_cfg = step5_mod._parse_risk_label_config(risk_cfg_raw)

        analyses: list[dict[str, Any]] = []
        s3 = self._step3_module()
        for candidate in routed_candidates:
            if candidate["routing_status"] != s3.ROUTING_ROUTED:
                continue
            if setup_type not in candidate["routed_setup_types"]:
                continue
            ticker = candidate["ticker"]
            feat = features_map.get(ticker)
            if feat is None:
                continue
            feat_for_validator = {**feat, "ticker": ticker, "signal_date": sig_iso}
            result = m14.validate_setup(setup_type, feat_for_validator, config)

            analysis_id = str(uuid.uuid4())
            sector, industry = sector_industry.get(ticker, (None, None))
            analyses.append({
                "analysis_id": analysis_id,
                "candidate_id": candidate["candidate_id"],
                "setup_config_id": result.setup_config_id,
                "ticker": ticker,
                "signal_date": sim_date,
                "setup_type": result.setup_type,
                "setup_score": result.setup_score,
                "setup_passed": result.setup_passed,
                "setup_reasons": result.pass_fail_reasons,
                "setup_fail_reason": result.setup_fail_reason,
                "entry_price_raw": result.entry_price_raw,
                "support_level": result.support_level_raw,
                "resistance_level": result.resistance_level_raw,
                "next_resistance_level": result.next_resistance_level_raw,
                "atr_pct": result.atr_pct,
                "distance_to_ema20_pct": result.distance_to_ema20_pct,
                "distance_to_ema50_pct": result.distance_to_ema50_pct,
                "rvol": result.rvol,
                "earnings_days": result.earnings_days,
                "market_regime": result.market_regime,
                "earnings_penalty": result.earnings_penalty,
                "macro_penalty": result.macro_penalty,
                "explanation_json": None,
                "target_is_structural": result.target_is_structural,
                "sector": sector,
                "industry": industry,
            })

        for a in analyses:
            connection.execute(
                _INSERT_SIM_STEP4,
                [
                    a["analysis_id"], a["candidate_id"], run_id, fold_id,
                    config_id, a["ticker"], sim_date, a["setup_type"],
                    a["setup_score"], a["setup_passed"],
                    None,  # estimated_rr — Phase 5 only, always NULL from Step 4
                    a["target_is_structural"],
                    a["entry_price_raw"],
                    None,  # stop_price_raw — Phase 5 only
                    None,  # target_price_raw — Phase 5 only
                ],
            )

        rows = step5_mod.Step5ProposalEngine()._build_rows(
            analyses, features_map, {setup_type: config}, parsed_risk_cfg,
            run_id, sim_date,
        )

        for row in rows:
            connection.execute(_INSERT_SIM_STEP5, self._step5_insert_params(fold_id, row))

        proposals = [
            {
                "proposal_id": row["proposal_id"],
                "ticker": row["ticker"],
                "setup_type": row["setup_type"],
                "risk_label": row["risk_label"],
                "target_price_raw": row["target_price_raw"],
                "in_raw_top_n": row["in_raw_top_n"],
                "in_diversified_top_n": row["in_diversified_top_n"],
            }
            for row in rows
        ]
        stop_by_ticker = {row["ticker"]: row["stop_price_raw"] for row in rows}

        return {
            "step4_rows": len(analyses),
            "step5_rows": len(rows),
            "proposals": proposals,
            "stop_by_ticker": stop_by_ticker,
        }

    @staticmethod
    def _step5_insert_params(fold_id: str | None, row: dict[str, Any]) -> list[Any]:
        return [
            row["proposal_id"], row["run_id"], fold_id, row["setup_config_id"],
            row["ticker"], row["signal_date"], row["setup_type"],
            row["setup_score"], row["risk_score"], row["risk_label"],
            row["risk_reasons"], row["disposition"],
            row["entry_price_raw"], row["stop_price_raw"], row["target_price_raw"],
            row["estimated_rr"], row["target_is_structural"],
            row["support_level"], row["resistance_level"], row["next_resistance_level"],
            row["market_regime"], row["earnings_days"],
            row["proposal_score_raw"], row["diversity_penalty"], row["proposal_score_final"],
            row["raw_rank"], row["diversified_rank"],
            row["in_raw_top_n"], row["in_diversified_top_n"],
            row["diversification_applied"], row["selected_top_n"], row["selected_flag"],
            row["rejection_reason"],
        ]

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
        *,
        cal: Any = None,
        embargo_bd: int = 0,
    ) -> None:
        """Select the best config per fold and insert ``sim_folds`` rows.

        ``cal`` / ``embargo_bd`` are optional (default off) so pre-existing
        callers that only pass the first five positional args keep their
        exact pre-embargo behavior; the engine's own run loop always supplies
        both (see ``_run_with_connection``).
        """
        for fold in folds:
            metrics_by_config = self._fold_train_metrics(
                fold, all_outcomes, cal=cal, embargo_bd=embargo_bd
            )
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
    def _embargo_cutoff(cal: Any, fold: dict[str, Any], embargo_bd: int) -> date:
        """Trading day at/after which train-window signals are embargoed.

        The last ``embargo_bd`` trading sessions immediately before
        ``fold["test_start"]`` are excluded from that fold's training metrics,
        so a signal near the train/test boundary whose outcome-realization
        window overlaps the test period cannot leak forward-looking
        information into training metrics. Falls back to ``train_start``
        (embargo the whole train window) when it is shorter than
        ``embargo_bd`` sessions.
        """
        pretest = cal.trading_days_between(
            fold["train_start"], fold["test_start"] - _dt.timedelta(days=1)
        )
        if embargo_bd <= 0 or len(pretest) <= embargo_bd:
            return fold["train_start"]
        return pretest[-embargo_bd]

    @staticmethod
    def _fold_train_metrics(
        fold: dict[str, Any],
        all_outcomes: list[dict[str, Any]],
        *,
        cal: Any = None,
        embargo_bd: int = 0,
    ) -> dict[str, dict[str, float | None]]:
        """Per-config training metrics for ``fold`` (diversified list, 40bd).

        Train outcomes are those whose ``signal_date`` falls in the fold's train
        window, excluding ``cross_fold_outcome = TRUE`` rows and (when
        ``embargo_bd > 0`` and ``cal`` supplied) rows inside the embargo window
        immediately before ``test_start``. Ordered by ``signal_date`` so the
        drawdown curve is chronological.
        """
        embargo_cutoff = (
            SimulationEngine._embargo_cutoff(cal, fold, embargo_bd)
            if embargo_bd > 0 and cal is not None
            else None
        )
        by_config: dict[str, list[tuple[date, float | None]]] = {}
        for o in all_outcomes:
            if o["cross_fold_outcome"]:
                continue
            if not (fold["train_start"] <= o["signal_date"] <= fold["train_end"]):
                continue
            if embargo_cutoff is not None and o["signal_date"] >= embargo_cutoff:
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
        *,
        setup_configs: dict[str, dict] | None = None,
    ) -> int:
        """Insert one ``sim_config_comparisons`` row per (config, horizon, list_type).

        For ``walk_forward`` only non-cross-fold outcomes are aggregated; for the
        other modes every outcome counts. Metrics are computed per list type
        (``raw`` includes ``raw_only`` + ``both``; ``diversified`` includes
        ``diversified_only`` + ``both``).

        ``setup_configs`` (optional) supplies ``setup_type`` per ``config_id`` —
        ``sim_config_comparisons.setup_type`` is ``NOT NULL`` in the schema.
        Defaults to ``None`` per config_id when omitted, so pre-existing direct
        callers of this method keep working; the engine's own run loop always
        supplies it (see ``_run_with_connection``).
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
            cfg_setup_type = (
                (setup_configs or {}).get(config_id, {}).get("setup_type")
            )
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
                            cfg_setup_type, None,  # risk_label: aggregate comparison row
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
