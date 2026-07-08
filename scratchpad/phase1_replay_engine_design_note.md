# Phase 1 — Setup-Mode Simulation Replay Engine: Design Note

## What changed

`app/services/simulation/simulation_engine.py`:

1. **`_replay_date` implemented** (was a stub raising `_REPLAY_UNSUPPORTED`). Split into two methods matching the execution model:
   - `_replay_step3(...)` — Step 3 (universal eligibility + routing), called **once per `sim_date`**, shared across every `(setup_config_id, risk_label_config_id)` variant. Writes `sim_step3_candidates` (config-independent — that table has no `setup_config_id` column).
   - `_replay_date(...)` — Step 4 (`m14_setup_validators.validate_setup`) + Step 5 (`Step5ProposalEngine._build_rows`) for **one variant**, given the date's already-computed Step 3 routing output and a shared feature+price frame. Writes `sim_step4_analysis` / `sim_step5_proposals`.
   - `_run_with_connection`'s loop is now date-outer / config-inner (was config-outer / date-inner in the stub scaffolding), and both the Step 3 universe read and the Step 4/5 feature+price read happen once per date, not once per variant.

2. **Reuse map followed exactly** per `M17_SIMULATION_ENGINE_CONFIG_DELTA.md`: only `step3_universal_eligibility._check_eligibility` / `_evaluate_routing` / `_compute_eligibility_score` (plus `_assert_universe_parity` / `_parse_universe_config`, the config-parsing helpers those three depend on), `m14_setup_validators.validate_setup`, and `Step5ProposalEngine._build_rows` are called. `Step3UniversalEligibilityEngine.run()` / `Step4SetupValidationEngine.run()` are never called. `_build_snapshot` (listed as available in the delta) turned out to be unnecessary — `sim_step3_candidates` has no snapshot column and replay always reads live features fresh, so there's no snapshot-merge step to feed.

3. **Batching interpretation.** The CODER_NOTE asked for "one Polars pass per date... express each variant's thresholds as column expressions" while also mandating "call only these pure functions." Those two are only simultaneously satisfiable if "batch" means *don't re-read the DB per variant*, not *vectorize the validators' branching logic into Polars expressions* (the latter would mean re-implementing `validate_breakout`/etc.'s formulas outside `m14_setup_validators`, which breaks the delta's own "no formula divergence" guarantee). I implemented the former: the Step 3 universe read and the Step 4/5 feature+price read each happen exactly once per `sim_date`; every variant then runs a plain Python pass over the shared in-memory data calling the frozen pure functions. With the stated variant space (~8–12 total, presets not a factorial grid) this is fast and avoids reimplementing scoring logic. Flagging this interpretation explicitly since it's a judgment call reconciling two requirements in tension, not a literal reading of "Polars column expressions."

4. **`risk_label_config` placement.** The delta's variants are `(setup_config_id, risk_label_config_id)` pairs, but `run()`'s public signature (documented in `M17_SIMULATION_ENGINE_SPEC.md`) only has one `setup_configs: dict[str, dict]` parameter — no separate risk-label dimension. Rather than changing the documented public API, each variant's `risk_label_config` block now lives nested inside its own `setup_configs[config_id]` dict (alongside the existing `universe`/`validation`/`scoring_weights`/`earnings`/`macro_event_risk` blocks). `run()`'s signature is unchanged.

5. **Schema-parity fixes** (surfaced only now that replay actually writes these tables — the old stub never reached them):
   - `_INSERT_SIM_STEP4` was missing `entry_price_raw` (present in the DDL). Added.
   - `_INSERT_SIM_STEP5` was missing 12 DDL columns (`risk_score`, `risk_reasons`, `entry_price_raw`, `stop_price_raw`, `target_price_raw`, `estimated_rr`, `target_is_structural`, `support_level`, `resistance_level`, `next_resistance_level`, `market_regime`, `earnings_days`) — the comment already said "prod-equivalent fields for audit/debug parity" but the INSERT never wrote them. All are already produced by `_build_rows`, so this is a plumbing fix, not new computation.
   - `_write_comparisons` hardcoded `setup_type=None`, but `sim_config_comparisons.setup_type` is `NOT NULL` in the DDL — this would have crashed the first time `_write_comparisons` ever actually ran against real DuckDB (it never had, since replay was stubbed). Now populated from `setup_configs[config_id]["setup_type"]`, via a new optional `setup_configs` keyword param (defaults preserve old behavior for direct unit-test callers).
   - `_SELECT_FEATURES_PRICES`'s column list mirrored the **legacy** `Step4AnalysisEngine.fp_cols` (19 columns) rather than what `m14_setup_validators.validate_setup` actually needs (43 columns, matching `step4_setup_validation_engine._FEATURE_COLS`). Replaced; the 4-placeholder parameter shape is unchanged (existing `test_sql_placeholder_counts` regression test still passes untouched).
   - `_SELECT_SCREENING_INPUT` (legacy step3_screening column shape) replaced with `_SELECT_UNIVERSE_STEP3`, matching `step3_universal_eligibility`'s actual universe-read column order exactly.
   - `_SELECT_RECENT_20D_LOW` / `_SELECT_PRIOR_10` were **not** touched — they're vestigial (legacy trend-resume/20-day-low concepts with no counterpart in `m14_setup_validators`, which gets `swing_low`/`support_level` etc. as precomputed feature columns instead). Left in place, unused, since an existing placeholder-count test (`test_sql_placeholder_counts`) references them by name; removing them was out of scope for this task.

6. **New requirements (embargo + fold-planner seam), both additive/backward-compatible:**
   - `SimulationEngine.__init__` gained two optional constructor params: `fold_planner: Callable[[date, date], list[dict]] = plan_walk_forward_folds` and `embargo_bd: int = DEFAULT_EMBARGO_BD` (40). `_run_with_connection` calls `self._fold_planner(...)` instead of the module-level function directly — a future CPCV-style planner can be swapped in without touching any replay method, since every replay/outcome/metric method already receives an already-materialized `folds: list[dict]` and never generates folds itself.
   - `_write_folds` / `_fold_train_metrics` gained optional keyword-only `cal` / `embargo_bd` params (default `None` / `0` = off). When the engine's own run loop calls them it always supplies both; direct unit-test callers that omit them keep the exact pre-embargo behavior, so no existing test needed to change. New static helper `_embargo_cutoff(cal, fold, embargo_bd)` computes the trading day at/after which train-window signals near `test_start` are excluded, using `cal.trading_days_between(...)` (works with both the real calendar and the test suite's `FakeCalendar`, since both already implement that method).

## Reuse map — confirmed followed exactly
Step 3 → `step3_universal_eligibility` pure functions. Step 4 → `m14_setup_validators.validate_setup`. Step 5 → `Step5ProposalEngine()._build_rows` (instantiated only to reach the pure method — never `.propose()`). No calls to either `Engine.run()` orchestration wrapper anywhere in the new code.

## Embargo default
40 business days (matches `SELECTION_HORIZON_BD`), as specified. Configurable via `SimulationEngine(embargo_bd=...)`; off (`0`) when omitted on direct calls to the lower-level methods, so no pre-existing test's expectations changed.

## Fold seam location
`SimulationEngine.__init__(fold_planner=...)` → stored as `self._fold_planner` → called once in `_run_with_connection` (`mode == MODE_WALK_FORWARD` branch). Everything downstream (`_fold_for_date`, `_replay_step3`, `_write_folds`, `_fold_train_metrics`, `_write_comparisons`) takes an already-built `folds`/`fold_ids` and never calls a planner itself.

## Testing

Added to `tests/test_simulation_engine.py` (all passing, real tmp DuckDB + real Polars/duckdb where relevant):
- `test_integration_replay_writes_all_sim_tables_single_variant` — full pipeline, single variant; asserts the newly-populated columns (`entry_price_raw` in `sim_step4_analysis`; `risk_score`/`stop_price_raw`/`target_price_raw`/`support_level`/`resistance_level`/`market_regime` in `sim_step5_proposals`; `setup_type` populated in `sim_config_comparisons`).
- `test_integration_step3_runs_once_per_date_regardless_of_variant_count` — 3 variants, same ticker/date: `sim_step3_candidates` count for that ticker/date is 1 (not 3), `sim_step4_analysis` count is 3.
- `test_integration_batch_variants_produce_distinct_outcomes` — a lenient (`min_setup_score=1.0`) and a strict (`min_setup_score=99.9`) variant on the same candidate diverge: `setup_passed=True` vs `False`.
- `test_fold_train_metrics_embargo_excludes_signals_near_test_boundary` — offline; a signal in the last 40 sessions before `test_start` is included with `embargo_bd=0` (default/off) and excluded with `embargo_bd=40`.
- `test_write_folds_default_embargo_off_preserves_prior_behavior` — the pre-existing direct-call pattern (no `cal`/`embargo_bd`) is unchanged.
- `test_fold_planner_is_injectable` + `test_fold_planner_seam_used_by_real_run` — constructor injection is stored and actually reaches `sim_folds` (a custom planner's `fold_number` shows up in the written row, not `plan_walk_forward_folds`'s).

No parity/regression test against an "old path" was applicable here (unlike the earlier N+1 CODER_NOTE) — there was no old *working* replay path to diff against; the stub raised unconditionally. Correctness instead rests on: (a) calling the exact same pure functions prod uses, with matching input row shapes (`_STEP3_COL_NAMES`/`_STEP4_FEATURE_COLS` mirror the prod engines' column orders 1:1), and (b) the new integration tests above.

## Test suite status
Full existing M17 suite green, plus M14 (`test_m14_setup_validators.py`), M15 (`test_step5_proposal_engine.py`), M13 (`test_step3_universal_eligibility.py`), and `test_phase7_setup_mode.py` all green.

**One pre-existing regression required a fix, unrelated to correctness of the replay logic itself:** `tests/test_phase7_setup_mode.py` had two tests (`test_replay_is_guarded_and_returns_failed`, `test_replay_unsupported_sentinel_exists`) that encoded the *old stub* as correct behavior — they imported `_REPLAY_UNSUPPORTED` and asserted `run()` always fails with that exact sentinel. Since removing the guard was this task's entire purpose, these were rewritten: the first now asserts replay actually executes and fails for a *real* configuration reason (the test's intentionally-minimal fixture config lacks a `universe` block) rather than the old hardcoded message; the second now asserts `_REPLAY_UNSUPPORTED` no longer exists (proving intentional removal).

**Two failures found in the full-repo run are pre-existing and unrelated** — confirmed by stashing all of this session's changes and re-running: `tests/test_data_validator.py::test_spec_documents_open_gaps_not_invented` and `tests/test_mutation_detector.py::test_spec_documents_open_gap_g1` both fail identically on a clean `main` checkout (`FileNotFoundError` for `M09_DATA_VALIDATOR_SPEC.md` / `M10_MUTATION_DETECTOR_SPEC.md` at the project root — both frozen M09/M10 modules, never touched this session). Left as-is; out of scope for this task and for the frozen-module rule.

## Performance
Not measured — no representative historical prod dataset was available in this session to run a real multi-month/multi-variant sweep against. The design's expected win is structural: Step 3 and the feature+price read now happen once per date instead of once per `(date, variant)` pair, which for the stated ~8–12 variant sweep means roughly an 8–12x reduction in redundant DB reads for those two queries, independent of dataset size.
