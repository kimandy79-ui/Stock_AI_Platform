# M20_PIPELINE_ORCHESTRATOR_SPEC.md
# Phase 6 — Setup-mode Pipeline Orchestrator

Status: accepted (Phase 6).
Replaces: old strategy-mode orchestrator spec.

---

## File

`app/services/pipeline/pipeline_orchestrator.py`

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
        regime_engine=None,
        eligibility_engine=None,          # M13 — Step 3
        setup_validation_engine=None,     # M14 — Step 4
        proposal_engine=None,             # M15 — Step 5
        outcome_creator=None,             # M16 enqueue
        outcome_processor=None,           # M16 process
        config_service=None,
        diagnostics_service=None,         # M22 funnel diagnostics
    ) -> None: ...

    def run(
        self,
        run_date: date,
        run_type: str = "scheduled",
        db_role: str = "prod",
        force_rerun: bool = False,
        resume_from: str | None = None,
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

## Step sequence (setup mode)

```
1.  benchmark_etf_ingestion       critical
2.  universe_ingestion             recoverable
3.  price_ingestion                critical
4.  validation                     critical
5.  mutation_detection             recoverable
6.  feature_calculation            critical
7.  market_regime_classification   recoverable
8.  step3_universal_eligibility    critical  ← M13, once per signal_date
9.  step4_setup_validation         critical  ← M14, iterates setup configs internally
10. step5_proposals                critical  ← M15, once per signal_date
11. outcome_queue_creation         recoverable (M16 compat shim, Phase 7 migration)
12. outcome_processing             recoverable
13. dashboard_materialization      recoverable (G-DASHBOARD-MAT, Phase 7)
```

Funnel diagnostics run after Step 5 completes (non-blocking on error).

## ServiceResult metadata keys

`run_id`, `run_date`, `run_type`, `db_role`, `steps_completed`,
`failed_step`, `error`, `duration_sec`, `status`

## Allowed db_roles

`prod`, `debug` only. `simulation` returns `failed` before any DB access.

## Allowed run_types

`scheduled`, `manual`, `force_rerun`, `catchup`, `debug`

## Lock constants

`PIPELINE_LOCK_NAME = "daily_pipeline"` / `LOCK_STALE_SECONDS = 300`

## DB write targets

Orchestrator SQL writes only:
- `pipeline_runs`
- `pipeline_locks`

Diagnostics service writes:
- `pipeline_run_diagnostics`

All domain tables written by domain engines (M13/M14/M15/M16).

## Compatibility notes

- M16 outcome queue uses legacy `strategy_config_id` API (Phase 7 migration scope).
  The orchestrator passes `setup_config_id` values as `strategy_config_id` via shim.
  `outcome_queue_creation` and `outcome_processing` are **recoverable** steps.
- `dashboard_materialization` is a no-op stub (M21 Phase 7 scope).
- No legacy strategy terms (`aggressive`/`normal`/`conservative`/`DEFAULT_STRATEGY_CONFIGS`)
  appear in this module.
