"""Module 17 — Simulation Engine package.

Exposes :class:`~app.services.simulation.simulation_engine.SimulationEngine`,
the single entry point that replays Step 3/4/5 and realized outcomes into the
``sim_*`` tables of ``simulation.duckdb`` while reading production data only
through a read-only prod attach.
"""

from __future__ import annotations

from app.services.simulation.simulation_engine import (
    ALLOWED_MODES,
    RUN_METADATA_KEYS,
    SimulationEngine,
)

__all__ = ["SimulationEngine", "ALLOWED_MODES", "RUN_METADATA_KEYS"]
