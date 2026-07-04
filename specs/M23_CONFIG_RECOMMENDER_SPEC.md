# M23 — Config Recommender Spec (learning layer)

Module 23 aggregates realized outcomes from prod `signal_outcomes` and
simulation `sim_signal_outcomes`, grouped by `(setup_type, regime, config_id)`,
and proposes config changes for human review when a candidate config beats
the currently-active ("incumbent") config for its setup_type by enough
margin, with enough samples, to be worth a look. It never activates a config.

Distinct from `app.services.outcomes` (Module 16 — tracks/realizes outcomes)
and `app.services.config` (ConfigService — stores/versions configs): this
module only *reads* outcomes and *proposes* changes.

Code: `app/services/learning/config_recommender.py` (+ package `__init__.py`).
Tests: `tests/test_config_recommender.py`.

Contract source of truth: this spec (new module — no prior spec to derive
from); reuses `app.services.simulation.simulation_engine.compute_metrics`
(the `sim_config_comparisons` metric formulas) rather than defining new ones;
`01b_SCHEMA_AND_DATA.md` for `signal_outcomes` / `step5_proposals` shapes;
`M17_SIMULATION_ENGINE_SPEC.md` / `M17_SIMULATION_ENGINE_CONFIG_DELTA.md` for
`sim_signal_outcomes` / `sim_step5_proposals` shapes.

## Public API

```python
class ConfigRecommenderService:
    def __init__(self, db_manager=None) -> None: ...
    def run(
        self,
        db_role: str = "prod",
        sample_floor: int = 30,
        margin_k: float = 1.0,
        run_id: str | None = None,
    ) -> ServiceResult: ...
    def get_pending_recommendations(
        self, db_role: str = "prod", setup_type: str | None = None,
    ) -> ServiceResult: ...
```

- `db_manager` injectable; defaults to `app.database.duckdb_manager`.
- `run_id` minted (`uuid4`) when `None`, otherwise preserved.
- `db_role` for both methods accepts **only** `"prod"` — reads additionally
  and unconditionally touch `simulation.duckdb` (a fixed second role, not a
  parameter); writes go to `prod` only. Any other value returns `failed`
  before any DB access.
- `rows_processed` on `run()` equals `metadata["recommendations_written"]`.

### Exact metadata keys (`run()`)

`db_role, run_id, sample_floor, margin_k, prod_outcomes_read,
sim_outcomes_read, cells_evaluated, cells_skipped_no_incumbent_data,
candidates_evaluated, recommendations_written`.

## DB boundaries (tri-role discipline)

- Reads `signal_outcomes` LEFT JOIN `step5_proposals` (for `market_regime`)
  from `prod`, read-only.
- Reads `sim_signal_outcomes` LEFT JOIN `sim_step5_proposals` (for
  `market_regime`) from `simulation`, read-only, excluding
  `cross_fold_outcome = TRUE` rows (same convention as M17's run-level
  `sim_config_comparisons` aggregation). A read failure here (e.g. no
  `simulation.duckdb` yet) degrades to a warning + empty sim data, not a hard
  failure — prod-only history can still be useful.
- Reads `setup_configs` directly (own local SQL — never through
  `ConfigService`) from both `prod` and `simulation` to resolve the
  currently-active incumbent per `setup_type` (prod only) and to fetch
  `config_json` for building the human-readable parameter diff (prod
  overrides simulation on config_id overlap, since presets may have been
  seeded into either DB).
- Writes `config_recommendations` to `prod` only, single
  `BEGIN TRANSACTION` / `COMMIT`, `ROLLBACK` on error.
- Never imports `app.services.config.config_service`; never references
  `activate_setup_config` anywhere in source (enforced by an AST-based static
  scan in the test suite — checks actual `Name`/`Attribute` nodes, not a raw
  substring search, since the module's own docstring legitimately explains
  *why* it never calls it).
- No direct `duckdb` import, no provider imports, no `print()`, no DDL/`ATTACH`.

## Aggregation

Outcome rows from both sources are grouped by `(setup_type, regime,
config_id)`. `regime` is `market_regime` as already computed by M12 — a
literal value including `NULL` (never defaulted to `"neutral"`, per
CLAUDE.md's non-negotiable rule); `NULL` regime is its own distinct group.

Per group, `realized_r_multiple` values (ordered by `signal_date`) are passed
to `compute_metrics()` to get `expectancy` / `win_rate` / `avg_win` /
`avg_loss` / `profit_factor` / `max_drawdown_pct` / `resolved_outcomes_pct` —
reused verbatim, not reimplemented. `target_hit_rate` / `stop_hit_rate` are a
separate simple mean-of-non-NULL-boolean (`stop_hit` / `target_hit` columns)
computed locally, since `compute_metrics()` doesn't produce them.

The **incumbent** for a `(setup_type, regime)` cell is whichever `config_id`
is currently `active_flag = TRUE` for that `setup_type` in prod. If the
incumbent has no realized outcomes in a cell, the cell is **skipped**
entirely (counted in `cells_skipped_no_incumbent_data`) — there is no
baseline to compare against, so no candidate can be evaluated as "better."

## Guardrails (statistical — the one genuinely new piece of math in this module)

A cell can have more than one candidate config (Phase 1.5 seeds 1-2 presets
per `setup_type` plus the incumbent), which is a multiple-comparison setting.
Two guardrails are applied **pairwise against the incumbent, independently
for every candidate** (never a pooled "max across all candidates" comparison):

1. **Sample floor** (`sample_floor`, default 30) — both the candidate's and
   the incumbent's *resolved* (`realized_r_multiple IS NOT NULL`)
   observation count in the cell must be `>= sample_floor`. A flat floor, not
   a scaled Minimum Track Record Length / Minimum Backtest Length treatment
   (Bailey & Lopez de Prado) — that fuller approach was judged out of scope
   for a first cut per the originating coder note.
2. **Improvement margin** (`margin_k`, default 1.0) — the candidate's
   expectancy must exceed the incumbent's by at least `margin_k` pooled
   standard errors of the difference in sample means:
   ```
   pooled_se = sqrt(var_candidate / n_candidate + var_incumbent / n_incumbent)
   margin_required = margin_k * pooled_se
   qualified = (candidate_expectancy - incumbent_expectancy) >= margin_required
   ```
   `var_*` is each group's own sample variance (`statistics.variance`,
   population estimate with N-1 denominator) over its resolved values. This
   is a simple, explicitly-stated one-sided margin rule — not a formal
   hypothesis test at a declared alpha, and not a full Deflated Sharpe
   Ratio / Harvey-Liu multiple-testing haircut.

Implemented as the pure function `evaluate_candidate(candidate_resolved,
incumbent_resolved, candidate_expectancy, incumbent_expectancy, *,
sample_floor, margin_k) -> dict` — no I/O, directly unit-testable.

When more than one candidate qualifies in a cell, the one with the largest
`margin_achieved` is written as **the** recommendation for that cell; every
evaluated candidate's stats (qualified or not) are kept in `evidence_json`
for human-reviewer transparency, not just the winner's.

## `config_recommendations` schema

Added via the schema-manager pattern (Module 03) — `_PROD_TABLE_DDL` +
`_PROD_INDEX_DDL` in `app/database/schema_manager.py` (present in both
`prod.duckdb` and `debug.duckdb`, which share `_PRODUCTION_SCHEMA`; absent
from `simulation.duckdb`, which does not get this table).

```sql
CREATE TABLE IF NOT EXISTS config_recommendations (
    recommendation_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    setup_type VARCHAR NOT NULL,
    regime VARCHAR,
    incumbent_config_id VARCHAR NOT NULL,
    candidate_config_id VARCHAR NOT NULL,
    proposal_json JSON NOT NULL,
    evidence_json JSON NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_config_recs_setup_regime_status
    ON config_recommendations(setup_type, regime, status);
```

`status` vocabulary: `pending | approved | rejected`. Rows are always
inserted `pending`. **Activation is exclusively human-gated**: a human
reviewing a `pending` (or later `approved`) row must call
`ConfigService.activate_setup_config()` directly — no code path in this
module, or triggered by this module, ever writes `setup_configs.active_flag`
or transitions a recommendation's own `status` column. (This module also
never writes `status = 'approved'` or `'rejected'` itself; that transition
is a human/future-tooling action against the row, out of scope here.)

`proposal_json`: `[{"parameter": str, "current_value": Any, "proposed_value":
Any}, ...]` — a diff of the candidate's `validation` block against the
incumbent's, built by the pure function `build_parameter_diff(incumbent_json,
candidate_json)`. Only keys present in the candidate that differ from the
incumbent are included (missing `config_json` on either side degrades to an
empty/partial diff rather than raising).

`evidence_json`: `{"sample_floor", "margin_k", "incumbent": {config_id, n,
expectancy, win_rate, profit_factor, max_drawdown_pct, resolved_outcomes_pct,
target_hit_rate, stop_hit_rate}, "candidates": [{config_id, n_candidate,
n_incumbent, meets_sample_floor, candidate_expectancy, incumbent_expectancy,
margin_required, margin_achieved, qualified, win_rate, profit_factor,
max_drawdown_pct, resolved_outcomes_pct, target_hit_rate, stop_hit_rate},
...], "winner_config_id"}`.

## Wiring (out of scope for this module's code)

Weekly-cadence scheduling and Dashboard (M21) Strategy Performance surfacing
are explicitly deferred — Task Scheduler wiring is a future phase's job;
`get_pending_recommendations()` exists as the plain read method a future
dashboard page would call, but no dashboard code is touched here.

## Non-goals

No auto-activation, ever. No changes to `setup_configs` / `risk_label_config`
/ `sim_config_comparisons` schemas. No ML ranking. No dashboard UI. No changes
to the M12 regime engine. No DSR / Harvey-Liu full multiple-testing
correction (margin-based guardrail only, per above).

## Tests

`tests/test_config_recommender.py` covers (offline): the guardrail function
across floor/margin/qualify cases (including a pairwise-independence check —
two candidates evaluated against the same incumbent don't influence each
other) with *non-constant* fixture data (a zero-variance series has
`pooled_se == 0`, which would trivially qualify any positive difference and
defeat the point of a margin test); the parameter-diff builder including
missing-config_json degradation; the hit-rate helper's NULL handling; the
row-grouping helper; static AST scans (no `activate_setup_config` reference,
no `config_service` import, no `print()`, `compute_metrics` actually imported
from `simulation_engine` rather than reimplemented); and pre-DB validation
(non-`prod` `db_role` fails before any I/O, on both `run()` and
`get_pending_recommendations()`).

Integration tests (real tmp DuckDB, Module 03 schema, both `prod` and
`simulation` databases seeded): a clear-winner case writes exactly one
`pending` recommendation with correct incumbent/candidate ids and non-empty
proposal/evidence JSON; a candidate below the sample floor emits nothing
despite a large expectancy edge; a candidate above the floor but within
margin (noise-sized improvement) emits nothing; a cell with no incumbent
data is skipped (not treated as an automatic win); a missing
`simulation.duckdb` degrades the run to `success_with_warnings` rather than
failing; `get_pending_recommendations()` returns what `run()` wrote, with
JSON columns parsed back to Python objects.

Run:

```text
pytest -q tests/test_config_recommender.py
pytest -q
```
