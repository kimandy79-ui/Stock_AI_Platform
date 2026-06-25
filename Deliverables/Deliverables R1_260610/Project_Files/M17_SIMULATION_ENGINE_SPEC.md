# M17 — Simulation Engine Spec

Module 17 replays the frozen Step 3/4/5 pipeline and realized outcomes over a
historical date range into the `sim_*` tables of `simulation.duckdb`. It is the
first writer of `sim_runs`, `sim_folds`, `sim_step3_candidates`,
`sim_step4_analysis`, `sim_step5_proposals`, `sim_signal_outcomes` and
`sim_config_comparisons`.

Contract source of truth: `M02_SCHEMA_SPEC.md` §4 / `01b_SCHEMA_AND_DATA.md`
(table shapes), `01d_MODULES_AND_PIPELINE.md` `SIMULATION/80`–`82` (simulation
flow, no-look-ahead, prod attach, walk-forward protocol),
`01c_FORMULAS_AND_CONFIGS.md` §61–64 plus the frozen Modules 13–16
(scoring/ranking/outcome formulas), `01a_CORE_PRINCIPLES.md` (enums), and the
frozen Module 16 service (db_role guard, validate-before-IO, single-write
transaction, metadata discipline).

Code: `app/services/simulation/simulation_engine.py` (+ package `__init__.py`).
Tests: `tests/test_simulation_engine.py`.

## Public API

```python
class SimulationEngine:
    def __init__(self, db_manager=None) -> None: ...
    def run(
        self,
        sim_name: str,
        mode: str,                      # research | walk_forward | config_comparison
        start_date: date,
        end_date: date,
        config_ids: list[str],
        strategy_configs: dict[str, dict],
        db_role: str = "simulation",
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

- Always returns a `ServiceResult`. `rows_processed` equals
  `metadata["outcomes_written"]`.
- `run_id` minted (`uuid4`) when `None`, otherwise preserved.
- `db_manager` injectable; defaults (lazily) to `app.database.duckdb_manager`.
- `polars` / `duckdb` and the frozen Step 3/4/5 engines are imported lazily so
  the pure helpers and validation are unit-testable without those packages.

### Exact metadata keys

`db_role, mode, sim_name, run_id, start_date, end_date, config_ids, sim_dates,
folds, step3_rows, step4_rows, step5_rows, outcomes_written, comparisons_written`.

## Pre-DB validation (before any I/O)

`db_role == "simulation"`; `mode in {research, walk_forward, config_comparison}`;
non-empty `config_ids`; `strategy_configs` is a dict containing every
`config_id`; `start_date <= end_date`. Any failure returns `failed` with the
error and **no** `sim_runs` row.

## DB boundaries

- One simulation connection per run via
  `duckdb_manager.connect_simulation_with_prod()` (prod attached **read-only** as
  alias `prod`). The engine never opens the prod path directly.
- All production reads are qualified with the `prod.` alias; all writes target
  unqualified `sim_*` tables in `simulation.duckdb`.
- No direct `duckdb` import in this module, no provider imports/calls, no
  `print()`, no DDL/`ATTACH`, and no writes to prod/debug or Module 16 prod
  outcome tables.

## Run lifecycle

`sim_runs` is inserted `pending`, updated to `running` at job start (both
autocommitted), then all replay/outcome/comparison/fold writes run inside one
`BEGIN TRANSACTION`. On success the status is set `success` and the transaction
commits. On any failure the transaction is rolled back (removing partial `sim_*`
rows), `sim_runs.status` is set `failed` with the error string in `sim_runs.notes`,
and the original error is returned in `ServiceResult.errors`.

## No look-ahead (enforced in SQL)

For each replayed `sim_date` the screening / analysis reads enforce
`feature_cutoff_date <= sim_date` and `daily_prices.date <= sim_date` directly in
SQL (the price ceiling lives in the JOIN `ON` clause so left-join semantics are
preserved). Outcome reads are intentionally forward-looking (they realize future
prices) and are **not** subject to the `sim_date` ceiling. V1 uses precomputed
prod features only; live features are never recomputed inside the simulation.

## Step 3/4/5 replay (chosen approach)

The frozen Module 13–15 services write only to prod/debug tables, so they cannot
write `sim_*` and are not called as services. Instead the engine **reuses the
frozen pure scoring code directly** — `Step3ScreeningEngine._evaluate` /
`_build_rows` (Polars sub-score expressions + hard filters), the frozen Step 4
classification / stop / target / component-score / penalty functions via
`Step4AnalysisEngine._build_rows`, and the frozen Step 5 RR / raw-score /
hard-cap / soft-penalty / ranking logic via `Step5ProposalEngine._build_rows`.
This guarantees simulated scores are identical to production with **no formula
divergence**; the engine owns only the no-look-ahead sim-scoped reads and the
`sim_*` writes. Step 5 input (`screening_score`, `setup_score`, `timing_score`,
`estimated_rr`, sector/industry) is assembled in memory from the Step 3/4 results
plus `prod.ticker_master`, so no intermediate re-read is required. Candidate /
analysis / proposal / outcome / fold ids are `uuid4` (a simulation run writes
each row once).

## Outcomes & list membership

One `sim_signal_outcomes` row is created per proposal that is in the raw **or**
diversified Top-N (`in_raw_top_n OR in_diversified_top_n`); proposals in neither
list get no outcome. `entry_date = next_trading_day(sim_date)`;
`entry_price_raw = entry-date open_raw`;
`entry_price_sim = open_raw * (1 + slippage_bps/10000)` (validated via the frozen
Module 16 `_validate_config`). Returns, 40bd MFE/MAE and `realized_r_multiple`
use `entry_price_sim` as the denominator and preserve Module 16 missing-candle
semantics (a partial 40bd window yields `NULL` MFE/MAE; a `<= 0` R denominator or
missing exit yields `NULL` R). A missing entry candle skips the outcome (V1).
`outcome_status` is `complete` iff all four horizon returns resolve, else
`partial`. `list_membership` is `raw_only` / `diversified_only` / `both`.

## Config comparisons

One `sim_config_comparisons` row per `(config_id, horizon_bd, list_type)` for both
`raw` (`raw_only` + `both`) and `diversified` (`diversified_only` + `both`).
Metrics use **resolved** outcomes only (the per-horizon return is present);
unresolved outcomes count only in the `resolved_outcomes_pct` denominator.
`win_rate` / `resolved_outcomes_pct` are fractions in `[0, 1]`;
`max_drawdown_pct` is a percentage in `[0, 100]` (so it composes directly with the
walk-forward thresholds). For `walk_forward` mode only non-`cross_fold` outcomes
are aggregated.

## Walk-forward

Expanding window. The first test fold is the calendar quarter beginning at least
12 months after `start_date`; each subsequent quarter up to `end_date` is another
fold. Each fold's train window is `[start_date, test_start - 1 day]`. The selected
config maximizes train-window expectancy (diversified list, 40bd) subject to
`resolved_outcomes_pct >= 0.85` and `max_drawdown_pct <= 25` (ties broken by
`config_id` ascending; `None` when no config qualifies). An outcome's
`cross_fold_outcome` is `TRUE` when its 40bd eval date falls outside its signal's
test fold; such rows are excluded from fold training metrics. Fold metadata
(train/test bounds + `selected_config_id`) is written to `sim_folds`. The pure
planning / metric / selection helpers (`plan_walk_forward_folds`,
`compute_metrics`, `select_config_for_fold`) are module-level and unit-tested in
isolation.

**V1 walk-forward is Option B (replay-all).** V1 replays every requested config
across the full window for transparency; it records `selected_config_id` in
`sim_folds` per fold but does **not** restrict test-fold signal generation to the
selected config. Run-level `sim_config_comparisons` exclude `cross_fold_outcome`
rows in `walk_forward` mode. True selected-config walk-forward execution (Option A)
is deferred to a future module; any implementation must update this spec.

## Assumptions / open gaps

- **G-EXPECTANCY** — Project Files give the selection thresholds (`>= 0.85`,
  `<= 25`) but no closed-form expectancy. `expectancy` is the mean realized
  return per resolved outcome; `profit_factor = gross win / gross loss`
  (`NULL` with no losses); `max_drawdown_pct` is the peak-to-trough drawdown of
  the chronologically-compounded equity curve, in percent.
- **G-WF-SELECTION-HORIZON** — the longest horizon (40bd) is used for
  walk-forward training metrics and config selection so a fold's cross-fold spill
  is measured against the full realization window.
- **G-WF-CONFIG-RESTRICTION** — V1 replays every requested config across the
  whole window for transparency and records the per-fold `selected_config_id` in
  `sim_folds`; it does not yet *restrict* test-fold signal generation to the
  selected config. Run-level `sim_config_comparisons` exclude `cross_fold`
  outcomes in `walk_forward` mode. Restricting generation is left to a future
  module.
- **G-SIM-STOP-JOIN** — `realized_r_multiple` uses the `stop_price_raw` from the
  same sim_date Step 4 analysis for the ticker (one analysis per candidate in
  simulation). Revisit if a ticker can carry multiple Step 4 analyses per
  sim_date.
- **G-OUTCOME-IDS** — `sim_*` ids are `uuid4` (single-write per run) rather than
  the deterministic `uuid5` used by Module 16's idempotent prod queue; reruns use
  a fresh `run_id`.
- **G-FEATURE-VERSION** — the simulation selects the current feature schema
  version with `MAX(feature_schema_version)` over `prod.daily_features`, matching
  the `daily_features_current` view.

## Tests

`tests/test_simulation_engine.py` covers (offline, no real DB files): pre-DB
validation (bad `db_role`, bad `mode`, empty `config_ids`, missing config entry,
invalid date range) with `ServiceResult` + metadata-key assertions; `run_id`
mint/preserve; the fold plan; metric maths incl. unresolved denominator and
all-unresolved/empty groups; threshold-based config selection; outcome
construction via a fake connection (list membership, no outcome outside both
lists, `entry_price_sim`/returns/MFE/MAE/realized-R, missing-entry skip,
partial status, cross-fold boundary flagging); config-comparison raw/diversified
rows + metrics + walk-forward cross-fold exclusion; `sim_folds` selection/insert
and train-metric exclusions; **SQL parameter binding regressions** (placeholder
count assertions for `_SELECT_FEATURES_PRICES` / `_SELECT_PRIOR_10` and an
exact-param recording test via injected fake engines + recording connection);
**failure before transaction starts** (feature-version query fails → failed
`ServiceResult` with `sim_runs.status = 'failed'`); **duplicate `run_id`** (PK
conflict on `sim_runs` INSERT → failed `ServiceResult`, `run_id` preserved, no
raw exception); and static scans (no direct `duckdb`, no provider imports, no
`print()`, no DDL/`ATTACH` in SQL, writes only `sim_*`, prod reads
alias-qualified). Integration tests (auto-skipped without `duckdb` + `polars`)
run the full pipeline against real tmp `prod`/`simulation` DBs: research
end-to-end (`sim_runs` success + `sim_step3/4/5` + outcomes + 8 comparison rows),
two no-look-ahead exclusions, **write-failure rollback via clean `FailingConnection`
proxy** + `failed`/`notes`, **duplicate `run_id`** (two calls, second returns
failed `ServiceResult`), and a walk-forward smoke run.

Run:

```text
pytest -q tests/test_simulation_engine.py
pytest -q
```
