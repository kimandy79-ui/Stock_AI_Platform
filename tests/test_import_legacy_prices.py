"""
tests/test_import_legacy_prices.py
───────────────────────────────────
Unit + integration tests for tools/import_legacy_prices.py.

All tests are fully offline: no real DB files, no network.
DuckDB is used in-memory or via tmp_path for integration paths.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make the tools module importable without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.import_legacy_prices import (
    FORBIDDEN_TABLES,
    IMPORTABLE_TABLES,
    REQUIRED_SOURCE_COLUMNS,
    ImportResult,
    _build_parser,
    _resolve_target_path,
    main,
    run_import,
)

duckdb = pytest.importorskip("duckdb", reason="duckdb not installed")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

LEGACY_DDL = {
    "ticker_master": """
        CREATE TABLE ticker_master (
            ticker VARCHAR PRIMARY KEY,
            yahoo_symbol VARCHAR,
            company_name VARCHAR,
            exchange VARCHAR,
            sector VARCHAR,
            industry VARCHAR,
            security_type VARCHAR,
            symbol_type VARCHAR NOT NULL,
            active_flag BOOLEAN NOT NULL DEFAULT TRUE,
            delisted_flag BOOLEAN NOT NULL DEFAULT FALSE,
            first_seen DATE,
            last_seen DATE,
            last_updated TIMESTAMP
        )
    """,
    "ticker_universe_snapshot": """
        CREATE TABLE ticker_universe_snapshot (
            snapshot_month DATE NOT NULL,
            ticker VARCHAR NOT NULL,
            exchange VARCHAR,
            sector VARCHAR,
            industry VARCHAR,
            market_cap_bucket VARCHAR,
            active_flag BOOLEAN NOT NULL,
            source VARCHAR NOT NULL,
            created_at TIMESTAMP NOT NULL,
            PRIMARY KEY (snapshot_month, ticker)
        )
    """,
    "daily_prices": """
        CREATE TABLE daily_prices (
            ticker VARCHAR NOT NULL,
            date DATE NOT NULL,
            open_raw DOUBLE,
            high_raw DOUBLE,
            low_raw DOUBLE,
            close_raw DOUBLE,
            volume_raw BIGINT,
            open_adj DOUBLE,
            high_adj DOUBLE,
            low_adj DOUBLE,
            close_adj DOUBLE,
            volume_adj BIGINT,
            dividend_amount DOUBLE DEFAULT 0,
            split_ratio DOUBLE DEFAULT 1,
            adjustment_factor DOUBLE,
            source_provider VARCHAR NOT NULL,
            data_quality_status VARCHAR NOT NULL,
            mutation_flag BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP,
            PRIMARY KEY (ticker, date)
        )
    """,
}

PHASE7_DDL = {
    **LEGACY_DDL,  # Phase-7 target has the same schema for these tables.
}


def _make_legacy_db(path: Path) -> None:
    """Create a minimal legacy DB at path."""
    con = duckdb.connect(str(path))
    for ddl in LEGACY_DDL.values():
        con.execute(ddl)
    # Seed some rows.
    con.execute(
        "INSERT INTO ticker_master VALUES "
        "('AAPL', 'AAPL', 'Apple Inc', 'NASDAQ', 'Technology', 'Consumer Electronics', "
        "'Common Stock', 'stock', TRUE, FALSE, '2020-01-01', '2024-06-30', NOW())"
    )
    con.execute(
        "INSERT INTO ticker_universe_snapshot VALUES "
        "('2024-01-01', 'AAPL', 'NASDAQ', 'Technology', 'Consumer Electronics', "
        "'large', TRUE, 'nasdaq', NOW())"
    )
    con.execute(
        "INSERT INTO daily_prices VALUES "
        "('AAPL', '2024-01-02', 185.0, 188.0, 184.0, 187.0, 1000000, "
        "183.0, 186.0, 182.0, 185.0, 1000000, 0, 1, 1.0, 'yahoo', 'ok', FALSE, NOW(), NULL)"
    )
    con.close()


def _make_target_db(path: Path) -> None:
    """Create a minimal Phase-7 target DB at path."""
    con = duckdb.connect(str(path))
    for ddl in PHASE7_DDL.values():
        con.execute(ddl)
    con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_importable_tables_keys(self):
        assert set(IMPORTABLE_TABLES.keys()) == {
            "ticker_master", "ticker_universe_snapshot", "daily_prices"
        }

    def test_forbidden_tables_never_overlap_importable(self):
        overlap = FORBIDDEN_TABLES & set(IMPORTABLE_TABLES.keys())
        assert overlap == frozenset(), f"Overlap found: {overlap}"

    def test_features_in_forbidden(self):
        assert "daily_features" in FORBIDDEN_TABLES

    def test_step_tables_in_forbidden(self):
        for t in ("step3_candidates", "step4_analysis", "step5_proposals"):
            assert t in FORBIDDEN_TABLES

    def test_pipeline_tables_in_forbidden(self):
        for t in ("pipeline_runs", "pipeline_locks"):
            assert t in FORBIDDEN_TABLES

    def test_config_tables_in_forbidden(self):
        assert "setup_configs" in FORBIDDEN_TABLES
        assert "risk_label_config" in FORBIDDEN_TABLES
        assert "strategy_configs" in FORBIDDEN_TABLES

    def test_required_source_columns_cover_importable(self):
        for table in IMPORTABLE_TABLES:
            assert table in REQUIRED_SOURCE_COLUMNS, f"Missing required cols for {table}"

    def test_daily_prices_required_cols_include_raw_adj(self):
        cols = REQUIRED_SOURCE_COLUMNS["daily_prices"]
        for c in ("open_raw", "close_raw", "open_adj", "close_adj", "source_provider"):
            assert c in cols


# ─────────────────────────────────────────────────────────────────────────────
# CLI parser
# ─────────────────────────────────────────────────────────────────────────────

class TestCLIParser:
    def test_defaults_to_dry_run(self):
        parser = _build_parser()
        args = parser.parse_args(["--source-db", "foo.duckdb", "--target-role", "prod"])
        assert args.dry_run is True

    def test_execute_flag_disables_dry_run(self):
        parser = _build_parser()
        args = parser.parse_args([
            "--source-db", "foo.duckdb", "--target-role", "prod", "--execute"
        ])
        assert args.dry_run is False

    def test_dry_run_and_execute_are_mutually_exclusive(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--source-db", "foo.duckdb", "--target-role", "prod",
                "--dry-run", "--execute",
            ])

    def test_invalid_target_role_rejected(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--source-db", "foo.duckdb", "--target-role", "simulation"
            ])

    def test_source_db_required(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--target-role", "prod"])


# ─────────────────────────────────────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveTargetPath:
    def test_env_prod_overrides_default(self, monkeypatch, tmp_path):
        target = tmp_path / "custom_prod.duckdb"
        monkeypatch.setenv("PROD_DB_PATH", str(target))
        assert _resolve_target_path("prod") == target

    def test_env_debug_overrides_default(self, monkeypatch, tmp_path):
        target = tmp_path / "custom_debug.duckdb"
        monkeypatch.setenv("DEBUG_DB_PATH", str(target))
        assert _resolve_target_path("debug") == target

    def test_falls_back_to_conventional_prod(self, monkeypatch):
        monkeypatch.delenv("PROD_DB_PATH", raising=False)
        p = _resolve_target_path("prod")
        assert "prod.duckdb" in str(p)

    def test_falls_back_to_conventional_debug(self, monkeypatch):
        monkeypatch.delenv("DEBUG_DB_PATH", raising=False)
        p = _resolve_target_path("debug")
        assert "debug.duckdb" in str(p)

    def test_invalid_role_raises(self):
        with pytest.raises(ValueError, match="must be 'prod' or 'debug'"):
            _resolve_target_path("simulation")


# ─────────────────────────────────────────────────────────────────────────────
# run_import — error paths
# ─────────────────────────────────────────────────────────────────────────────

class TestRunImportErrorPaths:
    def test_same_source_and_target_refused(self, tmp_path):
        p = tmp_path / "same.duckdb"
        p.touch()
        log = MagicMock()
        result = run_import(p, p, dry_run=True, log=log)
        assert result.status == "failed"
        assert any("same file" in e for e in result.errors)

    def test_missing_source_refused(self, tmp_path):
        src = tmp_path / "nonexistent.duckdb"
        tgt = tmp_path / "target.duckdb"
        tgt.touch()
        log = MagicMock()
        result = run_import(src, tgt, dry_run=True, log=log)
        assert result.status == "failed"
        assert any("Source DB not found" in e for e in result.errors)

    def test_missing_target_refused(self, tmp_path):
        src = tmp_path / "source.duckdb"
        src.touch()
        tgt = tmp_path / "nonexistent_target.duckdb"
        log = MagicMock()
        result = run_import(src, tgt, dry_run=True, log=log)
        assert result.status == "failed"
        assert any("Target DB not found" in e for e in result.errors)


# ─────────────────────────────────────────────────────────────────────────────
# run_import — dry-run integration
# ─────────────────────────────────────────────────────────────────────────────

class TestRunImportDryRun:
    def test_dry_run_reports_would_insert_without_writing(self, tmp_path):
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "prod.duckdb"
        _make_legacy_db(src)
        _make_target_db(tgt)

        log = MagicMock()
        result = run_import(src, tgt, dry_run=True, log=log)

        assert result.status == "dry_run"
        assert result.rows_inserted.get("ticker_master", 0) == 1
        assert result.rows_inserted.get("ticker_universe_snapshot", 0) == 1
        assert result.rows_inserted.get("daily_prices", 0) == 1
        assert not result.errors

        # Verify nothing was actually written.
        con = duckdb.connect(str(tgt))
        count = con.execute("SELECT count(*) FROM ticker_master").fetchone()[0]
        con.close()
        assert count == 0

    def test_dry_run_skips_table_missing_from_source(self, tmp_path):
        src = tmp_path / "legacy_partial.duckdb"
        tgt = tmp_path / "prod.duckdb"
        # Create source WITHOUT ticker_universe_snapshot.
        con = duckdb.connect(str(src))
        con.execute(LEGACY_DDL["ticker_master"])
        con.execute(LEGACY_DDL["daily_prices"])
        con.close()
        _make_target_db(tgt)

        log = MagicMock()
        result = run_import(src, tgt, dry_run=True, log=log)

        assert "ticker_universe_snapshot" in result.tables_skipped
        assert result.status == "dry_run"


# ─────────────────────────────────────────────────────────────────────────────
# run_import — execute integration
# ─────────────────────────────────────────────────────────────────────────────

class TestRunImportExecute:
    def test_execute_writes_rows(self, tmp_path):
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "prod.duckdb"
        _make_legacy_db(src)
        _make_target_db(tgt)

        log = MagicMock()
        result = run_import(src, tgt, dry_run=False, log=log)

        assert result.status == "success"
        assert not result.errors

        con = duckdb.connect(str(tgt))
        assert con.execute("SELECT count(*) FROM ticker_master").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM ticker_universe_snapshot").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM daily_prices").fetchone()[0] == 1
        con.close()

    def test_execute_skips_duplicate_rows(self, tmp_path):
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "prod.duckdb"
        _make_legacy_db(src)
        _make_target_db(tgt)

        # Pre-seed target with the same AAPL ticker_master row.
        con = duckdb.connect(str(tgt))
        con.execute(
            "INSERT INTO ticker_master VALUES "
            "('AAPL', 'AAPL', 'Apple Inc', 'NASDAQ', 'Technology', 'Consumer Electronics', "
            "'Common Stock', 'stock', TRUE, FALSE, '2020-01-01', '2024-06-30', NOW())"
        )
        con.close()

        log = MagicMock()
        result = run_import(src, tgt, dry_run=False, log=log)

        assert result.status == "success"
        assert result.rows_skipped_dup.get("ticker_master", 0) == 1
        assert result.rows_inserted.get("ticker_master", 0) == 0

        # Row count must remain 1, not 2.
        con = duckdb.connect(str(tgt))
        assert con.execute("SELECT count(*) FROM ticker_master").fetchone()[0] == 1
        con.close()

    def test_execute_multiple_new_rows(self, tmp_path):
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "prod.duckdb"
        _make_legacy_db(src)
        _make_target_db(tgt)

        # Add a second ticker to source.
        con_src = duckdb.connect(str(src))
        con_src.execute(
            "INSERT INTO ticker_master VALUES "
            "('MSFT', 'MSFT', 'Microsoft Corp', 'NASDAQ', 'Technology', 'Software', "
            "'Common Stock', 'stock', TRUE, FALSE, '2020-01-01', '2024-06-30', NOW())"
        )
        con_src.execute(
            "INSERT INTO daily_prices VALUES "
            "('MSFT', '2024-01-02', 370.0, 375.0, 368.0, 373.0, 800000, "
            "368.0, 373.0, 366.0, 371.0, 800000, 0, 1, 1.0, 'yahoo', 'ok', FALSE, NOW(), NULL)"
        )
        con_src.close()

        log = MagicMock()
        result = run_import(src, tgt, dry_run=False, log=log)

        assert result.rows_inserted.get("ticker_master", 0) == 2
        assert result.rows_inserted.get("daily_prices", 0) == 2

    def test_target_missing_table_skipped_gracefully(self, tmp_path):
        """If target schema is not fully initialised, skip the missing table."""
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "partial_target.duckdb"
        _make_legacy_db(src)

        # Create target with only ticker_master.
        con = duckdb.connect(str(tgt))
        con.execute(PHASE7_DDL["ticker_master"])
        con.close()

        log = MagicMock()
        result = run_import(src, tgt, dry_run=False, log=log)

        assert "ticker_universe_snapshot" in result.tables_skipped
        assert "daily_prices" in result.tables_skipped
        # ticker_master should still be imported.
        assert result.rows_inserted.get("ticker_master", 0) == 1

    def test_forbidden_tables_never_created_in_target(self, tmp_path):
        """Forbidden tables must not appear in the target after import."""
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "prod.duckdb"
        _make_legacy_db(src)
        _make_target_db(tgt)

        # Add a forbidden table to source.
        con_src = duckdb.connect(str(src))
        con_src.execute("CREATE TABLE step3_candidates (id VARCHAR PRIMARY KEY, data TEXT)")
        con_src.execute("INSERT INTO step3_candidates VALUES ('x', 'bad')")
        con_src.close()

        log = MagicMock()
        run_import(src, tgt, dry_run=False, log=log)

        con_tgt = duckdb.connect(str(tgt), read_only=True)
        tables = {
            r[0]
            for r in con_tgt.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        con_tgt.close()
        assert "step3_candidates" not in tables


# ─────────────────────────────────────────────────────────────────────────────
# main() exit codes
# ─────────────────────────────────────────────────────────────────────────────

class TestMainExitCodes:
    def test_main_returns_1_for_missing_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROD_DB_PATH", str(tmp_path / "prod.duckdb"))
        (tmp_path / "prod.duckdb").touch()
        code = main([
            "--source-db", str(tmp_path / "nope.duckdb"),
            "--target-role", "prod",
            "--dry-run",
        ])
        assert code == 1

    def test_main_returns_0_for_successful_dry_run(self, tmp_path, monkeypatch):
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "prod.duckdb"
        _make_legacy_db(src)
        _make_target_db(tgt)
        monkeypatch.setenv("PROD_DB_PATH", str(tgt))

        code = main([
            "--source-db", str(src),
            "--target-role", "prod",
            "--dry-run",
        ])
        assert code == 0

    def test_main_returns_0_for_successful_execute(self, tmp_path, monkeypatch):
        src = tmp_path / "legacy.duckdb"
        tgt = tmp_path / "prod.duckdb"
        _make_legacy_db(src)
        _make_target_db(tgt)
        monkeypatch.setenv("PROD_DB_PATH", str(tgt))

        code = main([
            "--source-db", str(src),
            "--target-role", "prod",
            "--execute",
        ])
        assert code == 0

    def test_main_same_path_returns_1(self, tmp_path, monkeypatch):
        p = tmp_path / "same.duckdb"
        p.touch()
        monkeypatch.setenv("PROD_DB_PATH", str(p))

        code = main([
            "--source-db", str(p),
            "--target-role", "prod",
            "--dry-run",
        ])
        assert code == 1
