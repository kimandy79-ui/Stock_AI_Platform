# Claude Coding Prompt — Module 17: Simulation Engine

Use Project Instructions and Project Files. Use `00_PROJECT_FILE_MAP.md` only for targeted retrieval. Do not restate global rules.

## Inputs

- Base code: `stock_ai_platform_module16_outcome_queue_stable.zip`
- No separate Module 17 spec is provided. Create `M17_SIMULATION_ENGINE_SPEC.md` from Project Files, frozen Module 16 style, and this prompt.

## Task

Implement **Module 17 — Simulation Engine** only. Modules 01–16 are accepted/frozen. Do not implement Module 18+. Do not modify frozen modules unless required by a failing test or real integration blocker; document any such change.

## Files

```text
app/services/simulation/__init__.py
app/services/simulation/simulation_engine.py
tests/test_simulation_engine.py
M17_SIMULATION_ENGINE_SPEC.md
README.md                         # add Module 17 note only
```

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

Requirements:
- Always return `ServiceResult`.
- Mint `run_id = uuid4()` when `None`; preserve supplied `run_id`.
- Inject `db_manager`; default to `app.database.duckdb_manager`.
- Validate before DB access: `db_role == "simulation"`; valid `mode`; non-empty `config_ids`; every `config_id` exists in `strategy_configs`; `start_date <= end_date`.

Allowed modes: `research`, `walk_forward`, `config_comparison`.

## Hard boundaries

- Read prod only via `duckdb_manager.connect_simulation_with_prod` or `attach_prod_read_only` on a simulation connection.
- Never open prod DB path directly.
- Write only to `sim_*` tables in `simulation.duckdb`.
- Never write to prod/debug tables.
- No provider calls, direct `duckdb` import in service code, `print()`, or production DDL.
- Do not mutate Module 16 prod outcome tables.

## Core logic

### No look-ahead

For each replayed `sim_date`, enforce in SQL, not Python post-filter: `feature_cutoff_date <= sim_date` and `daily_prices.date <= sim_date`.

V1 uses precomputed prod features only. Do not recalculate live features inside simulation.

### Step 3 / 4 / 5 replay

Populate `sim_step3_candidates`, `sim_step4_analysis`, `sim_step5_proposals`.

Use the same formulas/rules as frozen Modules 13–15. Prefer calling frozen services if they can safely write to simulation tables; otherwise mirror accepted logic without divergence. Document the chosen approach in the spec.

### Outcomes

Populate `sim_signal_outcomes` using Module 16 outcome rules adapted to simulation tables:
- use `entry_price_sim` as denominator for returns, MFE/MAE, realized R;
- preserve horizon and missing-candle semantics;
- create outcomes only for proposals in raw or diversified lists;
- compute `list_membership`:
  - `raw_only`: raw TRUE, diversified FALSE
  - `diversified_only`: raw FALSE, diversified TRUE
  - `both`: both TRUE

### Config comparisons

Populate `sim_config_comparisons`: one row per `(config_id, horizon_bd, list_type)` for both `raw` and `diversified`.

Compute separately by list type: expectancy, win_rate, avg_win, avg_loss, profit_factor, max_drawdown_pct, resolved_outcomes_pct.

Metrics use resolved outcomes only; unresolved rows count only in `resolved_outcomes_pct` denominator.

### Walk-forward mode

For `mode = "walk_forward"`:
- expanding train window;
- minimum 12-month initial train window;
- calendar-quarter test folds;
- select config per fold by max train-window expectancy subject to `resolved_outcomes_pct >= 0.85` and `max_drawdown_pct <= 25`;
- set `cross_fold_outcome = TRUE` when `eval_date` falls outside that fold’s test period;
- exclude `cross_fold_outcome = TRUE` rows from fold metrics;
- store fold metadata in `sim_folds`.

### Run lifecycle

- Insert `sim_runs` as `pending`.
- Update to `running` at job start.
- Update to `success` on completion.
- On failure: rollback partial writes where possible, set `status = 'failed'`, write error to `sim_runs.notes`, and return the original error in `ServiceResult`.

## Source retrieval map

Retrieve only as needed:
- schemas: `01b_SCHEMA_AND_DATA.md`, `M02_SCHEMA_SPEC.md` §4
- simulation/no-look-ahead/prod attach: `01d_MODULES_AND_PIPELINE.md` (`SIMULATION/80`, `81`, `82`)
- enums: `01a_CORE_PRINCIPLES.md`
- Step 3/4/5 formulas: `01c_FORMULAS_AND_CONFIGS.md` §61–63; frozen Modules 13–15
- outcome formulas: `01c_FORMULAS_AND_CONFIGS.md` §64; `M16_OUTCOME_QUEUE_SPEC.md`
- DB helpers: `app/database/duckdb_manager.py`
- Module 16 patterns: `app/services/outcomes/outcome_queue.py`, `tests/test_outcome_queue.py`

## Tests

All tests offline with `tmp_path` / injected fake DB manager; never touch real DB files.

Cover:
- public API and `ServiceResult` on all paths;
- `run_id` mint/preserve;
- pre-DB validation: bad `db_role`, bad `mode`, empty `config_ids`, missing config entry, invalid date range;
- `sim_runs` lifecycle success/failure with notes;
- no-look-ahead SQL excludes future feature/price rows;
- Step 3/4/5 sim table writes;
- `list_membership` = `raw_only`, `diversified_only`, `both`;
- no outcome outside both lists;
- config comparison rows for raw/diversified and metric calculations;
- unresolved denominator behavior;
- walk-forward folds, threshold-based config selection, `cross_fold_outcome` boundary flagging, metric exclusion;
- write-failure rollback;
- static scans: no direct `duckdb` import, provider imports/calls, `print()`, prod/debug writes, or production DDL.

Run:
```text
pytest -q tests/test_simulation_engine.py
pytest -q
```

## `M17_SIMULATION_ENGINE_SPEC.md`

Include: API, DB boundaries, lifecycle, no-look-ahead, Step 3/4/5 replay, outcomes/list membership, metrics, walk-forward, assumptions, tests.

## Output

Work silently. Return only:
1. Updated project zip.
2. Added/changed files.
3. `M17_SIMULATION_ENGINE_SPEC.md` summary.
4. Short design notes.
5. Test commands/results.
6. Assumptions/open gaps.
7. Suggested commit message: `module17_simulation_engine_stable`
