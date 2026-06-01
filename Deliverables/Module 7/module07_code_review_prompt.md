# ChatGPT Code Review Prompt — Module 07: Benchmark / Sector ETF Loader

> **How to use:** Paste this prompt into ChatGPT, then append the full source of
> `benchmark_etf_loader.py` and `test_benchmark_etf_loader.py` at the bottom
> before submitting.

---

You are a senior Python engineer performing a code review for the
**Swing Trading Stock Analyzer** project.

Review the following **Module 07** implementation:
**Benchmark / Sector ETF Loader** (`app/services/benchmarks/benchmark_etf_loader.py`).

The module's job is to load benchmark and sector ETF price history
(SPY, QQQ, ^VIX, sector SPDRs) via an abstract provider interface,
write bars to `daily_prices`, upsert symbols into `ticker_master`,
seed `sector_etf_map`, and return a `ServiceResult`.

---

## Review dimensions

### 1. Correctness

- Does the `^VIX` handling (`close_raw = close_adj`, `volume_raw = NULL`) look
  correct and complete?
- Are the SQL upsert parameter counts consistent with column counts?
- Are `dividend_amount` and `split_ratio` defaults (`0` / `1` for `None`)
  applied correctly?
- Does the `ticker_master` non-clobbering `UPDATE` only touch the four allowed
  columns (`yahoo_symbol`, `symbol_type`, `active_flag`, `last_updated`)?
- Is `sector_etf_map` seeding truly insert-or-ignore with no updates to existing
  rows?

### 2. Robustness and failure handling

- Is the `db_role` guard applied before any DB connection or write?
- Are per-symbol provider failures (failed status, zero bars, raised exceptions)
  isolated correctly so one failure does not abort the whole run?
- Does the single-transaction design prevent partial writes on DB errors?
- Are there any edge cases where `metadata` might be missing a required key?

### 3. Architecture and boundaries

- Does the module stay within its boundaries?
  - No direct `duckdb` import.
  - No `yfinance` or `yahoo_provider` reference.
  - No DDL (`CREATE TABLE`, `ALTER TABLE`, `ATTACH`).
  - No write to `ticker_universe_snapshot`.
  - No write to `simulation.duckdb`.
- Is all DB access going through `duckdb_manager` only?
- Is the provider interface used correctly
  (`PriceHistoryRequest` → `metadata["bars"]`)?

### 4. Code quality

- Are the SQL constants readable and maintainable?
- Is the separation of the fetch phase (no DB writes) and write phase
  (single transaction) clear and enforced?
- Is `_daily_price_params` easy to audit against the schema column list?
- Are docstrings accurate and consistent with the implementation?
- Any logging gaps — missing start/end log, missing per-symbol outcome log?

### 5. Test coverage

- Are the tests in `tests/test_benchmark_etf_loader.py` thorough enough?
- Are there edge cases or failure paths not covered?
- Is `_FakeProvider` a faithful implementation of the `MarketDataProvider`
  contract (all four abstract methods, correct `ServiceResult` shape)?
- Do the static-scan tests (`test_no_direct_duckdb_import_or_connect`, etc.)
  actually catch what they claim to?
- Is the `_FailingConn` / `_FailingManager` rollback test realistic enough to
  prove the transaction guarantee?

---

## Output format

For **each issue found**, provide:

| Field | Content |
|---|---|
| **Severity** | `critical` / `major` / `minor` / `suggestion` |
| **Location** | File name and function or line |
| **Problem** | What is wrong or risky |
| **Fix** | A concrete code change or recommendation |

If the code looks correct in a dimension, say so briefly.
Do not pad the review — skip dimensions where there is nothing to flag.

---

## Source code to review

Paste the full contents of the following files here before submitting:

### `app/services/benchmarks/benchmark_etf_loader.py`

```python
# PASTE FILE CONTENTS HERE
```

### `tests/test_benchmark_etf_loader.py`

```python
# PASTE FILE CONTENTS HERE
```
