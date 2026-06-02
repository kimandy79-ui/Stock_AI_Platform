# Claude Coding Prompt — Module 16: Outcome Queue

Use Project Instructions and Project Files. Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only. Work silently and return only actionable results. Do not restate global project rules.

## Attached base

- `stock_ai_platform_module15_v3_stable.zip`

Modules 01–15 are accepted and frozen. Use the attached zip as the implementation base. Implement only **Module 16 — Outcome Queue**. Do not implement Module 17 or later.

No separate Module 16 spec is provided. Create `M16_OUTCOME_QUEUE_SPEC.md` from Project Files, frozen Module 15 style, and this prompt.

## Source-of-truth retrieval hints

Use these files only as needed:

- Schemas for `outcome_tracking_queue`, `signal_outcomes`, `step5_proposals`, `step4_analysis`, `earnings_calendar` → `01b_SCHEMA_AND_DATA.md`
- Outcome formulas: entry prices, returns, MFE, MAE, R-multiple, `earnings_within_window` → `01c_FORMULAS_AND_CONFIGS.md` §64
- Queue condition and `entry_price_sim` decision → `02b_ARCHITECTURE_DECISIONS.md` AD-22.13, AD-22.14
- `OUTCOME_HORIZONS_BD`, `slippage_bps` config → `01a_CORE_PRINCIPLES.md`, `01c_FORMULAS_AND_CONFIGS.md`
- NYSE trading calendar / `next_trading_day` → `01d_MODULES_AND_PIPELINE.md`
- Service style, guard conventions, metadata discipline, transactions → frozen Module 15 code

## Required files

```text
app/services/outcomes/__init__.py
app/services/outcomes/outcome_queue.py
tests/test_outcome_queue.py
M16_OUTCOME_QUEUE_SPEC.md
README.md                          # add Module 16 note only
```

## Public API

Implement both classes in `app/services/outcomes/outcome_queue.py`:

```python
class OutcomeQueueCreator:
    def __init__(self, db_manager=None) -> None: ...

    def enqueue(
        self,
        signal_date: date,
        strategy_config_id: str,
        strategy_config: dict,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...


class OutcomeQueueProcessor:
    def __init__(self, db_manager=None) -> None: ...

    def process(
        self,
        run_date: date,
        strategy_config: dict,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

Rules:
- Mint `run_id` when `None`.
- Both classes accept injectable `db_manager`.
- Return `ServiceResult` on all paths.
- `db_role` accepts only `prod` / `debug`; reject `simulation` before DB access.

## Write ownership / forbidden actions

- `OutcomeQueueCreator.enqueue` writes only `outcome_tracking_queue` via idempotent INSERT. No UPDATE/DELETE.
- `OutcomeQueueProcessor.process` may write only:
  - `signal_outcomes` via INSERT or UPSERT per `outcome_id`
  - `outcome_tracking_queue.status`, `repair_attempts`, `last_repair_attempt`, `completed_at`
- Neither class may write other tables, run DDL, use `ATTACH`, import `duckdb` directly, call providers, or use `print()`.
- All DB access must go through `duckdb_manager` / injected `db_manager`.

## Config validation

Validate before DB access.

Required:

```text
simulation.slippage_bps: numeric, >= 0
```

On config failure, return `failed` with zero counts and exact metadata keys for that API.

## Enqueue behavior

For a given `signal_date` and `strategy_config_id`, read eligible proposals:

```sql
WHERE in_raw_top_n = TRUE OR in_diversified_top_n = TRUE
```

For each eligible proposal, create one `outcome_tracking_queue` row per horizon in `constants.OUTCOME_HORIZONS_BD` (`[5, 10, 20, 40]`).

Deterministic `tracking_id`:

```text
uuid5(NAMESPACE_URL, f"outcome_tracking_queue:{proposal_id}:{horizon_bd}")
```

Idempotency:
- Insert with `ON CONFLICT (tracking_id) DO NOTHING`.
- Rerun for the same proposal/horizon is a silent no-op.
- `rows_enqueued` counts only newly inserted rows. Use `RETURNING tracking_id` or equivalent reliable counting.

Dates:
- `entry_date` = next NYSE trading day after `signal_date`.
- `eval_date` = NYSE trading day `horizon_bd` business days after `entry_date`.
- Use the project trading-calendar utility. Do not hardcode date arithmetic.

Initial queue status: `'pending'`.

Enqueue metadata exact keys:

```text
db_role, signal_date, strategy_config_id, run_id,
proposals_read, rows_enqueued
```

`rows_processed == rows_enqueued`.

## Process behavior

For a given `run_date`, read queue rows:

```sql
WHERE status = 'pending' AND eval_date <= run_date
```

Process each eligible queue row independently.

### Per-row algorithm

1. Read entry-date `daily_prices.open_raw` for the ticker on `entry_date`.

2. If entry price is missing:
   - increment `repair_attempts`
   - set `last_repair_attempt = now()`
   - if the incremented attempt count is `>= 3`, set queue `status = 'unresolvable'`
   - otherwise leave queue `status = 'pending'`
   - write no `signal_outcomes` row
   - continue to the next queue row

3. Compute entry prices:

```text
entry_price_raw = open_raw on entry_date
entry_price_sim = entry_price_raw * (1 + slippage_bps / 10000)
```

4. Compute horizon returns for all horizons `<= this queue row's horizon_bd`:

```text
return_Nbd_pct = close_adj_on_Nbd_eval_date / entry_price_sim - 1
```

Use `NULL` when the relevant `close_adj` candle is missing.

5. For `horizon_bd == 40` only, compute MFE/MAE over `[entry_date, eval_date]`:

```text
mfe_40bd_pct = max(high_adj) / entry_price_sim - 1
mae_40bd_pct = min(low_adj)  / entry_price_sim - 1
```

Use `NULL` if any candle in the 40bd window is missing.

6. Compute `realized_r_multiple` using this queue row's own eval-date `close_adj`:

```text
realized_r_multiple = (close_adj_on_eval_date - entry_price_sim)
                      / (entry_price_sim - stop_price_raw)
```

Return `NULL` if denominator `<= 0` or required prices are missing. Read `stop_price_raw` from `step4_analysis` through the proposal/analysis relationship defined by the schema. If the join path is ambiguous, document the chosen path in `M16_OUTCOME_QUEUE_SPEC.md` under open gaps.

7. Compute `earnings_within_window`:
   - `TRUE` if any `earnings_calendar` row for the ticker is in `(entry_date, eval_date]`
   - `FALSE` if the ticker has earnings rows but none in the window
   - `NULL` if `earnings_calendar` has no rows for the ticker

8. Determine `outcome_status`:
   - `'complete'` when all returns for horizons `<= horizon_bd` are non-NULL
   - `'partial'` when at least one required return is NULL after the queue row is eligible for processing
   - `'unresolvable'` only for entry-price failure after 3 repair attempts; handled in step 2 without writing `signal_outcomes`

9. Write one `signal_outcomes` row for this queue row using deterministic `outcome_id`:

```text
uuid5(NAMESPACE_URL, f"signal_outcomes:{proposal_id}:{horizon_bd}")
```

Use INSERT or `ON CONFLICT (outcome_id) DO UPDATE` so reprocessing is idempotent.

10. After a successful outcome write, update the queue row:

```text
status = 'done'
completed_at = now()
```

### Process transaction model

- Read phase: read-only connection.
- Write phase: one transaction per `process()` call for all outcome writes and queue updates.
- Roll back on any write failure. Do not mask the original write error if rollback also fails.

Process metadata exact keys:

```text
db_role, run_date, run_id,
queue_rows_read, outcomes_written, unresolvable_count, repair_incremented_count
```

`rows_processed == outcomes_written`.

## Tests required

Create `tests/test_outcome_queue.py`. Tests must be offline and use temporary DB paths.

Creator coverage:
- public API, generated/provided `run_id`, exact metadata keys on success/failure/empty paths
- `db_role` guard and config validation before DB access
- eligibility filter: only `in_raw_top_n OR in_diversified_top_n`
- deterministic `tracking_id`
- idempotent second enqueue: zero new rows, same IDs
- correct NYSE `entry_date` and `eval_date` for all horizons
- `rows_enqueued` counts new rows only
- empty eligible input succeeds with zero counts
- write-failure rollback

Processor coverage:
- public API, generated/provided `run_id`, exact metadata keys on success/failure/empty paths
- `db_role` guard and config validation before DB access
- future queue rows (`eval_date > run_date`) are ignored
- missing entry-date price increments repair attempts and writes no outcome
- missing entry after incremented attempt count reaches 3 marks queue `unresolvable`
- return formulas and NULL handling for missing horizon candles
- 40bd MFE/MAE, including missing-window NULL behavior
- `realized_r_multiple`, including denominator `<= 0` NULL behavior
- `earnings_within_window`: in-window, out-of-window, no ticker rows → NULL
- deterministic `outcome_id`
- upsert/idempotent re-process behavior
- successful outcome sets queue `status = 'done'` and `completed_at`
- write-failure rollback

Static scans:
- no direct `duckdb` import
- no provider imports/calls
- no `print()`
- no DDL or `ATTACH`
- no writes outside allowed tables/columns

## Module 16 spec file

Create `M16_OUTCOME_QUEUE_SPEC.md` documenting:
- public APIs and exact metadata keys
- eligibility filters
- deterministic ID formulas
- calendar rules for `entry_date` / `eval_date`
- outcome formulas
- repair and unresolvable flow
- `outcome_status` rules
- transaction model
- write ownership
- idempotency guarantees
- tests
- assumptions/open gaps, especially `stop_price_raw` join path and `earnings_calendar` NULL semantics

## Output expected from Claude

Return only:
- updated zip
- added/changed files
- `M16_OUTCOME_QUEUE_SPEC.md`
- short design notes
- test commands and results
- assumptions/open gaps
- suggested commit message: `module16_outcome_queue_stable`
