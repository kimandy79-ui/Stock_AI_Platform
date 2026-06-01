# PROVIDER_INTERFACE_SPEC.md — Module 04 Provider Interface

> **Status**: source-of-truth for **Module 04 — Provider Interface**.
>
> This document plays the same role for Module 04 that `SCHEMA_SPEC.md` plays
> for Module 03. The coder implements `app/providers/provider_interface.py`
> against this document; the reviewer verifies the code against it.
>
> **Module 04 defines the ABSTRACT provider contract only.** It contains no
> YahooProvider, no `yfinance`, no network calls, no DuckDB access, and no
> business logic. Concrete behavior (real Yahoo downloads) is **Module 05**.

---

## 1. Status and purpose

Module 04 introduces the single, provider-neutral abstraction through which the
entire platform obtains market data. Every later module that needs prices,
ticker listings, or earnings dates depends on this contract — never on a
concrete vendor.

This spec exists so that:

- a coder can implement the interface without guessing names, signatures, or
  data shapes;
- a reviewer can check the implementation line-by-line against a fixed
  contract;
- Module 05 (YahooProvider) can implement the interface as-is;
- later modules (06 Universe, 07 Benchmark loader, 08 Daily Price Ingestion,
  09 Validator, …) can rely on stable method names, request/response shapes,
  and error semantics.

The interface is intentionally minimal: it defines exactly the operations the
V1 pipeline (`MASTER_SPEC.md` §4) needs from a data vendor, and nothing more.

---

## 2. Source documents

Derived, in priority order, from:

1. `ARCHITECTURE.md` — module boundaries; Module 04 (Provider Interface),
   Module 05 (YahooProvider), and the data-flow boundary
   `Provider → daily_prices → daily_features → …`.
2. `MASTER_SPEC.md` — market-data rules (§5), symbol types (§6), raw vs
   adjusted price rules (§7), benchmarks/VIX handling (§5, §11), V1 limitations
   (§21).
3. `CODING_STANDARDS.md` — typing, docstrings, logging, `ServiceResult`
   conventions, testing discipline, "do not call providers outside the provider
   layer", "prefer Polars, pandas only when unavoidable for provider/library
   compatibility".
4. Accepted codebase after Module 03 — existing layout and patterns
   (`app.utils.service_result.ServiceResult`, `app.config.constants` symbol/
   benchmark vocabulary, `app.utils.logging_config`). **Not to be modified.**
5. `SCHEMA_SPEC.md` — used **only** as downstream context: the provider's
   price output must eventually be able to populate `daily_prices`
   (§3.7), ticker metadata must support `ticker_master` (§3.4), and earnings
   output must support `earnings_calendar` (§3.15). Module 04 does **not**
   write to DuckDB and is **not** a schema module.

Where the documents are silent, this spec makes a **minimal explicit
assumption** and marks it in §15.

---

## 3. Module 04 boundaries

### 3.1 Module 04 MUST

- Define a provider-neutral abstraction (`MarketDataProvider`) for market data.
- Define the provider request/response domain models (DTOs) needed by V1.
- Define provider error / status semantics that downstream modules and tests
  can rely on.
- Be fully testable **without any network** (a fake in-memory provider must be
  implementable in tests).
- Be implementable as-is by Module 05 (YahooProvider).

### 3.2 Module 04 MUST NOT

- Implement YahooProvider or any concrete provider.
- Import `yfinance` or any HTTP/network client; make no network calls.
- Open DuckDB connections; read or write any database; change the schema.
- Implement ticker-universe updates, benchmark loading, daily price ingestion,
  validation, mutation detection, screening, scoring, trading, simulation, AI
  review, or dashboard logic.
- Add any new third-party dependency.
- Modify any Module 01–03 file or any existing test.

### 3.3 Data-flow position

```text
[Module 05 YahooProvider]  implements  [Module 04 MarketDataProvider]
                                              │
                  returns provider-neutral DTOs (this spec)
                                              │
        consumed by → 07 Benchmark loader, 08 Daily Price Ingestion,
                       06 Universe snapshot, (earnings consumer)
                                              │
                            persisted by those modules → daily_prices,
                            ticker_master, earnings_calendar (SCHEMA_SPEC.md)
```

Module 04 stops at "returns DTOs". Persistence, validation, and adjustment
logic belong to later modules.

---

## 4. Recommended implementation files

Module 04 coding is restricted to:

```text
app/providers/__init__.py            # create package if absent
app/providers/provider_interface.py  # the abstraction + DTOs + errors
tests/test_provider_interface.py     # tests
README.md                            # only if a short Module 04 note is added
```

- If `app/providers/` does not exist, Module 04 **may create it** as a package
  (with `__init__.py`).
- **Forbidden** to modify any Module 01–03 file or existing test:
  `app/database/*`, `app/config/*`, `app/utils/*`, `conftest.py`,
  `tests/test_project_skeleton.py`, `tests/test_duckdb_manager.py`,
  `tests/test_schema_manager.py`, `pyproject.toml`, `requirements.txt`,
  `.gitignore`, `.env.example`, any `docs/*.md`.

A single `provider_interface.py` holding the ABC, the DTOs, and the error types
is the expected, simplest layout. Do **not** split into many files.

---

## 5. Abstraction mechanism

**Decision: use `abc.ABC` + `@abstractmethod`. (Primary mechanism. Not
optional.)**

Justification, against the criteria in the prompt:

- **Enforces implementation.** Instantiating a subclass that forgets a required
  method raises `TypeError` at construction. `typing.Protocol` is structural and
  only checked by an external type-checker, so a missing method would not fail
  at runtime — weaker for a beginner-friendly project and harder to assert in
  tests.
- **Testability.** "Cannot instantiate the abstract base directly" and "a fake
  subclass implementing all methods works" are both directly assertable with
  `abc.ABC` (see §13). This mirrors the existing test discipline in
  `tests/test_duckdb_manager.py` / `tests/test_schema_manager.py`.
- **Clarity.** An explicit abstract class with documented `@abstractmethod`
  signatures is the clearest contract for a reader implementing Module 05.
- **Consistency / no new dependency.** `abc` is stdlib; no new dependency
  (`CODING_STANDARDS.md` §3). The DTOs use `@dataclass(frozen=True)` (stdlib),
  consistent with the frozen dataclasses already used in
  `app/config/settings.py`.

`typing.Protocol` **may** additionally be referenced in a docstring as "the
structural shape", but the **authoritative** mechanism is `abc.ABC`. Do not
introduce Pydantic for Module 04 — it is not currently a project dependency and
the prompt forbids adding one.

---

## 6. Domain DTOs / data models

All DTOs are **provider-neutral** (no Yahoo-specific fields) and implemented as
`@dataclass(frozen=True)` with full type hints. They live in
`provider_interface.py`. Dates are `datetime.date`; timestamps (if any) are
`datetime.datetime`. Monetary/price fields are `float`; volume is `int | None`.

DTOs are **plain data carriers**: they perform light structural validation in
`__post_init__` only where noted (e.g. non-empty ticker, `start <= end`). They
do **not** validate business semantics, fetch anything, or know about DuckDB.

### 6.1 `PriceBar` — one daily OHLCV row (provider-neutral)

Represents a single trading day for one symbol as returned by the provider.
Maps downstream onto `daily_prices` (`SCHEMA_SPEC.md` §3.7), but Module 04 does
not persist it.

Raw vs adjusted handling follows `MASTER_SPEC.md` §7 (see §11 of this spec):
the provider returns **both** raw and adjusted OHLC plus `volume_raw`.
`volume_adj` is **reserved/unused in V1** and is therefore **not** a field on
this DTO (it would otherwise be dead weight; see §11 and assumption A4).

| Field | Type | Optional | Meaning |
|---|---|---|---|
| `ticker` | `str` | no | Provider-neutral symbol (see §10). |
| `date` | `datetime.date` | no | Trading day. |
| `open_raw` | `float \| None` | yes | Raw open. |
| `high_raw` | `float \| None` | yes | Raw high. |
| `low_raw` | `float \| None` | yes | Raw low. |
| `close_raw` | `float \| None` | yes | Raw close. |
| `volume_raw` | `int \| None` | yes | Raw volume (V1 volume feature input). |
| `open_adj` | `float \| None` | yes | Adjusted open. |
| `high_adj` | `float \| None` | yes | Adjusted high. |
| `low_adj` | `float \| None` | yes | Adjusted low. |
| `close_adj` | `float \| None` | yes | Adjusted close. |
| `dividend_amount` | `float \| None` | yes | Cash dividend on `date` (0/None if none). |
| `split_ratio` | `float \| None` | yes | Split ratio on `date` (1/None if none). |
| `source_provider` | `str` | no | Provider identity, e.g. `"yahoo"`. |

`__post_init__`: assert `ticker` is non-empty. No other enforcement (a provider
may legitimately return `None` OHLC for a missing/halted day; validation is
Module 09).

> Note: `data_quality_status`, `mutation_flag`, `adjustment_factor`,
> `created_at`, `updated_at` from `daily_prices` are **NOT** provider fields —
> they are assigned by ingestion/validation/mutation modules (08/09/10), not by
> the vendor. They are intentionally absent from `PriceBar`.

### 6.2 `PriceHistoryRequest` — a price-history query

| Field | Type | Optional | Meaning |
|---|---|---|---|
| `ticker` | `str` | no | Symbol to fetch. |
| `start_date` | `datetime.date` | no | Inclusive start (see §10). |
| `end_date` | `datetime.date` | no | Inclusive end (see §10). |
| `symbol_type` | `str` | yes (default `"stock"`) | One of `ALLOWED_SYMBOL_TYPES` (see §12). |

`__post_init__`: assert `ticker` non-empty; assert `start_date <= end_date`
(else `ValueError`); if `symbol_type` is provided, assert it is in
`constants.ALLOWED_SYMBOL_TYPES`.

### 6.3 `TickerInfo` — a universe / listing item (provider-neutral)

Maps downstream onto a subset of `ticker_master` (`SCHEMA_SPEC.md` §3.4). Only
the fields a vendor can actually supply are included; flags such as
`active_flag`, `delisted_flag`, `first_seen`, `last_seen`, `last_updated` are
assigned by Module 06, not the provider.

| Field | Type | Optional | Meaning |
|---|---|---|---|
| `ticker` | `str` | no | Provider-neutral symbol. |
| `symbol_type` | `str` | no | One of `ALLOWED_SYMBOL_TYPES`. |
| `company_name` | `str \| None` | yes | Display name. |
| `exchange` | `str \| None` | yes | Listing exchange. |
| `sector` | `str \| None` | yes | GICS-style sector (may be unmapped). |
| `industry` | `str \| None` | yes | Industry. |
| `security_type` | `str \| None` | yes | Vendor security classification. |

`__post_init__`: assert `ticker` non-empty; assert `symbol_type` in
`constants.ALLOWED_SYMBOL_TYPES`.

### 6.4 `EarningsEvent` — one earnings date (provider-neutral)

Maps downstream onto `earnings_calendar` (`SCHEMA_SPEC.md` §3.15). Confidence
may be LOW in V1 (`MASTER_SPEC.md` §21).

| Field | Type | Optional | Meaning |
|---|---|---|---|
| `ticker` | `str` | no | Symbol. |
| `earnings_date` | `datetime.date` | no | Expected/confirmed earnings date. |
| `session` | `str \| None` | yes | `pre_market` / `post_market` / `during_market` / `unknown` (see §9 note). |
| `confidence` | `str` | no | `high` / `medium` / `low`. |
| `source_provider` | `str` | no | Provider identity. |

`__post_init__`: assert `ticker` non-empty.

### 6.5 `ProviderCapabilities` — what a concrete provider supports

Lets callers/tests introspect a provider without trial-and-error. Returned by
`get_capabilities()` (§7.1, a non-network metadata method).

| Field | Type | Optional | Meaning |
|---|---|---|---|
| `provider_name` | `str` | no | e.g. `"yahoo"`. |
| `supports_daily_prices` | `bool` | no | Whether `get_price_history` is implemented. |
| `supports_ticker_listing` | `bool` | no | Whether `list_symbols` is implemented. |
| `supports_earnings` | `bool` | no | Whether `get_earnings` is implemented. |
| `supports_adjusted_prices` | `bool` | no | Whether adjusted OHLC is populated. |

### 6.6 `ProviderErrorDetail` — structured non-fatal error payload

Carried inside `ServiceResult.metadata["error_detail"]` (and/or
`ServiceResult.errors`) so callers can branch on error kind without parsing
strings.

| Field | Type | Optional | Meaning |
|---|---|---|---|
| `kind` | `str` | no | One of `PROVIDER_ERROR_KINDS` (see §9). |
| `symbol` | `str \| None` | yes | Offending symbol, if any. |
| `message` | `str` | no | Human-readable detail. |

> **DTO inclusion rationale.** Every DTO above is justified by a concrete V1
> need: `PriceBar`/`PriceHistoryRequest` (pipeline steps 5/8, indicators),
> `TickerInfo` (universe, step 4), `EarningsEvent` (earnings-avoid window,
> `MASTER_SPEC.md` §20), `ProviderCapabilities` (graceful capability checks,
> §9), `ProviderErrorDetail` (testable error semantics, §9). No speculative
> models (e.g. intraday bars, fundamentals) are included — they are out of V1
> scope (`MASTER_SPEC.md` §21).

---

## 7. Provider interface methods

The abstract base class is **`MarketDataProvider(abc.ABC)`**. All data-fetching
methods return a `ServiceResult` (§8). Module 04 defines **signatures and
contracts only**; the body of each abstract method is empty (docstring +
`...`/`raise NotImplementedError` is acceptable but `@abstractmethod` is the
enforcement).

> **Network rule.** Module 04 itself performs **no** network calls — every
> abstract method body is empty. The "may perform network in future" column
> describes what a **concrete** provider (Module 05) is allowed to do, not
> Module 04.

### 7.1 Method contracts

| Method | Network in concrete impl? | Returns |
|---|---|---|
| `get_capabilities()` | no (pure metadata) | `ServiceResult` (DTO: `ProviderCapabilities`) |
| `get_price_history(request)` | yes | `ServiceResult` (DTO: `list[PriceBar]`) |
| `list_symbols(symbol_type=None)` | yes | `ServiceResult` (DTO: `list[TickerInfo]`) |
| `get_earnings(ticker)` | yes | `ServiceResult` (DTO: `list[EarningsEvent]`) |

### 7.2 Signatures

```python
class MarketDataProvider(abc.ABC):
    @abc.abstractmethod
    def get_capabilities(self) -> ServiceResult:
        """Return provider capabilities. No network. metadata['capabilities']
        carries a ProviderCapabilities."""

    @abc.abstractmethod
    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult:
        """Return daily OHLCV bars for request.ticker within the inclusive
        [start_date, end_date] range. metadata['bars'] carries list[PriceBar]
        (possibly empty). rows_processed = number of bars."""

    @abc.abstractmethod
    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult:
        """Return the provider's known symbols, optionally filtered to one
        symbol_type from ALLOWED_SYMBOL_TYPES. metadata['symbols'] carries
        list[TickerInfo]. rows_processed = number of symbols."""

    @abc.abstractmethod
    def get_earnings(self, ticker: str) -> ServiceResult:
        """Return known earnings events for ticker. metadata['events'] carries
        list[EarningsEvent] (possibly empty). rows_processed = number of
        events."""
```

### 7.3 Behavior contract (applies to concrete implementations)

- **Success with data**: `status = "success"`, the DTO list in the documented
  `metadata` key, `rows_processed` = list length, `errors = []`.
- **Empty result** (valid query, vendor has nothing — e.g. date range with no
  trading days, or symbol with no earnings): `status = "success"`,
  `metadata` key present with an **empty list**, `rows_processed = 0`,
  `warnings` MAY contain a note. An empty result is **not** an error (§9).
- **Partial result** (some bars returned, some eval candles missing): `status =
  "success_with_warnings"`, partial list returned, `warnings` describe the gap.
- **Error** (unsupported symbol, provider unavailable, rate limit, malformed
  response, unsupported capability): `status = "failed"`,
  `metadata["error_detail"]` carries a `ProviderErrorDetail`, `errors`
  non-empty, `rows_processed = 0` (§9).
- A concrete provider must **not** raise for these expected conditions; it must
  return a `ServiceResult`. Truly unexpected exceptions may propagate, but the
  documented conditions in §9 must be returned as `ServiceResult`.

> Module 04's abstract methods have empty bodies, so none of the above runs in
> Module 04; the contract is what Module 05 and the in-test fake must honor.

---

## 8. `ServiceResult` policy

**Decision: every data-fetching abstract method returns
`app.utils.service_result.ServiceResult`. Domain DTOs are carried in
`ServiceResult.metadata` under documented keys. Do not invent a new wrapper.**

Justification:

- `ServiceResult` is the established cross-module contract
  (`ARCHITECTURE.md` §7, `CODING_STANDARDS.md` §7) and is already returned by
  Module 03. Provider operations are service-level operations (they can warn,
  partially fail, or fail), so they fit `ServiceResult` precisely.
- Carrying DTOs in `metadata` (rather than inventing a `data=` field) keeps the
  **frozen** `ServiceResult` dataclass unchanged — Module 04 must not modify
  `service_result.py`.
- `rows_processed` carries the natural count (bars / symbols / events), exactly
  as Module 03 used it for "tables present".

**Documented `metadata` keys** (stable contract for downstream modules/tests):

| Method | metadata key | value type |
|---|---|---|
| `get_capabilities` | `capabilities` | `ProviderCapabilities` |
| `get_price_history` | `bars` | `list[PriceBar]` |
| `list_symbols` | `symbols` | `list[TickerInfo]` |
| `get_earnings` | `events` | `list[EarningsEvent]` |
| any (on failure) | `error_detail` | `ProviderErrorDetail` |
| any | `provider_name` | `str` |

`status` uses only the allowed values `success` / `success_with_warnings` /
`failed` (`service_result.ALLOWED_STATUSES`). `run_id` is a UUID4 string
(`CODING_STANDARDS.md` §4); for a provider call with no externally supplied run
id, a fresh `uuid4()` is acceptable (mirrors Module 03's `apply_schema`).

DTOs themselves are returned **as objects** in `metadata`, not re-serialized to
dicts. This keeps the contract typed and testable.

---

## 9. Error and empty-data semantics

Standard `kind` vocabulary, defined as a module constant
`PROVIDER_ERROR_KINDS: tuple[str, ...]` and used in `ProviderErrorDetail.kind`:

| `kind` | Trigger | ServiceResult.status |
|---|---|---|
| `unsupported_symbol` | Vendor does not know the symbol. | `failed` |
| `provider_unavailable` | Vendor/API down or unreachable. | `failed` |
| `rate_limited` | Vendor throttled the request. | `failed` |
| `malformed_response` | Vendor returned unparseable/invalid data. | `failed` |
| `unsupported_capability` | Method called on a provider that does not support it (per `ProviderCapabilities`). | `failed` |

Empty / partial **are not error kinds**:

| Condition | status | metadata list | rows_processed |
|---|---|---|---|
| Empty provider response (valid query, no data) | `success` | empty list | 0 |
| Date range with no trading days | `success` | empty `bars` | 0 |
| Symbol with no earnings | `success` | empty `events` | 0 |
| Partial data (some bars, gaps) | `success_with_warnings` | partial list | partial count |

This split is explicit so tests can assert: "no data ⇒ success + empty list"
vs "bad symbol ⇒ failed + `error_detail.kind == 'unsupported_symbol'`".

> **Session vocabulary note.** `EarningsEvent.session` values
> (`pre_market` / `post_market` / `during_market` / `unknown`) come from
> `SCHEMA_SPEC.md` §5 (`session` enum) and are **value catalogs validated at the
> service layer**, not DB enums. Module 04 may define them as module constants
> but must not create DuckDB ENUM types (out of scope anyway).

---

## 10. Date, timezone, and symbol rules

- **Date type**: `datetime.date` everywhere (never strings). Timestamps, if
  ever needed, are `datetime.datetime`; V1 DTOs use `date` only.
- **Range semantics**: `[start_date, end_date]` is **inclusive on both ends**
  (assumption A1). `__post_init__` enforces `start_date <= end_date`.
- **Timezone**: dates are calendar trading days in US market terms; Module 04
  stores no timezone and performs no tz conversion. Trading-calendar logic
  (`pandas-market-calendars`) belongs to later modules, not the interface.
- **Symbol neutrality**: DTO `ticker` values are **provider-neutral** symbols
  as the platform uses them (e.g. `SPY`, `QQQ`, `^VIX`, `XLK`). The interface
  does **not** define provider-specific symbol mapping; translating a neutral
  symbol to a vendor symbol (e.g. a Yahoo quirk) is the **concrete provider's**
  responsibility (Module 05), kept internal to that implementation.
- **`^VIX`**: passed through as the neutral symbol `^VIX` with
  `symbol_type = "index"` (`MASTER_SPEC.md` §5). The interface does not special-
  case it beyond allowing it as a normal symbol string; VIX-specific rules
  (`close_raw = close_adj`, volume may be NULL/0) are honored by the concrete
  provider and validated later, not enforced by Module 04.
- **Normalization**: the interface performs **no** symbol normalization (no
  upper-casing, trimming, or suffix handling). Inputs are used verbatim;
  normalization, if any, is a concrete-provider concern. (Assumption A2.)

---

## 11. Raw vs adjusted price requirements

Per `MASTER_SPEC.md` §7 and the Module-03 schema:

- **Raw OHLC** (`open_raw`, `high_raw`, `low_raw`, `close_raw`) and
  **`volume_raw`** are required: execution realism, entry price, stop/target,
  gaps, UI audit, and all V1 volume features (`rvol20`,
  `avg_dollar_volume_20d`).
- **Adjusted OHLC** (`open_adj`, `high_adj`, `low_adj`, `close_adj`) are
  required: indicators (EMA/RSI/ATR), 52-week metrics, relative strength,
  outcome returns.
- **`volume_adj` is reserved/unused in V1** → it is **NOT** a field on
  `PriceBar` (assumption A4). If a future version needs it, add it then.
- `dividend_amount` and `split_ratio` are included because adjusted prices and
  mutation detection (Module 10) derive from corporate-action data; a provider
  that can supply them should.

**Effect on the DTO**: `PriceBar` carries both raw and adjusted OHLC plus
`volume_raw` (see §6.1). A concrete provider that cannot supply adjusted data
sets the adjusted fields to `None` and reports
`ProviderCapabilities.supports_adjusted_prices = False`; deciding what to do
about that is a later module's concern, not Module 04's.

`adjustment_factor` (a `daily_prices` column) is a **derived** value computed by
the ingestion/mutation layer, not a raw vendor field, so it is **not** on
`PriceBar`.

---

## 12. Benchmarks and symbol types

- **Allowed `symbol_type` values** (from `MASTER_SPEC.md` §6, mirrored by
  `constants.ALLOWED_SYMBOL_TYPES`): `stock`, `etf`, `benchmark`, `index`.
  Only `stock` enters screening (enforced later, not by Module 04).
- **Required benchmark symbols** (`MASTER_SPEC.md` §5,
  `constants.REQUIRED_BENCHMARK_SYMBOLS`): `SPY`, `QQQ`, `^VIX`, and sector
  ETFs `XLK, XLF, XLV, XLY, XLP, XLC, XLI, XLE, XLB, XLU, XLRE`.

**Module 04 policy**: the interface **reuses the existing constants** from
`app.config.constants` (`ALLOWED_SYMBOL_TYPES`, `REQUIRED_BENCHMARK_SYMBOLS`,
`BENCHMARK_SPY/QQQ/VIX`, `SECTOR_ETFS`) for any validation it does (e.g.
`symbol_type` membership in DTO `__post_init__`). It must **not** redefine these
vocabularies. The interface treats these symbols as ordinary symbol strings;
**enforcement** that benchmarks are excluded from screening, that VIX volume may
be null, etc., belongs to later modules (07, 12, 13), **not** Module 04.

---

## 13. Testing requirements for Module 04

`tests/test_provider_interface.py` must include at least:

1. **Import smoke**: module imports; public symbols present
   (`MarketDataProvider`, all DTOs, `PROVIDER_ERROR_KINDS`).
2. **Abstract enforcement**: `MarketDataProvider()` cannot be instantiated
   directly (`pytest.raises(TypeError)`), since it is `abc.ABC` with
   `@abstractmethod`s.
3. **Incomplete subclass fails**: a subclass that omits a required method also
   raises `TypeError` on instantiation.
4. **Fake provider works**: an in-test `FakeProvider(MarketDataProvider)`
   implementing all four methods instantiates and returns `ServiceResult`s.
5. **Signature conformance**: each method's parameters/return match this spec
   (checked via `inspect.signature`).
6. **DTO construction**: each DTO builds with valid fields; frozen-ness holds
   (assigning an attribute raises `FrozenInstanceError`).
7. **DTO validation**: invalid inputs raise — empty `ticker`,
   `start_date > end_date`, `symbol_type` not in `ALLOWED_SYMBOL_TYPES`.
8. **Empty-result semantics**: fake returns `success` + empty list +
   `rows_processed == 0` for a no-data query.
9. **Error semantics**: fake returns `failed` + `error_detail.kind` in
   `PROVIDER_ERROR_KINDS` for an unsupported symbol; `ServiceResult.metadata`
   carries a `ProviderErrorDetail`.
10. **ServiceResult contract**: returned objects are real `ServiceResult`s with
    valid status and the documented `metadata` keys.
11. **No network**: static scan of `provider_interface.py` asserts no
    `import yfinance`, no `import requests`/`urllib`/`http`, no `socket`.
12. **No DB access**: static scan asserts no `duckdb`, no
    `app.database` import, no `duckdb.connect(`.
13. **No `yfinance`**: explicit (covered by 11).
14. **Frozen modules untouched**: not asserted in code, but the reviewer checks
    via the §14 checklist that no Module 01–03 file changed.
15. **Type/style**: module docstring present; every function has type hints; no
    `print()`.

Tests must not perform network calls or touch any DuckDB file. No `tmp_path`
DB redirection is needed because Module 04 never opens a database — but tests
must still avoid importing the database layer.

---

## 14. Acceptance checklist (for the reviewer)

**Files**

- [ ] Only `app/providers/provider_interface.py`,
      `app/providers/__init__.py`, `tests/test_provider_interface.py`, and
      (optionally) `README.md` are added/changed.
- [ ] No Module 01–03 file or existing test modified (frozen files unchanged).

**Abstraction**

- [ ] `MarketDataProvider` is `abc.ABC` with `@abstractmethod` on all four
      methods.
- [ ] Direct instantiation raises `TypeError`; incomplete subclass raises
      `TypeError`.

**DTOs** (all `@dataclass(frozen=True)`, full type hints)

- [ ] `PriceBar` with the §6.1 fields (raw+adj OHLC, `volume_raw`,
      `dividend_amount`, `split_ratio`, `source_provider`; **no** `volume_adj`).
- [ ] `PriceHistoryRequest` (§6.2) with `start_date <= end_date` enforcement.
- [ ] `TickerInfo` (§6.3) with `symbol_type` membership check.
- [ ] `EarningsEvent` (§6.4).
- [ ] `ProviderCapabilities` (§6.5).
- [ ] `ProviderErrorDetail` (§6.6).

**Methods / contract**

- [ ] `get_capabilities`, `get_price_history`, `list_symbols`, `get_earnings`
      with the exact §7.2 signatures.
- [ ] All return `ServiceResult`; DTOs carried in the §8 `metadata` keys.
- [ ] Empty vs error semantics match §9.

**Vocabulary**

- [ ] Symbol types / benchmark symbols reuse `app.config.constants` (not
      redefined).
- [ ] `PROVIDER_ERROR_KINDS` defined and used by `ProviderErrorDetail`.

**Forbidden patterns**

- [ ] No `yfinance`, no network client, no network calls.
- [ ] No `duckdb` / `app.database` import, no DB access, no schema change.
- [ ] No new third-party dependency.
- [ ] No screening/scoring/trading/simulation/AI/dashboard/ingestion logic.

**Tests**

- [ ] All §13 tests present and pass.
- [ ] Existing Module 01–03 tests still pass.

**Style**

- [ ] Module docstring; type hints on every function; no `print()`; logging via
      `app.utils.logging_config` if any logging is added.

---

## 15. Assumptions and open questions

### Accepted V1 assumptions

- **A1** — Price-history date ranges are **inclusive on both ends**
  `[start_date, end_date]`. (Docs do not state inclusivity; inclusive is the
  least surprising and matches typical EOD vendors.)
- **A2** — The interface performs **no symbol normalization**; symbols are used
  verbatim and vendor-specific mapping is internal to concrete providers.
- **A3** — Data-fetching methods return `ServiceResult` with DTOs in
  `metadata` (rather than returning bare DTO lists), to fit the established
  cross-module contract and keep `ServiceResult` unchanged.
- **A4** — `PriceBar` omits `volume_adj` because `MASTER_SPEC.md` §7 marks it
  reserved/unused in V1. Add it only when a later version needs it.
- **A5** — `get_earnings` takes a single `ticker` (V1 earnings need is per-
  ticker, for the earnings-avoid window, `MASTER_SPEC.md` §20). A batch variant
  is deferred (non-blocking; see below).
- **A6** — `list_symbols(symbol_type=None)` returns all known symbols; with a
  `symbol_type` it filters. Whether YahooProvider can actually enumerate a full
  universe is a Module 05 concern; the **interface** simply defines the method.
- **A7** — A fresh `uuid4()` `run_id` inside a provider call is acceptable when
  no run id is supplied, mirroring Module 03's `apply_schema`.

### Open questions — non-blocking (defer to Module 05+)

- **Q1** — Batch price/earnings fetch (multiple tickers per call) for
  efficiency. Defer to Module 05/08; V1 interface is per-ticker.
- **Q2** — Whether `ProviderCapabilities` should expose rate-limit metadata
  (e.g. requests/min). Defer; not needed by the V1 contract.
- **Q3** — Corporate-action granularity (separate dividends/splits endpoints
  vs inline on `PriceBar`). V1 keeps them inline on `PriceBar`; revisit if
  Module 10 (mutation detection) needs a dedicated call.

### Open questions — blocking

- **None.** The contract above is fully implementable from `MASTER_SPEC.md`,
  `ARCHITECTURE.md`, `CODING_STANDARDS.md`, and the accepted Module 01–03 code
  without any further decision.

---

## 16. Quick summary

| Item | Module 04 decision |
|---|---|
| Abstraction | `MarketDataProvider(abc.ABC)` + `@abstractmethod` |
| DTOs | `PriceBar`, `PriceHistoryRequest`, `TickerInfo`, `EarningsEvent`, `ProviderCapabilities`, `ProviderErrorDetail` (all frozen dataclasses) |
| Methods | `get_capabilities`, `get_price_history`, `list_symbols`, `get_earnings` |
| Return type | `ServiceResult` (DTOs in `metadata`) |
| Error kinds | `unsupported_symbol`, `provider_unavailable`, `rate_limited`, `malformed_response`, `unsupported_capability` |
| Empty data | `success` + empty list + `rows_processed = 0` (not an error) |
| Dates | `datetime.date`, inclusive ranges |
| Vocabulary | reuse `app.config.constants` symbol/benchmark constants |
| Network / DB | none in Module 04 (interface only) |
| New deps | none |
| Files | `app/providers/{__init__,provider_interface}.py`, `tests/test_provider_interface.py`, optional `README.md` |
