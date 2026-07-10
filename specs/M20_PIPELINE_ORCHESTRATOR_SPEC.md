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

## Step 5 optional inputs (P2.5, 2026-07-10)

M15 accepts `fundamentals_scores` and `ai_review_scores` as pass-through
parameters and has no read path for either — M20 assembles both. Behavior is
governed by two flags on the `pipeline` runtime config.

| Flag | Default | Effect |
|---|---|---|
| `auto_invoke_fundamentals` | `True` | Point-in-time read of `ticker_fundamentals` as of `signal_date`, scored via the shared `fundamentals_quality` helper, fed to Step 5. |
| `auto_invoke_ai_review` | `False` | Runs M19's review passes and re-scores Step 5 with `ai_review_scores`. |

**Fundamentals** runs by default because it is cheap and deterministic (one
DuckDB read plus pure arithmetic; no API calls). It is nonetheless a flag, not
unconditional, because the score it feeds is two-sided and can promote a ticker
into `BUY` once `risk_label_config.fundamentals.score_weight` is raised above
its seeded `0.0`. A read failure degrades to `None` (Step 5 scores exactly as it
did pre-Phase-4) rather than scoring against a partial map. An empty or missing
`ticker_fundamentals` yields `{}`, which Step 5 already treats as "no coverage,
no adjustment".

**AI review is off and must stay off** until explicitly activated: each pass is a
paid API call across multiple vendors, so a backfill multiplies cost by
`dates × candidates × passes`. Two consequences when enabled:

1. **Step 5 runs twice per `signal_date`.** `step5_proposals` must exist before
   M18 can export them and M19 can review them, so the scores cannot exist during
   the first pass. M15's `_write` is INSERT-only, so the first pass's rows are
   deleted before re-proposing rather than updated in place.
2. **The scores are supplied by an injected `ai_review_scores_provider`**
   — a callable `(signal_date, db_role, run_id, log) -> dict[ticker, dict]`.
   It is `None` by default, so the default pipeline never constructs an M18/M19
   engine, never writes a review ZIP, and never makes a paid call. With the flag
   on and no provider injected, M20 logs a warning and keeps the first pass.

Any failure in the AI review path degrades to the first pass's already-committed
proposals: an unreviewable AI response must not cost the run.

> **Open design question (deferred from Phase 3).** `ai_reviews` has no `ticker`
> column — one row covers many tickers via `selected_tickers_json`, keyed by
> `setup_config_id`. Producing the per-ticker `ai_review_scores` Step 5 expects
> therefore requires broadcasting an export's pass score across its tickers. That
> correlation semantic is unresolved and is why no default provider ships.

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
