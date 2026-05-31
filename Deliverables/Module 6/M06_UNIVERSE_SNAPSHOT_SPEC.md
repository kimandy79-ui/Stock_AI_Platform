# M06 — Universe Snapshot Engine — Module Spec

Status: accepted (Module 06). Concise, implementation-oriented source of truth
for the universe snapshot engine. Derived from the Module 06 task,
`01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md`,
`02_PROJECT_IMPLEMENTATION_CONTEXT.md`, `M02_SCHEMA_SPEC.md`,
`M04_PROVIDER_INTERFACE_SPEC.md`, and `M05_YAHOO_PROVIDER_SPEC.md`. This spec
does not introduce new architecture and does not override any higher-priority
document.

## 1. Purpose

Maintain the ticker universe and produce immutable monthly point-in-time
membership snapshots. Module 06 is the sole producer of two tables created by
the frozen Module 03 schema:

- `ticker_master` — current known-symbol master, one row per ticker
  (`M02_SCHEMA_SPEC.md` §3.4);
- `ticker_universe_snapshot` — one immutable row per `(snapshot_month, ticker)`,
  used later by simulation to mitigate survivorship bias
  (`02_PROJECT_IMPLEMENTATION_CONTEXT.md` decision 22.9: "simulation uses the
  historical snapshot nearest and not after the sim date") (`M02_SCHEMA_SPEC.md`
  §3.5).

## 2. Scope / non-scope

In scope: accept provider-neutral `TickerInfo` entries from a caller; upsert
`ticker_master`; assign lifecycle flags; write the monthly snapshot;
return a `ServiceResult`.

Out of scope (owned elsewhere): fetching tickers / calling Yahoo or `yfinance`
(Modules 04/05); benchmark & sector-ETF loading and any write to
`sector_etf_map` (Module 07); daily price ingestion (Module 08); validation,
mutation detection, features, screening, scoring, proposals, outcomes,
simulation, AI review, dashboard. Module 06 never computes market cap, never
opens DuckDB directly or `ATTACH`es, never runs DDL, and never writes to
`simulation.duckdb`.

## 3. Source-of-truth priority

1. This file (`M06_UNIVERSE_SNAPSHOT_SPEC.md`).
2. `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md`.
3. `02_PROJECT_IMPLEMENTATION_CONTEXT.md`.
4. `M02_SCHEMA_SPEC.md` (§3.4–3.6 + `symbol_type` catalog).
5. `M04_PROVIDER_INTERFACE_SPEC.md` (the `TickerInfo` DTO — consumed, never
   re-implemented).
6. `M05_YAHOO_PROVIDER_SPEC.md`.

On conflict: do not guess — report the conflict and recommend the safest
interpretation. (No conflicts were found: the frozen `schema_manager.py` DDL
matches `M02_SCHEMA_SPEC.md` §3.4–3.5 exactly, and the `TickerInfo` fields match
`M04`.)

## 4. Public API (exact)

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

There is exactly one public method. The constructor is parameter-free except for
an optional `db_manager` hook used only for test injection
(`UniverseSnapshotEngine(db_manager=...)`); when omitted, the real
`app.database.duckdb_manager` is used. No module-level-function variant, no extra
public methods.

- `entries` — provider-neutral `TickerInfo` objects; Module 06 does not fetch
  them.
- `as_of_date` — calendar date the snapshot is taken for.
- `db_role` — `"prod"` or `"debug"` only, resolved only through
  `duckdb_manager`; `"simulation"` and any other value yield a `failed` result
  with no writes.
- `source` — written verbatim to `ticker_universe_snapshot.source`.
- `run_id` — a fresh `uuid4` is minted when `None` (mirrors Module 03
  `apply_schema`).

## 5. `TickerInfo` → `ticker_master` mapping

```
ticker_master.ticker         = TickerInfo.ticker
ticker_master.yahoo_symbol   = TickerInfo.ticker        # V1 rule: identity
ticker_master.company_name   = TickerInfo.company_name
ticker_master.exchange       = TickerInfo.exchange
ticker_master.sector         = TickerInfo.sector
ticker_master.industry       = TickerInfo.industry
ticker_master.security_type  = TickerInfo.security_type
ticker_master.symbol_type    = TickerInfo.symbol_type   # validated by TickerInfo
```

`yahoo_symbol` is the ticker verbatim (V1 identity rule). A later module may
introduce a non-identity mapping; until then identity holds.

## 6. Lifecycle-flag rules

```
New ticker (absent from ticker_master):
    active_flag   = TRUE
    delisted_flag = FALSE
    first_seen    = snapshot_month
    last_seen     = snapshot_month
    last_updated  = now()

Existing ticker present in the current input:
    first_seen    = unchanged
    last_seen     = snapshot_month
    active_flag   = TRUE
    delisted_flag = unchanged (NOT flipped here)
    last_updated  = now()
    mutable metadata refreshed: company_name, exchange, sector, industry,
        security_type, symbol_type, yahoo_symbol

Previously-known ticker ABSENT from the current input:
    active_flag   = FALSE
    delisted_flag = FALSE        # absence alone is NOT delisting
    last_seen     = unchanged
    last_updated  = now()
```

Why `delisted_flag` is not flipped on absence: V1 historical delisted data is
incomplete, so absence from one monthly input is not proof of delisting.
`delisted_flag` is reserved for an explicit delisting signal a later module may
provide.

`tickers_marked_inactive` counts every previously-known ticker absent from the
current input (it is set to `active_flag = FALSE` each run; the operation is
idempotent in DB state).

## 7. `snapshot_month` normalization

```
snapshot_month = date(as_of_date.year, as_of_date.month, 1)
```

## 8. `ticker_universe_snapshot` write

One row per `(snapshot_month, ticker)` (the PK) for every accepted ticker in the
current input:

```
snapshot_month    = snapshot_month
ticker            = TickerInfo.ticker
exchange          = TickerInfo.exchange
sector            = TickerInfo.sector
industry          = TickerInfo.industry
market_cap_bucket = NULL          # V1: always NULL; market cap is never computed
active_flag       = TRUE          # snapshot captures present-in-input membership
source            = source        # the apply_snapshot() argument
created_at        = now()
```

## 9. `market_cap_bucket = NULL` (V1)

Always `NULL`. Module 06 never computes market cap and never calls a market-data
API to populate it. Bucketing is out of scope for V1.

## 10. `source` handling

Written verbatim to `ticker_universe_snapshot.source` for every snapshot row and
echoed in `metadata["source"]`. Default `"manual"`; e.g. `"yahoo"` when a future
caller wires a provider through the Module 04/05 interface.

## 11. Idempotency (transactional, locked strategy)

A single transaction per call:

```
BEGIN TRANSACTION;
  -- ticker_master upserts + lifecycle updates (present + absent)
  DELETE FROM ticker_universe_snapshot WHERE snapshot_month = ?;
  INSERT INTO ticker_universe_snapshot (...) VALUES (...);  -- current input
COMMIT;
```

Re-running the same `snapshot_month` never duplicates or errors (the delete
clears the month before re-insert). On any error inside the transaction the
engine issues `ROLLBACK` and returns a `failed` `ServiceResult` with no partial
writes (no orphaned snapshot rows, no half-applied master upserts).

## 12. Invalid / malformed input policy

`TickerInfo` validates `ticker` non-empty and `symbol_type ∈
ALLOWED_SYMBOL_TYPES` at construction, so a well-formed `TickerInfo` cannot carry
an invalid `symbol_type`. For an item in the iterable that is **not** a
`TickerInfo`, the engine skips it with a warning and continues (counted in
`skipped_rows`). Duplicate tickers within one input keep the **last** occurrence
and warn; each dropped earlier occurrence is counted in `skipped_rows`. Invariant:
`input_rows == valid_rows + skipped_rows`.

If the entire input is unusable (no valid `TickerInfo`), the engine returns
`success_with_warnings` with `valid_rows == 0` and `snapshot_rows == 0` (the
month is still cleared by the delete). A `failed` result is reserved for an
invalid `db_role` or a database error.

## 13. Empty input

Zero entries → `success` (no warnings). The month is cleared (delete-then-insert
with zero inserts) and no rows are written. Does not crash.

## 14. `sector_etf_map` non-write rule

`sector_etf_map` is schema context only and is owned by Module 07. Module 06
never `INSERT`s/`UPDATE`s/`DELETE`s it.

## 15. `ServiceResult` and exact metadata keys

Returns `app.utils.service_result.ServiceResult`. `status ∈ {success,
success_with_warnings, failed}`. `rows_processed == snapshot_rows`. Logging via
the bound-`run_id` logger (`logging_config.get_logger(__name__, run_id)`); no
`print()`; expected conditions return a `ServiceResult` rather than raising.

`metadata` contains exactly these keys (always present, including on the
guard-failure path):

```
snapshot_month           # ISO date string, "YYYY-MM-01"
db_role                  # "prod" | "debug" (echoes the rejected value on guard failure)
source                   # the source argument
input_rows               # len of the input iterable as received
valid_rows               # distinct TickerInfo entries accepted
skipped_rows             # entries skipped (bad type + dropped duplicates)
tickers_inserted         # new ticker_master rows
tickers_updated          # existing ticker_master rows refreshed (present in input)
tickers_marked_inactive  # previously-known tickers absent from input -> active_flag FALSE
snapshot_rows            # rows written to ticker_universe_snapshot
```

`snapshot_month` is delivered as an ISO date string (`"YYYY-MM-01"`); the
`ticker_universe_snapshot.snapshot_month` column itself is a DATE.

## 16. Allowed / forbidden files

Added/modified by Module 06 only:

- `app/services/__init__.py` (new service-layer package marker)
- `app/services/universe/__init__.py` (new package)
- `app/services/universe/universe_snapshot.py` (the engine)
- `tests/test_universe_snapshot.py` (tests)
- `README.md` (short Module 06 note only)
- `M06_UNIVERSE_SNAPSHOT_SPEC.md` (this file, project root)

The chosen service path `app/services/universe/` matches the architecture's
prescribed `app/services/<subpackage>/` layout
(`02_PROJECT_IMPLEMENTATION_CONTEXT.md` directory tree). No Module 01–05 file,
no `docs/*.md`, and neither `requirements.txt` nor `pyproject.toml` are modified;
no new dependency is added.

## 17. Testing requirements

`tests/test_universe_snapshot.py` runs fully offline (no network, no live
provider) and never touches real prod/debug/simulation DB files: DuckDB paths
are redirected into pytest `tmp_path` via `monkeypatch.setattr(settings, ...)`
and the real Module 03 schema is applied to the temp DB in a fixture (mirrors
`tests/test_schema_manager.py`). Coverage: import smoke + exact signature; fresh
insert; re-run idempotency; update path; absent-ticker lifecycle;
`snapshot_month` normalization; `yahoo_symbol` identity; `market_cap_bucket`
NULL; `source` propagation; invalid/duplicate input; empty input; `db_role`
guard (incl. `"simulation"`); transaction rollback; `sector_etf_map` untouched;
static scan (no `import duckdb`, no `duckdb.connect(`, no `import yfinance`, no
`ATTACH`, no `ALTER`/`CREATE TABLE`/`CREATE TYPE`, no `print(`); `ServiceResult`
contract with the exact metadata key set. Existing Module 01–05 tests must keep
passing unchanged.

## 18. Acceptance checklist

- [x] Exactly one public method `apply_snapshot` with the locked signature.
- [x] All DB access via `app.database.duckdb_manager`; no arbitrary paths, no
      `ATTACH`, no DDL.
- [x] `prod`/`debug` only; `simulation` and other roles → `failed`, no writes.
- [x] `snapshot_month = first-of-month`.
- [x] `yahoo_symbol == ticker`; lifecycle flags per §6; `delisted_flag` not
      flipped on absence.
- [x] `market_cap_bucket` always NULL; `source` written verbatim.
- [x] Delete-then-insert idempotency inside one transaction; rollback on error.
- [x] Invalid/duplicate input skipped+warned; empty input → success.
- [x] `sector_etf_map` never written.
- [x] Exact `ServiceResult` metadata key set; `rows_processed == snapshot_rows`;
      `run_id` logging; no `print()`.
- [x] No new dependency; `requirements.txt` / `pyproject.toml` unchanged.

## 19. Assumptions / open questions

- A1. `snapshot_month` is reported in metadata as an ISO string `"YYYY-MM-01"`
  (the spec permits string or `date`; string chosen for serialization safety).
  The DB column remains a DATE.
- A2. The per-run transformation (dedup + keyed upserts) is implemented in plain
  Python rather than Polars: it is a handful of upserts, not a columnar
  transform, so pulling in Polars would add weight without benefit. "Polars-first"
  is honored where it matters (feature/screening transforms). No new dependency
  is added either way.
- A3. `tickers_marked_inactive` counts all previously-known tickers absent from
  the current input (re-deactivation is idempotent in DB state).
- A4. On the `db_role`-guard failure path the engine still returns the full
  metadata key set (counts zero, `db_role` echoes the rejected value) to keep the
  "exact keys" contract uniform across all return paths.
- A5. Open: market-cap bucketing and an explicit delisting signal are deferred to
  later modules; this spec will be revisited if/when they land.
