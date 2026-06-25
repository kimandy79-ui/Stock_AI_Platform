"""Module 13 — Step 3: Universal Eligibility + Setup Routing (setup-mode migration).

Replaces the legacy strategy-first Step3ScreeningEngine with a setup-mode
implementation that:

1. Runs ONCE per signal_date (never per setup_config_id).
2. Applies universal tradability/data eligibility only — no RVOL, no setup
   score, no momentum, no ATR%, no EMA extension as universal hard gates
   (AD-22.23).
3. Routes eligible tickers into zero or more setup_types using coarse
   structural predicates.  Full validation is Step 4 (M14).
4. Sources the universe from ticker_master LEFT JOIN daily_prices LEFT JOIN
   daily_features_current so active tickers with missing price or feature rows
   are included as ineligible rather than silently dropped.
5. Populates feature_snapshot_json with all eligibility and routing inputs.
6. Writes inside a single BEGIN TRANSACTION / COMMIT (ROLLBACK on error).
7. Never opens a DuckDB connection directly; uses duckdb_manager exclusively.
8. Never imports duckdb directly, never runs DDL, never uses print().

Schema contract (01b_SCHEMA_AND_DATA.md — step3_candidates):
    candidate_id, run_id, ticker, signal_date, eligibility_score,
    passed_eligibility, routing_status, routing_fail_reason,
    eligibility_fail_reasons (JSON), routed_setup_types (JSON),
    feature_snapshot_json (JSON), created_at.

routing_status values: 'routed' | 'no_route' | 'ineligible'
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.config import constants
from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result as sr
from app.utils.service_result import ServiceResult

# ---------------------------------------------------------------------------
# Allowed roles
# ---------------------------------------------------------------------------
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# ---------------------------------------------------------------------------
# routing_status constants
# ---------------------------------------------------------------------------
ROUTING_ROUTED: Final[str] = "routed"
ROUTING_NO_ROUTE: Final[str] = "no_route"
ROUTING_INELIGIBLE: Final[str] = "ineligible"
ROUTING_FAIL_NO_ROUTE: Final[str] = "no_route"

# ---------------------------------------------------------------------------
# Eligibility rejection reason labels
# ---------------------------------------------------------------------------
REASON_FEATURE_NOT_READY: Final[str] = "feature_not_ready"
REASON_NOT_STOCK: Final[str] = "not_stock"
REASON_PRICE_BELOW_MIN: Final[str] = "price_below_min"
REASON_LIQUIDITY_BELOW_MIN: Final[str] = "liquidity_below_min"
REASON_DATA_QUALITY_FAIL: Final[str] = "data_quality_fail"
REASON_OHLCV_ANOMALY: Final[str] = "ohlcv_anomaly"
REASON_NO_PRICE_ROW: Final[str] = "no_price_row"

# Ordered check labels (order controls fail_reasons list order)
_ELIGIBILITY_ORDER: Final[tuple[str, ...]] = (
    REASON_NO_PRICE_ROW,
    REASON_FEATURE_NOT_READY,
    REASON_NOT_STOCK,
    REASON_PRICE_BELOW_MIN,
    REASON_LIQUIDITY_BELOW_MIN,
    REASON_DATA_QUALITY_FAIL,
    REASON_OHLCV_ANOMALY,
)

# ---------------------------------------------------------------------------
# Metadata key contract (every return path)
# ---------------------------------------------------------------------------
METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role",
    "signal_date",
    "run_id",
    "total_evaluated",
    "ineligible_count",
    "no_route_count",
    "routed_count",
    "rejection_reasons",
    "routed_by_setup_type",
    "candidates_written",
)


# ---------------------------------------------------------------------------
# DB manager protocol (structural subtyping — no hard duckdb import here)
# ---------------------------------------------------------------------------
class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------
class ConfigParityError(ValueError):
    """Universe blocks diverge across active setup configs."""


class MissingConfigError(ValueError):
    """Required config absent."""


# ---------------------------------------------------------------------------
# SQL — read phase
# ---------------------------------------------------------------------------
# Source universe from ticker_master (active, non-delisted).
# LEFT JOIN daily_prices for eligibility price/OHLCV/quality columns.
# LEFT JOIN daily_features_current for feature columns.
# Active tickers with no price row on signal_date appear with NULLs and are
# rejected with REASON_NO_PRICE_ROW (fix #4).
_SQL_READ_UNIVERSE: Final[str] = """
SELECT
    tm.ticker,
    tm.symbol_type,
    -- price columns (NULL if no price row on signal_date)
    dp.open_raw,
    dp.high_raw,
    dp.low_raw,
    dp.close_raw,
    dp.close_adj,
    CAST(dp.volume_raw AS BIGINT)      AS volume_raw,
    dp.data_quality_status,
    -- feature columns (NULL if no feature row)
    COALESCE(df.feature_ready, FALSE)  AS feature_ready,
    df.avg_dollar_volume_20d,
    -- routing predicate features
    df.breakout_proximity,
    df.range_duration,
    df.ema200,
    df.pullback_from_recent_high_pct,
    df.ema20,
    df.ema50,
    df.ema_alignment_score,
    df.ema50_slope,
    df.range_tightness_score
FROM ticker_master tm
LEFT JOIN daily_prices dp
    ON dp.ticker = tm.ticker AND dp.date = ?
LEFT JOIN daily_features_current df
    ON df.ticker = tm.ticker AND df.feature_date = ?
WHERE tm.active_flag = TRUE
  AND tm.delisted_flag = FALSE
ORDER BY tm.ticker
"""

# ---------------------------------------------------------------------------
# SQL — write phase
# ---------------------------------------------------------------------------
_SQL_INSERT: Final[str] = """
INSERT INTO step3_candidates (
    candidate_id,
    run_id,
    ticker,
    signal_date,
    eligibility_score,
    passed_eligibility,
    routing_status,
    routing_fail_reason,
    eligibility_fail_reasons,
    routed_setup_types,
    feature_snapshot_json,
    created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP))
"""


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def _extract_universe_block(cfg: dict[str, Any]) -> dict[str, Any]:
    ub = cfg.get("universe")
    if not isinstance(ub, dict):
        cid = cfg.get("config_id", "unknown")
        raise MissingConfigError(
            f"Setup config '{cid}' is missing a 'universe' block."
        )
    return ub


def _assert_universe_parity(setup_configs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Assert all active setup configs carry an identical universe block.
    Returns the shared block on success; raises ConfigParityError on divergence.
    """
    if not setup_configs:
        raise MissingConfigError("No active setup configs provided.")
    ref = _extract_universe_block(setup_configs[0])
    ref_id = setup_configs[0].get("config_id", "?")
    for cfg in setup_configs[1:]:
        ub = _extract_universe_block(cfg)
        cid = cfg.get("config_id", "?")
        if ub != ref:
            raise ConfigParityError(
                f"Universe config mismatch between '{ref_id}' and '{cid}'. "
                f"All active setup configs must carry identical universe blocks. "
                f"ref={ref!r}  got={ub!r}"
            )
    return ref


def _parse_universe_config(ub: dict[str, Any]) -> tuple[float, float, list[str]]:
    try:
        min_price = float(ub["min_price"])
        min_adv = float(ub["min_avg_dollar_volume_20d"])
        allowed_types: list[str] = list(ub.get("allowed_symbol_types", ["stock"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise MissingConfigError(f"Invalid universe config block: {exc}") from exc
    return min_price, min_adv, allowed_types


def _load_active_setup_configs(db_mgr: _DbManagerLike, db_role: str) -> list[dict[str, Any]]:
    conn = db_mgr.connect(db_role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT config_id, setup_type, config_json "
            "FROM setup_configs WHERE active_flag = TRUE"
        ).fetchall()
    finally:
        conn.close()
    configs: list[dict[str, Any]] = []
    for config_id, setup_type, config_json_raw in rows:
        parsed = json.loads(config_json_raw) if isinstance(config_json_raw, str) else config_json_raw
        parsed.setdefault("config_id", config_id)
        parsed.setdefault("setup_type", setup_type)
        configs.append(parsed)
    return configs


# ---------------------------------------------------------------------------
# Eligibility check
# ---------------------------------------------------------------------------
def _check_eligibility(
    row: dict[str, Any],
    min_price: float,
    min_adv: float,
    allowed_symbol_types: list[str],
) -> list[str]:
    """
    Return ordered list of failing eligibility reason labels.
    Empty list = eligible.

    Universal filters only (AD-22.23). RVOL, setup score, momentum,
    ATR%, EMA extension, consolidation quality are NOT checked here.
    """
    reasons: list[str] = []

    # Missing price row — cannot evaluate anything
    if row["close_raw"] is None and row["data_quality_status"] is None:
        reasons.append(REASON_NO_PRICE_ROW)
        # No further checks possible without price data
        if not row["feature_ready"]:
            reasons.append(REASON_FEATURE_NOT_READY)
        if row["symbol_type"] not in allowed_symbol_types:
            reasons.append(REASON_NOT_STOCK)
        return reasons

    if not row["feature_ready"]:
        reasons.append(REASON_FEATURE_NOT_READY)

    if row["symbol_type"] not in allowed_symbol_types:
        reasons.append(REASON_NOT_STOCK)

    close_raw = row["close_raw"]
    if close_raw is None or close_raw < min_price:
        reasons.append(REASON_PRICE_BELOW_MIN)

    adv = row["avg_dollar_volume_20d"]
    if adv is None or adv < min_adv:
        reasons.append(REASON_LIQUIDITY_BELOW_MIN)

    dq = row["data_quality_status"]
    if dq is None or dq != "ok":
        reasons.append(REASON_DATA_QUALITY_FAIL)

    # OHLCV anomaly checks
    anomaly = False
    high = row["high_raw"]
    low = row["low_raw"]
    open_ = row["open_raw"]
    vol = row["volume_raw"]
    if high is not None and low is not None and high < low:
        anomaly = True
    if close_raw is not None and close_raw <= 0:
        anomaly = True
    if open_ is not None and open_ <= 0:
        anomaly = True
    if vol is not None and vol < 0:
        anomaly = True
    if anomaly:
        reasons.append(REASON_OHLCV_ANOMALY)

    return reasons


# ---------------------------------------------------------------------------
# Eligibility score (diagnostics only — never gates)
# ---------------------------------------------------------------------------
def _compute_eligibility_score(
    row: dict[str, Any],
    all_dvols: list[float],
    all_prices: list[float],
) -> float | None:
    """
    Coarse tradability score 0-100 for diagnostics only.
    Formula: 0.5 * liquidity_norm + 0.3 * price_norm + 0.2 * history_norm
    Normalised min-max within day's eligible universe. Never rejects.
    """
    adv = row["avg_dollar_volume_20d"]
    price = row["close_raw"]
    if adv is None or price is None or not all_dvols or not all_prices:
        return None

    def _norm(val: float, vals: list[float]) -> float:
        lo, hi = min(vals), max(vals)
        return 50.0 if hi == lo else 100.0 * (val - lo) / (hi - lo)

    return 0.5 * _norm(adv, all_dvols) + 0.3 * _norm(price, all_prices) + 0.2 * 100.0


# ---------------------------------------------------------------------------
# Routing predicates (01c FORMULAS/61 — coarse only; Step 4 does full validation)
# ---------------------------------------------------------------------------
def _route_breakout(row: dict[str, Any]) -> bool:
    bp = row["breakout_proximity"]
    rd = row["range_duration"]
    return bp is not None and rd is not None and bp >= -1.0 and rd >= 10


def _route_pullback(row: dict[str, Any]) -> bool:
    ca = row["close_adj"]
    e200 = row["ema200"]
    prh = row["pullback_from_recent_high_pct"]
    e20 = row["ema20"]
    e50 = row["ema50"]
    if any(v is None for v in (ca, e200, prh, e20, e50)):
        return False
    return ca > e200 and -0.20 <= prh <= -0.02 and e20 > e50


def _route_trend_continuation(row: dict[str, Any]) -> bool:
    eas = row["ema_alignment_score"]
    es = row["ema50_slope"]
    ca = row["close_adj"]
    e50 = row["ema50"]
    if any(v is None for v in (eas, es, ca, e50)):
        return False
    return eas >= 50 and es > 0 and ca > e50


def _route_consolidation_base(row: dict[str, Any]) -> bool:
    rts = row["range_tightness_score"]
    rd = row["range_duration"]
    return rts is not None and rd is not None and rts >= 50 and rd >= 10


_ROUTING_PREDICATES: Final[dict[str, Any]] = {
    constants.SETUP_BREAKOUT: _route_breakout,
    constants.SETUP_PULLBACK: _route_pullback,
    constants.SETUP_TREND_CONTINUATION: _route_trend_continuation,
    constants.SETUP_CONSOLIDATION_BASE: _route_consolidation_base,
}


def _evaluate_routing(row: dict[str, Any]) -> list[str]:
    return [st for st, pred in _ROUTING_PREDICATES.items() if pred(row)]


# ---------------------------------------------------------------------------
# feature_snapshot_json — all eligibility and routing inputs (fix #5)
# ---------------------------------------------------------------------------
_SNAPSHOT_FIELDS: Final[tuple[str, ...]] = (
    # eligibility inputs
    "symbol_type",
    "feature_ready",
    "close_raw",
    "open_raw",
    "high_raw",
    "low_raw",
    "volume_raw",
    "data_quality_status",
    "avg_dollar_volume_20d",
    # routing inputs
    "close_adj",
    "ema200",
    "ema20",
    "ema50",
    "ema_alignment_score",
    "ema50_slope",
    "pullback_from_recent_high_pct",
    "breakout_proximity",
    "range_duration",
    "range_tightness_score",
)


def _build_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        val = row.get(field)
        # Convert non-JSON-serialisable types
        if hasattr(val, "item"):  # numpy scalar
            val = val.item()
        snap[field] = val
    return snap


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def _build_metadata(
    candidates: list[dict[str, Any]],
    db_role: str,
    signal_date: date,
    run_id: str,
    candidates_written: int,
) -> dict[str, Any]:
    total = len(candidates)
    ineligible = sum(1 for c in candidates if c["routing_status"] == ROUTING_INELIGIBLE)
    no_route = sum(1 for c in candidates if c["routing_status"] == ROUTING_NO_ROUTE)
    routed = sum(1 for c in candidates if c["routing_status"] == ROUTING_ROUTED)

    rejection_reasons: dict[str, int] = {}
    for c in candidates:
        for r in c["eligibility_fail_reasons"]:
            rejection_reasons[r] = rejection_reasons.get(r, 0) + 1

    routed_by_setup: dict[str, int] = {st: 0 for st in constants.ALLOWED_SETUP_TYPES}
    for c in candidates:
        for st in c["routed_setup_types"]:
            routed_by_setup[st] = routed_by_setup.get(st, 0) + 1

    return {
        "db_role": db_role,
        "signal_date": signal_date.isoformat(),
        "run_id": run_id,
        "total_evaluated": total,
        "ineligible_count": ineligible,
        "no_route_count": no_route,
        "routed_count": routed,
        "rejection_reasons": rejection_reasons,
        "routed_by_setup_type": routed_by_setup,
        "candidates_written": candidates_written,
    }


def _empty_metadata(db_role: str, signal_date: date, run_id: str) -> dict[str, Any]:
    return {
        "db_role": db_role,
        "signal_date": signal_date.isoformat(),
        "run_id": run_id,
        "total_evaluated": 0,
        "ineligible_count": 0,
        "no_route_count": 0,
        "routed_count": 0,
        "rejection_reasons": {},
        "routed_by_setup_type": {st: 0 for st in constants.ALLOWED_SETUP_TYPES},
        "candidates_written": 0,
    }


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------
class Step3UniversalEligibilityEngine:
    """
    Step 3 universal eligibility + setup routing engine (setup-mode migration).

    Stateless. Accepts an optional db_manager injection for testing; uses the
    real duckdb_manager by default (matching the accepted project pattern).
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = db_manager if db_manager is not None else duckdb_manager

    def run(
        self,
        signal_date: date,
        db_role: str = DB_ROLE_PROD,
        run_id: str | None = None,
        setup_configs: list[dict[str, Any]] | None = None,
    ) -> ServiceResult:
        """
        Execute Step 3 for one signal_date.

        Parameters
        ----------
        signal_date:
            The date to evaluate.
        db_role:
            'prod' or 'debug'. 'simulation' is rejected.
        run_id:
            UUID4 string. A new one is minted if None.
        setup_configs:
            Pre-loaded list of active setup config dicts. If None, loaded from
            DB. Pass explicitly in tests to avoid DB config reads.

        Returns
        -------
        ServiceResult
            rows_processed = candidates written.
            metadata carries exactly METADATA_KEYS.
        """
        run_id = run_id or str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        signal_iso = signal_date.isoformat()

        log.info(
            "Step3 start db_role=%s signal_date=%s",
            db_role, signal_iso,
        )

        # --- db_role guard (before any I/O) ---
        if db_role not in ALLOWED_DB_ROLES:
            msg = (
                f"Unsupported db_role {db_role!r}. "
                f"Step 3 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("Step3 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # --- config ---
        try:
            if setup_configs is None:
                setup_configs = _load_active_setup_configs(self._db, db_role)
            universe_block = _assert_universe_parity(setup_configs)
            min_price, min_adv, allowed_symbol_types = _parse_universe_config(universe_block)
        except (ConfigParityError, MissingConfigError) as exc:
            log.error("Step3 config error: %s", exc)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[str(exc)],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # --- read phase (read-only connection) ---
        try:
            raw_rows = self._read(db_role, signal_date)
        except Exception as exc:
            msg = f"read failed: {type(exc).__name__}: {exc}"
            log.error("Step3 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        if not raw_rows:
            log.warning("Step3: no active ticker rows for signal_date=%s", signal_iso)
            return ServiceResult(
                status=sr.STATUS_SUCCESS_WITH_WARNINGS,
                run_id=run_id,
                rows_processed=0,
                warnings=[f"No active ticker rows found for signal_date={signal_iso}"],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        # --- compute phase (pure Python — no DB) ---
        # Pre-compute normalisation pools for eligibility score
        eligible_dvols = [
            r["avg_dollar_volume_20d"]
            for r in raw_rows
            if r["avg_dollar_volume_20d"] is not None and r["close_raw"] is not None
        ]
        eligible_prices = [
            r["close_raw"]
            for r in raw_rows
            if r["close_raw"] is not None and r["avg_dollar_volume_20d"] is not None
        ]

        candidates: list[dict[str, Any]] = []
        for row in raw_rows:
            rejection_reasons = _check_eligibility(row, min_price, min_adv, allowed_symbol_types)
            passed = len(rejection_reasons) == 0

            if passed:
                routed_setup_types = _evaluate_routing(row)
                routing_status = ROUTING_ROUTED if routed_setup_types else ROUTING_NO_ROUTE
                routing_fail_reason = None if routed_setup_types else ROUTING_FAIL_NO_ROUTE
                eligibility_score = _compute_eligibility_score(row, eligible_dvols, eligible_prices)
            else:
                routed_setup_types = []
                routing_status = ROUTING_INELIGIBLE
                routing_fail_reason = rejection_reasons[0] if rejection_reasons else None
                eligibility_score = None

            candidates.append({
                "candidate_id": str(uuid.uuid4()),
                "run_id": run_id,
                "ticker": row["ticker"],
                "signal_date": signal_date,
                "eligibility_score": eligibility_score,
                "passed_eligibility": passed,
                "routing_status": routing_status,
                "routing_fail_reason": routing_fail_reason,
                "eligibility_fail_reasons": rejection_reasons,
                "routed_setup_types": routed_setup_types,
                "feature_snapshot_json": _build_snapshot(row),
            })

        # --- write phase: single BEGIN/COMMIT transaction ---
        try:
            candidates_written = self._write(db_role, signal_date, candidates)
        except Exception as exc:
            msg = f"write failed (rolled back): {type(exc).__name__}: {exc}"
            log.error("Step3 failed: %s", msg)
            return ServiceResult(
                status=sr.STATUS_FAILED,
                run_id=run_id,
                errors=[msg],
                metadata=_empty_metadata(db_role, signal_date, run_id),
            )

        meta = _build_metadata(candidates, db_role, signal_date, run_id, candidates_written)
        log.info(
            "Step3 done evaluated=%d ineligible=%d no_route=%d routed=%d written=%d",
            meta["total_evaluated"], meta["ineligible_count"],
            meta["no_route_count"], meta["routed_count"], candidates_written,
        )
        log.info("Step3 routed_by_setup=%s", meta["routed_by_setup_type"])

        return ServiceResult(
            status=sr.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=candidates_written,
            metadata=meta,
        )

    # -----------------------------------------------------------------------
    # Read
    # -----------------------------------------------------------------------
    def _read(self, db_role: str, signal_date: date) -> list[dict[str, Any]]:
        sig_iso = signal_date.isoformat()
        conn = self._db.connect(db_role, read_only=True)
        try:
            rows = conn.execute(_SQL_READ_UNIVERSE, [sig_iso, sig_iso]).fetchall()
        finally:
            conn.close()

        col_names = (
            "ticker", "symbol_type",
            "open_raw", "high_raw", "low_raw", "close_raw", "close_adj",
            "volume_raw", "data_quality_status",
            "feature_ready", "avg_dollar_volume_20d",
            "breakout_proximity", "range_duration",
            "ema200", "pullback_from_recent_high_pct",
            "ema20", "ema50", "ema_alignment_score", "ema50_slope",
            "range_tightness_score",
        )
        return [dict(zip(col_names, r)) for r in rows]

    # -----------------------------------------------------------------------
    # Write
    # -----------------------------------------------------------------------
    def _write(
        self,
        db_role: str,
        signal_date: date,
        candidates: list[dict[str, Any]],
    ) -> int:
        """
        Insert all candidate rows in a single BEGIN TRANSACTION / COMMIT.
        ROLLBACK on any error (original exception re-raised; rows_processed
        count is not updated on rollback).
        """
        if not candidates:
            return 0

        sig_iso = signal_date.isoformat()
        conn = self._db.connect(db_role)
        try:
            conn.execute("BEGIN TRANSACTION")
            try:
                for c in candidates:
                    conn.execute(
                        _SQL_INSERT,
                        [
                            c["candidate_id"],
                            c["run_id"],
                            c["ticker"],
                            sig_iso,
                            c["eligibility_score"],
                            c["passed_eligibility"],
                            c["routing_status"],
                            c["routing_fail_reason"],
                            json.dumps(c["eligibility_fail_reasons"]),
                            json.dumps(c["routed_setup_types"]),
                            json.dumps(c["feature_snapshot_json"], default=str),
                        ],
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass  # original error is the one to surface
                raise
        finally:
            conn.close()
        return len(candidates)


__all__ = [
    "Step3UniversalEligibilityEngine",
    "ConfigParityError",
    "MissingConfigError",
    "ALLOWED_DB_ROLES",
    "METADATA_KEYS",
    "ROUTING_ROUTED",
    "ROUTING_NO_ROUTE",
    "ROUTING_INELIGIBLE",
]
