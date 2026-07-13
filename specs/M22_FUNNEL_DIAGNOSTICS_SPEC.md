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

## Report surface (`build_report`) — read-only, does not persist

`build_report()` returns a rich human-readable report dict in
`ServiceResult.metadata["report"]` (rendered by `tools/run_funnel_diagnostics.py`).
It does **not** write to `pipeline_run_diagnostics`.

### `evidence_summaries` — routed/validated population split (P0 batch, 2026-07-13)

Prior to this delta the evidence section mixed populations under a single
"step4-passed rows" header: `setup_score` was summarised over **all** step4
rows while every other field was gated on `setup_passed == True`, so their `n`
values silently disagreed (e.g. `setup_score n=713` next to `rvol n=14`).

The section now splits each setup_type into two explicit populations, each
carrying its own row count:

```
evidence_summaries[setup_type] = {
    "routed_n":    <count of all step4 rows for setup_type (pass + fail)>,
    "validated_n": <count of setup_passed == True step4 rows>,
    "routed":    { "setup_score": {stats}, ... },
    "validated": { "rvol": {stats}, "atr_pct": {stats}, ..., "breakout_proximity": {stats} },
}
```

- **routed** — fields defined for every routed candidate regardless of pass/fail.
  Currently: `setup_score`.
- **validated** — gate-input / evidence fields that only exist for candidates
  that cleared the validator: `rvol`, `atr_pct`, `ema20_distance_pct`,
  `ema50_distance_pct`, `estimated_rr`, `stop_distance_pct`, `range_width_pct`,
  `price_position_in_range`, `days_in_range`; `support_found`/`resistance_found`
  (consolidation_base only); `breakout_proximity` (breakout only); and the
  step5-derived `estimated_rr_s5` / `stop_distance_pct_s5` (a step5 proposal
  only exists for a validated candidate).

No field may appear in both series. The CLI prints two sub-headers per setup:
`<setup_type> — routed (n=…)` and `<setup_type> — validated (n=…)`.

### Other report sections

- `eligibility_rejection_reasons` — list of `{reason, count, pct_of_ineligible}`
  over step3 ineligible candidates, sorted by count desc (surfaces M13 gates such
  as `merger_pending`). Printed as CLI section `1b`.
- `s5_rejection_reasons` — step5 rejection reasons. The diversity-cap rejections
  `industry_cap` / `sector_cap` (post-ranking diversity trims, not validation
  gates) are relabelled `diversity_trim_industry_cap` / `diversity_trim_sector_cap`
  in the report **display only**; the raw DB `rejection_reason` values are
  unchanged.
- `borderline_failures` — printed twice: CLI section `6` (sorted by
  `setup_score`, primary) and section `6b` (sorted ascending by direction-aware
  normalised distance to the failed threshold — nearest miss first).

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
