# M17 Simulation Engine — Config Delta (setup-mode replay correction)

## Why this delta exists
`M17_SIMULATION_ENGINE_SPEC.md` §"Step 3/4/5 replay (chosen approach)" names
`Step3ScreeningEngine._evaluate`/`_build_rows` and
`Step4AnalysisEngine._build_rows` as the reused pure functions. These are the
legacy (pre-setup-mode) engines. The current `simulation_engine.py` guard
comment already states legacy `step3_screening` / `step4_analysis_engine`
must **not** execute. This delta corrects the spec to name the actual
setup-mode engines and confirms the "no formula divergence" guarantee still
holds with them.

## Corrected replay-approach reuse map

| Step | Correct source (setup-mode) | Reusable pure entry points |
|---|---|---|
| Step 3 — universal eligibility + routing | `app/services/screening/step3_universal_eligibility.py` | `_check_eligibility`, `_compute_eligibility_score`, `_evaluate_routing`, `_build_snapshot` (all pure, module-level, no DB I/O) |
| Step 4 — setup validation + trade plan | `app/services/screening/m14_setup_validators.py` | `validate_setup` (dispatches to `validate_breakout` / `validate_pullback` / `validate_trend_continuation` / `validate_consolidation_base`) — pure, no DB I/O. Note: `step4_setup_validation_engine.py` itself is I/O orchestration only; its `run()` calls into `m14_setup_validators.validate_setup` for the actual scoring/trade-plan logic. Replay must call `m14_setup_validators` directly, not `step4_setup_validation_engine.run()`. |
| Step 5 — risk labeling + ranking + proposals | `app/services/proposal/step5_proposal_engine.py` | `Step5ProposalEngine._build_rows` (pure, confirmed unchanged from original spec) |

`Step3UniversalEligibilityEngine.run()` and `Step4SetupValidationEngine.run()`
remain I/O orchestration wrappers (config load, DB read, DB write) around
these pure functions — replay reuses only the pure functions, never the
`run()` methods, to avoid prod/debug writes and to keep sim-scoped no-look-ahead
reads under the replay engine's own control.

## What does not change
- No-look-ahead enforcement, DB boundaries (`connect_simulation_with_prod`,
  read-only `prod` alias, writes only to `sim_*`), run lifecycle
  (`pending → running → success/failed`, single transaction), outcome/list-membership
  rules, config-comparison rules, and walk-forward fold logic in
  `M17_SIMULATION_ENGINE_SPEC.md` are all unaffected by this delta and remain
  authoritative as written.
- **V1 walk-forward stays Option B (replay-all).** This delta does not
  introduce Option A (restrict test-fold generation to the fold's
  `selected_config_id`). Every requested `(setup_config_id,
  risk_label_config_id)` variant is still replayed across the full window;
  `sim_folds.selected_config_id` remains a recorded metric, not a filter.

## Governance note
Per `02b_ARCHITECTURE_DECISIONS.md` AD-22.24, M17 (simulation) is explicitly
in the frozen-module exemption scope for the setup-mode migration. Implementing
`_replay_date` against the setup-mode engines above is completing already-declared
scope (the sim schema already carries `setup_config_id` / `risk_label_config_id`
columns; the M17 spec's own "chosen approach" section already commits to this
kind of pure-function reuse — only the named source modules were stale). No
additional ADR is required for this delta.

## Open item carried forward (not resolved by this delta)
Batching strategy for evaluating multiple `(setup_config_id,
risk_label_config_id)` variants per fold/date (single Polars pass via column
expressions vs. chunked batching) depends on the real variant-space size per
sweep — still pending input before the coder note is written.
