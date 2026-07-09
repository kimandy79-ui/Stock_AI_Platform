#!/usr/bin/env python3
"""CLI: auto-generate the setup-factor matrix from live config + reader source.

Replaces the *factual/value* layer of the hand-maintained
``Deliverables/setup_factor_matrix.csv`` (which has drifted before) by
introspecting ``default_configs.py`` for seeded values and AST-scanning the
reader modules for which code actually reads each key.

Scope / honesty note: this emits the introspectable core only — per-config
values, which module(s) read each key, and a coverage status. It does NOT
reproduce the hand-maintained matrix's editorial layer ([HC]/[CFG]/[DEAD]
tags, prose descriptions, exact source line refs). Treat the generated file as
the source of truth for *values and read-coverage*; keep any editorial prose
as a separate, thinner human layer if still wanted. The companion contract
test ``tests/test_config_read_coverage.py`` is what actually enforces no dead
keys; this tool is for human review/diffing.

Usage::

    python tools/generate_setup_factor_matrix.py
    python tools/generate_setup_factor_matrix.py --out Deliverables/setup_factor_matrix_generated.csv
    python tools/generate_setup_factor_matrix.py --stdout
"""
from __future__ import annotations

import argparse
import ast
import csv
import io
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.services.config import default_configs as dc

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


def _key_readers() -> dict[str, set[str]]:
    """Map each literal-read dict key -> set of reader module basenames."""
    readers: dict[str, set[str]] = {}
    for rel in _READER_MODULES:
        base = rel.split("/")[-1]
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "get" and node.args:
                    a0 = node.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        key = a0.value
            elif isinstance(node, ast.Subscript):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    key = sl.value
            if key is not None:
                readers.setdefault(key, set()).add(base)
    return readers


def _leaf_paths(obj, path: tuple[str, ...] = ()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = path + (k,)
            if isinstance(v, dict):
                yield from _leaf_paths(v, p)
            else:
                yield p, v


def _fmt(v) -> str:
    return "" if v is None else str(v)


def build_setup_matrix() -> list[list[str]]:
    """configs (v1 + presets) as columns, seeded leaf paths as rows."""
    readers = _key_readers()
    configs: list[tuple[str, dict]] = [
        (cfg["config_id"], cfg) for cfg in dc.DEFAULT_SETUP_CONFIGS.values()
    ]
    configs += [(p["config_id"], p) for p in dc.PRESET_SETUP_CONFIGS]

    # union of all leaf paths across setup configs, preserving first-seen order
    all_paths: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    per_config_vals: list[dict[tuple[str, ...], object]] = []
    for _cid, cfg in configs:
        vals = {p: v for p, v in _leaf_paths(cfg)}
        per_config_vals.append(vals)
        for p in vals:
            if p not in seen:
                seen.add(p)
                all_paths.append(p)

    header = ["Path", *[cid for cid, _ in configs], "Readers", "Coverage_Status"]
    rows: list[list[str]] = [header]
    for path in all_paths:
        leaf = path[-1]
        rd = sorted(readers.get(leaf, set()))
        status = "READ" if rd else "UNREAD"
        row = [".".join(path)]
        for vals in per_config_vals:
            row.append(_fmt(vals.get(path, "")) if path in vals else "—")
        row.append(",".join(rd))
        row.append(status)
        rows.append(row)
    return rows


def build_risk_label_matrix() -> list[list[str]]:
    readers = _key_readers()
    cfg = dc.DEFAULT_RISK_LABEL_CONFIG
    rows: list[list[str]] = [["Path", "Value", "Readers", "Coverage_Status"]]
    for path, v in _leaf_paths(cfg):
        leaf = path[-1]
        rd = sorted(readers.get(leaf, set()))
        rows.append([".".join(path), _fmt(v), ",".join(rd), "READ" if rd else "UNREAD"])
    return rows


def _write_csv(rows: list[list[str]], buf) -> None:
    w = csv.writer(buf, lineterminator="\n")
    w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the setup-factor matrix from live config.")
    ap.add_argument(
        "--out",
        default="Deliverables/setup_factor_matrix_generated.csv",
        help="Output CSV path (default: Deliverables/setup_factor_matrix_generated.csv).",
    )
    ap.add_argument("--stdout", action="store_true", help="Write to stdout instead of a file.")
    args = ap.parse_args(argv)

    setup_rows = build_setup_matrix()
    risk_rows = build_risk_label_matrix()

    if args.stdout:
        buf = io.StringIO()
        buf.write("# SETUP CONFIGS\n")
        _write_csv(setup_rows, buf)
        buf.write("\n# RISK_LABEL_CONFIG\n")
        _write_csv(risk_rows, buf)
        sys.stdout.write(buf.getvalue())
        return 0

    out_path = (_ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write("# SETUP CONFIGS\n")
        _write_csv(setup_rows, fh)
        fh.write("\n# RISK_LABEL_CONFIG\n")
        _write_csv(risk_rows, fh)
    print(f"Wrote {out_path} ({len(setup_rows) - 1} setup rows, {len(risk_rows) - 1} risk rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
