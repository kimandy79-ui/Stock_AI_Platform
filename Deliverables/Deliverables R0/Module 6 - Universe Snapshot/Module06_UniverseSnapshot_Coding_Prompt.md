# Module 06 Coding Prompt — Universe Snapshot Engine (v2)

Use Project Instructions and Project Files.

You are a senior Python engineer implementing the next module of a local
swing-trading stock analyzer.

> **v2 changelog**: locked the public API, `yahoo_symbol` / `source` /
> `market_cap_bucket` rules, the absent-ticker lifecycle, `first_seen` /
> `last_seen` semantics, the idempotency transaction strategy, invalid-input
> policy, the `sector_etf_map` non-write rule, the exact `ServiceResult`
> metadata keys, and the required Project Files set.

---

## FILES ATTACHED

Attach exactly one file:

1. `stock_ai_platform_module05_stable.zip`
   * Current stable codebase after Modules 01–05.
   * Modules 01–05 are **frozen and accepted**. It already contains the
     Module 02 DuckDB manager, the Module 03 schema manager (which already
     creates `ticker_master`, `ticker_universe_snapshot`, and `sector_etf_map`),
     the Module 04 provider interface (incl. the `TickerInfo` DTO), and the
     Module 05 `YahooProvider`.

### Required Project Files (must be present in the Claude Project, not attached)

Source of truth lives in **Project Files**, in this priority order:

1. `M06_UNIVERSE_SNAPSHOT_SPEC.md` — *you create this in this task* (does not
   exist yet); until it exists, items 2–6 govern.
2. `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md`
3. `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
4. `M02_SCHEMA_SPEC.md` — merged DuckDB schema; `ticker_master`,
   `ticker_universe_snapshot`, `sector_etf_map` are §3.4–3.6.
5. `M04_PROVIDER_INTERFACE_SPEC.md` — the provider contract Module 06 *consumes*
   (and the `TickerInfo` DTO), never re-implements.
6. `M05_YAHOO_PROVIDER_SPEC.md` — the accepted concrete provider behavior.

All six of the above must be available in Project Files. Do not use old FULL /
PATCH / MINI-PATCH archives, duplicate manifests, or old prompt drafts as
implementation guidance. If sources conflict, **do not guess — report the
conflict** and recommend the safest interpretation.

---

## TASK

Implement ONLY **Module 06 — Universe Snapshot Engine**.

Per the roadmap and `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §7, Module 06
"maintains the ticker universe and monthly snapshots". It is the producer of
two tables already defined in the frozen Module 03 schema:

* `ticker_master` — the current known-symbol master (one row per ticker);
* `ticker_universe_snapshot` — an immutable monthly point-in-time membership
  snapshot used later by simulation to mitigate survivorship bias
  (decision 22.9: "simulation uses the historical snapshot nearest and not
  after the sim date").

Module 06 takes a provided set of provider-neutral `TickerInfo` entries, upserts
them into `ticker_master`, manages the lifecycle flags, writes a monthly
snapshot row per ticker into `ticker_universe_snapshot`, and returns a
`ServiceResult`.

---

## PUBLIC API (EXACT — do not vary)

Implement exactly this class and method. No alternative shapes (no
module-level-functions variant, no extra public methods).

```python
class UniverseSnapshotEngine:
    def apply_snapshot(
        self,
        entries: Iterable[TickerInfo],
        as_of_date: date,
        db_role: str = "prod",
        source: str = "manual",
        run_id: str | None = None,
    ) -> ServiceResult:
        ...
```

* `entries` — provider-neutral `TickerInfo` objects (from Module 04). Module 06
  does **not** fetch them; a caller (or a later pipeline module) supplies them.
* `as_of_date` — the calendar date the snapshot is taken for.
* `db_role` — `"prod"` or `"debug"` only; **never** `"simulation"`. Resolved
  only through `duckdb_manager`. Reject any other role as a `failed` result.
* `source` — written verbatim to `ticker_universe_snapshot.source`
  (default `"manual"`; e.g. `"yahoo"` when wired to a provider later).
* `run_id` — mint a fresh `uuid4` when `None` (mirror Module 03 `apply_schema`).

If the engine needs a constructor, keep it parameter-free (or accept only an
injected `duckdb_manager`-like hook for testing); document it in the spec. Do
not add a second public method.

---

## STRICT SCOPE — ALLOWED FILES

Add or modify only:

1. `app/services/universe/__init__.py`         (new package; create if absent)
2. `app/services/universe/universe_snapshot.py` (new — the engine)
3. `tests/test_universe_snapshot.py`           (new — tests)
4. `README.md`                                 (only a short Module 06 note)
5. `M06_UNIVERSE_SNAPSHOT_SPEC.md`             (new — module source-of-truth doc,
   delivered as a separate output file, not inside `docs/`)

*(If the established service layout in the stable zip / `ARCHITECTURE.md`
differs from `app/services/universe/`, follow the existing layout and state the
chosen path in your design notes.)*

`requirements.txt` and `pyproject.toml` must **NOT** be modified — no new
dependencies. Polars is preferred for transformations; pandas only if
unavoidable.

---

## STRICT SCOPE — FORBIDDEN

Do NOT modify any Module 01–05 file, including (non-exhaustive):
`app/providers/*`, `app/database/duckdb_manager.py`,
`app/database/schema_manager.py`, `app/config/*`, `app/utils/*`, `conftest.py`,
`requirements.txt`, `pyproject.toml`, `.gitignore`, `.env.example`, any
`docs/*.md`, and every existing test under `tests/`.

Do NOT:

* implement benchmark / sector-ETF loading — that is **Module 07**;
* **write to `sector_etf_map`** — it is schema context only and is owned by
  Module 07. Module 06 must not `INSERT` / `UPDATE` it;
* implement daily price ingestion — that is **Module 08**;
* implement validation, mutation detection, features, screening, scoring,
  proposals, outcomes, simulation, AI review, or dashboard logic;
* call Yahoo / `yfinance` directly, or import `yfinance`. Module 06 receives
  `TickerInfo` entries; if a future caller wires a provider, it must go through
  the Module 04/05 interface (`MarketDataProvider`), never around it;
* **compute market cap** or call any market-data API to populate
  `market_cap_bucket`;
* open DuckDB directly, build connection strings, or `ATTACH` arbitrary paths —
  **all** DB access goes through `app.database.duckdb_manager`;
* run `ALTER TABLE`, `CREATE TABLE`, or `CREATE TYPE` — Module 03 already created
  the schema; Module 06 only `INSERT` / `UPDATE` / `DELETE`-within-month on
  existing tables;
* write to `simulation.duckdb`;
* add new DTOs, metadata keys, or error kinds to the frozen provider contract;
* hardcode tunable thresholds — universe *filters* (e.g. `min_price`,
  `allowed_symbol_types`, `exclude_benchmarks`) belong to strategy config and
  are **not** Module 06's job. Module 06 records what it is given.

---

## REQUIRED BEHAVIOR (LOCKED RULES)

### snapshot_month normalization

```text
snapshot_month = date(as_of_date.year, as_of_date.month, 1)
```

### `ticker_master` mapping and lifecycle

Map each `TickerInfo` field directly; lifecycle fields are assigned by Module 06:

```text
ticker_master.ticker         = TickerInfo.ticker
ticker_master.yahoo_symbol   = TickerInfo.ticker          # V1 rule: identity
ticker_master.company_name   = TickerInfo.company_name
ticker_master.exchange       = TickerInfo.exchange
ticker_master.sector         = TickerInfo.sector
ticker_master.industry       = TickerInfo.industry
ticker_master.security_type  = TickerInfo.security_type
ticker_master.symbol_type    = TickerInfo.symbol_type     # already validated by TickerInfo
```

Lifecycle rules:

```text
New ticker (not in ticker_master):
    active_flag   = TRUE
    delisted_flag = FALSE
    first_seen    = snapshot_month
    last_seen     = snapshot_month
    last_updated  = now()

Existing ticker present in current input:
    first_seen    = unchanged
    last_seen     = snapshot_month
    active_flag   = TRUE
    delisted_flag = unchanged (do not flip to TRUE here)
    last_updated  = now()
    (refresh mutable metadata: company_name, exchange, sector, industry,
     security_type, symbol_type, yahoo_symbol)

Previously known ticker ABSENT from current input:
    active_flag   = FALSE
    delisted_flag = FALSE        # absence alone is NOT delisting
    last_seen     = unchanged
    last_updated  = now()
```

Rationale (state this in the spec): absence from one monthly input is not proof
of delisting given the V1 "historical delisted data is incomplete" limitation;
`delisted_flag` is reserved for an explicit signal a later module may provide.

### `ticker_universe_snapshot` write

One row per `(snapshot_month, ticker)` (the PK), for every ticker in the current
input:

```text
snapshot_month    = snapshot_month
ticker            = TickerInfo.ticker
exchange          = TickerInfo.exchange
sector            = TickerInfo.sector
industry          = TickerInfo.industry
market_cap_bucket = NULL          # V1: always NULL; do not compute market cap
active_flag       = TRUE          # snapshot captures present-in-input membership
source            = source        # the apply_snapshot() argument
created_at        = now()
```

### Idempotency (LOCKED strategy)

Re-running the same `snapshot_month` must not duplicate or error. Use a single
transaction:

```text
BEGIN;
  -- ticker_master upserts + lifecycle updates
  DELETE FROM ticker_universe_snapshot WHERE snapshot_month = ?;
  INSERT INTO ticker_universe_snapshot (...) VALUES (...);  -- current input
COMMIT;
```

On any error inside the transaction, roll back and return a `failed`
`ServiceResult` (no partial writes).

### Invalid / malformed input policy

`TickerInfo` already validates `ticker` non-empty and `symbol_type ∈
ALLOWED_SYMBOL_TYPES` at construction, so a well-formed `TickerInfo` cannot
carry an invalid `symbol_type`. For input that is **not** a `TickerInfo`
(wrong type in the iterable) or otherwise unusable, the engine must **skip the
item with a warning** and continue (count it in `skipped_rows`), rather than
crash. If the entire input is unusable, still return a valid `ServiceResult`
(`success_with_warnings` with `valid_rows == 0`, or `failed` if the DB itself is
unavailable — document which). Duplicate tickers within one input: keep the
last occurrence and warn.

### Empty input

Zero entries → `success` (no rows written, snapshot for the month becomes
empty per the delete-then-insert rule). Do not crash.

---

## ServiceResult (EXACT metadata keys)

Return `app.utils.service_result.ServiceResult`. `status ∈ {success,
success_with_warnings, failed}`. `rows_processed` = number of snapshot rows
written (= `snapshot_rows`). `metadata` must contain **exactly** these keys:

```text
snapshot_month           # ISO date string or datetime.date (document which)
db_role                  # "prod" | "debug"
source                   # the source argument
input_rows               # len of the input iterable as received
valid_rows               # TickerInfo entries accepted
skipped_rows             # entries skipped (bad type / duplicates)
tickers_inserted         # new ticker_master rows
tickers_updated          # existing ticker_master rows refreshed (present in input)
tickers_marked_inactive  # previously-known tickers absent from input -> active_flag FALSE
snapshot_rows            # rows written to ticker_universe_snapshot
```

Log start / end / rows / warnings / errors via the bound-`run_id` logger
(`logging_config.get_logger(__name__, run_id)`). No `print()`. Do not raise for
expected conditions; return a `ServiceResult`.

---

## REQUIRED TESTS

Create `tests/test_universe_snapshot.py`. Tests run **fully offline** — no
network, no live provider — and must **never** touch real prod / debug /
simulation DB files. Redirect DuckDB paths into pytest `tmp_path` via
`monkeypatch.setattr(settings, ...)` and apply the real Module 03 schema to that
temp DB in a fixture (mirror `tests/test_schema_manager.py` and
`tests/test_duckdb_manager.py`). Feed input as in-test `TickerInfo` lists.

Cover at minimum:

1. **Import smoke**: `UniverseSnapshotEngine` imports; `apply_snapshot` present
   with the exact signature.
2. **Fresh insert**: empty `ticker_master` + N entries → `success`, N master
   rows with the locked lifecycle flags, N snapshot rows; metadata counts
   correct (`tickers_inserted == N`, `snapshot_rows == N`).
3. **Re-run idempotency**: same month twice → no duplicate
   `ticker_universe_snapshot` rows, no error (delete-then-insert verified).
4. **Update path**: a ticker already present → `last_seen = snapshot_month`,
   `first_seen` unchanged, stays active, metadata refreshed,
   `tickers_updated` counted.
5. **Absent-ticker lifecycle**: a previously-known ticker missing from the new
   input → `active_flag = FALSE`, `delisted_flag = FALSE`, `last_seen`
   unchanged; `tickers_marked_inactive` counted.
6. **snapshot_month normalization**: an `as_of_date` mid-month → snapshot key is
   first-of-month.
7. **yahoo_symbol identity**: `ticker_master.yahoo_symbol == ticker`.
8. **market_cap_bucket NULL**: every snapshot row has NULL `market_cap_bucket`.
9. **source propagation**: a non-default `source` lands in
   `ticker_universe_snapshot.source` and in metadata.
10. **Invalid input**: a non-`TickerInfo` item in the iterable is skipped with a
    warning and counted in `skipped_rows`; duplicates handled per policy.
11. **Empty input**: zero entries → `success`, zero snapshot rows, no crash.
12. **db_role guard**: `db_role="simulation"` (or any invalid role) →
    `failed`, no writes.
13. **Transaction rollback**: a forced mid-transaction failure leaves no partial
    rows (no orphaned snapshot rows for the month).
14. **No-sector_etf_map-write**: `sector_etf_map` is untouched by
    `apply_snapshot`.
15. **DB isolation / static scan**: the module does not `import duckdb`, does not
    `import yfinance`, has no `duckdb.connect(`, no `ATTACH`, no `ALTER TABLE` /
    `CREATE TABLE`, and no `print(` (mirror the token scan in
    `tests/test_provider_interface.py` / `tests/test_schema_manager.py`).
16. **ServiceResult contract**: real `ServiceResult`, valid status, and the
    **exact** metadata key set above present.

Existing Module 01–05 tests must continue to pass unchanged.

---

## MODULE-SPECIFIC SOURCE OF TRUTH

Create **`M06_UNIVERSE_SNAPSHOT_SPEC.md`** (separate output file, not inside
`docs/`). Derive it from the Project Files listed above, this task, and the
accepted implementation. Do **not** invent new architecture or override
higher-priority docs; if it would conflict, report the conflict instead.

Keep it concise and implementation-oriented. Include: purpose; scope / non-scope;
source-of-truth priority; the exact public API (`UniverseSnapshotEngine.apply_snapshot`
signature); the `TickerInfo → ticker_master` mapping incl. the `yahoo_symbol`
identity rule; lifecycle-flag rules (incl. the absent-ticker policy and why
`delisted_flag` is not flipped); `snapshot_month` normalization; the
`market_cap_bucket = NULL` V1 rule; the `source` handling; the delete-then-insert
transactional idempotency policy; invalid-input policy; the `sector_etf_map`
non-write rule; the exact `ServiceResult` metadata keys; allowed/forbidden files;
testing requirements; an acceptance checklist; and assumptions / open questions.

---

## OUTPUT REQUIRED (per Project Instructions §8)

1. Updated project zip. Top-level folder `stock_ai_platform/`; preserve the
   Module 05 layout.
2. List of added/changed files.
3. `M06_UNIVERSE_SNAPSHOT_SPEC.md` as a separate downloadable file.
4. Short design notes (lifecycle decision, idempotency transaction, how the
   DB manager is used without opening arbitrary paths, how tests stay offline +
   isolated, how frozen Modules 01–05 are protected).
5. Test command and full results:
   ```bash
   pytest -q
   ```
   If the environment cannot install `duckdb` / `polars`, say so clearly and
   list the isolated/static checks performed, plus
   `pytest -q tests/test_universe_snapshot.py`.
6. Any assumptions (do not hide them; do not assume where a doc is explicit).
7. Suggested commit message:
   ```text
   module06_universe_snapshot_stable
   ```

---

## STARTING STEPS

Read in this order:
1. `02_PROJECT_IMPLEMENTATION_CONTEXT.md` §7 (Module 06), §6 (pipeline step 4),
   §22.9 (monthly snapshots decision).
2. `M02_SCHEMA_SPEC.md` §3.4 `ticker_master`, §3.5 `ticker_universe_snapshot`,
   §3.6 `sector_etf_map`, and the `symbol_type` value catalog.
3. `app/database/duckdb_manager.py` (the only DB entry point) and
   `app/database/schema_manager.py` (the `run_id` + `ServiceResult` + transaction
   pattern to mirror).
4. `app/providers/provider_interface.py` (`TickerInfo` fields + validation) —
   consume, do not modify.
5. `tests/test_schema_manager.py` (the `tmp_path` + `monkeypatch.setattr(settings)`
   isolation fixture and the static-scan discipline).

Then implement Module 06.

Do not reopen the Module 03 schema or the Module 04 contract.
Do not implement Module 07 or later.
Do not modify any Module 01–05 file or any `docs/*.md`.
Do not call Yahoo outside the provider layer; do not open DuckDB outside the
manager.
