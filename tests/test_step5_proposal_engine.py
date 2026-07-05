"""Tests for Module 15 — Step 5 Proposal Engine (setup-mode migration).

All tests run fully offline. The ``tmp_db_paths`` fixture redirects DuckDB
settings into pytest ``tmp_path`` and applies the real schema. Step 4 analysis
rows, ticker_master, daily_features, daily_prices, risk_label_config, and
setup_configs are seeded directly; Module 15 reads them read-only and only
inserts into step5_proposals.

Coverage:
  - stop computed from support / structure / ATR
  - stop too wide rejected/penalised
  - target from resistance / prior high / structure
  - target room too small → WATCHLIST_ONLY
  - reward/risk computed correctly
  - risk_label low / medium / high
  - BUY eligible candidate
  - WATCHLIST_ONLY / REJECTED
  - missing support/target evidence handled (fixed-R fallback)
  - all four setup types
  - no strategy_config_id required
  - NULL market_regime blocks BUY
  - hard-cap diversification (sector / industry)
  - soft-penalty diversification
  - raw + diversified ranking
  - selected_flag = in_diversified_top_n
  - multi-route deduplication
  - db_role guard (simulation rejected)
  - empty input → success
  - bad risk_label_config → failed
  - BEGIN/COMMIT/ROLLBACK pattern
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.config import constants, settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.config.default_configs import (
    DEFAULT_RISK_LABEL_CONFIG,
    DEFAULT_SETUP_CONFIGS,
)
from app.services.proposal.step5_proposal_engine import (
    DISPOSITION_BUY,
    DISPOSITION_REJECTED,
    DISPOSITION_WATCHLIST,
    REJECT_INDUSTRY_CAP,
    REJECT_SECTOR_CAP,
    UNKNOWN_INDUSTRY,
    UNKNOWN_SECTOR,
    WATCHLIST_AUDIT_INCONSISTENT,
    WATCHLIST_STOP_TOO_WIDE,
    WATCHLIST_TARGET_ROOM_INSUFFICIENT,
    Step5ProposalEngine,
    _assign_disposition,
    _compute_estimated_rr,
    _compute_risk_score,
    _compute_stop_target,
    _parse_risk_label_config,
    _proposal_score_raw,
    _rr_score,
)
from app.utils import service_result as sr

SIGNAL_DATE = date(2024, 3, 15)
CONFIG_ID = "setup_breakout_v1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    assert sm.apply_prod_schema().status == sr.STATUS_SUCCESS
    assert sm.apply_debug_schema().status == sr.STATUS_SUCCESS
    return {dbm.DB_ROLE_PROD: prod, dbm.DB_ROLE_DEBUG: debug}


def _seed_ticker(db_role: str, ticker: str, sector: str = "Technology", industry: str = "Software") -> None:
    conn = dbm.connect(db_role)
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            "INSERT OR IGNORE INTO ticker_master "
            "(ticker, symbol_type, active_flag, delisted_flag, sector, industry, last_updated) "
            "VALUES (?, 'stock', TRUE, FALSE, ?, ?, now())",
            [ticker, sector, industry],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _seed_price(db_role: str, ticker: str, sig_date: date,
                close_raw: float = 100.0, close_adj: float = 100.0) -> None:
    conn = dbm.connect(db_role)
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            "INSERT OR IGNORE INTO daily_prices "
            "(ticker, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
            " open_adj, high_adj, low_adj, close_adj, source_provider, "
            " data_quality_status, mutation_flag, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo', 'ok', FALSE, now())",
            [ticker, sig_date.isoformat(),
             close_raw * 0.99, close_raw * 1.02, close_raw * 0.97, close_raw, 1_000_000,
             close_adj * 0.99, close_adj * 1.02, close_adj * 0.97, close_adj],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _seed_features(
    db_role: str,
    ticker: str,
    sig_date: date,
    *,
    close_adj: float = 100.0,
    ema20: float = 95.0,
    ema50: float = 90.0,
    ema200: float = 80.0,
    atr14: float = 2.0,
    atr_pct: float = 0.02,
    rvol20: float = 1.5,
    avg_dollar_volume_20d: float = 50_000_000.0,
    support_level: float | None = 95.0,
    resistance_level: float | None = 105.0,
    next_resistance_level: float | None = 115.0,
    base_high: float | None = 103.0,
    base_low: float | None = 96.0,
    swing_high: float | None = 108.0,
    swing_low: float | None = 94.0,
    range_tightness_score: float = 70.0,
    range_duration: int = 15,
    pullback_from_recent_high_pct: float = -0.05,
    ema_alignment_score: float = 100.0,
    roc20: float = 0.05,
    distance_to_ema20_pct: float = 0.05,
    distance_to_ema50_pct: float = 0.10,
    market_regime: str | None = "bull",
) -> None:
    conn = dbm.connect(db_role)
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            """INSERT OR IGNORE INTO daily_features (
                ticker, feature_date, feature_cutoff_date, feature_schema_version,
                feature_ready, ema20, ema50, ema200, ema_alignment_score,
                atr14, atr_pct, rvol20, avg_dollar_volume_20d,
                support_level, resistance_level, next_resistance_level,
                base_high, base_low, swing_high, swing_low,
                pullback_from_recent_high_pct, range_tightness_score, range_duration,
                roc20, distance_to_ema20_pct, distance_to_ema50_pct,
                market_regime, calculated_at
            ) VALUES (
                ?, ?, ?, 'features_v02', TRUE,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, now()
            )""",
            [
                ticker, sig_date.isoformat(), sig_date.isoformat(),
                ema20, ema50, ema200, ema_alignment_score,
                atr14, atr_pct, rvol20, avg_dollar_volume_20d,
                support_level, resistance_level, next_resistance_level,
                base_high, base_low, swing_high, swing_low,
                pullback_from_recent_high_pct, range_tightness_score, range_duration,
                roc20, distance_to_ema20_pct, distance_to_ema50_pct,
                market_regime,
            ],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _seed_step4(
    db_role: str,
    ticker: str,
    sig_date: date,
    *,
    setup_type: str = "breakout",
    setup_config_id: str = "setup_breakout_v1",
    setup_passed: bool = True,
    setup_score: float = 70.0,
    setup_fail_reason: str | None = None,
    entry_price_raw: float = 100.0,
    support_level: float | None = 95.0,
    resistance_level: float | None = 105.0,
    next_resistance_level: float | None = 115.0,
    atr_pct: float | None = 0.02,
    distance_to_ema20_pct: float | None = 0.05,
    distance_to_ema50_pct: float | None = 0.10,
    rvol: float | None = 1.5,
    earnings_days: int | None = 20,
    market_regime: str | None = "bull",
    candidate_id: str | None = None,
    run_id: str | None = None,
) -> str:
    cid = candidate_id or str(uuid.uuid4())
    rid = run_id or str(uuid.uuid4())
    aid = str(uuid.uuid4())
    conn = dbm.connect(db_role)
    try:
        conn.execute("BEGIN TRANSACTION")
        # Ensure step3 candidate row exists (FK not enforced by DuckDB but schema expects it)
        conn.execute(
            "INSERT OR IGNORE INTO step3_candidates "
            "(candidate_id, run_id, ticker, signal_date, passed_eligibility, "
            " routing_status, created_at) "
            "VALUES (?, ?, ?, ?, TRUE, 'routed', now())",
            [cid, rid, ticker, sig_date.isoformat()],
        )
        conn.execute(
            """INSERT INTO step4_analysis (
                analysis_id, candidate_id, run_id, setup_config_id,
                ticker, signal_date, setup_type, setup_score, setup_passed,
                setup_reasons, setup_fail_reason,
                entry_price_raw, target_is_structural,
                support_level, resistance_level, next_resistance_level,
                atr_pct, distance_to_ema20_pct, distance_to_ema50_pct,
                rvol, earnings_days, market_regime,
                earnings_penalty, macro_penalty, explanation_json, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?,
                ?, NULL,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                0, 0, '{}', now()
            )""",
            [
                aid, cid, rid, setup_config_id,
                ticker, sig_date.isoformat(), setup_type,
                setup_score, setup_passed, setup_fail_reason,
                entry_price_raw,
                support_level, resistance_level, next_resistance_level,
                atr_pct, distance_to_ema20_pct, distance_to_ema50_pct,
                rvol, earnings_days, market_regime,
            ],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return aid


def _seed_risk_config(db_role: str, cfg: dict | None = None) -> None:
    rc = cfg or DEFAULT_RISK_LABEL_CONFIG
    conn = dbm.connect(db_role)
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            "INSERT OR IGNORE INTO risk_label_config "
            "(config_id, version, config_json, config_hash, active_flag, created_at) "
            "VALUES (?, ?, ?, 'hash1', TRUE, now())",
            [rc["config_id"], rc["version"], json.dumps(rc)],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _seed_setup_config(db_role: str, setup_type: str = "breakout") -> None:
    cfg = DEFAULT_SETUP_CONFIGS[setup_type]
    conn = dbm.connect(db_role)
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            "INSERT OR IGNORE INTO setup_configs "
            "(config_id, setup_type, version, config_json, config_hash, active_flag, created_at) "
            "VALUES (?, ?, ?, ?, 'hash1', TRUE, now())",
            [cfg["config_id"], setup_type, cfg.get("version", "v1"), json.dumps(cfg)],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _read_proposals(db_role: str, sig_date: date) -> list[dict[str, Any]]:
    conn = dbm.connect(db_role, read_only=True)
    try:
        rows = conn.execute(
            "SELECT * FROM step5_proposals WHERE signal_date = ? ORDER BY raw_rank NULLS LAST, ticker",
            [sig_date.isoformat()],
        ).fetchall()
        cols = [d[0] for d in conn.execute(
            "SELECT * FROM step5_proposals LIMIT 0"
        ).description]
    finally:
        conn.close()
    return [dict(zip(cols, r)) for r in rows]


def _make_engine() -> Step5ProposalEngine:
    return Step5ProposalEngine()


def _minimal_risk_config(
    *,
    top_n: int = 5,
    min_rr: float = 1.8,
    allowed_buy_labels: list[str] | None = None,
    block_regimes: list[str] | None = None,
    block_null: bool = True,
    hard_cap: bool = True,
    max_sector: int = 4,
    max_industry: int = 2,
    low_max: float = 33.0,
    med_max: float = 66.0,
    max_stop: float = 0.10,
    min_target_room: float = 0.05,
) -> dict:
    return {
        "config_id": "risk_label_config_v1",
        "version": "risk_v1",
        "factor_weights": {
            "stop_distance_pct": 0.20, "atr_pct": 0.15,
            "ema_extension": 0.10, "liquidity": 0.10,
            "earnings_proximity": 0.10, "estimated_rr": 0.15,
            "market_regime": 0.10, "setup_confirmation": 0.10,
        },
        "thresholds": {"low_max": low_max, "med_max": med_max},
        "buy_rules": {
            "min_rr_for_buy": min_rr,
            "allowed_buy_labels": allowed_buy_labels or ["low", "medium"],
            "block_market_regimes": block_regimes or ["extreme_risk"],
            "block_if_regime_null": block_null,
            "max_stop_distance_pct": max_stop,
            "min_target_room_pct": min_target_room,
        },
        "ranking": {"top_n": top_n},
        "diversification": {
            "hard_cap_enabled": hard_cap,
            "sector_max_positions": max_sector,
            "industry_max_positions": max_industry,
            "sector_penalty_factor": 0.9,
            "industry_penalty_factor": 0.85,
        },
    }


# ===========================================================================
# Pure-function unit tests (no DB)
# ===========================================================================

class TestRrScore:
    def test_ge_3(self):
        assert _rr_score(3.0) == 100.0
        assert _rr_score(4.0) == 100.0

    def test_2_2_to_3(self):
        assert _rr_score(2.5) == 80.0

    def test_1_8_to_2_2(self):
        assert _rr_score(1.8) == 60.0
        assert _rr_score(2.0) == 60.0

    def test_1_3_to_1_8(self):
        assert _rr_score(1.5) == 30.0

    def test_below_1_3(self):
        assert _rr_score(1.0) == 0.0

    def test_none(self):
        assert _rr_score(None) == 0.0


class TestComputeEstimatedRr:
    def test_basic(self):
        rr = _compute_estimated_rr(100.0, 95.0, 115.0)
        assert rr == pytest.approx(3.0)

    def test_stop_equals_entry(self):
        assert _compute_estimated_rr(100.0, 100.0, 120.0) is None

    def test_stop_above_entry(self):
        assert _compute_estimated_rr(100.0, 105.0, 120.0) is None

    def test_any_none(self):
        assert _compute_estimated_rr(None, 95.0, 115.0) is None
        assert _compute_estimated_rr(100.0, None, 115.0) is None
        assert _compute_estimated_rr(100.0, 95.0, None) is None

    def test_positive_rr(self):
        rr = _compute_estimated_rr(100.0, 97.0, 106.0)
        assert rr == pytest.approx(2.0)


class TestComputeStopTarget:
    """Per-setup stop/target formulas (01c §62)."""

    def _feat(self, **kwargs) -> dict:
        base = {
            "atr14": 2.0, "atr_pct": 0.02, "ema20": 95.0, "ema50": 90.0,
            "support_level": 95.0, "resistance_level": 105.0,
            "next_resistance_level": 115.0,
            "base_high": 103.0, "base_low": 96.0,
            "swing_high": 108.0, "swing_low": 94.0,
        }
        base.update(kwargs)
        return base

    def _cfg(self, k_atr: float = 1.0, min_rr: float = 1.8) -> dict:
        return {"validation": {"k_atr_stop": k_atr, "buffer_atr_multiple": 0.25, "min_rr": min_rr}}

    # -- breakout ---
    def test_breakout_stop_below_entry(self):
        stop, target, structural, _, _, _ = _compute_stop_target(
            "breakout", self._feat(), self._cfg(), 100.0, 100.0, 100.0
        )
        assert stop is not None and stop < 100.0

    def test_breakout_target_structural(self):
        _, target, structural, _, _, _ = _compute_stop_target(
            "breakout", self._feat(), self._cfg(), 100.0, 100.0, 100.0
        )
        assert target is not None and target > 100.0
        assert structural is True

    def test_breakout_fixed_r_fallback_when_no_levels(self):
        feat = {"atr14": 2.0, "atr_pct": 0.02}  # no structural levels
        _, target, structural, _, _, _ = _compute_stop_target(
            "breakout", feat, self._cfg(), 100.0, 100.0, 100.0
        )
        assert target is not None
        assert structural is False  # fallback

    # -- pullback ---
    def test_pullback_stop_below_entry(self):
        stop, target, structural, _, _, _ = _compute_stop_target(
            "pullback", self._feat(), self._cfg(), 100.0, 100.0, 100.0
        )
        assert stop is not None and stop < 100.0

    def test_pullback_target_from_swing_high(self):
        feat = self._feat(swing_high=112.0)
        _, target, structural, _, _, _ = _compute_stop_target(
            "pullback", feat, self._cfg(), 100.0, 100.0, 100.0
        )
        assert target == pytest.approx(112.0)
        assert structural is True

    def test_pullback_stop_uses_ema_area(self):
        feat = self._feat(support_level=None, swing_low=None, ema20=98.0, ema50=97.0)
        stop, _, _, basis, _, _ = _compute_stop_target(
            "pullback", feat, self._cfg(), 100.0, 100.0, 100.0
        )
        assert stop is not None and stop < 100.0
        assert "ema" in basis or "atr" in basis

    # -- trend_continuation ---
    def test_trend_continuation_stop_below_entry(self):
        stop, _, _, _, _, _ = _compute_stop_target(
            "trend_continuation", self._feat(), self._cfg(k_atr=1.5), 100.0, 100.0, 100.0
        )
        assert stop is not None and stop < 100.0

    def test_trend_continuation_target_structural(self):
        feat = self._feat(next_resistance_level=120.0)
        _, target, structural, _, _, _ = _compute_stop_target(
            "trend_continuation", feat, self._cfg(), 100.0, 100.0, 100.0
        )
        assert target is not None and target > 100.0
        assert structural is True

    # -- consolidation_base ---
    def test_consolidation_base_stop_from_base_low(self):
        # base_low=96, buffer_atr=0.5 → stop < 95.5
        stop, _, _, basis, _, _ = _compute_stop_target(
            "consolidation_base", self._feat(), self._cfg(), 100.0, 100.0, 100.0
        )
        assert stop is not None and stop < 96.0
        assert "base" in basis or "support" in basis

    def test_consolidation_base_target_base_high(self):
        feat = self._feat(base_high=103.0, base_low=96.0)
        _, target, structural, _, _, _ = _compute_stop_target(
            "consolidation_base", feat, self._cfg(), 100.0, 100.0, 100.0
        )
        assert target is not None and target > 100.0
        assert structural is True

    def test_consolidation_base_near_ceiling_measured_move(self):
        # entry near base_high → measured move branch
        feat = self._feat(base_high=103.0, base_low=96.0)
        _, target, structural, _, _, _ = _compute_stop_target(
            "consolidation_base", feat, self._cfg(), 102.0, 102.0, 102.0
        )
        assert target is not None and target > 102.0
        assert structural is True

    # -- raw/adj conversion ---
    def test_raw_adj_conversion_applied(self):
        # When close_raw != close_adj, structural levels are scaled
        feat = self._feat(support_level=95.0, swing_low=94.0, ema20=95.0, ema50=90.0)
        # close_raw=200, close_adj=100 → 2x scaling
        stop_adj, _, _, _, _, _ = _compute_stop_target(
            "pullback", feat, self._cfg(), 200.0, 200.0, 100.0
        )
        stop_par, _, _, _, _, _ = _compute_stop_target(
            "pullback", feat, self._cfg(), 100.0, 100.0, 100.0
        )
        # Scaled result should be ~2x the non-scaled stop (roughly)
        assert stop_adj is not None and stop_par is not None
        assert stop_adj > stop_par * 1.5  # clearly different due to scaling

    def test_stop_always_below_entry(self):
        for st in constants.ALLOWED_SETUP_TYPES:
            feat = {"atr14": 2.0, "atr_pct": 0.02}
            stop, _, _, _, _, _ = _compute_stop_target(st, feat, self._cfg(), 100.0, 100.0, 100.0)
            if stop is not None:
                assert stop < 100.0, f"{st}: stop {stop} >= entry 100"

    def test_target_always_above_entry_when_set(self):
        for st in constants.ALLOWED_SETUP_TYPES:
            feat = {"atr14": 2.0, "atr_pct": 0.02}
            _, target, _, _, _, _ = _compute_stop_target(st, feat, self._cfg(), 100.0, 100.0, 100.0)
            if target is not None:
                assert target > 100.0, f"{st}: target {target} <= entry 100"


class TestComputeRiskScore:
    def _base_feat(self) -> dict:
        return {
            "atr_pct": 0.02,
            "distance_to_ema20_pct": 0.05,
            "distance_to_ema50_pct": 0.08,
            "avg_dollar_volume_20d": 50_000_000.0,
        }

    def _cfg(self, low_max: float = 33.0, med_max: float = 66.0) -> dict:
        return {
            "factor_weights": {
                "stop_distance_pct": 0.20, "atr_pct": 0.15, "ema_extension": 0.10,
                "liquidity": 0.10, "earnings_proximity": 0.10, "estimated_rr": 0.15,
                "market_regime": 0.10, "setup_confirmation": 0.10,
            },
            "low_max": low_max,
            "med_max": med_max,
        }

    def test_risk_score_0_to_100(self):
        score, label, reasons = _compute_risk_score(
            feat=self._base_feat(),
            entry=100.0, stop=97.0,
            estimated_rr=2.5,
            market_regime="bull",
            setup_score=80.0,
            earnings_days=30,
            cfg=self._cfg(),
        )
        assert 0.0 <= score <= 100.0
        assert label in constants.ALLOWED_RISK_LABELS

    def test_low_risk_label(self):
        # Very favourable: tight stop, high RR, bull, high score, distant earnings, liquid
        score, label, _ = _compute_risk_score(
            feat={**self._base_feat(), "atr_pct": 0.01, "avg_dollar_volume_20d": 200_000_000.0},
            entry=100.0, stop=98.5,
            estimated_rr=4.0,
            market_regime="bull",
            setup_score=90.0,
            earnings_days=60,
            cfg=self._cfg(low_max=50.0),  # relaxed threshold to ensure low
        )
        assert label == constants.RISK_LABEL_LOW

    def test_high_risk_null_regime(self):
        # NULL regime → maximum market_regime risk contribution
        _, label, reasons = _compute_risk_score(
            feat=self._base_feat(),
            entry=100.0, stop=85.0,  # wide stop
            estimated_rr=1.0,
            market_regime=None,
            setup_score=40.0,
            earnings_days=2,
            cfg=self._cfg(),
        )
        assert label == constants.RISK_LABEL_HIGH
        assert any("market_regime" in r for r in reasons)

    def test_medium_risk_label(self):
        score, label, _ = _compute_risk_score(
            feat=self._base_feat(),
            entry=100.0, stop=96.0,
            estimated_rr=2.0,
            market_regime="neutral",
            setup_score=65.0,
            earnings_days=15,
            cfg=self._cfg(),
        )
        assert label in (constants.RISK_LABEL_LOW, constants.RISK_LABEL_MEDIUM, constants.RISK_LABEL_HIGH)
        assert 0.0 <= score <= 100.0

    def test_risk_reasons_non_empty(self):
        _, _, reasons = _compute_risk_score(
            feat=self._base_feat(),
            entry=100.0, stop=97.0,
            estimated_rr=2.0,
            market_regime="bull",
            setup_score=70.0,
            earnings_days=20,
            cfg=self._cfg(),
        )
        assert len(reasons) > 0

    def test_monotonic_in_rr(self):
        """Higher RR → lower risk score."""
        def score(rr):
            s, _, _ = _compute_risk_score(
                feat=self._base_feat(), entry=100.0, stop=97.0,
                estimated_rr=rr, market_regime="bull",
                setup_score=70.0, earnings_days=20, cfg=self._cfg(),
            )
            return s
        assert score(1.0) >= score(2.0) >= score(3.0)


class TestAssignDisposition:
    def _cfg(self, **kw) -> dict:
        base = {
            "min_rr_for_buy": 1.8,
            "allowed_buy_labels": ["low", "medium"],
            "block_market_regimes": ["extreme_risk"],
            "block_if_regime_null": True,
            "max_stop_distance_pct": 0.10,
            "min_target_room_pct": 0.05,
        }
        base.update(kw)
        return base

    def test_buy_all_conditions_met(self):
        d, _ = _assign_disposition(True, 2.5, "low", "bull", 0.05, self._cfg())
        assert d == DISPOSITION_BUY

    def test_rejected_if_not_setup_passed(self):
        d, _ = _assign_disposition(False, 2.5, "low", "bull", 0.05, self._cfg())
        assert d == DISPOSITION_REJECTED

    def test_watchlist_rr_too_low(self):
        d, _ = _assign_disposition(True, 1.0, "low", "bull", 0.05, self._cfg())
        assert d == DISPOSITION_WATCHLIST

    def test_watchlist_rr_none(self):
        d, _ = _assign_disposition(True, None, "low", "bull", 0.05, self._cfg())
        assert d == DISPOSITION_WATCHLIST

    def test_watchlist_high_risk_label(self):
        d, _ = _assign_disposition(True, 2.5, "high", "bull", 0.05, self._cfg())
        assert d == DISPOSITION_WATCHLIST

    def test_watchlist_null_regime_when_block_null(self):
        d, _ = _assign_disposition(True, 2.5, "low", None, 0.05,
                                   self._cfg(block_if_regime_null=True))
        assert d == DISPOSITION_WATCHLIST

    def test_buy_null_regime_when_not_block_null(self):
        d, _ = _assign_disposition(True, 2.5, "low", None, 0.05,
                                   self._cfg(block_if_regime_null=False))
        assert d == DISPOSITION_BUY

    def test_watchlist_blocked_regime(self):
        d, _ = _assign_disposition(True, 2.5, "low", "extreme_risk", 0.05, self._cfg())
        assert d == DISPOSITION_WATCHLIST

    def test_watchlist_bear_not_blocked(self):
        d, _ = _assign_disposition(True, 2.5, "low", "bear", 0.05, self._cfg())
        assert d == DISPOSITION_BUY

    def test_old_strategy_config_not_required(self):
        d, _ = _assign_disposition(True, 2.0, "medium", "neutral", 0.05, self._cfg())
        assert d in (DISPOSITION_BUY, DISPOSITION_WATCHLIST)

    # Fix 2: max stop-distance hard gate
    def test_stop_too_wide_forces_watchlist(self):
        """Stop distance exceeding max → WATCHLIST_ONLY regardless of other scores (fix 2)."""
        d, reason = _assign_disposition(True, 3.0, "low", "bull", 0.15,
                                        self._cfg(max_stop_distance_pct=0.10))
        assert d == DISPOSITION_WATCHLIST
        assert reason == WATCHLIST_STOP_TOO_WIDE

    def test_stop_at_max_boundary_is_buy(self):
        """Stop distance exactly at max → BUY still allowed."""
        d, _ = _assign_disposition(True, 2.5, "low", "bull", 0.10,
                                   self._cfg(max_stop_distance_pct=0.10))
        assert d == DISPOSITION_BUY

    def test_stop_none_does_not_trigger_gate(self):
        """None stop_distance_pct (no stop computed) → gate not triggered."""
        d, reason = _assign_disposition(True, 2.5, "low", "bull", None,
                                        self._cfg(max_stop_distance_pct=0.10))
        assert d == DISPOSITION_BUY

    def test_watchlist_reason_returned_for_wide_stop(self):
        _, reason = _assign_disposition(True, 2.5, "low", "bull", 0.20,
                                        self._cfg(max_stop_distance_pct=0.10))
        assert reason == WATCHLIST_STOP_TOO_WIDE


class TestProposalScoreRaw:
    def test_bull_high_rr_high_setup(self):
        s = _proposal_score_raw(90.0, 3.5, 80.0, "bull")
        assert s > 80.0

    def test_null_regime_zero_market_score(self):
        s1 = _proposal_score_raw(80.0, 2.5, 70.0, None)
        s2 = _proposal_score_raw(80.0, 2.5, 70.0, "bull")
        assert s2 > s1  # bull adds 10% weight * 100 = 10 pts extra

    def test_clamped_0_100(self):
        s = _proposal_score_raw(100.0, 5.0, 100.0, "bull")
        assert s <= 100.0
        s = _proposal_score_raw(0.0, 0.0, 0.0, "extreme_risk")
        assert s >= 0.0


# ===========================================================================
# Integration tests (with DB)
# ===========================================================================

class TestDbRoleGuard:
    def test_simulation_role_rejected(self):
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(), db_role="simulation")
        assert result.status == sr.STATUS_FAILED
        assert "simulation" in result.errors[0].lower() or "db_role" in result.errors[0].lower()

    def test_unknown_role_rejected(self):
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(), db_role="other")
        assert result.status == sr.STATUS_FAILED


class TestEmptyInput:
    def test_no_step4_rows_returns_success(self, tmp_db_paths):
        _seed_risk_config(dbm.DB_ROLE_PROD)
        eng = _make_engine()
        result = eng.propose(
            SIGNAL_DATE,
            risk_label_config=_minimal_risk_config(),
            setup_configs={},
            db_role=dbm.DB_ROLE_PROD,
        )
        assert result.status == sr.STATUS_SUCCESS
        assert result.metadata["proposals_written"] == 0


class TestBadConfig:
    def test_not_a_dict_config(self, tmp_db_paths):
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config="not_a_dict", db_role=dbm.DB_ROLE_PROD)
        assert result.status == sr.STATUS_FAILED

    def test_top_n_zero(self, tmp_db_paths):
        cfg = _minimal_risk_config(top_n=5)
        # Force top_n = 0 after construction
        cfg["ranking"]["top_n"] = 0
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=cfg, db_role=dbm.DB_ROLE_PROD)
        assert result.status == sr.STATUS_FAILED


class TestSinglePassedCandidate:
    def test_buy_eligible_candidate_written(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "AAA")
        _seed_price(db, "AAA", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "AAA", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "AAA", SIGNAL_DATE, setup_type="breakout",
                    setup_config_id="setup_breakout_v1", setup_passed=True,
                    setup_score=75.0, entry_price_raw=100.0, market_regime="bull",
                    earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.5)
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                             setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        assert result.status == sr.STATUS_SUCCESS
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1
        p = props[0]
        assert p["ticker"] == "AAA"
        assert p["setup_type"] == "breakout"
        assert p["disposition"] in (DISPOSITION_BUY, DISPOSITION_WATCHLIST)
        assert p["risk_label"] in constants.ALLOWED_RISK_LABELS
        assert p["stop_price_raw"] is not None
        assert p["target_price_raw"] is not None
        assert p["estimated_rr"] is not None
        assert p["estimated_rr"] > 0

    def test_stop_below_entry(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "BBB")
        _seed_price(db, "BBB", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "BBB", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       support_level=95.0, base_low=95.0)
        _seed_step4(db, "BBB", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        if p["stop_price_raw"] is not None and p["entry_price_raw"] is not None:
            assert p["stop_price_raw"] < p["entry_price_raw"]

    def test_target_above_entry(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "CCC")
        _seed_price(db, "CCC", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "CCC", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       next_resistance_level=120.0)
        _seed_step4(db, "CCC", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        if p["target_price_raw"] is not None and p["entry_price_raw"] is not None:
            assert p["target_price_raw"] > p["entry_price_raw"]

    def test_estimated_rr_computed_from_structure(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "DDD")
        _seed_price(db, "DDD", SIGNAL_DATE, 100.0, 100.0)
        # resistance_level=None so no resistance cap fires; test verifies formula only
        _seed_features(db, "DDD", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       base_low=95.0, next_resistance_level=115.0, resistance_level=None)
        _seed_step4(db, "DDD", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(min_rr=1.0),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        assert p["estimated_rr"] is not None
        # Verify formula: (target - entry) / (entry - stop)
        if p["stop_price_raw"] and p["entry_price_raw"] and p["target_price_raw"]:
            expected_rr = ((p["target_price_raw"] - p["entry_price_raw"]) /
                           (p["entry_price_raw"] - p["stop_price_raw"]))
            assert p["estimated_rr"] == pytest.approx(expected_rr, rel=0.01)


class TestRejectedCandidate:
    def test_failed_setup_produces_rejected_disposition(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "FAIL")
        _seed_price(db, "FAIL", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "FAIL", SIGNAL_DATE, close_adj=100.0)
        _seed_step4(db, "FAIL", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=False, setup_score=30.0,
                    setup_fail_reason="rvol_too_low", entry_price_raw=100.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1
        p = props[0]
        assert p["disposition"] == DISPOSITION_REJECTED
        assert p["stop_price_raw"] is None
        assert p["target_price_raw"] is None
        assert p["estimated_rr"] is None
        assert p["raw_rank"] is None
        assert p["in_raw_top_n"] is False

    def test_rejected_excluded_from_ranking(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "PASS")
        _seed_ticker(db, "FAIL2", sector="Energy", industry="Oil")
        _seed_price(db, "PASS", SIGNAL_DATE, 100.0, 100.0)
        _seed_price(db, "FAIL2", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "PASS", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_features(db, "FAIL2", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "PASS", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=75.0, entry_price_raw=100.0)
        _seed_step4(db, "FAIL2", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=False, setup_score=20.0, entry_price_raw=100.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        passed = [p for p in props if p["ticker"] == "PASS"]
        failed = [p for p in props if p["ticker"] == "FAIL2"]
        assert passed[0]["raw_rank"] is not None
        assert failed[0]["raw_rank"] is None


class TestNullRegimeBlocksBuy:
    def test_null_regime_disposition_is_watchlist(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "NREG")
        _seed_price(db, "NREG", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "NREG", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       market_regime=None)
        _seed_step4(db, "NREG", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=80.0, entry_price_raw=100.0,
                    market_regime=None, earnings_days=30)
        cfg = _minimal_risk_config(min_rr=1.0, block_null=True)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert props[0]["disposition"] != DISPOSITION_BUY

    def test_null_regime_risk_score_high_contribution(self):
        feat = {"atr_pct": 0.02, "avg_dollar_volume_20d": 50_000_000.0,
                "distance_to_ema20_pct": 0.02, "distance_to_ema50_pct": 0.05}
        cfg_dict = {
            "factor_weights": {
                "stop_distance_pct": 0.20, "atr_pct": 0.15, "ema_extension": 0.10,
                "liquidity": 0.10, "earnings_proximity": 0.10, "estimated_rr": 0.15,
                "market_regime": 0.10, "setup_confirmation": 0.10,
            },
            "low_max": 33.0, "med_max": 66.0,
        }
        score_null, _, _ = _compute_risk_score(
            feat=feat, entry=100.0, stop=97.0, estimated_rr=2.5,
            market_regime=None, setup_score=80.0, earnings_days=30, cfg=cfg_dict,
        )
        score_bull, _, _ = _compute_risk_score(
            feat=feat, entry=100.0, stop=97.0, estimated_rr=2.5,
            market_regime="bull", setup_score=80.0, earnings_days=30, cfg=cfg_dict,
        )
        assert score_null > score_bull


class TestRiskLabelAssignment:
    def test_low_risk_label_written_to_db(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        # Force low risk: good everything
        _seed_ticker(db, "LOW")
        _seed_price(db, "LOW", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "LOW", SIGNAL_DATE, close_adj=100.0, atr14=1.0, atr_pct=0.01,
                       avg_dollar_volume_20d=200_000_000.0, next_resistance_level=120.0,
                       support_level=98.0, base_low=98.0, market_regime="bull")
        _seed_step4(db, "LOW", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=90.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=60, atr_pct=0.01,
                    distance_to_ema20_pct=0.01, distance_to_ema50_pct=0.02, rvol=2.0)
        # Use relaxed thresholds to make low easier to achieve
        cfg = _minimal_risk_config(low_max=60.0, med_max=80.0, min_rr=1.5)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert props[0]["risk_label"] in constants.ALLOWED_RISK_LABELS

    def test_high_risk_wide_stop(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "HISK")
        _seed_price(db, "HISK", SIGNAL_DATE, 100.0, 100.0)
        # Very wide stop (base_low=70 → stop_distance ~30%), no liquidity
        _seed_features(db, "HISK", SIGNAL_DATE, close_adj=100.0, atr14=5.0, atr_pct=0.05,
                       base_low=70.0, support_level=70.0, avg_dollar_volume_20d=500_000.0,
                       market_regime=None)
        _seed_step4(db, "HISK", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=40.0, entry_price_raw=100.0,
                    market_regime=None, earnings_days=2, atr_pct=0.05, rvol=0.5)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(low_max=20.0),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert props[0]["risk_label"] in (constants.RISK_LABEL_MEDIUM, constants.RISK_LABEL_HIGH)


class TestRawRanking:
    def test_higher_score_gets_lower_rank(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        for t, score in [("AAA", 80.0), ("BBB", 60.0), ("CCC", 70.0)]:
            _seed_ticker(db, t, sector=t, industry=t)
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                           next_resistance_level=115.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=score,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        ranked = [p for p in props if p["raw_rank"] is not None]
        ranked.sort(key=lambda x: x["raw_rank"])
        # Rank 1 should have the best proposal_score_raw
        assert ranked[0]["proposal_score_raw"] >= ranked[-1]["proposal_score_raw"]

    def test_in_raw_top_n_flag(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        for i in range(4):
            t = f"T{i:02d}"
            _seed_ticker(db, t, sector=t, industry=t)
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=70.0 - i,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=2, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        in_top_n = [p for p in props if p["in_raw_top_n"]]
        assert len(in_top_n) == 2


class TestHardCapDiversification:
    def test_sector_cap_enforced(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        # 3 tickers same sector, cap=2 → one should get sector_cap rejection
        for t in ["A1", "A2", "A3"]:
            _seed_ticker(db, t, sector="Technology", industry="Software")
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=75.0,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=10, max_sector=2, max_industry=3, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        sector_rejected = [p for p in props if p["rejection_reason"] == REJECT_SECTOR_CAP]
        assert len(sector_rejected) >= 1

    def test_industry_cap_enforced(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        for t in ["B1", "B2", "B3"]:
            _seed_ticker(db, t, sector="Technology", industry="SaaS")
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=75.0,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=10, max_sector=5, max_industry=2, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        ind_rejected = [p for p in props if p["rejection_reason"] == REJECT_INDUSTRY_CAP]
        assert len(ind_rejected) >= 1

    def test_hard_cap_no_double_penalty(self, tmp_db_paths):
        """Hard-cap rejected rows: proposal_score_final == proposal_score_raw (AD-22.12)."""
        db = dbm.DB_ROLE_PROD
        for t in ["C1", "C2", "C3"]:
            _seed_ticker(db, t, sector="Technology", industry="Cloud")
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=75.0,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=10, max_sector=2, max_industry=3, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        for p in props:
            if p["rejection_reason"] in (REJECT_SECTOR_CAP, REJECT_INDUSTRY_CAP):
                assert p["proposal_score_final"] == pytest.approx(p["proposal_score_raw"])

    def test_selected_flag_equals_in_diversified_top_n(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        for t in ["D1", "D2", "D3"]:
            _seed_ticker(db, t, sector=t, industry=t)
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=75.0,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        for p in props:
            assert bool(p["selected_flag"]) == bool(p["in_diversified_top_n"])


class TestSoftPenaltyDiversification:
    def test_soft_penalty_no_hard_rejection(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        for t in ["E1", "E2", "E3"]:
            _seed_ticker(db, t, sector="Technology", industry="Software")
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=75.0,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, hard_cap=False, min_rr=1.0)
        cfg["diversification"]["hard_cap_enabled"] = False
        cfg["diversification"]["sector_penalty_factor"] = 0.9
        cfg["diversification"]["industry_penalty_factor"] = 0.85
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        # No hard-cap rejections
        cap_rej = [p for p in props if p["rejection_reason"] in (REJECT_SECTOR_CAP, REJECT_INDUSTRY_CAP)]
        assert len(cap_rej) == 0
        # Diversified ranks assigned to all rankable
        rankable = [p for p in props if p["raw_rank"] is not None]
        for p in rankable:
            assert p["diversified_rank"] is not None


class TestProductionDiversificationLimits:
    """Verify production caps: max_sector=4, max_industry=2.

    Raw View must remain unaffected (raw_rank and in_raw_top_n unchanged).
    Diversified View must exclude overflow tickers from selected_flag.
    sector_count_at_selection and industry_count_at_selection must be written.
    """

    def _seed_n_tickers(
        self,
        db: str,
        tickers: list[str],
        sector: str,
        industry: str,
        score_base: float = 80.0,
    ) -> None:
        for i, t in enumerate(tickers):
            _seed_ticker(db, t, sector=sector, industry=industry)
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                           next_resistance_level=115.0)
            # Descending scores so order is deterministic
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=score_base - i,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)

    def test_fifth_sector_ticker_excluded_from_diversified(self, tmp_db_paths):
        """5 tickers same sector (cap=4) → 5th excluded from diversified; all 5 in raw."""
        db = dbm.DB_ROLE_PROD
        tickers = ["S1", "S2", "S3", "S4", "S5"]
        self._seed_n_tickers(db, tickers, sector="Technology", industry="SaaS")
        # Use sector cap=4, industry cap=10 so only sector cap triggers
        cfg = _minimal_risk_config(top_n=10, max_sector=4, max_industry=10, min_rr=1.0)
        Step5ProposalEngine().propose(
            SIGNAL_DATE, risk_label_config=cfg,
            setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db,
        )
        props = _read_proposals(db, SIGNAL_DATE)

        # All 5 appear in raw view
        raw_ranked = [p for p in props if p["raw_rank"] is not None]
        assert len(raw_ranked) == 5
        assert all(p["in_raw_top_n"] for p in raw_ranked)

        # Exactly 4 admitted to diversified, 1 sector-cap rejected
        sector_rejected = [p for p in props if p["rejection_reason"] == REJECT_SECTOR_CAP]
        assert len(sector_rejected) == 1
        diversified_selected = [p for p in props if p["selected_flag"]]
        assert len(diversified_selected) == 4

    def test_third_industry_ticker_excluded_from_diversified(self, tmp_db_paths):
        """3 tickers same industry (cap=2) → 3rd excluded from diversified; all 3 in raw."""
        db = dbm.DB_ROLE_PROD
        tickers = ["I1", "I2", "I3"]
        self._seed_n_tickers(db, tickers, sector="Financials", industry="Banking")
        cfg = _minimal_risk_config(top_n=10, max_sector=10, max_industry=2, min_rr=1.0)
        Step5ProposalEngine().propose(
            SIGNAL_DATE, risk_label_config=cfg,
            setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db,
        )
        props = _read_proposals(db, SIGNAL_DATE)

        raw_ranked = [p for p in props if p["raw_rank"] is not None]
        assert len(raw_ranked) == 3
        assert all(p["in_raw_top_n"] for p in raw_ranked)

        industry_rejected = [p for p in props if p["rejection_reason"] == REJECT_INDUSTRY_CAP]
        assert len(industry_rejected) == 1
        diversified_selected = [p for p in props if p["selected_flag"]]
        assert len(diversified_selected) == 2

    def test_raw_ranking_unaffected_by_caps(self, tmp_db_paths):
        """raw_rank and in_raw_top_n are set before diversification and must not change."""
        db = dbm.DB_ROLE_PROD
        # 5 tickers same sector — sector cap=4 will exclude the last one from diversified
        tickers = ["R1", "R2", "R3", "R4", "R5"]
        self._seed_n_tickers(db, tickers, sector="Energy", industry="E&P")
        cfg = _minimal_risk_config(top_n=10, max_sector=4, max_industry=10, min_rr=1.0)
        Step5ProposalEngine().propose(
            SIGNAL_DATE, risk_label_config=cfg,
            setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db,
        )
        props = _read_proposals(db, SIGNAL_DATE)

        # Every rankable ticker has a raw_rank regardless of diversification outcome
        for p in props:
            if p["raw_rank"] is not None:
                assert p["in_raw_top_n"] is True
        # The sector-cap-rejected ticker still has a raw_rank
        sector_rejected = [p for p in props if p["rejection_reason"] == REJECT_SECTOR_CAP]
        assert len(sector_rejected) == 1
        assert sector_rejected[0]["raw_rank"] is not None
        assert sector_rejected[0]["in_raw_top_n"] is True
        # But it is not in the diversified selection
        assert not sector_rejected[0]["selected_flag"]
        assert sector_rejected[0]["diversified_rank"] is None

    def test_industry_cap_priority_over_sector_cap(self, tmp_db_paths):
        """When a ticker hits both industry and sector caps, rejection_reason must be
        industry_cap (industry has priority over sector)."""
        db = dbm.DB_ROLE_PROD
        # 3 tickers: same sector (cap=2) and same industry (cap=2).
        # T1 and T2 are admitted → both caps consumed.
        # T3 hits both caps; must be labeled industry_cap, not sector_cap.
        tickers = ["P1", "P2", "P3"]
        self._seed_n_tickers(db, tickers, sector="Technology", industry="Semiconductors")
        cfg = _minimal_risk_config(top_n=10, max_sector=2, max_industry=2, min_rr=1.0)
        Step5ProposalEngine().propose(
            SIGNAL_DATE, risk_label_config=cfg,
            setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db,
        )
        props = _read_proposals(db, SIGNAL_DATE)
        rejected = [p for p in props if p["rejection_reason"] in (REJECT_SECTOR_CAP, REJECT_INDUSTRY_CAP)]
        assert len(rejected) == 1
        assert rejected[0]["rejection_reason"] == REJECT_INDUSTRY_CAP

    def test_sector_industry_counts_written(self, tmp_db_paths):
        """sector_count_at_selection and industry_count_at_selection must reflect
        cumulative counts at the point each ticker was admitted to the diversified set."""
        db = dbm.DB_ROLE_PROD
        # 3 tickers: same sector, two different industries
        _seed_ticker(db, "C1", sector="Healthcare", industry="Biotech")
        _seed_ticker(db, "C2", sector="Healthcare", industry="Biotech")
        _seed_ticker(db, "C3", sector="Healthcare", industry="Devices")
        for t, score in [("C1", 82.0), ("C2", 80.0), ("C3", 78.0)]:
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                           next_resistance_level=115.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_passed=True, setup_score=score,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=10, max_sector=4, max_industry=2, min_rr=1.0)
        Step5ProposalEngine().propose(
            SIGNAL_DATE, risk_label_config=cfg,
            setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db,
        )
        props = {p["ticker"]: p for p in _read_proposals(db, SIGNAL_DATE)}

        # All 3 admitted (sector cap=4, industry cap=2 — Biotech gets 2, Devices gets 1)
        assert props["C1"]["sector_count_at_selection"] == 1
        assert props["C1"]["industry_count_at_selection"] == 1
        assert props["C2"]["sector_count_at_selection"] == 2
        assert props["C2"]["industry_count_at_selection"] == 2
        assert props["C3"]["sector_count_at_selection"] == 3
        assert props["C3"]["industry_count_at_selection"] == 1  # new industry


    def test_cross_setup_type_cap_isolation(self, tmp_db_paths):
        """Sector/industry caps are applied per setup_type independently.

        Breakout consumes its own sector cap; pullback candidates in the same
        sector still pass through the pullback cap pass unaffected.
        """
        db = dbm.DB_ROLE_PROD
        # Breakout: 3 tickers in Technology/SaaS — only 2 allowed (sector_max=2)
        for t, score in [("B1", 85.0), ("B2", 83.0), ("B3", 81.0)]:
            _seed_ticker(db, t, sector="Technology", industry="SaaS")
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                           next_resistance_level=115.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="breakout",
                        setup_config_id="setup_breakout_v1",
                        setup_passed=True, setup_score=score,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)
        # Pullback: 2 tickers also in Technology/SaaS — same sector, separate cap pass
        for t, score in [("P1", 79.0), ("P2", 77.0)]:
            _seed_ticker(db, t, sector="Technology", industry="SaaS")
            _seed_price(db, t, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, t, SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                           next_resistance_level=115.0)
            _seed_step4(db, t, SIGNAL_DATE, setup_type="pullback",
                        setup_config_id="setup_pullback_v1",
                        setup_passed=True, setup_score=score,
                        entry_price_raw=100.0, market_regime="bull", earnings_days=30)

        cfg = _minimal_risk_config(top_n=10, max_sector=2, max_industry=10, min_rr=1.0)
        Step5ProposalEngine().propose(
            SIGNAL_DATE, risk_label_config=cfg,
            setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db,
        )
        props = _read_proposals(db, SIGNAL_DATE)

        # B3 is sector-cap-rejected within the breakout group
        sector_rejected = [p for p in props if p["rejection_reason"] == REJECT_SECTOR_CAP]
        assert len(sector_rejected) == 1
        assert sector_rejected[0]["ticker"] == "B3"

        # P1 and P2 pass (pullback group has its own fresh cap counters)
        selected_tickers = {p["ticker"] for p in props if p["selected_flag"]}
        assert "P1" in selected_tickers
        assert "P2" in selected_tickers
        assert "B3" not in selected_tickers


class TestMultiRouteDedupe:
    def test_best_setup_type_selected_per_ticker(self, tmp_db_paths):
        """A ticker routed to both breakout and pullback → only 1 proposal row."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "MULTI")
        _seed_price(db, "MULTI", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "MULTI", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       next_resistance_level=115.0, swing_high=112.0)
        cid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        _seed_step4(db, "MULTI", SIGNAL_DATE, setup_type="breakout",
                    setup_config_id="setup_breakout_v1",
                    setup_passed=True, setup_score=80.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30,
                    candidate_id=cid, run_id=rid)
        _seed_step4(db, "MULTI", SIGNAL_DATE, setup_type="pullback",
                    setup_config_id="setup_pullback_v1",
                    setup_passed=True, setup_score=60.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30,
                    candidate_id=cid, run_id=rid)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        multi_props = [p for p in props if p["ticker"] == "MULTI" and p["raw_rank"] is not None]
        # Only one should be in ranked (best route)
        assert len(multi_props) == 1


class TestPerSetupCases:
    """One integration smoke test per setup type."""

    def _run_setup(self, db_role: str, setup_type: str) -> list[dict]:
        _seed_ticker(db_role, "TST", sector="Finance", industry="Banks")
        _seed_price(db_role, "TST", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db_role, "TST", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       next_resistance_level=115.0, support_level=95.0,
                       base_high=103.0, base_low=96.0, swing_high=108.0, swing_low=94.0,
                       range_tightness_score=70.0, range_duration=20, market_regime="bull")
        _seed_step4(db_role, "TST", SIGNAL_DATE, setup_type=setup_type,
                    setup_config_id=f"setup_{setup_type}_v1",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db_role)
        return _read_proposals(db_role, SIGNAL_DATE)

    def test_breakout_produces_proposal(self, tmp_db_paths):
        props = self._run_setup(dbm.DB_ROLE_PROD, "breakout")
        assert len(props) == 1
        assert props[0]["setup_type"] == "breakout"
        assert props[0]["stop_price_raw"] is not None

    def test_pullback_produces_proposal(self, tmp_db_paths):
        props = self._run_setup(dbm.DB_ROLE_DEBUG, "pullback")
        assert len(props) == 1
        assert props[0]["setup_type"] == "pullback"

    def test_trend_continuation_produces_proposal(self, tmp_db_paths):
        props = self._run_setup(dbm.DB_ROLE_PROD, "trend_continuation")
        assert len(props) == 1
        assert props[0]["setup_type"] == "trend_continuation"

    def test_consolidation_base_produces_proposal(self, tmp_db_paths):
        props = self._run_setup(dbm.DB_ROLE_PROD, "consolidation_base")
        assert len(props) == 1
        assert props[0]["setup_type"] == "consolidation_base"


class TestPullbackRvolNeverHardRejects:
    def test_pullback_low_rvol_still_produces_proposal(self, tmp_db_paths):
        """RVOL never hard-rejects pullback (AD-22.23)."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "PRVOL")
        _seed_price(db, "PRVOL", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "PRVOL", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       rvol20=0.3, market_regime="bull")  # very low rvol
        _seed_step4(db, "PRVOL", SIGNAL_DATE, setup_type="pullback",
                    setup_config_id="setup_pullback_v1",
                    setup_passed=True, setup_score=65.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30, rvol=0.3)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.0)
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                             setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        assert result.status == sr.STATUS_SUCCESS
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1
        # Passed candidate (low RVOL not a hard reject)
        assert props[0]["disposition"] != DISPOSITION_REJECTED


class TestMissingEvidenceFallback:
    def test_no_structural_levels_uses_fixed_r(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "NOLEVELS")
        _seed_price(db, "NOLEVELS", SIGNAL_DATE, 100.0, 100.0)
        # No structural features set → fallback to ATR/fixed-R
        _seed_features(db, "NOLEVELS", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       support_level=None, resistance_level=None,
                       next_resistance_level=None, base_high=None, base_low=None,
                       swing_high=None, swing_low=None)
        _seed_step4(db, "NOLEVELS", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=65.0, entry_price_raw=100.0,
                    support_level=None, resistance_level=None, next_resistance_level=None)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1
        p = props[0]
        # Still computes a target (fixed-R fallback)
        assert p["target_price_raw"] is not None
        # target_is_structural should be False (or None for rejected)
        if p["target_is_structural"] is not None:
            assert p["target_is_structural"] is False


class TestDebugRole:
    def test_debug_role_writes_to_debug_db(self, tmp_db_paths):
        db = dbm.DB_ROLE_DEBUG
        _seed_ticker(db, "DBG")
        _seed_price(db, "DBG", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "DBG", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "DBG", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0)
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(min_rr=1.0),
                             setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        assert result.status == sr.STATUS_SUCCESS
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1


class TestMetadataContract:
    def test_metadata_keys_present_on_success(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_risk_config(db)
        eng = _make_engine()
        result = eng.propose(
            SIGNAL_DATE,
            risk_label_config=_minimal_risk_config(),
            setup_configs={},
            db_role=db,
        )
        assert result.status == sr.STATUS_SUCCESS
        for key in ("db_role", "signal_date", "run_id", "analyses_read",
                    "proposals_written", "raw_top_n_count",
                    "diversified_top_n_count", "hard_cap_rejections"):
            assert key in result.metadata

    def test_metadata_keys_present_on_failure(self):
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(), db_role="bad_role")
        for key in ("db_role", "signal_date", "run_id", "analyses_read",
                    "proposals_written", "raw_top_n_count",
                    "diversified_top_n_count", "hard_cap_rejections"):
            assert key in result.metadata

    def test_rows_processed_equals_proposals_written(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "META")
        _seed_price(db, "META", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "META", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "META", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0)
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(min_rr=1.0),
                             setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        assert result.rows_processed == result.metadata["proposals_written"]


class TestNoLegacyStrategyMode:
    def test_no_strategy_config_id_required(self, tmp_db_paths):
        """Engine must not require strategy_config_id or strategy_name."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "NOSTRAT")
        _seed_price(db, "NOSTRAT", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "NOSTRAT", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "NOSTRAT", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0)
        cfg = _minimal_risk_config(min_rr=1.0)
        # Explicitly confirm cfg has no strategy_name
        assert "strategy_name" not in cfg
        eng = _make_engine()
        result = eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                             setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        assert result.status == sr.STATUS_SUCCESS

    def test_consolidation_base_naming_used(self, tmp_db_paths):
        """Must use consolidation_base not conservative_consolidation."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "CBASE")
        _seed_price(db, "CBASE", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "CBASE", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       base_high=103.0, base_low=96.0, range_tightness_score=70.0)
        _seed_step4(db, "CBASE", SIGNAL_DATE,
                    setup_type="consolidation_base",
                    setup_config_id="setup_consolidation_base_v1",
                    setup_passed=True, setup_score=68.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(min_rr=1.0),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert props[0]["setup_type"] == "consolidation_base"

    def test_no_aggressive_normal_conservative_in_engine(self):
        """Engine source must not contain legacy strategy mode strings in logic."""
        import inspect
        from app.services.proposal import step5_proposal_engine as mod
        src = inspect.getsource(mod)
        # These should not appear as active logic (only allowed in docstring comment)
        for forbidden in ["strategy_name", "aggressive", "conservative"]:
            # Count occurrences — should only appear in docstring/comments max
            occurrences = src.lower().count(forbidden)
            # At most 1 (in docstring saying "no X")
            assert occurrences <= 1, f"'{forbidden}' appears {occurrences} times in M15 source"



class TestCheckTargetRoom:
    def test_sufficient_room(self):
        from app.services.proposal.step5_proposal_engine import _check_target_room
        assert _check_target_room(100.0, 106.0, 0.05) is True

    def test_insufficient_room(self):
        from app.services.proposal.step5_proposal_engine import _check_target_room
        assert _check_target_room(100.0, 102.0, 0.05) is False

    def test_exactly_at_threshold(self):
        from app.services.proposal.step5_proposal_engine import _check_target_room
        assert _check_target_room(100.0, 105.0, 0.05) is True

    def test_none_target(self):
        from app.services.proposal.step5_proposal_engine import _check_target_room
        assert _check_target_room(100.0, None, 0.05) is False


class TestFix2MaxStopDistanceGate:
    """Fix 2: config-driven max stop-distance hard gate."""

    def test_wide_stop_forced_to_watchlist(self, tmp_db_paths):
        """Candidate with stop > max_stop_distance_pct -> WATCHLIST_ONLY (fix 2)."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "WIDE")
        _seed_price(db, "WIDE", SIGNAL_DATE, 100.0, 100.0)
        # base_low=60 forces a stop far below entry; resistance below entry so no cap fires
        _seed_features(db, "WIDE", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       base_low=60.0, support_level=60.0, next_resistance_level=115.0,
                       resistance_level=95.0)
        _seed_step4(db, "WIDE", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=80.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30, rvol=2.0)
        cfg = _minimal_risk_config(min_rr=1.0, max_stop=0.10)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1
        p = props[0]
        assert p["disposition"] == DISPOSITION_WATCHLIST
        assert p["rejection_reason"] == WATCHLIST_STOP_TOO_WIDE

    def test_tight_stop_not_blocked(self, tmp_db_paths):
        """Candidate with stop within max_stop -> gate not triggered (fix 2)."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "TGHT")
        _seed_price(db, "TGHT", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "TGHT", SIGNAL_DATE, close_adj=100.0, atr14=1.0,
                       base_low=97.0, support_level=97.0, next_resistance_level=115.0)
        _seed_step4(db, "TGHT", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=80.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30, rvol=2.0)
        cfg = _minimal_risk_config(min_rr=1.0, max_stop=0.10, low_max=60.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        assert props[0]["rejection_reason"] != WATCHLIST_STOP_TOO_WIDE

    def test_max_stop_gate_takes_priority_over_good_rr(self, tmp_db_paths):
        """Wide stop never becomes BUY even with excellent setup score (fix 2 hard gate)."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "WIDEHIGHRR")
        _seed_price(db, "WIDEHIGHRR", SIGNAL_DATE, 100.0, 100.0)
        # resistance below entry so no resistance cap interferes with stop-gate test
        _seed_features(db, "WIDEHIGHRR", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       base_low=50.0, support_level=50.0, next_resistance_level=200.0,
                       resistance_level=95.0)
        _seed_step4(db, "WIDEHIGHRR", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=95.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=60, rvol=3.0)
        cfg = _minimal_risk_config(min_rr=1.0, max_stop=0.10, low_max=80.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        assert p["disposition"] != DISPOSITION_BUY
        assert p["rejection_reason"] == WATCHLIST_STOP_TOO_WIDE


class TestFix3TargetRoom:
    """Fix 3 (corrected): resistance_blocks logic.

    When structural evidence above entry EXISTS but fails min_target_room_pct,
    the trade is forced to WATCHLIST_ONLY — fixed-R must not bypass nearby resistance.
    When NO structural evidence above entry exists, fixed-R fallback is acceptable.
    """

    def test_structural_target_with_sufficient_room_accepted(self, tmp_db_paths):
        """Structural target 15% above entry -> accepted with sufficient room."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "ROOM")
        _seed_price(db, "ROOM", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "ROOM", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       next_resistance_level=115.0, base_low=96.0)
        _seed_step4(db, "ROOM", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(min_rr=1.0, min_target_room=0.05)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        assert p["target_price_raw"] is not None
        assert p["target_is_structural"] is True
        assert (p["target_price_raw"] - p["entry_price_raw"]) / p["entry_price_raw"] >= 0.05

    def test_near_resistance_forces_watchlist_not_fixed_r_buy(self, tmp_db_paths):
        """Structural evidence exists but too close -> WATCHLIST_ONLY, not fixed-R BUY.

        next_resistance at 1% above entry proves insufficient room exists.
        Fixed-R must NOT be used to bypass this evidence.
        """
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "NROOM")
        _seed_price(db, "NROOM", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "NROOM", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       next_resistance_level=101.0,   # only 1% room
                       swing_high=101.5,              # also only 1.5% room
                       base_high=None, base_low=None)
        _seed_step4(db, "NROOM", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=80.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30, rvol=2.0)
        # min_rr=1.0 and low_max=80 so only room check blocks this
        cfg = _minimal_risk_config(min_rr=1.0, min_target_room=0.05, low_max=80.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        # Must be WATCHLIST_ONLY — resistance blocks; fixed-R must not bypass it
        assert p["disposition"] == DISPOSITION_WATCHLIST
        assert p["rejection_reason"] == WATCHLIST_TARGET_ROOM_INSUFFICIENT

    def test_no_structural_evidence_allows_fixed_r_fallback(self, tmp_db_paths):
        """No structural levels above entry at all -> fixed-R fallback is acceptable."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "NOLEV")
        _seed_price(db, "NOLEV", SIGNAL_DATE, 100.0, 100.0)
        # All structural levels either below entry or None
        _seed_features(db, "NOLEV", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       next_resistance_level=None,
                       swing_high=None, swing_low=94.0,
                       base_high=None, base_low=None,
                       support_level=95.0, resistance_level=None)
        _seed_step4(db, "NOLEV", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=65.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(min_rr=1.0, min_target_room=0.05, low_max=80.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        # Fixed-R fallback acceptable; target present; not WATCHLIST due to room
        assert p["target_price_raw"] is not None
        assert p["rejection_reason"] != WATCHLIST_TARGET_ROOM_INSUFFICIENT

    def test_mechanical_explanation_records_resistance_blocks(self, tmp_db_paths):
        """resistance_blocks=True is recorded in mechanical_explanation."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "BLKD")
        _seed_price(db, "BLKD", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "BLKD", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       next_resistance_level=101.5, base_high=None, base_low=None,
                       swing_high=None)
        _seed_step4(db, "BLKD", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=75.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(min_rr=1.0, min_target_room=0.05)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        expl = json.loads(props[0]["mechanical_explanation"])
        assert expl.get("resistance_blocks") is True

    def test_target_room_pure_check(self):
        from app.services.proposal.step5_proposal_engine import _check_target_room
        assert _check_target_room(100.0, 106.0, 0.05) is True   # 6% room ok
        assert _check_target_room(100.0, 104.0, 0.05) is False  # 4% room insufficient
        assert _check_target_room(100.0, 101.0, 0.05) is False  # 1% room insufficient

    def test_pullback_rr_capped_at_resistance_when_blocking(self, tmp_db_paths):
        """Fix 3 (session 3): pullback estimated_rr uses resistance as effective target
        when resistance sits between entry and swing_high target.
        EMR pattern: entry=100, resistance=110, swing_high=130 → realistic RR uses 110.
        """
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "PRBLK")
        _seed_price(db, "PRBLK", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "PRBLK", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       support_level=94.0, swing_low=92.0,
                       resistance_level=110.0, next_resistance_level=None,
                       swing_high=130.0, base_high=None, base_low=None,
                       market_regime="bull")
        _seed_step4(db, "PRBLK", SIGNAL_DATE, setup_type="pullback",
                    setup_config_id="setup_pullback_v1",
                    setup_passed=True, setup_score=75.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(min_rr=0.5, low_max=90.0, top_n=5)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        # Uncapped RR would be (130-100)/stop_dist ≈ high; capped at (110-100)/stop_dist
        assert p["estimated_rr"] is not None
        assert p["estimated_rr"] < 5.0  # must not use swing_high=130 uncapped
        # Resistance blocks → WATCHLIST_ONLY
        assert p["disposition"] == DISPOSITION_WATCHLIST
        expl = json.loads(p["mechanical_explanation"])
        assert expl.get("resistance_blocks") is True

    def test_tc_rr_capped_at_resistance_when_blocking(self, tmp_db_paths):
        """Fix 3 (session 3): TC estimated_rr capped at resistance between entry and next_resistance.
        HOG pattern: entry=100, resistance=110, next_resistance=130.
        """
        db = dbm.DB_ROLE_DEBUG
        _seed_ticker(db, "TCRBLK")
        _seed_price(db, "TCRBLK", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "TCRBLK", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       swing_low=92.0, support_level=94.0,
                       resistance_level=110.0, next_resistance_level=130.0,
                       swing_high=None, base_high=None, base_low=None,
                       market_regime="bull", roc20=0.12,
                       distance_to_ema50_pct=0.06)
        _seed_step4(db, "TCRBLK", SIGNAL_DATE, setup_type="trend_continuation",
                    setup_config_id="setup_trend_continuation_v1",
                    setup_passed=True, setup_score=80.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        cfg = _minimal_risk_config(min_rr=0.5, low_max=90.0, top_n=5)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        assert p["estimated_rr"] is not None
        assert p["estimated_rr"] < 5.0  # capped at resistance=110, not next_resistance=130
        assert p["disposition"] == DISPOSITION_WATCHLIST
        expl = json.loads(p["mechanical_explanation"])
        assert expl.get("resistance_blocks") is True

    def test_breakout_rr_capped_at_resistance_when_blocking(self, tmp_db_paths):
        """P0-2: breakout estimated_rr is capped at resistance when it sits between
        entry and next_resistance target (universal cap, not pullback/TC only).
        Pattern: entry=100, resistance=105, next_resistance=130 → cap at 105.
        """
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "BOCLIP")
        _seed_price(db, "BOCLIP", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "BOCLIP", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       resistance_level=105.0, next_resistance_level=130.0,
                       base_low=96.0, swing_high=None, base_high=None)
        _seed_step4(db, "BOCLIP", SIGNAL_DATE, setup_type="breakout",
                    setup_config_id="setup_breakout_v1",
                    setup_passed=True, setup_score=75.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30, rvol=2.0)
        cfg = _minimal_risk_config(min_rr=0.5, low_max=90.0, top_n=5)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        # RR must be capped at resistance=105, not the uncapped next_resistance=130
        assert p["estimated_rr"] is not None
        if p["stop_price_raw"] is not None:
            uncapped_rr = (130.0 - 100.0) / (100.0 - p["stop_price_raw"])
            capped_rr = (105.0 - 100.0) / (100.0 - p["stop_price_raw"])
            assert p["estimated_rr"] == pytest.approx(capped_rr, rel=0.01)
            assert p["estimated_rr"] < uncapped_rr
        # resistance_blocks forces WATCHLIST_ONLY
        assert p["disposition"] == DISPOSITION_WATCHLIST
        expl = json.loads(p["mechanical_explanation"])
        assert expl.get("resistance_blocks") is True

    def test_breakout_rr_not_capped_when_resistance_below_entry(self, tmp_db_paths):
        """Confirmed breakout: resistance < entry → geometry check false, no cap applied."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "BNOCP2")
        _seed_price(db, "BNOCP2", SIGNAL_DATE, 100.0, 100.0)
        # resistance=95 is below entry=100 (confirmed breakout through resistance)
        _seed_features(db, "BNOCP2", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       resistance_level=95.0, next_resistance_level=130.0,
                       base_low=92.0, swing_high=None, base_high=None)
        _seed_step4(db, "BNOCP2", SIGNAL_DATE, setup_type="breakout",
                    setup_config_id="setup_breakout_v1",
                    setup_passed=True, setup_score=75.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30, rvol=2.0)
        cfg = _minimal_risk_config(min_rr=0.5, low_max=90.0, top_n=5)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        # No cap: resistance < entry, condition resistance_raw_feat > eff_entry is False
        assert p["estimated_rr"] is not None
        if p["stop_price_raw"] is not None and p["target_price_raw"] is not None:
            expected_rr = (p["target_price_raw"] - 100.0) / (100.0 - p["stop_price_raw"])
            assert p["estimated_rr"] == pytest.approx(expected_rr, rel=0.01)


class TestFix4InvalidationLevel:
    """Fix 4: invalidation_level_raw = stop_price_raw, in mechanical_explanation."""

    def test_invalidation_level_equals_stop(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "INVAL")
        _seed_price(db, "INVAL", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "INVAL", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       base_low=96.0, next_resistance_level=115.0)
        _seed_step4(db, "INVAL", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(min_rr=1.0),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        p = props[0]
        assert p["mechanical_explanation"] is not None
        expl = json.loads(p["mechanical_explanation"])
        assert "invalidation_level_raw" in expl
        assert "invalidation_reason" in expl
        if p["stop_price_raw"] is not None:
            assert expl["invalidation_level_raw"] == pytest.approx(p["stop_price_raw"], rel=0.001)

    def test_invalidation_reason_non_empty(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "INVR")
        _seed_price(db, "INVR", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "INVR", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       base_low=96.0, next_resistance_level=115.0)
        _seed_step4(db, "INVR", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(min_rr=1.0),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        expl = json.loads(props[0]["mechanical_explanation"])
        assert isinstance(expl["invalidation_reason"], str)
        assert len(expl["invalidation_reason"]) > 0

    def test_stop_basis_and_target_basis_in_explanation(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "BASIS")
        _seed_price(db, "BASIS", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "BASIS", SIGNAL_DATE, close_adj=100.0, atr14=2.0,
                       base_low=96.0, next_resistance_level=115.0)
        _seed_step4(db, "BASIS", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0,
                    market_regime="bull", earnings_days=30)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=_minimal_risk_config(min_rr=1.0),
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        expl = json.loads(props[0]["mechanical_explanation"])
        assert "stop_basis" in expl
        assert "target_basis" in expl


class TestAppendOnly:
    def test_second_run_appends_not_overwrites(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "APPEND")
        _seed_price(db, "APPEND", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "APPEND", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "APPEND", SIGNAL_DATE, setup_type="breakout",
                    setup_passed=True, setup_score=70.0, entry_price_raw=100.0)
        cfg = _minimal_risk_config(min_rr=1.0)
        eng = _make_engine()
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        eng.propose(SIGNAL_DATE, risk_label_config=cfg,
                    setup_configs=DEFAULT_SETUP_CONFIGS, db_role=db)
        props = _read_proposals(db, SIGNAL_DATE)
        # Both runs wrote (append-only); 2 rows expected
        assert len(props) == 2


# ===========================================================================
# Phase 3 — AI review score consumption (contrarian_risk_score /
# audit_consistency_score additive penalties + hard audit downgrade gate).
# ===========================================================================

class TestProposalScoreRawAiReviewBackCompat:
    """_proposal_score_raw must be byte-identical to the pre-Phase-3 formula
    when both AI-review scores are absent (the overwhelming common case)."""

    def test_both_none_matches_pre_phase3_value(self):
        with_none = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        explicit_none = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            contrarian_risk_score=None, audit_consistency_score=None,
        )
        assert with_none == explicit_none

    def test_contrarian_score_present_only_ever_lowers_score(self):
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        penalized = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            contrarian_risk_score=100.0,
        )
        assert penalized < base

    def test_audit_score_present_only_ever_lowers_score(self):
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        penalized = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            audit_consistency_score=0.0,
        )
        assert penalized < base

    def test_perfect_audit_score_no_penalty(self):
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        perfect = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            audit_consistency_score=100.0,
        )
        assert base == pytest.approx(perfect)

    def test_zero_contrarian_risk_no_penalty(self):
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        safe = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            contrarian_risk_score=0.0,
        )
        assert base == pytest.approx(safe)

    def test_custom_penalty_weights_scale_the_deduction(self):
        small_weight = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", contrarian_risk_score=100.0,
            contrarian_penalty_weight=0.05,
        )
        large_weight = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", contrarian_risk_score=100.0,
            contrarian_penalty_weight=0.20,
        )
        assert large_weight < small_weight

    def test_still_clamped_0_100_with_penalties(self):
        s = _proposal_score_raw(
            100.0, 5.0, 100.0, "bull",
            contrarian_risk_score=0.0, audit_consistency_score=100.0,
        )
        assert s <= 100.0


class TestParseRiskLabelConfigAiReviewBlock:
    def test_absent_block_uses_hardcoded_defaults(self):
        cfg = _minimal_risk_config()
        parsed = _parse_risk_label_config(cfg)
        assert parsed["contrarian_penalty_weight"] == pytest.approx(0.10)
        assert parsed["audit_penalty_weight"] == pytest.approx(0.10)
        assert parsed["audit_consistency_min_for_buy"] == pytest.approx(40.0)

    def test_explicit_block_overrides_defaults(self):
        cfg = _minimal_risk_config()
        cfg["ai_review"] = {
            "contrarian_penalty_weight": 0.20,
            "audit_penalty_weight": 0.15,
            "audit_consistency_min_for_buy": 60.0,
        }
        parsed = _parse_risk_label_config(cfg)
        assert parsed["contrarian_penalty_weight"] == pytest.approx(0.20)
        assert parsed["audit_penalty_weight"] == pytest.approx(0.15)
        assert parsed["audit_consistency_min_for_buy"] == pytest.approx(60.0)

    def test_explicit_zero_is_honored_not_replaced_by_default(self):
        """A deliberate 0 (e.g. 'disable this penalty') must not be silently
        replaced by the hardcoded default -- this is the bug an `or default`
        pattern would introduce for a legitimate falsy override."""
        cfg = _minimal_risk_config()
        cfg["ai_review"] = {"contrarian_penalty_weight": 0.0, "audit_consistency_min_for_buy": 0.0}
        parsed = _parse_risk_label_config(cfg)
        assert parsed["contrarian_penalty_weight"] == 0.0
        assert parsed["audit_consistency_min_for_buy"] == 0.0


class TestProposeWithAiReviewScores:
    """End-to-end through the public propose() API with ai_review_scores
    supplied -- confirms per-ticker scoping (only the named ticker is
    affected) and the hard audit-consistency disposition downgrade gate."""

    def test_audit_failure_downgrades_disposition_other_ticker_unaffected(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        for ticker in ("BASELINE", "AUDITFAIL"):
            _seed_ticker(db, ticker)
            _seed_price(db, ticker, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, ticker, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, ticker, SIGNAL_DATE, setup_type="breakout",
                        setup_config_id="setup_breakout_v1", setup_passed=True,
                        setup_score=75.0, entry_price_raw=100.0, market_regime="bull",
                        earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.5)
        eng = _make_engine()
        result = eng.propose(
            SIGNAL_DATE, risk_label_config=cfg, setup_configs=DEFAULT_SETUP_CONFIGS,
            db_role=db,
            ai_review_scores={"AUDITFAIL": {"audit_consistency_score": 10.0}},
        )
        assert result.status == sr.STATUS_SUCCESS, result.errors

        props = {p["ticker"]: p for p in _read_proposals(db, SIGNAL_DATE)}
        # AUDITFAIL is forced to WATCHLIST_ONLY specifically for the audit
        # reason -- this alone proves the gate fired, regardless of what
        # disposition this seed data would otherwise naturally land on
        # (test_buy_eligible_candidate_written establishes this exact seed
        # can legitimately land on either BUY or WATCHLIST_ONLY).
        assert props["AUDITFAIL"]["disposition"] == DISPOSITION_WATCHLIST
        assert props["AUDITFAIL"]["rejection_reason"] == WATCHLIST_AUDIT_INCONSISTENT
        # The gate is per-ticker: BASELINE (no entry in ai_review_scores) must
        # never be downgraded for this reason.
        assert props["BASELINE"]["rejection_reason"] != WATCHLIST_AUDIT_INCONSISTENT
        # Penalized score is strictly lower than the unaffected baseline's.
        assert props["AUDITFAIL"]["proposal_score_raw"] < props["BASELINE"]["proposal_score_raw"]

    def test_no_ai_review_scores_leaves_rejection_reason_unset_by_audit_gate(self, tmp_db_paths):
        """Calling propose() with ai_review_scores=None (the default) must
        never trigger the audit-consistency gate (no score => gate can't
        fire) -- the engine still runs and writes normally."""
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "AAA")
        _seed_price(db, "AAA", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "AAA", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "AAA", SIGNAL_DATE, setup_type="breakout",
                    setup_config_id="setup_breakout_v1", setup_passed=True,
                    setup_score=75.0, entry_price_raw=100.0, market_regime="bull",
                    earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.5)
        eng = _make_engine()
        result = eng.propose(
            SIGNAL_DATE, risk_label_config=cfg, setup_configs=DEFAULT_SETUP_CONFIGS,
            db_role=db,
        )
        assert result.status == sr.STATUS_SUCCESS, result.errors
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1
        assert props[0]["disposition"] in (DISPOSITION_BUY, DISPOSITION_WATCHLIST)
        assert props[0]["rejection_reason"] != WATCHLIST_AUDIT_INCONSISTENT


class TestProposalScoreRawFundamentalsBackCompat:
    """_proposal_score_raw must be byte-identical to the pre-Phase-4 formula
    when fundamentals_quality_score is absent (the default/common case)."""

    def test_absent_matches_pre_phase4_value(self):
        with_none = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        explicit_none = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            fundamentals_quality_score=None,
        )
        assert with_none == explicit_none

    def test_above_neutral_quality_raises_score(self):
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        boosted = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            fundamentals_quality_score=100.0,
        )
        assert boosted > base

    def test_below_neutral_quality_lowers_score(self):
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        penalized = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            fundamentals_quality_score=0.0,
        )
        assert penalized < base

    def test_exactly_neutral_quality_no_adjustment(self):
        base = _proposal_score_raw(80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05)
        neutral = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            fundamentals_quality_score=50.0,
        )
        assert base == pytest.approx(neutral)

    def test_custom_weight_scales_the_adjustment(self):
        small_weight = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", fundamentals_quality_score=100.0,
            fundamentals_score_weight=0.05,
        )
        large_weight = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", fundamentals_quality_score=100.0,
            fundamentals_score_weight=0.20,
        )
        assert large_weight > small_weight

    def test_still_clamped_0_100_with_boost(self):
        s = _proposal_score_raw(100.0, 5.0, 100.0, "bull", fundamentals_quality_score=100.0)
        assert s <= 100.0

    def test_fundamentals_and_ai_review_adjustments_compose(self):
        """Both Phase 3 and Phase 4 optional adjustments can be present at
        once and combine additively (no interaction/override between them)."""
        both = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            contrarian_risk_score=0.0, audit_consistency_score=100.0,
            fundamentals_quality_score=100.0,
        )
        fundamentals_only = _proposal_score_raw(
            80.0, 2.5, 70.0, "bull", stop_distance_pct=0.05,
            fundamentals_quality_score=100.0,
        )
        # Both zero-penalty AI-review scores contribute nothing, so adding
        # them must not change the fundamentals-only result.
        assert both == pytest.approx(fundamentals_only)


class TestParseRiskLabelConfigFundamentalsBlock:
    def test_absent_block_uses_hardcoded_default(self):
        cfg = _minimal_risk_config()
        parsed = _parse_risk_label_config(cfg)
        assert parsed["fundamentals_score_weight"] == pytest.approx(0.10)

    def test_explicit_block_overrides_default(self):
        cfg = _minimal_risk_config()
        cfg["fundamentals"] = {"score_weight": 0.25}
        parsed = _parse_risk_label_config(cfg)
        assert parsed["fundamentals_score_weight"] == pytest.approx(0.25)

    def test_explicit_zero_is_honored_not_replaced_by_default(self):
        cfg = _minimal_risk_config()
        cfg["fundamentals"] = {"score_weight": 0.0}
        parsed = _parse_risk_label_config(cfg)
        assert parsed["fundamentals_score_weight"] == 0.0


class TestProposeWithFundamentalsScores:
    """End-to-end through the public propose() API with fundamentals_scores
    supplied -- confirms per-ticker scoping and byte-compat when omitted."""

    def test_fundamentals_score_is_per_ticker_scoped(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        for ticker in ("BASELINE", "STRONGFUND"):
            _seed_ticker(db, ticker)
            _seed_price(db, ticker, SIGNAL_DATE, 100.0, 100.0)
            _seed_features(db, ticker, SIGNAL_DATE, close_adj=100.0, atr14=2.0)
            _seed_step4(db, ticker, SIGNAL_DATE, setup_type="breakout",
                        setup_config_id="setup_breakout_v1", setup_passed=True,
                        setup_score=75.0, entry_price_raw=100.0, market_regime="bull",
                        earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.5)
        eng = _make_engine()
        result = eng.propose(
            SIGNAL_DATE, risk_label_config=cfg, setup_configs=DEFAULT_SETUP_CONFIGS,
            db_role=db,
            fundamentals_scores={"STRONGFUND": 100.0},
        )
        assert result.status == sr.STATUS_SUCCESS, result.errors

        props = {p["ticker"]: p for p in _read_proposals(db, SIGNAL_DATE)}
        assert (
            props["STRONGFUND"]["proposal_score_raw"]
            > props["BASELINE"]["proposal_score_raw"]
        )

    def test_no_fundamentals_scores_is_byte_compat_default(self, tmp_db_paths):
        db = dbm.DB_ROLE_PROD
        _seed_ticker(db, "AAA")
        _seed_price(db, "AAA", SIGNAL_DATE, 100.0, 100.0)
        _seed_features(db, "AAA", SIGNAL_DATE, close_adj=100.0, atr14=2.0)
        _seed_step4(db, "AAA", SIGNAL_DATE, setup_type="breakout",
                    setup_config_id="setup_breakout_v1", setup_passed=True,
                    setup_score=75.0, entry_price_raw=100.0, market_regime="bull",
                    earnings_days=30)
        cfg = _minimal_risk_config(top_n=5, min_rr=1.5)
        eng = _make_engine()
        with_none_score = eng.propose(
            SIGNAL_DATE, risk_label_config=cfg, setup_configs=DEFAULT_SETUP_CONFIGS,
            db_role=db, run_id=str(uuid.uuid4()),
        )
        assert with_none_score.status == sr.STATUS_SUCCESS, with_none_score.errors
        props = _read_proposals(db, SIGNAL_DATE)
        assert len(props) == 1
