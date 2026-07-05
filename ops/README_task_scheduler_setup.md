# Windows Task Scheduler setup — daily prod pipeline

Replaces the activity-simulator workaround with a real OS-level scheduled
task that runs `tools/run_prod_pipeline.py` unattended, once per day at
**23:30 local time**, whether the user is logged on or not.

## What this does NOT need

- No code changes to `run_prod_pipeline.py` — it already runs headlessly
  (no `input()`, no interactive-console assumption; `print()` output is a
  CLI-tool convention, not a service-module violation, and works fine when
  redirected to a log file by Task Scheduler).
- `--date` is intentionally omitted from the scheduled action's arguments —
  it defaults to `date.today()`, which is what a daily trigger needs.

## Import via Task Scheduler GUI (recommended — never types a password on the CLI)

1. Open **Task Scheduler** (`taskschd.msc`).
2. Action menu → **Import Task...** → select
   `ops\StockAIPlatform_DailyPipeline.xml`.
3. On the **General** tab, confirm **"Run whether user is logged on or
   not"** is selected (the imported XML already sets this — `LogonType =
   Password` — but Windows will prompt for the account password on import
   or on first save, since a stored password is required for this logon
   type to work when nobody is logged in interactively).
4. Confirm the account shown is `DESKTOP-10TI0F5\kiman` (adjust if importing
   onto a different machine/account — edit `<UserId>` in the XML first, or
   just change it in the GUI after import).
5. **Triggers** tab: confirms daily at 23:30, recurring every 1 day.
6. **Actions** tab: confirms
   `D:\Python\Stock_AI_Platform\.venv\Scripts\python.exe
   tools\run_prod_pipeline.py --run-type scheduled`, working directory
   `D:\Python\Stock_AI_Platform`.
7. Click OK, enter the account password when prompted. Done.

## Equivalent command-line import

```
schtasks /create /XML "ops\StockAIPlatform_DailyPipeline.xml" /TN "StockAIPlatform_DailyPipeline" /RU "DESKTOP-10TI0F5\kiman" /RP *
```

`/RP *` makes `schtasks` prompt interactively for the password rather than
taking it as a plaintext argument (avoids leaving the password in shell
history).

## Manual verification steps (to run after import — not yet performed)

Creating/registering the actual scheduled task modifies real OS state on
this machine, so it was not done automatically as part of this change — the
XML/doc are the deliverable; import and verification are a deliberate
operator action. After importing, verify with:

1. `schtasks /run /TN "StockAIPlatform_DailyPipeline"` — triggers the task
   immediately regardless of the 23:30 schedule, so you don't have to wait
   overnight to check it works.
2. `schtasks /query /TN "StockAIPlatform_DailyPipeline" /V /FO LIST` —
   confirm **Last Run Result** is `0x0` (success).
3. Query `pipeline_runs` in `prod.duckdb` and confirm a row was written with
   `run_type = 'scheduled'` (not `'manual'`) for that run — this is what
   actually proves the `--run-type scheduled` argument reached
   `PipelineOrchestrator.run()`, not just that *some* process ran.
4. Re-run `schtasks /run` a second time the same day (simulating a scheduler
   misfire / double-trigger) and confirm the second invocation exits with
   status `failed` / error `"run_date already succeeded"` rather than
   reprocessing — this exercises the pre-existing `_already_run` guard in
   `pipeline_orchestrator.py`, which is orthogonal to `run_type` and was not
   changed here; it should already work, this step is verification only.
5. To confirm "whether logged on or not" specifically (not just "the task
   exists"): lock the workstation (Win+L) before the scheduled fire time and
   confirm the task still ran and produced a `pipeline_runs` row.

None of these steps have been executed against a real registered task as
part of this change — do them after importing, and note the actual results
here (or in a follow-up) once confirmed.

## Non-goals confirmed unaffected

- Step order, lock/heartbeat logic, `pipeline_runs`/`pipeline_locks` schema:
  untouched.
- `force_rerun`/`resume_from` semantics: untouched — the scheduled action
  intentionally omits both flags, so a normal scheduled run behaves exactly
  like today's manual runs did, just with `run_type=scheduled` instead of
  `run_type=manual`.
