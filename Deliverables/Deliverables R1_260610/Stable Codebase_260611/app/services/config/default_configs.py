"""Default config seed payloads for fresh DB initialization.

These are the **seed defaults** referenced by the M21 Config Management
Addendum §5. They are plain data (no I/O) and may remain in Python permanently
as seed values; runtime code must load the *active* config from the DB through
:class:`app.services.config.config_service.ConfigService`, not from here.

Strategy-config seeds are sourced from the canonical
``pipeline_orchestrator.DEFAULT_STRATEGY_CONFIGS`` via :func:`get_default_strategy_configs`
(a lazy import to avoid an import cycle), so there is exactly one strategy-config
source of truth.

Sector seeds (``sector_alias_map``) come from ``constants.SECTOR_ALIAS_MAP`` so
there is exactly one sector-normalization source of truth.
"""

from __future__ import annotations

from typing import Any, Final

from app.config import constants

# Default config version label applied to every seeded config row.
SEED_VERSION: Final[str] = "v1"

# Provenance written to ``created_by`` on seeded rows.
SEED_CREATED_BY: Final[str] = "system_seed"


def get_default_strategy_configs() -> dict[str, dict[str, Any]]:
    """Return the canonical default strategy configs (lazy import).

    Imported lazily from the pipeline orchestrator so this module has no
    import-time dependency on the service layer (and to keep
    ``DEFAULT_STRATEGY_CONFIGS`` as the single strategy-config source).
    """
    from app.services.pipeline.pipeline_orchestrator import (
        DEFAULT_STRATEGY_CONFIGS,
    )

    # Return a deep-ish copy so callers cannot mutate the module constant.
    return {name: dict(cfg) for name, cfg in DEFAULT_STRATEGY_CONFIGS.items()}


# --------------------------------------------------------------------------- #
# Runtime config defaults (addendum §5.2). Architecture/safety values that must
# stay hardcoded are NOT placed here; only runtime/tunable settings are.
# --------------------------------------------------------------------------- #
DEFAULT_RUNTIME_CONFIGS: Final[dict[str, dict[str, Any]]] = {
    "pipeline": {
        # NOTE: runtime configs are reference/visibility-only at this stage.
        # Runtime code reads the hardcoded constants (e.g. LOCK_STALE_SECONDS=300
        # in pipeline_orchestrator.py). Seed values here intentionally match the
        # hardcoded values so there is no conflict; wiring consumers to read from
        # DB is follow-up work.
        "default_run_type": "scheduled",
        "lock_stale_seconds": 300,          # matches LOCK_STALE_SECONDS constant
        "force_rerun_default": False,
        "resume_behavior": "resume_from_step",
        "critical_step_groups": [
            "benchmark_etf_ingestion",
            "universe_ingestion",
            "price_ingestion",
            "validation",
            "feature_calculation",
        ],
        "recoverable_step_groups": [
            "mutation_detection",
            "dashboard_materialization",
            "backup",
        ],
        "strategy_step_groups": [
            "step3_screening",
            "step4_analysis",
            "step5_proposals",
            "outcome_queue_creation",
            "outcome_processing",
        ],
    },
    "provider": {
        "provider_name": "yahoo",
        "default_batch_size": 50,
        "retry_count": 3,
        "sleep_seconds": 1.0,
        "jitter_seconds": 0.5,
        "timeout_seconds": 30,
    },
    "data_completeness": {
        "target_historical_years": 3,
        "full_completeness_mode": "all_active_tickers",
        "sample_completeness_tickers": ["AAPL", "MSFT", "SPY"],
        "repair_max_attempts": 3,
        "max_auto_repair_gap_days": 5,
    },
    "debug": {
        "default_sample_count": 25,
        "max_sample_count": 100,
        "debug_presets": ["tiny", "small", "medium"],
        # Safety: forced role/run_type are recorded for visibility but remain
        # enforced in code (addendum §9). They are not user-editable switches.
        "forced_db_role": "debug",
        "forced_run_type": "debug",
    },
    "simulation": {
        "default_mode": "walk_forward",
        "min_resolved_outcomes_pct": 0.85,
        "max_drawdown_constraint_pct": 25,
    },
    "dashboard": {
        "default_db_role": "prod",
        "default_row_limit": 100,
        "default_diversified_display": True,
    },
    "ai_review": {
        "prompt_version": "v1",
        "max_tokens": 1500,
        "provider_mode": "manual",
    },
    "export": {
        "price_window_bd_around_signal": 40,
        "score_bucket_width": 5,
    },
}


def get_default_runtime_configs() -> dict[str, dict[str, Any]]:
    """Return copies of the default runtime configs keyed by config_type."""
    return {ctype: dict(cfg) for ctype, cfg in DEFAULT_RUNTIME_CONFIGS.items()}


def get_sector_alias_seeds() -> list[tuple[str, str, str]]:
    """Return ``(source, raw_sector, canonical_sector)`` seed rows.

    Sourced from ``constants.SECTOR_ALIAS_MAP`` so the DB ``sector_alias_map``
    table and the runtime normalization share one definition.
    """
    source = constants.SECTOR_ALIAS_SOURCE_YAHOO
    return [
        (source, raw, canonical)
        for raw, canonical in constants.SECTOR_ALIAS_MAP.items()
    ]
