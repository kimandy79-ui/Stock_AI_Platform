# Claude Coding Prompt — Module 20: Pipeline Orchestrator

Use Project Instructions/Files. Use `00_PROJECT_FILE_MAP.md` only for targeted retrieval. Do not restate global rules. Token-saving output only: blocking issues, implementation summary, changed files, spec summary, tests, assumptions/gaps, commit message.

## Inputs / Scope

Base code: `stock_ai_platform_module19_ai_review_engine_stable.zip`.

Implement **Module 20 only** and create `M20_PIPELINE_ORCHESTRATOR_SPEC.md`.

Allowed file changes only:
```text
app/services/pipeline/__init__.py
app/services/pipeline/pipeline_orchestrator.py
tests/test_pipeline_orchestrator.py
M20_PIPELINE_ORCHESTRATOR_SPEC.md
README.md  # Module 20 note only
```

No other file may be modified.

## Public API

```python
class PipelineOrchestrator:
    def __init__(
        self,
        db_manager=None,
        provider=None,
        benchmark_loader=None,
        universe_engine=None,
        ingestion_engine=None,
        validation_engine=None,
        mutation_engine=None,
        feature_engine=None,
        screening_engine=None,
        analysis_engine=None,
        proposal_engine=None,
        outcome_creator=None,
        outcome_processor=None,
    ) -> None: ...

    def run(
        self,
        run_date: date,
        run_type: str = "scheduled",
        db_role: str = "prod",
        force_rerun: bool = False,
        resume_from: str | None = None,
        strategy_configs: dict[str, dict] | None = None,
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

Hard rules:
- Always return `ServiceResult`; never raise for expected validation/DB/step failures.
- Mint `uuid4()` `run_id` only when `None`; preserve supplied value verbatim.
- Instantiate default real dependencies in `__init__` only, using injected `db_manager` where applicable. Tests inject fakes.
- `strategy_configs=None` → `DEFAULT_STRATEGY_CONFIGS`.

## Constants / Validation

```python
STEP_NAMES: Final[tuple[str, ...]] = (
    "benchmark_etf_ingestion",
    "universe_ingestion",
    "price_ingestion",
    "validation",
    "mutation_detection",
    "feature_calculation",
    "step3_screening",
    "step4_analysis",
    "step5_proposals",
    "outcome_queue_creation",
    "outcome_processing",
    "dashboard_materialization",
    "backup",
)
PIPELINE_LOCK_NAME: Final[str] = "daily_pipeline"
LOCK_STALE_SECONDS: Final[int] = 300
DEFAULT_STRATEGY_CONFIGS: Final[dict[str, dict]] = {...}
```

`DEFAULT_STRATEGY_CONFIGS` must contain `normal`, `aggressive`, `conservative` with the full config shapes required by frozen step engines (`universe`, `screening`, `scoring_weights`, `diversification`, `market_regime`, etc.). Retrieve from `01c_FORMULAS_AND_CONFIGS.md` / `settings.py` as needed. Document **G-STRATEGY-CONFIGS**: V1 hardcoded; future DB/config-file source.

Pre-DB validation: fail before any DB connection if:
- `run_type` not in `scheduled`, `manual`, `force_rerun`, `catchup`, `debug`
- `db_role` not in `prod`, `debug`
- `resume_from` not in `STEP_NAMES` and not `None`
- `strategy_configs` is not a non-empty dict after defaulting

## DB Protocol

All DB access goes through injected `db_manager` or approved default. No direct `duckdb` import.

### Lock acquire
1. Read `pipeline_locks WHERE lock_name = 'daily_pipeline'` using read-only connection.
2. If locked and `heartbeat_at >= now - 300s`: return failed:
   ```text
   pipeline is already running (lock_run_id=<run_id>, heartbeat_at=<ts>)
   ```
   Do not insert/update `pipeline_runs`.
3. If locked but stale: log warning with stale `run_id`; overwrite lock.
4. Upsert lock using write connection:
```sql
INSERT INTO pipeline_locks
    (lock_name, is_locked, run_id, locked_at, heartbeat_at)
VALUES ('daily_pipeline', TRUE, ?, CAST(now() AS TIMESTAMP), CAST(now() AS TIMESTAMP))
ON CONFLICT (lock_name) DO UPDATE SET
    is_locked = TRUE,
    run_id = EXCLUDED.run_id,
    locked_at = EXCLUDED.locked_at,
    heartbeat_at = EXCLUDED.heartbeat_at
```

### Already-run check
After lock acquire and before `pipeline_runs` insert:
```sql
SELECT run_id, status FROM pipeline_runs
WHERE run_date = ? AND status IN ('success', 'success_with_warnings')
LIMIT 1
```
If row exists and `force_rerun=False`: release lock and return failed:
```text
run_date already succeeded (prev_run_id=<id>, status=<s>)
```
If `force_rerun=True`: log and continue.

### pipeline_runs lifecycle
Insert running row:
```sql
INSERT INTO pipeline_runs
    (run_id, run_date, run_type, status, started_at, steps_completed, error_message, created_at)
VALUES (?, ?, ?, 'running', CAST(now() AS TIMESTAMP), '[]', NULL, CAST(now() AS TIMESTAMP))
```

After each executed step:
```sql
UPDATE pipeline_runs SET steps_completed = ? WHERE run_id = ?
```
`steps_completed` = JSON array (`json.dumps`) of step names executed in this run only.

Heartbeat immediately after recording each completed step:
```sql
UPDATE pipeline_locks
SET heartbeat_at = CAST(now() AS TIMESTAMP)
WHERE lock_name = 'daily_pipeline'
```
Document **G-HEARTBEAT-THREADING**: V1 inline after each step; future background heartbeat.

Final success:
```sql
UPDATE pipeline_runs
SET status = ?, completed_at = CAST(now() AS TIMESTAMP), duration_sec = ?, steps_completed = ?
WHERE run_id = ?
```
`status` = `success` unless any warning/recoverable failure occurred, then `success_with_warnings`.

Critical failure:
```sql
UPDATE pipeline_runs
SET status = 'failed', completed_at = CAST(now() AS TIMESTAMP), duration_sec = ?, error_message = ?
WHERE run_id = ?
```

Release lock in `finally` after pipeline body, success or failure:
```sql
UPDATE pipeline_locks
SET is_locked = FALSE, run_id = NULL
WHERE lock_name = 'daily_pipeline'
```
If release fails, log it but preserve original result.

## Step Execution

Resume: if `resume_from` is set, skip earlier steps by index. Skipped steps are logged only; do **not** store skipped names in DB/result `steps_completed`.

Failure policy:
```text
critical: benchmark_etf_ingestion, price_ingestion, validation, feature_calculation,
          step3_screening, step4_analysis, step5_proposals, outcome_queue_creation
recoverable: universe_ingestion, mutation_detection, outcome_processing,
             dashboard_materialization, backup
```

Any step returning `success_with_warnings`: collect warning, continue, final status `success_with_warnings`.

Exact calls:
```python
benchmark_loader.load(provider=provider, start_date=run_date, end_date=run_date, db_role=db_role, run_id=run_id)

symbol_result = provider.list_symbols(symbol_type="stock")
entries = symbol_result.metadata.get("symbols", [])
universe_engine.apply_snapshot(entries=entries, as_of_date=run_date, db_role=db_role, source="yahoo", run_id=run_id)

ingestion_engine.ingest(provider=provider, start_date=run_date, end_date=run_date, db_role=db_role, run_id=run_id)
validation_engine.validate(start_date=run_date, end_date=run_date, db_role=db_role, run_id=run_id)
mutation_engine.detect(start_date=run_date, end_date=run_date, db_role=db_role, run_id=run_id)
feature_engine.calculate(start_date=run_date, end_date=run_date, tickers=None, db_role=db_role, run_id=run_id)

for config_id, config_dict in strategy_configs.items():
    screening_engine.screen(signal_date=run_date, strategy_config=config_dict, strategy_config_id=config_id, db_role=db_role, run_id=run_id)
    analysis_engine.analyze(signal_date=run_date, strategy_config=config_dict, strategy_config_id=config_id, db_role=db_role, run_id=run_id)
    proposal_engine.propose(signal_date=run_date, strategy_config=config_dict, strategy_config_id=config_id, db_role=db_role, run_id=run_id)
    outcome_creator.enqueue(signal_date=run_date, strategy_config_id=config_id, strategy_config=config_dict, db_role=db_role, run_id=run_id)
    outcome_processor.process(run_date=run_date, strategy_config=config_dict, db_role=db_role, run_id=run_id)
```

Dashboard materialization: V1 no-op. Log:
```text
dashboard materialization skipped (G-DASHBOARD-MAT: Module 21 not yet implemented)
```

Backup: best-effort/recoverable. Copy current role DB file to `settings.BACKUPS_DIR` using `shutil.copy2`; monkeypatch file copy in tests. For `prod`, use `settings.PROD_DB_PATH`; for `debug`, use `settings.DEBUG_DB_PATH`. Filename:
```python
f"{db_role}_{run_date.isoformat()}_{run_id[:8]}.duckdb"
```

## ServiceResult Contract

Exact metadata keys on every path (`None` if unavailable):
```text
run_id, run_date, run_type, db_role, steps_completed, failed_step, error, duration_sec, status
```

Values:
- `run_date`: `run_date.isoformat()`
- `steps_completed`: executed step-name strings only
- `failed_step`: critical failed step name or `None`
- `status`: `success`, `success_with_warnings`, or `failed`
- `duration_sec`: float elapsed seconds, or `None` if unavailable

## Boundaries

- No direct `duckdb` import; no `print()`.
- No DDL/`ATTACH` in executed SQL.
- This module mutates only `pipeline_runs` and `pipeline_locks`; it never writes directly to step-engine tables.
- No simulation DB writes.
- No market-data logic; only call injected `provider`.
- Step engines instantiated in `__init__`, not inside step methods.
- Backup failure is recoverable.

## Targeted Retrieval

- `pipeline_runs` / `pipeline_locks`: `01b_SCHEMA_AND_DATA.md`, `M02_SCHEMA_SPEC.md` §3.2/§3.3
- `run_type` / `run_status`: `01a_CORE_PRINCIPLES.md`
- Step order/failure modes: `01d_MODULES_AND_PIPELINE.md` §70/§72
- Lock/heartbeat/resume: `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §6/§20
- Step signatures: `M07`, `M06`, `M08`, `M09`, `M10`, `M11`, `M13`, `M14`, `M15`, `M16` specs
- `ServiceResult`: `app/utils/service_result.py`
- Strategy/path constants: `01c_FORMULAS_AND_CONFIGS.md`, `app/config/settings.py`

## Tests Required

Create `tests/test_pipeline_orchestrator.py`. Fully offline: fake/monkeypatch every engine, provider, db_manager, and file copy. No real DuckDB/provider/file copy.

Cover:
1. `ServiceResult`, run_id mint/preserve, exact metadata keys on success/failure.
2. Pre-DB validation no-I/O for invalid `run_type`, `db_role`, `resume_from`, empty `strategy_configs`.
3. Active non-stale lock → failed, no `pipeline_runs` insert.
4. Stale lock override → warning logged, lock overwritten, proceeds.
5. Already-run block with `force_rerun=False` → lock released, failed.
6. Already-run with `force_rerun=True` → proceeds.
7. Happy path → calls in exact order, all 13 steps completed, status `success`, lock released.
8. `resume_from` → earlier steps skipped and absent from `steps_completed`; remaining order correct.
9. Critical failure at `price_ingestion` → stop, status `failed`, failed_step set, later steps not called, lock released.
10. Recoverable failure at `universe_ingestion` → continue, final `success_with_warnings`, warning present.
11. Step `success_with_warnings` propagates to final status.
12. Backup failure recoverable.
13. Strategy loop: with 2 configs, steps 7–11 each called twice in config order.
14. Static scans: no `duckdb` import, no `print`, no DDL/`ATTACH`, only `pipeline_runs`/`pipeline_locks` as INSERT/UPDATE targets.

Run:
```bash
pytest -q tests/test_pipeline_orchestrator.py
pytest -q
```

## Required Spec: `M20_PIPELINE_ORCHESTRATOR_SPEC.md`

Include only module-specific content:
- Public API.
- `STEP_NAMES` / ordering.
- Pre-DB validation.
- Lock acquire/heartbeat/release SQL.
- Already-run SQL.
- `pipeline_runs` lifecycle SQL.
- Step call/failure-classification table.
- `DEFAULT_STRATEGY_CONFIGS` and gaps: `G-STRATEGY-CONFIGS`, `G-HEARTBEAT-THREADING`, `G-DASHBOARD-MAT`, `G-UNIVERSE-PROVIDER`.
- ServiceResult metadata contract.
- Boundaries.
- Test summary.

## Final Output

Return only:
1. Updated project zip.
2. Added/changed files.
3. Spec summary.
4. Short design notes.
5. Test commands/results.
6. Assumptions/open gaps.
7. Commit message: `module20_pipeline_orchestrator_stable`
