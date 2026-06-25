# M09 — Data Validator — Module Spec

Status: implementation-oriented source of truth for Module 09. Derived from the
Module 09 task and the frozen project sources actually present in the codebase:
`docs/SCHEMA_SPEC.md` (enums, `daily_prices`, `data_repair_queue`),
`docs/ARCHITECTURE.md` (§ Module 09 / Module 10 responsibilities and pipeline
order), `docs/MASTER_SPEC.md` (pipeline step 6 "Validate data"; screening hard
filter `data_quality_status = ok`), `docs/CODING_STANDARDS.md` (logging,
ServiceResult, no-`print`, thresholds-in-config), `docs/DECISIONS_LOG.md`,
and `M08_DAILY_PRICE_INGESTION_SPEC.md` (repair-queue deterministic-id
insert-or-ignore pattern, single-transaction write, ServiceResult metadata
style). This spec introduces no new architecture and overrides no
higher-priority document.

> **Source-mapping note.** The Module 09 prompt referenced project-knowledge
> files (`01b_SCHEMA_AND_DATA.md`, `01c_FORMULAS_AND_CONFIGS.md`,
> `01d_MODULES_AND_PIPELINE.md`, `02_…`, `02b_…`). Those files are not present
> in `stock_ai_platform_module08_stable.zip`; their authoritative content lives
> in `docs/*.md` (schema/enums → `SCHEMA_SPEC.md`; modules/pipeline →
> `ARCHITECTURE.md` + `MASTER_SPEC.md`; coding/architecture rules →
> `CODING_STANDARDS.md` / `DECISIONS_LOG.md`). The `docs/*.md` files were used
> as the frozen source of truth.

## 1. Purpose

Validate already-ingested `daily_prices` rows after ingestion (Module 08) and
before mutation detection (Module 10) / feature calculation (Module 11). Module
09 owns the **real** `daily_prices.data_quality_status` value (Module 08 wrote
the placeholder `"ok"`), and optionally enqueues validation repairs into
`data_repair_queue`. It is pipeline step 6, "Validate data"
(`MASTER_SPEC.md` §4).

## 2. Scope / non-scope

In scope: read `daily_prices` rows for an inclusive `[start_date, end_date]`
range (read-only); run the structural OHLCV integrity checks defined in §6;
escalate `daily_prices.data_quality_status` to `failed` for all rows that fail
any check; enqueue one `data_repair_queue` row (`repair_reason = 'bad_ohlc'`)
per **OHLC-invalid** row (rules 1–6 only) using the Module 08 deterministic-id
insert-or-ignore pattern — negative-volume rows (rule 7) are escalated but
receive **no** repair enqueue because no suitable `repair_reason` exists in the
frozen enum (open spec gap G6); return a `ServiceResult`.

Out of scope (owned elsewhere, never touched here): calling any market-data
provider / vendor or fetching data (this module validates DB rows only —
Modules 04/05/07/08); modifying price values or any OHLCV raw/adjusted column,
`dividend_amount`, `split_ratio`, `adjustment_factor`, or `mutation_flag`
(Module 08 ingests, Module 10 owns mutations/adjustment); writing
`ticker_master`, `ticker_universe_snapshot`, `sector_etf_map`, the simulation
DB, or any feature/step/proposal/outcome/AI/execution table; split / mutation /
large-jump detection (Module 10); feature calculation (Module 11); processing,
resolving, updating, or deleting existing `data_repair_queue` rows (Module 09
only **inserts** — it is not the repair processor); opening DuckDB directly,
`ATTACH`, DDL, or schema changes.

## 3. Source-of-truth priority

1. This file (`M09_DATA_VALIDATOR_SPEC.md`).
2. `docs/SCHEMA_SPEC.md` (`daily_prices`, `data_repair_queue`, enum catalogs).
3. `docs/ARCHITECTURE.md` / `docs/MASTER_SPEC.md` (module responsibilities,
   pipeline order, screening hard filter).
4. `docs/CODING_STANDARDS.md` / `docs/DECISIONS_LOG.md`.
5. `M08_DAILY_PRICE_INGESTION_SPEC.md` (repair-queue + transaction pattern).

On conflict or a missing rule: do not guess. Record it in §8 as an open spec
gap or blocker and implement only the safest explicitly supported subset.

## 4. Public API (exact)

```python
class DataValidator:
    def validate(
        self,
        start_date: date,
        end_date: date,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        ...
```

Exactly one public method (mirrors the Module 08 service shape, minus the
`provider` argument — Module 09 never calls a provider). The constructor is
parameter-free except an optional `db_manager` hook used only for test
injection (`DataValidator(db_manager=...)`); when omitted, the real
`app.database.duckdb_manager` is used. No module-level function variant, no
extra public methods.

- `start_date` / `end_date` — inclusive `[start_date, end_date]` range applied
  to `daily_prices.date`.
- `db_role` — `"prod"` or `"debug"` only, resolved through `duckdb_manager`.
  `"simulation"` and any other value yield a `failed` result **before** any DB
  read or write.
- `run_id` — a fresh `uuid4` is minted when `None` and is propagated to the
  `RunIdLoggerAdapter` and the returned `ServiceResult.run_id`.

Guards (both run before any DB access — no reads, no writes):
- invalid `db_role` (incl. `"simulation"`) → `failed`.
- invalid date range (`start_date > end_date`) → `failed`.

## 5. Validation ownership and scope of rows

Module 09 owns `daily_prices.data_quality_status`
(`ok / warning / suspect / failed / quarantined`; `SCHEMA_SPEC.md` §5). It
validates **every** `daily_prices` row whose `date` is in the requested range.
`daily_prices` has no `symbol_type` column (that lives in `ticker_master`), and
the task says to validate the table's rows for the range, so no `symbol_type`
filter is applied and `ticker_master` is not joined (and never written).

## 6. Rule matrix

Only **structural OHLCV invariants** are implemented — properties that are true
of any valid price row by definition and require **no numeric threshold and no
trading-calendar**. The frozen sources define the enums and the downstream
consumer (`data_quality_status = ok` is a screening hard filter,
`MASTER_SPEC.md` §12) but define **no** thresholds, status-transition table, or
per-check repair-reason mapping for Module 09. Threshold- or calendar-dependent
checks are therefore left as open spec gaps (§8), not invented.

A row failing **any** implemented check is invalid and its status is escalated
to `failed` (§7). **OHLC-invalid rows (rules 1–6)** additionally have one
`bad_ohlc` repair enqueued (§7.2). **Volume-invalid rows (rule 7)** are
escalated to `failed` but receive **no** repair enqueue — no suitable
`repair_reason` exists in the frozen enum (open spec gap G6). Checks are
applied to the raw OHLC tuple and, independently, to the adjusted OHLC tuple;
a predicate is evaluated only when its operands are non-null.

| # | Check | Source | Condition (row is INVALID when…) | Resulting status | Repair written | `repair_reason` | Status |
|---|---|---|---|---|---|---|---|
| 1 | `null_ohlc` | ARCHITECTURE §Module 09 ("Validates OHLCV … suspicious values"); SCHEMA_SPEC daily_prices | any of `open_raw,high_raw,low_raw,close_raw,open_adj,high_adj,low_adj,close_adj` IS NULL | `failed` | yes | `bad_ohlc` | implemented |
| 2 | `high_lt_low_raw` | structural OHLC invariant | `high_raw < low_raw` (both non-null) | `failed` | yes | `bad_ohlc` | implemented |
| 3 | `high_lt_low_adj` | structural OHLC invariant | `high_adj < low_adj` (both non-null) | `failed` | yes | `bad_ohlc` | implemented |
| 4 | `oc_out_of_range_raw` | structural OHLC invariant | `open_raw` or `close_raw` ∉ `[low_raw, high_raw]` (operands non-null) | `failed` | yes | `bad_ohlc` | implemented |
| 5 | `oc_out_of_range_adj` | structural OHLC invariant | `open_adj` or `close_adj` ∉ `[low_adj, high_adj]` (operands non-null) | `failed` | yes | `bad_ohlc` | implemented |
| 6 | `non_positive_price` | structural invariant (equity price domain) | any of the 8 OHLC price columns `<= 0` (non-null) | `failed` | yes | `bad_ohlc` | implemented (assumption A2) |
| 7 | `negative_volume` | structural invariant | `volume_raw < 0` or `volume_adj < 0` (non-null) | `failed` | **no** (gap G6) | n/a — no suitable enum value | implemented; status escalated, no repair enqueued |
| — | missing price rows / missing expected trading days | ARCHITECTURE §Module 09 ("missing rows") | needs a trading-day calendar **and** an undefined "expected coverage" rule | — | — | (`missing_price` owned by M08) | **open spec gap** (G1) |
| — | null / zero volume | — | `volume_adj` is intentionally `NULL` (M08); zero volume is a legitimate halt/illiquid day | not flagged | no | — | **not flagged** (A4) |
| — | duplicate ticker/date rows | SCHEMA_SPEC daily_prices PK | `daily_prices` PK `(ticker, date)` makes duplicates impossible | — | — | — | **N/A** (G2) |
| — | large price jumps / outliers | ARCHITECTURE §Module 10 (Mutation Detector) | no threshold defined; splits/mutations are Module 10's responsibility | — | — | — | **open spec gap / out of scope** (G3) |
| — | stale / incomplete ticker coverage | — | no threshold defined; needs a trading-day calendar | — | — | — | **open spec gap** (G4) |

## 7. Status rules, precedence, and repair queue

### 7.1 Status assignment and precedence

Severity precedence (assumption A1, the conservative order suggested by the
task; uses only frozen enum values):

```text
quarantined (4) > failed (3) > suspect (2) > warning (1) > ok (0)
```

Module 09 computes only two statuses from the rule matrix: `failed` (≥1 check
fails) and `ok` (all checks pass). It applies a **monotonic, no-downgrade**
escalation enforced in SQL: a row's stored status is overwritten **only** when
the computed status is *strictly more severe* than the stored status. Because
the only escalation target is `failed` (severity 3):

- a bad row currently `ok` / `warning` / `suspect` is escalated to `failed`;
- a bad row already `failed` / `quarantined` is left unchanged (idempotent, and
  never downgrades a `quarantined` row);
- a good row (computed `ok`, severity 0) never overwrites any stored status, so
  it never downgrades a worse status and never churns an already-`ok` row.

The escalation `UPDATE` carries `RETURNING ticker`, so the count of rows whose
status actually changed (`status_updates_written`) is exact. Re-running over the
same data produces zero status changes.

> Consequence (open gap G5): a row escalated to `failed` is **not** cleared back
> to `ok` even if its data later validates clean, because no source file defines
> a re-validation / clearing transition and the task forbids downgrading a worse
> status. Clearing is left to a future, explicitly specified rule.

### 7.2 Repair queue

Allowed `repair_reason` values (frozen, `SCHEMA_SPEC.md` §3.17):
`missing_price`, `bad_ohlc`, `mutation`, `provider_empty`, `outcome_missing`.
Every implemented (repairable) check maps to the single validation-related
reason **`bad_ohlc`** — the others are owned elsewhere (`missing_price` = M08
empty/failed fetch; `mutation` = M10; `provider_empty` = provider layer;
`outcome_missing` = outcome layer). No new enum is created. Volume-invalid rows (rule 7) are escalated to
`failed` but receive **no** repair row — the frozen enum has no suitable
reason for negative volume (gap G6).

For each **OHLC-invalid** row (rules 1–6), a repair task is enqueued with:

```text
repair_id     = uuid5(NAMESPACE_URL, "data_repair_queue:<ticker>:<date>:bad_ohlc")
ticker        = row ticker
repair_date   = row date            # the exact invalid trading date is known per row
repair_reason = "bad_ohlc"
attempts      = 0
max_attempts  = 3
last_attempt  = NULL
status        = "pending"
created_at    = now()
updated_at    = NULL
```

Insert uses `ON CONFLICT (repair_id) DO NOTHING RETURNING repair_id` (the Module
08 mechanism, `M08_…_SPEC.md` §7/§16): the frozen schema has no
`UNIQUE (ticker, repair_date, repair_reason)`, so dedup is achieved by deriving
`repair_id` deterministically from the logical key and conflicting on the
existing `repair_id` PRIMARY KEY. This is atomic at the DB layer (no
read-then-write race), so re-runs and concurrent runs cannot create duplicate
rows. `RETURNING` yields one row per actual insert, which is how
`repair_queue_enqueued` is counted (newly inserted rows only). No
legacy-compatibility pre-read is needed: Module 09 is the first writer of
`bad_ohlc` rows. Module 09 only inserts — it never reads queue rows for
processing, never updates `attempts` / `status`, and never deletes.

## 8. Assumptions, open spec gaps, blockers

- **A1.** Severity precedence `quarantined > failed > suspect > warning > ok`
  (the task's suggested conservative order; not otherwise defined in sources).
- **A2.** A non-positive OHLC price (`<= 0`) is treated as structurally invalid
  (a traded equity price is strictly positive). This is the price *domain*, not
  the tunable screening `min_price` threshold (which stays in strategy config
  per `CODING_STANDARDS.md` and is **not** used here).
- **A3.** ~~Removed — see G6.~~
- **A4.** Null `volume_adj` (always `NULL` from M08) and zero volume are **not**
  flagged — both are legitimate, so flagging them would be an invented rule.
- **G1.** Missing rows / missing expected trading days: not implemented — needs
  a trading-day calendar and an undefined "expected coverage" rule. (Module 08
  already enqueues `missing_price` for tickers that returned no data.)
- **G2.** Duplicate `(ticker, date)` rows: N/A — prevented by the `daily_prices`
  PRIMARY KEY.
- **G3.** Large price jumps / outliers: not implemented — no threshold defined,
  and split/mutation detection is Module 10's responsibility.
- **G4.** Stale / incomplete ticker coverage: not implemented — no threshold or
  calendar rule defined.
- **G5.** No `failed → ok` clearing transition is defined (see §7.1); Module 09
  never downgrades a worse status.
- **G6.** `negative_volume` repair enqueue: the frozen `repair_reason` enum
  (`missing_price`, `bad_ohlc`, `mutation`, `provider_empty`, `outcome_missing`)
  has no volume-specific value. `bad_ohlc` is a price-column reason and mapping
  negative volume to it would be an incorrect force-map per the prompt rule
  ("If no suitable enum exists, do not … force-map incorrectly"). Therefore
  negative-volume rows are escalated to `failed` but **no repair row is
  enqueued**. A future schema revision adding a `bad_volume` (or generic
  `bad_data`) reason would enable repair enqueue for this case.
- **Reserved statuses.** `warning`, `suspect`, `quarantined` are valid enum
  values but no source-defined check maps to them, so Module 09 does not assign
  them. They remain available for a future, explicitly specified rule.

These gaps are not blockers: the implemented structural subset is a meaningful,
non-trivial validator (null/inverted/out-of-range/non-positive/negative-volume
integrity) that sets real statuses and enqueues real repairs.

## 9. Transaction / idempotency strategy

Read, compute, and write phases are separated:

1. **Read** (read-only connection, closed before compute): one `SELECT` of the
   range's rows (`ticker, date, OHLC raw+adj, volume raw+adj`). No transaction
   held during computation.
2. **Compute** (pure Python, no DB): evaluate §6 checks per row; collect the set
   of failing `(ticker, date)` keys and the counts.
3. **Write** (single transaction): escalate statuses for failing rows (§7.1),
   then insert `bad_ohlc` repairs (§7.2). On any error → `ROLLBACK`, leaving no
   partial status updates and no partial repair rows, and a `failed`
   `ServiceResult` is returned.

Re-running the same range over the same data is stable: escalation only fires
when strictly more severe (so re-runs change zero statuses) and repair inserts
deduplicate on the deterministic `repair_id` (so re-runs enqueue zero rows).

## 10. DB-manager usage

All DB access is via `app.database.duckdb_manager.connect(db_role)` (read-only
for the range read, read-write for the single write transaction) or an injected
manager-like object in tests. Module 09 never imports `duckdb`, never opens a
path, never `ATTACH`es, and runs no DDL.

## 11. ServiceResult (exact metadata keys)

`rows_processed = rows_validated`. `metadata` carries exactly these keys on
**every** return path (guard failure, read failure, write failure, success):

```text
db_role
start_date              # ISO string
end_date                # ISO string
rows_validated          # rows read in range
rows_ok                 # rows whose current data passes all checks
rows_failed             # rows whose current data fails >= 1 check
status_updates_written  # daily_prices rows whose status actually escalated
repair_queue_enqueued   # newly inserted bad_ohlc repair rows (excludes ignored dups)
```

`rows_ok + rows_failed == rows_validated`. On the two guard paths (before any
read) all counts are `0`. Status: `failed` for an invalid `db_role`, invalid
range, read failure, or any write error; otherwise `success` (the validator
runs cleanly even when it finds invalid rows — finding bad data is a normal
successful outcome, recorded in the counts, not a warning). Logging uses the
project `RunIdLoggerAdapter` with the bound `run_id`; no `print()`.

## 12. Allowed / forbidden files

Allowed (created/changed by Module 09):

```text
app/services/validation/__init__.py
app/services/validation/data_validator.py
tests/test_data_validator.py
README.md                       # short Module 09 note only
M09_DATA_VALIDATOR_SPEC.md      # project root
```

Forbidden: modifying frozen Modules 01–08; provider/vendor calls; opening
DuckDB directly or bypassing the manager; DDL / schema changes; writing any
table other than `daily_prices.data_quality_status` (+ its `updated_at` audit
column) and `data_repair_queue`; modifying price values, OHLCV raw/adjusted
columns, `dividend_amount`, `split_ratio`, `adjustment_factor`, or
`mutation_flag`; processing/resolving/deleting repair rows; implementing
Module 10+ logic.

## 13. Tests

`tests/test_data_validator.py`, fully offline and isolated (temp DuckDB paths
via the same `tmp_db_paths` fixture used by the Module 07/08 suites; rows seeded
directly into `daily_prices`). Covers: import + exact signature and `run_id`
propagation; exact metadata keys on success / guard-failure / write-failure
paths; valid rows stay `ok`; rules 1–6 (OHLC-invalid) each escalate to
`failed` and enqueue one `bad_ohlc` repair; rule 7 (negative volume) escalates
to `failed` but enqueues **no** repair (gap G6); status precedence / no-downgrade
(a stored
`quarantined` is not lowered; a bad row escalates `ok`→`failed`; a good row does
not change a stored worse status); idempotency (re-run → zero status changes,
zero new repairs) and deterministic `repair_id`; repair defaults
(attempts/max_attempts/status/last_attempt/updated_at); no modification of price
values, OHLCV raw/adjusted, dividend/split, `adjustment_factor`, or
`mutation_flag`; no writes to forbidden tables or the simulation DB; existing
repair rows are not processed/updated/deleted; invalid `db_role` / `"simulation"`
and invalid date range fail before any DB write; transaction rollback leaves no
partial status updates or repair rows; no provider/network dependency; static
scan (no `duckdb` import, no `ATTACH`/DDL, no provider/vendor import, no
`print`, no forbidden-table or price-column writes). Module 01–08 tests pass
unchanged.

Suggested commit message: `module09_data_validator_stable`.
