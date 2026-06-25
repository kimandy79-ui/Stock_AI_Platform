"""Create / apply the simulation schema to ``data/duckdb/simulation.duckdb``.

Thin wrapper around ``app.database.schema_manager.apply_simulation_schema()``
followed by ``ConfigService().seed_defaults("simulation")  # setup_configs + risk_label_config + sector_alias`` (M21 Config
Management Addendum §12). Targets the **simulation** role only; never touches
``prod.duckdb`` or ``debug.duckdb``. Idempotent (M02 §6).

Exit code: ``0`` on success / success_with_warnings, ``1`` otherwise.

Usage::

    python tools/init_simulation_db.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._bootstrap import ensure_repo_root_on_path

logger = logging.getLogger("tools.init_simulation_db")


def _apply_simulation_schema() -> Any:
    """Invoke the Module 03 simulation-schema entry point (isolated for tests)."""
    ensure_repo_root_on_path()
    from app.database import schema_manager

    return schema_manager.apply_simulation_schema()


def _seed_defaults() -> Any:
    """Seed default strategy/runtime/sector-alias configs into the simulation DB."""
    ensure_repo_root_on_path()
    from app.services.config.config_service import ConfigService

    return ConfigService().seed_defaults("simulation")  # setup_configs + risk_label_config + sector_alias


def _resolve_simulation_path() -> str:
    try:
        ensure_repo_root_on_path()
        from app.database import duckdb_manager

        return str(duckdb_manager.get_database_path("simulation"))
    except Exception:  # noqa: BLE001 - cosmetic only
        return "data/duckdb/simulation.duckdb"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="init_simulation_db",
        description="Create/apply the simulation schema to data/duckdb/simulation.duckdb.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        result = _apply_simulation_schema()
    except Exception as exc:  # noqa: BLE001 - operator script reports any failure
        print(f"FAILURE: simulation schema init raised an exception: {exc}")
        logger.exception("simulation schema init failed")
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
            print(f"FAILURE: simulation config seeding raised an exception: {exc}")
            logger.exception("simulation config seeding failed")
            return 1
        seed_ok = seed.is_ok() if hasattr(seed, "is_ok") else (
            getattr(seed, "status", "failed") in ("success", "success_with_warnings")
        )
        if not seed_ok:
            print(
                f"FAILURE: simulation schema applied but config seeding failed; "
                f"errors={getattr(seed, 'errors', [])}"
            )
            return 1
        seed_meta = getattr(seed, "metadata", {}) or {}
        print(
            f"SUCCESS: simulation schema applied to {_resolve_simulation_path()} "
            f"(status={status}, tables={n_tables}, "
            f"version={metadata.get('schema_version', 'schema_v02')}, "
            f"newly_seeded={metadata.get('seed_row_inserted')}, "
            f"setup_seeded={seed_meta.get('setup_seeded')}, "
            f"risk_label_seeded={seed_meta.get('risk_label_seeded')}, "
            f"sector_alias_seeded={seed_meta.get('sector_alias_seeded')})."
        )
        return 0

    print(
        f"FAILURE: simulation schema init returned status={status}; "
        f"errors={getattr(result, 'errors', [])}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
