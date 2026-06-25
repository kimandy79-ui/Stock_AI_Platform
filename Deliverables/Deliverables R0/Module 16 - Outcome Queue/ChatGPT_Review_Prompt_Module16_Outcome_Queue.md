# ChatGPT Review Prompt — Module 16: Outcome Queue

> **Usage:** Paste this prompt into ChatGPT (GPT-4o or o-series). Attach the
> listed files before sending. Do not modify the review goals or the frozen
> module range without updating the spec accordingly.

---

Review the attached implementation for **Module 16 — Outcome Queue**.

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not rely on archived monolithic source files or old FULL / PATCH / MINI PATCH
files if split Project Files are available.

## Attached files

1. `stock_ai_platform_module16_outcome_queue_stable.zip`
2. `M16_OUTCOME_QUEUE_SPEC.md`
3. Test output or notes (paste inline if pytest was not runnable)

## Context

- Modules 01–15 were previously accepted and are **frozen**.
- Claude was instructed to implement **only Module 16**.
- Module 16 introduces two new services in `app/services/outcomes/outcome_queue.py`
  (`OutcomeQueueCreator` / `OutcomeQueueProcessor`) and a new shared utility
  `app/utils/trading_calendar.py` (NYSE calendar, `pandas_market_calendars`).
- Module 17 and later are **out of scope**.

## Key source files for this review

- `01b_SCHEMA_AND_DATA.md` — `outcome_tracking_queue`, `signal_outcomes`,
  `step5_proposals`, `step4_analysis`, `daily_prices`, `earnings_calendar`
- `01c_FORMULAS_AND_CONFIGS.md` §64 — entry prices, horizon returns, MFE/MAE,
  realized R-multiple, `earnings_within_window`
- `02b_ARCHITECTURE_DECISIONS.md` — AD-22.13 (eligibility), AD-22.14
  (`entry_price_sim`)
- `01a_CORE_PRINCIPLES.md` — `OUTCOME_HORIZONS_BD`, `simulation.slippage_bps`
- `01d_MODULES_AND_PIPELINE.md` — `PIPELINE/73_Trading_Calendar_Spec.md`
  (NYSE calendar functions)
- `02_PROJECT_IMPLEMENTATION_CONTEXT.md` — coding standards, guard conventions,
  metadata discipline, transaction style (frozen Module 15 pattern)

## Module-specific risks to focus on

1. **Eligibility filter** — only `in_raw_top_n OR in_diversified_top_n` proposals
   (AD-22.13); nothing else should be enqueued.
2. **Calendar arithmetic** — `entry_date = next_trading_day(signal_date)`;
   `eval_date = add_trading_days(entry_date, horizon_bd)` per the NYSE calendar
   spec. Check that `trading_calendar.py` adds the right boundary semantics
   (entry_date is session index 0; horizon_bd steps forward).
3. **`entry_price_sim` as performance denominator** — every return, MFE/MAE, and
   R-multiple must divide by `entry_price_sim`, not `entry_price_raw` (AD-22.14).
4. **40bd MFE/MAE missing-candle rule** — must be `NULL` when _any_ expected NYSE
   session candle in `[entry_date, eval_date]` is absent or has a NULL
   `high_adj` / `low_adj`; a best-effort partial window is incorrect.
5. **Realized R-multiple** — check NULL guard when denominator `<= 0` or either
   price is missing; check the `stop_price_raw` join path
   (`step4_analysis` via `(ticker, signal_date, strategy_config_id)` with
   deterministic `LIMIT 1`).
6. **`earnings_within_window` tri-state** — `TRUE` / `FALSE` / `NULL` (no rows for
   ticker at all); verify the SQL filter window is `(entry_date, eval_date]`
   (exclusive open, inclusive close).
7. **Repair / unresolvable flow** — check that missing entry price only increments
   `repair_attempts` and writes no `signal_outcomes` row; confirm `status =
   'unresolvable'` fires on the 3rd attempt (incremented count `>= 3`), not the
   4th.
8. **Idempotency** — enqueue uses `ON CONFLICT (tracking_id) DO NOTHING RETURNING`
   and counts only newly inserted rows; process upserts on `outcome_id`; reruns
   must be safe.
9. **Transaction model** — read phase uses a read-only connection; all writes
   (outcome upserts + queue updates) commit in one transaction; rollback must not
   mask the original error.
10. **Write ownership** — verify no writes to tables other than
    `outcome_tracking_queue` and `signal_outcomes`; no DDL, no `ATTACH`, no direct
    `duckdb` import, no provider calls, no `print()`.
11. **Metadata key completeness** — both APIs must return exactly their documented
    key sets on every return path (success, empty, guard failure, write failure).
12. **`trading_calendar.py` correctness and lazy import** — the real module import
    is inside `_default_calendar()` so tests can inject a fake offline; check the
    `add_trading_days` window expansion is sufficient for a 40bd horizon.
13. **`cross_fold_outcome`** — must not be touched by Module 16 (walk-forward
    simulation owns it); check the upsert leaves it unchanged.

## Review goals

1. Check **scope compliance** — only Module 16 added; frozen modules 01–15
   unchanged; Module 17 not pre-implemented.
2. Check **correctness** against `M16_OUTCOME_QUEUE_SPEC.md` and the source files
   listed above; pay attention to the module-specific risks above.
3. Check **architecture boundaries** — DB access through `duckdb_manager` /
   injected `db_manager` only; provider layer untouched; no forbidden imports.
4. Check **test quality** — offline suite with `FakeCalendar`; all required cases
   from the spec covered; static guardrail scans present and correct; `tmp_path`
   isolation.
5. Check whether **`M16_OUTCOME_QUEUE_SPEC.md`** is accurate, concise, and
   consistent with the implementation (open gaps documented, join path explained).
6. Identify **bugs, hallucinations, overengineering, missing requirements, or
   incorrect formula implementations**.
7. Recommend one of: **ACCEPT / ACCEPT WITH MINOR FIXES / REJECT**.

Do not rewrite the whole module unless necessary.
Provide only concrete findings and actionable fixes.
