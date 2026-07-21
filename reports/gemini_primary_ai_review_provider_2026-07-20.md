# Gemini as Primary AI Review Provider, Claude as Fallback — Implementation Report

**Date:** 2026-07-20 (delivered 2026-07-21)
**Scope:** Provider layer inside `app/services/ai_review/ai_review_engine.py` only.
**Status:** Implemented, tested, **not committed** (per policy).

---

## 0. Headline anomalies (read first)

Four of the coder note's premises did not survive verification. Two of them change what was
delivered; one is an operational blocker the architect must act on.

| # | Note's premise | Reality | Impact |
|---|---|---|---|
| A | "Use a Flash-tier model (e.g. `gemini-2.5-flash`)" | **`gemini-2.5-flash` returns HTTP 404 for this project's key** — "no longer available to new users". So does `gemini-2.5-flash-lite`. Verified by live call. | **Default changed to `gemini-3.5-flash`.** Had the note been followed literally, every Gemini call would have 404'd and silently fallen through to a provider that has no key (see C) — i.e. AI review would have been 100% broken. |
| B | "Gemini API key stored via `keyring`… **same pattern already used for `FMP_API_KEY`**" | **There is no such pattern in the codebase.** `keyring` is imported **zero** times anywhere in `app/`, `tools/`, or `tests/`. The FMP module that used it (`fmp_insider_provider.py`) was **deleted** and replaced by `edgar_insider_provider.py` (SEC EDGAR, no API key) in commit `536a833`. | No pattern to copy; the keyring integration here is the codebase's **first**. Written fresh, env-first/keyring-second. |
| C | "the existing Anthropic client becomes the fallback" | **No Anthropic API key exists** — not in env, not in keyring (`ANTHROPIC_API_KEY` → `None` in both). Verified live. | **The fallback leg is currently non-functional.** The chain degrades cleanly (one error, no DB write), but there is effectively no fallback until a key is provisioned. **Architect action required.** |
| D | "15 RPM for Flash… pacing unlikely to be needed" | Free-tier Flash is **~10 RPM / 1,500 RPD**, not 15 (15 RPM is `gemini-3.1-flash-lite` only). Google **no longer publishes exact numbers** in the public docs — they are per-project in AI Studio. At 10 RPM, a 60-call batch bursts past the limit in the first minute. | **Pacing was needed and was implemented** (6.0s min interval = 10 RPM). |

Also confirmed: the "Claude Code as fallback" reading in the note **is correct** — this is a normal
Anthropic API call through the existing `DefaultAiClient`, not a subprocess invocation of the
`claude` CLI. No code path shells out.

---

## 1. Investigation results (note items 1–3)

### 1.1 Which providers are wired today

**Anthropic *and* OpenAI — both already exist.** `DefaultAiClient.send` dispatches on a
`provider` string against `_PROVIDER_ENV_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai":
"OPENAI_API_KEY"}`, with `_send_anthropic` / `_send_openai` methods. Both SDKs are imported
lazily; neither is installed.

**How the provider is selected is the load-bearing detail the note did not anticipate:** it is
**not** a config lookup at send time. It is **row data**. M18 (`export_package_engine.py`) writes
`ai_reviews.provider` / `.model` per row from `ai_review.multi_pass.<kind>.{provider,model}`;
M19 reads that row back and hands the stored strings to the client. Seeded values:

| pass | provider | model |
|---|---|---|
| thesis | anthropic | claude-sonnet-5 |
| contrarian | **openai** | gpt-4o |
| audit | anthropic | claude-haiku-4-5 |

**But `multi_pass.enabled` is `False`.** With the shipped default config, every row is written
`provider="manual"`, `model="none"` — and `DefaultAiClient` raises `unsupported provider 'manual'`
on those. **So the send path today cannot make a successful call at all**; M19 is a
manual-copy-paste export in practice. This is the actual pre-change baseline.

### 1.2 `AiClientProtocol` shape

```python
def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]: ...   # (response_text, model_used)
```

Unchanged — `GeminiClient` and `FallbackAiClient` both conform. Note the engine previously
**discarded** the second element (`response_text, _model_used = ...`); it is now used.

### 1.3 How failures surface

Any exception from `send` is caught by a blanket `except Exception` in `AiReviewEngine._send`,
producing `"ai call failed: {Type}: {msg}"`, `status=failed`, `rows_processed=0`, and **no DB
write**. There is no retry and no partial state.

**Consequence for the design:** the engine treats *any* raise as terminal, so fallback logic
**cannot** live in the engine — it must live inside a client that only raises once every leg is
exhausted. That is exactly what `FallbackAiClient` does.

---

## 2. What was implemented

### 2.1 `GeminiClient` — raw REST, no new SDK

Deliberately **not** the `google-genai` SDK: `G-SDK-DEP` says M19 adds no SDK dependency, and
`google.genai` is not installed. Instead a direct POST to
`https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with an
`x-goog-api-key` header, through an **injectable `fetch` seam** — the same pattern
`edgar_insider_provider.py` uses for `fetch_json`. `requests` is imported lazily inside the
default fetch (already present transitively; same lazy usage as `edgar_provider.py` /
`stooq_provider.py`). This also makes every test offline, which is what the note's "fake HTTP
responses" ask implies.

Each failure mode raises a *classified* `RuntimeError` so the chain can log why it fell through:
missing key · transport error · **429 rate limit (called out separately)** · other non-2xx ·
unparseable JSON · non-object JSON · no usable candidate text (covers safety blocks and
`MAX_TOKENS` truncation).

### 2.2 `FallbackAiClient` — the routing chain

Per call, builds an ordered, de-duplicated chain:

1. `routing.primary` — default `"gemini"`
2. `routing.fallback` — default `"anthropic"`
3. **the row's own provider**, appended only if not already present

**Step 3 is an addition beyond the note, and it is deliberate — flagging it explicitly.** Without
it, a contrarian row that M18 recorded as `openai` would be silently re-vendored to
Gemini/Anthropic and OpenAI would become unreachable dead code — which the note's "do not remove
the existing OpenAI client code" instruction argues against. With it, the recorded provider
survives as a last resort. Easy to drop if unwanted.

**Model selection per leg:** if the leg *is* the row's provider, the row's own `model` wins (most
specific instruction). Otherwise the leg uses `routing.per_provider.<provider>.model` — a model
name is vendor-specific and cannot be handed across vendors (Gemini must never receive
`claude-sonnet-5`).

**`provider="manual"` raises immediately and calls nobody.** "Manual" means a human pastes the
prompt into a chat; auto-billing a vendor for it would be a silent behavior change. This preserves
today's baseline exactly.

**All legs failed → one `RuntimeError` naming every leg and its error.** Exactly one attempt per
leg; no retry loop, no empty-string return. The engine's existing contract turns it into a single
`failed` ServiceResult with no DB write — note item 3 satisfied.

**Pacing:** `_pace()` sleeps so successive calls to the same provider respect
`min_interval_s`. State is **module-level, keyed by provider**, because the rate limit belongs to
the API key, not to whichever engine instance holds a client. Clock and sleep are injectable.

### 2.3 Config knob (not hardcoded — note item 4)

`default_configs.DEFAULT_RUNTIME_CONFIGS["ai_review"]["routing"]`:

```python
"routing": {
    "primary": "gemini",
    "fallback": "anthropic",
    "per_provider": {
        "gemini":    {"model": "gemini-3.5-flash",  "min_interval_s": 6.0},
        "anthropic": {"model": "claude-sonnet-5",   "min_interval_s": 0.0},
        "openai":    {"model": "gpt-4o",            "min_interval_s": 0.0},
    },
},
```

Resolved the same way M18 resolves its `ai_review` config (explicit override wins, else seed
defaults from the pure, DB-free `default_configs` module). A test asserts the order can be
inverted purely by config with no code edit.

### 2.4 Key resolution — env first, keyring second

New shared `_resolve_api_key(env_var, keyring_key)`: `os.environ` first, then
`keyring.get_password("stock_ai_platform", ...)`, never raising (a missing/locked backend degrades
to `None` → clean "missing key" error, not an opaque traceback).

**Applied to all three providers, including the existing Anthropic/OpenAI paths.** This is a small
additive change to `DefaultAiClient` — flagging it, since the note didn't ask for it. It is
strictly a *second chance*: when the env var is set, behavior is byte-identical to before. It
avoids an inconsistency where Gemini reads keyring but Anthropic could only read env — which would
have made provisioning the fallback key (issue C) unnecessarily confusing.

### 2.5 "Which provider served this call" (note item 2)

The note requires this be visible, and simultaneously forbids touching the `ai_reviews` schema.
**There is no column for it** — so the record lives in the two places that remain:

- **`ServiceResult.metadata`** gains `served_provider` and `served_model`, alongside the existing
  `provider` / `model` (which continue to report what the *row* recorded). They differ whenever
  the chain moved off the recorded provider.
- **Log lines**: `INFO` when the primary serves; `WARNING` naming the fallback provider, its leg
  position, and every earlier failure when it doesn't. The engine logs a second `WARNING` when
  `served_provider != provider`.

The engine reads this via `getattr(self._ai, "last_served_provider", None)`, so **every existing
non-routing client keeps working unchanged** (a plain `DefaultAiClient` or an injected test fake
simply reports the row's provider). Tested.

`SEND_METADATA_KEYS` is a `Final` tuple asserted exactly by the existing test suite — but that
test reads the constant dynamically, so extending it required no test edit and broke nothing.

**Design caveat, flagged:** `last_served_provider` is per-instance mutable state, read
synchronously immediately after `send` returns. Correct for this codebase (calls are sequential;
the dashboard sends one review at a time), but it would be wrong under concurrent sends sharing one
client. A schema column would be the durable fix, and the note explicitly ruled that out.

---

## 3. Live verification (real API, real key)

Run against this project's actual keyring credential (`stock_ai_platform` / `GEMINI_API_KEY`,
present, 53 chars).

**Model availability — the critical finding:**

| model | result |
|---|---|
| `gemini-2.5-flash` | ❌ **HTTP 404** — "no longer available to new users" |
| `gemini-2.5-flash-lite` | ❌ **HTTP 404** — same |
| **`gemini-3.5-flash`** | ✅ **OK** ← new default |
| `gemini-3.1-flash-lite` | ✅ OK |
| `gemini-3-flash-preview` | ✅ OK |

Note the trap: the `ListModels` endpoint **still advertises `gemini-2.5-flash` to this key**, and
so does the public docs page — but `generateContent` 404s on it. Model listings are not
authoritative; only a live generate call is. This is why the note's "confirm the model name against
the docs" instruction was insufficient on its own.

**End-to-end chain:**

- **Primary success** — row recorded `anthropic`/`claude-sonnet-5`, chain served it from Gemini:
  `served_provider=gemini`, `served_model=gemini-3.5-flash`, one attempt, correct response text.
- **Both legs fail** — forced a bad Gemini model, then hit Anthropic:
  ```
  WARNING  ai routing leg 1/2 failed provider=gemini model=gemini-BOGUS: RuntimeError: HTTP 400 …
  WARNING  ai routing leg 2/2 failed provider=anthropic model=claude-sonnet-5: RuntimeError:
           DefaultAiClient: missing API key ANTHROPIC_API_KEY … (checked env and keyring)
  RuntimeError: FallbackAiClient: all providers failed (gemini: … | anthropic: …)
  ```
  Clean single failure naming both legs — and incidental live proof of issue **C**.

---

## 4. Rate limits (note item 4 — checked against live docs)

- Google **removed per-model RPM/TPM/RPD from the public rate-limits page**; it now redirects to
  the per-project AI Studio dashboard. There is no longer an authoritative public number.
- Best current published figures for free-tier Flash: **10 RPM · 250,000 TPM · 1,500 RPD**
  (`gemini-3-flash` / `gemini-2.5-flash`). **15 RPM applies only to `gemini-3.1-flash-lite`** — the
  note's assumed 15 RPM is 50% too generous for the model actually being used.
- **RPD is ample** (1,500 vs ~60/day needed). **RPM is not**: 60 calls issued back-to-back would
  breach 10 RPM within the first minute, so the note's "unlikely to need pacing" is wrong.
  `min_interval_s: 6.0` (= 10 RPM) is set as a conservative floor; 60 calls then take ~6 minutes.
- **Recommendation:** confirm the real limits for `gemini-3.5-flash` in AI Studio for this specific
  project and tune `min_interval_s` from observed values.

Sources: [Rate limits | Gemini API](https://ai.google.dev/gemini-api/docs/rate-limits) ·
[Gemini models](https://ai.google.dev/gemini-api/docs/models) ·
[Gemini API free-tier limits (TokenMix)](https://tokenmix.ai/blog/gemini-api-free-tier-limits) ·
[Gemini API rate limits per tier (AI Free API)](https://www.aifreeapi.com/en/posts/gemini-api-rate-limits-per-tier)

---

## 5. Tests

**New: `tests/test_ai_review_gemini_fallback.py` — 43 tests, all passing, fully offline.**

| Group | Covers |
|---|---|
| GeminiClient success (4) | text/model extraction, request shape (URL, headers, body, timeout), multi-part concatenation, `modelVersion` fallback |
| GeminiClient failures (13) | missing key · 429 rate limit · 400/401/403/404/500/503 · malformed JSON · non-object JSON · 5 "no usable text" shapes incl. safety block · transport exception |
| Routing (10) | primary success skips fallback · **Gemini failure → Claude fallback, same prompt** · cross-vendor model isolation · row provider as last resort · dedup · manual-row calls nobody · **all-fail raises once, one attempt each** · unregistered provider skipped · missing model skipped · **config can invert primary/fallback** · empty chain |
| Degradation (1) | **missing Gemini key → clean fall-through to Anthropic** (note item 5) |
| Pacing (2) | 6.0s enforced between Gemini calls; `0.0` provider not paced |
| Engine integration (5) | metadata keys · **primary served** · **fallback served** · **both fail = clean failure, nothing written** · non-routing client unchanged |
| Config + boundary (4) | routing config shape · **no top-level `requests`/`keyring`/`google`/SDK imports** · endpoint host · default client is the routing client |

**Regression runs:**

- `test_ai_review_engine.py` (existing M19 suite, 72 tests) + M18 + config + dashboard-actions +
  orchestrator-config: **205 passed, 31 skipped** (all skips pre-existing `PENDING Phase 5/6
  migration`).
- **Full suite: 3 failures, all pre-existing and unrelated** — verified by inspecting each:
  - `test_data_validator.py::test_spec_documents_open_gaps_not_invented` — looks for
    `M09_DATA_VALIDATOR_SPEC.md` at repo root; it lives in `specs/`.
  - `test_mutation_detector.py::test_spec_documents_open_gap_g1` — same root-vs-`specs/` path bug.
  - `test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance` — known
    `edgar_provider.py` / yfinance overlap.

  These are the exact three logged as out-of-scope on 2026-07-08. None touch M19, M18, or config.

---

## 6. Compliance with "what NOT to do"

| Constraint | Status |
|---|---|
| Don't change `auto_invoke_ai_review` | ✅ still `False`, untouched |
| Don't restructure per-export vs per-ticker calls | ✅ untouched |
| Don't touch `ai_reviews` schema | ✅ **zero** schema changes (this is why `served_provider` had to go in metadata/logs) |
| Don't remove Anthropic/OpenAI client code | ✅ `DefaultAiClient` intact and now serves both fallback legs |

M19's hard boundaries also hold: no `duckdb` import, no provider imports, no `print()`, no DDL, no
`ATTACH`, UPDATE-only SQL on the same two columns. All new imports (`requests`, `keyring`) are
lazy, enforced by a new AST test.

---

## 7. Files changed

```
 app/services/ai_review/__init__.py          |  10 +-      export new clients
 app/services/ai_review/ai_review_engine.py  | 480 ++++-   GeminiClient, FallbackAiClient,
                                                            _resolve_api_key, served_* metadata
 app/services/config/default_configs.py      |  36 +       ai_review.routing block
 tests/test_ai_review_gemini_fallback.py     | NEW         43 tests
```

**Not committed.**

---

## 8. Architect decisions needed

1. **Provision an Anthropic API key** (issue C) — the fallback is decorative until
   `ANTHROPIC_API_KEY` exists in env or keyring (`stock_ai_platform`). Until then a Gemini outage
   means AI review fails outright rather than falling back. *Highest priority.*
2. **Ratify `gemini-3.5-flash`** as the default (issue A), or pick `gemini-3.1-flash-lite`
   (cheaper, 15 RPM, weaker) — config-only change either way.
3. **Confirm the row-provider-as-last-resort chain leg** (§2.2) is wanted, or say so and it comes out.
4. **Confirm the `_resolve_api_key` keyring extension to Anthropic/OpenAI** (§2.4) is wanted, or it
   reverts to Gemini-only.
5. **Note the unchanged blocker:** with `multi_pass.enabled = False`, every row is still written
   `provider="manual"` and nothing sends at all. **None of this routing work takes effect until
   `multi_pass.enabled` is turned on** — which is a separate decision the note explicitly did not
   authorize, and which interacts with the standing `auto_invoke_ai_review` cost guard in CLAUDE.md.
