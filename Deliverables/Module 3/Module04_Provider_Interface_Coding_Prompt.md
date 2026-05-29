# Module 04 Coding Prompt — Provider Interface

You are a senior Python engineer implementing the next module of a local swing-trading stock analyzer.

## FILES ATTACHED

I am attaching exactly three files:

1. `stock_ai_platform_module03_stable.zip`

   * Current stable codebase after Modules 01, 02, and 03.
   * Modules 01–03 are frozen and accepted.
   * This zip already contains the accepted project layout, docs, tests, and Module 03 schema manager.

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

   * Final source-of-truth for Module 04.
   * Defines the exact provider interface contract, DTOs, method signatures, error semantics, testing requirements, scope boundaries, and acceptance checklist.

Do not ask for old archives. The implementation base is `stock_ai_platform_module03_stable.zip`.

---

## SOURCE OF TRUTH PRIORITY

Use the sources in this priority order:

1. `PROVIDER_INTERFACE_SPEC.md`

   * Highest authority for Module 04.
   * Use it for class names, DTO names, fields, method signatures, return contracts, `ServiceResult` metadata keys, error kinds, testing requirements, and forbidden behavior.
   * Do not invent any provider method, DTO, enum, dependency, or behavior not present in this document.

2. `StockAnalyzer_Shared_Context_Pack_v1_3 (M01).zip`

   * Use for architecture rules, coding standards, module boundaries, `ServiceResult` rules, logging rules, and testing discipline.

3. Existing code inside `stock_ai_platform_module03_stable.zip`

   * Use as the implementation base.
   * Do not modify frozen Module 01, Module 02, or Module 03 behavior.

If `PROVIDER_INTERFACE_SPEC.md` conflicts with older high-level docs, do not guess. Stop and report the conflict.

---

## TASK

Implement ONLY **Module 04 — Provider Interface**.

According to `PROVIDER_INTERFACE_SPEC.md`:

* Module 04 defines the ABSTRACT provider contract only.
* Module 04 creates a provider-neutral interface for market data.
* Module 04 defines DTOs and structured provider error/status semantics.
* Module 04 must be fully testable without any network and without any database access.
* Module 04 must be implementable as-is by Module 05 YahooProvider later.

Module 04 must NOT implement YahooProvider or any concrete provider.

---

## STRICT SCOPE — ALLOWED

You may add or modify only:

1. `app/providers/__init__.py`
2. `app/providers/provider_interface.py`
3. `tests/test_provider_interface.py`
4. `README.md` only if needed to add a short Module 04 usage note

If `app/providers/` does not exist, create it as a Python package.

Prefer the simplest implementation. A single `provider_interface.py` containing the ABC, DTOs, constants, and error type is expected.

Do not split the interface into many files.

---

## STRICT SCOPE — FORBIDDEN

Do NOT modify any Module 01/02/03 file, including:

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
* `conftest.py`
* `pyproject.toml`
* `requirements.txt`
* `.gitignore`
* `.env.example`
* any `docs/*.md`
* any file inside the shared context pack

Do NOT:

* import `yfinance`
* import or use `requests`, `urllib`, `http`, `socket`, or any network client
* make network calls
* implement YahooProvider
* implement any concrete data provider
* open DuckDB connections
* import `duckdb`
* import `app.database`
* read or write a database
* change the schema
* add third-party dependencies
* implement ticker-universe updates
* implement benchmark loading
* implement daily price ingestion
* implement validation or mutation detection
* implement screening, scoring, trading, simulation, AI review, or dashboard logic
* implement Module 05 or later modules

---

## REQUIRED IMPLEMENTATION

Implement `app/providers/provider_interface.py` exactly according to `PROVIDER_INTERFACE_SPEC.md`.

### Required abstraction

Implement:

```python
class MarketDataProvider(abc.ABC):
    ...
```

Use `abc.ABC` and `@abstractmethod`.

Do not replace this with `typing.Protocol`.

### Required DTOs

Implement all DTOs as `@dataclass(frozen=True)` with full type hints:

```python
PriceBar
PriceHistoryRequest
TickerInfo
EarningsEvent
ProviderCapabilities
ProviderErrorDetail
```

Use the exact field names, types, optionality, and validation rules from `PROVIDER_INTERFACE_SPEC.md` §6.

Dates must use `datetime.date`.

Do not introduce Pydantic.

Do not add fields not listed in the spec.

Important examples:

* `PriceBar` must include raw OHLC, adjusted OHLC, `volume_raw`, `dividend_amount`, `split_ratio`, and `source_provider`.
* `PriceBar` must NOT include `volume_adj`.
* `PriceHistoryRequest` must enforce `start_date <= end_date`.
* `PriceHistoryRequest.symbol_type` and `TickerInfo.symbol_type` must validate against `app.config.constants.ALLOWED_SYMBOL_TYPES`.
* DTOs must be frozen.

### Required constants

Define:

```python
PROVIDER_ERROR_KINDS: tuple[str, ...]
```

with exactly the error kinds from `PROVIDER_INTERFACE_SPEC.md` §9:

```text
unsupported_symbol
provider_unavailable
rate_limited
malformed_response
unsupported_capability
```

Reuse existing vocabulary from `app.config.constants` for symbol types and benchmark symbols. Do not redefine those vocabularies.

### Required abstract methods

Implement exactly these abstract methods with the signatures from `PROVIDER_INTERFACE_SPEC.md` §7.2:

```python
def get_capabilities(self) -> ServiceResult: ...

def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult: ...

def list_symbols(self, symbol_type: str | None = None) -> ServiceResult: ...

def get_earnings(self, ticker: str) -> ServiceResult: ...
```

All four methods must return `ServiceResult` from `app.utils.service_result`.

The abstract methods should contain only docstrings and an empty abstract body (`...` or `raise NotImplementedError`). Module 04 must not perform real behavior.

### Required ServiceResult metadata contract

Use the stable metadata keys from `PROVIDER_INTERFACE_SPEC.md` §8:

| Method | Metadata key | Value type |
|---|---|---|
| `get_capabilities` | `capabilities` | `ProviderCapabilities` |
| `get_price_history` | `bars` | `list[PriceBar]` |
| `list_symbols` | `symbols` | `list[TickerInfo]` |
| `get_earnings` | `events` | `list[EarningsEvent]` |
| any failure | `error_detail` | `ProviderErrorDetail` |
| any method | `provider_name` | `str` |

Module 04 does not need to create concrete `ServiceResult`s except inside tests/fake provider. But the interface docstrings and tests must reflect this contract.

---

## REQUIRED TESTS

Create `tests/test_provider_interface.py`.

Tests must follow the discipline from the existing test suite and the checklist in `PROVIDER_INTERFACE_SPEC.md` §13.

At minimum, tests must cover:

1. Import smoke:
   * `MarketDataProvider`
   * all DTOs
   * `PROVIDER_ERROR_KINDS`

2. Abstract enforcement:
   * `MarketDataProvider()` cannot be instantiated directly.
   * An incomplete subclass that omits a required method raises `TypeError`.

3. Fake provider:
   * Create an in-test `FakeProvider(MarketDataProvider)` implementing all four methods.
   * It must instantiate successfully.
   * Each method returns a valid `ServiceResult`.

4. Signature conformance:
   * Use `inspect.signature`.
   * Verify method names, parameters, and return annotations match `PROVIDER_INTERFACE_SPEC.md`.

5. DTO construction:
   * Valid construction for every DTO.
   * DTOs are frozen; assigning to a field raises `dataclasses.FrozenInstanceError`.

6. DTO validation:
   * Empty ticker raises.
   * `PriceHistoryRequest(start_date > end_date)` raises.
   * Invalid `symbol_type` raises.

7. Empty-result semantics:
   * Fake provider returns `success` + empty list + `rows_processed == 0` for valid no-data query.

8. Error semantics:
   * Fake provider returns `failed`.
   * `metadata["error_detail"]` is a `ProviderErrorDetail`.
   * `error_detail.kind in PROVIDER_ERROR_KINDS`.

9. ServiceResult contract:
   * Returned objects are real `ServiceResult`s.
   * Status is one of allowed statuses.
   * Documented metadata keys are present.

10. Forbidden static scans:
   * `provider_interface.py` contains no `yfinance`.
   * no `requests`, `urllib`, `http`, `socket`.
   * no `duckdb`.
   * no `app.database`.
   * no `duckdb.connect(`.
   * no `print(`.

11. No database / network:
   * Tests must not touch any real DB.
   * Tests must not use network.
   * No `tmp_path` DB redirection is needed because Module 04 must never open a DB.

12. Style:
   * Module docstring present.
   * Every function and method has type hints.

Existing Module 01, Module 02, and Module 03 tests must continue to pass.

---

## LOGGING AND STYLE

Use project style from `CODING_STANDARDS.md`.

In `provider_interface.py`, include:

```python
from app.utils import logging_config

_LOG = logging_config.get_logger(__name__)
```

Even if the interface itself logs nothing yet, this keeps consistency with project modules.

Do not use `print()` in library code.

Every new Python file must have a module-level docstring.

Every function and method must have type hints.

Use stdlib only.

Do not add new dependencies.

Keep the code small and boring.

No retry framework.

No connection pools.

No threading.

No network.

No DB.

---

## README

README may be updated only if you add a short Module 04 note.

Do not over-document.

Do not modify `docs/*.md`.

README examples may show how a future concrete provider would implement the interface, but must not include real network calls or yfinance.

---

## OUTPUT REQUIRED

Return:

1. Updated project zip.

   * Top-level folder must be `stock_ai_platform/`.
   * Preserve the same layout as Module 03 stable zip.

2. List of added/changed files.

3. Design notes:

   * where the provider interface is implemented;
   * why `abc.ABC` was used;
   * how DTOs map to `PROVIDER_INTERFACE_SPEC.md`;
   * how `ServiceResult` metadata contract is represented;
   * how Module 04 avoids network, DB, yfinance, and concrete-provider logic;
   * how frozen Modules 01–03 are protected.

4. Test command and full test result.

   Preferred:

   ```bash
   pytest -q
   ```

   If the environment cannot run tests, clearly state why and list static checks performed.

5. Any assumptions.

   * Do not hide assumptions.
   * Do not add assumptions if the answer is explicitly specified in `PROVIDER_INTERFACE_SPEC.md`.

6. Suggested commit message:

```text
module04_provider_interface_stable
```

---

## STARTING STEPS

Read in this order:

1. `PROVIDER_INTERFACE_SPEC.md`
2. `docs/ARCHITECTURE.md`
3. `docs/MASTER_SPEC.md`
4. `docs/CODING_STANDARDS.md`
5. `app/utils/service_result.py`
6. `app/config/constants.py`
7. `tests/test_duckdb_manager.py`
8. `tests/test_schema_manager.py`

Then implement Module 04.

Do not reopen architecture.

Do not implement Module 05 or later.

Do not modify any Module 01/02/03 file.

Do not modify any `docs/*.md`.

Do not ask for old archives.
