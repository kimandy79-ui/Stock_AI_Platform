"""Run a debug-mode preset against ``debug.duckdb`` via Module 22.

Thin operator wrapper around the frozen ``DebugModeController``. It owns no
pipeline logic and never selects a ``db_role``: the controller is hard-wired to
``debug`` and rejects any ``prod`` / ``simulation`` target itself (M22 §3), so
this runner can **never** write to ``prod.duckdb``. It only chooses a named
preset and forwards optional sampling overrides.

Before the controller starts, the runner ensures ``debug.duckdb`` exists with
the full schema applied (via the Module 03 schema manager, debug role only).
The initialization is self-sufficient (no separate setup step required) and
idempotent — it is skipped when ``debug.duckdb`` already exists.

Exit code: ``0`` on ``success`` / ``success_with_warnings``, ``1`` otherwise.

Usage::

    python tools/run_debug_pipeline.py --preset fast_smoke_test --date 2025-06-02
    python tools/run_debug_pipeline.py --preset pipeline_sanity --sample-count 50
    python tools/run_debug_pipeline.py --preset config_tuning_test \\
        --setups breakout pullback
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from typing import Any

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._bootstrap import ensure_repo_root_on_path

logger = logging.getLogger("tools.run_debug_pipeline")

# Mirror of Module 22 preset keys (kept here only for --help / choices; the
# controller remains the single source of truth and re-validates the name).
_PRESET_CHOICES = (
    "fast_smoke_test",
    "indicator_validation",
    "pipeline_sanity",
    "config_tuning_test",
)

# Canonical setup types (AD-22.20).
_SETUP_TYPE_CHOICES = (
    "breakout",
    "pullback",
    "trend_continuation",
    "consolidation_base",
)


def _ensure_debug_db() -> str | None:
    """Apply the debug schema and seed defaults if ``debug.duckdb`` is missing.

    Reuses the Module 03 schema manager (no duplicated SQL/DDL) and the
    ConfigService to seed setup config defaults. Targets the **debug** role
    only and never opens ``prod.duckdb``. Idempotent: returns early when the
    file already exists. Returns an error string on failure, or ``None`` on
    success. Isolated behind a function so offline tests can monkeypatch it.
    """
    ensure_repo_root_on_path()
    from app.database import duckdb_manager, schema_manager

    debug_path = duckdb_manager.get_database_path("debug")
    if debug_path.exists():
        return None

    print(f"debug.duckdb not found at {debug_path}; applying schema (debug role)...")
    result = schema_manager.apply_debug_schema()
    is_ok = result.is_ok() if hasattr(result, "is_ok") else (
        getattr(result, "status", "failed") in ("success", "success_with_warnings")
    )
    if not is_ok:
        return (
            f"debug schema init failed: status={getattr(result, 'status', 'failed')}; "
            f"errors={getattr(result, 'errors', [])}"
        )

    from app.services.config.config_service import ConfigService  # lazy

    seed = ConfigService().seed_defaults("debug")
    seed_ok = seed.is_ok() if hasattr(seed, "is_ok") else (
        getattr(seed, "status", "failed") in ("success", "success_with_warnings")
    )
    if not seed_ok:
        return (
            f"debug config seeding failed: "
            f"errors={getattr(seed, 'errors', [])}"
        )
    print(f"Initialized debug.duckdb at {debug_path} (setup configs seeded).")
    return None


def _build_controller() -> Any:
    """Construct the real Module 22 controller with default dependencies."""
    ensure_repo_root_on_path()
    from app.services.debug.debug_mode import DebugModeController
    return DebugModeController()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_debug_pipeline",
        description="Run a debug-mode preset (Module 22) against debug.duckdb ONLY.",
    )
    parser.add_argument(
        "--preset",
        choices=_PRESET_CHOICES,
        default="fast_smoke_test",
        help="Debug preset to run (default: fast_smoke_test).",
    )
    parser.add_argument(
        "--date",
        dest="run_date",
        type=date.fromisoformat,
        default=date.today(),
        help="Run date as YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--sample-count",
        type=int,
        default=None,
        help="Override the preset ticker sample count (1..500).",
    )
    parser.add_argument(
        "--setups",
        nargs="+",
        choices=_SETUP_TYPE_CHOICES,
        default=None,
        metavar="SETUP_TYPE",
        help=(
            "Override the active setup types for this run. "
            f"Choices: {', '.join(_SETUP_TYPE_CHOICES)}. "
            "Default: all four setup types."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    ensure_repo_root_on_path()
    from app.config import env
    env.load_environment()

    print(
        f"Starting debug pipeline: preset={args.preset} run_date={args.run_date} "
        f"sample_count={args.sample_count} setups={args.setups} db_role=debug"
    )

    try:
        init_error = _ensure_debug_db()
        if init_error is not None:
            print(f"FAILURE: {init_error}")
            return 1
        controller = _build_controller()
        result = controller.run_preset(
            args.preset,
            args.run_date,
            sample_count=args.sample_count,
            setup_types=args.setups,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAILURE: debug pipeline raised an exception: {exc}")
        logger.exception("debug pipeline failed")
        return 1

    is_ok = result.is_ok() if hasattr(result, "is_ok") else (
        getattr(result, "status", "failed") in ("success", "success_with_warnings")
    )
    metadata = getattr(result, "metadata", {}) or {}
    status = getattr(result, "status", "failed")
    debug_meta = metadata.get("debug", {}) if isinstance(metadata, dict) else {}

    if is_ok:
        print(
            f"SUCCESS: debug pipeline finished (status={status}, "
            f"preset={debug_meta.get('preset', args.preset)}, "
            f"db_role={debug_meta.get('db_role', 'debug')}, "
            f"setup_types={debug_meta.get('setup_types')}, "
            f"executed_steps={debug_meta.get('executed_steps')})."
        )
        if status == "success_with_warnings":
            for warn in getattr(result, "warnings", []) or []:
                print(f"  warning: {warn}")
        return 0

    print(
        f"FAILURE: debug pipeline status={status}; "
        f"errors={getattr(result, 'errors', [])}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
