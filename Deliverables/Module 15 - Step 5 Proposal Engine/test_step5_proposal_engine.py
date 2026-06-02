"""Tests for Module 15 — Step 5 Proposal Engine.

All tests run fully offline (no network, no provider) and never touch the real
prod / debug / simulation DB files: the ``tmp_db_paths`` fixture redirects every
DuckDB settings path into pytest ``tmp_path`` and applies the real Module 03
schema there (mirroring ``tests/test_step4_analysis.py``). Step 4 analysis rows
are seeded directly into ``step4_analysis``, screening scores into
``step3_candidates`` and sector/industry into ``ticker_master``; Module 15 reads
them read-only and only ever inserts into ``step5_proposals``.
"""

from __future__ import annotations

import ast
import inspect
import math
import uuid
from datetime import date
from pathlib import Path

import pytest

from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager as sm
from app.services.proposal import step5_proposal_engine as s5mod
from app.services.proposal.step5_proposal_engine import Step5ProposalEngine
from app.utils import service_result

REQUIRED_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "db_role",
        "signal_date",
        "strategy_config_id",
        "run_id",
        "analyses_read",
        "proposals_written",
        "raw_top_n_count",
        "diversified_top_n_count",
        "hard_cap_rejections",
    }
)

CONFIG_ID = "cfg-1"
SIGNAL_DATE = date(2024, 1, 15)


# --------------------------------------------------------------------------- #
# Strategy config helpers.
# --------------------------------------------------------------------------- #
def hard_cap_config(
    *,
    top_n: int = 3,
    max_sector_count: int = 2,
    max_industry_count: int = 1,
) -> dict:
    return {
        "diversification": {
            "hard_cap_enabled": True,
            "top_n": top_n,
            "max_sector_count": max_sector_count,
            "max_industry_count": max_industry_count,
        }
    }


def soft_penalty_config(
    *,
    top_n: int = 3,
    sector_penalty: float = 0.9,
    industry_penalty: float = 0.85,
) -> dict:
    return {
        "diversification": {
            "hard_cap_enabled": False,
            "top_n": top_n,
            "sector_penalty": sector_penalty,
            "industry_penalty": industry_penalty,
        }
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
# Seeding helpers (write directly to the DB; harness only, not Module 15).
# --------------------------------------------------------------------------- #
_INSERT_TICKER = (
    "INSERT INTO ticker_master "
    "(ticker, sector, industry, symbol_type, active_flag, delisted_flag, "
    " last_updated) "
    "VALUES (?, ?, ?, 'stock', TRUE, FALSE, CAST(now() AS TIMESTAMP))"
)

_INSERT_CANDIDATE = (
    "INSERT INTO step3_candidates "
    "(candidate_id, run_id, strategy_config_id, ticker, signal_date, "
    " screening_score, passed_hard_filters, hard_filter_fail_reasons, "
    " soft_score_components, feature_snapshot_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, TRUE, '[]', '{}', '{}', CAST(now() AS TIMESTAMP))"
)

_INSERT_ANALYSIS = (
    "INSERT INTO step4_analysis "
    "(analysis_id, candidate_id, run_id, strategy_config_id, ticker, "
    " signal_date, setup_type, setup_score, breakout_quality_score, "
    " squeeze_score, timing_score, confirmation_score, estimated_rr, "
    " stop_price_raw, target_price_raw, earnings_penalty, macro_penalty, "
    " explanation_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, 'breakout', ?, 0, 0, ?, 0, ?, 0, 0, 0, 0, "
    " '{}', CAST(now() AS TIMESTAMP))"
)


def _connect(db_path: Path, read_only: bool = False):
    import duckdb

    return duckdb.connect(str(db_path), read_only=read_only)


def seed_candidate(
    db_path: Path,
    ticker: str,
    *,
    candidate_id: str,
    screening_score: float | None,
    sector: str | None = "Technology",
    industry: str | None = "Software",
    signal_date: date = SIGNAL_DATE,
    config_id: str = CONFIG_ID,
) -> None:
    """Seed a ticker_master row + a step3_candidates row for one candidate."""
    conn = _connect(db_path)
    try:
        # ticker_master may already have the ticker; ignore duplicate inserts.
        try:
            conn.execute(_INSERT_TICKER, [ticker, sector, industry])
        except Exception:  # noqa: BLE001 - duplicate ticker primary key is fine
            pass
        conn.execute(
            _INSERT_CANDIDATE,
            [
                candidate_id,
                "seed-run",
                config_id,
                ticker,
                signal_date,
                screening_score,
            ],
        )
    finally:
        conn.close()


def seed_analysis(
    db_path: Path,
    ticker: str,
    *,
    candidate_id: str,
    analysis_id: str | None = None,
    setup_score: float | None,
    timing_score: float | None = 60.0,
    estimated_rr: float | None = 2.5,
    signal_date: date = SIGNAL_DATE,
    config_id: str = CONFIG_ID,
) -> str:
    """Seed one step4_analysis row; returns its analysis_id."""
    aid = analysis_id if analysis_id is not None else str(uuid.uuid4())
    conn = _connect(db_path)
    try:
        conn.execute(
            _INSERT_ANALYSIS,
            [
                aid,
                candidate_id,
                "seed-run",
                config_id,
                ticker,
                signal_date,
                setup_score,
                timing_score,
                estimated_rr,
            ],
        )
    finally:
        conn.close()
    return aid


def seed_full(
    db_path: Path,
    ticker: str,
    *,
    setup_score: float | None,
    screening_score: float | None,
    timing_score: float | None = 60.0,
    estimated_rr: float | None = 2.5,
    sector: str | None = "Technology",
    industry: str | None = "Software",
    candidate_id: str | None = None,
    analysis_id: str | None = None,
) -> tuple[str, str]:
    """Seed candidate + analysis for a ticker. Returns (candidate_id, analysis_id)."""
    cid = candidate_id if candidate_id is not None else f"cand-{ticker}"
    seed_candidate(
        db_path,
        ticker,
        candidate_id=cid,
        screening_score=screening_score,
        sector=sector,
        industry=industry,
    )
    aid = seed_analysis(
        db_path,
        ticker,
        candidate_id=cid,
        analysis_id=analysis_id,
        setup_score=setup_score,
        timing_score=timing_score,
        estimated_rr=estimated_rr,
    )
    return cid, aid


def fetch_proposals(db_path: Path) -> list[dict]:
    conn = _connect(db_path, read_only=True)
    try:
        cols = [d[0] for d in conn.execute("SELECT * FROM step5_proposals").description]
        rows = conn.execute("SELECT * FROM step5_proposals").fetchall()
    finally:
        conn.close()
    return [dict(zip(cols, r)) for r in rows]


# --------------------------------------------------------------------------- #
# Public API / metadata.
# --------------------------------------------------------------------------- #
def test_propose_signature_exact() -> None:
    sig = inspect.signature(Step5ProposalEngine.propose)
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
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    assert res.run_id
    uuid.UUID(res.run_id)  # parses as a valid UUID


def test_run_id_preserved_when_supplied(tmp_db_paths: dict[str, Path]) -> None:
    res = Step5ProposalEngine().propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID, run_id="fixed-run"
    )
    assert res.run_id == "fixed-run"
    assert res.metadata["run_id"] == "fixed-run"


def test_metadata_keys_exact_on_success(tmp_db_paths: dict[str, Path]) -> None:
    seed_full(tmp_db_paths[dbm.DB_ROLE_PROD], "AAA", setup_score=80, screening_score=70)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    assert set(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_exact_on_guard_failure() -> None:
    res = Step5ProposalEngine().propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID, db_role="simulation"
    )
    assert set(res.metadata) == REQUIRED_METADATA_KEYS


def test_rows_processed_equals_proposals_written(tmp_db_paths: dict[str, Path]) -> None:
    seed_full(tmp_db_paths[dbm.DB_ROLE_PROD], "AAA", setup_score=80, screening_score=70)
    seed_full(tmp_db_paths[dbm.DB_ROLE_PROD], "BBB", setup_score=60, screening_score=50)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    assert res.rows_processed == res.metadata["proposals_written"] == 2


# --------------------------------------------------------------------------- #
# db_role + config guards (before DB access).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_role", ["simulation", "prod_x", "", "PROD"])
def test_invalid_db_role_fails_without_db_access(bad_role: str) -> None:
    # No tmp_db_paths fixture: any DB access would error, proving guard runs first.
    res = Step5ProposalEngine().propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID, db_role=bad_role
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == 0
    assert res.metadata["analyses_read"] == 0
    assert res.metadata["proposals_written"] == 0


def test_debug_role_supported(tmp_db_paths: dict[str, Path]) -> None:
    seed_full(tmp_db_paths[dbm.DB_ROLE_DEBUG], "AAA", setup_score=80, screening_score=70)
    res = Step5ProposalEngine().propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID, db_role="debug"
    )
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["proposals_written"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["diversification"].pop("hard_cap_enabled"),
        lambda c: c["diversification"].__setitem__("hard_cap_enabled", "yes"),
        lambda c: c["diversification"].pop("top_n"),
        lambda c: c["diversification"].__setitem__("top_n", 0),
        lambda c: c["diversification"].__setitem__("top_n", True),
        lambda c: c["diversification"].pop("max_sector_count"),
        lambda c: c["diversification"].__setitem__("max_sector_count", 0),
        lambda c: c["diversification"].pop("max_industry_count"),
        lambda c: c.pop("diversification"),
    ],
)
def test_bad_hard_cap_config_fails_without_db_access(mutate) -> None:
    cfg = hard_cap_config()
    mutate(cfg)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["analyses_read"] == 0
    assert res.metadata["proposals_written"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["diversification"].pop("sector_penalty"),
        lambda c: c["diversification"].__setitem__("sector_penalty", 0.0),
        lambda c: c["diversification"].__setitem__("sector_penalty", 1.5),
        lambda c: c["diversification"].__setitem__("sector_penalty", True),
        lambda c: c["diversification"].pop("industry_penalty"),
        lambda c: c["diversification"].__setitem__("industry_penalty", -0.1),
    ],
)
def test_bad_soft_penalty_config_fails_without_db_access(mutate) -> None:
    cfg = soft_penalty_config()
    mutate(cfg)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["proposals_written"] == 0


def test_non_dict_config_fails() -> None:
    res = Step5ProposalEngine().propose(SIGNAL_DATE, ["not", "a", "dict"], CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED


def test_soft_penalty_boundary_one_is_valid(tmp_db_paths: dict[str, Path]) -> None:
    cfg = soft_penalty_config(sector_penalty=1.0, industry_penalty=1.0)
    seed_full(tmp_db_paths[dbm.DB_ROLE_PROD], "AAA", setup_score=80, screening_score=70)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS


# --------------------------------------------------------------------------- #
# Empty input.
# --------------------------------------------------------------------------- #
def test_empty_input_success_no_insert(tmp_db_paths: dict[str, Path]) -> None:
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["analyses_read"] == 0
    assert res.metadata["proposals_written"] == 0
    assert fetch_proposals(tmp_db_paths[dbm.DB_ROLE_PROD]) == []


def test_signal_date_and_config_isolation(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    # Different config id -> not read.
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), "other-cfg")
    assert res.metadata["analyses_read"] == 0


# --------------------------------------------------------------------------- #
# NULL handling.
# --------------------------------------------------------------------------- #
def test_null_setup_or_screening_not_analyzable(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=None, screening_score=70)
    seed_full(prod, "BBB", setup_score=80, screening_score=None)
    seed_full(prod, "CCC", setup_score=80, screening_score=70)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    assert res.metadata["analyses_read"] == 3
    assert res.metadata["proposals_written"] == 1
    rows = fetch_proposals(prod)
    assert {r["ticker"] for r in rows} == {"CCC"}


def test_null_timing_defaults_to_50(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # estimated_rr below 1.8 -> rr_score 0; isolate timing contribution.
    seed_full(
        prod, "AAA", setup_score=0, screening_score=0, timing_score=None,
        estimated_rr=1.0,
    )
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    row = fetch_proposals(prod)[0]
    # 0.15 * 50.0 == 7.5
    assert math.isclose(row["proposal_score_raw"], 7.5, rel_tol=1e-9)


def test_null_rr_scores_zero_and_sorts_lowest(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Two tickers with identical raw score (same setup/screening/timing, rr_score
    # both 0 because one is NULL and the other < 1.8) -> tie broken by RR then ticker.
    seed_full(
        prod, "BBB", setup_score=50, screening_score=50, timing_score=50,
        estimated_rr=None,
    )
    seed_full(
        prod, "AAA", setup_score=50, screening_score=50, timing_score=50,
        estimated_rr=1.0,
    )
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    # Equal raw score; estimated_rr 1.0 > NULL(-inf), so AAA ranks above BBB.
    assert rows["AAA"]["raw_rank"] < rows["BBB"]["raw_rank"]


def test_null_sector_industry_buckets(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(
        prod, "AAA", setup_score=80, screening_score=70, sector=None, industry=None
    )
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    row = fetch_proposals(prod)[0]
    assert s5mod.UNKNOWN_SECTOR in row["mechanical_explanation"]
    assert s5mod.UNKNOWN_INDUSTRY in row["mechanical_explanation"]


# --------------------------------------------------------------------------- #
# Raw scoring / RR tiers / clamping / raw ranking.
# --------------------------------------------------------------------------- #
def test_raw_score_formula(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(
        prod, "AAA", setup_score=80, screening_score=60, timing_score=40,
        estimated_rr=2.5,  # rr_score 80
    )
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    row = fetch_proposals(prod)[0]
    expected = 0.40 * 80 + 0.25 * 60 + 0.20 * 80 + 0.15 * 40
    assert math.isclose(row["proposal_score_raw"], expected, rel_tol=1e-9)


@pytest.mark.parametrize(
    "rr,expected_rr_score",
    [
        (3.0, 100.0),
        (3.5, 100.0),
        (2.99, 80.0),
        (2.2, 80.0),
        (2.19, 60.0),
        (1.8, 60.0),
        (1.79, 0.0),
        (0.0, 0.0),
        (None, 0.0),
    ],
)
def test_rr_tier_boundaries(rr, expected_rr_score) -> None:
    assert s5mod._rr_score(rr) == expected_rr_score


def test_raw_score_clamped_to_100() -> None:
    # All-max inputs would exceed nothing here, but verify clamp helper directly.
    assert s5mod._proposal_score_raw(100, 100, 100, 100) == 100.0
    assert s5mod._proposal_score_raw(0, 0, 0, 0) == 0.0


def test_raw_ranking_tie_break_ticker(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Identical scores and identical RR -> ticker ASC decides.
    for tk in ("CCC", "AAA", "BBB"):
        seed_full(
            prod, tk, setup_score=50, screening_score=50, timing_score=50,
            estimated_rr=2.5,
        )
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(top_n=2), CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    assert rows["AAA"]["raw_rank"] == 1
    assert rows["BBB"]["raw_rank"] == 2
    assert rows["CCC"]["raw_rank"] == 3
    assert rows["AAA"]["in_raw_top_n"] is True
    assert rows["BBB"]["in_raw_top_n"] is True
    assert rows["CCC"]["in_raw_top_n"] is False


# --------------------------------------------------------------------------- #
# Hard-cap diversification.
# --------------------------------------------------------------------------- #
def test_hard_cap_sector_reject(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # max_sector_count=2, all Technology with distinct industries; 3rd is rejected.
    seed_full(prod, "AAA", setup_score=90, screening_score=90,
              sector="Tech", industry="I1")
    seed_full(prod, "BBB", setup_score=80, screening_score=80,
              sector="Tech", industry="I2")
    seed_full(prod, "CCC", setup_score=70, screening_score=70,
              sector="Tech", industry="I3")
    cfg = hard_cap_config(top_n=5, max_sector_count=2, max_industry_count=5)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    assert rows["AAA"]["diversified_rank"] == 1
    assert rows["BBB"]["diversified_rank"] == 2
    assert rows["CCC"]["diversified_rank"] is None
    assert rows["CCC"]["rejection_reason"] == "sector_cap"
    assert rows["CCC"]["in_diversified_top_n"] is False
    assert rows["CCC"]["selected_flag"] is False
    # rejected row still inserted with final == raw, no penalty.
    assert rows["CCC"]["proposal_score_final"] == rows["CCC"]["proposal_score_raw"]
    assert res.metadata["hard_cap_rejections"] == 1
    assert res.metadata["proposals_written"] == 3


def test_hard_cap_industry_reject(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # max_industry_count=1; two share an industry, 2nd rejected on industry.
    seed_full(prod, "AAA", setup_score=90, screening_score=90,
              sector="S1", industry="Same")
    seed_full(prod, "BBB", setup_score=80, screening_score=80,
              sector="S2", industry="Same")
    cfg = hard_cap_config(top_n=5, max_sector_count=5, max_industry_count=1)
    Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    assert rows["AAA"]["diversified_rank"] == 1
    assert rows["BBB"]["diversified_rank"] is None
    assert rows["BBB"]["rejection_reason"] == "industry_cap"


def test_hard_cap_both_full_uses_sector_cap(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Both caps = 1; 2nd row shares sector AND industry -> both full -> sector_cap.
    seed_full(prod, "AAA", setup_score=90, screening_score=90,
              sector="S", industry="I")
    seed_full(prod, "BBB", setup_score=80, screening_score=80,
              sector="S", industry="I")
    cfg = hard_cap_config(top_n=5, max_sector_count=1, max_industry_count=1)
    Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    assert rows["BBB"]["rejection_reason"] == "sector_cap"


def test_hard_cap_accept_sets_final_equals_raw(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70,
              sector="S1", industry="I1")
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    row = fetch_proposals(prod)[0]
    assert row["proposal_score_final"] == row["proposal_score_raw"]
    assert row["diversity_penalty"] == 0.0
    assert row["rejection_reason"] is None


# --------------------------------------------------------------------------- #
# Soft-penalty diversification.
# --------------------------------------------------------------------------- #
def test_soft_penalty_multiplier(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Three same-sector/same-industry rows in descending raw score order.
    seed_full(prod, "AAA", setup_score=90, screening_score=90,
              timing_score=90, estimated_rr=3.0, sector="S", industry="I")
    seed_full(prod, "BBB", setup_score=80, screening_score=80,
              timing_score=80, estimated_rr=3.0, sector="S", industry="I")
    cfg = soft_penalty_config(top_n=5, sector_penalty=0.9, industry_penalty=0.8)
    Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    # AAA: prior counts 0 -> final == raw.
    assert math.isclose(
        rows["AAA"]["proposal_score_final"],
        rows["AAA"]["proposal_score_raw"],
        rel_tol=1e-9,
    )
    # BBB: prior_sector=1, prior_industry=1 -> raw * 0.9 * 0.8.
    expected = rows["BBB"]["proposal_score_raw"] * 0.9 * 0.8
    assert math.isclose(rows["BBB"]["proposal_score_final"], expected, rel_tol=1e-9)
    # No rejections in soft-penalty mode.
    assert all(r["rejection_reason"] is None for r in rows.values())


def test_soft_penalty_reranks_by_final(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # AAA highest raw but heavily penalised (3rd in its sector); CCC different
    # sector, unpenalised, can overtake on final score.
    seed_full(prod, "AAA", setup_score=70, screening_score=70,
              timing_score=70, estimated_rr=3.0, sector="S", industry="I")
    seed_full(prod, "BBB", setup_score=69, screening_score=69,
              timing_score=69, estimated_rr=3.0, sector="S", industry="I")
    seed_full(prod, "CCC", setup_score=60, screening_score=60,
              timing_score=60, estimated_rr=3.0, sector="OTHER", industry="OTH")
    cfg = soft_penalty_config(top_n=5, sector_penalty=0.5, industry_penalty=0.5)
    Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    # BBB is 2nd in sector S: raw*0.5*0.5 = raw*0.25, dropping it below CCC.
    assert rows["CCC"]["diversified_rank"] < rows["BBB"]["diversified_rank"]


def test_soft_penalty_diversified_ticker_tie_break(
    tmp_db_paths: dict[str, Path],
) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Distinct sectors/industries so no penalty; equal final scores -> ticker ASC.
    seed_full(prod, "BBB", setup_score=50, screening_score=50,
              timing_score=50, estimated_rr=2.5, sector="S1", industry="I1")
    seed_full(prod, "AAA", setup_score=50, screening_score=50,
              timing_score=50, estimated_rr=2.5, sector="S2", industry="I2")
    Step5ProposalEngine().propose(SIGNAL_DATE, soft_penalty_config(), CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    assert rows["AAA"]["diversified_rank"] == 1
    assert rows["BBB"]["diversified_rank"] == 2


# --------------------------------------------------------------------------- #
# selected_flag / selected_top_n semantics.
# --------------------------------------------------------------------------- #
def test_selected_semantics(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # top_n=1, max_sector=1: AAA accepted (div rank 1), BBB rejected (sector cap)
    # but BBB is raw_rank 2 -> not in raw top 1 either.
    seed_full(prod, "AAA", setup_score=90, screening_score=90,
              sector="S", industry="I1")
    seed_full(prod, "BBB", setup_score=80, screening_score=80,
              sector="S", industry="I2")
    cfg = hard_cap_config(top_n=1, max_sector_count=1, max_industry_count=5)
    Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    # AAA: raw_rank 1 (in_raw_top_n), div_rank 1 (in_div_top_n) -> selected.
    assert rows["AAA"]["selected_flag"] is True
    assert rows["AAA"]["selected_top_n"] is True
    # BBB: raw_rank 2 (not top 1), rejected (div NULL) -> neither.
    assert rows["BBB"]["selected_flag"] is False
    assert rows["BBB"]["selected_top_n"] is False


def test_selected_top_n_raw_only(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # A raw-top-N row that gets rejected by the cap is still selected_top_n=True
    # via the raw branch (in_raw_top_n OR in_diversified_top_n).
    seed_full(prod, "AAA", setup_score=90, screening_score=90,
              sector="S", industry="I")
    seed_full(prod, "BBB", setup_score=80, screening_score=80,
              sector="S", industry="I")
    cfg = hard_cap_config(top_n=2, max_sector_count=1, max_industry_count=5)
    Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    rows = {r["ticker"]: r for r in fetch_proposals(prod)}
    # BBB raw_rank 2 <= top_n 2 -> in_raw_top_n True, but div NULL (rejected).
    assert rows["BBB"]["in_raw_top_n"] is True
    assert rows["BBB"]["in_diversified_top_n"] is False
    assert rows["BBB"]["selected_top_n"] is True
    assert rows["BBB"]["selected_flag"] is False


# --------------------------------------------------------------------------- #
# Append-only / id / preservation.
# --------------------------------------------------------------------------- #
def test_append_only_reruns(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    eng = Step5ProposalEngine()
    eng.propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID, run_id="run-1")
    eng.propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID, run_id="run-2")
    rows = fetch_proposals(prod)
    assert len(rows) == 2
    assert {r["run_id"] for r in rows} == {"run-1", "run-2"}


def test_proposal_ids_unique_and_valid(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    seed_full(prod, "BBB", setup_score=60, screening_score=50)
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    ids = [r["proposal_id"] for r in fetch_proposals(prod)]
    assert len(ids) == len(set(ids))
    for pid in ids:
        uuid.UUID(pid)


def test_candidate_and_analysis_preserved(tmp_db_paths: dict[str, Path]) -> None:
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    cid, aid = seed_full(prod, "AAA", setup_score=80, screening_score=70)
    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    row = fetch_proposals(prod)[0]
    # candidate_id and analysis_id are preserved in the explanation payload.
    assert cid in row["mechanical_explanation"]
    assert aid in row["mechanical_explanation"]


# --------------------------------------------------------------------------- #
# Write ownership / rollback.
# --------------------------------------------------------------------------- #
def test_only_step5_proposals_written(tmp_db_paths: dict[str, Path]) -> None:
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    cid_b, aid_b = seed_full(prod, "BBB", setup_score=60, screening_score=50)

    # Baseline row counts of upstream tables.
    conn = duckdb.connect(str(prod), read_only=True)
    try:
        base_cand = conn.execute("SELECT COUNT(*) FROM step3_candidates").fetchone()[0]
        base_anal = conn.execute("SELECT COUNT(*) FROM step4_analysis").fetchone()[0]
        base_tick = conn.execute("SELECT COUNT(*) FROM ticker_master").fetchone()[0]
    finally:
        conn.close()

    Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)

    conn = duckdb.connect(str(prod), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM step3_candidates").fetchone()[0] == base_cand
        assert conn.execute("SELECT COUNT(*) FROM step4_analysis").fetchone()[0] == base_anal
        assert conn.execute("SELECT COUNT(*) FROM ticker_master").fetchone()[0] == base_tick
        assert conn.execute("SELECT COUNT(*) FROM step5_proposals").fetchone()[0] == 2
    finally:
        conn.close()


class _FailingConn:
    """Wraps a real connection but raises on the Nth proposal INSERT."""

    def __init__(self, real, fail_after: int) -> None:
        self._real = real
        self._fail_after = fail_after
        self._inserts = 0
        self.rolled_back = False

    def execute(self, sql, params=None):  # noqa: ANN001
        if "INSERT INTO step5_proposals" in sql:
            self._inserts += 1
            if self._inserts > self._fail_after:
                raise RuntimeError("boom")
        if sql == "ROLLBACK":
            self.rolled_back = True
        return self._real.execute(sql, params) if params is not None else self._real.execute(sql)

    def close(self) -> None:
        self._real.close()


def test_write_failure_rolls_back_no_rows(tmp_db_paths: dict[str, Path]) -> None:
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    seed_full(prod, "BBB", setup_score=60, screening_score=50)

    class FakeManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            real = duckdb.connect(str(prod), read_only=read_only)
            if read_only:
                return real
            return _FailingConn(real, fail_after=1)

    res = Step5ProposalEngine(db_manager=FakeManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == 0
    assert res.metadata["proposals_written"] == 0
    # analyses_read preserved on write failure.
    assert res.metadata["analyses_read"] == 2
    # Rollback => no partial rows survived.
    assert fetch_proposals(prod) == []


def test_read_failure_returns_failed(tmp_db_paths: dict[str, Path]) -> None:
    class FakeManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            raise RuntimeError("read boom")

    res = Step5ProposalEngine(db_manager=FakeManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["analyses_read"] == 0


# --------------------------------------------------------------------------- #
# Static scans on the engine source.
# --------------------------------------------------------------------------- #
def _engine_source() -> str:
    return Path(s5mod.__file__).read_text(encoding="utf-8")


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
        assert "UPDATE STEP5_PROPOSALS" not in upper
        assert "DELETE FROM" not in upper
        assert "INSERT INTO STEP3" not in upper
        assert "INSERT INTO STEP4" not in upper
        assert "INSERT INTO TICKER_" not in upper
        assert "INSERT INTO DAILY_" not in upper


def test_only_step5_proposals_insert() -> None:
    inserts = [
        s
        for s in _non_docstring_strings(_engine_source())
        if "INSERT INTO" in s.upper()
    ]
    assert inserts, "expected at least one INSERT statement"
    for s in inserts:
        assert "INSERT INTO step5_proposals" in s


def test_no_print_in_engine() -> None:
    tree = ast.parse(_engine_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_provider_or_network_imports() -> None:
    imported = _imported_module_names(_engine_source())
    assert "yfinance" not in imported
    assert "requests" not in imported
    assert "urllib" not in imported
    assert "socket" not in imported
    assert not any(
        m == "providers" or m.startswith("providers") for m in imported
    )
    for s in _non_docstring_strings(_engine_source()):
        low = s.lower()
        assert "yfinance" not in low
        assert "providers" not in low


# --------------------------------------------------------------------------- #
# Config-name normalisation (items 1 + 3a).
# --------------------------------------------------------------------------- #
def test_legacy_hard_cap_names_accepted(tmp_db_paths: dict[str, Path]) -> None:
    """sector_max_positions / industry_max_positions map to canonical names."""
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    cfg = {
        "diversification": {
            "hard_cap_enabled": True,
            "top_n": 3,
            "sector_max_positions": 2,       # legacy name
            "industry_max_positions": 1,     # legacy name
        }
    }
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["proposals_written"] == 1


def test_legacy_soft_penalty_names_accepted(tmp_db_paths: dict[str, Path]) -> None:
    """sector_penalty_factor / industry_penalty_factor map to canonical names."""
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    cfg = {
        "diversification": {
            "hard_cap_enabled": False,
            "top_n": 3,
            "sector_penalty_factor": 0.9,    # legacy name
            "industry_penalty_factor": 0.85, # legacy name
        }
    }
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["proposals_written"] == 1


def test_canonical_names_still_accepted(tmp_db_paths: dict[str, Path]) -> None:
    """Canonical key names (max_sector_count etc.) continue to work unchanged."""
    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS


def test_normalise_block_pure_function() -> None:
    """_normalise_diversification_block does not mutate original and rewrites keys."""
    original = {
        "sector_max_positions": 3,
        "industry_max_positions": 2,
        "sector_penalty_factor": 0.9,
        "industry_penalty_factor": 0.85,
        "hard_cap_enabled": True,
        "top_n": 5,
    }
    original_copy = dict(original)
    result = s5mod._normalise_diversification_block(original)
    # Original untouched.
    assert original == original_copy
    # Legacy names rewritten.
    assert result["max_sector_count"] == 3
    assert result["max_industry_count"] == 2
    assert result["sector_penalty"] == 0.9
    assert result["industry_penalty"] == 0.85
    # Non-legacy keys preserved.
    assert result["hard_cap_enabled"] is True
    assert result["top_n"] == 5
    # Legacy names no longer present.
    assert "sector_max_positions" not in result
    assert "industry_max_positions" not in result
    assert "sector_penalty_factor" not in result
    assert "industry_penalty_factor" not in result


def test_legacy_invalid_value_still_rejected() -> None:
    """After normalisation, value validation still fires on the canonical key."""
    cfg = {
        "diversification": {
            "hard_cap_enabled": True,
            "top_n": 3,
            "sector_max_positions": 0,   # valid legacy name but invalid value
            "industry_max_positions": 1,
        }
    }
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED


# --------------------------------------------------------------------------- #
# _write rollback hardening (items 2 + 3b).
# --------------------------------------------------------------------------- #
class _RollbackFailingConn:
    """Raises on the INSERT that triggers failure AND on ROLLBACK.

    Used to verify that a ROLLBACK failure does not mask the original error.
    """

    def __init__(self, real, fail_on_insert: bool = True) -> None:
        self._real = real
        self._fail_on_insert = fail_on_insert
        self._inserts = 0

    def execute(self, sql, params=None):  # noqa: ANN001
        if "INSERT INTO step5_proposals" in sql and self._fail_on_insert:
            self._inserts += 1
            if self._inserts > 1:
                raise RuntimeError("insert boom")
        if sql == "ROLLBACK":
            raise RuntimeError("rollback boom")
        return self._real.execute(sql, params) if params is not None else self._real.execute(sql)

    def close(self) -> None:
        self._real.close()


def test_rollback_failure_does_not_mask_original_error(
    tmp_db_paths: dict[str, Path],
) -> None:
    """If ROLLBACK raises, the original write error is still propagated."""
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    seed_full(prod, "BBB", setup_score=60, screening_score=50)

    class FakeManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            real = duckdb.connect(str(prod), read_only=read_only)
            if read_only:
                return real
            return _RollbackFailingConn(real)

    res = Step5ProposalEngine(db_manager=FakeManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )
    # Result must be failed regardless of the ROLLBACK error.
    assert res.status == service_result.STATUS_FAILED
    # The error recorded is the original insert error, not the rollback error.
    assert any("insert boom" in e for e in res.errors)


def test_write_failure_rollback_flag_set(tmp_db_paths: dict[str, Path]) -> None:
    """The _FailingConn.rolled_back flag is True after a write failure."""
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    seed_full(prod, "BBB", setup_score=60, screening_score=50)

    captured: list[_FailingConn] = []

    class FakeManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            real = duckdb.connect(str(prod), read_only=read_only)
            if read_only:
                return real
            conn = _FailingConn(real, fail_after=1)
            captured.append(conn)
            return conn

    Step5ProposalEngine(db_manager=FakeManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )
    assert captured, "write connection was never opened"
    assert captured[0].rolled_back, "ROLLBACK was not called after insert failure"


# --------------------------------------------------------------------------- #
# Exact metadata keys on all return paths (item 3c).
# --------------------------------------------------------------------------- #
def test_metadata_keys_on_config_failure() -> None:
    cfg = {"diversification": {"hard_cap_enabled": True, "top_n": 0}}  # invalid top_n
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED
    assert set(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_on_read_failure() -> None:
    class BoomManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            raise RuntimeError("read boom")

    res = Step5ProposalEngine(db_manager=BoomManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert set(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_on_write_failure(tmp_db_paths: dict[str, Path]) -> None:
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    seed_full(prod, "BBB", setup_score=60, screening_score=50)

    class FakeManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            real = duckdb.connect(str(prod), read_only=read_only)
            if read_only:
                return real
            return _FailingConn(real, fail_after=0)

    res = Step5ProposalEngine(db_manager=FakeManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )
    assert res.status == service_result.STATUS_FAILED
    assert set(res.metadata) == REQUIRED_METADATA_KEYS


def test_metadata_keys_on_empty_path(tmp_db_paths: dict[str, Path]) -> None:
    # No rows seeded -> empty path through propose.
    res = Step5ProposalEngine().propose(SIGNAL_DATE, hard_cap_config(), CONFIG_ID)
    assert res.status == service_result.STATUS_SUCCESS
    assert set(res.metadata) == REQUIRED_METADATA_KEYS


# --------------------------------------------------------------------------- #
# No transaction when all rows are filtered as non-analyzable (item 3d).
# --------------------------------------------------------------------------- #
def test_no_write_transaction_when_all_non_analyzable(
    tmp_db_paths: dict[str, Path],
) -> None:
    """If all step4 rows are non-analyzable, _write receives [] and opens no transaction."""
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    # Both rows have NULL setup_score -> not analyzable -> rows==[] after _build_rows.
    seed_full(prod, "AAA", setup_score=None, screening_score=70)
    seed_full(prod, "BBB", setup_score=None, screening_score=50)

    transaction_opened: list[bool] = []

    class TrackingConn:
        def __init__(self, real) -> None:
            self._real = real

        def execute(self, sql, params=None):  # noqa: ANN001
            if "BEGIN TRANSACTION" in sql:
                transaction_opened.append(True)
            return self._real.execute(sql, params) if params is not None else self._real.execute(sql)

        def close(self) -> None:
            self._real.close()

    class FakeManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            real = duckdb.connect(str(prod), read_only=read_only)
            if read_only:
                return real
            return TrackingConn(real)

    res = Step5ProposalEngine(db_manager=FakeManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["analyses_read"] == 2
    assert res.metadata["proposals_written"] == 0
    assert not transaction_opened, "BEGIN TRANSACTION should not be called when rows==[]"
    assert fetch_proposals(prod) == []


# --------------------------------------------------------------------------- #
# Package-level import (item 5).
# --------------------------------------------------------------------------- #
def test_package_level_import() -> None:
    """Step5ProposalEngine is importable from the package root."""
    from app.services.proposal import Step5ProposalEngine as PackageImport

    assert PackageImport is Step5ProposalEngine


# --------------------------------------------------------------------------- #
# COMMIT-failure triggers rollback (item 1).
# --------------------------------------------------------------------------- #
class _CommitFailingConn:
    """Wraps a real connection but raises when COMMIT is executed.

    All INSERTs succeed so the partial writes exist inside the transaction
    at the moment COMMIT fails; the engine must issue ROLLBACK so they don't
    survive.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.rolled_back = False

    def execute(self, sql, params=None):  # noqa: ANN001
        if sql == "COMMIT":
            raise RuntimeError("commit boom")
        if sql == "ROLLBACK":
            self.rolled_back = True
        return self._real.execute(sql, params) if params is not None else self._real.execute(sql)

    def close(self) -> None:
        self._real.close()


def test_commit_failure_triggers_rollback_no_rows(
    tmp_db_paths: dict[str, Path],
) -> None:
    """A COMMIT failure causes rollback and returns failed with zero written rows."""
    import duckdb

    prod = tmp_db_paths[dbm.DB_ROLE_PROD]
    seed_full(prod, "AAA", setup_score=80, screening_score=70)
    seed_full(prod, "BBB", setup_score=60, screening_score=50)

    captured: list[_CommitFailingConn] = []

    class FakeManager:
        def connect(self, db_role, read_only=False):  # noqa: ANN001
            real = duckdb.connect(str(prod), read_only=read_only)
            if read_only:
                return real
            conn = _CommitFailingConn(real)
            captured.append(conn)
            return conn

    res = Step5ProposalEngine(db_manager=FakeManager()).propose(
        SIGNAL_DATE, hard_cap_config(), CONFIG_ID
    )

    assert res.status == service_result.STATUS_FAILED
    assert res.rows_processed == 0
    assert res.metadata["proposals_written"] == 0
    # analyses_read preserved even on write failure.
    assert res.metadata["analyses_read"] == 2
    # ROLLBACK was called.
    assert captured, "write connection never opened"
    assert captured[0].rolled_back, "ROLLBACK not called after COMMIT failure"
    # No rows survived in the database.
    assert fetch_proposals(prod) == []


# --------------------------------------------------------------------------- #
# Duplicate canonical + legacy key detection (item 2).
# --------------------------------------------------------------------------- #
def test_both_canonical_and_legacy_hard_cap_key_rejected() -> None:
    """Supplying both sector_max_positions and max_sector_count raises _ConfigError."""
    cfg = {
        "diversification": {
            "hard_cap_enabled": True,
            "top_n": 3,
            "sector_max_positions": 2,   # legacy
            "max_sector_count": 2,       # canonical — duplicate
            "industry_max_positions": 1,
        }
    }
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["analyses_read"] == 0  # rejected before DB access


def test_both_canonical_and_legacy_soft_penalty_key_rejected() -> None:
    """Supplying both sector_penalty_factor and sector_penalty raises _ConfigError."""
    cfg = {
        "diversification": {
            "hard_cap_enabled": False,
            "top_n": 3,
            "sector_penalty_factor": 0.9,  # legacy
            "sector_penalty": 0.9,         # canonical — duplicate
            "industry_penalty_factor": 0.85,
        }
    }
    res = Step5ProposalEngine().propose(SIGNAL_DATE, cfg, CONFIG_ID)
    assert res.status == service_result.STATUS_FAILED
    assert res.metadata["analyses_read"] == 0


def test_normalise_block_raises_on_duplicate_keys() -> None:
    """_normalise_diversification_block raises _ConfigError on ambiguous duplicates."""
    import pytest as _pytest

    block = {"sector_max_positions": 2, "max_sector_count": 3}
    with _pytest.raises(s5mod._ConfigError, match="ambiguity"):
        s5mod._normalise_diversification_block(block)


def test_normalise_block_no_false_positive_unrelated_keys() -> None:
    """A block with only canonical or only legacy names passes without error."""
    # only canonical
    s5mod._normalise_diversification_block(
        {"max_sector_count": 2, "max_industry_count": 1, "hard_cap_enabled": True}
    )
    # only legacy
    s5mod._normalise_diversification_block(
        {"sector_max_positions": 2, "industry_max_positions": 1}
    )
