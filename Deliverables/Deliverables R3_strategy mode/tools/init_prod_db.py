"""Create / apply the production schema to ``data/duckdb/prod.duckdb``.

Thin operator wrapper around the Module 03 schema manager. It performs no DDL
itself: it delegates to ``app.database.schema_manager.apply_prod_schema()``,
which routes every connection through the approved
``app.database.duckdb_manager`` and creates the parent directory as needed. The
schema manager is idempotent (M02 §6), so running this twice is safe.

Exit code: ``0`` on a ``success`` / ``success_with_warnings`` result, ``1``
otherwise (or on an unexpected exception).

Usage::

    python tools/init_prod_db.py
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._bootstrap import ensure_repo_root_on_path

logger = logging.getLogger("tools.init_prod_db")


def _apply_prod_schema() -> Any:
    """Invoke the Module 03 prod-schema entry point.

    Isolated behind a function so offline tests can monkeypatch it without
    importing the real schema manager / DuckDB.
    """
    ensure_repo_root_on_path()
    from app.database import schema_manager  # lazy import

    return schema_manager.apply_prod_schema()


def _seed_defaults() -> Any:
    """Seed default strategy/runtime/sector-alias configs into the prod DB.

    Isolated behind a function so offline tests can monkeypatch it. Runs only
    after the schema has been applied (M21 Config Management Addendum §12).
    """
    ensure_repo_root_on_path()
    from app.services.config.config_service import ConfigService  # lazy import

    return ConfigService().seed_defaults("prod")


def _resolve_prod_path() -> str:
    """Return the configured prod DB path for the success message (best effort)."""
    try:
        ensure_repo_root_on_path()
        from app.database import duckdb_manager

        return str(duckdb_manager.get_database_path("prod"))
    except Exception:  # noqa: BLE001 - cosmetic only
        return "data/duckdb/prod.duckdb"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="init_prod_db",
        description="Create/apply the production schema to data/duckdb/prod.duckdb.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Apply the prod schema and report. Returns a process exit code."""
    _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        result = _apply_prod_schema()
    except Exception as exc:  # noqa: BLE001 - operator script reports any failure
        print(f"FAILURE: prod schema init raised an exception: {exc}")
        logger.exception("prod schema init failed")
        return 1

    is_ok = result.is_ok() if hasattr(result, "is_ok") else (
        getattr(result, "status", "failed") in ("success", "success_with_warnings")
    )
    metadata = getattr(result, "metadata", {}) or {}
    status = getattr(result, "status", "failed")

    if is_ok:
        tables = metadata.get("tables_created")
        n_tables = len(tables) if isinstance(tables, list) else tables
        try:
            seed = _seed_defaults()
        except Exception as exc:  # noqa: BLE001 - report seeding failure
            print(f"FAILURE: prod config seeding raised an exception: {exc}")
            logger.exception("prod config seeding failed")
            return 1
        seed_ok = seed.is_ok() if hasattr(seed, "is_ok") else (
            getattr(seed, "status", "failed") in ("success", "success_with_warnings")
        )
        if not seed_ok:
            print(
                f"FAILURE: prod schema applied but config seeding failed; "
                f"errors={getattr(seed, 'errors', [])}"
            )
            return 1
        seed_meta = getattr(seed, "metadata", {}) or {}
        print(
            f"SUCCESS: prod schema applied to {_resolve_prod_path()} "
            f"(status={status}, tables={n_tables}, "
            f"version={metadata.get('schema_version', 'schema_v01')}, "
            f"newly_seeded={metadata.get('seed_row_inserted')}, "
            f"strategy_seeded={seed_meta.get('strategy_seeded')}, "
            f"runtime_seeded={seed_meta.get('runtime_seeded')}, "
            f"sector_alias_seeded={seed_meta.get('sector_alias_seeded')})."
        )
        return 0

    print(
        f"FAILURE: prod schema init returned status={status}; "
        f"errors={getattr(result, 'errors', [])}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
