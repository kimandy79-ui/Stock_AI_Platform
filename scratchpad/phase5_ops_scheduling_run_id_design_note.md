# Phase 5 — Operations (Scheduling + Run-ID Correlation): Design Note

## Task 1 — Headless scheduling

**No code changes to `run_prod_pipeline.py`.** Read the full script:

- No `input()`, no interactive-console dependency, no session-scoped
  assumption. `print()` calls are the tool's own operator-facing status
  output (a CLI-tool convention per `tools/*.py`, not a "no `print()` in
  service/library/provider/database modules" violation) — these work fine
  redirected to a log file under Task Scheduler.
- `--date` defaults to `date.today()` when omitted, `--run-type` defaults
  to `"manual"` (correct for an ad-hoc operator invocation, per the script's
  own documented usage example). The Task Scheduler action explicitly passes
  `--run-type scheduled` — no script default needed to change, since the
  scheduled action supplies its own explicit flag.
- Did not find any "activity simulator" script/artifact anywhere in the
  repo (checked for `.bat`/`.ps1`/`.vbs` files, mouse-jiggler/keep-alive
  references, `schtasks` mentions) — whatever kept the process
  alive/triggered previously was evidently an external/manual workaround
  outside the codebase, not something to delete from the repo.

**Deliverable:** `ops/StockAIPlatform_DailyPipeline.xml` (an importable
Task Scheduler task definition) + `ops/README_task_scheduler_setup.md`
(import steps + manual verification checklist). Key settings, all
deliberate:

- `LogonType = Password`, `RunLevel = LeastPrivilege` — "run whether user is
  logged on or not" requires a stored password (Windows won't run headless
  network-capable tasks with `S4U`/no-password when nobody's logged in).
  The XML cannot embed a real password (nobody should type one into a file
  I write); Windows prompts for it interactively during import in the GUI,
  or via `schtasks /RP *` (interactive prompt, never a CLI argument) — I
  never see or handle the actual credential.
- `CalendarTrigger` / `ScheduleByDay DaysInterval=1` at **23:30 local time**
  (user-specified — after market close, after Yahoo/EDGAR/Stooq end-of-day
  data has settled).
- `WakeToRun = true` so a sleeping machine wakes for the trigger.
- `MultipleInstancesPolicy = IgnoreNew` — if a prior run is still executing
  when the next trigger fires, the new instance is skipped rather than
  running concurrently (DuckDB single-writer discipline; this is a
  scheduler-level guard *in addition to*, not instead of, the existing
  `pipeline_locks` protocol).
- Action: `.venv\Scripts\python.exe tools\run_prod_pipeline.py --run-type
  scheduled`, working directory the repo root — no `--date` (defaults to
  today, correct for a daily trigger), no `--force-rerun`/`--resume-from`
  (non-goal: don't change these semantics).

**Not executed as part of this change:** actually registering the task on
this machine (`schtasks /create` or GUI import) modifies real OS state, so
it wasn't done automatically — it's a deliberate operator action documented
in the README's verification checklist, not yet performed. The already-run
guard behavior described there (`_already_run` blocking a same-day
re-trigger) was re-confirmed by reading `pipeline_orchestrator.py`
directly (unchanged, pre-existing, orthogonal to `run_type`) rather than by
actually firing a duplicate scheduled trigger — that live check is left for
the operator per the README.

## Task 2 — run_id correlation audit

**Finding: no gap.** Every path the note asked about already threads a
single `run_id` correctly, verified by reading (not assuming) each site:

- **`pipeline_runs`**: `_INSERT_RUNNING` / `_UPDATE_STEPS` / `_UPDATE_SUCCESS`
  all bind the orchestrator's own `run_id` as a SQL parameter.
- **Step-level logging**: `run()` calls `logging_config.get_logger(__name__,
  run_id)` exactly once and threads the resulting adapter (`log`) into every
  `_step_*` method as a parameter — none of the fourteen `_step_*` methods
  re-derives its own logger with a different run_id.
- **Every step engine call** (`_step_benchmark` through `_step_dashboard`,
  including the Phase 4 `_step_fundamentals`) explicitly passes
  `run_id=run_id` to the underlying engine (`benchmark_loader.load(...)`,
  `universe_engine.apply_snapshot(...)`, `ingestion_engine.ingest(...)`,
  `validation_engine.validate(...)`, `mutation_engine.detect(...)`,
  `feature_engine.calculate(...)`, `regime_engine.classify(...)`,
  `eligibility_engine.run(...)`, `setup_validation_engine.run(...)`,
  `proposal_engine.propose(...)`, `outcome_creator.enqueue(...)`,
  `outcome_processor.process(...)`) — confirmed via direct grep of every
  `_step_*` body, not assumed.
- **Every one of those engines** follows the identical, pre-established
  codebase convention: `run_id = run_id if run_id is not None else
  str(uuid.uuid4())` (or the `run_id or str(uuid.uuid4())` variant), then
  returns that same value as its own `ServiceResult.run_id` — confirmed by
  grep across `benchmark_etf_loader.py`, `universe_snapshot.py`,
  `daily_price_ingestion.py`, `data_validator.py`, `mutation_detector.py`,
  `feature_engine.py`, `market_regime_engine.py`,
  `step3_universal_eligibility.py`, `step4_setup_validation_engine.py`,
  `step5_proposal_engine.py`, `outcome_queue.py`. This is the same pattern
  the coder note itself anticipated ("most do").
- **`pipeline_run_diagnostics`** (M22 `funnel_diagnostics.py`): the
  orchestrator's `_run_diagnostics` passes `run_id=run_id` to
  `SetupModeFunnelDiagnosticsService.run(...)`, which threads it through
  `_collect_metrics(..., run_id, ...)` into `params = [run_id, sd_iso]` used
  for every step3/step4/step5 read query, and into every `_row(run_id, ...)`
  call that becomes an `INSERT INTO pipeline_run_diagnostics` row.
  (Separately noted: `_SQL_RESOLVE_RUN_ID` in the same file belongs to a
  distinct, read-only `build_report` convenience function used for ad hoc
  "look up the report for this signal_date without knowing run_id" tooling
  queries — it is not on the orchestrator's write path and is not a
  correlation gap.)
- **Provider-level `run_id`s are a distinct, lower-level concept and
  correctly don't need to match**: e.g. `YahooProvider.get_price_history()`
  mints its own internal `run_id` via `_new_run_id()` for its own
  per-HTTP-call `ServiceResult` (M04 spec design — one id per provider
  call). Step engines that call providers use the *orchestrator's* run_id
  for their own `ServiceResult` and for what they write to the DB; the
  provider's internal id is a separate, nested concern. This resolves the
  note's own hedge ("...or the orchestrator's own run_id is the sole source
  of truth and step-level echoes are irrelevant") — the orchestrator's
  run_id is what's written everywhere that matters (`pipeline_runs`,
  `pipeline_run_diagnostics`, every domain table's `run_id` column);
  provider-internal ids never leak into persisted state.

**No fix was needed anywhere** — this audit is a verification, not a repair.
Per the note's own explicit instruction ("check whether the engine already
accepts an optional `run_id` parameter... before assuming a signature
change is needed"), no signature changes were made to any step engine or to
`pipeline_orchestrator.py`.

## Testing

New: `TestRunIdCorrelation` in `tests/test_phase6_orchestrator.py` — one
fixture run (existing `FakeDb`/`build_orchestrator` harness, no real DB
files) with a fixed `run_id` passed explicitly to `.run(...)`, a small
`_RunIdEchoingEngine` fake standing in for Step 3 (recording the run_id it
receives and echoing it back in its own `ServiceResult`, mirroring what
every real engine already does), and the **real**
`SetupModeFunnelDiagnosticsService` (not a fake) wired to the same `FakeDb`
so its actual `_collect_metrics`/`_persist` write path executes and gets
recorded. Asserts the same run_id appears in: the `pipeline_runs` INSERT
params, at least one `pipeline_run_diagnostics` INSERT's params (the
diagnostics service always emits a `proposal.final_count` row even against
an empty fake DB, so this row exists deterministically), the step engine's
received kwargs and its returned `ServiceResult.run_id`, and the
orchestrator's own top-level result. Passed on first run — consistent with
the "no gap" finding, not a fix-then-pass result.

Full existing orchestrator/diagnostics/tools suites
(`test_phase6_orchestrator.py`, `test_pipeline_orchestrator.py`,
`test_pipeline_fundamentals_step.py`, `test_funnel_diagnostics.py`,
`test_tools_runners.py`, `test_run_debug_pipeline.py`) confirmed green,
unchanged, zero new failures.

## Non-goals confirmed untouched

Step order, lock/heartbeat logic, `pipeline_runs`/`pipeline_locks` schema,
Phase 2/3 modules (`config_recommender.py`, `ai_review_engine.py` — neither
is called by the orchestrator, out of scope by construction), dashboard,
`force_rerun`/`resume_from` semantics: none were modified.

## Exit criterion status

Task Scheduler artifact + setup doc delivered (real registration is a
deliberate follow-up operator action, documented, not auto-performed).
Single `run_id` traceability confirmed end-to-end across `pipeline_runs`,
`pipeline_run_diagnostics`, and step-level `ServiceResult`s — verified with
a passing test, not assumed. Full suite green.
