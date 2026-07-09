"""P2.5 — Config read-coverage contract test.

Structural guard against the dead-key class of bug (a config key seeded in
default_configs.py that no code ever reads — found and cleaned up multiple
times in this project). Complements
``test_config_service.test_preset_setup_configs_use_only_existing_validator_fields``
(which proves presets introduce no *new* validation keys); this test proves
every *seeded* key is actually *read* somewhere in the reader modules.

Mechanism: AST-scan each reader module for every string literal used as the
first argument of a ``.get(...)`` call or as a ``[...]`` subscript index — i.e.
the set of dict keys the code actually reads. Every leaf key seeded across the
setup configs, presets, and risk_label_config must be either:

  (1) read as a literal in some reader module; or
  (2) a leaf of a data-map that code consumes whole (``_MAPS_CONSUMED_WHOLE``)
      — leaves are data (accessed by dynamic key), so the block name is what's
      read, not each leaf; or
  (3) listed in the ``KNOWN_SEEDED_BUT_UNREAD`` ledger below — a pre-existing
      seeded-but-unread key, each annotated with its status. THIS LEDGER IS THE
      SURFACED DRIFT: every entry is a real finding for architect decision
      (remove the dead key, or wire it up), not a silent exemption. The test
      stays green so it can ship, but the ledger makes the drift explicit and
      reviewable, and any *new* unread key (not in the ledger) fails the test —
      the going-forward guarantee.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.config import default_configs as dc

_ROOT = Path(__file__).resolve().parents[1]

# Every module that legitimately reads config dicts.
_READER_MODULES: tuple[str, ...] = (
    "app/services/screening/step3_universal_eligibility.py",
    "app/services/screening/m14_setup_validators.py",
    "app/services/analysis/step4_setup_validation_engine.py",
    "app/services/proposal/step5_proposal_engine.py",
    "app/services/simulation/simulation_engine.py",
    "app/services/regime/market_regime_engine.py",
    "app/services/outcomes/outcome_queue.py",
    "app/services/pipeline/pipeline_orchestrator.py",
    "app/services/diagnostics/funnel_diagnostics.py",
    "app/services/learning/config_recommender.py",
    "app/services/features/feature_engine.py",
    "app/dashboard/ticker_report.py",
    "app/dashboard/data_access.py",
    "app/dashboard/action_service.py",
)

# Identity / lineage metadata — not behavioral config, never "read by a validator".
_METADATA_KEYS: frozenset[str] = frozenset({"parent_config_id"})

# Data-map blocks consumed as a whole dict (leaves are values accessed by a
# dynamic key, so the *block name* is the read, not each leaf).
_MAPS_CONSUMED_WHOLE: frozenset[str] = frozenset({"valuation_band_quality"})

# --------------------------------------------------------------------------- #
# KNOWN_SEEDED_BUT_UNREAD — the drift ledger (pre-existing findings).
# key name -> status/rationale. Flagged for architect decision; not fixed here
# (removing seeded config is behavior-adjacent and out of this task's scope).
# --------------------------------------------------------------------------- #
KNOWN_SEEDED_BUT_UNREAD: dict[str, str] = {
    # macro penalty is a flat points hit when the macro flag is set; the window
    # sizing params were seeded but the penalty logic never consults them.
    "window_bd_before": "VESTIGIAL: _compute_penalties applies a flat macro penalty; window unused",
    "window_bd_after": "VESTIGIAL: _compute_penalties applies a flat macro penalty; window unused",
    # consolidation_base forces rvol_required=False in code (AD-22.23); the
    # seeded key is never read.
    "rvol_required": "VESTIGIAL: consolidation_base hardcodes rvol not-required (AD-22.23)",
    # NOTE: the two shadow VIX-threshold keys (high_risk_vix / extreme_risk_vix)
    # were REMOVED from the seed in P2.5 (they duplicated constants.VIX_* — the
    # live source — and were a drift trap), so they are no longer ledgered here.
    # step5 diversification reads the other keys in this block but not this flag.
    "penalty_applies_before_cap_only": "VESTIGIAL: never read by step5 _apply_hard_cap/_apply_soft_penalty",
    # simulation engine hardcodes these behaviors; only slippage_bps is read.
    "entry_rule": "SIM-HARDCODED: simulation_engine fixes the entry rule in code",
    "return_price_type": "SIM-HARDCODED: simulation_engine fixes the return price type in code",
    "commission_per_trade": "SIM-HARDCODED: not read by outcome/simulation compute",
    "horizons_bd": "SIM-HARDCODED: horizon set fixed in simulation_engine/outcome_queue",
    "min_resolved_outcomes_pct": "SIM-HARDCODED: 0.85 constraint applied in code, not read from config",
    "max_drawdown_constraint_pct": "SIM-HARDCODED: 25% constraint applied in code, not read from config",
}

# Entire blocks seeded but unread (shadow of a constants-based source).
# NOTE: the sole prior entry, "sector_etf_mapping", was REMOVED from the seed
# in P2.5 (it duplicated constants.SECTOR_ETF_MAP), so this ledger is now empty.
# Kept as a named hook for any future whole-block shadow that gets flagged.
KNOWN_DEAD_BLOCKS: dict[str, str] = {}


def _read_keys() -> set[str]:
    """Union of dict keys any reader module reads as a string literal."""
    keys: set[str] = set()
    for rel in _READER_MODULES:
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "get" and node.args:
                    a0 = node.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        keys.add(a0.value)
            elif isinstance(node, ast.Subscript):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    keys.add(sl.value)
    return keys


def _leaf_paths(obj, path: tuple[str, ...] = ()) -> list[tuple[str, tuple[str, ...]]]:
    """(leaf_key, full_path_tuple) for every scalar leaf in a nested dict."""
    out: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + (k,)
            if isinstance(v, dict):
                out.extend(_leaf_paths(v, p))
            else:
                out.append((k, p))
    return out


def _all_seeded_leaves() -> list[tuple[str, tuple[str, ...], str]]:
    """(leaf_key, path, source_label) across every seeded config."""
    leaves: list[tuple[str, tuple[str, ...], str]] = []
    for st, cfg in dc.DEFAULT_SETUP_CONFIGS.items():
        for k, p in _leaf_paths(cfg):
            leaves.append((k, p, f"setup:{st}"))
    for preset in dc.PRESET_SETUP_CONFIGS:
        cid = preset.get("config_id", "?")
        for k, p in _leaf_paths(preset):
            leaves.append((k, p, f"preset:{cid}"))
    for k, p in _leaf_paths(dc.DEFAULT_RISK_LABEL_CONFIG):
        leaves.append((k, p, "risk_label_config_v1"))
    return leaves


def _is_covered(key: str, path: tuple[str, ...], read: set[str]) -> bool:
    if key in read:
        return True
    if key in _METADATA_KEYS:
        return True
    # data-map leaf: parent block consumed whole and its name is read
    if len(path) >= 2 and path[-2] in _MAPS_CONSUMED_WHOLE and path[-2] in read:
        return True
    if any(comp in KNOWN_DEAD_BLOCKS for comp in path):
        return True
    if key in KNOWN_SEEDED_BUT_UNREAD:
        return True
    return False


def test_every_seeded_config_key_is_read_or_ledgered() -> None:
    """No seeded config key is silently dead: it is read by a reader module,
    is a whole-consumed data-map leaf, or is an explicitly-ledgered known
    unread key. A new unread key that is none of these fails here."""
    read = _read_keys()
    uncovered = [
        (src, ".".join(path))
        for key, path, src in _all_seeded_leaves()
        if not _is_covered(key, path, read)
    ]
    assert not uncovered, (
        "Seeded config keys that are neither read nor ledgered "
        "(add to KNOWN_SEEDED_BUT_UNREAD with a rationale, or wire them up):\n"
        + "\n".join(f"  {src}: {p}" for src, p in sorted(set(uncovered)))
    )


def test_p2_1_promoted_keys_are_actually_read() -> None:
    """Positive assertion that the P2.1 [HC->CFG] promotions are wired (not
    just present) — guards against a future refactor silently orphaning them."""
    read = _read_keys()
    for key in (
        "eligibility_score_weights", "routing",          # universe (step3)
        "breakout_proximity_min", "range_duration_min",  # routing thresholds
        "pullback_depth_min", "pullback_depth_max",
        "ema_alignment_min", "ema50_slope_min", "range_tightness_min",
        "liquidity", "price", "history",                 # eligibility weights
        "scoring", "confidence", "high_threshold", "medium_threshold",
        "valuation_band_quality",                        # scoring maps (m14)
    ):
        assert key in read, f"P2.1-promoted config key {key!r} is not read by any module"


def test_ledger_entries_are_genuinely_unread() -> None:
    """Hygiene: every ledgered 'unread' key really is unread. If one becomes
    read (e.g. someone wires it up), remove it from the ledger — this keeps the
    drift ledger honest and prevents it from masking a real future dead key."""
    read = _read_keys()
    still_read = [k for k in KNOWN_SEEDED_BUT_UNREAD if k in read]
    assert not still_read, (
        "These ledgered keys are now READ — remove them from "
        f"KNOWN_SEEDED_BUT_UNREAD: {still_read}"
    )
