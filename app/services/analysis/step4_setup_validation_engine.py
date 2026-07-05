"""Module 14 — Step 4 Setup Validation Engine (setup-mode migration).

Reads routed Step 3 candidates for one signal_date, iterates active
setup_config_ids that match each ticker's routed_setup_types, calls the
per-setup validator (m14_setup_validators), and writes one step4_analysis row
per (ticker, setup_type) combination.

Pipeline position (01d_MODULES_AND_PIPELINE.md):
    Step 3 (once per signal_date) → Step 4 (per active setup_config_id) → Step 5

This module:
- Runs AFTER Step3UniversalEligibilityEngine (step3_candidates must exist).
- Reads step3_candidates WHERE routing_status = 'routed'.
- For each routed ticker, iterates setup_types in routed_setup_types JSON.
- Finds the active setup_config for each setup_type; skips if none found.
- Calls validate_setup() from m14_setup_validators.
- Writes to step4_analysis inside a single BEGIN TRANSACTION / COMMIT.
- Phase 4 leaves stop_price_raw / target_price_raw / estimated_rr /
  stop_distance_pct as NULL; Phase 5 (M15) fills them.
- Never writes stop/target/RR/disposition; never computes risk_label.
- Only target_is_structural is written (from validator evidence).
- Writes market_regime from feature snapshot (NULL = unknown; not defaulted).
- Never opens DuckDB directly; never imports duckdb; never runs DDL; no print().

DB roles: prod and debug only (not simulation).
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.config import constants
from app.database import duckdb_manager
from app.services.screening.m14_setup_validators import (
    SetupValidationResult,
    validate_setup,
)
from app.utils import logging_config
from app.utils import service_result as sr
from app.utils.service_result import ServiceResult

# ---------------------------------------------------------------------------
# DB role constants
# ---------------------------------------------------------------------------
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# ---------------------------------------------------------------------------
# Metadata keys contract
# ---------------------------------------------------------------------------
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "signal_date",
    "run_id",
    "candidates_evaluated",
    "setup_configs_used",
    "analyses_written",
    "setup_type_counts",
    "passed_counts",
    "failed_counts",
)


class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# ---------------------------------------------------------------------------
# SQL — read routed candidates
# ---------------------------------------------------------------------------
_SQL_READ_ROUTED: Final[str] = """
SELECT
    candidate_id,
    ticker,
    signal_date,
    routed_setup_types,
    feature_snapshot_json
FROM step3_candidates
WHERE signal_date = ?
  AND routing_status = 'routed'
ORDER BY ticker, candidate_id
"""

# Read active setup configs (once per run)
_SQL_READ_SETUP_CONFIGS: Final[str] = """
SELECT config_id, setup_type, config_json
FROM setup_configs
WHERE active_flag = TRUE
ORDER BY setup_type
"""

# Read features + prices for signal_date (for all routed tickers in one query)
_SQL_READ_FEATURES_PRICES: Final[str] = """
SELECT
    f.ticker,
    f.feature_schema_version,
    f.ema20,
    f.ema50,
    f.ema200,
    f.ema_alignment_score,
    f.ema20_slope,
    f.ema50_slope,
    f.distance_to_ema20_pct,
    f.distance_to_ema50_pct,
    f.rsi14,
    f.roc20,
    f.atr14,
    f.atr_pct,
    f.atr_compression_score,
    f.rvol20,
    f.avg_dollar_volume_20d,
    f.pullback_from_recent_high_pct,
    f.pullback_depth_pct,
    f.breakout_proximity,
    f.consolidation_score,
    f.swing_high,
    f.swing_low,
    f.support_level,
    f.resistance_level,
    f.next_resistance_level,
    f.base_high,
    f.base_low,
    f.range_width_pct,
    f.range_duration,
    f.range_tightness_score,
    f.volume_dry_up_score,
    f.volume_expansion_score,
    f.relative_strength_vs_spy,
    f.sector_relative_strength,
    f.market_regime,
    f.days_to_earnings_bd,
    f.macro_event_risk_flag,
    p.open_raw,
    p.high_raw,
    p.low_raw,
    p.close_raw,
    p.close_adj
FROM daily_features_current f
LEFT JOIN daily_prices p
    ON p.ticker = f.ticker AND p.date = f.feature_date
WHERE f.feature_date = ?
ORDER BY f.ticker
"""

# Fundamentals/events layer (coder-note Phase 4 — not to be confused with the
# migration-phase numbering in this module's own docstring). Point-in-time
# correct: only rows with as_of_date <= signal_date are eligible, and the
# most recent such row per ticker wins (mirrors daily_prices' asof-safe
# "date <= end_date" pattern from the Phase 0 point-in-time audit).
_SQL_READ_FUNDAMENTALS: Final[str] = """
SELECT
    ticker,
    eps_growth_trend,
    leverage_ratio,
    valuation_band,
    piotroski_f_score,
    altman_z_score
FROM ticker_fundamentals
WHERE as_of_date <= ?
QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY as_of_date DESC) = 1
"""

_FUNDAMENTALS_COLS: Final[tuple[str, ...]] = (
    "ticker",
    "eps_growth_trend",
    "leverage_ratio",
    "valuation_band",
    "piotroski_f_score",
    "altman_z_score",
)

# Write one analysis row
_SQL_INSERT: Final[str] = """
INSERT INTO step4_analysis (
    analysis_id,
    candidate_id,
    run_id,
    setup_config_id,
    ticker,
    signal_date,
    setup_type,
    setup_score,
    setup_passed,
    setup_reasons,
    setup_fail_reason,
    entry_price_raw,
    stop_price_raw,
    target_price_raw,
    estimated_rr,
    target_is_structural,
    stop_distance_pct,
    support_level,
    resistance_level,
    next_resistance_level,
    atr_pct,
    distance_to_ema20_pct,
    distance_to_ema50_pct,
    rvol,
    earnings_days,
    market_regime,
    earnings_penalty,
    macro_penalty,
    explanation_json,
    created_at
) VALUES (
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, NULL, NULL, NULL, ?,
    NULL,
    ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?,
    ?, ?,
    ?,
    CAST(now() AS TIMESTAMP)
)
"""

_FEATURE_COLS: Final[tuple[str, ...]] = (
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_metadata(db_role: str, signal_date: date, run_id: str) -> dict[str, Any]:
    return {
        "db_role": db_role,
        "signal_date": signal_date.isoformat(),
        "run_id": run_id,
        "candidates_evaluated": 0,
        "setup_configs_used": [],
        "analyses_written": 0,
        "setup_type_counts": {st: 0 for st in constants.ALLOWED_SETUP_TYPES},
        "passed_counts": {st: 0 for st in constants.ALLOWED_SETUP_TYPES},
        "failed_counts": {st: 0 for st in constants.ALLOWED_SETUP_TYPES},
    }


def _load_active_setup_configs(
    db_mgr: _DbManagerLike, db_role: str
) -> dict[str, dict[str, Any]]:
    """Return dict keyed by setup_type → parsed config dict."""
    conn = db_mgr.connect(db_role)
    try:
        rows = conn.execute(_SQL_READ_SETUP_CONFIGS).fetchall()
    finally:
        conn.close()
    configs: dict[str, dict[str, Any]] = {}
    for config_id, setup_type, config_json_raw in rows:
        parsed = (
            json.loads(config_json_raw)
            if isinstance(config_json_raw, str)
            else config_json_raw
        )
        parsed.setdefault("config_id", config_id)
        parsed.setdefault("setup_type", setup_type)
        configs[setup_type] = parsed
    return configs


def _read_routed_candidates(
    db_mgr: _DbManagerLike, db_role: str, signal_date: date
) -> list[dict[str, Any]]:
    sig_iso = signal_date.isoformat()
    conn = db_mgr.connect(db_role)
    try:
        rows = conn.execute(_SQL_READ_ROUTED, [sig_iso]).fetchall()
    finally:
        conn.close()
    result = []
    for candidate_id, ticker, sig_d, routed_json, snapshot_json in rows:
        routed_types = (
            json.loads(routed_json) if isinstance(routed_json, str) else routed_json
        ) or []
        snapshot = (
            json.loads(snapshot_json) if isinstance(snapshot_json, str) else snapshot_json
        ) or {}
        result.append({
            "candidate_id": candidate_id,
            "ticker": ticker,
            "signal_date": sig_d,
            "routed_setup_types": routed_types,
            "feature_snapshot": snapshot,
        })
    return result


def _read_features_prices(
    db_mgr: _DbManagerLike, db_role: str, signal_date: date
) -> dict[str, dict[str, Any]]:
    """Return dict keyed by ticker → feature+price row."""
    sig_iso = signal_date.isoformat()
    conn = db_mgr.connect(db_role)
    try:
        rows = conn.execute(_SQL_READ_FEATURES_PRICES, [sig_iso]).fetchall()
    finally:
        conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = dict(zip(_FEATURE_COLS, row))
        result[d["ticker"]] = d
    return result


def _read_fundamentals(
    db_mgr: _DbManagerLike, db_role: str, signal_date: date
) -> dict[str, dict[str, Any]]:
    """Return dict keyed by ticker -> most recent ticker_fundamentals row
    known as of signal_date (Phase 4 — coder-note fundamentals/events layer).

    Absence (query returns nothing for a ticker, or the table has no rows at
    all for it yet) is not an error: validators treat missing fundamentals
    fields as "no adjustment" (see m14_setup_validators._compute_fundamentals_adjustment),
    exactly like a routed candidate with no earnings-calendar row.
    """
    sig_iso = signal_date.isoformat()
    conn = db_mgr.connect(db_role)
    try:
        rows = conn.execute(_SQL_READ_FUNDAMENTALS, [sig_iso]).fetchall()
    finally:
        conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = dict(zip(_FUNDAMENTALS_COLS, row))
        result[d["ticker"]] = d
    return result


def _build_feat_dict(
    candidate: dict[str, Any],
    features_prices: dict[str, dict[str, Any]],
    signal_date: date,
    fundamentals_by_ticker: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge feature snapshot with live feature+price data.

    Live data (features_prices) takes priority; snapshot fills gaps.
    Adds ticker + signal_date keys for validators. Fundamentals fields
    (Phase 4) are merged last under their own keys, which never collide with
    feature/price columns, so merge order relative to them is immaterial.
    """
    ticker = candidate["ticker"]
    live = features_prices.get(ticker, {})
    snap = candidate.get("feature_snapshot", {})
    feat: dict[str, Any] = {}
    # Start from snapshot (may have routing features)
    feat.update(snap)
    # Override with live feature+price data (more complete)
    feat.update(live)
    # Ensure identity fields
    feat["ticker"] = ticker
    feat["signal_date"] = signal_date.isoformat()
    if fundamentals_by_ticker is not None:
        fundamentals = fundamentals_by_ticker.get(ticker)
        if fundamentals is not None:
            feat["eps_growth_trend"] = fundamentals["eps_growth_trend"]
            feat["leverage_ratio"] = fundamentals["leverage_ratio"]
            feat["valuation_band"] = fundamentals["valuation_band"]
            feat["piotroski_f_score"] = fundamentals["piotroski_f_score"]
            feat["altman_z_score"] = fundamentals["altman_z_score"]
    return feat


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

class Step4SetupValidationEngine:
    """Step 4 setup validation engine (setup-mode migration).

    Reads routed step3_candidates, validates each (ticker, setup_type) pair
    against the matching active setup_config, writes step4_analysis rows.
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = db_manager if db_manager is not None else duckdb_manager

    def run(
        self,
        signal_date: date,
        db_role: str = DB_ROLE_PROD,
        run_id: str | None = None,
        setup_configs: dict[str, dict[str, Any]] | None = None,
    ) -> ServiceResult:
        """Execute Step 4 for one signal_date.

        Parameters
        ----------
        signal_date:
            The date to process.
        db_role:
            'prod' or 'debug'. 'simulation' is rejected.
        run_id:
            UUID4 string. Minted if None.
        setup_configs:
            Pre-loaded dict of {setup_type: config_dict}. If None, loaded from DB.
            Pass explicitly in tests to avoid DB config reads.

        Returns
        -------
        ServiceResult
            rows_processed = analyses written.
        """
        run_id = run_id or str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        sig_iso = signal_date.isoformat()

        log.info("Step4 start db_role=%s signal_date=%s", db_role, sig_iso)

        if db_role not in ALLOWED_DB_ROLES:
            msg = (
                f"Unsupported db_role {db_role!r}. "
                f"Step 4 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("Step4 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # Load configs
        try:
            if setup_configs is None:
                setup_configs = _load_active_setup_configs(self._db, db_role)
        except Exception as exc:
            msg = f"config load failed: {type(exc).__name__}: {exc}"
            log.error("Step4 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        if not setup_configs:
            msg = "No active setup configs found."
            log.warning("Step4: %s", msg)
            return ServiceResult(
                status=sr.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id,
                rows_processed=0,
                warnings=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # Read routed candidates
        try:
            candidates = _read_routed_candidates(self._db, db_role, signal_date)
        except Exception as exc:
            msg = f"read candidates failed: {type(exc).__name__}: {exc}"
            log.error("Step4 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        if not candidates:
            log.warning("Step4: no routed candidates for signal_date=%s", sig_iso)
            return ServiceResult(
                status=sr.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id,
                rows_processed=0,
                warnings=[f"No routed candidates for signal_date={sig_iso}"],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # Read features+prices
        try:
            features_prices = _read_features_prices(self._db, db_role, signal_date)
        except Exception as exc:
            msg = f"read features failed: {type(exc).__name__}: {exc}"
            log.error("Step4 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # Read fundamentals (Phase 4 — optional, config-weighted soft input;
        # never a hard gate). A read failure here fails the whole step, same
        # as a features-read failure, since ticker_fundamentals is a
        # schema-managed table expected to exist in every migrated DB.
        try:
            fundamentals_by_ticker = _read_fundamentals(self._db, db_role, signal_date)
        except Exception as exc:
            msg = f"read fundamentals failed: {type(exc).__name__}: {exc}"
            log.error("Step4 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # Validate
        analyses: list[tuple[SetupValidationResult, str]] = []  # (result, candidate_id)
        warnings: list[str] = []
        setup_type_counts: dict[str, int] = {st: 0 for st in constants.ALLOWED_SETUP_TYPES}
        passed_counts: dict[str, int] = {st: 0 for st in constants.ALLOWED_SETUP_TYPES}
        failed_counts: dict[str, int] = {st: 0 for st in constants.ALLOWED_SETUP_TYPES}

        for candidate in candidates:
            ticker = candidate["ticker"]
            cand_id = candidate["candidate_id"]
            routed = candidate["routed_setup_types"]
            feat = _build_feat_dict(
                candidate, features_prices, signal_date, fundamentals_by_ticker
            )

            for setup_type in routed:
                cfg = setup_configs.get(setup_type)
                if cfg is None:
                    warnings.append(
                        f"{ticker}: no active config for setup_type={setup_type!r}; skipped"
                    )
                    continue
                try:
                    result = validate_setup(setup_type, feat, cfg)
                except Exception as exc:
                    warnings.append(
                        f"{ticker}/{setup_type}: validation error {type(exc).__name__}: {exc}"
                    )
                    continue
                analyses.append((result, cand_id))
                setup_type_counts[setup_type] = setup_type_counts.get(setup_type, 0) + 1
                if result.setup_passed:
                    passed_counts[setup_type] = passed_counts.get(setup_type, 0) + 1
                else:
                    failed_counts[setup_type] = failed_counts.get(setup_type, 0) + 1

        # Write
        try:
            written = self._write(db_role, signal_date, run_id, analyses)
        except Exception as exc:
            msg = f"write failed (rolled back): {type(exc).__name__}: {exc}"
            log.error("Step4 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        meta: dict[str, Any] = {
            "db_role": db_role,
            "signal_date": sig_iso,
            "run_id": run_id,
            "candidates_evaluated": len(candidates),
            "setup_configs_used": sorted(setup_configs.keys()),
            "analyses_written": written,
            "setup_type_counts": setup_type_counts,
            "passed_counts": passed_counts,
            "failed_counts": failed_counts,
        }
        log.info(
            "Step4 done candidates=%d analyses_written=%d setup_counts=%s",
            len(candidates), written, setup_type_counts,
        )

        status = sr.STATUS_SUCCESS if not warnings else sr.STATUS_SUCCESS_WITH_WARNINGS
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=written,
            warnings=warnings,
            metadata=meta,
        )

    # -----------------------------------------------------------------------
    # Write
    # -----------------------------------------------------------------------
    def _write(
        self,
        db_role: str,
        signal_date: date,
        run_id: str,
        analyses: list[tuple[SetupValidationResult, str]],
    ) -> int:
        if not analyses:
            return 0
        sig_iso = signal_date.isoformat()
        conn = self._db.connect(db_role)
        try:
            conn.execute("BEGIN TRANSACTION")
            try:
                for result, candidate_id in analyses:
                    conn.execute(
                        _SQL_INSERT,
                        [
                            str(uuid.uuid4()),           # analysis_id
                            candidate_id,                 # candidate_id
                            run_id,                       # run_id
                            result.setup_config_id,       # setup_config_id
                            result.ticker,                # ticker
                            sig_iso,                      # signal_date
                            result.setup_type,            # setup_type
                            result.setup_score,           # setup_score
                            result.setup_passed,          # setup_passed
                            json.dumps(result.pass_fail_reasons),  # setup_reasons
                            result.setup_fail_reason,     # setup_fail_reason
                            result.entry_price_raw,       # entry_price_raw
                            # stop_price_raw → NULL (Phase 5)
                            # target_price_raw → NULL (Phase 5)
                            # estimated_rr → NULL (Phase 5)
                            result.target_is_structural,  # target_is_structural
                            # stop_distance_pct → NULL (Phase 5)
                            result.support_level_raw,     # support_level
                            result.resistance_level_raw,  # resistance_level
                            result.next_resistance_level_raw,  # next_resistance_level
                            result.atr_pct,               # atr_pct
                            result.distance_to_ema20_pct, # distance_to_ema20_pct
                            result.distance_to_ema50_pct, # distance_to_ema50_pct
                            result.rvol,                  # rvol
                            result.earnings_days,         # earnings_days
                            result.market_regime,         # market_regime
                            result.earnings_penalty,      # earnings_penalty
                            result.macro_penalty,         # macro_penalty
                            json.dumps(result.evidence_json, default=str),  # explanation_json
                        ],
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()
        return len(analyses)


__all__ = [
    "Step4SetupValidationEngine",
    "ALLOWED_DB_ROLES",
    "METADATA_KEYS",
]
