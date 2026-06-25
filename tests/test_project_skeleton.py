"""Tests for Module 01 — Project Skeleton.

Covers, per CODING_STANDARDS.md section 12:
- unit tests (ServiceResult contract, constants, settings, env helpers)
- invalid input test (unknown strategy, bad env values)
- empty data test (default-constructed ServiceResult collections)
- expected output test (logging format, directory creation idempotency)

No database connections, provider calls, or trading logic are exercised here;
this module is structural scaffolding only.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from app.config import constants, env, settings
from app.utils import logging_config
from app.utils.service_result import (
    ALLOWED_STATUSES,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_SUCCESS_WITH_WARNINGS,
    ServiceResult,
)


# --------------------------------------------------------------------------- #
# ServiceResult contract
# --------------------------------------------------------------------------- #
class TestServiceResult:
    """Validate the exact ServiceResult contract from the shared context."""

    def test_field_names_and_order(self) -> None:
        fields = [f.name for f in dataclasses.fields(ServiceResult)]
        assert fields == [
            "status",
            "run_id",
            "rows_processed",
            "warnings",
            "errors",
            "metadata",
        ]

    def test_defaults_are_empty(self) -> None:
        result = ServiceResult(status=STATUS_SUCCESS, run_id="run-123")
        # Empty data test: default collections start empty, counters at zero.
        assert result.rows_processed == 0
        assert result.warnings == []
        assert result.errors == []
        assert result.metadata == {}

    def test_default_collections_are_independent(self) -> None:
        a = ServiceResult(status=STATUS_SUCCESS, run_id="a")
        b = ServiceResult(status=STATUS_SUCCESS, run_id="b")
        a.add_warning("w")
        # Mutating one instance must not leak into another (default_factory).
        assert a.warnings == ["w"]
        assert b.warnings == []

    def test_allowed_statuses(self) -> None:
        assert ALLOWED_STATUSES == frozenset(
            {STATUS_SUCCESS, STATUS_SUCCESS_WITH_WARNINGS, STATUS_FAILED}
        )

    def test_is_ok(self) -> None:
        assert ServiceResult(STATUS_SUCCESS, "r").is_ok() is True
        assert ServiceResult(STATUS_SUCCESS_WITH_WARNINGS, "r").is_ok() is True
        assert ServiceResult(STATUS_FAILED, "r").is_ok() is False

    def test_has_valid_status(self) -> None:
        assert ServiceResult(STATUS_SUCCESS, "r").has_valid_status() is True
        # Invalid input test: an unrecognized status is reported as invalid.
        assert ServiceResult("bogus", "r").has_valid_status() is False

    def test_add_helpers(self) -> None:
        result = ServiceResult(STATUS_SUCCESS, "r")
        result.add_warning("careful")
        result.add_error("boom")
        assert result.warnings == ["careful"]
        assert result.errors == ["boom"]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
class TestConstants:
    """Validate structural constants required by Module 01."""

    @pytest.mark.skip(
        reason="RETIRED (setup-mode AD-22.8/22.19): FEATURE_SCHEMA_VERSION bumped "
               "to 'features_v02'. Legacy 'features_v01' assertion no longer valid."
    )
    def test_feature_schema_version_exact(self) -> None:
        # Retired: value is now "features_v02" per AD-22.8/22.19.
        assert constants.FEATURE_SCHEMA_VERSION == "features_v01"

    def test_db_filenames(self) -> None:
        assert constants.PROD_DB_FILENAME == "prod.duckdb"
        assert constants.DEBUG_DB_FILENAME == "debug.duckdb"
        assert constants.SIMULATION_DB_FILENAME == "simulation.duckdb"

    def test_symbol_types(self) -> None:
        assert constants.ALLOWED_SYMBOL_TYPES == (
            "stock",
            "etf",
            "benchmark",
            "index",
        )

    def test_required_benchmarks_include_core_and_sectors(self) -> None:
        required = constants.REQUIRED_BENCHMARK_SYMBOLS
        assert "SPY" in required
        assert "QQQ" in required
        assert "^VIX" in required
        for etf in constants.SECTOR_ETFS:
            assert etf in required

    def test_sector_etf_map_count(self) -> None:
        # MASTER_SPEC.md section 10 lists 11 sector mappings.
        assert len(constants.SECTOR_ETF_MAP) == 11
        assert constants.SECTOR_ETF_MAP["Technology"] == "XLK"
        assert constants.SECTOR_ETF_MAP["Real Estate"] == "XLRE"

    def test_market_regime_priority_order(self) -> None:
        assert constants.MARKET_REGIME_PRIORITY == (
            "extreme_risk",
            "high_risk",
            "bear",
            "bull",
            "neutral",
        )

    @pytest.mark.skip(
        reason='RETIRED (setup-mode AD-22.20): ALLOWED_SETUP_TYPES now carries exactly 4 '
               'active values. SETUP_UNKNOWN and legacy six-value vocab are retired.'
    )
    def test_setup_types(self) -> None:
        # Retired legacy six-value enum.
        assert constants.SETUP_UNKNOWN in constants.ALLOWED_SETUP_TYPES
        assert "trend_resume" in constants.ALLOWED_SETUP_TYPES
        assert len(constants.ALLOWED_SETUP_TYPES) == 6

    def test_outcome_horizons(self) -> None:
        assert constants.OUTCOME_HORIZONS_BD == (5, 10, 20, 40)

    @pytest.mark.skip(
        reason='RETIRED (setup-mode AD-22.20): SCREENING_BLOCK_WEIGHTS removed; '
               'step3 universal eligibility no longer uses a block-weights score model.'
    )
    def test_screening_block_weights_sum_to_one(self) -> None:
        # Retired: block-weights concept replaced by setup routing.
        total = sum(constants.SCREENING_BLOCK_WEIGHTS.values())
        assert total == pytest.approx(1.0)

    def test_log_format_contains_run_id(self) -> None:
        # Format must follow: timestamp | level | module | run_id | message
        assert "run_id" in constants.LOG_FORMAT
        assert constants.LOG_FORMAT.count("|") == 4


# --------------------------------------------------------------------------- #
# Settings (paths + strategy presets)
# --------------------------------------------------------------------------- #
class TestSettings:
    """Validate path settings and immutable strategy presets."""

    def test_paths_are_pathlib(self) -> None:
        for path in (
            settings.PROJECT_ROOT,
            settings.DATA_DIR,
            settings.DUCKDB_DIR,
            settings.LOGS_DIR,
            settings.EXPORTS_DIR,
            settings.BACKUPS_DIR,
            settings.PROD_DB_PATH,
        ):
            assert isinstance(path, Path)

    def test_db_paths_under_duckdb_dir(self) -> None:
        assert settings.PROD_DB_PATH.parent == settings.DUCKDB_DIR
        assert settings.PROD_DB_PATH.name == "prod.duckdb"
        assert settings.SIMULATION_DB_PATH.name == "simulation.duckdb"

    def test_strategy_presets_present(self) -> None:
        assert set(settings.STRATEGY_PRESETS) == {
            "normal",
            "aggressive",
            "conservative",
        }

    def test_normal_preset_values(self) -> None:
        cfg = settings.get_strategy("normal")
        assert cfg.min_price == 10.0
        assert cfg.min_avg_dollar_volume_20d == 20_000_000.0
        assert cfg.min_rvol == 1.5
        assert cfg.min_screening_score == 65.0
        assert cfg.sector_max_positions == 3
        assert cfg.industry_max_positions == 2
        assert cfg.earnings_avoid_window_bd == 10

    def test_aggressive_and_conservative_values(self) -> None:
        agg = settings.get_strategy("aggressive")
        con = settings.get_strategy("conservative")
        assert agg.min_price == 5.0
        assert agg.min_rvol == 1.2
        assert con.min_price == 15.0
        assert con.min_screening_score == 75.0

    def test_strategy_config_is_immutable(self) -> None:
        cfg = settings.get_strategy("normal")
        # Config is immutable per CODING_STANDARDS.md section 10.
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.min_price = 1.0  # type: ignore[misc]

    def test_unknown_strategy_raises(self) -> None:
        # Invalid input test.
        with pytest.raises(KeyError):
            settings.get_strategy("does_not_exist")

    def test_diversification_defaults(self) -> None:
        div = settings.DIVERSIFICATION_DEFAULTS
        assert div.hard_cap_enabled is True
        assert div.sector_max_positions == 3
        assert div.industry_max_positions == 2
        assert div.sector_penalty_factor == pytest.approx(0.90)
        assert div.industry_penalty_factor == pytest.approx(0.85)

    def test_ensure_directories_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Redirect required directories into a temp location so the test does
        # not touch the real data/ tree.
        targets = (
            tmp_path / "data",
            tmp_path / "data" / "duckdb",
            tmp_path / "data" / "logs",
            tmp_path / "data" / "exports",
            tmp_path / "data" / "backups",
        )
        monkeypatch.setattr(settings, "REQUIRED_DIRECTORIES", targets, raising=True)
        # Calling twice must not raise (idempotency).
        settings.ensure_directories()
        settings.ensure_directories()
        for directory in targets:
            assert directory.is_dir()


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
class TestEnv:
    """Validate typed environment getters and dotenv loading behavior."""

    def test_get_str_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOME_MISSING_VAR", raising=False)
        assert env.get_str("SOME_MISSING_VAR", "fallback") == "fallback"

    def test_get_str_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_VAR", "hello")
        assert env.get_str("MY_VAR") == "hello"

    def test_get_int_valid_and_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_INT", "42")
        assert env.get_int("MY_INT", 0) == 42
        # Invalid input test: non-numeric falls back to default.
        monkeypatch.setenv("MY_INT", "notanint")
        assert env.get_int("MY_INT", 7) == 7

    def test_get_float_valid_and_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_FLOAT", "3.14")
        assert env.get_float("MY_FLOAT", 0.0) == pytest.approx(3.14)
        monkeypatch.setenv("MY_FLOAT", "")
        assert env.get_float("MY_FLOAT", 1.5) == pytest.approx(1.5)

    def test_get_bool_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for token in ("1", "true", "YES", "on"):
            monkeypatch.setenv("MY_BOOL", token)
            assert env.get_bool("MY_BOOL", False) is True
        for token in ("0", "false", "NO", "off"):
            monkeypatch.setenv("MY_BOOL", token)
            assert env.get_bool("MY_BOOL", True) is False
        # Unrecognized token falls back to default.
        monkeypatch.setenv("MY_BOOL", "maybe")
        assert env.get_bool("MY_BOOL", True) is True

    def test_get_path_relative_resolves_against_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_PATH", "some/dir")
        resolved = env.get_path("MY_PATH", Path("default"))
        assert resolved == env.PROJECT_ROOT / "some" / "dir"

    def test_get_path_absolute_preserved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MY_PATH", str(tmp_path))
        assert env.get_path("MY_PATH", Path("default")) == tmp_path

    def test_load_environment_missing_file(self, tmp_path: Path) -> None:
        # Missing .env is not an error; returns False.
        assert env.load_environment(tmp_path / "nope.env") is False
        assert env.is_loaded() is True


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class TestLogging:
    """Validate logging configuration and the run_id-aware adapter."""

    def test_configure_logging_returns_root(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        root = logging_config.configure_logging(log_file=log_file, force=True)
        assert isinstance(root, logging.Logger)
        assert logging_config.is_configured() is True

    def test_log_record_format_includes_run_id(self, tmp_path: Path) -> None:
        log_file = tmp_path / "format.log"
        logging_config.configure_logging(log_file=log_file, force=True)
        log = logging_config.get_logger("app.tests.format", run_id="run-xyz")
        log.info("hello world")
        for handler in logging.getLogger().handlers:
            handler.flush()
        contents = log_file.read_text(encoding="utf-8")
        # Format: timestamp | level | module | run_id | message
        assert "app.tests.format" in contents
        assert "run-xyz" in contents
        assert "hello world" in contents
        assert contents.count("|") >= 4

    def test_direct_logger_gets_default_run_id(self, tmp_path: Path) -> None:
        log_file = tmp_path / "default.log"
        logging_config.configure_logging(log_file=log_file, force=True)
        # A plain logger call (no adapter) must still format without KeyError.
        logging.getLogger("app.tests.direct").warning("no adapter here")
        for handler in logging.getLogger().handlers:
            handler.flush()
        contents = log_file.read_text(encoding="utf-8")
        assert "no adapter here" in contents
        assert constants.DEFAULT_RUN_ID in contents

    def test_get_logger_returns_adapter(self) -> None:
        log = logging_config.get_logger("app.tests.adapter")
        assert isinstance(log, logging_config.RunIdLoggerAdapter)


# --------------------------------------------------------------------------- #
# Module-01 scope guard (negative assertions)
# --------------------------------------------------------------------------- #
class TestScopeGuards:
    """Ensure Module 01 stays in scope: no DB/provider/trading logic leaks."""

    def test_no_duckdb_import_in_config_or_utils(self) -> None:
        # Module 01 must not open or import database connectivity. We scan the
        # source text rather than importing duckdb to keep the test light.
        roots = [settings.APP_DIR / "config", settings.APP_DIR / "utils"]
        offenders: list[str] = []
        for root in roots:
            for py_file in root.glob("*.py"):
                text = py_file.read_text(encoding="utf-8")
                if "import duckdb" in text or "duckdb.connect" in text:
                    offenders.append(str(py_file))
        assert offenders == [], f"Unexpected DB usage in Module 01: {offenders}"

    def test_no_provider_calls_in_config_or_utils(self) -> None:
        roots = [settings.APP_DIR / "config", settings.APP_DIR / "utils"]
        offenders: list[str] = []
        for root in roots:
            for py_file in root.glob("*.py"):
                text = py_file.read_text(encoding="utf-8")
                if "import yfinance" in text:
                    offenders.append(str(py_file))
        assert offenders == [], f"Unexpected provider usage in Module 01: {offenders}"
