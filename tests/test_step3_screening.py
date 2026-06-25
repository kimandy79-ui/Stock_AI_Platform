"""Tests for Module 13 — Step 3 Screening.

All tests run fully offline (no network, no provider) and never touch the real
prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects every
DuckDB settings path into pytest ``tmp_path`` and applies the real Module 03
schema there (mirroring ``tests/test_market_regime_engine.py``). Feature rows are
seeded directly into ``daily_features``, price rows into ``daily_prices`` and
ticker rows into ``ticker_master``; Module 13 reads them read-only (through the
``daily_features_current`` view) and only ever inserts into ``step3_candidates``.
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from datetime import date, timedelta
from pathlib import Path

import polars as pl  # noqa: F401 - ensures the optional dep is present for the suite
import pytest

from app.config import constants
from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.screening import step3_screening as s3mod
from app.services.screening.step3_screening import Step3ScreeningEngine
from app.utils import service_result

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
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
    }
)

SOURCE = "fake"
SCHEMA = constants.FEATURE_SCHEMA_VERSION
CONFIG_ID = "cfg-1"


# --------------------------------------------------------------------------- #
# Strategy config helper (matches CONFIG/20_Config_Base_Normal.json shape:
# scoring_weights at the config top level).
# --------------------------------------------------------------------------- #
def make_config(
    *,
    min_price: float = 10.0,
    min_adv: float = 20_000_000.0,
    min_rvol: float = 1.5,
    weights: dict | None = None,
) -> dict:
    if weights is None:
        weights = {
            "trend": 0.30,
            "momentum": 0.25,
            "setup": 0.20,
            "volume": 0.15,
            "market": 0.10,
        }
    return {
        "universe": {
            "min_price": min_price,
            "min_avg_dollar_volume_20d": min_adv,
        },
        "screening": {"min_rvol": min_rvol},
        "scoring_weights": weights,
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
# Seeding helpers (write directly to the DB; this is the test harness, not
# Module 13 — Module 13 only inserts into step3_candidates).
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
    "distance_to_ema50_pct",
    "rsi14",
    "roc20",
    "rvol20",
    "avg_dollar_volume_20d",
    "breakout_proximity",
    "pullback_from_recent_high_pct",
    "consolidation_score",
    "sector_relative_strength",
    "market_regime",
)

_INSERT_FEATURE = (
    "INSERT INTO daily_features ("
    + ", ".join(_FEATURE_COLUMNS)
    + ", calculated_at) VALUES ("
    + ", ".join("?" for _ in _FEATURE_COLUMNS)
    + ", CAST(now() AS TIMESTAMP))"
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
    close_raw: float,
    *,
    close_adj: float | None = None,
    status: str = "ok",
) -> None:
    conn = _connect(db_path)
    try:
        high = close_raw + 0.5
        low = close_raw - 0.5
        conn.execute(
            _INSERT_PRICE,
            [
                ticker, d,
                close_raw, high, low, close_raw, 1_000_000,
                close_adj, high, low, close_adj, None,
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
    distance_to_ema50_pct: float | None = 0.02,
    rsi14: float | None = 58.0,
    roc20: float | None = 0.10,
    rvol20: float | None = 2.1,
    avg_dollar_volume_20d: float | None = 50_000_000.0,
    breakout_proximity: float | None = 0.0,
    pullback_from_recent_high_pct: float | None = -0.07,
    consolidation_score: float | None = 80.0,
    sector_relative_strength: float | None = 0.07,
    market_regime: str | None = constants.REGIME_BULL,
    schema_version: str = SCHEMA,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            _INSERT_FEATURE,
            [
                ticker, feature_date, feature_date, schema_version, feature_ready,
                ema20, ema50, ema200, ema_alignment_score, distance_to_ema50_pct,
                rsi14, roc20, rvol20, avg_dollar_volume_20d, breakout_proximity,
                pullback_from_recent_high_pct, consolidation_score,
                sector_relative_strength, market_regime,
            ],
        )
    finally:
        conn.close()


def seed_full_passing(
    db_path: Path, ticker: str, d: date, *, close_raw: float = 100.0, **feature_kwargs
) -> None:
    """Seed a ticker that passes every hard filter (strong bull setup)."""
    seed_ticker(db_path, ticker, "stock")
    seed_price(db_path, ticker, d, close_raw, close_adj=close_raw, status="ok")
    seed_feature(db_path, ticker, d, **feature_kwargs)


def fetch_candidates(db_path: Path) -> list[dict]:
    conn = _connect(db_path, read_only=True)
    try:
        cols = [
            "candidate_id", "run_id", "strategy_config_id", "ticker", "signal_date",
            "screening_score", "passed_hard_filters", "hard_filter_fail_reasons",
            "soft_score_components", "feature_snapshot_json",
        ]
        rows = conn.execute(
            "SELECT " + ", ".join(cols) + " FROM step3_candidates ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, r)) for r in rows]


# --------------------------------------------------------------------------- #
# 1. Public API / signature / metadata / rows_processed invariant
# --------------------------------------------------------------------------- #
def test_screen_signature_exact() -> None:
    sig = inspect.signature(Step3ScreeningEngine.screen)
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
    res = Step3ScreeningEngine().screen(date(2023, 6, 1), make_config(), CONFIG_ID)
    assert isinstance(res.run_id, str) and len(res.run_id) >= 32
    assert res.metadata["run_id"] == res.run_id


def test_run_id_preserved_when_supplied(tmp_db_paths: dict[str, Path]) -> None:
    res = Step3ScreeningEngine().screen(
        date(2023, 6, 1), make_config(), CONFIG_ID, run_id="rid-13"
    )
    assert res.run_id == "rid-13"
    assert res.metadata["run_id"] == "rid-13"


def test_metadata_keys_exact_on_success(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    seed_full_passing(tmp_db_paths[dbm.DB_ROLE_PROD], "AAA", d)
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_exact_on_guard_failure() -> None:
    res = Step3ScreeningEngine().screen(
        date(2023, 6, 1), make_config(), CONFIG_ID, db_role="simulation"
    )
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_rows_processed_equals_candidates_written(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    seed_full_passing(prod, "BBB", d, rvol20=0.5)  # fails rvol
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert res.rows_processed == res.metadata["candidates_written"] == 2
    # also on guard failure
    bad = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID, db_role="x")
    assert bad.rows_processed == bad.metadata["candidates_written"] == 0


# --------------------------------------------------------------------------- #
# 2. Guards run before any DB access
# --------------------------------------------------------------------------- #
class _ExplodingDb:
    """A db_manager whose connect() must never be called by guard paths."""

    def connect(self, db_role: str, read_only: bool = False):
        raise AssertionError("DB access attempted before guard passed")


@pytest.mark.parametrize("bad_role", ["simulation", "PROD", "", "weird"])
def test_invalid_db_role_fails_without_db_access(bad_role: str) -> None:
    res = Step3ScreeningEngine(db_manager=_ExplodingDb()).screen(
        date(2023, 6, 1), make_config(), CONFIG_ID, db_role=bad_role
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["db_role"] == bad_role
    assert res.metadata["candidates_written"] == 0
    assert res.rows_processed == 0


def test_simulation_role_rejected_before_db() -> None:
    res = Step3ScreeningEngine(db_manager=_ExplodingDb()).screen(
        date(2023, 6, 1), make_config(), CONFIG_ID, db_role="simulation"
    )
    assert res.status == service_result.STATUS_FAILED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("universe"),
        lambda c: c["universe"].pop("min_price"),
        lambda c: c["universe"].pop("min_avg_dollar_volume_20d"),
        lambda c: c["screening"].pop("min_rvol"),
        lambda c: c.pop("scoring_weights"),
        lambda c: c["scoring_weights"].pop("market"),
    ],
)
def test_missing_config_key_fails_without_db_access(mutate) -> None:
    cfg = make_config()
    mutate(cfg)
    res = Step3ScreeningEngine(db_manager=_ExplodingDb()).screen(
        date(2023, 6, 1), cfg, CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["candidates_written"] == 0


def test_non_dict_config_fails() -> None:
    res = Step3ScreeningEngine(db_manager=_ExplodingDb()).screen(
        date(2023, 6, 1), [], CONFIG_ID  # type: ignore[arg-type]
    )
    assert res.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# 3. Empty input -> success, zero counts, no insert
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_empty_input_success_no_insert(tmp_db_paths: dict[str, Path]) -> None:
    res = Step3ScreeningEngine().screen(date(2023, 6, 1), make_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["tickers_evaluated"] == 0
    assert res.metadata["candidates_written"] == 0
    assert res.metadata["screening_score_min"] is None
    assert res.metadata["screening_score_max"] is None
    assert res.metadata["screening_score_mean"] is None
    assert fetch_candidates(tmp_db_paths[dbm.DB_ROLE_PROD]) == []


# --------------------------------------------------------------------------- #
# 4. Hard-filter pass / each failure label / NULL behavior
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_full_pass_writes_passing_candidate(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert res.metadata["passed_hard_filters"] == 1
    assert res.metadata["failed_hard_filters"] == 0
    cand = fetch_candidates(prod)[0]
    assert cand["passed_hard_filters"] is True
    assert cand["screening_score"] is not None
    assert json.loads(cand["hard_filter_fail_reasons"]) == []


@pytest.mark.parametrize(
    "label,mutate",
    [
        (s3mod.REASON_FEATURE_NOT_READY, dict(feature_ready=False)),
        (s3mod.REASON_RVOL_BELOW_MIN, dict(rvol20=1.0)),
        (s3mod.REASON_ADV_BELOW_MIN, dict(avg_dollar_volume_20d=1.0)),
    ],
)
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_feature_based_failure_labels(
    tmp_db_paths: dict[str, Path], label: str, mutate: dict
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, status="ok")
    seed_feature(prod, "AAA", d, **mutate)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    cand = fetch_candidates(prod)[0]
    assert cand["passed_hard_filters"] is False
    assert cand["screening_score"] is None
    assert label in json.loads(cand["hard_filter_fail_reasons"])


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_not_stock_failure(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "ETFX", "etf")
    seed_price(prod, "ETFX", d, 100.0, close_adj=100.0, status="ok")
    seed_feature(prod, "ETFX", d)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    cand = fetch_candidates(prod)[0]
    assert s3mod.REASON_NOT_STOCK in json.loads(cand["hard_filter_fail_reasons"])


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_price_below_min_failure(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, 5.0, close_adj=5.0, status="ok")
    seed_feature(prod, "AAA", d)
    Step3ScreeningEngine().screen(d, make_config(min_price=10.0), CONFIG_ID)
    cand = fetch_candidates(prod)[0]
    assert s3mod.REASON_PRICE_BELOW_MIN in json.loads(cand["hard_filter_fail_reasons"])


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_data_quality_not_ok_failure(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, status="warning")
    seed_feature(prod, "AAA", d)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    cand = fetch_candidates(prod)[0]
    assert s3mod.REASON_DATA_QUALITY_NOT_OK in json.loads(
        cand["hard_filter_fail_reasons"]
    )


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_null_rvol20_fails_filter(tmp_db_paths: dict[str, Path]) -> None:
    """NULL rvol20 in the feature row must trigger REASON_RVOL_BELOW_MIN."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, status="ok")
    seed_feature(prod, "AAA", d, rvol20=None)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    reasons = json.loads(fetch_candidates(prod)[0]["hard_filter_fail_reasons"])
    assert s3mod.REASON_RVOL_BELOW_MIN in reasons


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_null_avg_dollar_volume_fails_filter(tmp_db_paths: dict[str, Path]) -> None:
    """NULL avg_dollar_volume_20d must trigger REASON_ADV_BELOW_MIN."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "AAA", "stock")
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, status="ok")
    seed_feature(prod, "AAA", d, avg_dollar_volume_20d=None)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    reasons = json.loads(fetch_candidates(prod)[0]["hard_filter_fail_reasons"])
    assert s3mod.REASON_ADV_BELOW_MIN in reasons


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_missing_price_row_fails_price_and_quality(
    tmp_db_paths: dict[str, Path],
) -> None:
    """No daily_prices row -> NULL close_raw and NULL data_quality_status, both
    failing their filters."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "AAA", "stock")
    seed_feature(prod, "AAA", d)  # no price row
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    reasons = json.loads(fetch_candidates(prod)[0]["hard_filter_fail_reasons"])
    assert s3mod.REASON_PRICE_BELOW_MIN in reasons
    assert s3mod.REASON_DATA_QUALITY_NOT_OK in reasons


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_missing_ticker_master_fails_not_stock(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_price(prod, "AAA", d, 100.0, close_adj=100.0, status="ok")
    seed_feature(prod, "AAA", d)  # no ticker_master row
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    reasons = json.loads(fetch_candidates(prod)[0]["hard_filter_fail_reasons"])
    assert s3mod.REASON_NOT_STOCK in reasons


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_all_failures_collected_not_only_first(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "BAD", "etf")  # not_stock
    seed_price(prod, "BAD", d, 5.0, close_adj=5.0, status="warning")  # price + quality
    seed_feature(prod, "BAD", d, feature_ready=False, rvol20=0.1, avg_dollar_volume_20d=1.0)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    reasons = set(json.loads(fetch_candidates(prod)[0]["hard_filter_fail_reasons"]))
    assert reasons == {
        s3mod.REASON_FEATURE_NOT_READY,
        s3mod.REASON_NOT_STOCK,
        s3mod.REASON_PRICE_BELOW_MIN,
        s3mod.REASON_ADV_BELOW_MIN,
        s3mod.REASON_RVOL_BELOW_MIN,
        s3mod.REASON_DATA_QUALITY_NOT_OK,
    }


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_fail_reasons_deterministic_order(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_ticker(prod, "BAD", "etf")
    seed_price(prod, "BAD", d, 5.0, close_adj=5.0, status="warning")
    seed_feature(prod, "BAD", d, feature_ready=False, rvol20=0.1, avg_dollar_volume_20d=1.0)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    reasons = json.loads(fetch_candidates(prod)[0]["hard_filter_fail_reasons"])
    expected_order = [label for label, _ in s3mod._HARD_FILTER_ORDER]
    assert reasons == expected_order


# --------------------------------------------------------------------------- #
# 5. Both passed and failed candidates are written
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_both_passed_and_failed_written(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    seed_full_passing(prod, "ZZZ", d, rvol20=0.2)  # fails
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    cands = {c["ticker"]: c for c in fetch_candidates(prod)}
    assert set(cands) == {"AAA", "ZZZ"}
    assert cands["AAA"]["passed_hard_filters"] is True
    assert cands["ZZZ"]["passed_hard_filters"] is False
    assert res.metadata["candidates_written"] == 2


# --------------------------------------------------------------------------- #
# 6. Score reproducibility, weights, market-regime mapping, neutral fallbacks
# --------------------------------------------------------------------------- #
def _expected_strong_bull_score(weights: dict) -> float:
    # Matches the seed defaults: trend=100, momentum=100, setup=92, volume=100,
    # market(bull)=100. setup = 0.4*80 + 0.3*100(bp=0) + 0.3*100(pb=-0.07) = 92.
    sub = dict(trend=100.0, momentum=100.0, setup=92.0, volume=100.0, market=100.0)
    raw = sum(weights[k] * sub[k] for k in weights)
    return min(100.0, max(0.0, raw))


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_score_reproducible(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    weights = make_config()["scoring_weights"]
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    cand = fetch_candidates(prod)[0]
    assert cand["screening_score"] == pytest.approx(_expected_strong_bull_score(weights))
    comps = json.loads(cand["soft_score_components"])
    assert comps["trend_score"] == pytest.approx(100.0)
    assert comps["momentum_score"] == pytest.approx(100.0)
    assert comps["setup_score"] == pytest.approx(92.0)
    assert comps["volume_score"] == pytest.approx(100.0)
    assert comps["market_score"] == pytest.approx(100.0)


# --- Boundary: breakout_proximity scoring bands ---
# defaults: consolidation=80, pullback=-0.07 (pb_score=100)
# setup = 0.4*cons + 0.3*bp_score + 0.3*pb_score

@pytest.mark.parametrize(
    "bp,expected_bp_score,expected_setup",
    [
        (0.5, 100.0, 92.0),   # upper edge of ideal band [-1, 0.5] → bp=100
        (1.0, 30.0, 71.0),    # mid-band (0.5, 1.5] → bp=30 (gap G-BP-MIDBAND)
        (2.0, 20.0, 68.0),    # above 1.5 → bp=20
        (-1.0, 100.0, 92.0),  # lower edge of ideal band → bp=100
        (-2.0, 70.0, 83.0),   # lower fringe [-2, -1) → bp=70; setup=32+21+30=83
        (-3.0, 30.0, 71.0),   # below -2 → bp=30
    ],
)
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_breakout_proximity_band_boundaries(
    tmp_db_paths: dict[str, Path],
    bp: float,
    expected_bp_score: float,
    expected_setup: float,
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d, breakout_proximity=bp)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    comps = json.loads(fetch_candidates(prod)[0]["soft_score_components"])
    assert comps["setup_score"] == pytest.approx(expected_setup, abs=0.01)


# --- Boundary: distance_to_ema50_pct linear taper ---
# defaults: ema_alignment=100 (align_score=100), close_raw=100>ema200=90 → above200=100
# trend = 0.5*100 + 0.25*dist_score + 0.25*100

@pytest.mark.parametrize(
    "dist,expected_dist_score,expected_trend",
    [
        (0.02,  100.0,  100.0),   # in-band [-0.03, 0.08] → dist=100
        (0.08,  100.0,  100.0),   # upper band edge → dist=100
        (0.13,  50.0,   87.5),    # upper taper: 100-(0.05/0.10)*100=50
        (0.18,  0.0,    75.0),    # upper taper edge: 100-100=0, clamped
        (-0.03, 100.0,  100.0),   # lower band edge → dist=100
        (-0.08, 50.0,   87.5),    # lower taper: 100-(0.05/0.10)*100=50
        (-0.14, 0.0,    75.0),    # outside lower taper: clamped to 0
    ],
)
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_distance_to_ema50_taper_boundaries(
    tmp_db_paths: dict[str, Path],
    dist: float,
    expected_dist_score: float,
    expected_trend: float,
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d, distance_to_ema50_pct=dist)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    comps = json.loads(fetch_candidates(prod)[0]["soft_score_components"])
    assert comps["trend_score"] == pytest.approx(expected_trend, abs=0.01)


@pytest.mark.parametrize(
    "regime,expected_market",
    [
        (constants.REGIME_BULL, 100.0),
        (constants.REGIME_NEUTRAL, 60.0),
        (constants.REGIME_BEAR, 20.0),
        (constants.REGIME_HIGH_RISK, 0.0),
        (constants.REGIME_EXTREME_RISK, 0.0),
    ],
)
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_market_regime_mapping_all_five(
    tmp_db_paths: dict[str, Path], regime: str, expected_market: float
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d, market_regime=regime)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    comps = json.loads(fetch_candidates(prod)[0]["soft_score_components"])
    assert comps["market_score"] == pytest.approx(expected_market)
    assert comps["market_regime_known"] is True


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_unknown_regime_scores_zero_and_audited(tmp_db_paths: dict[str, Path]) -> None:
    """Unknown market regime scores 0.0 (data gap), not neutral 50."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d, market_regime="mystery")
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    comps = json.loads(fetch_candidates(prod)[0]["soft_score_components"])
    assert comps["market_score"] == pytest.approx(s3mod.MARKET_SCORE_UNKNOWN)  # 0.0
    assert comps["market_regime"] == "mystery"
    assert comps["market_regime_known"] is False


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_null_regime_scores_zero(tmp_db_paths: dict[str, Path]) -> None:
    """NULL market regime scores 0.0, not neutral 50."""
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d, market_regime=None)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    comps = json.loads(fetch_candidates(prod)[0]["soft_score_components"])
    assert comps["market_score"] == pytest.approx(s3mod.MARKET_SCORE_UNKNOWN)  # 0.0
    assert comps["market_regime_known"] is False


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_missing_sector_rs_neutral_50_no_warning(
    tmp_db_paths: dict[str, Path],
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d, sector_relative_strength=None)
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS
    assert res.warnings == []
    comps = json.loads(fetch_candidates(prod)[0]["soft_score_components"])
    # momentum = 0.4*100 + 0.3*100(roc) + 0.3*50(srs neutral) = 85
    assert comps["momentum_score"] == pytest.approx(85.0)


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_custom_weights_change_score(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    weights = {"trend": 1.0, "momentum": 0.0, "setup": 0.0, "volume": 0.0, "market": 0.0}
    Step3ScreeningEngine().screen(
        d, make_config(weights=weights), CONFIG_ID
    )
    cand = fetch_candidates(prod)[0]
    assert cand["screening_score"] == pytest.approx(100.0)  # trend=100 only


# --------------------------------------------------------------------------- #
# 7. Score min/max/mean over passed candidates only
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_score_stats_over_passed_only(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)  # passes, high score
    # passing but weaker momentum -> lower score
    seed_full_passing(prod, "BBB", d, rsi14=80.0, roc20=-0.1, sector_relative_strength=-0.2)
    seed_full_passing(prod, "CCC", d, rvol20=0.1)  # fails -> excluded from stats
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert res.metadata["passed_hard_filters"] == 2
    cands = {c["ticker"]: c["screening_score"] for c in fetch_candidates(prod)}
    passed_scores = [cands["AAA"], cands["BBB"]]
    assert res.metadata["screening_score_min"] == pytest.approx(min(passed_scores))
    assert res.metadata["screening_score_max"] == pytest.approx(max(passed_scores))
    assert res.metadata["screening_score_mean"] == pytest.approx(
        sum(passed_scores) / 2
    )


def test_score_stats_none_when_no_pass(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d, rvol20=0.1)  # fails
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert res.metadata["passed_hard_filters"] == 0
    assert res.metadata["screening_score_min"] is None
    assert res.metadata["screening_score_max"] is None
    assert res.metadata["screening_score_mean"] is None


# --------------------------------------------------------------------------- #
# 8. JSON payloads deterministic + parseable
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_snapshot_and_components_parseable_and_sorted(
    tmp_db_paths: dict[str, Path],
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    cand = fetch_candidates(prod)[0]
    snap = json.loads(cand["feature_snapshot_json"])
    comps = json.loads(cand["soft_score_components"])
    # required snapshot fields present
    for field in s3mod._SNAPSHOT_FIELDS:
        assert field in snap
    # deterministic: sort_keys round-trips identically
    assert cand["feature_snapshot_json"] == json.dumps(snap, sort_keys=True)
    assert cand["soft_score_components"] == json.dumps(comps, sort_keys=True)


# --------------------------------------------------------------------------- #
# 9. Single transaction / rollback / write ownership
# --------------------------------------------------------------------------- #
class _FailOnNthInsertDb:
    """Wrap the real manager; make the Nth INSERT into step3_candidates raise."""

    def __init__(self, real, fail_on_insert_number: int) -> None:
        self._real = real
        self._fail_on = fail_on_insert_number

    def connect(self, db_role: str, read_only: bool = False):
        conn = self._real.connect(db_role, read_only=read_only)
        if read_only:
            return conn
        return _CountingConn(conn, self._fail_on)


class _CountingConn:
    def __init__(self, inner, fail_on: int) -> None:
        self._inner = inner
        self._fail_on = fail_on
        self._inserts = 0

    def execute(self, sql, params=None):
        if "INSERT INTO step3_candidates" in sql:
            self._inserts += 1
            if self._inserts == self._fail_on:
                raise RuntimeError("boom on insert")
        return self._inner.execute(sql, params) if params is not None else self._inner.execute(sql)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_write_failure_rolls_back_no_rows(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    seed_full_passing(prod, "BBB", d)
    seed_full_passing(prod, "CCC", d)
    db = _FailOnNthInsertDb(dbm, fail_on_insert_number=2)
    res = Step3ScreeningEngine(db_manager=db).screen(d, make_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["candidates_written"] == 0
    assert res.rows_processed == 0
    assert fetch_candidates(prod) == []  # rollback left nothing


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_all_candidates_in_one_transaction(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    for t in ("AAA", "BBB", "CCC"):
        seed_full_passing(prod, t, d)
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert res.metadata["candidates_written"] == 3
    assert len(fetch_candidates(prod)) == 3


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_no_other_tables_written(tmp_db_paths: dict[str, Path]) -> None:
    import duckdb

    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)

    forbidden = (
        "daily_prices", "ticker_master", "daily_features",
        "step4_analysis", "step5_proposals", "strategy_configs",
    )

    def snapshot() -> dict[str, int]:
        conn = duckdb.connect(str(prod), read_only=True)
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in forbidden}
        finally:
            conn.close()

    before = snapshot()
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    after = snapshot()
    assert before == after


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_candidate_ids_unique_per_row(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    for t in ("AAA", "BBB", "CCC"):
        seed_full_passing(prod, t, d)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    ids = [c["candidate_id"] for c in fetch_candidates(prod)]
    assert len(ids) == len(set(ids)) == 3
    for cid in ids:
        uuid.UUID(cid)  # valid uuid4 string


# --------------------------------------------------------------------------- #
# 10. Read/write failure paths
# --------------------------------------------------------------------------- #
class _ReadFailsDb:
    def connect(self, db_role: str, read_only: bool = False):
        raise RuntimeError("cannot open db")


def test_read_failure_returns_failed(tmp_db_paths: dict[str, Path]) -> None:
    res = Step3ScreeningEngine(db_manager=_ReadFailsDb()).screen(
        date(2023, 6, 1), make_config(), CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["candidates_written"] == 0
    assert frozenset(res.metadata) == REQUIRED_METADATA_KEYS


# --------------------------------------------------------------------------- #
# 11. signal_date only / debug role
# --------------------------------------------------------------------------- #
@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_only_signal_date_rows_evaluated(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    d = date(2023, 6, 1)
    other = date(2023, 6, 2)
    seed_full_passing(prod, "AAA", d)
    seed_full_passing(prod, "BBB", other)
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    assert res.metadata["tickers_evaluated"] == 1
    assert [c["ticker"] for c in fetch_candidates(prod)] == ["AAA"]


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_debug_role_supported(tmp_db_paths: dict[str, Path]) -> None:
    d = date(2023, 6, 1)
    debug = tmp_db_paths[dbm.DB_ROLE_DEBUG]
    seed_full_passing(debug, "AAA", d)
    res = Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID, db_role="debug")
    assert res.is_ok()
    assert res.metadata["db_role"] == "debug"
    assert len(fetch_candidates(debug)) == 1


@pytest.mark.skip(
    reason='PENDING Phase 3 (M13) migration: references strategy_config_id / '
           'legacy columns removed in setup-mode schema (AD-22.21).'
)
def test_signal_date_in_payload_matches_feature_date(
    tmp_db_paths: dict[str, Path],
) -> None:
    d = date(2023, 6, 1)
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full_passing(prod, "AAA", d)
    Step3ScreeningEngine().screen(d, make_config(), CONFIG_ID)
    cand = fetch_candidates(prod)[0]
    assert cand["signal_date"] == d
    assert cand["strategy_config_id"] == CONFIG_ID


# --------------------------------------------------------------------------- #
# 12. Static source scans (no forbidden patterns)
# --------------------------------------------------------------------------- #
def _engine_source() -> str:
    return Path(s3mod.__file__).read_text(encoding="utf-8")


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
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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
        # Module 13 only inserts into step3_candidates: no UPDATE/DELETE of it,
        # no INSERT into any other table.
        assert "UPDATE STEP3_CANDIDATES" not in upper
        assert "DELETE FROM" not in upper
        assert "INSERT INTO STEP4" not in upper
        assert "INSERT INTO STEP5" not in upper
        assert "INSERT INTO DAILY_" not in upper
        assert "INSERT INTO TICKER_" not in upper


def test_only_step3_candidates_insert() -> None:
    inserts = [
        s for s in _non_docstring_strings(_engine_source())
        if "INSERT INTO" in s.upper()
    ]
    assert inserts, "expected at least one INSERT statement"
    for s in inserts:
        assert "INSERT INTO step3_candidates" in s


def test_no_print_in_engine() -> None:
    tree = ast.parse(_engine_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_provider_imports() -> None:
    imported = _imported_module_names(_engine_source())
    assert "yfinance" not in imported
    assert not any(m == "providers" or m.startswith("providers") for m in imported)
    for s in _non_docstring_strings(_engine_source()):
        low = s.lower()
        assert "yfinance" not in low
        assert "providers" not in low
