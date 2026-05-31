# Module 05 Coding Prompt — YahooProvider

You are a senior Python engineer implementing the next module of a local
swing-trading stock analyzer.

## FILES ATTACHED

I am attaching exactly three files:

1. `stock_ai_platform_module04_stable.zip`

   * Current stable codebase after Modules 01, 02, 03, and 04.
   * Modules 01–04 are frozen and accepted.
   * This zip already contains the accepted project layout, docs, tests,
     the Module 03 schema manager, and the Module 04 provider interface
     (`app/providers/provider_interface.py`).

2. `StockAnalyzer_Shared_Context_Pack_v1_3 (M01).zip`

   * Latest shared context pack.
   * Contains:
     * `MASTER_SPEC.md`
     * `ARCHITECTURE.md`
     * `DECISIONS_LOG.md`
     * `TODO_ROADMAP.md`
     * `CODING_STANDARDS.md`
     * `manifest.json`

3. `PROVIDER_INTERFACE_SPEC.md`

   * The Module 04 contract YahooProvider must implement as-is.
   * Defines `MarketDataProvider`, the DTOs, the method signatures, the
     `ServiceResult` metadata keys, the error kinds, and the empty/error
     semantics. Module 05 must conform to this contract exactly and must not
     change it.

Do not ask for old archives. The implementation base is
`stock_ai_platform_module04_stable.zip`.

---

## SOURCE OF TRUTH PRIORITY

Use the sources in this priority order:

1. `PROVIDER_INTERFACE_SPEC.md`

   * Highest authority for the **interface contract** YahooProvider implements:
     class to subclass (`MarketDataProvider`), DTO names/fields, method
     signatures, `ServiceResult` metadata keys (§8), error kinds (§9), and
     empty/error/partial semantics (§7.3, §9).
   * Module 05 must not invent new provider methods, DTOs, enums, or metadata
     keys beyond this contract.

2. `MASTER_SPEC.md` (and the rest of the shared context pack)

   * Highest authority for **provider behavior rules**: V1 provider is
     YahooProvider via the provider interface (§5); raw vs adjusted price rules
     (§7); VIX handling (`^VIX`, `symbol_type = index`, `close_raw = close_adj`,
     volume may be NULL/0, excluded from screening) (§5); required benchmark
     symbols (§5); symbol types (§6); accepted V1 limitations
     ("YahooProvider is research-grade", earnings may be LOW confidence).
   * `ARCHITECTURE.md` for module boundaries and the rule
     "do not call Yahoo directly outside the provider layer".
   * `CODING_STANDARDS.md` for typing, docstrings, logging, `ServiceResult`
     conventions, testing discipline, and dependency rules.

3. Existing code inside `stock_ai_platform_module04_stable.zip`

   * Use as the implementation base.
   * Reuse `app.utils.service_result.ServiceResult`,
     `app.config.constants` symbol/benchmark vocabulary, and
     `app.utils.logging_config`.
   * Do not modify frozen Module 01/02/03/04 behavior.

If `PROVIDER_INTERFACE_SPEC.md` (contract) and `MASTER_SPEC.md` (behavior) ever
conflict, do not guess. Stop and report the conflict.

The **CLARIFICATIONS TO PREVENT AMBIGUITY** section below resolves yfinance-
specific implementation questions. It is binding for Module 05 and overrides any
contradictory guess; if a clarification ever conflicts with the contract in
`PROVIDER_INTERFACE_SPEC.md`, the contract wins and you must stop and report it.

---

## TASK

Implement ONLY **Module 05 — YahooProvider**.

Module 05 is the FIRST concrete `MarketDataProvider`. It:

* implements `MarketDataProvider` from Module 04 (`abc.ABC` subclass);
* fetches daily prices, ticker info, and earnings dates from Yahoo via
  `yfinance`, isolated entirely inside this provider layer;
* returns the Module 04 DTOs wrapped in `ServiceResult`, honoring the §7.3/§9
  success / empty / partial / error semantics;
* honors the raw vs adjusted price rules (`MASTER_SPEC.md` §7) and VIX handling
  (`MASTER_SPEC.md` §5).

Module 05 must NOT persist anything, open DuckDB, validate business semantics,
or do screening/scoring/trading/simulation/dashboard work. It only fetches and
returns provider-neutral DTOs.

---

## STRICT SCOPE — ALLOWED

You may add or modify only:

1. `app/providers/yahoo_provider.py`            (new — the concrete provider)
2. `app/providers/__init__.py`                  (only to export `YahooProvider`)
3. `tests/test_yahoo_provider.py`               (new — tests)
4. `README.md`                                  (only a short Module 05 note)

`requirements.txt` and `pyproject.toml` must **NOT** be modified: `yfinance`
(>=0.2.40) and `pandas` (>=2.2) are already declared in the Module 04 stable
base (see CLARIFICATION 11). Do not touch dependency files.

Prefer the simplest implementation. A single `yahoo_provider.py` containing the
`YahooProvider` class is expected. Do not split it into many files. Do not add a
retry framework, connection pool, threading, async, or caching layer in V1.

---

## STRICT SCOPE — FORBIDDEN

Do NOT modify any Module 01/02/03/04 file, including:

* `app/providers/provider_interface.py`   (the Module 04 contract is frozen)
* `app/database/duckdb_manager.py`
* `app/database/schema_manager.py`
* `app/config/settings.py`
* `app/config/constants.py`
* `app/utils/logging_config.py`
* `app/utils/service_result.py`
* existing tests:
  * `tests/test_project_skeleton.py`
  * `tests/test_duckdb_manager.py`
  * `tests/test_schema_manager.py`
  * `tests/test_provider_interface.py`
* `conftest.py`
* `requirements.txt`
* `pyproject.toml`
* `.gitignore`
* `.env.example`
* any `docs/*.md`
* any file inside the shared context pack

Do NOT:

* change, re-declare, or subclass anything in `provider_interface.py` other
  than subclassing `MarketDataProvider` and constructing the existing DTOs
* invent new DTOs, new metadata keys, or new error kinds
* open DuckDB connections / import `duckdb` / import `app.database`
* read or write any database or the schema
* implement ticker-universe persistence (Module 06), benchmark loading
  (Module 07), daily price ingestion (Module 08), validation (Module 09),
  mutation detection (Module 10), screening, scoring, trading, simulation,
  AI review, or dashboard logic
* call Yahoo from anywhere except inside `YahooProvider`
* add third-party dependencies beyond `yfinance` (already declared) and the
  already-declared `pandas` used only at the yfinance boundary
* add a retry framework, async, threading, or a caching layer
* scrape Yahoo web pages or attempt full US-universe discovery (see
  CLARIFICATIONS 8 and 9)

---

## REQUIRED IMPLEMENTATION

Implement `app/providers/yahoo_provider.py` so that:

```python
class YahooProvider(MarketDataProvider):
    ...
```

implements all four abstract methods with the EXACT Module 04 signatures:

```python
def get_capabilities(self) -> ServiceResult: ...
def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult: ...
def list_symbols(self, symbol_type: str | None = None) -> ServiceResult: ...
def get_earnings(self, ticker: str) -> ServiceResult: ...
```

### Behavior requirements (per `PROVIDER_INTERFACE_SPEC.md` §7.3, §8, §9)

* All four methods return a real `ServiceResult` from
  `app.utils.service_result`. DTOs go in `ServiceResult.metadata` under the
  documented keys: `capabilities`, `bars`, `symbols`, `events`; `error_detail`
  on failure; `provider_name` on every call. `provider_name` is `"yahoo"`.
* `run_id`: a fresh `uuid4()` string per call when none is supplied (mirrors
  Module 03's `apply_schema`; allowed by spec §8 / assumption A7).
* **Success with data**: `status = "success"`, DTO list under its key,
  `rows_processed = len(list)`, `errors = []`.
* **Empty result** (valid query, vendor has nothing): `status = "success"`,
  key present with an EMPTY list, `rows_processed = 0`. Empty is NOT an error.
* **Partial result** (some bars, some missing): `status =
  "success_with_warnings"`, partial list, `warnings` describe the gap.
* **Error**: `status = "failed"`, `metadata["error_detail"]` is a
  `ProviderErrorDetail` whose `kind` is in `PROVIDER_ERROR_KINDS`, `errors`
  non-empty, `rows_processed = 0`. The provider must NOT raise for the expected
  conditions in §9 — it returns a `ServiceResult`. Map at least:
  * unknown / not-found symbol  → `unsupported_symbol`
  * network / Yahoo down        → `provider_unavailable`
  * throttling                  → `rate_limited`
  * unparseable vendor payload  → `malformed_response`
  * method on a provider that does not support it → `unsupported_capability`
* Truly unexpected exceptions may propagate; the documented §9 conditions must
  be returned as `ServiceResult`, not raised.

### Price-history requirements (`MASTER_SPEC.md` §7, spec §10–11)

* Map each Yahoo daily row to a `PriceBar`, including BOTH raw OHLC
  (`open_raw`/`high_raw`/`low_raw`/`close_raw`), `volume_raw`, AND adjusted OHLC
  (`open_adj`/`high_adj`/`low_adj`/`close_adj`), plus `dividend_amount`,
  `split_ratio`, and `source_provider = "yahoo"`. The concrete yfinance column
  mapping and adjusted-OHLC derivation are specified in CLARIFICATIONS 2–6.
* Do NOT populate a `volume_adj` (it does not exist on `PriceBar`).
* Honor the inclusive `[start_date, end_date]` range (assumption A1); see
  CLARIFICATION 3 for the yfinance exclusive-`end` adjustment.
* `dates` are `datetime.date` (never strings/timestamps).
* No symbol normalization at the interface level; if Yahoo needs a vendor-
  specific symbol form, do that translation INTERNALLY to `YahooProvider`
  (assumption A2), keeping DTO `ticker` values provider-neutral.
* VIX (`^VIX`, `symbol_type = "index"`): `close_raw = close_adj`, volume may be
  `None`/0 — passed through; do not special-case beyond what Yahoo returns.
* Set `ProviderCapabilities.supports_adjusted_prices` to `True` for YahooProvider
  because it can derive adjusted OHLC from `Adj Close / Close` when Yahoo supplies
  the required columns; individual missing rows are handled as partial/warning
  cases, not as a provider capability change.
* Set `ProviderCapabilities.supports_ticker_listing` to `False` in V1 unless
  `list_symbols()` is backed by a real injected/static symbol source. The method
  still exists and returns a valid `ServiceResult`, but full Yahoo universe
  discovery is intentionally deferred (see CLARIFICATION 8).

### yfinance isolation

* `import yfinance` only inside `yahoo_provider.py`.
* All Yahoo access is confined to `YahooProvider`. No other module imports
  `yfinance` or calls Yahoo (`ARCHITECTURE.md`, `MASTER_SPEC.md` §5).
* `pandas` may be used ONLY where unavoidable for yfinance/library
  compatibility (`CODING_STANDARDS.md`: "prefer Polars, pandas only when
  unavoidable for provider/library compatibility"). Convert to plain DTOs at
  the provider boundary; do not leak DataFrames out of the provider.

---

## CLARIFICATIONS TO PREVENT AMBIGUITY

These resolve yfinance-specific decisions so the coder does not guess. They are
binding for Module 05 V1. Where a clarification specifies an exact formula or
column name, use it verbatim. If `yfinance`'s real behavior differs from what is
described (e.g. an attribute is unavailable in the installed version), prefer the
documented, contract-preserving fallback (empty `success`, or `None` adjusted
fields with `success_with_warnings`) over inventing new behavior — and note it in
the assumptions section of your output.

1. **Primary price API.** Use `yfinance.Ticker(ticker).history(...)` as the
   primary API for `get_price_history`. Do **not** use `yfinance.download(...)`
   in V1 unless strictly necessary; if you ever do, justify it explicitly in the
   design notes. The single-ticker `Ticker(...).history(...)` path keeps V1
   per-ticker (spec assumption A5/Q1) and is simpler to mock offline.

2. **History call arguments.** Call `history(...)` with `auto_adjust=False` and
   `actions=True` where the installed yfinance supports them, so that the result
   contains raw OHLC, `Volume`, `Dividends`, `Stock Splits`, and `Adj Close`.
   Pass the date range as `start=` / `end=` per CLARIFICATION 3. Do not pass
   `period=`.

3. **Inclusive end date.** The Module 04 interface range is inclusive on both
   ends: `[start_date, end_date]`. yfinance treats `end` as **exclusive**, so
   the provider must call yfinance internally with `end = end_date + 1 day`
   (use `datetime.timedelta(days=1)`). The DTOs returned to the caller must
   still reflect the inclusive contract: a bar dated exactly `end_date` is
   included, and no bar after `end_date` is returned.

4. **Raw column mapping.** Map raw OHLC and volume from the Yahoo columns
   `Open`, `High`, `Low`, `Close`, and `Volume` →
   `open_raw`, `high_raw`, `low_raw`, `close_raw`, `volume_raw`. Map corporate
   actions from `Dividends` → `dividend_amount` and `Stock Splits` →
   `split_ratio` **when those columns are present**; when absent or NaN, leave
   the corresponding field as `None`. Convert the row's date index to a plain
   `datetime.date`.

5. **Adjusted-OHLC derivation.** If Yahoo provides `Adj Close` but not adjusted
   OHLC, derive the adjusted OHLC per row as:

   ```text
   adjustment_factor = Adj Close / Close
   open_adj  = Open  * adjustment_factor
   high_adj  = High  * adjustment_factor
   low_adj   = Low   * adjustment_factor
   close_adj = Adj Close
   ```

   `adjustment_factor` here is a **local computation variable only**.
   - If `Close` is missing or zero, or `Adj Close` is missing for a row, set
     that row's adjusted fields (`open_adj`/`high_adj`/`low_adj`/`close_adj`) to
     `None`.
   - If at least one valid bar exists but some bars could not be adjusted (or
     other gaps exist), return `status = "success_with_warnings"` with a warning
     describing the gap, and still return the bars that were produced.
   - VIX (`^VIX`) keeps `close_raw = close_adj` (the §5 VIX rule above); when a
     provider/test supplies VIX rows, do not force a derived factor that breaks
     that identity.

6. **No `adjustment_factor` field.** Do not create, persist, or expose an
   `adjustment_factor` on `PriceBar` or anywhere in the returned DTOs — it is a
   `daily_prices` column derived later by the ingestion/mutation layer, not a
   provider field (`PROVIDER_INTERFACE_SPEC.md` §11). It exists only as the
   transient local variable in CLARIFICATION 5.

7. **Empty vs unknown symbol.** Do **not** classify every empty Yahoo DataFrame
   as `unsupported_symbol`. An empty-but-valid response (e.g. a real ticker with
   no trading days in the requested window) is `status = "success"` + empty
   `bars` list + `rows_processed = 0`, optionally with a warning. Return
   `unsupported_symbol` **only** when the vendor response (or the mocked test)
   clearly indicates an invalid/unknown ticker (e.g. yfinance raises/flags a
   not-found condition, or the injected fake signals "unknown symbol"). When in
   doubt between "empty" and "unknown", treat it as empty success.

8. **`list_symbols()` in V1.** `list_symbols()` must NOT scrape Yahoo or attempt
   full US-universe discovery. In V1 it returns `status = "success"` + empty
   `symbols` list + `rows_processed = 0`, optionally with a warning that symbol
   enumeration is deferred — UNLESS a simple injected/static symbol source is
   explicitly provided (e.g. an injected list used only by tests), in which case
   it maps that source to `TickerInfo` objects. Full universe construction is
   Module 06's responsibility, not Module 05's.

9. **`get_earnings(ticker)` in V1.** Earnings is best-effort. Use a documented
   yfinance `Ticker` attribute such as `calendar` (or equivalent) when available
   to obtain a candidate earnings date. Do **not** scrape web pages. If no
   reliable earnings date is found, return `status = "success"` + empty `events`
   list + `rows_processed = 0`. When an event is produced, use
   `confidence = "low"` unless the vendor clearly provides confirmed data; set
   `session` to `"unknown"` (or `None`) when the vendor does not specify it, and
   `source_provider = "yahoo"`.

10. **No network in the constructor.** `YahooProvider.__init__` must perform no
    network calls and no Yahoo access. It should accept an optional injection
    parameter for the yfinance module/client (e.g. `yf_module=None`, defaulting
    to the real `yfinance` when not supplied) so that offline tests can inject a
    deterministic fake. Store the injected dependency and use it for all vendor
    access; do not reach for the global `yfinance` directly inside the methods
    when an injected one is present.

11. **Dependencies already declared.** `yfinance>=0.2.40` and `pandas>=2.2` are
    already declared in BOTH `requirements.txt` and `pyproject.toml` of
    `stock_ai_platform_module04_stable.zip`. Therefore do NOT modify dependency
    files. Confirm this in your output rather than editing them.

12. **Capabilities in V1.** `get_capabilities()` must report the actual V1
    provider scope clearly. Set `supports_daily_prices=True` and
    `supports_adjusted_prices=True` for YahooProvider. Set
    `supports_ticker_listing=False` unless `list_symbols()` is backed by a real
    injected/static symbol source; returning an empty success for deferred full
    universe discovery does not mean full listing is supported. Set
    `supports_earnings=True` only if `get_earnings()` implements a best-effort
    yfinance calendar extraction path; otherwise set it to `False` and make
    `get_earnings()` return `unsupported_capability`. Prefer implementing the
    best-effort empty-success path described in CLARIFICATION 9 for V1.

---

## REQUIRED TESTS

Create `tests/test_yahoo_provider.py`. Tests must run **fully offline** — no
real network, no real Yahoo calls, no DuckDB. Inject a fake yfinance module/
client (via the CLARIFICATION 10 injection parameter and/or `monkeypatch`) so
the provider's mapping and error handling are exercised without hitting the
network. A small in-test pandas DataFrame (or an equivalent fake that mimics the
columns in CLARIFICATIONS 2/4) is the expected way to feed vendor rows.

At minimum, cover:

1. **Import smoke**: `YahooProvider` imports; it is a subclass of
   `MarketDataProvider`; it instantiates with an injected fake (all four
   abstract methods present).
2. **Signature conformance**: `inspect.signature` of each method matches the
   Module 04 contract exactly (names, params, return annotation).
3. **Capabilities**: `get_capabilities()` returns `success` with a
   `ProviderCapabilities` under `metadata["capabilities"]`,
   `metadata["provider_name"] == "yahoo"`, `supports_daily_prices=True`,
   `supports_adjusted_prices=True`, and `supports_ticker_listing=False` unless
   a real injected/static symbol source is implemented.
4. **Price history happy path** (mocked vendor data): returns `success`, a
   `list[PriceBar]` under `metadata["bars"]`, `rows_processed == len(bars)`,
   each bar has raw + adjusted OHLC and `source_provider == "yahoo"`, dates are
   `datetime.date`, and there is NO `volume_adj` attribute.
5. **Inclusive end-date handling**: assert that for a request with
   `start_date`/`end_date`, the provider calls the injected yfinance with
   `end == end_date + 1 day` (capture the args passed to the fake), and that a
   bar dated exactly `end_date` is included while none after it is returned.
6. **Single-day inclusive range**: a request with `start == end` is accepted and
   maps the one in-range bar correctly.
7. **Adjusted-OHLC derivation**: feed rows where `Adj Close != Close`; assert
   `close_adj == Adj Close` and `open_adj == Open * (Adj Close / Close)` (and
   likewise for high/low) within float tolerance. Feed a row with `Close == 0`
   or missing `Adj Close`; assert that row's adjusted fields are `None` and, if
   other valid bars exist, the overall status is `success_with_warnings`.
8. **Empty DataFrame is success, not failed**: a mocked empty result →
   `status == "success"`, empty `bars`, `rows_processed == 0`, and NOT an error.
9. **Unknown symbol is failed only when explicitly signaled**: when the injected
   fake clearly signals an invalid/unknown ticker, `status == "failed"`,
   `metadata["error_detail"]` is a `ProviderErrorDetail`,
   `error_detail.kind == "unsupported_symbol"`, `rows_processed == 0`,
   `errors` non-empty. (Contrast with test 8: empty ≠ unknown.)
10. **Transport error mapping**: when the fake raises a network/throttle
    condition, the provider returns `status == "failed"` with
    `error_detail.kind` in `{"provider_unavailable", "rate_limited"}` (or the
    appropriate §9 kind), `rows_processed == 0`.
11. **`list_symbols()` does not scrape**: in V1 it returns `success` + empty
    `symbols` + `rows_processed == 0` (or maps an explicitly injected static
    source), and it performs no network access (verify via the fake / by
    asserting the fake's network entry points were not called).
12. **`get_earnings()` best-effort empty success**: with no reliable earnings
    data from the fake, returns `success` + empty `events` + `rows_processed ==
    0`; when an event is produced, `confidence == "low"` unless confirmed and
    `source_provider == "yahoo"`.
13. **VIX mapping**: a mocked `^VIX` row maps with `close_raw == close_adj` and
    volume `None`/0 tolerated.
14. **Constructor performs no network**: constructing `YahooProvider(...)` with
    a fake that records calls makes zero vendor/network calls; assert the fake's
    counters remain at zero after `__init__`.
15. **Yahoo access isolated to the provider file**: a static scan asserts that
    only `app/providers/yahoo_provider.py` references `yfinance` (no other
    `app/**` module imports it), and that `yahoo_provider.py` does NOT import
    `duckdb` / `app.database` and does NOT use `print(`. (Mirror the literal-vs-
    descriptive scan discipline from `tests/test_provider_interface.py`:
    docstring/comment mentions are allowed; only executable references count.)
16. **Style**: module docstring present; every function/method has type hints;
    no `print(`.

Existing Module 01–04 tests must continue to pass unchanged.

---

## LOGGING AND STYLE

Use project style from `CODING_STANDARDS.md`.

In `yahoo_provider.py`, include:

```python
from app.utils import logging_config

_LOG = logging_config.get_logger(__name__)
```

Bind the per-call `run_id` for log records emitted during a fetch
(`logging_config.get_logger(__name__, run_id)`), mirroring Module 03.

* Do not use `print()` in library code.
* Every new Python file must have a module-level docstring.
* Every function and method must have type hints.
* Keep the code small and boring. No retry framework, no connection pools, no
  threading, no async, no caching layer. No new dependencies beyond the
  already-declared `yfinance` (and `pandas`, only at the yfinance boundary).

---

## README

README may be updated only with a short Module 05 usage note (how a caller
obtains a `YahooProvider` — including the optional injected yfinance dependency
for testing — and calls `get_price_history` etc., returning a `ServiceResult`).
Keep the existing Module 01–04 notes intact. Do not modify `docs/*.md`. The
example must not include real network calls; it should mirror the inclusive-
range and injection points described in the CLARIFICATIONS.

---

## OUTPUT REQUIRED

Return:

1. Updated project zip.
   * Top-level folder must be `stock_ai_platform/`.
   * Preserve the same layout as the Module 04 stable zip.

2. List of added/changed files.

3. Design notes:
   * how `YahooProvider` implements the Module 04 contract;
   * how Yahoo/yfinance access is isolated to the provider layer and injected
     for tests (CLARIFICATION 10);
   * how vendor rows map to `PriceBar` (raw columns per CLARIFICATION 4,
     adjusted-OHLC derivation per CLARIFICATION 5, dividends/splits, VIX);
   * how the inclusive end-date is handled against yfinance's exclusive `end`
     (CLARIFICATION 3);
   * how the `ServiceResult` metadata keys and §9 error kinds are produced, and
     how empty-vs-unknown is distinguished (CLARIFICATION 7);
   * how `get_capabilities()` reports the V1 scope, especially
     `supports_ticker_listing=False` unless a real injected/static symbol source
     exists (CLARIFICATION 12);
   * how `list_symbols` and `get_earnings` stay within V1 scope
     (CLARIFICATIONS 8–9);
   * how the tests run fully offline (mock/monkeypatch/injection strategy);
   * how frozen Modules 01–04 (especially the provider interface) are
     protected.

4. Test command and full test result.
   Preferred:
   ```bash
   pytest -q
   ```
   If the environment cannot run tests (e.g. `duckdb`/`yfinance` not
   installed), clearly state why and list the isolated/static checks performed
   (e.g. `pytest -q tests/test_yahoo_provider.py`).

5. Any assumptions.
   * Do not hide assumptions.
   * Do not add assumptions where the answer is explicitly specified in
     `PROVIDER_INTERFACE_SPEC.md`, `MASTER_SPEC.md`, or the CLARIFICATIONS
     section above.
   * If the installed yfinance behaves differently from the CLARIFICATIONS
     (e.g. an attribute is missing), record the contract-preserving fallback you
     chose here.

6. Suggested commit message:

```text
module05_yahoo_provider_stable
```

---

## STARTING STEPS

Read in this order:

1. `PROVIDER_INTERFACE_SPEC.md`  (the contract to implement)
2. `app/providers/provider_interface.py`  (the actual Module 04 code)
3. The **CLARIFICATIONS TO PREVENT AMBIGUITY** section of this prompt
4. `docs/MASTER_SPEC.md`  (§5 market data, §6 symbol types, §7 raw/adjusted)
5. `docs/ARCHITECTURE.md`  (provider-layer boundary)
6. `docs/CODING_STANDARDS.md`
7. `app/utils/service_result.py`
8. `app/config/constants.py`
9. `tests/test_provider_interface.py`  (the FakeProvider + static-scan pattern
   to follow)

Then implement Module 05.

Do not reopen the Module 04 contract.
Do not implement Module 06 or later modules.
Do not modify any Module 01/02/03/04 file.
Do not modify any `docs/*.md`.
Do not ask for old archives.
