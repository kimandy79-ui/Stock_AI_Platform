# M19 — AI Review Engine Spec

Module 19 sends a previously-recorded manual review row (written by Module 18)
to an AI provider, records the AI response onto the same row, and later records
the reviewer's qualitative `human_action`. It is an **UPDATE-only** overlay on
`ai_reviews` (ticker, `prod` / `debug`) and `sim_ai_reviews` (simulation). No
row is ever inserted or deleted, and no other table is touched.

**Phase 3 delta (2026-07-05) — multi-pass review (`review_kind`).** Module 18
may now write **one row per pass** — `thesis` / `contrarian` / `audit` — for a
single export event, distinguished by the new `review_kind` column (nullable;
`NULL` = legacy/single-row export, unchanged from pre-Phase-3 behavior). Each
`review_kind` is its own independent row with its own `ai_review_id`, `provider`,
`model`, and `ai_response_text` — Module 19's `_send` flow already operates on
one row per call, so the double-send guard and every other send-flow rule
below is **unchanged** and already correctly per-row; only the read/metadata
surface grew to include `review_kind`. See
`M18_EXPORT_PACKAGE_ENGINE_SPEC.md` for the row-creation side and
`01c_FORMULAS_AND_CONFIGS.md` §63 for how Step 5 consumes the contrarian/audit
scores derived from these rows.

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
run_id, export_type, db_role, ai_review_id, review_kind, provider, model,
prompt_version, response_chars, status, error
```

`review_kind` (Phase 3) echoes the row's `review_kind` column
(`"thesis"` / `"contrarian"` / `"audit"` / `None` for a legacy row) on every
return path once the row has been read — `None` on validation-failure /
row-not-found paths where no row was ever read.

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
   SELECT ai_review_id, review_type, review_kind, prompt_text, ai_response_text,
          provider, model, prompt_version
   FROM ai_reviews
   WHERE ai_review_id = ?
   ```
   The `sim_ai_reviews` flow reads the equivalent columns **except**
   `review_type`, which that table does not have (`sim_ai_reviews` has no
   `review_type` column — see `M02_SCHEMA_SPEC.md` §4.9 /
   G-SIM-AI-SCHEMA). **Bug fix (Phase 3, 2026-07-05):** the sim-flow read
   previously selected `review_type` from `sim_ai_reviews` anyway, which
   would raise against a real DuckDB connection (`column "review_type" not
   found`) — never caught before because this module's test suite is
   entirely offline with a fake connection. Fixed while adding `review_kind`
   to the same query.
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

## 6a. Structured pass output parsing (Phase 3)

Two pure, no-I/O module-level functions parse a `review_kind`'s
`ai_response_text` into the derived score Step 5 consumes. Neither is called
by `_send` itself — they're used by whatever caller (e.g. Step 5's
`propose(ai_review_scores=...)`) has already read a row's `ai_response_text`
and wants the derived score.

```python
def parse_audit_response(response_text: str | None) -> dict[str, Any] | None: ...
def parse_contrarian_response(response_text: str | None) -> dict[str, Any] | None: ...
```

- **`parse_audit_response`** expects
  `{"claims": [{"claim": str, "classification": "grounded"|"speculative"|"unverifiable"}, ...]}`
  and returns `{"grounded": int, "speculative": int, "unverifiable": int,
  "total": int, "audit_consistency_score": float}` (`100 * grounded / total`).
  Claims with an unrecognized classification are skipped (not counted in
  `total`).
- **`parse_contrarian_response`** expects
  `{"risk_score": 0-100, "concerns": [str, ...]}` and returns
  `{"risk_score": float, "concerns": list}`, `risk_score` clamped to `[0, 100]`.
- Both return `None` on falsy/unparseable/non-object JSON or missing required
  fields (empty `claims`, missing/non-numeric `risk_score`). **A malformed
  response degrades to "no score available" rather than a penalty** — an AI
  response that failed to follow the requested format is not itself evidence
  of a bad thesis, only of unusable output; Step 5 treats an absent score
  exactly like a pass that hasn't run yet.
- The `thesis` pass has no structured-output requirement (freeform assessment,
  same as every review before Phase 3).

## 7. Mutation boundaries

- Mutates **only**: `ai_reviews.ai_response_text`, `ai_reviews.human_action`,
  `sim_ai_reviews.ai_response_text`, `sim_ai_reviews.human_action`.
- **UPDATE only** — no `INSERT`, no `DELETE`. This is unchanged by Phase 3:
  the multi-pass **row creation** (one `INSERT` per `review_kind`) is entirely
  Module 18's responsibility (`M18_EXPORT_PACKAGE_ENGINE_SPEC.md`) — Module 19
  only ever `SELECT`s and conditionally `UPDATE`s one already-existing row per
  `send_*` call, exactly as before.
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

**Phase 3 additions:** `review_kind` echoed in send metadata for both a
`thesis`/`contrarian`/`audit` row and a legacy `None`-kind row; double-send
guard confirmed independent across two rows sharing the same underlying
proposal (different `ai_review_id`s, different `review_kind`s) — i.e. sending
one pass never blocks or is blocked by another pass's row; `parse_audit_response`
/ `parse_contrarian_response` covered for a clean response, a
high-`unverifiable` response, and malformed/missing-field inputs (`None` in
every case, not an exception or a hard-0 score).
