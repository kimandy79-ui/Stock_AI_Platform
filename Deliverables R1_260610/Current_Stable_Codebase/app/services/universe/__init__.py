"""Universe service package (Module 06).

Exposes the :class:`~app.services.universe.universe_snapshot.UniverseSnapshotEngine`,
which maintains ``ticker_master`` and writes immutable monthly
``ticker_universe_snapshot`` rows. See ``M06_UNIVERSE_SNAPSHOT_SPEC.md``.
"""

from __future__ import annotations

from app.services.universe.universe_snapshot import UniverseSnapshotEngine

__all__ = ["UniverseSnapshotEngine"]
