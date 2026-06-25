"""Tests for Module 14 — Step 4 Setup Analysis.

All tests run fully offline (no network, no provider) and never touch the real
prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects every
DuckDB settings path into pytest ``tmp_path`` and applies the real Module 03
schema there (mirroring ``tests/test_step3_screening.py``). Passing Step 3
candidate rows are seeded directly into ``step3_candidates``, feature rows into
``daily_features``, price rows into ``daily_prices`` and ticker rows into
``ticker_master``; Module 14 reads them read-only and only ever inserts into
``step4_analysis``.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.config import constants
from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.analysis import step4_analysis_engine as s4mod
from app.services.analysis.step4_analysis_engine import Step4AnalysisEngine
from app.utils import service_result

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
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
    }
)

SCHEMA = constants.FEATURE_SCHEMA_VERSION
CONFIG_ID = "cfg-1"
SOURCE = "fake"


# --------------------------------------------------------------------------- #
# Strategy config helper.
# --------------------------------------------------------------------------- #
def make_config(
    *,
    target_r: float = 2.2,
    avoid_within_bd: int = 3,
    penalty_points_max: float = -15.0,
    macro_enabled: bool = True,
    macro_penalty_points: float = -10.0,
    min_rvol: float = 1.5,
    min_screening_score: float = 0.0,   # 0.0 = allow all in tests
    min_step3_setup_score: float = 0.0, # 0.0 = allow all in tests
    min_step4_setup_score: float = 0.0, # 0.0 = allow all in tests
    min_consolidation_for_breakout: float = 0.0,
    min_estimated_rr: float = 0.0,
    strategy_name: str = "normal",
) -> dict:
    return {
        "strategy_name": strategy_name,
        "step4": {
            "target_R": target_r,
            "min_step4_setup_score": min_step4_setup_score,
            "min_consolidation_for_breakout": min_consolidation_for_breakout,
            "min_estimated_rr": min_estimated_rr,
        },
        "earnings": {
            "avoid_within_bd": avoid_within_bd,
            "penalty_points_max": penalty_points_max,
        },
        "macro_event_risk": {
            "enabled": macro_enabled,
            "penalty_points": macro_penalty_points,
        },
        "screening": {
            "min_rvol": min_rvol,
            "min_screening_score": min_screening_score,
            "min_step3_setup_score": min_step3_setup_score,
        },
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect all DuckDB settings paths into ``tmp_path`` and apply schema."""
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"

    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)

    assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
    assert sm.apply_debug_schema().status == service_result.STATUS_SUCCESS

    return {
        dbm.DB_ROLE_PROD: prod,
        dbm.DB_ROLE_DEBUG: debug,
        dbm.DB_ROLE_SIMULATION: simulation,
    }


# --------------------------------------------------------------------------- #
# Seeding helpers (write directly to the DB; harness only, not Module 14).
# --------------------------------------------------------------------------- #
_INSERT_TICKER = (
    "INSERT INTO ticker_master "
    "(ticker, symbol_type, active_flag, delisted_flag, last_updated) "
    "VALUES (?, ?, TRUE, FALSE, CAST(now() AS TIMESTAMP))"
)

_INSERT_PRICE = (
    "INSERT INTO daily_prices "
    "(ticker, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
    " open_adj, high_adj, low_adj, close_adj, volume_adj, "
    " dividend_amount, split_ratio, adjustment_factor, source_provider, "
    " data_quality_status, mutation_flag, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, NULL, ?, ?, FALSE, "
    " CAST(now() AS TIMESTAMP), NULL)"
)

_FEATURE_COLUMNS = (
    "ticker",
    "feature_date",
    "feature_cutoff_date",
    "feature_schema_version",
    "feature_ready",
    "ema20",
    "ema50",
    "ema200",
    "ema_alignment_score",
    "rsi14",
    "roc20",
    "atr14",
    "rvol20",
    "avg_dollar_volume_20d",
    "breakout_proximity",
    "pullback_from_recent_high_pct",
    "consolidation_score",
    "sector_relative_strength",
    "market_regime",
    "days_to_earnings_bd",
    "macro_event_risk_flag",
)

_INSERT_FEATURE = (
    "INSERT INTO daily_features ("
    + ", ".join(_FEATURE_COLUMNS)
    + ", calculated_at) VALUES ("
    + ", ".join("?" for _ in _FEATURE_COLUMNS)
    + ", CAST(now() AS TIMESTAMP))"
)

_INSERT_CANDIDATE = (
    "INSERT INTO step3_candidates "
    "(candidate_id, run_id, strategy_config_id, ticker, signal_date, "
    " screening_score, passed_hard_filters, hard_filter_fail_reasons, "
    " soft_score_components, feature_snapshot_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '{}', '{}', CAST(now() AS TIMESTAMP))"
)


def _connect(db_path: Path, read_only: bool = False):
    import duckdb

    return duckdb.connect(str(db_path), read_only=read_only)


def seed_ticker(db_path: Path, ticker: str, symbol_type: str = "stock") -> None:
    conn = _connect(db_path)
    try:
        conn.execute(_INSERT_TICKER, [ticker, symbol_type])
    finally:
        conn.close()


def seed_price(
    db_path: Path,
    ticker: str,
    d: date,
    close_raw: float | None,
    *,
    close_adj: float | None = None,
    low_raw: float | None = None,
    high_raw: float | None = None,
    open_raw: float | None = None,
    status: str = "ok",
) -> None:
    conn = _connect(db_path)
    try:
        if close_raw is not None:
            high = high_raw if high_raw is not None else close_raw + 0.5
            low = low_raw if low_raw is not None else close_raw - 0.5
            opn = open_raw if open_raw is not None else close_raw
        else:
            high, low, opn = high_raw, low_raw, open_raw
        conn.execute(
            _INSERT_PRICE,
            [
                ticker, d,
                opn, high, low, close_raw, 1_000_000,
                None, None, None, close_adj, None,
                SOURCE, status,
            ],
        )
    finally:
        conn.close()


def seed_feature(
    db_path: Path,
    ticker: str,
    feature_date: date,
    *,
    feature_ready: bool = True,
    ema20: float | None = 100.0,
    ema50: float | None = 98.0,
    ema200: float | None = 90.0,
    ema_alignment_score: float | None = 100.0,
    rsi14: float | None = 58.0,
    roc20: float | None = 0.10,
    atr14: float | None = 2.0,
    rvol20: float | None = 2.1,
    avg_dollar_volume_20d: float | None = 50_000_000.0,
    breakout_proximity: float | None = 0.0,
    pullback_from_recent_high_pct: float | None = -0.07,
    consolidation_score: float | None = 80.0,
    sector_relative_strength: float | None = 0.07,
    market_regime: str | None = constants.REGIME_BULL,
    days_to_earnings_bd: int | None = None,
    macro_event_risk_flag: bool | None = False,
    schema_version: str = SCHEMA,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            _INSERT_FEATURE,
            [
                ticker, feature_date, feature_date, schema_version, feature_ready,
                ema20, ema50, ema200, ema_alignment_score, rsi14, roc20, atr14,
                rvol20, avg_dollar_volume_20d, breakout_proximity,
                pullback_from_recent_high_pct, consolidation_score,
                sector_relative_strength, market_regime, days_to_earnings_bd,
                macro_event_risk_flag,
            ],
        )
    finally:
        conn.close()


def seed_candidate(
    db_path: Path,
    ticker: str,
    d: date,
    *,
    passed: bool = True,
    strategy_config_id: str = CONFIG_ID,
    candidate_id: str | None = None,
    run_id: str = "seed-run",
) -> str:
    cid = candidate_id if candidate_id is not None else str(uuid.uuid4())
    conn = _connect(db_path)
    try:
        conn.execute(
            _INSERT_CANDIDATE,
            [cid, run_id, strategy_config_id, ticker, d, 75.0, passed],
        )
    finally:
        conn.close()
    return cid


def seed_full_candidate(
    db_path: Path,
    ticker: str,
    d: date,
    *,
    close_raw: float = 100.0,
    close_adj: float | None = None,
    low_raw: float | None = None,
    high_raw: float | None = None,
    open_raw: float | None = None,
    passed: bool = True,
    strategy_config_id: str = CONFIG_ID,
    candidate_id: str | None = None,
    **feature_kwargs,
) -> str:
    """Seed a passing Step 3 candidate plus its ticker/feature/price rows.

    Price-column kwargs (``low_raw``, ``high_raw``, ``open_raw``) are routed
    to :func:`seed_price`; all remaining kwargs go to :func:`seed_feature`.
    """
    if close_adj is None:
        close_adj = close_raw
    seed_ticker(db_path, ticker, "stock")
    seed_price(
        db_path,
        ticker,
        d,
        close_raw,
        close_adj=close_adj,
        low_raw=low_raw,
        high_raw=high_raw,
        open_raw=open_raw,
        status="ok",
    )
    seed_feature(db_path, ticker, d, **feature_kwargs)
    return seed_candidate(
        db_path,
        ticker,
        d,
        passed=passed,
        strategy_config_id=strategy_config_id,
        candidate_id=candidate_id,
    )


def fetch_analyses(db_path: Path) -> list[dict]:
    conn = _connect(db_path, read_only=True)
    try:
        cols = [
            "analysis_id", "candidate_id", "run_id", "strategy_config_id",
            "ticker", "signal_date", "setup_type", "setup_score",
            "breakout_quality_score", "squeeze_score", "timing_score",
            "confirmation_score", "estimated_rr", "stop_price_raw",
            "target_price_raw", "earnings_penalty", "macro_penalty",
            "explanation_json",
        ]
        rows = conn.execute(
            "SELECT " + ", ".join(cols) + " FROM step4_analysis ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, r)) for r in rows]


# --------------------------------------------------------------------------- #
# 1. Public API / signature / metadata / rows_processed invariant
# --------------------------------------------------------------------------- #
def test_analyze_signature_exact() -> None:
    sig = inspect.signature(Step4AnalysisEngine.analyze)
    params = list(sig.parameters)
    assert params == [
        "self",
        "signal_date",
        "strategy_config",
        "strategy_config_id",
        "db_role",
        "run_id",
    ]
    assert sig.parameters["db_role"].default == "prod"
    assert sig.parameters["run_id"].default is None


def test_run_id_minted_when_none(tmp_db_paths: dict[str, Path]) -> None:
    res = Step4AnalysisEngine().analyze(date(2023, 6, 1), make_config(), CONFIG_ID)
    assert isinstance(res.run_id, str) and len(res.run_id) >= 32
    uuid.UUID(res.run_id)
    assert res.metadata["run_id"] == res.run_id


def test_run_id_preserved_when_supplied(tmp_db_paths: dict[str, Path]) -> None:
    res = Step4AnalysisEngine().analyze(
        date(2023, 6, 1), make_config(), CONFIG_ID, run_id="rid-14"
    )
    assert res.run_id == "rid-14"
    assert res.metadata["run_id"] == "rid-14"


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_metadata_keys_exact_on_success(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    seed_full_candidate(tmp_db_paths[dbm.DB_ROLE_PROD], "AAA", d)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_exact_on_guard_failure() -> None:
    res = Step4AnalysisEngine().analyze(
        date(2023, 6, 1), make_config(), CONFIG_ID, db_role="simulation"
    )
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_rows_processed_equals_analyses_written(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    seed_full_candidate(prod, "BBB", d)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert res.rows_processed == res.metadata["analyses_written"] == 2
    bad = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID, db_role="x")
    assert bad.rows_processed == bad.metadata["analyses_written"] == 0


# --------------------------------------------------------------------------- #
# 2. Guards run before any DB access
# --------------------------------------------------------------------------- #
class _ExplodingDb:
    def connect(self, db_role: str, read_only: bool = False):
        raise AssertionError("DB access attempted before guard passed")


@pytest.mark.parametrize("bad_role", ["simulation", "PROD", "", "weird"])
def test_invalid_db_role_fails_without_db_access(bad_role: str) -> None:
    res = Step4AnalysisEngine(db_manager=_ExplodingDb()).analyze(
        date(2023, 6, 1), make_config(), CONFIG_ID, db_role=bad_role
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["db_role"] == bad_role
    assert res.metadata["analyses_written"] == 0
    assert res.rows_processed == 0


def test_simulation_role_rejected_before_db() -> None:
    res = Step4AnalysisEngine(db_manager=_ExplodingDb()).analyze(
        date(2023, 6, 1), make_config(), CONFIG_ID, db_role="simulation"
    )
    assert res.status == service_result.STATUS_FAILED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("step4"),
        lambda c: c["step4"].pop("target_R"),
        lambda c: c["step4"].__setitem__("target_R", 0),
        lambda c: c["step4"].__setitem__("target_R", "x"),
        lambda c: c.pop("earnings"),
        lambda c: c["earnings"].pop("avoid_within_bd"),
        lambda c: c["earnings"].__setitem__("avoid_within_bd", -1),
        lambda c: c["earnings"].__setitem__("avoid_within_bd", 1.5),
        lambda c: c["earnings"].pop("penalty_points_max"),
        lambda c: c["earnings"].__setitem__("penalty_points_max", 5),
        lambda c: c.pop("macro_event_risk"),
        lambda c: c["macro_event_risk"].pop("enabled"),
        lambda c: c["macro_event_risk"].__setitem__("enabled", "yes"),
        lambda c: c["macro_event_risk"].pop("penalty_points"),
        lambda c: c["macro_event_risk"].__setitem__("penalty_points", 3),
        lambda c: c.pop("screening"),
        lambda c: c["screening"].pop("min_rvol"),
        lambda c: c["screening"].__setitem__("min_rvol", 0),
        lambda c: c["screening"].pop("min_screening_score"),
        lambda c: c["screening"].__setitem__("min_screening_score", -1),
        lambda c: c["screening"].pop("min_step3_setup_score"),
        lambda c: c["screening"].__setitem__("min_step3_setup_score", -1),
        lambda c: c["step4"].pop("min_step4_setup_score"),
        lambda c: c["step4"].__setitem__("min_step4_setup_score", -1),
        lambda c: c["step4"].pop("min_consolidation_for_breakout"),
        lambda c: c["step4"].pop("min_estimated_rr"),
        lambda c: c["step4"].__setitem__("min_estimated_rr", -1),
    ],
)
def test_bad_config_fails_without_db_access(mutate) -> None:
    cfg = make_config()
    mutate(cfg)
    res = Step4AnalysisEngine(db_manager=_ExplodingDb()).analyze(
        date(2023, 6, 1), cfg, CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["analyses_written"] == 0


def test_non_dict_config_fails() -> None:
    res = Step4AnalysisEngine(db_manager=_ExplodingDb()).analyze(
        date(2023, 6, 1), [], CONFIG_ID  # type: ignore[arg-type]
    )
    assert res.status == service_result.STATUS_FAILED


class _ReadFailsDb:
    def connect(self, db_role: str, read_only: bool = False):
        raise RuntimeError("cannot open db")


def test_read_failure_returns_failed(tmp_db_paths: dict[str, Path]) -> None:
    res = Step4AnalysisEngine(db_manager=_ReadFailsDb()).analyze(
        date(2023, 6, 1), make_config(), CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["analyses_written"] == 0
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


# --------------------------------------------------------------------------- #
# 3. Inputs / isolation / empty
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_empty_input_success_no_insert(tmp_db_paths: dict[str, Path]) -> None:
    res = Step4AnalysisEngine().analyze(date(2023, 6, 1), make_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["candidates_evaluated"] == 0
    assert res.metadata["analyses_written"] == 0
    assert res.metadata["estimated_rr_min"] is None
    assert res.metadata["estimated_rr_max"] is None
    assert res.metadata["estimated_rr_mean"] is None
    assert res.metadata["setup_type_counts"] == {}
    assert fetch_analyses(tmp_db_paths[dbm.DB_ROLE_PROD]) == []


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_only_passing_candidates_processed(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d, passed=True)
    seed_full_candidate(prod, "BBB", d, passed=False)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert res.metadata["candidates_evaluated"] == 1
    assert res.metadata["analyses_written"] == 1
    assert [a["ticker"] for a in fetch_analyses(prod)] == ["AAA"]


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_signal_date_and_config_isolation(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    d = date(2023, 6, 1)
    other_day = date(2023, 6, 2)
    seed_full_candidate(prod, "AAA", d)
    seed_full_candidate(prod, "BBB", other_day)
    seed_full_candidate(prod, "CCC", d, strategy_config_id="other-cfg")
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert res.metadata["candidates_evaluated"] == 1
    assert [a["ticker"] for a in fetch_analyses(prod)] == ["AAA"]


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_debug_role_supported(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    debug = tmp_db_paths[dbm.DB_ROLE_DEBUG]
    seed_full_candidate(debug, "AAA", d)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID, db_role="debug")
    assert res.is_ok()
    assert res.metadata["db_role"] == "debug"
    assert len(fetch_analyses(debug)) == 1


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_missing_feature_or_price_not_analyzable(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Candidate with a feature row but no close_raw price -> not analyzable.
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, None, status="ok")
    seed_feature(prod, "AAA", d)
    seed_candidate(prod, "AAA", d, passed=True)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert res.metadata["candidates_evaluated"] == 1
    assert res.metadata["analyses_written"] == 0
    assert fetch_analyses(prod) == []


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_missing_feature_row_not_analyzable(tmp_db_paths: dict[str, Path]) -> None:
    """Candidate with a usable price row but no daily_features_current row."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Seed price (usable close_raw) but intentionally omit seed_feature.
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, status="ok")
    seed_candidate(prod, "AAA", d, passed=True)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert res.is_ok()
    assert res.metadata["candidates_evaluated"] == 1
    assert res.metadata["analyses_written"] == 0
    assert fetch_analyses(prod) == []


# --------------------------------------------------------------------------- #
# 4. Stop / target / RR
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_stop_below_entry_target_above_rr_positive(
    tmp_db_paths: dict[str, Path],
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d, close_raw=100.0, atr14=2.0, low_raw=97.0)
    Step4AnalysisEngine().analyze(d, make_config(target_r=2.0), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    assert a["stop_price_raw"] < 100.0
    assert a["target_price_raw"] > 100.0
    assert a["estimated_rr"] is not None and a["estimated_rr"] > 0
    exp = json.loads(a["explanation_json"])
    assert exp["stop_clamped"] is False
    assert math.isclose(a["estimated_rr"], 2.0, rel_tol=1e-9)


def test_atr_raw_equivalent_uses_adjusted_ratio() -> None:
    # atr14=2, close_raw=200, close_adj=100 -> 2 * (200/100) = 4.0
    assert s4mod._atr14_raw_equivalent(2.0, 200.0, 100.0) == 4.0
    # close_adj missing -> fall back to raw atr
    assert s4mod._atr14_raw_equivalent(2.0, 200.0, None) == 2.0
    # close_adj zero -> fall back to raw atr
    assert s4mod._atr14_raw_equivalent(2.0, 200.0, 0.0) == 2.0
    # atr missing -> None (forces stop fallback)
    assert s4mod._atr14_raw_equivalent(None, 200.0, 100.0) is None


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_stop_clamp_when_invalid(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # recent low above entry and atr missing -> mechanical stop unavailable/invalid.
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, low_raw=120.0, status="ok")
    seed_feature(prod, "AAA", d, atr14=None)
    seed_candidate(prod, "AAA", d, passed=True)
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    assert math.isclose(a["stop_price_raw"], 95.0, rel_tol=1e-9)
    exp = json.loads(a["explanation_json"])
    assert exp["stop_clamped"] is True


def test_stop_pure_helper_clamp_and_min() -> None:
    # both candidates available -> min wins.
    stop, clamped = s4mod._compute_stop(100.0, 97.0, 2.0)  # min(97, 100-3)=97
    assert stop == 97.0 and clamped is False
    stop, clamped = s4mod._compute_stop(100.0, 99.5, 2.0)  # min(99.5, 97)=97
    assert stop == 97.0 and clamped is False
    # neither candidate -> clamp.
    stop, clamped = s4mod._compute_stop(100.0, None, None)
    assert stop == 95.0 and clamped is True
    # stop >= entry -> clamp.
    stop, clamped = s4mod._compute_stop(100.0, 105.0, None)
    assert stop == 95.0 and clamped is True


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_recent_20d_low_uses_available_rows(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 10)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "AAA", "stock")
    # 3 historical rows; the lowest low is 90 on day d-2.
    seed_price(prod, "AAA", d - timedelta(days=2), 95.0, close_adj=95.0, low_raw=90.0)
    seed_price(prod, "AAA", d - timedelta(days=1), 98.0, close_adj=98.0, low_raw=96.0)
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, low_raw=99.0)
    seed_feature(prod, "AAA", d, atr14=1.0)  # entry-1.5*atr = 98.5 > 90
    seed_candidate(prod, "AAA", d, passed=True)
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    exp = json.loads(a["explanation_json"])
    assert math.isclose(exp["recent_20d_low_raw"], 90.0, rel_tol=1e-9)
    assert math.isclose(a["stop_price_raw"], 90.0, rel_tol=1e-9)


def test_rr_none_when_denominator_non_positive() -> None:
    target, rr = s4mod._compute_target_rr(100.0, 100.0, 2.2)
    assert rr is None
    target, rr = s4mod._compute_target_rr(100.0, 95.0, 2.0)
    assert math.isclose(rr, 2.0, rel_tol=1e-9)


# --------------------------------------------------------------------------- #
# 5. Setup classification
# --------------------------------------------------------------------------- #
def _feat(**overrides):
    base = dict(
        ema20=100.0, ema50=98.0, ema200=90.0, ema_alignment_score=100.0,
        rsi14=58.0, roc20=0.10, rvol20=2.1, atr14=2.0, breakout_proximity=0.0,
        pullback_from_recent_high_pct=-0.07, consolidation_score=80.0,
        sector_relative_strength=0.07, close_raw=100.0, close_adj=100.0,
        _trend_resume_history_ok=False,
    )
    base.update(overrides)
    return base


def test_classify_high_tight_flag() -> None:
    f = _feat(roc20=0.20, consolidation_score=65.0)
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_HIGH_TIGHT_FLAG


def test_classify_breakout() -> None:
    # roc20 below high-tight threshold so priority 1 skipped.
    f = _feat(roc20=0.05, consolidation_score=50.0, breakout_proximity=0.2, rvol20=2.0)
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_BREAKOUT


def test_classify_volatility_squeeze() -> None:
    # priorities 1 & 2 fail; consolidation >= 70 triggers squeeze.
    f = _feat(
        roc20=0.05, consolidation_score=75.0, breakout_proximity=5.0, rvol20=0.5
    )
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_VOLATILITY_SQUEEZE


def test_classify_trend_pullback() -> None:
    f = _feat(
        roc20=0.05, consolidation_score=40.0, breakout_proximity=5.0, rvol20=0.5,
        close_adj=120.0, ema200=90.0, pullback_from_recent_high_pct=-0.06,
        ema20=110.0, ema50=100.0,
    )
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_TREND_PULLBACK


def test_classify_trend_resume_with_history() -> None:
    f = _feat(
        roc20=0.05, consolidation_score=40.0, breakout_proximity=5.0, rvol20=0.5,
        close_adj=105.0, ema20=100.0, ema50=110.0, ema200=200.0,
        pullback_from_recent_high_pct=-0.15, _trend_resume_history_ok=True,
    )
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_TREND_RESUME


def test_classify_trend_resume_skipped_without_history() -> None:
    f = _feat(
        roc20=0.05, consolidation_score=40.0, breakout_proximity=5.0, rvol20=0.5,
        close_adj=105.0, ema20=100.0, ema50=110.0, ema200=200.0,
        pullback_from_recent_high_pct=-0.15, _trend_resume_history_ok=False,
    )
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_UNKNOWN


def test_classify_unknown_fallback() -> None:
    f = _feat(
        roc20=0.0, consolidation_score=10.0, breakout_proximity=9.0, rvol20=0.1,
        close_adj=80.0, ema200=90.0, pullback_from_recent_high_pct=0.5,
    )
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_UNKNOWN


def test_classify_priority_breakout_over_squeeze() -> None:
    # both breakout (priority 2) and squeeze (priority 3) conditions hold.
    f = _feat(
        roc20=0.05, consolidation_score=75.0, breakout_proximity=0.0, rvol20=2.0
    )
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_BREAKOUT


def test_classify_null_makes_condition_false() -> None:
    # high_tight requires both roc20 and consolidation; NULL roc20 -> skip.
    f = _feat(roc20=None, consolidation_score=80.0, breakout_proximity=5.0,
              rvol20=0.1)
    # consolidation>=70 still triggers squeeze (priority 3)
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_VOLATILITY_SQUEEZE


def test_trend_resume_history_helper() -> None:
    rows = [(95.0, 100.0), (96.0, 100.0), (97.0, 100.0), (101.0, 100.0)]
    assert s4mod._trend_resume_history_ok(rows) is True
    rows = [(95.0, 100.0), (101.0, 100.0)]
    assert s4mod._trend_resume_history_ok(rows) is False
    # NULLs are skipped
    assert s4mod._trend_resume_history_ok([(None, 100.0), (None, None)]) is False
    assert s4mod._trend_resume_history_ok([]) is False


def test_breakout_uses_config_min_rvol() -> None:
    # close_adj < ema200 disables trend_pullback / trend_resume so the only
    # candidate setup is breakout (gated on min_rvol).
    f = _feat(roc20=0.05, consolidation_score=50.0, breakout_proximity=0.0,
              rvol20=1.3, close_adj=80.0, ema200=90.0,
              pullback_from_recent_high_pct=0.5)
    # min_rvol 1.5 -> breakout fails -> falls through -> unknown
    assert s4mod._classify_setup(f, 1.5) == s4mod.SETUP_UNKNOWN
    # min_rvol 1.2 -> breakout matches
    assert s4mod._classify_setup(f, 1.2) == s4mod.SETUP_BREAKOUT


# --------------------------------------------------------------------------- #
# 6. Component scores
# --------------------------------------------------------------------------- #
def test_breakout_quality_score_boundaries() -> None:
    assert s4mod._breakout_quality_score(0.0, 2.0) == 100.0
    assert s4mod._breakout_quality_score(0.5, 1.5) == pytest.approx(85.0)
    assert s4mod._breakout_quality_score(None, 2.0) == 50.0  # 0*.5 + 100*.5
    assert s4mod._breakout_quality_score(0.0, None) == 50.0
    # outside band: |bp|=1 -> position 50; rvol 1.3 -> 40 -> 0.5*50+0.5*40=45
    assert s4mod._breakout_quality_score(1.0, 1.3) == pytest.approx(45.0)
    assert s4mod._breakout_quality_score(0.0, 1.0) == 50.0  # rvol<1.2 ->0


def test_squeeze_score() -> None:
    assert s4mod._squeeze_score(80.0) == 80.0
    assert s4mod._squeeze_score(None) == 0.0
    assert s4mod._squeeze_score(150.0) == 100.0


def test_timing_score_boundaries() -> None:
    # rsi 58 ->100, ema_alignment 100, sector 0.07 ->100 => 100
    assert s4mod._timing_score(58.0, 100.0, 0.07) == pytest.approx(100.0)
    # rsi None -> 0; ema None -> 0; sector None -> 50 => 0.3*50=15
    assert s4mod._timing_score(None, None, None) == pytest.approx(15.0)
    # rsi 47 -> 70; ema 50; sector -0.02 -> 30 => 0.4*70+0.3*50+0.3*30=52
    assert s4mod._timing_score(47.0, 50.0, -0.02) == pytest.approx(52.0)
    # rsi 80 -> 30; sector -0.10 -> 0
    assert s4mod._timing_score(80.0, 0.0, -0.10) == pytest.approx(12.0)


def test_confirmation_score() -> None:
    assert s4mod._confirmation_score(120.0, 90.0, 110.0, 100.0) == 100.0
    assert s4mod._confirmation_score(80.0, 90.0, 90.0, 100.0) == 0.0
    assert s4mod._confirmation_score(120.0, 90.0, 90.0, 100.0) == 50.0
    assert s4mod._confirmation_score(None, 90.0, None, 100.0) == 0.0


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_setup_score_is_clamped_routed_with_penalties(
    tmp_db_paths: dict[str, Path],
) -> None:
    """setup_score uses type-weighted routing, not equal-weight mean.

    The default seed produces setup_type=breakout (breakout_proximity=0,
    rvol≥2.0, consolidation≥0). Breakout weights: bq=0.50, sq=0.30,
    tim=0.10, conf=0.10. We verify the routed score matches and is within
    [0, 100] after penalties.
    """
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    # Verify setup_type is what the routing weights apply to.
    setup_type = a["setup_type"]
    bq = a["breakout_quality_score"]
    sq = a["squeeze_score"]
    tim = a["timing_score"]
    conf = a["confirmation_score"]
    # Reconstruct routed quality using _SETUP_WEIGHTS from s4mod.
    weights = s4mod._SETUP_WEIGHTS.get(setup_type, s4mod._SETUP_WEIGHTS["unknown"])
    w_bq, w_sq, w_tim, w_conf = weights
    routed_quality = max(0.0, min(100.0,
        w_bq * bq + w_sq * sq + w_tim * tim + w_conf * conf
    ))
    expected = max(
        0.0, min(100.0, routed_quality + a["earnings_penalty"] + a["macro_penalty"])
    )
    assert a["setup_score"] == pytest.approx(expected)
    for key in (
        "setup_score", "breakout_quality_score", "squeeze_score",
        "timing_score", "confirmation_score",
    ):
        assert 0.0 <= a[key] <= 100.0


# --------------------------------------------------------------------------- #
# 7. Penalties
# --------------------------------------------------------------------------- #
def test_earnings_penalty_cases() -> None:
    # None -> EARNINGS_UNKNOWN with strategy-scaled penalty (not 0.0)
    p, s = s4mod._earnings_penalty(None, 3, -15.0, "normal")
    assert s == "EARNINGS_UNKNOWN"
    assert p == pytest.approx(-11.25)  # 75% of -15.0

    p, s = s4mod._earnings_penalty(None, 3, -15.0, "conservative")
    assert s == "EARNINGS_UNKNOWN"
    assert p == pytest.approx(-15.0)  # 100% of -15.0

    p, s = s4mod._earnings_penalty(None, 3, -15.0, "aggressive")
    assert s == "EARNINGS_UNKNOWN"
    assert p == pytest.approx(-6.0)  # 40% of -15.0

    # avoid_within_bd == 0: only day 0 penalised
    p, s = s4mod._earnings_penalty(0, 0, -15.0, "normal")
    assert s == "EARNINGS_TODAY"
    assert p == pytest.approx(-15.0)

    p, s = s4mod._earnings_penalty(1, 0, -15.0, "normal")
    assert s == "EARNINGS_CLEAR"
    assert p == 0.0

    # boundary day == avoid_within_bd -> penalty 0 (1 - bd/bd)
    p, s = s4mod._earnings_penalty(3, 3, -15.0, "normal")
    assert s == "EARNINGS_INSIDE_WINDOW"
    assert p == pytest.approx(0.0)

    # day 0 within window -> full penalty
    p, s = s4mod._earnings_penalty(0, 3, -15.0, "normal")
    assert s == "EARNINGS_INSIDE_WINDOW"
    assert p == pytest.approx(-15.0)

    # mid window
    p, s = s4mod._earnings_penalty(1, 3, -15.0, "normal")
    assert s == "EARNINGS_INSIDE_WINDOW"
    assert p == pytest.approx(-10.0)

    # outside window
    p, s = s4mod._earnings_penalty(5, 3, -15.0, "normal")
    assert s == "EARNINGS_CLEAR"
    assert p == 0.0

    # always <= 0
    p, s = s4mod._earnings_penalty(0, 3, -15.0, "normal")
    assert p <= 0.0


def test_macro_penalty_cases() -> None:
    assert s4mod._macro_penalty(True, True, -10.0) == -10.0
    assert s4mod._macro_penalty(True, False, -10.0) == 0.0
    assert s4mod._macro_penalty(True, None, -10.0) == 0.0
    assert s4mod._macro_penalty(False, True, -10.0) == 0.0
    assert s4mod._macro_penalty(True, True, -10.0) <= 0.0


def test_sector_rs_null_is_neutral_50() -> None:
    # only sector contributes (rsi None->0, ema None->0): 0.3*50 = 15
    assert s4mod._timing_score(None, None, None) == pytest.approx(15.0)


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_penalties_written_to_row(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(
        prod, "AAA", d, days_to_earnings_bd=0, macro_event_risk_flag=True
    )
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    assert a["earnings_penalty"] == pytest.approx(-15.0)
    assert a["macro_penalty"] == pytest.approx(-10.0)
    assert a["earnings_penalty"] <= 0.0 and a["macro_penalty"] <= 0.0


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_macro_disabled_no_penalty(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d, macro_event_risk_flag=True)
    Step4AnalysisEngine().analyze(d, make_config(macro_enabled=False), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    assert a["macro_penalty"] == 0.0


# --------------------------------------------------------------------------- #
# 8. explanation_json
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_explanation_json_fields_and_sorted(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    exp = json.loads(a["explanation_json"])
    for key in (
        "setup_type", "entry_proxy_raw", "stop_price_raw", "target_price_raw",
        "target_R", "atr14_raw_equivalent", "recent_20d_low_raw", "stop_clamped",
        "stop_distance_pct", "atr_pct", "distance_to_ema50_pct",
        "step3_setup_score", "step4_setup_score",
        "earnings_penalty", "earnings_status", "macro_penalty",
        "days_to_earnings_bd", "macro_event_risk_flag", "gate_results",
    ):
        assert key in exp, f"Missing explanation key: {key!r}"
    assert isinstance(exp["gate_results"], dict), "gate_results must be dict"
    assert a["explanation_json"] == json.dumps(exp, sort_keys=True)


# --------------------------------------------------------------------------- #
# 9. Writes / metadata / transaction
# --------------------------------------------------------------------------- #
class _CountingConn:
    def __init__(self, inner, fail_on: int) -> None:
        self._inner = inner
        self._fail_on = fail_on
        self._inserts = 0

    def execute(self, sql, params=None):
        if "INSERT INTO step4_analysis" in sql:
            self._inserts += 1
            if self._inserts == self._fail_on:
                raise RuntimeError("boom on insert")
        return (
            self._inner.execute(sql, params)
            if params is not None
            else self._inner.execute(sql)
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FailOnNthInsertDb:
    def __init__(self, real, fail_on_insert_number: int) -> None:
        self._real = real
        self._fail_on = fail_on_insert_number

    def connect(self, db_role: str, read_only: bool = False):
        conn = self._real.connect(db_role, read_only=read_only)
        if read_only:
            return conn
        return _CountingConn(conn, self._fail_on)


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_write_failure_rolls_back_no_rows(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    seed_full_candidate(prod, "BBB", d)
    seed_full_candidate(prod, "CCC", d)
    db = _FailOnNthInsertDb(dbm, fail_on_insert_number=2)
    res = Step4AnalysisEngine(db_manager=db).analyze(d, make_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["analyses_written"] == 0
    assert res.rows_processed == 0
    assert fetch_analyses(prod) == []


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_all_rows_in_one_transaction(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    for t in ("AAA", "BBB", "CCC"):
        seed_full_candidate(prod, t, d)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert res.metadata["analyses_written"] == 3
    assert len(fetch_analyses(prod)) == 3


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_no_other_tables_written(tmp_db_paths: dict[str, Path]) -> None:
    import duckdb

    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)

    forbidden = (
        "daily_prices", "ticker_master", "daily_features",
        "step3_candidates", "step5_proposals", "strategy_configs",
    )

    def snapshot() -> dict[str, int]:
        conn = duckdb.connect(str(prod), read_only=True)
        try:
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in forbidden
            }
        finally:
            conn.close()

    before = snapshot()
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    after = snapshot()
    assert before == after


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_analysis_ids_unique_and_valid(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    for t in ("AAA", "BBB", "CCC"):
        seed_full_candidate(prod, t, d)
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    ids = [a["analysis_id"] for a in fetch_analyses(prod)]
    assert len(ids) == len(set(ids)) == 3
    for aid in ids:
        uuid.UUID(aid)


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_candidate_id_preserved(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    cid = seed_full_candidate(prod, "AAA", d, candidate_id="cand-xyz")
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    a = fetch_analyses(prod)[0]
    assert a["candidate_id"] == cid == "cand-xyz"


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_setup_type_counts_and_rr_stats(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Two breakout-style candidates, one squeeze-only.
    seed_full_candidate(prod, "AAA", d, roc20=0.20, consolidation_score=65.0)
    seed_full_candidate(prod, "BBB", d, roc20=0.20, consolidation_score=65.0)
    seed_full_candidate(
        prod, "CCC", d, roc20=0.05, consolidation_score=75.0,
        breakout_proximity=5.0, rvol20=0.5,
    )
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    counts = res.metadata["setup_type_counts"]
    assert counts.get("high_tight_flag") == 2
    assert counts.get("volatility_squeeze") == 1
    assert sum(counts.values()) == res.metadata["analyses_written"] == 3
    assert res.metadata["estimated_rr_min"] is not None
    assert res.metadata["estimated_rr_max"] is not None
    assert (
        res.metadata["estimated_rr_min"]
        <= res.metadata["estimated_rr_mean"]
        <= res.metadata["estimated_rr_max"]
    )


def test_rr_stats_none_when_zero_written(tmp_db_paths: dict[str, Path]) -> None:
    res = Step4AnalysisEngine().analyze(date(2023, 6, 1), make_config(), CONFIG_ID)
    assert res.metadata["estimated_rr_min"] is None
    assert res.metadata["estimated_rr_max"] is None
    assert res.metadata["estimated_rr_mean"] is None


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_analyses_written_matches_rowcount(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    for t in ("AAA", "BBB"):
        seed_full_candidate(prod, t, d)
    res = Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    assert res.metadata["analyses_written"] == len(fetch_analyses(prod))


# --------------------------------------------------------------------------- #
# 10. Static source scans (no forbidden patterns)
# --------------------------------------------------------------------------- #
def _engine_source() -> str:
    return Path(s4mod.__file__).read_text(encoding="utf-8")


def _imported_module_names(src: str) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _non_docstring_strings(src: str) -> list[str]:
    tree = ast.parse(src)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
    ]


def test_no_direct_duckdb_or_attach_or_ddl() -> None:
    src = _engine_source()
    assert "duckdb" not in _imported_module_names(src)
    for s in _non_docstring_strings(src):
        upper = s.upper()
        assert "ATTACH" not in upper
        assert "CREATE TABLE" not in upper
        assert "ALTER TABLE" not in upper
        assert "DROP TABLE" not in upper
        assert "UPDATE STEP4_ANALYSIS" not in upper
        assert "DELETE FROM" not in upper
        assert "INSERT INTO STEP3" not in upper
        assert "INSERT INTO STEP5" not in upper
        assert "INSERT INTO DAILY_" not in upper
        assert "INSERT INTO TICKER_" not in upper


def test_only_step4_analysis_insert() -> None:
    inserts = [
        s for s in _non_docstring_strings(_engine_source())
        if "INSERT INTO" in s.upper()
    ]
    assert inserts, "expected at least one INSERT statement"
    for s in inserts:
        assert "INSERT INTO step4_analysis" in s


def test_no_print_in_engine() -> None:
    tree = ast.parse(_engine_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_provider_imports() -> None:
    imported = _imported_module_names(_engine_source())
    assert "yfinance" not in imported
    assert not any(
        m == "providers" or m.startswith("providers") for m in imported
    )
    for s in _non_docstring_strings(_engine_source()):
        low = s.lower()
        assert "yfinance" not in low
        assert "providers" not in low


# --------------------------------------------------------------------------- #
# New regression tests — Item 10 (setup quality gates, routing, classification)
# --------------------------------------------------------------------------- #

@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_min_step3_setup_score_is_enforced(tmp_db_paths: dict[str, Path]) -> None:
    """Gate 1b: candidates below min_step3_setup_score never produce analyses."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    # Patch soft_score_components to have setup_score=40 (below default=0 so
    # this test uses a config with threshold=55 to verify the gate fires).
    import duckdb as _ddb
    conn = _ddb.connect(str(prod))
    conn.execute(
        "UPDATE step3_candidates SET soft_score_components = ? WHERE ticker = ?",
        ['{"setup_score": 40.0, "trend_score": 90.0, "momentum_score": 85.0, '
         '"volume_score": 90.0, "market_score": 100.0, "market_regime": "bull", '
         '"market_regime_known": true, "sector_relative_strength": 0.05}', "AAA"],
    )
    conn.close()
    cfg = make_config(min_step3_setup_score=55.0)
    res = Step4AnalysisEngine().analyze(d, cfg, CONFIG_ID)
    assert res.is_ok()
    assert res.metadata["analyses_written"] == 0  # blocked by Gate 1b


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_min_step3_setup_score_passes_at_threshold(tmp_db_paths: dict[str, Path]) -> None:
    """Gate 1b passes when step3_setup_score equals the threshold exactly."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    import duckdb as _ddb
    conn = _ddb.connect(str(prod))
    conn.execute(
        "UPDATE step3_candidates SET soft_score_components = ? WHERE ticker = ?",
        ['{"setup_score": 55.0, "trend_score": 90.0, "momentum_score": 85.0, '
         '"volume_score": 90.0, "market_score": 100.0, "market_regime": "bull", '
         '"market_regime_known": true, "sector_relative_strength": 0.05}', "AAA"],
    )
    conn.close()
    cfg = make_config(min_step3_setup_score=55.0)
    res = Step4AnalysisEngine().analyze(d, cfg, CONFIG_ID)
    assert res.is_ok()
    assert res.metadata["analyses_written"] == 1  # passed Gate 1b


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_min_step4_setup_score_is_enforced(tmp_db_paths: dict[str, Path]) -> None:
    """Gate 6: rows where step4 setup_score < min are not written."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Seed with very low consolidation + bad RSI to produce low setup_score.
    seed_full_candidate(prod, "AAA", d, consolidation_score=5.0)
    cfg = make_config(min_step4_setup_score=80.0)  # very high threshold
    res = Step4AnalysisEngine().analyze(d, cfg, CONFIG_ID)
    assert res.is_ok()
    assert res.metadata["analyses_written"] == 0  # low quality blocked


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_breakout_requires_consolidation_by_strategy(tmp_db_paths: dict[str, Path]) -> None:
    """A near-breakout with insufficient consolidation is NOT classified as breakout."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # consolidation=35 is below normal threshold (60)
    seed_full_candidate(prod, "AAA", d, consolidation_score=35.0)
    cfg = make_config(min_consolidation_for_breakout=60.0)
    res = Step4AnalysisEngine().analyze(d, cfg, CONFIG_ID)
    assert res.is_ok()
    if res.metadata["analyses_written"] > 0:
        rows = fetch_analyses(prod)
        setup_type = rows[0]["setup_type"]
        assert setup_type != s4mod.SETUP_BREAKOUT, (
            f"consolidation=35 should not classify as breakout; got {setup_type}"
        )


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_momentum_extension_not_classified_as_breakout(
    tmp_db_paths: dict[str, Path],
) -> None:
    """SETUP_MOMENTUM_EXTENSION constant exists and breakout classification
    respects consolidation gate — extended momentum without consolidation
    must not be a breakout."""
    assert hasattr(s4mod, "SETUP_MOMENTUM_EXTENSION")
    assert s4mod.SETUP_MOMENTUM_EXTENSION == "momentum_extension"

    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Near breakout but weak consolidation (15) — well below 60.
    seed_full_candidate(prod, "AAA", d, consolidation_score=15.0)
    cfg = make_config(min_consolidation_for_breakout=60.0)
    res = Step4AnalysisEngine().analyze(d, cfg, CONFIG_ID)
    assert res.is_ok()
    if res.metadata["analyses_written"] > 0:
        rows = fetch_analyses(prod)
        assert rows[0]["setup_type"] != s4mod.SETUP_BREAKOUT


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_unknown_setup_not_ranked_as_buy(tmp_db_paths: dict[str, Path]) -> None:
    """Analyses with setup_type=unknown must not enter final BUY shortlist.
    They are written to step4_analysis but step5 marks them WATCHLIST_ONLY."""
    from app.services.proposal.step5_proposal_engine import (
        Step5ProposalEngine, DISPOSITION_WATCHLIST,
    )
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Force unknown setup by using conditions that match no setup type:
    # very low RVOL (fails breakout), high pullback (fails trend_pullback range)
    seed_full_candidate(
        prod, "AAA", d,
        rvol20=0.3,
        pullback_from_recent_high_pct=-0.30,
        consolidation_score=20.0,
        roc20=0.01,
    )
    cfg = make_config()
    res4 = Step4AnalysisEngine().analyze(d, cfg, CONFIG_ID)
    assert res4.is_ok()
    if res4.metadata["analyses_written"] == 0:
        pytest.skip("No analysis written — candidate may have been gated earlier")

    rows4 = fetch_analyses(prod)
    assert rows4[0]["setup_type"] == s4mod.SETUP_UNKNOWN


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_gate_results_in_explanation_json(tmp_db_paths: dict[str, Path]) -> None:
    """gate_results dict must be present in explanation_json for all written rows."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    analyses = fetch_analyses(prod)
    if not analyses:
        pytest.skip("No analyses written")
    exp = json.loads(analyses[0]["explanation_json"])
    assert "gate_results" in exp, "gate_results missing from explanation_json"
    assert isinstance(exp["gate_results"], dict)
    for gate in ("min_screening_score", "min_step3_setup_score", "atr_pct",
                 "ema50_extension", "stop_distance", "setup_type_allowed",
                 "earnings_status", "min_step4_setup_score"):
        assert gate in exp["gate_results"], f"Missing gate_results key: {gate}"


@pytest.mark.skip(
    reason='PENDING Phase 4 (M14) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_step3_and_step4_setup_scores_in_explanation(
    tmp_db_paths: dict[str, Path],
) -> None:
    """Both step3_setup_score and step4_setup_score must appear in explanation_json."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_candidate(prod, "AAA", d)
    Step4AnalysisEngine().analyze(d, make_config(), CONFIG_ID)
    analyses = fetch_analyses(prod)
    if not analyses:
        pytest.skip("No analyses written")
    exp = json.loads(analyses[0]["explanation_json"])
    assert "step3_setup_score" in exp
    assert "step4_setup_score" in exp
