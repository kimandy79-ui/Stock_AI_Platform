# M20 — Pipeline Orchestrator Spec

Module 20 coordinates one daily pipeline run end to end. It is a **control-plane
layer only**: it owns the `daily_pipeline` lock, the `pipeline_runs` lifecycle
row, and the *ordering* of the frozen step engines. It performs no market-data,
screening, proposal, outcome, or dashboard logic itself — every domain action is
delegated to an injected engine that returns a `ServiceResult`.

Source of truth for the contracts below: `M02_SCHEMA_SPEC.md` §3.2 / §3.3
(`pipeline_runs` / `pipeline_locks`), `01a_CORE_PRINCIPLES.md` (`run_type` /
`run_status` enums), `01d_MODULES_AND_PIPELINE.md` §70 / §72 (step order and
failure modes), `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §6 / §20 (lock /
heartbeat / resume), the per-engine specs (step signatures),
`01c_FORMULAS_AND_CONFIGS.md` + `app/config/settings.py`
(`DEFAULT_STRATEGY_CONFIGS`, path constants), and
`app/utils/service_result.py`.

## 1. Public API

```python
PipelineOrchestrator(
    db_manager=None, provider=None, benchmark_loader=None,
    universe_engine=None, ingestion_engine=None, validation_engine=None,
    mutation_engine=None, feature_engine=None, screening_engine=None,
    analysis_engine=None, proposal_engine=None, outcome_creator=None,
    outcome_processor=None,
)

PipelineOrchestrator.run(
    run_date: date,
    run_type: str = "scheduled",
    db_role: str = "prod",
    force_rerun: bool = False,
    resume_from: str | None = None,
    strategy_configs: dict[str, dict] | None = None,
    run_id: str | None = None,
) -> ServiceResult
```

Every dependency is injected for testability; when omitted, the real default is
constructed **in `__init__` only** (never inside a step). `db_manager` defaults
to the approved `app.database.duckdb_manager` module; `provider` defaults to
`YahooProvider()`; each engine defaults to its class constructed with
`db_manager=self._db`. `run()` mints a UUID4 `run_id` when none is supplied (and
preserves a supplied one), and defaults `strategy_configs` to
`DEFAULT_STRATEGY_CONFIGS`. `run()` **always returns a `ServiceResult`**;
expected validation, lock, already-run, and step failures are reported as
`failed` / `success_with_warnings` results, never raised.

## 2. Step order (`STEP_NAMES`) and failure classification

| # | Step name | Engine call | Class |
|---|-----------|-------------|-------|
| 1 | `benchmark_etf_ingestion` | `benchmark_loader.load(provider, start_date, end_date, db_role, run_id)` | critical |
| 2 | `universe_ingestion` | `provider.list_symbols(symbol_type="stock")` → `universe_engine.apply_snapshot(entries, as_of_date, db_role, source="yahoo", run_id)` | recoverable |
| 3 | `price_ingestion` | `ingestion_engine.ingest(provider, start_date, end_date, db_role, run_id)` | critical |
| 4 | `validation` | `validation_engine.validate(start_date, end_date, db_role, run_id)` | critical |
| 5 | `mutation_detection` | `mutation_engine.detect(start_date, end_date, db_role, run_id)` | recoverable |
| 6 | `feature_calculation` | `feature_engine.calculate(start_date, end_date, tickers=None, db_role, run_id)` | critical |
| 7 | `step3_screening` | `screening_engine.screen(signal_date, strategy_config, strategy_config_id, db_role, run_id)` | critical |
| 8 | `step4_analysis` | `analysis_engine.analyze(signal_date, strategy_config, strategy_config_id, db_role, run_id)` | critical |
| 9 | `step5_proposals` | `proposal_engine.propose(signal_date, strategy_config, strategy_config_id, db_role, run_id)` | critical |
| 10 | `outcome_queue_creation` | `outcome_creator.enqueue(signal_date, strategy_config_id, strategy_config, db_role, run_id)` | critical |
| 11 | `outcome_processing` | `outcome_processor.process(run_date, strategy_config, db_role, run_id)` | recoverable |
| 12 | `dashboard_materialization` | V1 no-op (G-DASHBOARD-MAT) | recoverable |
| 13 | `backup` | `shutil.copy2(<role db path>, BACKUPS_DIR/<role>_<run_date>_<run_id[:8]>.duckdb)` | recoverable |

The order mirrors `01d_MODULES_AND_PIPELINE.md` §70. Steps 7–11 execute in
**step-major** order so that each logical step is recorded in `pipeline_runs`
only after all configured strategies have completed it:

1. Run `step3_screening` for **all** strategy configs → record `step3_screening`.
2. Run `step4_analysis` for **all** strategy configs → record `step4_analysis`.
3. Run `step5_proposals` for **all** strategy configs → record `step5_proposals`.
4. Run `outcome_queue_creation` for **all** strategy configs → record `outcome_queue_creation`.
5. Run `outcome_processing` for **all** strategy configs → record `outcome_processing`
   (recoverable; failures become warnings, execution continues to the next step).

This guarantees correct resume/progress semantics (`01d §72`): if a run fails
at `step4_analysis` for any config, `step3_screening` is in `steps_completed`
(all configs finished it) while `step4_analysis` and all later steps are absent.
A **critical** failure on any config for a step aborts the run immediately,
sets `failed_step`, and writes `pipeline_runs.status = 'failed'`. A
**recoverable** failure logs a warning, degrades the result to
`success_with_warnings`, and continues to the next logical step.

## 3. Pre-DB input validation (no I/O)

Before any connection is opened, `run()` validates: `run_type ∈ {scheduled,
manual, force_rerun, catchup, debug}`; `db_role ∈ {prod, debug}`; `resume_from
∈ STEP_NAMES ∪ {None}`; `strategy_configs` is a non-empty dict. On any failure
it returns a `failed` `ServiceResult` **without touching the database**
(verified by test: `db_manager.connect` is never called).

## 4. Lock acquire / heartbeat / release

Lock name: `daily_pipeline`. Stale threshold: **300 s** (`02 §20`).

* **Read** (read-only connection):
  `SELECT run_id, is_locked, heartbeat_at FROM pipeline_locks WHERE lock_name = ?`
* **Active, non-stale** (`is_locked` and `now − heartbeat_at ≤ 300 s`): return
  `failed` with message `pipeline is already running (lock_run_id=…,
  heartbeat_at=…)`; **no** `pipeline_runs` insert, **no** release (the lock is
  not ours).
* **Stale or missing heartbeat**: log a warning and overwrite via the upsert.
* **Acquire / override (upsert)**:
  `INSERT INTO pipeline_locks (lock_name, is_locked, run_id, locked_at,
  heartbeat_at) VALUES ('daily_pipeline', TRUE, ?, CAST(now() AS TIMESTAMP),
  CAST(now() AS TIMESTAMP)) ON CONFLICT (lock_name) DO UPDATE SET is_locked =
  TRUE, run_id = EXCLUDED.run_id, locked_at = EXCLUDED.locked_at, heartbeat_at =
  EXCLUDED.heartbeat_at`
* **Heartbeat** (after recording each completed step):
  `UPDATE pipeline_locks SET heartbeat_at = CAST(now() AS TIMESTAMP) WHERE
  lock_name = 'daily_pipeline'`
* **Release** (always, in `finally`, once the lock is held):
  `UPDATE pipeline_locks SET is_locked = FALSE, run_id = NULL WHERE lock_name =
  'daily_pipeline'`. Release logs but never raises on failure.

## 5. Already-run guard

`SELECT run_id, status FROM pipeline_runs WHERE run_date = ? AND status IN
('success', 'success_with_warnings') LIMIT 1`. If a prior successful run exists
and `force_rerun` is `False`: return `failed` with message `run_date already
succeeded (prev_run_id=…, status=…)` (the lock is released in `finally`). If
`force_rerun` is `True`: log and continue.

## 6. `pipeline_runs` lifecycle

* **Insert (running)**: `INSERT INTO pipeline_runs (run_id, run_date, run_type,
  status, started_at, steps_completed, error_message, created_at) VALUES (?, ?,
  ?, 'running', CAST(now() AS TIMESTAMP), '[]', NULL, CAST(now() AS
  TIMESTAMP))`
* **After each executed step**: `UPDATE pipeline_runs SET steps_completed = ?
  WHERE run_id = ?` (JSON array), immediately followed by the lock heartbeat.
* **Finalize success**: `UPDATE pipeline_runs SET status = ?, completed_at =
  CAST(now() AS TIMESTAMP), duration_sec = ?, steps_completed = ? WHERE run_id =
  ?`
* **Finalize failure**: `UPDATE pipeline_runs SET status = 'failed',
  completed_at = CAST(now() AS TIMESTAMP), duration_sec = ?, error_message = ?
  WHERE run_id = ?`

All run/lock SQL is parameterized, targets **only** `pipeline_runs` /
`pipeline_locks`, and contains **no DDL / `ATTACH`**. Each DB operation opens,
executes, and closes its own connection through the injected `db_manager`, so
the orchestrator never holds a writer open while an engine runs (DuckDB
single-writer safety).

### `steps_completed` semantics

`steps_completed` lists the steps **executed** in this run: every non-skipped
step the pipeline ran and moved past. Recoverably-failed steps **are** recorded
(the pipeline progressed past them with a warning). A critically-failed step is
**not** recorded (the run aborts at it). Steps skipped via `resume_from` are not
recorded.

## 7. `ServiceResult` metadata contract

Every return path produces `metadata` with exactly these nine keys: `run_id`,
`run_date` (ISO string), `run_type`, `db_role`, `steps_completed` (list),
`failed_step` (str or `None`), `error` (str or `None`), `duration_sec` (float),
`status`. `rows_processed` is the number of completed steps; `errors` carries
the fatal message when failed.

## 8. `DEFAULT_STRATEGY_CONFIGS`

Three presets — `normal`, `aggressive`, `conservative` — each a full
strategy-config dict accepted by every frozen engine validator (Step 3/4/5 and
the outcome-queue creator). Shapes are the canonical
`01c_FORMULAS_AND_CONFIGS.md` preset JSON (`universe`, `features`, `screening`,
`scoring_weights`, `market_regime`, `diversification`, `sector_etf_mapping`,
`simulation`, `macro_event_risk`, `earnings`) augmented with the two
engine-required keys the 01c JSON omits (see G-STRATEGY-CONFIGS).

## 9. Boundaries

* No direct `duckdb` import; all DB access flows through the injected
  `db_manager` / approved `duckdb_manager`.
* No `print()` — logging only, via `logging_config.get_logger(__name__,
  run_id)`.
* No DDL / `ATTACH`; no schema creation or migration.
* No simulation-DB writes (`db_role ∈ {prod, debug}` only).
* No market-data, screening, proposal, outcome, or dashboard logic — delegated
  to engines.
* Step engines and the provider are constructed only in `__init__`.

## 10. Gaps / assumptions

* **G-STRATEGY-CONFIGS** — `DEFAULT_STRATEGY_CONFIGS` is V1-hardcoded. The
  canonical 01c preset JSON omits two keys the frozen engines require, so they
  are supplied here: `step4.target_R` (01c §222–224: normal 2.2 / aggressive
  1.8 / conservative 2.8) and `diversification.top_n` (M15 §174 requires
  `int > 0`; 01c provides no value, defaulted to **10**). The legacy
  `diversification.sector_max_positions` / `industry_max_positions` names are
  kept verbatim from 01c; the Step 5 engine normalises them to
  `max_sector_count` / `max_industry_count`. Future work: load configs from the
  DB / a config file rather than hardcoding.
* **G-HEARTBEAT-THREADING** — the heartbeat is written inline after each
  completed step (V1). A long-running step cannot refresh the heartbeat
  mid-execution; future work is a background heartbeat thread on the 60 s
  interval from `02 §20`.
* **G-DASHBOARD-MAT** — dashboard materialization (step 12) is a logged no-op;
  Module 21 (Streamlit dashboard) is not yet implemented.
* **G-UNIVERSE-PROVIDER** — the default `YahooProvider` has no `symbol_source`,
  so `list_symbols` returns an empty symbol set with a warning (full universe
  construction is deferred to Module 06). In production a provider with a symbol
  source must be injected for `universe_ingestion` to populate tickers.

## 11. Test summary

`tests/test_pipeline_orchestrator.py` runs fully offline (all engines, the
provider, and the DB manager are faked; the backup file copy and `settings`
paths are monkeypatched). It covers: the `ServiceResult` + exact-metadata +
run_id mint/preserve contract; pre-DB validation with zero I/O; the
active-lock / stale-override / missing-heartbeat lock paths; the already-run
guard with and without `force_rerun`; the happy-path call order and full
`steps_completed`; `resume_from` skipping (linear and within the strategy
block); critical vs. recoverable failure (including engines that raise);
`success_with_warnings` propagation; recoverable backup failure; the step-major
strategy loop and its abort-on-critical behavior; and the static boundaries
(no `import duckdb`, no `print()`, no DDL/`ATTACH`, only pipeline tables as
write targets).
