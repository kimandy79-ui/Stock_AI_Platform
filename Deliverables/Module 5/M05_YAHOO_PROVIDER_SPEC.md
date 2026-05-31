# M05_YAHOO_PROVIDER_SPEC.md — Module 05 YahooProvider

> **Status**: source-of-truth for **Module 05 — YahooProvider**.
>
> Derived from: `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md`,
> `02_PROJECT_IMPLEMENTATION_CONTEXT.md`, `M04_PROVIDER_INTERFACE_SPEC.md`
> (the frozen Module 04 contract this module implements), and the accepted
> Module 05 implementation (`app/providers/yahoo_provider.py`).
>
> This document does **not** introduce new architecture and does **not** override
> any higher-priority source-of-truth document. Where it would conflict, the
> higher-priority document and the Module 04 contract win.

---

## 1. Purpose

Module 05 is the first **concrete** `MarketDataProvider`. It implements the
frozen Module 04 contract by fetching daily prices, ticker metadata, and
earnings dates from Yahoo via `yfinance`, and returning provider-neutral DTOs
wrapped in `ServiceResult`.

It is the single sanctioned location for Yahoo / `yfinance` access in the whole
codebase (`01_MASTER...` guardrail and decision 22.5: *no direct provider calls
outside the provider layer*).

## 2. Scope

**In scope**

- Subclass `MarketDataProvider` (ABC) and implement its four methods with the
  exact Module 04 §7.2 signatures.
- Fetch daily OHLCV + corporate actions; map each Yahoo row to a `PriceBar`
  with both raw and derived adjusted OHLC.
- Best-effort earnings dates and capability reporting.
- Honor the Module 04 success / empty / partial / failed `ServiceResult`
  semantics.

**Out of scope (belongs to other modules)**

- Persistence / DuckDB access (Module 02/03 + downstream writers).
- Universe construction and snapshots (Module 06).
- Benchmark / sector-ETF loading (Module 07).
- Daily price ingestion, validation, mutation detection, features, screening,
  proposals, outcomes, simulation, AI review, dashboard (Modules 08+).

Module 05 **only fetches and returns DTOs**. It never writes to a database,
imports `app.database`, or implements downstream logic.

## 3. Source-of-truth priority

1. `M04_PROVIDER_INTERFACE_SPEC.md` (the contract — frozen).
2. `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md`.
3. `02_PROJECT_IMPLEMENTATION_CONTEXT.md`.
4. This spec + the accepted implementation.

## 4. Location and public API

```text
app/providers/yahoo_provider.py
```

Exports (also re-exported from `app/providers/__init__.py`):

```python
PROVIDER_NAME: str = "yahoo"

class YahooProvider(MarketDataProvider):
    def __init__(
        self,
        yf_module: Any | None = None,
        symbol_source: Iterable[TickerInfo] | None = None,
    ) -> None: ...

    def get_capabilities(self) -> ServiceResult: ...
    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult: ...
    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult: ...
    def get_earnings(self, ticker: str) -> ServiceResult: ...
```

The four method signatures match the Module 04 abstract base exactly (parameter
names, annotations, defaults, and `-> ServiceResult`). The constructor adds two
**optional injection hooks** that do not alter any method signature:

- `yf_module` — an injected `yfinance`-like dependency exposing
  `Ticker(symbol)` whose result has `history(...)` and `calendar`. When `None`
  (production), the real `yfinance` is imported **lazily inside `__init__`**.
- `symbol_source` — an optional static universe (list of `TickerInfo`) for
  `list_symbols`. V1 does **not** scrape Yahoo (universe construction is
  Module 06); this hook exists for tests and future wiring.

`__init__` performs **no** network calls and **no** Yahoo access; its only side
effect when `yf_module` is omitted is a local `import yfinance`.

## 5. DTOs consumed (from Module 04 — unchanged)

`PriceHistoryRequest`, `PriceBar`, `TickerInfo`, `EarningsEvent`,
`ProviderCapabilities`, `ProviderErrorDetail`. Module 05 adds no new DTOs,
metadata keys, or error kinds to the frozen contract.

`ServiceResult.metadata` keys used (all documented in Module 04 §8):
`capabilities`, `bars`, `symbols`, `events`, `error_detail`, and
`provider_name == "yahoo"` on every call.

## 6. Method behavior

### 6.1 `get_capabilities`

Pure metadata, no network. Returns `success` + a `ProviderCapabilities`:

| field | value |
|---|---|
| `provider_name` | `"yahoo"` |
| `supports_daily_prices` | `True` |
| `supports_adjusted_prices` | `True` |
| `supports_earnings` | `True` (best-effort calendar path) |
| `supports_ticker_listing` | `True` only when a static `symbol_source` was injected, else `False` |

### 6.2 `get_price_history`

Returns daily bars for `request.ticker` over the **inclusive** range
`[start_date, end_date]`.

- **Inclusive→exclusive end**: yfinance treats `end` as exclusive, so the
  provider calls it with `end = end_date + 1 day`. Output is additionally
  filtered to `[start_date, end_date]`, so a bar dated exactly `end_date` is
  included and none after it is returned. Single-day ranges (`start == end`) are
  valid.
- yfinance is called with `auto_adjust=False, actions=True` so that raw OHLC,
  `Adj Close`, `Dividends`, and `Stock Splits` are all available.
- **Row mapping** (`PriceBar`): raw OHLCV from `Open/High/Low/Close/Volume`;
  `Dividends → dividend_amount`, `Stock Splits → split_ratio` (else `None`);
  index → `datetime.date`; `source_provider = "yahoo"`.
- **Adjusted OHLC derivation**: `factor = Adj Close / Close`, applied to each of
  open/high/low; `close_adj = Adj Close`. The factor is a transient local and is
  **never** exposed on a DTO. If `Close` is missing/zero or `Adj Close` is
  missing, that row's adjusted fields are `None`.
- **`^VIX` rule**: adjusted OHLC mirrors raw OHLC, so `close_raw == close_adj`
  (`01_MASTER...` §5). Null/zero volume is tolerated.
- `metadata["bars"]` is `list[PriceBar]`; `rows_processed == len(bars)`.

### 6.3 `list_symbols`

V1 never scrapes Yahoo.

- No `symbol_source` injected → `success` + empty `symbols` +
  `rows_processed == 0` + a warning that enumeration is deferred to Module 06.
- Static `symbol_source` injected → maps it to `TickerInfo`, optionally filtered
  by `symbol_type`.
- Either way, **no network access**.

### 6.4 `get_earnings`

Best-effort, reads the yfinance `Ticker.calendar` attribute (no web scraping).

- No reliable date → `success` + empty `events` + `rows_processed == 0`.
- Produced `EarningsEvent`s use `confidence == "low"`, `session == "unknown"`,
  `source_provider == "yahoo"` (consistent with the V1 limitation: *earnings
  source may be LOW confidence*).
- Supports the modern `dict` calendar form (`{"Earnings Date": [...]}`) plus a
  defensive DataFrame-like fallback; unparseable calendars degrade to empty +
  warning, not failure.

## 7. Error handling (Module 04 §7.3 / §9)

Documented conditions are **returned** as a `failed` `ServiceResult` carrying a
`ProviderErrorDetail` in `metadata["error_detail"]` — they are not raised. An
**empty result is not an error** (success + empty list).

Exception classification at the vendor-call boundary:

| condition | `ProviderErrorDetail.kind` |
|---|---|
| throttling (rate limit / 429 / "too many requests") | `rate_limited` |
| unknown / delisted / not-found symbol | `unsupported_symbol` |
| unparseable price payload | `malformed_response` |
| other vendor / network failure (default) | `provider_unavailable` |

All kinds are members of the frozen `PROVIDER_ERROR_KINDS`. Bugs in mapping code
outside the vendor-call boundary still propagate (per Module 04 §7.3).

Every call logs start / end / rows / warnings / errors via a bound-`run_id`
logger (`logging_config.get_logger(__name__, run_id)`); `run_id` is a fresh
`uuid4` per call. No `print()`.

## 8. Architecture boundaries (guardrails honored)

- **Provider isolation**: only `yahoo_provider.py` references `yfinance`, and the
  import is lazy. No other module imports `yfinance`. Importing the providers
  package does **not** import `yfinance`.
- **No pandas leakage**: `pandas` is not imported by the provider. The vendor
  frame is consumed through its duck-typed interface (`empty`, `columns`,
  `iterrows`) and converted to plain DTOs at this boundary, so no DataFrame ever
  leaves the provider (CODING_STANDARDS: *prefer Polars; pandas only when
  unavoidable*).
- **No database access**: no `import duckdb`, no `app.database`, no
  connection/`ATTACH`.
- **No downstream logic**: no ingestion, validation, screening, scoring,
  proposals, outcomes, simulation, AI review, or dashboard code.

## 9. Dependencies

No new dependencies. `yfinance` and `pandas` were already declared in
`requirements.txt` and `pyproject.toml`; neither file was modified by Module 05.

## 10. Testing requirements

`tests/test_yahoo_provider.py` runs **fully offline**:

- Inject a deterministic fake `yfinance` via `YahooProvider(yf_module=...)`;
  feed vendor rows as small in-test `pandas` DataFrames mirroring the Yahoo
  columns. No real network, no live Yahoo, no DuckDB.
- Coverage: import/subclass smoke; signature conformance vs the Module 04 base;
  capabilities; price-history happy path; inclusive end-date → exclusive vendor
  end; single-day range; adjusted-OHLC derivation + partial-warning;
  empty-frame-is-success; unknown-symbol-is-failed; rate-limit and
  network-error mapping; `list_symbols` no-scrape (+ static-source filter);
  best-effort earnings (empty + low-confidence event); `^VIX` mapping;
  constructor performs no network; static scan that only `yahoo_provider.py`
  references `yfinance` and that it has no DuckDB/`print`/`requests`/`urllib`/
  `socket`; type-hint/docstring style.
- All Module 01–04 tests continue to pass unchanged.

## 11. Downstream consumers (what later modules may rely on)

- A concrete `MarketDataProvider` is available as
  `app.providers.YahooProvider`, constructible without arguments in production.
- Price bars carry both raw and adjusted OHLC; adjusted OHLC is `None` per-row
  when underivable; `^VIX` has `close_raw == close_adj`.
- Symbol enumeration is **not** provided in V1 — Module 06 (Universe Snapshot
  Engine) is responsible for universe construction; it must not assume
  `YahooProvider.list_symbols()` returns a full universe.
- Earnings are LOW confidence with `session == "unknown"`.

## 12. Acceptance checklist

- [x] `YahooProvider(MarketDataProvider)` implements all four methods with exact
      Module 04 signatures.
- [x] Returns real `ServiceResult`s with the documented metadata keys and
      `provider_name == "yahoo"`.
- [x] Inclusive `[start_date, end_date]` honored (vendor `end = end_date + 1d`).
- [x] Adjusted OHLC derived; `adjustment_factor` never on a DTO; `^VIX`
      `close_raw == close_adj`.
- [x] Empty = success; documented error conditions = `failed` + `error_detail`
      with a `PROVIDER_ERROR_KINDS` kind.
- [x] `list_symbols` does not scrape; `get_earnings` best-effort LOW confidence.
- [x] Yahoo/`yfinance` confined to this file; lazy import; no pandas leakage; no
      DB access; no `print()`.
- [x] No new dependencies; no frozen Module 01–04 file modified.
- [x] Offline, isolated pytest tests; Module 01–04 tests still pass.

## 13. Assumptions / open questions

- **Identity vendor-symbol mapping** in V1 (`_to_vendor_symbol` is the identity;
  Yahoo already uses `SPY`/`^VIX`/`XLK`); single internal hook for any future
  vendor quirk.
- **Exception classification by type/message tokens**; unrecognized vendor-call
  failures default to `provider_unavailable`.
- **Earnings calendar parsing** supports the modern `dict` form plus a defensive
  DataFrame-like fallback; unparseable calendars degrade gracefully.
- The `symbol_source` constructor hook is a test/forward-wiring convenience and
  is **not** a universe-construction mechanism; that remains Module 06.
