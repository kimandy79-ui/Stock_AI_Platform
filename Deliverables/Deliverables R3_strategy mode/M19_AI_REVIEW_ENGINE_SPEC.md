# M19 — AI Review Engine Spec

Module 19 sends a previously-recorded manual review row (written by Module 18)
to an AI provider, records the AI response onto the same row, and later records
the reviewer's qualitative `human_action`. It is an **UPDATE-only** overlay on
`ai_reviews` (ticker, `prod` / `debug`) and `sim_ai_reviews` (simulation). No
row is ever inserted or deleted, and no other table is touched.

Source of truth: `01b_SCHEMA_AND_DATA.md` + `M02_SCHEMA_SPEC.md` §3.19 / §4.9
(`ai_reviews` / `sim_ai_reviews`), `01a_CORE_PRINCIPLES.md` (`human_action`
enum), `M18_EXPORT_PACKAGE_ENGINE_SPEC.md` (the review-row write contract this
module consumes), `app/utils/service_result.py` (`ServiceResult`), and
`app/config/env.py` / `app/config/settings.py` (lazy provider/key resolution).

## 1. Public API

```python
class AiReviewEngine:
    def __init__(self, db_manager=None, ai_client=None) -> None: ...

    def send_ticker_review(
        self, ai_review_id, db_role="prod", run_id=None,
    ) -> ServiceResult: ...

    def send_simulation_review(
        self, ai_review_id, db_role="simulation", run_id=None,
    ) -> ServiceResult: ...

    def record_human_action(
        self, ai_review_id, human_action, db_role="prod", run_id=None,
    ) -> ServiceResult: ...
```

- Always returns `ServiceResult`; never raises for expected validation / DB /
  AI failures.
- `run_id` is minted with `uuid4()` only when `None`; a supplied value is
  preserved verbatim.
- `db_manager` is injected; defaults to `app.database.duckdb_manager`.
- `ai_client` is injected; defaults to `DefaultAiClient`, which resolves
  config / env / SDK **lazily at call time**, never at import time.

## 2. Role / id / action validation (before any DB or AI access)

- `send_ticker_review`: `db_role ∈ {"prod", "debug"}`, non-empty `ai_review_id`.
- `send_simulation_review`: `db_role == "simulation"`, non-empty `ai_review_id`.
- `record_human_action`: `db_role ∈ {"prod", "debug"}`, non-empty
  `ai_review_id`, `human_action ∈ {"ignored", "accepted", "overrode",
  "deferred"}`.

Validation failures return `failed` with **no DB access and no AI call**.

## 3. Metadata keys (every return path)

Send methods (`send_ticker_review` / `send_simulation_review`):

```
run_id, export_type, db_role, ai_review_id, provider, model,
prompt_version, response_chars, status, error
```

`record_human_action`:

```
run_id, db_role, ai_review_id, human_action, status, error
```

`provider` / `model` / `prompt_version` / `response_chars` / `error` are `None`
on paths where the value is not yet available (e.g. validation failure, row not
found). After a successful read, `provider` / `model` / `prompt_version` are
echoed from the row on both success and subsequent-failure paths. `export_type`
mirrors the flow (`ticker_review` / `simulation_review`); `status` is
`success` / `failed`.

## 4. Send flow (read → guard → AI → write)

Ticker and simulation share one flow; only the table and allowed role differ.

1. Validate role + id (no I/O).
2. Read the review row **read-only**:
   ```sql
   SELECT ai_review_id, review_type, prompt_text, ai_response_text,
          provider, model, prompt_version
   FROM ai_reviews        -- sim flow reads sim_ai_reviews
   WHERE ai_review_id = ?
   ```
3. Row not found → `failed`; **no AI call, no write**.
4. `ai_response_text IS NOT NULL` → `failed`; **no AI call, no write**; error
   exactly:
   ```
   review already sent (ai_response_text is not null); use force=True to override
   ```
   See **G-FORCE-RESEND**.
5. Call `ai_client.send(prompt_text, provider, model)` with the row's stored
   `prompt_text` / `provider` / `model` (the engine never hardcodes provider or
   model).
6. UPDATE only `ai_response_text = ?` on the same row.
7. Return `success` with `response_chars = len(response_text)` and the row's
   `provider` / `model` / `prompt_version` echoed in metadata.

### Failure semantics

- **AI call failure** (any exception from `ai_client.send`): `failed`, **no DB
  write**; row's `provider` / `model` / `prompt_version` echoed, `response_chars`
  `None`.
- **DB write failure after a successful AI response**: `failed`; the AI response
  is obtained but not persisted. See **G-RESPONSE-ORPHAN**. `response_chars`
  reflects the obtained (unpersisted) response length.

## 5. `record_human_action` flow

1. Validate role + id + enum (no I/O).
2. Read the ticker review row from `ai_reviews` **read-only**; not found →
   `failed`.
3. `ai_response_text IS NULL` → `failed`; error exactly:
   ```
   cannot record human action before AI send
   ```
4. UPDATE only `human_action = ?` on that row.
5. Return `success`.

Note: this API currently targets **ticker** review rows only (`db_role ∈
{"prod", "debug"}`).

## 6. AI client Protocol and lazy default client

```python
class AiClientProtocol(Protocol):
    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]: ...
    # returns (response_text, model_used); raises on any failure
```

`DefaultAiClient`:

- Reads `provider` / `model` supplied by the engine from the review row; it
  never hardcodes them.
- Resolves the API key from `app.config.env` at call time:
  `ANTHROPIC_API_KEY` (provider `anthropic`) / `OPENAI_API_KEY` (provider
  `openai`). See **G-API-KEY-ENV**.
- Imports no provider SDK at module import time. The SDK is imported **lazily**
  inside `send`; a missing SDK is wrapped in a clear `RuntimeError`. See
  **G-SDK-DEP**.
- Any failure (unknown provider, missing key, SDK import, provider/network
  error) raises, which the engine surfaces as an AI call failure with no DB
  write.

Tests never invoke `DefaultAiClient`; an injected fake `AiClientProtocol` is
used so no real AI / network call occurs.

## 7. Mutation boundaries

- Mutates **only**: `ai_reviews.ai_response_text`, `ai_reviews.human_action`,
  `sim_ai_reviews.ai_response_text`, `sim_ai_reviews.human_action`.
- **UPDATE only** — no `INSERT`, no `DELETE`.
- No direct `duckdb` import; all DB access via the injected manager /
  `app.database.duckdb_manager`.
- No market-data provider imports or calls; Module 19 uses its own AI client
  abstraction.
- No `print()`, no DDL, no `ATTACH`.
- AI calls happen only inside an explicit `send_*` invocation.
- AI review is a qualitative overlay only; it does not affect mechanical
  performance attribution.

## 8. Assumptions / open gaps

- **G-FORCE-RESEND** — a row whose `ai_response_text` is already set is treated
  as already sent and is refused. A `force=True` re-send override is named in
  the error string but is **not implemented** in this module; it is reserved for
  a future revision so resends are an explicit, deliberate action.
- **G-RESPONSE-ORPHAN** — if the AI call succeeds but the single
  `ai_response_text` UPDATE then fails, the obtained response is lost (not
  persisted) and a `failed` `ServiceResult` is returned. There is no retry or
  staging buffer; the reviewer simply re-runs the send (the row is still
  unsent, so no double-send guard is triggered).
- **G-API-KEY-ENV** — the default client expects `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` in the environment (loaded via `app.config.env`). Other
  providers are unsupported by the default client and raise.
- **G-SDK-DEP** — Module 19 adds **no** provider SDK dependency. The default
  client imports `anthropic` / `openai` lazily and raises a clear error if the
  SDK is absent. Injecting a custom `AiClientProtocol` is the supported path for
  environments without those SDKs.

## 9. Test summary

`tests/test_ai_review_engine.py` runs fully offline (in-memory fake DB manager,
fake AI client; no DuckDB / network / SDK). Coverage:

- `ServiceResult` success/failure and `run_id` mint/preserve for all three
  public methods; exact metadata key sets for send and human-action paths.
- Pre-DB/pre-AI validation: invalid roles, empty `ai_review_id`, invalid
  `human_action` — all fail with no DB connection and no AI call.
- Row-not-found on send and on `record_human_action`: failed, no AI call, no
  write.
- Double-send guard: existing `ai_response_text` fails before AI call/write;
  row `provider` / `model` / `prompt_version` echoed.
- Successful ticker send (`prod` and `debug`): writes `ai_response_text`,
  correct `response_chars`, echoes `provider` / `model` / `prompt_version`.
- Successful simulation send writes to `sim_ai_reviews` and leaves `ai_reviews`
  untouched.
- AI call failure: failed, no DB write, `response_chars` `None`.
- DB write failure after AI call: response-orphan path (`G-RESPONSE-ORPHAN`),
  row not persisted.
- Read failure surfaced as `failed`.
- `record_human_action` success for every enum value; blocked when
  `ai_response_text IS NULL`.
- Static scans: no `duckdb` import, no market-data provider import, no
  `print()`, no DDL/`ATTACH` in executed SQL, only `UPDATE` (no `INSERT` /
  `DELETE`), only `ai_reviews` / `sim_ai_reviews` updated, only
  `ai_response_text` / `human_action` in SET clauses, and no module-level
  provider-SDK import.
