# Phase 2 — Config Recommender: Design Note

## Guardrail formula (the one genuinely new piece of math)

Applied **pairwise against the incumbent, independently for every candidate** in a `(setup_type, regime)` cell — never a pooled "max across all candidates" comparison:

1. **Sample floor** (default 30): both candidate's and incumbent's resolved (`realized_r_multiple IS NOT NULL`) observation count must be `>= sample_floor`. Flat floor, not a scaled Minimum Track Record Length / Minimum Backtest Length (Bailey & Lopez de Prado) treatment — judged out of scope for a first cut per the coder note. Flagging this explicitly in case a future pass wants the fuller treatment.
2. **Improvement margin** (default `margin_k=1.0`): candidate's expectancy must exceed incumbent's by at least `margin_k` pooled standard errors of the difference in sample means:
   ```
   pooled_se = sqrt(var_candidate/n_candidate + var_incumbent/n_incumbent)
   margin_required = margin_k * pooled_se
   qualified = (candidate_expectancy - incumbent_expectancy) >= margin_required
   ```
   `var_*` = each group's own sample variance (`statistics.variance`, N-1 denominator). This is a simple, stated one-sided margin rule — not a formal hypothesis test at a declared alpha, not a full Deflated Sharpe Ratio / Harvey-Liu multiple-testing haircut. Both guardrails required to qualify.

Implemented as pure function `evaluate_candidate(...)` — no I/O, directly unit-testable. When multiple candidates qualify in a cell, the one with the largest margin achieved is written as the recommendation; every evaluated candidate (qualified or not) is kept in `evidence_json` for reviewer transparency.

**A note on testing this**: a zero-variance fixture (identical repeated values) makes `pooled_se == 0`, which trivially "qualifies" any positive difference regardless of size — worth remembering if extending the test suite, since it's an easy way to write a guardrail test that silently passes for the wrong reason. Fixed this in my own first draft of the tests before landing on the final version.

## Sample threshold logic

Flat floor of 30, applied independently to both groups (not just the candidate) — a candidate compared against a tiny incumbent baseline would be just as unreliable as a tiny candidate sample. `sample_floor` and `margin_k` are both `run()` parameters, not hardcoded constants.

## Key design decisions / assumptions

1. **`realized_r_multiple` as the aggregation metric**, not %-return. The coder note said "avg realized R," and R-multiple is horizon-independent (one terminal value per outcome per Module 16 rules), so grouping is exactly `(setup_type, regime, config_id)` with no horizon dimension — simpler than `sim_config_comparisons`' `(config, horizon, list_type)` shape, and matches what was actually asked.
2. **`market_regime` is joined in, not stored on the outcome tables directly.** Neither `signal_outcomes` nor `sim_signal_outcomes` has a `market_regime` column — it lives on `step5_proposals`/`sim_step5_proposals` (joined via `proposal_id`). `NULL` regime is treated as its own distinct group, never defaulted to `"neutral"` (CLAUDE.md's non-negotiable rule).
3. **No `ConfigService` import at all** — `setup_configs` reads (active-config lookup, `config_json` fetch for the diff) go through this module's own local SQL, exactly like every other M13-M16 service already reads its own configs directly rather than through `ConfigService`. This makes "never calls `activate_setup_config`" true by construction (no import path exists to reach it), not just a promise — and the test suite's static scan is AST-based (checking actual `Name`/`Attribute` nodes) rather than a raw substring search, since the module's own docstring legitimately explains *why* it never calls it in prose.
4. **`config_json` lookup merges prod + simulation** (prod wins on overlap) — since Phase 1.5 presets may have been seeded into either DB (nothing auto-seeds them anywhere yet, per that phase's design note), and a candidate's parameter diff needs its config_json from wherever it actually landed.
5. **Missing `simulation.duckdb` degrades to a warning**, not a hard failure — a fresh install with no sim runs yet can still usefully aggregate prod-only history for setup types with production track record.
6. **A cell with no incumbent data is skipped entirely** (`cells_skipped_no_incumbent_data`), not treated as an automatic candidate win — there's no baseline to measure "improvement" against.
7. **One recommendation row per qualifying cell**, naming the single best-margin candidate, with all evaluated candidates in `evidence_json` — not one row per qualifying candidate. Only one config can practically replace the incumbent, so this is the actionable shape for a human reviewer, while still being fully transparent about what else was considered.

## What shipped

- `app/services/learning/config_recommender.py` + `__init__.py` (new `learning/` domain).
- `config_recommendations` table + index added via the schema-manager pattern (`_PROD_TABLE_DDL`/`_PROD_INDEX_DDL` in `app/database/schema_manager.py`) — present in prod/debug (shared `_PRODUCTION_SCHEMA`), absent from simulation.
- `specs/M23_CONFIG_RECOMMENDER_SPEC.md` — full contract (public API, DB boundaries, aggregation, guardrail formula, schema, non-goals, test coverage).
- `CLAUDE.md` updated: M23 module-to-file mapping, test file listing, spec listing.
- `tests/test_schema_manager.py` updated: new table/index added to the `EXPECTED_PROD_TABLES`/`EXPECTED_PROD_INDEXES`/`FORBIDDEN_IN_SIM` fixtures (exact-set assertions would otherwise fail).

## Testing

`tests/test_config_recommender.py` — 20 tests: pure guardrail function (floor/margin/qualify/pairwise-independence with non-constant fixture data), parameter-diff builder (including missing-config_json degradation), hit-rate NULL handling, row-grouping helper, static AST scans (no `activate_setup_config`, no `config_service` import, no `print()`, `compute_metrics` actually imported not reimplemented), pre-DB validation (non-prod db_role fails before I/O on both `run()` and `get_pending_recommendations()`), and 6 real-tmp-DuckDB integration tests: clear winner → 1 pending recommendation with correct ids/JSON; below sample floor → nothing despite huge edge; above floor but within margin → nothing despite "higher"; no incumbent data → cell skipped; missing simulation.duckdb → `success_with_warnings`, not failure; `get_pending_recommendations()` round-trips what `run()` wrote.

Full suite green: `test_config_recommender.py`, `test_schema_manager.py`, `test_config_service.py`, `test_simulation_engine.py`, `test_m14_setup_validators.py`, `test_step5_proposal_engine.py`, `test_outcome_queue.py`.

## Exit criterion status

Recommender aggregates real Phase 1/1.5 outcome data (via prod `signal_outcomes` + sim `sim_signal_outcomes`), emits proposals only when both sample-floor and margin guardrails are satisfied, writes `config_recommendations` with `status=pending`, never activates anything (verified structurally, not just by convention). Full suite green.

**Not done in this task (explicitly out of scope per the coder note):** weekly-cadence scheduling wiring (Phase 5), Dashboard/M21 Strategy Performance surfacing (separate M21 task — `get_pending_recommendations()` is the plain read method ready for it), DSR/Harvey-Liu full multiple-testing correction, actually seeding Phase 1.5 presets anywhere (still nobody's job yet — noted in that phase's memory too).
