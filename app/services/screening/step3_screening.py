"""Module 13 — Step 3 Screening.

Reads ``daily_features_current`` for one ``signal_date``, joins ``ticker_master``
(symbol type) and ``daily_prices`` (close + ``data_quality_status``), applies the
Step 3 **hard filters** and, for the passing rows, the Step 3 **soft score**, then
appends *every* evaluated row — passed and failed — into ``step3_candidates`` in a
single transaction. It runs after Module 12 (Market Regime Engine) and before
Module 14 (Step 4 Setup Analysis).

Contract source of truth: ``M13_STEP3_SCREENING_SPEC.md`` (derived from the frozen
split Project Files — ``01a_CORE_PRINCIPLES.md`` for guardrails / enums,
``01b_SCHEMA_AND_DATA.md`` for the ``step3_candidates`` / ``daily_features`` /
``daily_prices`` / ``ticker_master`` schema and the ``daily_features_current``
view, ``01c_FORMULAS_AND_CONFIGS.md`` §``FORMULAS/61_Scoring_Formulas_Step3.md``
for the hard filters and sub-scores, ``01d_MODULES_AND_PIPELINE.md`` for the
pipeline position, ``02_PROJECT_IMPLEMENTATION_CONTEXT.md`` for the Polars-first /
DB-boundary / logging rules, and the Module 12 spec for the ``db_role`` / service
style). Under-specified scoring details that the frozen sources leave open
(``scoring_weights`` config path, the ``distance_to_ema50`` linear taper, the
``breakout_proximity`` mid-band, and the missing ``avg_dollar_volume_20d`` volume
sub-score) are recorded as gaps and closed conservatively in the spec.

Scope
-----
For one ``signal_date`` it:

- reads every ``daily_features_current`` row for ``feature_date == signal_date``,
  left-joining ``ticker_master`` (``symbol_type``) and ``daily_prices``
  (``close_raw`` / ``close_adj`` / ``data_quality_status``) on
  ``(ticker, date == feature_date)``;
- evaluates all six hard filters per row, collecting *all* failing labels into a
  deterministic ``hard_filter_fail_reasons`` JSON array (NULL / missing inputs fail
  the related filter);
- scores only the rows that pass every hard filter, using the Step 3 weighted
  sub-scores (all clamped to 0–100) and the caller's ``scoring_weights``;
- writes one ``step3_candidates`` row per evaluated ticker (passed rows carry a
  non-null ``screening_score`` and ``hard_filter_fail_reasons == []``; failed rows
  carry ``screening_score == NULL`` and a populated reasons array), in a single
  per-row ``execute()`` calls inside a single ``BEGIN TRANSACTION / COMMIT``; and
- returns a :class:`~app.utils.service_result.ServiceResult`.

This module only ever *inserts* into ``step3_candidates``. It never updates or
deletes existing ``step3_candidates`` rows, never writes any other table, never
runs DDL, never calls providers, never imports ``duckdb`` directly, never uses
``ATTACH``, never bypasses the DuckDB manager, and never uses ``print()``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Final, Protocol

import polars as pl

from app.config import constants
from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult


# --------------------------------------------------------------------------- #
# Roles this module is allowed to target.
# --------------------------------------------------------------------------- #
# ``simulation`` is a valid duckdb_manager role but is *forbidden* here: Module 13
# never writes to ``simulation.duckdb``. Only ``prod`` / ``debug`` are accepted;
# any other value yields a ``failed`` result with no reads/writes.
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# daily_prices.data_quality_status value that satisfies the data-quality filter.
STATUS_OK: Final[str] = "ok"

# symbol_type required by the Step 3 hard filter.
SYMBOL_TYPE_STOCK: Final[str] = constants.SYMBOL_TYPE_STOCK

# --------------------------------------------------------------------------- #
# Hard-filter labels (deterministic; the order here is the order in which a
# failing row's reasons are emitted).
# --------------------------------------------------------------------------- #
REASON_FEATURE_NOT_READY: Final[str] = "feature_not_ready"
REASON_NOT_STOCK: Final[str] = "not_stock"
REASON_PRICE_BELOW_MIN: Final[str] = "price_below_min"
REASON_ADV_BELOW_MIN: Final[str] = "avg_dollar_volume_below_min"
REASON_RVOL_BELOW_MIN: Final[str] = "rvol_below_min"
REASON_DATA_QUALITY_NOT_OK: Final[str] = "data_quality_not_ok"

# Ordered (label, pass-flag-column) pairs. The pass-flag columns are built as
# Polars boolean expressions in :func:`_hard_filter_flag_exprs`.
_HARD_FILTER_ORDER: Final[tuple[tuple[str, str], ...]] = (
    (REASON_FEATURE_NOT_READY, "_pass_feature_ready"),
    (REASON_NOT_STOCK, "_pass_is_stock"),
    (REASON_PRICE_BELOW_MIN, "_pass_price"),
    (REASON_ADV_BELOW_MIN, "_pass_adv"),
    (REASON_RVOL_BELOW_MIN, "_pass_rvol"),
    (REASON_DATA_QUALITY_NOT_OK, "_pass_data_quality"),
)

# --------------------------------------------------------------------------- #
# Market-regime → market sub-score mapping (FORMULAS/61). Any regime value not in
# this map (including NULL / unknown) scores 0.0 (not neutral 50.0) and is
# recorded in ``soft_score_components`` (prompt rule). Unknown market regime
# is treated as a data gap, not a neutral condition.
# --------------------------------------------------------------------------- #
MARKET_SCORE_BY_REGIME: Final[dict[str, float]] = {
    constants.REGIME_BULL: 100.0,
    constants.REGIME_NEUTRAL: 60.0,
    constants.REGIME_BEAR: 20.0,
    constants.REGIME_HIGH_RISK: 0.0,
    constants.REGIME_EXTREME_RISK: 0.0,
}
MARKET_SCORE_UNKNOWN: Final[float] = 0.0

# distance_to_ema50 ideal band (FORMULAS/61) and the documented linear-taper width
# used outside the band (gap G-EMA50-TAPER, closed in the spec): the sub-score
# falls linearly from 100 to 0 across this many fractional units beyond each edge.
_EMA50_BAND_LOW: Final[float] = -0.03
_EMA50_BAND_HIGH: Final[float] = 0.08
_EMA50_TAPER_WIDTH: Final[float] = 0.10

# scoring_weights sub-keys (Project Files place ``scoring_weights`` at config top
# level — see gap G-WEIGHTS-PATH in the spec).
_WEIGHT_KEYS: Final[tuple[str, ...]] = (
    "trend",
    "momentum",
    "setup",
    "volume",
    "market",
)

# The exact metadata key set returned on every return path.
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "signal_date",
    "strategy_config_id",
    "run_id",
    "tickers_evaluated",
    "passed_hard_filters",
    "failed_hard_filters",
    "candidates_written",
    "screening_score_min",
    "screening_score_max",
    "screening_score_mean",
)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for test injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class _ConfigError(ValueError):
    """Raised internally when ``strategy_config`` is missing a required key."""


# --------------------------------------------------------------------------- #
# SQL (operates only on existing objects; no DDL).
# --------------------------------------------------------------------------- #
# Read every current-schema feature row for the signal date, left-joining the
# ticker's symbol_type and that date's price row (close + quality). Left joins so
# rows with no ticker_master / daily_prices match still appear and fail the
# relevant hard filter rather than vanishing.
_SELECT_SCREENING_INPUT: Final[str] = (
    "SELECT "
    "  f.ticker AS ticker, "
    "  f.feature_date AS feature_date, "
    "  f.feature_ready AS feature_ready, "
    "  f.ema20 AS ema20, "
    "  f.ema50 AS ema50, "
    "  f.ema200 AS ema200, "
    "  f.ema_alignment_score AS ema_alignment_score, "
    "  f.distance_to_ema50_pct AS distance_to_ema50_pct, "
    "  f.rsi14 AS rsi14, "
    "  f.roc20 AS roc20, "
    "  f.rvol20 AS rvol20, "
    "  f.avg_dollar_volume_20d AS avg_dollar_volume_20d, "
    "  f.breakout_proximity AS breakout_proximity, "
    "  f.pullback_from_recent_high_pct AS pullback_from_recent_high_pct, "
    "  f.consolidation_score AS consolidation_score, "
    "  f.sector_relative_strength AS sector_relative_strength, "
    "  f.market_regime AS market_regime, "
    "  tm.symbol_type AS symbol_type, "
    "  p.close_raw AS close_raw, "
    "  p.close_adj AS close_adj, "
    "  p.data_quality_status AS data_quality_status "
    "FROM daily_features_current f "
    "LEFT JOIN ticker_master tm ON tm.ticker = f.ticker "
    "LEFT JOIN daily_prices p ON p.ticker = f.ticker AND p.date = f.feature_date "
    "WHERE f.feature_date = ? "
    "ORDER BY f.ticker"
)

# Input frame schema (column order matches the SELECT above).
_INPUT_SCHEMA: Final[list[tuple[str, Any]]] = [
    ("ticker", pl.Utf8),
    ("feature_date", pl.Date),
    ("feature_ready", pl.Boolean),
    ("ema20", pl.Float64),
    ("ema50", pl.Float64),
    ("ema200", pl.Float64),
    ("ema_alignment_score", pl.Float64),
    ("distance_to_ema50_pct", pl.Float64),
    ("rsi14", pl.Float64),
    ("roc20", pl.Float64),
    ("rvol20", pl.Float64),
    ("avg_dollar_volume_20d", pl.Float64),
    ("breakout_proximity", pl.Float64),
    ("pullback_from_recent_high_pct", pl.Float64),
    ("consolidation_score", pl.Float64),
    ("sector_relative_strength", pl.Float64),
    ("market_regime", pl.Utf8),
    ("symbol_type", pl.Utf8),
    ("close_raw", pl.Float64),
    ("close_adj", pl.Float64),
    ("data_quality_status", pl.Utf8),
]

# Parameterized single-row INSERT. Each row gets its own execute() call inside
# one BEGIN TRANSACTION / COMMIT (per-row execute, not executemany).
_INSERT_CANDIDATE: Final[str] = (
    "INSERT INTO step3_candidates "
    "(candidate_id, run_id, strategy_config_id, ticker, signal_date, "
    " screening_score, passed_hard_filters, hard_filter_fail_reasons, "
    " soft_score_components, feature_snapshot_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))"
)

# feature_snapshot_json keys (deterministic order is enforced by sort_keys).
_SNAPSHOT_FIELDS: Final[tuple[str, ...]] = (
    "ema20",
    "ema50",
    "ema200",
    "ema_alignment_score",
    "rsi14",
    "roc20",
    "rvol20",
    "avg_dollar_volume_20d",
    "breakout_proximity",
    "pullback_from_recent_high_pct",
    "consolidation_score",
    "sector_relative_strength",
    "market_regime",
    "close_raw",
)


# --------------------------------------------------------------------------- #
# Pure helpers (JSON / floats).
# --------------------------------------------------------------------------- #
def _json_dumps(payload: Any) -> str:
    """Serialise ``payload`` deterministically (sorted keys, compact-ish)."""
    return json.dumps(payload, sort_keys=True)


def _f(value: Any) -> float | None:
    """Coerce a Polars cell to ``float`` or ``None`` (NaN -> ``None``)."""
    if value is None:
        return None
    fv = float(value)
    if fv != fv:  # NaN
        return None
    return fv


# --------------------------------------------------------------------------- #
# Polars sub-score expressions (vectorized; no per-ticker Python loops).
# --------------------------------------------------------------------------- #
def _clamp(expr: pl.Expr) -> pl.Expr:
    """Clamp a sub-score expression to ``[0, 100]`` (NULL stays NULL)."""
    return expr.clip(0.0, 100.0)


def _trend_score_expr() -> pl.Expr:
    """Trend sub-score: 50% ema_alignment_score + 25% distance_to_ema50 band
    + 25% close-above-EMA200, clamped 0–100.

    The distance_to_ema50 component scores 100 inside ``[-3%, +8%]`` and tapers
    linearly to 0 across a documented :data:`_EMA50_TAPER_WIDTH` window beyond each
    edge (gap G-EMA50-TAPER). NULL inputs (other than the regime / sector
    components handled elsewhere) drive the component to 0 (assumption
    A-NULL-COMPONENT).
    """
    align = pl.col("ema_alignment_score").fill_null(0.0).clip(0.0, 100.0)

    d = pl.col("distance_to_ema50_pct")
    below = _clamp(100.0 - ((_EMA50_BAND_LOW - d) / _EMA50_TAPER_WIDTH) * 100.0)
    above = _clamp(100.0 - ((d - _EMA50_BAND_HIGH) / _EMA50_TAPER_WIDTH) * 100.0)
    dist_score = (
        pl.when(d.is_null())
        .then(0.0)
        .when((d >= _EMA50_BAND_LOW) & (d <= _EMA50_BAND_HIGH))
        .then(100.0)
        .when(d < _EMA50_BAND_LOW)
        .then(below)
        .otherwise(above)
    )

    # "close above EMA200" — use coalesce(close_adj, close_raw) to match the
    # adjusted EMA basis (assumption A-CLOSE-EMA200).
    close_used = pl.coalesce([pl.col("close_adj"), pl.col("close_raw")])
    above_200 = (
        pl.when(close_used.is_null() | pl.col("ema200").is_null())
        .then(0.0)
        .when(close_used > pl.col("ema200"))
        .then(100.0)
        .otherwise(0.0)
    )

    return _clamp(0.50 * align + 0.25 * dist_score + 0.25 * above_200)


def _momentum_score_expr() -> pl.Expr:
    """Momentum sub-score: 40% RSI14 + 30% ROC20 + 30% sector_relative_strength."""
    rsi = pl.col("rsi14")
    rsi_score = (
        pl.when(rsi.is_null())
        .then(0.0)
        .when((rsi >= 50.0) & (rsi <= 65.0))
        .then(100.0)
        .when(((rsi >= 45.0) & (rsi < 50.0)) | ((rsi > 65.0) & (rsi <= 70.0)))
        .then(70.0)
        .otherwise(30.0)
    )

    roc = pl.col("roc20")
    roc_score = (
        pl.when(roc.is_null())
        .then(0.0)
        .when(roc > 0.08)
        .then(100.0)
        .when((roc >= 0.03) & (roc <= 0.08))
        .then(70.0)
        .when((roc >= 0.0) & (roc < 0.03))
        .then(30.0)
        .otherwise(0.0)
    )

    srs = pl.col("sector_relative_strength")
    srs_score = (
        pl.when(srs.is_null())
        .then(50.0)  # explicit neutral-50 for missing sector RS (FORMULAS/61)
        .when(srs > 0.05)
        .then(100.0)
        .when((srs >= 0.0) & (srs <= 0.05))
        .then(70.0)
        .when((srs >= -0.05) & (srs < 0.0))
        .then(30.0)
        .otherwise(0.0)
    )

    return _clamp(0.40 * rsi_score + 0.30 * roc_score + 0.30 * srs_score)


def _setup_score_expr() -> pl.Expr:
    """Setup sub-score: 40% consolidation_score + 30% breakout_proximity band
    + 30% pullback band.

    The breakout_proximity mid-band ``(0.5, 1.5]`` is left open by the frozen
    sources; it is scored 30 (gap G-BP-MIDBAND, closed in the spec).
    """
    cons = pl.col("consolidation_score").fill_null(0.0).clip(0.0, 100.0)

    bp = pl.col("breakout_proximity")
    bp_score = (
        pl.when(bp.is_null())
        .then(0.0)
        .when((bp >= -1.0) & (bp <= 0.5))
        .then(100.0)
        .when((bp >= -2.0) & (bp < -1.0))
        .then(70.0)
        .when(bp < -2.0)
        .then(30.0)
        .when(bp > 1.5)
        .then(20.0)
        .otherwise(30.0)  # (0.5, 1.5] documented gap-closure
    )

    pb = pl.col("pullback_from_recent_high_pct")
    pb_score = (
        pl.when(pb.is_null())
        .then(0.0)
        .when((pb >= -0.12) & (pb <= -0.03))
        .then(100.0)
        .when((pb >= -0.20) & (pb < -0.12))
        .then(70.0)
        .otherwise(30.0)
    )

    return _clamp(0.40 * cons + 0.30 * bp_score + 0.30 * pb_score)


def _volume_score_expr() -> pl.Expr:
    """Volume sub-score.

    FORMULAS/61 weights this 60% RVOL + 40% avg_dollar_volume_20d but provides no
    avg_dollar_volume_20d sub-score mapping (gap G-VOL-ADV). Per the prompt's
    "implement only the explicitly supported subset" rule the dollar-volume
    component is omitted and the volume sub-score is the RVOL sub-score alone.
    """
    rvol = pl.col("rvol20")
    rvol_score = (
        pl.when(rvol.is_null())
        .then(0.0)
        .when(rvol >= 2.0)
        .then(100.0)
        .when((rvol >= 1.5) & (rvol < 2.0))
        .then(70.0)
        .when((rvol >= 1.2) & (rvol < 1.5))
        .then(40.0)
        .otherwise(0.0)
    )
    return _clamp(rvol_score)


def _market_score_expr() -> pl.Expr:
    """Market sub-score from ``market_regime``; unknown / NULL -> 0.0 (data gap).

    Unknown regime is not neutral — it is an absent signal and scores 0.
    """
    expr = pl.col("market_regime")
    out = pl.lit(MARKET_SCORE_UNKNOWN, dtype=pl.Float64)
    for regime, score in MARKET_SCORE_BY_REGIME.items():
        out = pl.when(expr == regime).then(pl.lit(score, dtype=pl.Float64)).otherwise(out)
    return out


def _hard_filter_flag_exprs(
    min_price: float, min_adv: float, min_rvol: float
) -> list[pl.Expr]:
    """Return the six per-row hard-filter PASS boolean expressions.

    A flag is ``True`` only when the filter passes; NULL / missing inputs fail
    (yield ``False``).
    """
    return [
        pl.col("feature_ready").fill_null(False).alias("_pass_feature_ready"),
        (pl.col("symbol_type") == SYMBOL_TYPE_STOCK)
        .fill_null(False)
        .alias("_pass_is_stock"),
        (pl.col("close_raw") >= min_price).fill_null(False).alias("_pass_price"),
        (pl.col("avg_dollar_volume_20d") >= min_adv)
        .fill_null(False)
        .alias("_pass_adv"),
        (pl.col("rvol20") >= min_rvol).fill_null(False).alias("_pass_rvol"),
        (pl.col("data_quality_status") == STATUS_OK)
        .fill_null(False)
        .alias("_pass_data_quality"),
    ]


# --------------------------------------------------------------------------- #
# Step 3 screening engine.
# --------------------------------------------------------------------------- #
class Step3ScreeningEngine:
    """Apply Step 3 hard filters + soft scoring for one signal date.

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
    def screen(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Screen the universe for ``signal_date`` and append candidates.

        Parameters
        ----------
        signal_date:
            The feature date to screen. ``daily_features_current.feature_date``
            must equal this for a row to be considered.
        strategy_config:
            Parsed strategy-config JSON. Required keys: ``universe.min_price``,
            ``universe.min_avg_dollar_volume_20d``, ``screening.min_rvol`` and the
            top-level ``scoring_weights`` (see gap G-WEIGHTS-PATH).
        strategy_config_id:
            Opaque config id, copied to every written candidate.
        db_role:
            ``"prod"`` or ``"debug"`` only. Any other value (including
            ``"simulation"``) returns ``failed`` before any DB read/write.
        run_id:
            A fresh ``uuid4`` is minted when ``None``; a supplied value is kept.

        Returns
        -------
        ServiceResult
            ``rows_processed`` equals ``metadata["candidates_written"]`` on every
            return path. ``metadata`` carries exactly :data:`METADATA_KEYS`.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        signal_iso = signal_date.isoformat()

        log.info(
            "screen start db_role=%s signal_date=%s strategy_config_id=%s",
            db_role,
            signal_iso,
            strategy_config_id,
        )

        # --- db_role guard: prod/debug only, never simulation. No I/O. ----- #
        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 13 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("screen failed: %s", message)
            return self._failed(run_id, db_role, signal_iso, strategy_config_id, message)

        # --- config guard: validate required keys before any DB access. ---- #
        try:
            min_price, min_adv, min_rvol, weights = self._parse_config(strategy_config)
        except _ConfigError as exc:
            log.error("screen failed: %s", exc)
            return self._failed(
                run_id, db_role, signal_iso, strategy_config_id, str(exc)
            )

        # --- read phase (read-only). --------------------------------------- #
        try:
            frame = self._read(db_role, signal_date)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure as failed
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("screen failed: %s", message)
            return self._failed(
                run_id, db_role, signal_iso, strategy_config_id, message
            )

        # --- empty input: success, zero counts, no insert. ----------------- #
        if frame.height == 0:
            log.info("screen done: no daily_features_current rows for %s", signal_iso)
            return ServiceResult(
                status=service_result.STATUS_SUCCESS,
                run_id=run_id,
                rows_processed=0,
                metadata=self._metadata(
                    db_role=db_role,
                    signal_date=signal_iso,
                    strategy_config_id=strategy_config_id,
                    run_id=run_id,
                    tickers_evaluated=0,
                    passed_hard_filters=0,
                    failed_hard_filters=0,
                    candidates_written=0,
                    screening_score_min=None,
                    screening_score_max=None,
                    screening_score_mean=None,
                ),
            )

        # --- compute phase (pure Polars, no DB). --------------------------- #
        scored = self._evaluate(frame, min_price, min_adv, min_rvol, weights)
        rows = self._build_rows(scored, run_id, strategy_config_id)

        tickers_evaluated = len(rows)
        passed = sum(1 for r in rows if r["passed"])
        failed = tickers_evaluated - passed

        passed_scores = [
            r["screening_score"] for r in rows if r["passed"] and r["screening_score"] is not None
        ]
        score_min = min(passed_scores) if passed_scores else None
        score_max = max(passed_scores) if passed_scores else None
        score_mean = (sum(passed_scores) / len(passed_scores)) if passed_scores else None

        # --- write phase: per-row execute() inside one BEGIN/COMMIT transaction. #
        try:
            candidates_written = self._write(db_role, rows)
        except Exception as exc:  # noqa: BLE001 - surface as failed; rollback inside
            log.error(
                "screen failed during write (rolled back): %s: %s",
                type(exc).__name__,
                exc,
            )
            return self._failed(
                run_id,
                db_role,
                signal_iso,
                strategy_config_id,
                f"{type(exc).__name__}: {exc}",
            )

        log.info(
            "screen done status=success tickers_evaluated=%d passed=%d failed=%d "
            "candidates_written=%d",
            tickers_evaluated,
            passed,
            failed,
            candidates_written,
        )
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=candidates_written,
            metadata=self._metadata(
                db_role=db_role,
                signal_date=signal_iso,
                strategy_config_id=strategy_config_id,
                run_id=run_id,
                tickers_evaluated=tickers_evaluated,
                passed_hard_filters=passed,
                failed_hard_filters=failed,
                candidates_written=candidates_written,
                screening_score_min=score_min,
                screening_score_max=score_max,
                screening_score_mean=score_mean,
            ),
        )

    # ------------------------------------------------------------------ #
    # Config parsing / validation.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_config(
        strategy_config: dict,
    ) -> tuple[float, float, float, dict[str, float]]:
        """Validate and extract the required config values.

        Raises
        ------
        _ConfigError
            If ``strategy_config`` is not a dict or any required key is missing /
            not numeric. ``scoring_weights`` is read from the config *top level*
            (gap G-WEIGHTS-PATH) and must contain all five sub-weights.
        """
        if not isinstance(strategy_config, dict):
            raise _ConfigError("strategy_config must be a dict")

        def _num(section: str, key: str) -> float:
            block = strategy_config.get(section)
            if not isinstance(block, dict) or key not in block:
                raise _ConfigError(f"missing config key {section}.{key}")
            value = block[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _ConfigError(f"config key {section}.{key} must be numeric")
            return float(value)

        min_price = _num("universe", "min_price")
        min_adv = _num("universe", "min_avg_dollar_volume_20d")
        min_rvol = _num("screening", "min_rvol")

        weights_block = strategy_config.get("scoring_weights")
        if not isinstance(weights_block, dict):
            raise _ConfigError("missing config key scoring_weights")
        weights: dict[str, float] = {}
        for key in _WEIGHT_KEYS:
            if key not in weights_block:
                raise _ConfigError(f"missing config key scoring_weights.{key}")
            value = weights_block[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _ConfigError(f"config key scoring_weights.{key} must be numeric")
            weights[key] = float(value)

        return min_price, min_adv, min_rvol, weights

    # ------------------------------------------------------------------ #
    # Read phase.
    # ------------------------------------------------------------------ #
    def _read(self, db_role: str, signal_date: date) -> pl.DataFrame:
        """Read the joined screening input frame for ``signal_date`` (read-only).

        Only the columns needed for filtering / scoring / the snapshot are
        selected. The read connection is closed before any computation.
        """
        connection = self._db.connect(db_role)
        try:
            input_rows = connection.execute(
                _SELECT_SCREENING_INPUT, [signal_date]
            ).fetchall()
        finally:
            connection.close()

        return pl.DataFrame(input_rows, schema=_INPUT_SCHEMA, orient="row")

    # ------------------------------------------------------------------ #
    # Compute phase.
    # ------------------------------------------------------------------ #
    def _evaluate(
        self,
        frame: pl.DataFrame,
        min_price: float,
        min_adv: float,
        min_rvol: float,
        weights: dict[str, float],
    ) -> pl.DataFrame:
        """Attach hard-filter flags, sub-scores and the final screening score.

        All work is vectorized Polars — no per-ticker Python loops. Sub-scores are
        computed for *every* row; the final ``screening_score`` is set to NULL for
        rows that fail any hard filter (only passed rows are scored).
        """
        flagged = frame.with_columns(
            _hard_filter_flag_exprs(min_price, min_adv, min_rvol)
        )

        pass_cols = [flag for _, flag in _HARD_FILTER_ORDER]
        passed_all = pl.all_horizontal([pl.col(c) for c in pass_cols])
        flagged = flagged.with_columns(passed_all.alias("_passed"))

        scored = flagged.with_columns(
            [
                _trend_score_expr().alias("_trend"),
                _momentum_score_expr().alias("_momentum"),
                _setup_score_expr().alias("_setup"),
                _volume_score_expr().alias("_volume"),
                _market_score_expr().alias("_market"),
            ]
        )

        final_raw = (
            weights["trend"] * pl.col("_trend")
            + weights["momentum"] * pl.col("_momentum")
            + weights["setup"] * pl.col("_setup")
            + weights["volume"] * pl.col("_volume")
            + weights["market"] * pl.col("_market")
        ).clip(0.0, 100.0)

        scored = scored.with_columns(
            pl.when(pl.col("_passed"))
            .then(final_raw)
            .otherwise(None)
            .alias("_screening_score")
        )
        return scored

    def _build_rows(
        self,
        scored: pl.DataFrame,
        run_id: str,
        strategy_config_id: str,
    ) -> list[dict[str, Any]]:
        """Materialise the per-ticker insert payloads (deterministic order).

        Iterating the small evaluated frame here (in ticker order) only assembles
        per-row JSON / UUIDs for the write; all filtering and scoring stayed
        vectorized in :meth:`_evaluate`.
        """
        rows: list[dict[str, Any]] = []
        for rec in scored.iter_rows(named=True):
            passed = bool(rec["_passed"])

            reasons: list[str] = []
            if not passed:
                for label, flag_col in _HARD_FILTER_ORDER:
                    if not bool(rec[flag_col]):
                        reasons.append(label)

            score = _f(rec["_screening_score"]) if passed else None

            components = {
                "trend_score": _f(rec["_trend"]),
                "momentum_score": _f(rec["_momentum"]),
                "setup_score": _f(rec["_setup"]),
                "volume_score": _f(rec["_volume"]),
                "market_score": _f(rec["_market"]),
                "market_regime": rec["market_regime"],
                "market_regime_known": rec["market_regime"] in MARKET_SCORE_BY_REGIME,
                "sector_relative_strength": _f(rec["sector_relative_strength"]),
            }

            snapshot = {
                "ema20": _f(rec["ema20"]),
                "ema50": _f(rec["ema50"]),
                "ema200": _f(rec["ema200"]),
                "ema_alignment_score": _f(rec["ema_alignment_score"]),
                "rsi14": _f(rec["rsi14"]),
                "roc20": _f(rec["roc20"]),
                "rvol20": _f(rec["rvol20"]),
                "avg_dollar_volume_20d": _f(rec["avg_dollar_volume_20d"]),
                "breakout_proximity": _f(rec["breakout_proximity"]),
                "pullback_from_recent_high_pct": _f(
                    rec["pullback_from_recent_high_pct"]
                ),
                "consolidation_score": _f(rec["consolidation_score"]),
                "sector_relative_strength": _f(rec["sector_relative_strength"]),
                "market_regime": rec["market_regime"],
                "close_raw": _f(rec["close_raw"]),
            }

            rows.append(
                {
                    "candidate_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "strategy_config_id": strategy_config_id,
                    "ticker": rec["ticker"],
                    "signal_date": rec["feature_date"],
                    "screening_score": score,
                    "passed": passed,
                    "hard_filter_fail_reasons": _json_dumps(reasons),
                    "soft_score_components": _json_dumps(components),
                    "feature_snapshot_json": _json_dumps(snapshot),
                }
            )
        return rows

    # ------------------------------------------------------------------ #
    # Write phase.
    # ------------------------------------------------------------------ #
    def _write(self, db_role: str, rows: list[dict[str, Any]]) -> int:
        """Append all candidate rows inside a single ``BEGIN TRANSACTION / COMMIT``.

        Each row is written with its own ``execute()`` call (not ``executemany``).
        Return rows written.

        Any error triggers ``ROLLBACK`` so no partial Module 13 candidate rows
        survive. An empty plan opens and commits with no inserts.
        """
        if not rows:
            return 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for row in rows:
                    connection.execute(
                        _INSERT_CANDIDATE,
                        [
                            row["candidate_id"],
                            row["run_id"],
                            row["strategy_config_id"],
                            row["ticker"],
                            row["signal_date"],
                            row["screening_score"],
                            row["passed"],
                            row["hard_filter_fail_reasons"],
                            row["soft_score_components"],
                            row["feature_snapshot_json"],
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
    def _failed(
        self,
        run_id: str,
        db_role: str,
        signal_iso: str,
        strategy_config_id: str,
        message: str,
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
                tickers_evaluated=0,
                passed_hard_filters=0,
                failed_hard_filters=0,
                candidates_written=0,
                screening_score_min=None,
                screening_score_max=None,
                screening_score_mean=None,
            ),
        )

    @staticmethod
    def _metadata(
        *,
        db_role: str,
        signal_date: str,
        strategy_config_id: str,
        run_id: str,
        tickers_evaluated: int,
        passed_hard_filters: int,
        failed_hard_filters: int,
        candidates_written: int,
        screening_score_min: float | None,
        screening_score_max: float | None,
        screening_score_mean: float | None,
    ) -> dict[str, Any]:
        """Build the metadata dict carrying exactly :data:`METADATA_KEYS`."""
        return {
            "db_role": db_role,
            "signal_date": signal_date,
            "strategy_config_id": strategy_config_id,
            "run_id": run_id,
            "tickers_evaluated": tickers_evaluated,
            "passed_hard_filters": passed_hard_filters,
            "failed_hard_filters": failed_hard_filters,
            "candidates_written": candidates_written,
            "screening_score_min": screening_score_min,
            "screening_score_max": screening_score_max,
            "screening_score_mean": screening_score_mean,
        }


__all__ = [
    "Step3ScreeningEngine",
    "METADATA_KEYS",
    "ALLOWED_DB_ROLES",
    "MARKET_SCORE_BY_REGIME",
    "MARKET_SCORE_UNKNOWN",
]
