# M16 — Outcome Queue Spec

Module 16 turns Step 5 proposals into realized `signal_outcomes`. It has two
services in `app/services/outcomes/outcome_queue.py`:

- `OutcomeQueueCreator` — enqueues `outcome_tracking_queue` rows.
- `OutcomeQueueProcessor` — computes `signal_outcomes` once eval dates arrive.

Contract source of truth: `01b_SCHEMA_AND_DATA.md` (table shapes),
`01c_FORMULAS_AND_CONFIGS.md` §64 (outcome formulas), `02b_ARCHITECTURE_DECISIONS.md`
AD-22.13 / AD-22.14 (eligibility + `entry_price_sim`), `01a` / `01c`
(`OUTCOME_HORIZONS_BD`, `simulation.slippage_bps`),
`PIPELINE/73_Trading_Calendar_Spec.md` (NYSE calendar), and the frozen Module 15
service style.

## Public APIs

```python
class OutcomeQueueCreator:
    def __init__(self, db_manager=None) -> None: ...
    def enqueue(self, signal_date, strategy_config_id, strategy_config,
                db_role="prod", run_id=None) -> ServiceResult: ...

class OutcomeQueueProcessor:
    def __init__(self, db_manager=None) -> None: ...
    def process(self, run_date, strategy_config,
                db_role="prod", run_id=None) -> ServiceResult: ...
```

- `run_id` is minted (`uuid4`) when `None`, otherwise kept.
- `db_manager` is injectable; defaults to `app.database.duckdb_manager`.
- `db_role` accepts only `prod` / `debug`; `simulation` (or anything else) returns
  `failed` before any DB access.
- Every path returns a `ServiceResult` with `rows_processed` equal to the API's
  primary write count.

### Exact metadata keys

`enqueue`: `db_role, signal_date, strategy_config_id, run_id, proposals_read,
rows_enqueued` (`rows_processed == rows_enqueued`).

`process`: `db_role, run_date, run_id, queue_rows_read, outcomes_written,
unresolvable_count, repair_incremented_count` (`rows_processed == outcomes_written`).

## Config validation (both APIs, before any DB access)

Required: `simulation.slippage_bps` — numeric, `>= 0`. On failure: `failed`, zero
counts, exact metadata keys.

## Eligibility filter (enqueue)

`step5_proposals` for the given `signal_date` / `strategy_config_id` where
`in_raw_top_n = TRUE OR in_diversified_top_n = TRUE` (AD-22.13).

## Deterministic IDs

```text
tracking_id = uuid5(NAMESPACE_URL, f"outcome_tracking_queue:{proposal_id}:{horizon_bd}")
outcome_id  = uuid5(NAMESPACE_URL, f"signal_outcomes:{proposal_id}:{horizon_bd}")
```

One `tracking_id` / `outcome_id` per `(proposal_id, horizon_bd)` for each horizon
in `constants.OUTCOME_HORIZONS_BD = [5, 10, 20, 40]`.

## Calendar rules

NYSE sessions via `app/utils/trading_calendar.py` (`pandas_market_calendars`,
spec 73). No hardcoded date arithmetic.

```text
entry_date          = next_trading_day(signal_date)
eval_date(horizon)  = add_trading_days(entry_date, horizon)   # entry = session 0
```

`entry_date` is shared by every horizon of a signal date.

## Outcome formulas (§64, AD-22.14)

```text
entry_price_raw  = daily_prices.open_raw on entry_date
entry_price_sim  = entry_price_raw * (1 + slippage_bps / 10000)
return_Nbd_pct   = close_adj(eval_date(N)) / entry_price_sim - 1   (NULL if candle missing)
mfe_40bd_pct     = max(high_adj over [entry_date, eval_date(40)]) / entry_price_sim - 1
mae_40bd_pct     = min(low_adj  over [entry_date, eval_date(40)]) / entry_price_sim - 1
realized_r_multiple = (close_adj(eval_date(horizon_bd)) - entry_price_sim)
                      / (entry_price_sim - stop_price_raw)
```

- Returns are computed only for horizons `<= the row's horizon_bd`; larger-horizon
  columns stay NULL. A 5bd row writes `return_5bd_pct` only; a 40bd row writes all.
- MFE/MAE are computed only for `horizon_bd == 40`. They are NULL if **any**
  expected session candle in `[entry_date, eval_date]` is absent or has a NULL
  `high_adj` / `low_adj` (expected sessions come from
  `trading_days_between(entry_date, eval_date)`).
- `realized_r_multiple` is NULL when `stop_price_raw` or the eval close is missing,
  or when the denominator `entry_price_sim - stop_price_raw <= 0`.
- `entry_price_sim` is the denominator for every performance figure (AD-22.14);
  `entry_price_raw` is the audit reference.

### `earnings_within_window`

Over the half-open window `(entry_date, eval_date]`:
- `TRUE` if any `earnings_calendar` row for the ticker falls in the window;
- `FALSE` if the ticker has earnings rows but none in the window;
- `NULL` if the ticker has no `earnings_calendar` rows at all.

## Repair / unresolvable flow

When `open_raw` on `entry_date` is missing for a row:
1. `repair_attempts += 1`, `last_repair_attempt = now()`;
2. if the incremented count `>= 3` → queue `status = 'unresolvable'`, else stays
   `'pending'`;
3. **no** `signal_outcomes` row is written; processing continues to the next row.

## `outcome_status`

- `'complete'` — all returns for horizons `<= horizon_bd` are non-NULL.
- `'partial'` — at least one required return is NULL.
- `'unresolvable'` — entry-price failure after 3 attempts; handled in the repair
  flow without writing `signal_outcomes`.

## Transaction model

- Read phase: a single read-only connection gathers queue rows, proposal
  `strategy_config_id`, `stop_price_raw`, prices, and earnings; it is closed
  before any write.
- Write phase: one transaction per `process()` / `enqueue()` call for all writes;
  rollback on any failure. A rollback failure never masks the original error.

## Write ownership

- `enqueue` writes only `outcome_tracking_queue` via
  `INSERT ... ON CONFLICT (tracking_id) DO NOTHING RETURNING tracking_id`
  (`rows_enqueued` counts only newly inserted rows). No UPDATE/DELETE.
- `process` writes only:
  - `signal_outcomes` via `INSERT ... ON CONFLICT (outcome_id) DO UPDATE`;
  - `outcome_tracking_queue` columns `status`, `repair_attempts`,
    `last_repair_attempt`, `completed_at`.
- Neither service writes any other table, runs DDL, uses `ATTACH`, imports
  `duckdb` directly, calls providers, or uses `print()`. All access goes through
  `duckdb_manager` / the injected `db_manager`.

## Idempotency

- Re-running `enqueue` for the same proposal/horizon is a silent no-op
  (`rows_enqueued == 0`); IDs are stable.
- Re-running `process` upserts the same `outcome_id`, so reprocessing updates in
  place rather than duplicating. A successful outcome write sets the queue row
  `status = 'done'`, `completed_at = now()`.

## Tests

`tests/test_outcome_queue.py` — fully offline. DuckDB settings paths are
redirected into `tmp_path` with the real Module 03 schema; a `FakeCalendar`
(weekday sessions) is injected via `monkeypatch` of
`outcome_queue._default_calendar`, so `pandas_market_calendars` is never imported.
Coverage: public API / `run_id` / exact metadata on success/failure/empty paths;
`db_role` and config guards before DB access; eligibility filter; deterministic
IDs; horizon dates; idempotent enqueue; empty input; write-failure rollback;
future-row skipping; repair increment and 3rd-attempt unresolvable; return
formulas and NULL handling; 40bd MFE/MAE including incomplete-window NULL;
`realized_r_multiple` including `denom <= 0`; earnings tri-state; queue `done` +
`completed_at`; idempotent reprocess; and static scans (no direct `duckdb`, no
providers, no `print()`, no DDL/`ATTACH`, writes only to the two allowed tables).

## Assumptions / open gaps

- **G-STOP-JOIN** — `step5_proposals` carries no `analysis_id` / `candidate_id`,
  so `stop_price_raw` is read from `step4_analysis` via the
  `(ticker, signal_date, strategy_config_id)` relationship, taking the single
  deterministic row `ORDER BY analysis_id LIMIT 1`. If multiple Step 4 analyses
  ever share that key, the lowest `analysis_id` wins; revisit if Step 5 starts
  persisting the originating `analysis_id`.
- **G-EARNINGS-NULL** — "no earnings rows for the ticker" is treated as unknown
  (`NULL`), distinct from "rows exist but none in window" (`FALSE`), per the
  prompt's tri-state requirement.
- **G-CALENDAR-UTILITY** — Module 16 is the first consumer of the NYSE trading
  calendar mandated by `PIPELINE/73_Trading_Calendar_Spec.md`, which did not yet
  exist in the codebase. It was added as `app/utils/trading_calendar.py` (shared
  utility, `pandas_market_calendars`-backed). Module 16 needs only
  `next_trading_day`, `add_trading_days`, and `trading_days_between`; the utility
  also exposes `is_trading_day` / `previous_trading_day` for future modules. The
  calendar is resolved through the module-level `outcome_queue._default_calendar`
  hook so tests inject a fake offline.
- **G-WINDOW-COMPLETENESS** — the 40bd MFE/MAE "missing candle" check requires
  every expected NYSE session in `[entry_date, eval_date]` to be present with
  non-NULL `high_adj` / `low_adj`; partial windows yield NULL rather than a
  best-effort extreme.
- **cross_fold_outcome** is left `FALSE` (walk-forward simulation owns it; out of
  Module 16 scope) and is not modified on upsert.
