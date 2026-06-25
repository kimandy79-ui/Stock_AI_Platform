# M22_FUNNEL_DIAGNOSTICS_SPEC.md
# Phase 6 — Setup-mode Funnel Diagnostics

Status: accepted (Phase 6 v2).

---

## File

`app/services/diagnostics/funnel_diagnostics.py`

## Public API

```python
class SetupModeFunnelDiagnosticsService:
    def __init__(self, db_manager=None) -> None: ...

    def run(
        self,
        signal_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
        step_timings: dict[str, float] | None = None,
    ) -> ServiceResult: ...

    def read(
        self,
        run_id: str,
        signal_date: date,
        db_role: str = "prod",
    ) -> list[dict]: ...

# Legacy alias:
FunnelDiagnosticsService = SetupModeFunnelDiagnosticsService
```

`step_timings` is provided by the orchestrator: `{step_name: duration_seconds}`.
Each entry produces a `timing.step_duration_sec.<step_name>` row.

## Writes to

`pipeline_run_diagnostics` (Phase 6 schema addition)

## Metric rows

### Pipeline-level (setup_type = NULL)

| step_name                   | metric_name                                   | notes |
|-----------------------------|-----------------------------------------------|-------|
| step3_universal_eligibility | eligibility.total_input                       | |
| step3_universal_eligibility | eligibility.passed                            | |
| step3_universal_eligibility | eligibility.failed                            | |
| step3_universal_eligibility | eligibility.feature_ready                     | |
| step3_universal_eligibility | eligibility.feature_missing                   | |
| step3_universal_eligibility | eligibility.rejection_reason.\<reason\>       | one row per distinct reason |
| step3_routing               | routing.not_routed                            | |
| step3_routing               | routing.ineligible                            | |
| step5_risk_label            | risk_label.low                                | |
| step5_risk_label            | risk_label.medium                             | |
| step5_risk_label            | risk_label.high                               | |
| step5_proposals             | proposal.buy_eligible                         | |
| step5_proposals             | proposal.watchlist                            | |
| step5_proposals             | proposal.rejected                             | |
| step5_proposals             | proposal.final_count                          | |
| step5_proposals             | proposal.rejection_reason.\<reason\>          | v2 — one row per distinct rejection_reason |
| step5_risk_label            | stop.failure_reason.\<factor\>                | v2 — from risk_reasons where factor starts with stop_distance/stop_price/stop_basis |
| step5_risk_label            | target.failure_reason.\<factor\>              | v2 — from risk_reasons where factor starts with target_room/target_price/target_is_structural |
| step5_risk_label            | rr.failure_reason.\<factor\>                  | v2 — from risk_reasons where factor starts with estimated_rr/rr_/min_rr |
| step5_risk_label            | risk.failure_reason.\<factor\>                | v2 — from risk_reasons for all other factors |
| config_snapshot             | config.setup_config                           | v2 — one row per active setup_type; details in metadata_json |
| config_snapshot             | config.risk_label_config                      | v2 — one row; details in metadata_json |
| pipeline_timing             | timing.step_duration_sec.\<step_name\>        | v2 — one row per step supplied in step_timings |

### Per-setup (setup_type = breakout | pullback | trend_continuation | consolidation_base)

| step_name              | metric_name                               | notes |
|------------------------|-------------------------------------------|-------|
| step3_routing          | routing.routed                            | |
| step4_setup_validation | validation.passed                         | |
| step4_setup_validation | validation.failed                         | |
| step4_setup_validation | validation.failure_reason.\<reason\>      | one row per distinct setup_fail_reason |

## Risk failure breakdown classification

`risk_reasons` is a JSON array of `"factor_name=score"` strings written by Step 5.
Each entry is classified by prefix:

| Factor prefix                                    | metric category        |
|--------------------------------------------------|------------------------|
| `stop_distance`, `stop_price`, `stop_basis`      | `stop.failure_reason`  |
| `target_room`, `target_price`, `target_is_structural` | `target.failure_reason` |
| `estimated_rr`, `rr_`, `min_rr`                 | `rr.failure_reason`    |
| all others                                       | `risk.failure_reason`  |

## Config snapshot metadata_json

`config.setup_config` rows (one per active setup_type):
```json
{
  "config_id": "setup_breakout_v1",
  "version": "breakout_v1",
  "config_hash": "<sha256>",
  "setup_type": "breakout"
}
```

`config.risk_label_config` row:
```json
{
  "config_id": "risk_label_config_v1",
  "version": "risk_v1",
  "config_hash": "<sha256>"
}
```

## Schema: pipeline_run_diagnostics

```sql
CREATE TABLE IF NOT EXISTS pipeline_run_diagnostics (
    diag_id        VARCHAR PRIMARY KEY,
    run_id         VARCHAR NOT NULL,
    signal_date    DATE NOT NULL,
    db_role        VARCHAR NOT NULL,
    step_name      VARCHAR NOT NULL,
    setup_type     VARCHAR,          -- NULL for pipeline-level metrics
    metric_name    VARCHAR NOT NULL,
    metric_value   DOUBLE,           -- NULL for config snapshot rows
    reason         VARCHAR,
    metadata_json  JSON,
    created_at     TIMESTAMP NOT NULL
);
```

Indexes: `idx_diag_run_date (run_id, signal_date)`, `idx_diag_run_step (run_id, step_name, setup_type)`.

## Constraints

- `db_role` must be `prod` or `debug`; `simulation` returns `failed`.
- Diagnostics failure is **non-blocking** — orchestrator adds a warning, run continues.
- No DDL, ATTACH, or direct `duckdb` import.
- `conservative_consolidation` never appears — canonical name is `consolidation_base`.
- Config snapshot rows have `metric_value = NULL`; data is in `metadata_json`.
- Timing rows: `metric_value` = seconds as float; `reason` = step_name; `setup_type` = NULL.
- All writes in a single transaction; rolled back on any insert failure.
