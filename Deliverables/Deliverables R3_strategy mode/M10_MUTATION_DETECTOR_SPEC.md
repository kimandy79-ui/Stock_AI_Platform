# M10 — Mutation Detector Spec

Module-specific source of truth for **Module 10 — Mutation Detector**. Higher-
priority sources win on conflict: module specs > current codebase > split
source-of-truth files. This spec must match the accepted implementation in
`app/services/mutation/mutation_detector.py`.

## 1. Purpose and non-scope

Module 10 runs after Module 09 validation and before Module 11 feature
calculation. For an inclusive `[start_date, end_date]` range it operates on
already-ingested `daily_prices` rows only:

- derives and writes `daily_prices.adjustment_factor` where computable;
- detects explicit splits and sets `daily_prices.mutation_flag = TRUE`;
- enqueues one `data_repair_queue` row (`repair_reason = 'mutation'`) and one
  `feature_rebuild_log` row per eligible ticker that has a detected mutation;
- returns a `ServiceResult`.

**Non-scope.** Module 10 does **not**: call any provider/vendor or fetch data;
modify price values or any OHLCV raw/adjusted column, `dividend_amount`,
`split_ratio`, `data_quality_status`, or `created_at`; write `ticker_master`,
`ticker_universe_snapshot`, `sector_etf_map`, `simulation.duckdb`, or any
feature/step/proposal/outcome/AI/execution table; process / resolve / update /
delete existing `data_repair_queue` or `feature_rebuild_log` rows (insert-only);
import `duckdb`, `ATTACH`, run DDL, change schema, or bypass the DuckDB manager.
It implements no part of Module 11 or later.

## 2. Source references

- Columns / enums: `docs/SCHEMA_SPEC.md` §3.7 (`daily_prices`), §3.17
  (`data_repair_queue`), §3.18 (`feature_rebuild_log`), §5 (enum catalog);
  mirrored by `01b_SCHEMA_AND_DATA.md`.
- "Module 10 owns `adjustment_factor` derivation": `M07_*` §, `M08_*` §6,
  `M04_PROVIDER_INTERFACE_SPEC.md` §11.
- Module placement (after 09, before 11): `01d_MODULES_AND_PIPELINE.md`.
- Raw-vs-adjusted storage / DB boundary: `02b_ARCHITECTURE_DECISIONS.md` §22.6.
- Deterministic `repair_id` / single-transaction model:
  `M08_DAILY_PRICE_INGESTION_SPEC.md` §7, §10.
- Status ownership / no-downgrade discipline: `M09_DATA_VALIDATOR_SPEC.md` §5.
- `FEATURE_SCHEMA_VERSION` (`features_v01`): `app/config/constants.py`.

## 3. Public API

```python
from app.services.mutation import MutationDetector

MutationDetector(db_manager=None)        # db_manager injectable for tests only
    .detect(
        start_date: datetime.date,
        end_date: datetime.date,
        db_role: str = "prod",           # "prod" | "debug" only
        run_id: str | None = None,       # minted uuid4 when None
    ) -> ServiceResult
```

`detect` is the only public method. Mirrors the Module 09 service style.

- **`db_role` guard:** only `prod` / `debug`. Any other value (including
  `simulation`) returns `failed` **before any DB read/write**; the simulation DB
  is never opened.
- **date-range guard:** `start_date > end_date` returns `failed` **before any DB
  read/write**.
- **`run_id` propagation:** passed through when provided, else a fresh `uuid4`.
- All DB access goes through the Module 02 `duckdb_manager` (no arbitrary paths,
  no `ATTACH`, no `duckdb` import).

`ServiceResult.rows_processed` equals the eligible-row count (= metadata
`rows_processed`).

## 4. Exact `ServiceResult.metadata` keys

Present on **every** return path (guard failure, read failure, write failure,
success):

```
db_role
start_date
end_date
rows_read
rows_processed
rows_skipped_non_ok
adjustment_factors_written
mutation_rows_detected
mutation_flags_written
tickers_with_mutation
repair_queue_enqueued
rebuild_logs_enqueued
```

Definitions:

- `rows_read` — all `daily_prices` rows read in range.
- `rows_processed` — eligible rows after the `data_quality_status = 'ok'` filter.
- `rows_skipped_non_ok` — rows with any other `data_quality_status`.
- `adjustment_factors_written` — rows whose stored `adjustment_factor` changed,
  was inserted, or was cleared to `NULL`.
- `mutation_rows_detected` — eligible rows passing a mutation detection rule.
- `mutation_flags_written` — rows whose `mutation_flag` changed `FALSE` → `TRUE`.
- `tickers_with_mutation` — distinct eligible tickers with ≥1 detected mutation.
- `repair_queue_enqueued` — newly inserted repair rows (ignored duplicates not
  counted).
- `rebuild_logs_enqueued` — newly inserted rebuild-log rows (ignored duplicates
  not counted).

On guard failure all numeric counts are `0`. On write failure rollback is
mandatory, so the four durable write counts (`adjustment_factors_written`,
`mutation_flags_written`, `repair_queue_enqueued`, `rebuild_logs_enqueued`) are
`0`; the read/compute counts still reflect what was computed.

## 5. Data-quality boundary

Process only rows where `daily_prices.data_quality_status = 'ok'`. Rows with any
other status are counted in `rows_skipped_non_ok` and must not cause any write
to `mutation_flag`, `adjustment_factor`, `data_repair_queue`, or
`feature_rebuild_log`.

## 6. Rule matrix

| Check | Source | Condition (eligible rows only) | Columns written | Repair queue (`repair_reason`) | Rebuild log | Status |
|---|---|---|---|---|---|---|
| Explicit split | SCHEMA §3.7 `split_ratio DEFAULT 1`; locked prompt | `split_ratio` non-null AND `!= 1` | `mutation_flag` → `TRUE` (no-downgrade) | yes — `mutation` (per ticker, first detected date) | yes (per ticker, first detected date) | implemented |
| Adjustment-factor derivation | M07/M08 ("Module 10 owns derivation"); locked prompt | `close_raw` non-null & `!= 0` & `close_adj` non-null | `adjustment_factor = close_adj/close_raw`; else `NULL` | no | no | implemented |
| Ratio discontinuity (`close_raw/close_adj` jump) | undefined in frozen sources | — | — | — | — | **open spec gap G1** |

`updated_at` is refreshed on any row whose `mutation_flag` or
`adjustment_factor` is written (allowed audit column).

## 7. `adjustment_factor` formula

```
if close_raw is not None and close_raw != 0 and close_adj is not None:
    adjustment_factor = close_adj / close_raw
else:
    adjustment_factor = NULL
```

A write occurs only when the derived value **differs** from the stored value
(NULL-aware exact comparison). Recomputing `close_adj / close_raw` from the same
stored doubles yields the identical IEEE-754 value, so an unchanged row is not
rewritten on re-run. Clearing a previously stored value to `NULL` counts as a
write.

## 8. `mutation_flag` no-downgrade rule

`mutation_flag` is only ever flipped `FALSE` → `TRUE`. The `SET` is guarded by
`WHERE ... mutation_flag = FALSE`, so a row already `TRUE` is never rewritten and
`TRUE` is never lowered to `FALSE`. A detected row already `TRUE` still counts in
`mutation_rows_detected` and `tickers_with_mutation` but adds `0` to
`mutation_flags_written`.

## 9. Repair queue and rebuild-log rules

For each eligible ticker with ≥1 detected mutation, keyed on the **earliest
detected mutation date in range** (`first_date`):

- `data_repair_queue` insert: `repair_reason = 'mutation'`, `repair_date =
  first_date`, `attempts = 0`, `max_attempts = 3`, `status = 'pending'`,
  `created_at = now()`, `updated_at = NULL`.
- `feature_rebuild_log` insert: `reason = 'mutation'`, `affected_start_date =
  first_date`, `affected_end_date = NULL`, `feature_schema_version =
  constants.FEATURE_SCHEMA_VERSION`, `triggered_at = now()`, `status =
  'pending'`.

Deterministic ids (insert-or-ignore via the PRIMARY KEY conflict target):

```
repair_id  = uuid5(NAMESPACE_URL, "data_repair_queue:<ticker>:<first_date>:mutation")
rebuild_id = uuid5(NAMESPACE_URL, "feature_rebuild_log:<ticker>:<first_date>:mutation")
```

`ON CONFLICT (<pk>) DO NOTHING RETURNING <pk>` yields one row per real insert and
zero per conflict — re-runs over identical input enqueue `0` and create no
duplicates.

`repair_reason = 'mutation'` is present in the frozen enum (SCHEMA_SPEC §3.17 /
§5), so the repair path is fully supported. `feature_rebuild_log` exists with the
required columns (SCHEMA_SPEC §3.18), so the rebuild path is fully supported.

## 10. Transaction / idempotency strategy

Three phases:

1. **Read** eligible source rows through a short read-only connection that is
   closed before computation (no transaction held open during compute).
2. **Compute** all decisions and payloads in pure Python (no DB writes).
3. **Write** all `daily_prices` updates, repair inserts, and rebuild inserts
   inside **one** `BEGIN TRANSACTION ... COMMIT`. Order: adjustment-factor
   writes → mutation-flag writes → per-ticker repair + rebuild inserts.

On any error inside the transaction the engine issues `ROLLBACK` and returns
`failed` — no partial flag/factor updates, no stray repair/rebuild rows. Re-runs
over the same range and input data produce stable values and no duplicate
repair/rebuild rows.

## 11. DB-manager usage

All access is through `app.database.duckdb_manager` (`prod` / `debug` only). The
engine accepts an injected `db_manager` for tests; in production the real module
is used. No direct `duckdb` import, no `ATTACH`, no DDL.

## 12. Tests

`tests/test_mutation_detector.py` — fully offline, isolated via the `tmp_db_paths`
fixture (settings DB paths redirected into `tmp_path`, real Module 03 schema
applied). Coverage: public API + exact signature + `run_id` propagation; exact
metadata keys on success and guard failure; `adjustment_factor` derivation,
underivable-`NULL`, clear-to-`NULL`, and unchanged-no-rewrite; non-`ok` rows
skipped/untouched (parametrized over all bad statuses); explicit-split detection
→ flag/repair/rebuild; `split_ratio` of `1`/`NULL` not detected; first-mutation-
date keying; clean rows unchanged; no-downgrade of existing `TRUE`; re-run
stability and dedup; deterministic repair/rebuild ids (incl. namespace
separation) and DB id match; write ownership (only `mutation_flag` /
`adjustment_factor` / `updated_at` change; all other `daily_prices` columns
byte-identical); forbidden tables untouched; simulation DB never created/written;
invalid `db_role` / `simulation` and invalid date range fail before DB access;
transaction rollback (injected failing manager) leaves no partial/ repair/
rebuild writes; read-failure returns `failed`; debug role; rows outside range
ignored; G1 documented + no ratio-discontinuity detection; static scans (no
direct `duckdb` / `ATTACH` / DDL / vendor / provider import, no `print()`,
`daily_prices` SET-clause column ownership, no processing of existing repair/
rebuild rows). Existing Module 01–09 tests pass unchanged.

## 13. Assumptions, open gaps, blockers

- **A1.** `feature_rebuild_log.reason` uses the value `'mutation'` (mirrors the
  repair reason); the frozen enum catalog defines no dedicated domain for this
  column, so the natural mutation-reason value is used rather than inventing a
  new vocabulary.
- **A2.** `feature_rebuild_log.status` uses `'pending'` (reuses the queue
  `status` enum value); no dedicated rebuild-status domain exists in the frozen
  catalog. `affected_end_date` is left `NULL` (no end is defined by the frozen
  sources).
- **A3.** Per-ticker repair/rebuild rows are keyed on the **earliest** detected
  mutation date in range (locked-prompt default: "first detected mutation date
  in the requested range as the repair date / affected start date").
- **A4.** `adjustment_factor` change detection uses NULL-aware exact float
  comparison; deterministic recomputation makes this idempotent on re-run.
- **G1 (open spec gap).** No `close_raw / close_adj` ratio-discontinuity
  threshold is defined anywhere in the frozen project sources. Historical /
  retroactive ratio-discontinuity detection is therefore **not implemented**;
  only the explicit `split_ratio != 1` path plus `adjustment_factor` derivation
  are. If a threshold is later defined, the planned shape is: read one
  immediately-previous eligible row per ticker before `start_date` for
  comparison only, never writing outside the requested range.
- **Blockers:** none. `repair_reason = 'mutation'` and `feature_rebuild_log`
  with required columns are both present in the frozen schema.
