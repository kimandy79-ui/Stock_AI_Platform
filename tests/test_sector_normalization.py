"""Sector naming alignment tests (M21 Config Management Addendum §8).

Covers the pure ``normalize_sector`` mapping and an end-to-end check that the
universe ingestion writes canonical sectors into ``ticker_master`` /
``ticker_universe_snapshot`` against a real temp DuckDB.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.config import constants
from app.config import settings
from app.database import duckdb_manager as dbm
from app.database import schema_manager
from app.providers.provider_interface import TickerInfo
from app.services.universe.universe_snapshot import (
    UniverseSnapshotEngine,
    normalize_sector,
)


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("Technology", "Technology"),
        ("Financial Services", "Financials"),
        ("Financials", "Financials"),
        ("Healthcare", "Healthcare"),
        ("Health Care", "Healthcare"),
        ("Consumer Cyclical", "Consumer Discretionary"),
        ("Consumer Discretionary", "Consumer Discretionary"),
        ("Consumer Defensive", "Consumer Staples"),
        ("Consumer Staples", "Consumer Staples"),
        ("Communication Services", "Communication Services"),
        ("Industrials", "Industrials"),
        ("Energy", "Energy"),
        ("Basic Materials", "Materials"),
        ("Materials", "Materials"),
        ("Utilities", "Utilities"),
        ("Real Estate", "Real Estate"),
    ],
)
def test_yahoo_aliases_normalize_to_canonical(raw: str, canonical: str) -> None:
    assert normalize_sector(raw) == canonical


def test_normalize_is_case_insensitive() -> None:
    assert normalize_sector("financial services") == "Financials"
    assert normalize_sector("HEALTH CARE") == "Healthcare"


def test_normalize_passthrough_and_none() -> None:
    assert normalize_sector(None) is None
    assert normalize_sector("") == ""
    # Unknown sectors are preserved (not dropped) for later curation.
    assert normalize_sector("Frobnications") == "Frobnications"


def test_canonical_sectors_map_to_etf() -> None:
    # Every canonical sector has an ETF, and every ETF-map key is canonical.
    assert set(constants.SECTOR_ETF_MAP) == set(constants.CANONICAL_SECTORS)


def test_alias_targets_are_all_canonical() -> None:
    for canonical in constants.SECTOR_ALIAS_MAP.values():
        assert canonical in constants.CANONICAL_SECTORS


# --------------------------------------------------------------------------- #
# End-to-end: ingestion stores canonical sectors (real temp DuckDB).
# --------------------------------------------------------------------------- #
@pytest.fixture
def prod_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    duckdb_dir = tmp_path / "duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", duckdb_dir / "prod.duckdb")
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", duckdb_dir / "debug.duckdb")
    monkeypatch.setattr(
        settings, "SIMULATION_DB_PATH", duckdb_dir / "simulation.duckdb"
    )
    assert schema_manager.apply_prod_schema().is_ok()


def test_ingestion_canonicalizes_sector(prod_db: None) -> None:
    engine = UniverseSnapshotEngine(db_manager=dbm)
    entries = [
        TickerInfo(
            ticker="AAA",
            symbol_type=constants.SYMBOL_TYPE_STOCK,
            sector="Financial Services",
            industry="Banks",
            exchange="NASDAQ",
        ),
        TickerInfo(
            ticker="BBB",
            symbol_type=constants.SYMBOL_TYPE_STOCK,
            sector="Consumer Cyclical",
            industry="Retail",
            exchange="NYSE",
        ),
    ]
    result = engine.apply_snapshot(
        entries, as_of_date=date(2025, 6, 2), db_role="prod", source="test"
    )
    assert result.is_ok(), result.errors

    conn = dbm.connect("prod", read_only=True)
    try:
        master = dict(
            conn.execute("SELECT ticker, sector FROM ticker_master").fetchall()
        )
        snap = dict(
            conn.execute(
                "SELECT ticker, sector FROM ticker_universe_snapshot"
            ).fetchall()
        )
    finally:
        conn.close()

    assert master["AAA"] == "Financials"
    assert master["BBB"] == "Consumer Discretionary"
    assert snap["AAA"] == "Financials"
    assert snap["BBB"] == "Consumer Discretionary"
