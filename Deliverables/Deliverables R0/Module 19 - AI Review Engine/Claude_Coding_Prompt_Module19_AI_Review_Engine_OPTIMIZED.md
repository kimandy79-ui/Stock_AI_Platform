# Claude Coding Prompt — Module 19: AI Review Engine

Use Project Instructions and Project Files. Use `00_PROJECT_FILE_MAP.md` only for targeted retrieval. Do not restate global rules.

## Scope / Inputs

Base code: `stock_ai_platform_module18_export_package_engine_stable.zip`.

Implement Module 19 only and create `M19_AI_REVIEW_ENGINE_SPEC.md` because no Module 19 spec exists yet.

Allowed changes:
```text
app/services/ai_review/__init__.py
app/services/ai_review/ai_review_engine.py
tests/test_ai_review_engine.py
M19_AI_REVIEW_ENGINE_SPEC.md
README.md                           # Module 19 note only
```

## Public API

```python
class AiReviewEngine:
    def __init__(self, db_manager=None, ai_client=None) -> None: ...

    def send_ticker_review(
        self,
        ai_review_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...

    def send_simulation_review(
        self,
        ai_review_id: str,
        db_role: str = "simulation",
        run_id: str | None = None,
    ) -> ServiceResult: ...

    def record_human_action(
        self,
        ai_review_id: str,
        human_action: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

Module-specific rules:
- Always return `ServiceResult` on expected validation/DB/AI failures.
- Mint `uuid4()` only when `run_id is None`; preserve supplied `run_id`.
- Inject `db_manager`; default `app.database.duckdb_manager`.
- Inject `ai_client`; default client must resolve config/env lazily at call time, never at import time.
- `ai_client` must use an injectable Protocol. Tests must never call real AI/network.

## Pre-DB Validation

Before any DB or AI access:
- `send_ticker_review`: `db_role in {"prod", "debug"}` and non-empty `ai_review_id`.
- `send_simulation_review`: `db_role == "simulation"` and non-empty `ai_review_id`.
- `record_human_action`: `db_role in {"prod", "debug"}`, non-empty `ai_review_id`, and `human_action in {"ignored", "accepted", "overrode", "deferred"}`.

## Stable Metadata Keys

Every return path must use exact keys.

Send methods:
```text
run_id, export_type, db_role, ai_review_id, provider, model,
prompt_version, response_chars, status, error
```

`record_human_action`:
```text
run_id, db_role, ai_review_id, human_action, status, error
```

Use `None` for unavailable failed-path values.

## Send Flow

Ticker and simulation share the same flow; only table/role differ.

Read row using read-only connection:
```sql
SELECT ai_review_id, review_type, prompt_text, ai_response_text,
       provider, model, prompt_version
FROM ai_reviews
WHERE ai_review_id = ?
```

For simulation, read the same columns from `sim_ai_reviews`.

Flow:
1. Validate role + id before DB/AI.
2. Read review row read-only.
3. If not found: failed `ServiceResult`; no AI call/write.
4. If `ai_response_text IS NOT NULL`: failed `ServiceResult`; no AI call/write; error exactly:
   ```text
   review already sent (ai_response_text is not null); use force=True to override
   ```
   Document `G-FORCE-RESEND` in spec.
5. Call `ai_client.send(prompt_text, provider, model)`.
6. Write only `ai_response_text = response_text` to the same row.
7. Return success with `response_chars = len(response_text)` and row `provider/model/prompt_version` echoed in metadata.

Failure semantics:
- AI call failure: failed `ServiceResult`; no DB write.
- DB write failure after AI response: failed `ServiceResult`; document `G-RESPONSE-ORPHAN`.

## AI Client Protocol

```python
class AiClientProtocol(Protocol):
    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]: ...
    # returns (response_text, model_used); raises on failure
```

Default client requirements:
- Read `provider` and `model` from review row; engine must not hardcode them.
- Resolve provider/model/API key from `app.config.env` / `app.config.settings` at call time.
- Document expected env vars in spec as `G-API-KEY-ENV`, e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.
- No provider SDK import at module import time.
- If SDK import is needed, import lazily inside default client and wrap `ImportError` with clear message.
- Do not add SDK dependencies unless already present; document as `G-SDK-DEP`.

## `record_human_action` Flow

1. Validate role + id + enum before DB.
2. Read ticker review row from `ai_reviews` read-only; fail if not found.
3. If `ai_response_text IS NULL`: failed `ServiceResult` with error exactly:
   ```text
   cannot record human action before AI send
   ```
4. Write only `human_action = ?` to that row.
5. Return success.

Note: API currently targets ticker review rows only via `db_role in {"prod", "debug"}`.

## Module-Specific Boundaries

- Mutate only:
  - `ai_reviews.ai_response_text`
  - `ai_reviews.human_action`
  - `sim_ai_reviews.ai_response_text`
  - `sim_ai_reviews.human_action`
- Module 19 must use UPDATE only; no INSERT/DELETE.
- AI calls only on explicit `send_*` method invocation.
- AI review is qualitative overlay only and must not affect mechanical performance attribution.
- Do not import/call market-data providers; Module 19 uses its own AI client abstraction.

## Targeted Retrieval Map

- `ai_reviews` / `sim_ai_reviews` schema: `01b_SCHEMA_AND_DATA.md`, `M02_SCHEMA_SPEC.md` §3.19 / §4.9
- `human_action` enum: `01a_CORE_PRINCIPLES.md`
- Module 19 pipeline position: `01d_MODULES_AND_PIPELINE.md`
- Module 18 review-row write contract: `M18_EXPORT_PACKAGE_ENGINE_SPEC.md`
- `ServiceResult`: `app/utils/service_result.py`
- Env/config patterns: `app/config/env.py`, `app/config/settings.py`

## Tests Required

Create/update `tests/test_ai_review_engine.py`. Cover:

1. `ServiceResult` success/failure and `run_id` mint/preserve for all public methods.
2. Pre-DB validation before DB/AI access:
   - invalid roles
   - empty `ai_review_id`
   - invalid `human_action`
3. Row not found: failed; no AI call; no write.
4. Double-send guard: existing `ai_response_text` fails before AI call/write.
5. Successful ticker send:
   - writes `ai_response_text`
   - `response_chars` correct
   - `provider/model/prompt_version` echoed
6. Successful simulation send uses `sim_ai_reviews`.
7. AI call failure: failed; no DB write.
8. DB write failure after AI call: failed; response orphan path covered.
9. `record_human_action` success writes valid enum and exact metadata keys.
10. `record_human_action` blocked when `ai_response_text IS NULL`.
11. Static scans:
    - no direct `duckdb`
    - no market-data provider imports
    - no `print()`
    - no DDL/ATTACH
    - only `ai_reviews` / `sim_ai_reviews` are updated
    - no INSERT/DELETE in Module 19

Run:
```text
pytest -q tests/test_ai_review_engine.py
pytest -q
```

## Spec Required Content

`M19_AI_REVIEW_ENGINE_SPEC.md` must include:
- API
- role/id/action validation
- metadata key contracts
- send flow: read → guard → AI → write
- `record_human_action` flow
- AI client Protocol and lazy default client behavior
- failure semantics
- mutation boundaries
- assumptions/open gaps: `G-FORCE-RESEND`, `G-RESPONSE-ORPHAN`, `G-API-KEY-ENV`, `G-SDK-DEP`
- test summary

## Output

Return only:
1. Updated project zip.
2. Added/changed files.
3. Spec summary.
4. Short design notes.
5. Test commands/results.
6. Assumptions/open gaps.
7. Suggested commit message: `module19_ai_review_engine_stable`
