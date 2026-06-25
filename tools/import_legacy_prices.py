"""
tools/import_legacy_prices.py
─────────────────────────────
Import ticker_master, ticker_universe_snapshot, and daily_prices from an old
strategy-mode DuckDB into a Phase-7 setup-mode database.

Usage:
    python tools/import_legacy_prices.py \\
        --source-db data/legacy_import/old_strategy_prod.duckdb \\
        --target-role prod \\
        [--dry-run | --execute]

Rules enforced:
  - Source DB is attached READ ONLY; never written.
  - Target role must be 'prod' or 'debug'; resolved from settings.
  - Source and target paths must differ (refused otherwise).
  - Default mode is --dry-run; --execute is required for actual writes.
  - Only ticker_master, ticker_universe_snapshot, daily_prices are imported
    when their schema is compatible with the Phase-7 target schema.
  - features, regime, step3/4/5, proposals, outcomes, pipeline tables, and
    configs are never imported.
  - Duplicate rows (same primary key) are skipped; counts reported.
  - All output goes to stdout/stderr via logging; no print() in library code.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# ── Constants ────────────────────────────────────────────────────────────────

TOOL_VERSION: Final[str] = "1.0.0"

# Tables permitted for import and their primary-key column(s).
IMPORTABLE_TABLES: Final[dict[str, list[str]]] = {
    "ticker_master": ["ticker"],
    "ticker_universe_snapshot": ["snapshot_month", "ticker"],
    "daily_prices": ["ticker", "date"],
}

# Columns that must be present in the SOURCE table for us to consider it
# schema-compatible.  We do NOT require every Phase-7 column to exist in the
# legacy DB — extra target columns are filled with NULL/defaults on INSERT.
REQUIRED_SOURCE_COLUMNS: Final[dict[str, set[str]]] = {
    "ticker_master": {
        "ticker", "symbol_type", "active_flag", "delisted_flag",
    },
    "ticker_universe_snapshot": {
        "snapshot_month", "ticker", "active_flag", "source", "created_at",
    },
    "daily_prices": {
        "ticker", "date",
        "open_raw", "high_raw", "low_raw", "close_raw", "volume_raw",
        "open_adj", "high_adj", "low_adj", "close_adj",
        "source_provider", "data_quality_status",
    },
}

# Columns that must NOT appear on import (strategy-mode artefacts).
BLOCKED_SOURCE_COLUMNS: Final[dict[str, set[str]]] = {
    "ticker_master": {"strategy_config_id"},
    "ticker_universe_snapshot": {"strategy_config_id"},
    "daily_prices": {"strategy_config_id"},
}

# Never touch these tables, even if they exist in the source DB.
FORBIDDEN_TABLES: Final[frozenset[str]] = frozenset({
    "daily_features", "daily_features_current",
    "market_regime",
    "step3_candidates", "step4_analysis", "step5_proposals",
    "signal_outcomes", "outcome_tracking_queue",
    "pipeline_runs", "pipeline_locks",
    "setup_configs", "risk_label_config", "strategy_configs",
    "sim_runs", "sim_folds", "sim_step3_candidates",
    "sim_step4_analysis", "sim_step5_proposals", "sim_signal_outcomes",
    "sim_config_comparisons", "sim_ai_reviews",
    "ai_reviews", "execution_decisions",
    "feature_rebuild_log", "data_repair_queue",
})

# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    status: str = "success"           # success | failed | dry_run
    tables_attempted: list[str] = field(default_factory=list)
    tables_skipped: list[str] = field(default_factory=list)
    rows_inserted: dict[str, int] = field(default_factory=dict)
    rows_skipped_dup: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── Logging setup ─────────────────────────────────────────────────────────────

def _configure_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s | %(levelname)-8s | import_legacy_prices | %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)
    return logging.getLogger("import_legacy_prices")


# ── Path / role resolution ────────────────────────────────────────────────────

def _resolve_target_path(role: str) -> Path:
    """
    Resolve the Phase-7 target DB path from settings or environment.

    Priority:
      1. Environment variable PROD_DB_PATH / DEBUG_DB_PATH.
      2. Import from app.config.settings (if the package is on sys.path).
      3. Conventional default paths relative to CWD.
    """
    role_lower = role.lower()
    if role_lower not in ("prod", "debug"):
        raise ValueError(f"--target-role must be 'prod' or 'debug', got '{role}'")

    env_key = "PROD_DB_PATH" if role_lower == "prod" else "DEBUG_DB_PATH"
    if env_val := os.environ.get(env_key):
        return Path(env_val)

    try:
        # Attempt to import from the application package.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        if role_lower == "prod":
            from app.config.settings import PROD_DB_PATH  # type: ignore[import]
            return Path(PROD_DB_PATH)
        else:
            from app.config.settings import DEBUG_DB_PATH  # type: ignore[import]
            return Path(DEBUG_DB_PATH)
    except (ImportError, AttributeError):
        pass

    # Conventional fallback.
    default = (
        Path("data/duckdb/prod.duckdb")
        if role_lower == "prod"
        else Path("data/duckdb/debug.duckdb")
    )
    return default


# ── Schema-compatibility checks ───────────────────────────────────────────────

def _table_exists_in_source(con, table: str) -> bool:
    rows = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name = ?", [table]
    ).fetchone()
    return bool(rows and rows[0] > 0)


def _table_exists_in_target(con, table: str) -> bool:
    # information_schema.tables conflates attached schemas with 'main'.
    # duckdb_tables() exposes the actual database_name, letting us scope
    # the check to the target DB only (not the attached 'legacy' schema).
    # The target DB's database_name is its file stem (e.g. 'prod', 'debug').
    # We exclude the known attached alias 'legacy' to be safe.
    rows = con.execute(
        "SELECT count(*) FROM duckdb_tables() "
        "WHERE table_name = ? AND database_name != 'legacy'", [table]
    ).fetchone()
    return bool(rows and rows[0] > 0)


def _get_source_columns(con, table: str) -> set[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ?", [table]
    ).fetchall()
    return {r[0] for r in rows}


def _get_target_columns(con, table: str) -> set[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ?", [table]
    ).fetchall()
    return {r[0] for r in rows}


def _check_compatibility(
    src_con,
    tgt_con,
    table: str,
    log: logging.Logger,
) -> tuple[bool, list[str]]:
    """
    Return (compatible, list_of_issues).
    Compatible means: source has the required columns AND the target table
    exists in the Phase-7 DB.
    """
    issues: list[str] = []

    if not _table_exists_in_source(src_con, table):
        issues.append(f"Table '{table}' not found in source DB.")
        return False, issues

    if not _table_exists_in_target(tgt_con, table):
        issues.append(f"Table '{table}' not found in target DB (schema not initialised?).")
        return False, issues

    src_cols = _get_source_columns(src_con, table)
    required = REQUIRED_SOURCE_COLUMNS.get(table, set())
    missing = required - src_cols
    if missing:
        issues.append(
            f"Table '{table}': source is missing required columns: {sorted(missing)}"
        )

    blocked = BLOCKED_SOURCE_COLUMNS.get(table, set())
    present_blocked = blocked & src_cols
    if present_blocked:
        # Warn but do not abort — we simply exclude these columns from INSERT.
        log.warning(
            "Table '%s': source has strategy-mode columns that will be excluded: %s",
            table, sorted(present_blocked),
        )

    return len(issues) == 0, issues


# ── Core import logic ─────────────────────────────────────────────────────────

def _build_insert_columns(
    src_con,
    tgt_con,
    table: str,
) -> list[str]:
    """
    Return the sorted intersection of source and target columns, minus any
    blocked (strategy-mode) artefact columns.
    """
    src_cols = _get_source_columns(src_con, table)
    tgt_cols = _get_target_columns(tgt_con, table)
    blocked = BLOCKED_SOURCE_COLUMNS.get(table, set())
    usable = (src_cols & tgt_cols) - blocked
    return sorted(usable)


def _import_table(
    src_con,
    tgt_con,
    table: str,
    pk_cols: list[str],
    dry_run: bool,
    log: logging.Logger,
) -> tuple[int, int]:
    """
    Import rows from legacy.{table} → {table}, skipping PK conflicts.

    Returns (inserted, skipped_duplicate).
    """
    insert_cols = _build_insert_columns(src_con, tgt_con, table)
    if not insert_cols:
        log.warning("Table '%s': no overlapping columns found; skipping.", table)
        return 0, 0

    col_list = ", ".join(insert_cols)
    pk_condition = " AND ".join(
        f"t.{c} = s.{c}" for c in pk_cols
    )

    # Count rows to import.
    total_src: int = src_con.execute(f"SELECT count(*) FROM legacy.{table}").fetchone()[0]  # type: ignore[index]

    # Count duplicates that already exist in target.
    dup_sql = (
        f"SELECT count(*) FROM legacy.{table} s "
        f"WHERE EXISTS (SELECT 1 FROM {table} t WHERE {pk_condition})"
    )
    dup_count: int = tgt_con.execute(dup_sql).fetchone()[0]  # type: ignore[index]

    new_count = total_src - dup_count

    log.info(
        "Table '%s': source=%d  duplicates=%d  to_insert=%d",
        table, total_src, dup_count, new_count,
    )

    if dry_run:
        return new_count, dup_count

    if new_count == 0:
        return 0, dup_count

    insert_sql = (
        f"INSERT INTO {table} ({col_list}) "
        f"SELECT {col_list} FROM legacy.{table} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {pk_condition})"
    )
    tgt_con.execute(insert_sql)
    return new_count, dup_count


# ── Main orchestration ────────────────────────────────────────────────────────

def run_import(
    source_db: Path,
    target_db: Path,
    dry_run: bool,
    log: logging.Logger,
) -> ImportResult:
    result = ImportResult(
        status="dry_run" if dry_run else "success"
    )

    # Safety: refuse if source == target (resolved absolute paths).
    if source_db.resolve() == target_db.resolve():
        result.status = "failed"
        result.errors.append(
            f"Source and target paths resolve to the same file: {source_db.resolve()}"
        )
        return result

    if not source_db.exists():
        result.status = "failed"
        result.errors.append(f"Source DB not found: {source_db}")
        return result

    if not target_db.exists():
        result.status = "failed"
        result.errors.append(
            f"Target DB not found: {target_db}  "
            "(Run the Phase-7 schema initialiser first.)"
        )
        return result

    try:
        import duckdb  # lazy import — keeps the module importable without duckdb for unit tests
    except ImportError:
        result.status = "failed"
        result.errors.append("duckdb package not installed.")
        return result

    # Open target with write access; attach source read-only as 'legacy'.
    try:
        tgt_con = duckdb.connect(str(target_db))
    except Exception as exc:
        result.status = "failed"
        result.errors.append(f"Cannot open target DB: {exc}")
        return result

    try:
        tgt_con.execute(
            f"ATTACH '{source_db}' AS legacy (READ_ONLY)"
        )
    except Exception as exc:
        tgt_con.close()
        result.status = "failed"
        result.errors.append(f"Cannot attach source DB read-only: {exc}")
        return result

    # Use a separate read-only connection for source schema introspection.
    try:
        src_con = duckdb.connect(str(source_db), read_only=True)
    except Exception as exc:
        tgt_con.close()
        result.status = "failed"
        result.errors.append(f"Cannot open source DB for introspection: {exc}")
        return result

    try:
        for table, pk_cols in IMPORTABLE_TABLES.items():
            result.tables_attempted.append(table)
            compatible, issues = _check_compatibility(src_con, tgt_con, table, log)
            if not compatible:
                result.tables_skipped.append(table)
                for iss in issues:
                    result.warnings.append(f"[{table}] {iss}")
                log.warning("Table '%s' skipped: %s", table, "; ".join(issues))
                continue

            try:
                inserted, skipped = _import_table(
                    tgt_con, tgt_con, table, pk_cols, dry_run, log
                )
                result.rows_inserted[table] = inserted
                result.rows_skipped_dup[table] = skipped
            except Exception as exc:
                result.status = "failed"
                err = f"[{table}] Import error: {exc}"
                result.errors.append(err)
                log.error(err)
                # Continue with remaining tables so we report everything.

    finally:
        src_con.close()
        try:
            tgt_con.execute("DETACH legacy")
        except Exception:
            pass
        tgt_con.close()

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_legacy_prices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Import ticker_master, ticker_universe_snapshot, and daily_prices
            from an old strategy-mode DuckDB into a Phase-7 setup-mode DB.

            SAFETY:
              • Source DB is always attached READ ONLY.
              • Default mode is --dry-run.  Pass --execute for real writes.
              • Source and target paths must differ.
              • features, step3/4/5, proposals, outcomes, pipeline tables,
                and configs are NEVER imported.
        """),
    )
    parser.add_argument(
        "--source-db", required=True, type=Path,
        metavar="PATH",
        help="Path to the old strategy-mode DuckDB file.",
    )
    parser.add_argument(
        "--target-role", required=True, choices=["prod", "debug"],
        metavar="ROLE",
        help="Target role: 'prod' or 'debug'.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=True,
        help="Show what would be imported without writing (default).",
    )
    mode.add_argument(
        "--execute", dest="dry_run", action="store_false",
        help="Actually write rows to the target DB.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {TOOL_VERSION}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    log = _configure_logging(args.verbose)

    mode_label = "DRY-RUN" if args.dry_run else "EXECUTE"
    log.info("import_legacy_prices v%s — mode: %s", TOOL_VERSION, mode_label)

    # Resolve paths.
    source_db: Path = args.source_db
    try:
        target_db: Path = _resolve_target_path(args.target_role)
    except ValueError as exc:
        log.error("Invalid --target-role: %s", exc)
        return 1

    log.info("Source DB : %s", source_db.resolve() if source_db.exists() else source_db)
    log.info("Target DB : %s  (role=%s)", target_db, args.target_role)

    result = run_import(source_db, target_db, args.dry_run, log)

    # Summary report.
    log.info("─" * 60)
    log.info("RESULT: %s", result.status.upper())
    for table in result.tables_attempted:
        if table in result.tables_skipped:
            log.info("  %-30s  SKIPPED", table)
        else:
            inserted = result.rows_inserted.get(table, 0)
            dupes = result.rows_skipped_dup.get(table, 0)
            action = "would insert" if args.dry_run else "inserted"
            log.info(
                "  %-30s  %s=%d  duplicates_skipped=%d",
                table, action, inserted, dupes,
            )
    for w in result.warnings:
        log.warning("  WARN: %s", w)
    for e in result.errors:
        log.error("  ERR : %s", e)
    log.info("─" * 60)

    if result.status == "failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
