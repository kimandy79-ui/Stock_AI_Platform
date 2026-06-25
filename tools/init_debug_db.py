"""Create / apply setup-mode schema to data/duckdb/debug.duckdb.

Thin wrapper around schema_manager.apply_debug_schema() + ConfigService seeding.
Idempotent. Exit code: 0 on success, 1 otherwise.

Usage::
    python tools/init_debug_db.py
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

logger = logging.getLogger("tools.init_debug_db")


def _apply_debug_schema() -> Any:
    ensure_repo_root_on_path()
    from app.database import schema_manager
    return schema_manager.apply_debug_schema()


def _seed_defaults() -> Any:
    ensure_repo_root_on_path()
    from app.services.config.config_service import ConfigService
    return ConfigService().seed_defaults("debug")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="init_debug_db",
        description="Create/apply setup-mode schema to data/duckdb/debug.duckdb.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        result = _apply_debug_schema()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILURE: debug schema init raised: {exc}")
        return 1

    is_ok = result.is_ok() if hasattr(result, "is_ok") else (
        getattr(result, "status", "failed") in ("success", "success_with_warnings")
    )
    if not is_ok:
        print(f"FAILURE: status={getattr(result, 'status', 'failed')} errors={getattr(result, 'errors', [])}")
        return 1

    try:
        seed = _seed_defaults()
    except Exception as exc:  # noqa: BLE001
        print(f"FAILURE: debug config seeding raised: {exc}")
        return 1
    seed_ok = seed.is_ok() if hasattr(seed, "is_ok") else False
    if not seed_ok:
        print(f"FAILURE: debug seeding failed errors={getattr(seed, 'errors', [])}")
        return 1

    meta = getattr(result, "metadata", {}) or {}
    seed_meta = getattr(seed, "metadata", {}) or {}
    print(
        f"SUCCESS: debug schema applied "
        f"(version={meta.get('schema_version', 'schema_v02')}, "
        f"setup_seeded={seed_meta.get('setup_seeded')}, "
        f"risk_label_seeded={seed_meta.get('risk_label_seeded')})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
