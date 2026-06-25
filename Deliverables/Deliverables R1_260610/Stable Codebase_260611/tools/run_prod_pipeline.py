"""Run one daily pipeline against ``prod.duckdb`` via Module 20.

Thin operator wrapper around the frozen ``PipelineOrchestrator``. It constructs
the orchestrator with its real injected defaults and calls ``run(...)`` with
``db_role="prod"``; it owns no pipeline logic. The orchestrator always returns
a ``ServiceResult`` and does not raise for expected lock / already-run / step
failures (M20 §1).

Exit code: ``0`` on ``success`` / ``success_with_warnings``, ``1`` otherwise.

Usage::

    python tools/run_prod_pipeline.py --date 2025-06-02 --run-type manual

Note (M20 G-UNIVERSE-PROVIDER): the default ``YahooProvider`` has no symbol
source, so ``universe_ingestion`` yields an empty universe with a warning. A
provider with a symbol source must be injected for real universe population;
that wiring is out of scope for this thin runner.
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

logger = logging.getLogger("tools.run_prod_pipeline")

_VALID_RUN_TYPES = ("scheduled", "manual", "force_rerun", "catchup")


def _build_orchestrator() -> Any:
    """Construct the real Module 20 orchestrator with default dependencies.

    Isolated so offline tests can monkeypatch it without importing the real
    engines / DuckDB.
    """
    ensure_repo_root_on_path()
    from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator

    return PipelineOrchestrator()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_prod_pipeline",
        description="Run the daily pipeline (Module 20) against prod.duckdb.",
    )
    parser.add_argument(
        "--date",
        dest="run_date",
        type=date.fromisoformat,
        default=date.today(),
        help="Run date as YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--run-type",
        choices=_VALID_RUN_TYPES,
        default="manual",
        help="Pipeline run_type (default: manual, for an operator launch).",
    )
    parser.add_argument(
        "--force-rerun",
        action="store_true",
        help="Re-run even if run_date already succeeded.",
    )
    parser.add_argument(
        "--resume-from",
        default=None,
        help="Optional step name to resume from (must be a valid STEP_NAME).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print(
        f"Starting prod pipeline: run_date={args.run_date} run_type={args.run_type} "
        f"force_rerun={args.force_rerun} resume_from={args.resume_from} db_role=prod"
    )

    try:
        orchestrator = _build_orchestrator()
        result = orchestrator.run(
            run_date=args.run_date,
            run_type=args.run_type,
            db_role="prod",
            force_rerun=args.force_rerun,
            resume_from=args.resume_from,
        )
    except Exception as exc:  # noqa: BLE001 - operator script reports any failure
        print(f"FAILURE: prod pipeline raised an exception: {exc}")
        logger.exception("prod pipeline failed")
        return 1

    is_ok = result.is_ok() if hasattr(result, "is_ok") else (
        getattr(result, "status", "failed") in ("success", "success_with_warnings")
    )
    metadata = getattr(result, "metadata", {}) or {}
    status = getattr(result, "status", "failed")
    steps = metadata.get("steps_completed", [])
    n_steps = len(steps) if isinstance(steps, list) else steps

    if is_ok:
        print(
            f"SUCCESS: prod pipeline finished (status={status}, "
            f"run_id={metadata.get('run_id')}, steps_completed={n_steps}, "
            f"duration_sec={metadata.get('duration_sec')})."
        )
        if status == "success_with_warnings":
            for warn in getattr(result, "warnings", []) or []:
                print(f"  warning: {warn}")
        return 0

    print(
        f"FAILURE: prod pipeline status={status}; "
        f"failed_step={metadata.get('failed_step')}; "
        f"errors={getattr(result, 'errors', [])}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
