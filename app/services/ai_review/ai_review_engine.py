"""Module 19 — AI Review Engine.

Sends a previously-recorded manual review row (written by Module 18) to an AI
provider, records the AI response back onto the same row, and later records the
human's qualitative action on that review. It is a thin, UPDATE-only overlay on
top of the ``ai_reviews`` (ticker, ``prod`` / ``debug``) and ``sim_ai_reviews``
(simulation) tables.

Three public flows:

``AiReviewEngine.send_ticker_review``
    Read one ``ai_reviews`` row (``prod`` / ``debug``), call the injected AI
    client with the row's stored ``prompt_text`` / ``provider`` / ``model``, and
    write the response into ``ai_reviews.ai_response_text``.

``AiReviewEngine.send_simulation_review``
    The same flow against ``sim_ai_reviews`` (``simulation`` role only).

``AiReviewEngine.record_human_action``
    After a ticker review has an AI response, record the reviewer's
    ``human_action`` (``ignored`` / ``accepted`` / ``overrode`` / ``deferred``)
    into ``ai_reviews.human_action``.

Contract source of truth: ``M19_AI_REVIEW_ENGINE_SPEC.md`` (derived from
``01b_SCHEMA_AND_DATA.md`` / ``M02_SCHEMA_SPEC.md`` §3.19 / §4.9 for the
``ai_reviews`` / ``sim_ai_reviews`` schema, ``01a_CORE_PRINCIPLES.md`` for the
``human_action`` enum, ``M18_EXPORT_PACKAGE_ENGINE_SPEC.md`` for the review-row
write contract this module consumes, ``app/utils/service_result.py`` for the
``ServiceResult`` discipline, and ``app/config/env.py`` / ``app/config/settings.py``
for the lazy provider/key resolution patterns).

Send-time provider routing (2026-07-20 coder note): the ``provider`` recorded
on each review row by Module 18 is *what to record*, not necessarily *who is
called*. :class:`FallbackAiClient` — the default injected client — resolves an
ordered chain from ``ai_review.routing`` (primary ``gemini``, fallback
``anthropic``, then the row's own provider as a last resort) and tries each in
turn with the same prompt. :class:`GeminiClient` speaks the Gemini REST API
directly (no new SDK dependency); :class:`DefaultAiClient` still serves the
Anthropic/OpenAI legs unchanged. Which vendor actually answered is surfaced on
``ServiceResult.metadata`` as ``served_provider`` / ``served_model`` and in the
client's log lines — the ``ai_reviews`` schema is deliberately untouched, so
those are the only record. When every leg fails the client raises once and the
engine returns a single ``failed`` result with no DB write and no retry loop.

Hard boundaries (Module 19): no direct ``duckdb`` import, no market-data
provider imports or calls, no ``print()``, no DDL, and no ``ATTACH``. Every
database access is routed through the approved :mod:`app.database.duckdb_manager`
(or an injected ``db_manager``). The engine issues ``UPDATE`` statements only
(no ``INSERT`` / ``DELETE``) and mutates only ``ai_reviews.ai_response_text`` /
``ai_reviews.human_action`` / ``sim_ai_reviews.ai_response_text`` /
``sim_ai_reviews.human_action``. AI calls happen only inside an explicit
``send_*`` invocation, through the injectable :class:`AiClientProtocol`.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Final, Protocol, runtime_checkable

from app.config import env
from app.database import duckdb_manager
from app.services.config import default_configs
from app.utils import logging_config, service_result
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# Roles / constants.
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
DB_ROLE_SIMULATION: Final[str] = duckdb_manager.DB_ROLE_SIMULATION

TICKER_ALLOWED_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)
SIM_ALLOWED_ROLES: Final[tuple[str, ...]] = (DB_ROLE_SIMULATION,)

EXPORT_TYPE_TICKER: Final[str] = "ticker_review"
EXPORT_TYPE_SIM: Final[str] = "simulation_review"

REVIEW_TABLE_TICKER: Final[str] = "ai_reviews"
REVIEW_TABLE_SIM: Final[str] = "sim_ai_reviews"

# human_action enum (01a_CORE_PRINCIPLES.md / M02 §enums).
HUMAN_ACTIONS: Final[tuple[str, ...]] = (
    "ignored",
    "accepted",
    "overrode",
    "deferred",
)

STATUS_SUCCESS_META: Final[str] = "success"
STATUS_FAILED_META: Final[str] = "failed"

# Exact, stable error strings (asserted by tests; documented in the spec).
ERROR_ALREADY_SENT: Final[str] = (
    "review already sent (ai_response_text is not null); use force=True to override"
)
ERROR_ACTION_BEFORE_SEND: Final[str] = (
    "cannot record human action before AI send"
)

# Exact metadata key sets (returned on every path).
#
# provider / model are what the review ROW recorded (M18 write contract).
# served_provider / served_model are what actually answered the call after
# send-time routing/fallback (2026-07-20 coder note): they are None on every
# path that never reached a successful AI call, and they may differ from
# provider/model whenever the fallback chain moved off the recorded provider.
# The ai_reviews table has no column for this (schema untouched by design),
# so the ServiceResult metadata plus the client's log lines are the only
# record of which vendor was billed.
SEND_METADATA_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "export_type",
    "db_role",
    "ai_review_id",
    "review_kind",
    "provider",
    "model",
    "served_provider",
    "served_model",
    "prompt_version",
    "response_chars",
    "status",
    "error",
)
HUMAN_ACTION_METADATA_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "db_role",
    "ai_review_id",
    "human_action",
    "status",
    "error",
)

# Provider -> (env var holding the API key) for the default lazy client.
# G-API-KEY-ENV: resolved from app.config.env at call time, never at import.
_PROVIDER_ENV_KEY: Final[dict[str, str]] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# Keyring service name for credentials held in Windows Credential Manager
# (02_PROJECT_IMPLEMENTATION_CONTEXT.md §"keyring / Windows Credential Manager
# for API keys"). Every provider key is looked up env-first, keyring-second, so
# a test/CI environment variable always wins over the machine credential store
# and the existing env-only behavior of DefaultAiClient is unchanged.
KEYRING_SERVICE: Final[str] = "stock_ai_platform"

PROVIDER_GEMINI: Final[str] = "gemini"
PROVIDER_ANTHROPIC: Final[str] = "anthropic"
PROVIDER_OPENAI: Final[str] = "openai"

# M18 writes this provider on the single legacy review row when multi-pass is
# disabled. It means "a human will paste this prompt into a chat by hand" --
# it must never be auto-routed to a real vendor by the send-time chain.
PROVIDER_MANUAL: Final[str] = "manual"

GEMINI_API_KEY_NAME: Final[str] = "GEMINI_API_KEY"
GEMINI_ENDPOINT_TEMPLATE: Final[str] = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GEMINI_TIMEOUT_S: Final[float] = 60.0


def _resolve_api_key(env_var: str, keyring_key: str | None = None) -> str | None:
    """Return an API key from the environment, else from keyring; else None.

    Both lookups are lazy (call time only) and neither raises: a keyring
    backend that is missing, locked, or unavailable degrades to ``None`` so
    the caller can report a clean "missing key" failure rather than an opaque
    backend traceback.
    """
    env.load_environment()
    value = env.get_str(env_var)
    if value:
        return value

    try:
        import keyring  # noqa: PLC0415 - lazy by design (G-SDK-DEP)

        stored = keyring.get_password(KEYRING_SERVICE, keyring_key or env_var)
    except Exception:  # noqa: BLE001 - any backend failure means "no key"
        return None
    return stored or None

# --------------------------------------------------------------------------- #
# Structured pass-output parsing (Phase 3 — thesis/contrarian/audit).
#
# The thesis pass has no special structure requirement (freeform assessment,
# unchanged from today). The contrarian and audit passes are expected to
# return a JSON string in ai_response_text; these pure, no-I/O functions
# parse that JSON into the derived scores Step 5 consumes
# (contrarian_risk_score / audit_consistency_score). A malformed or
# unparseable response degrades to None ("no score available yet") rather
# than a hard penalty — an AI response that failed to follow the requested
# format is not necessarily evidence of a bad thesis, only of unusable
# output; Step 5 treats an absent score exactly like a pass that hasn't run.
# --------------------------------------------------------------------------- #
AUDIT_CLASSIFICATION_GROUNDED: Final[str] = "grounded"
AUDIT_CLASSIFICATION_SPECULATIVE: Final[str] = "speculative"
AUDIT_CLASSIFICATION_UNVERIFIABLE: Final[str] = "unverifiable"
ALLOWED_AUDIT_CLASSIFICATIONS: Final[tuple[str, ...]] = (
    AUDIT_CLASSIFICATION_GROUNDED,
    AUDIT_CLASSIFICATION_SPECULATIVE,
    AUDIT_CLASSIFICATION_UNVERIFIABLE,
)


def parse_audit_response(response_text: str | None) -> dict[str, Any] | None:
    """Parse an audit-pass ``ai_response_text`` into claim counts + score.

    Expected JSON shape::

        {"claims": [{"claim": str, "classification":
            "grounded" | "speculative" | "unverifiable"}, ...]}

    Returns ``None`` when ``response_text`` is falsy, not valid JSON, not a
    JSON object, has no non-empty ``"claims"`` list, or every claim has an
    unrecognized/missing classification (so ``total`` would be 0) — see the
    module-level note on why malformed output degrades to "absent" rather
    than a penalty.

    Otherwise returns::

        {"grounded": int, "speculative": int, "unverifiable": int,
         "total": int, "audit_consistency_score": float}  # 0-100

    ``audit_consistency_score = 100 * grounded / total``. Claims with an
    unrecognized classification are skipped entirely (not counted in any
    bucket, nor in ``total``).
    """
    if not response_text:
        return None
    try:
        parsed = json.loads(response_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    claims = parsed.get("claims")
    if not isinstance(claims, list) or not claims:
        return None

    counts = {k: 0 for k in ALLOWED_AUDIT_CLASSIFICATIONS}
    total = 0
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        classification = claim.get("classification")
        if classification not in counts:
            continue
        counts[classification] += 1
        total += 1
    if total == 0:
        return None

    return {
        **counts,
        "total": total,
        "audit_consistency_score": 100.0 * counts[AUDIT_CLASSIFICATION_GROUNDED] / total,
    }


def parse_contrarian_response(response_text: str | None) -> dict[str, Any] | None:
    """Parse a contrarian-pass ``ai_response_text`` into a 0-100 risk score.

    Expected JSON shape: ``{"risk_score": 0-100, "concerns": [str, ...]}``
    (``concerns`` optional). Returns ``None`` on falsy/unparseable text,
    non-object JSON, or a missing/non-numeric ``risk_score`` — same
    "absent, not penalized" degradation as :func:`parse_audit_response`.

    Otherwise returns ``{"risk_score": float, "concerns": list}``, with
    ``risk_score`` clamped to ``[0, 100]``.
    """
    if not response_text:
        return None
    try:
        parsed = json.loads(response_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    risk_score = parsed.get("risk_score")
    if not isinstance(risk_score, (int, float)) or isinstance(risk_score, bool):
        return None
    return {
        "risk_score": max(0.0, min(100.0, float(risk_score))),
        "concerns": parsed.get("concerns", []),
    }


# --------------------------------------------------------------------------- #
# Injected DB manager hook.
# --------------------------------------------------------------------------- #
class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# AI client protocol + default lazy client.
# --------------------------------------------------------------------------- #
@runtime_checkable
class AiClientProtocol(Protocol):
    """Injectable AI client contract.

    ``send`` returns ``(response_text, model_used)`` and raises on any failure
    (network, auth, SDK import, provider error). The engine treats any raised
    exception as an AI call failure and performs no DB write.
    """

    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]: ...


class _ValidationError(ValueError):
    """Raised internally for pre-DB validation failures."""


class DefaultAiClient:
    """Default :class:`AiClientProtocol` with fully lazy resolution.

    Resolution discipline (so importing this module never touches a provider
    SDK, env, or network):

    - ``provider`` / ``model`` are supplied by the engine from the review row;
      this client never hardcodes them.
    - The API key is read from :mod:`app.config.env` (``ANTHROPIC_API_KEY`` /
      ``OPENAI_API_KEY``; **G-API-KEY-ENV**) at call time.
    - The provider SDK is imported lazily inside :meth:`send`; a missing SDK is
      wrapped in a clear ``RuntimeError`` (**G-SDK-DEP** — no new SDK
      dependency is added by Module 19).
    """

    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]:
        provider_key = (provider or "").strip().lower()
        env_var = _PROVIDER_ENV_KEY.get(provider_key)
        if env_var is None:
            raise RuntimeError(
                f"DefaultAiClient: unsupported provider {provider!r}; "
                f"known providers: {sorted(_PROVIDER_ENV_KEY)}"
            )

        # Lazy env load + key lookup (call time only). env wins; keyring is a
        # second chance, so the pre-existing env-only behavior is unchanged
        # whenever the variable is set.
        api_key = _resolve_api_key(env_var)
        if not api_key:
            raise RuntimeError(
                f"DefaultAiClient: missing API key {env_var} for provider "
                f"{provider!r} (checked env and keyring service "
                f"{KEYRING_SERVICE!r}; G-API-KEY-ENV)"
            )

        if provider_key == "anthropic":
            return self._send_anthropic(prompt, model, api_key)
        return self._send_openai(prompt, model, api_key)

    @staticmethod
    def _send_anthropic(prompt: str, model: str, api_key: str) -> tuple[str, str]:
        try:
            import anthropic  # noqa: PLC0415 - lazy by design (G-SDK-DEP)
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise RuntimeError(
                "DefaultAiClient: the 'anthropic' SDK is not installed; "
                "Module 19 does not add SDK dependencies (G-SDK-DEP)"
            ) from exc

        client = anthropic.Anthropic(api_key=api_key)
        max_tokens = env.get_int("AI_REVIEW_MAX_TOKENS", 2048)
        message = client.messages.create(  # pragma: no cover - network
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            getattr(block, "text", "") for block in getattr(message, "content", [])
        )
        used = getattr(message, "model", model)
        return text, used

    @staticmethod
    def _send_openai(prompt: str, model: str, api_key: str) -> tuple[str, str]:
        try:
            import openai  # noqa: PLC0415 - lazy by design (G-SDK-DEP)
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise RuntimeError(
                "DefaultAiClient: the 'openai' SDK is not installed; "
                "Module 19 does not add SDK dependencies (G-SDK-DEP)"
            ) from exc

        client = openai.OpenAI(api_key=api_key)
        completion = client.chat.completions.create(  # pragma: no cover - network
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        text = completion.choices[0].message.content or ""
        used = getattr(completion, "model", model)
        return text, used


# --------------------------------------------------------------------------- #
# Gemini client (primary provider as of the 2026-07-20 routing coder note).
# --------------------------------------------------------------------------- #
# Deliberately raw HTTP against the REST endpoint rather than the
# ``google-genai`` SDK: G-SDK-DEP says Module 19 adds no SDK dependency, and
# ``google.genai`` is not installed in this environment. ``requests`` is
# already present (transitively, and used the same lazily-imported way by
# app/providers/edgar_provider.py and stooq_provider.py), and an injectable
# ``fetch`` seam keeps every test fully offline -- the same pattern
# edgar_insider_provider.py uses for its ``fetch_json``.
_FetchJson = Callable[[str, dict[str, str], str, float], tuple[int, str]]


def _default_gemini_fetch(
    url: str, headers: dict[str, str], body: str, timeout: float
) -> tuple[int, str]:  # pragma: no cover - network
    """POST ``body`` to ``url``; return ``(status_code, response_text)``.

    Never raises for a non-2xx status -- the status is returned so the caller
    can classify it (429 rate limit vs 4xx auth vs 5xx outage). Transport
    errors still propagate as exceptions, which the fallback chain treats
    identically to a bad status.
    """
    import requests  # noqa: PLC0415 - lazy by design (G-SDK-DEP)

    response = requests.post(url, headers=headers, data=body.encode("utf-8"),
                             timeout=timeout)
    return response.status_code, response.text


class GeminiClient:
    """:class:`AiClientProtocol` implementation for the Gemini REST API.

    Resolution is lazy in exactly the same way as :class:`DefaultAiClient`:
    the API key is read at call time (env ``GEMINI_API_KEY`` first, then
    keyring ``stock_ai_platform`` / ``GEMINI_API_KEY``), and the HTTP library
    is imported inside the injected default fetch.

    Every failure mode raises ``RuntimeError`` with a classified message so
    :class:`FallbackAiClient` can log *why* it fell through:

    - missing API key
    - transport failure (propagated from ``fetch``)
    - non-2xx status, with 429 called out as a rate limit
    - unparseable JSON body
    - well-formed JSON with no usable candidate text (includes a safety block
      or a ``MAX_TOKENS`` truncation that produced no text)
    """

    def __init__(self, fetch: _FetchJson | None = None) -> None:
        self._fetch: _FetchJson = fetch if fetch is not None else _default_gemini_fetch

    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]:
        api_key = _resolve_api_key(GEMINI_API_KEY_NAME)
        if not api_key:
            raise RuntimeError(
                f"GeminiClient: missing API key {GEMINI_API_KEY_NAME} (checked "
                f"env and keyring service {KEYRING_SERVICE!r})"
            )

        url = GEMINI_ENDPOINT_TEMPLATE.format(model=model)
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        body = json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": env.get_int("AI_REVIEW_MAX_TOKENS", 2048),
                },
            }
        )

        status, text = self._fetch(url, headers, body, GEMINI_TIMEOUT_S)
        if status == 429:
            raise RuntimeError(
                f"GeminiClient: rate limited (HTTP 429) for model {model!r}: "
                f"{text[:400]}"
            )
        if not 200 <= status < 300:
            raise RuntimeError(
                f"GeminiClient: HTTP {status} for model {model!r}: {text[:400]}"
            )

        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"GeminiClient: unparseable JSON response for model {model!r}: "
                f"{text[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"GeminiClient: expected a JSON object for model {model!r}, got "
                f"{type(payload).__name__}"
            )

        response_text = self._extract_text(payload)
        if not response_text:
            raise RuntimeError(
                f"GeminiClient: no candidate text in response for model "
                f"{model!r} (finish_reason="
                f"{self._first_finish_reason(payload)!r}; "
                f"prompt_feedback={payload.get('promptFeedback')!r})"
            )
        used = payload.get("modelVersion") or model
        return response_text, str(used)

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        """Concatenate every ``candidates[0].content.parts[*].text`` fragment."""
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        first = candidates[0]
        if not isinstance(first, dict):
            return ""
        content = first.get("content")
        if not isinstance(content, dict):
            return ""
        parts = content.get("parts")
        if not isinstance(parts, list):
            return ""
        return "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    @staticmethod
    def _first_finish_reason(payload: dict[str, Any]) -> str | None:
        candidates = payload.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            reason = candidates[0].get("finishReason")
            return reason if isinstance(reason, str) else None
        return None


# --------------------------------------------------------------------------- #
# Send-time provider routing + fallback.
# --------------------------------------------------------------------------- #
# Pacing state is module-level (not per-instance) because the rate limit is a
# property of the API key, not of whichever engine instance happens to hold a
# client. Keys are provider names; values are time.monotonic() stamps.
_LAST_CALL_MONOTONIC: dict[str, float] = {}


def _routing_config(ai_review_config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the ``ai_review.routing`` block, defaults filled in.

    Mirrors how Module 18 resolves the same ``ai_review`` runtime config:
    an explicit override wins, otherwise the seed defaults are used. Reading
    ``default_configs`` (a pure, DB-free module) keeps this side of the engine
    free of a ConfigService/DB dependency at client-construction time.
    """
    config = ai_review_config
    if config is None:
        config = default_configs.get_default_runtime_configs()["ai_review"]
    routing = config.get("routing")
    return routing if isinstance(routing, dict) else {}


class FallbackAiClient:
    """Route each send through a configured provider chain, in order.

    Chain construction (per call):

    1. ``routing.primary`` (default ``"gemini"``)
    2. ``routing.fallback`` (default ``"anthropic"``)
    3. the *row's own* provider, appended only if it is not already present

    Step 3 is what keeps the pre-existing behavior reachable: a contrarian row
    that Module 18 recorded as ``openai`` would otherwise be silently
    re-vendored to Gemini/Anthropic and never reach OpenAI at all. With it,
    the recorded provider remains the last resort rather than being dropped.

    Model selection per chain entry: when the entry *is* the row's provider the
    row's own ``model`` is used (it is the most specific instruction available);
    otherwise the entry falls back to ``routing.per_provider.<provider>.model``,
    because a model name is vendor-specific and cannot be handed across
    vendors.

    A row whose provider is ``"manual"`` (Module 18's single-row legacy export,
    written whenever ``multi_pass.enabled`` is False) raises immediately and
    calls nobody: "manual" means a human pastes the prompt into a chat, and
    auto-billing a vendor for it would be a silent behavior change.

    If every leg fails the client raises a single ``RuntimeError`` naming each
    leg and its error — the engine's existing contract turns that into a
    ``failed`` ServiceResult with no DB write, so there is no silent empty
    response and no unbounded retry.
    """

    def __init__(
        self,
        ai_review_config: dict[str, Any] | None = None,
        clients: dict[str, AiClientProtocol] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = ai_review_config
        default_client = DefaultAiClient()
        self._clients: dict[str, AiClientProtocol] = clients or {
            PROVIDER_GEMINI: GeminiClient(),
            PROVIDER_ANTHROPIC: default_client,
            PROVIDER_OPENAI: default_client,
        }
        self._sleep = sleep
        self._monotonic = monotonic
        # Observability for the caller (read synchronously by the engine right
        # after send returns). Reset at the start of every call.
        self.last_served_provider: str | None = None
        self.last_served_model: str | None = None
        self.last_attempts: list[tuple[str, str | None]] = []

    # ------------------------------------------------------------------ #
    # AiClientProtocol.
    # ------------------------------------------------------------------ #
    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]:
        log = logging_config.get_logger(__name__)
        self.last_served_provider = None
        self.last_served_model = None
        self.last_attempts = []

        row_provider = (provider or "").strip().lower()
        if row_provider == PROVIDER_MANUAL:
            raise RuntimeError(
                "FallbackAiClient: review row is a manual export "
                "(provider='manual'); enable ai_review.multi_pass to record a "
                "real provider before sending. No provider was called."
            )

        routing = _routing_config(self._config)
        per_provider = routing.get("per_provider")
        per_provider = per_provider if isinstance(per_provider, dict) else {}
        chain = self._build_chain(routing, row_provider)
        if not chain:
            raise RuntimeError(
                f"FallbackAiClient: empty provider chain for row provider "
                f"{provider!r}; check ai_review.routing."
            )

        errors: list[str] = []
        for position, chain_provider in enumerate(chain, start=1):
            client = self._clients.get(chain_provider)
            if client is None:
                message = f"no client registered for provider {chain_provider!r}"
                errors.append(f"{chain_provider}: {message}")
                self.last_attempts.append((chain_provider, message))
                log.warning("ai routing leg %s/%s %s", position, len(chain), message)
                continue

            provider_cfg = per_provider.get(chain_provider)
            provider_cfg = provider_cfg if isinstance(provider_cfg, dict) else {}
            call_model = (
                model
                if chain_provider == row_provider and model
                else provider_cfg.get("model")
            )
            if not call_model:
                message = (
                    f"no model configured (ai_review.routing.per_provider."
                    f"{chain_provider}.model)"
                )
                errors.append(f"{chain_provider}: {message}")
                self.last_attempts.append((chain_provider, message))
                log.warning("ai routing leg %s/%s %s", position, len(chain), message)
                continue

            self._pace(chain_provider, provider_cfg)
            try:
                response_text, model_used = client.send(
                    prompt, chain_provider, call_model
                )
            except Exception as exc:  # noqa: BLE001 - classify + try next leg
                message = f"{type(exc).__name__}: {exc}"
                errors.append(f"{chain_provider}: {message}")
                self.last_attempts.append((chain_provider, message))
                log.warning(
                    "ai routing leg %s/%s failed provider=%s model=%s: %s",
                    position, len(chain), chain_provider, call_model, message,
                )
                continue

            self.last_served_provider = chain_provider
            self.last_served_model = model_used or call_model
            self.last_attempts.append((chain_provider, None))
            if position == 1:
                log.info(
                    "ai call served by primary provider=%s model=%s "
                    "(row provider=%s)",
                    chain_provider, self.last_served_model, provider,
                )
            else:
                log.warning(
                    "ai call served by FALLBACK provider=%s model=%s "
                    "(leg %s/%s; row provider=%s; earlier failures: %s)",
                    chain_provider, self.last_served_model, position, len(chain),
                    provider, "; ".join(errors),
                )
            return response_text, self.last_served_model

        raise RuntimeError(
            "FallbackAiClient: all providers failed ("
            + " | ".join(errors)
            + ")"
        )

    # ------------------------------------------------------------------ #
    # Helpers.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_chain(routing: dict[str, Any], row_provider: str) -> list[str]:
        """Return the ordered, de-duplicated primary/fallback/row chain."""
        ordered = [
            str(routing.get("primary") or "").strip().lower(),
            str(routing.get("fallback") or "").strip().lower(),
            row_provider,
        ]
        chain: list[str] = []
        for candidate in ordered:
            if candidate and candidate != PROVIDER_MANUAL and candidate not in chain:
                chain.append(candidate)
        return chain

    def _pace(self, provider: str, provider_cfg: dict[str, Any]) -> None:
        """Sleep so successive calls to ``provider`` respect ``min_interval_s``."""
        try:
            min_interval = float(provider_cfg.get("min_interval_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            min_interval = 0.0
        now = self._monotonic()
        if min_interval > 0.0:
            previous = _LAST_CALL_MONOTONIC.get(provider)
            if previous is not None:
                wait = min_interval - (now - previous)
                if wait > 0.0:
                    self._sleep(wait)
                    now = self._monotonic()
        _LAST_CALL_MONOTONIC[provider] = now


# --------------------------------------------------------------------------- #
# AI review engine.
# --------------------------------------------------------------------------- #
class AiReviewEngine:
    """Send recorded review rows to an AI provider and record human actions.

    ``db_manager`` / ``ai_client`` are injected for testing; when ``None`` the
    approved :mod:`app.database.duckdb_manager` and :class:`FallbackAiClient`
    are used. The default client resolves config/env/SDK lazily at call time,
    never at import time.

    The default client is :class:`FallbackAiClient` (Gemini primary, Anthropic
    fallback, row provider last), which delegates to :class:`DefaultAiClient`
    for the Anthropic/OpenAI legs — so the pre-existing behavior is retained
    as a chain member, not replaced. Injecting a bare :class:`DefaultAiClient`
    restores the old single-provider behavior exactly.
    """

    def __init__(
        self,
        db_manager: _DbManagerLike | None = None,
        ai_client: AiClientProtocol | None = None,
    ) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )
        self._ai: AiClientProtocol = (
            ai_client if ai_client is not None else FallbackAiClient()
        )

    # ------------------------------------------------------------------ #
    # Public: send ticker review.
    # ------------------------------------------------------------------ #
    def send_ticker_review(
        self,
        ai_review_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Send a ticker ``ai_reviews`` row to the AI client and store the reply.

        ``db_role`` must be ``"prod"`` or ``"debug"``; ``ai_review_id`` must be
        non-empty. A fresh ``uuid4`` ``run_id`` is minted only when ``None``.
        """
        return self._send(
            ai_review_id=ai_review_id,
            db_role=db_role,
            run_id=run_id,
            allowed_roles=TICKER_ALLOWED_ROLES,
            table=REVIEW_TABLE_TICKER,
            export_type=EXPORT_TYPE_TICKER,
        )

    # ------------------------------------------------------------------ #
    # Public: send simulation review.
    # ------------------------------------------------------------------ #
    def send_simulation_review(
        self,
        ai_review_id: str,
        db_role: str = "simulation",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Send a ``sim_ai_reviews`` row to the AI client and store the reply.

        ``db_role`` must be ``"simulation"``; ``ai_review_id`` must be non-empty.
        """
        return self._send(
            ai_review_id=ai_review_id,
            db_role=db_role,
            run_id=run_id,
            allowed_roles=SIM_ALLOWED_ROLES,
            table=REVIEW_TABLE_SIM,
            export_type=EXPORT_TYPE_SIM,
        )

    # ------------------------------------------------------------------ #
    # Shared send flow (ticker / simulation differ only by table + role).
    # ------------------------------------------------------------------ #
    def _send(
        self,
        *,
        ai_review_id: str,
        db_role: str,
        run_id: str | None,
        allowed_roles: tuple[str, ...],
        table: str,
        export_type: str,
    ) -> ServiceResult:
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        # --- pre-DB / pre-AI validation. --------------------------------- #
        try:
            self._validate_send(db_role, ai_review_id, allowed_roles)
        except _ValidationError as exc:
            log.error("%s send validation failed: %s", export_type, exc)
            return self._send_failed(
                run_id, export_type, db_role, ai_review_id, str(exc),
            )

        # --- read the review row (read-only). ----------------------------- #
        try:
            row = self._read_review_row(db_role, table, ai_review_id)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("%s send %s", export_type, message)
            return self._send_failed(
                run_id, export_type, db_role, ai_review_id, message,
            )

        if row is None:
            message = f"review row not found: ai_review_id={ai_review_id!r}"
            log.error("%s send %s", export_type, message)
            return self._send_failed(
                run_id, export_type, db_role, ai_review_id, message,
            )

        provider = row["provider"]
        model = row["model"]
        prompt_version = row["prompt_version"]
        # review_kind (Phase 3): thesis/contrarian/audit, or None for a
        # legacy/single-row export. Each review_kind is its own row (its own
        # ai_review_id), so the double-send guard below is already per-row
        # by construction -- no guard logic change was needed for this.
        review_kind = row.get("review_kind")

        # --- double-send guard (no AI call, no write). -------------------- #
        if row["ai_response_text"] is not None:
            log.error("%s send blocked: %s", export_type, ERROR_ALREADY_SENT)
            return self._send_failed(
                run_id, export_type, db_role, ai_review_id, ERROR_ALREADY_SENT,
                provider=provider, model=model, prompt_version=prompt_version,
                review_kind=review_kind,
            )

        # --- AI call (failure => no DB write). ---------------------------- #
        # A routing client (FallbackAiClient) may serve this from a different
        # vendor than the row recorded; it raises only when EVERY leg failed,
        # so the failure path below is unchanged (one clean failed result, no
        # write, no retry loop).
        try:
            response_text, model_used = self._ai.send(
                row["prompt_text"], provider, model
            )
        except Exception as exc:  # noqa: BLE001 - any client failure is non-fatal
            message = f"ai call failed: {type(exc).__name__}: {exc}"
            log.error("%s send %s", export_type, message)
            return self._send_failed(
                run_id, export_type, db_role, ai_review_id, message,
                provider=provider, model=model, prompt_version=prompt_version,
                review_kind=review_kind,
            )

        # Which vendor actually answered. getattr keeps every non-routing
        # client (including a plain DefaultAiClient or an injected fake)
        # working unchanged — it simply reports the row's provider.
        served_provider = getattr(self._ai, "last_served_provider", None) or provider
        served_model = model_used or model
        if served_provider != provider:
            log.warning(
                "%s served by fallback provider=%s (row recorded provider=%s) "
                "ai_review_id=%s",
                export_type, served_provider, provider, ai_review_id,
            )

        response_chars = len(response_text)

        # --- single conditional UPDATE: ai_response_text only. ----------- #
        # Uses AND ai_response_text IS NULL so exactly one concurrent caller
        # wins. A return of 0 rows updated means a race was lost (G-RESPONSE-ORPHAN
        # for the AI response; G-FORCE-RESEND semantics for the guard).
        try:
            rows_updated = self._write_ai_response(
                db_role, table, ai_review_id, response_text
            )
        except Exception as exc:  # noqa: BLE001 - response orphaned on write fail
            message = (
                f"response write failed: {type(exc).__name__}: {exc} "
                f"(AI response obtained but not persisted; G-RESPONSE-ORPHAN)"
            )
            log.error("%s send %s", export_type, message)
            return self._send_failed(
                run_id, export_type, db_role, ai_review_id, message,
                provider=provider, model=model, prompt_version=prompt_version,
                response_chars=response_chars, review_kind=review_kind,
                served_provider=served_provider, served_model=served_model,
            )

        if rows_updated == 0:
            # Lost the TOCTOU race: another caller already wrote the response.
            log.warning(
                "%s send lost race (0 rows updated): ai_review_id=%s",
                export_type, ai_review_id,
            )
            return self._send_failed(
                run_id, export_type, db_role, ai_review_id, ERROR_ALREADY_SENT,
                provider=provider, model=model, prompt_version=prompt_version,
                response_chars=response_chars, review_kind=review_kind,
                served_provider=served_provider, served_model=served_model,
            )

        log.info(
            "%s send ok run_id=%s ai_review_id=%s chars=%s served_provider=%s "
            "served_model=%s",
            export_type, run_id, ai_review_id, response_chars,
            served_provider, served_model,
        )
        return self._send_success(
            run_id, export_type, db_role, ai_review_id,
            provider=provider, model=model, prompt_version=prompt_version,
            response_chars=response_chars, review_kind=review_kind,
            served_provider=served_provider, served_model=served_model,
        )

    # ------------------------------------------------------------------ #
    # Public: record human action.
    # ------------------------------------------------------------------ #
    def record_human_action(
        self,
        ai_review_id: str,
        human_action: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Record a reviewer's ``human_action`` on a ticker ``ai_reviews`` row.

        Targets ticker review rows only (``db_role`` ``"prod"`` / ``"debug"``).
        ``human_action`` must be one of :data:`HUMAN_ACTIONS`. The row must
        already have an AI response (``ai_response_text IS NOT NULL``).
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        # --- pre-DB validation. ------------------------------------------- #
        try:
            self._validate_human_action(db_role, ai_review_id, human_action)
        except _ValidationError as exc:
            log.error("record_human_action validation failed: %s", exc)
            return self._human_action_failed(
                run_id, db_role, ai_review_id, human_action, str(exc),
            )

        # --- read ticker review row (read-only). -------------------------- #
        try:
            row = self._read_review_row(
                db_role, REVIEW_TABLE_TICKER, ai_review_id
            )
        except Exception as exc:  # noqa: BLE001 - surface DB read failure
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("record_human_action %s", message)
            return self._human_action_failed(
                run_id, db_role, ai_review_id, human_action, message,
            )

        if row is None:
            message = f"review row not found: ai_review_id={ai_review_id!r}"
            log.error("record_human_action %s", message)
            return self._human_action_failed(
                run_id, db_role, ai_review_id, human_action, message,
            )

        if row["ai_response_text"] is None:
            log.error("record_human_action blocked: %s", ERROR_ACTION_BEFORE_SEND)
            return self._human_action_failed(
                run_id, db_role, ai_review_id, human_action,
                ERROR_ACTION_BEFORE_SEND,
            )

        # --- single UPDATE: human_action only. ---------------------------- #
        try:
            self._write_human_action(db_role, ai_review_id, human_action)
        except Exception as exc:  # noqa: BLE001 - surface DB write failure
            message = f"human_action write failed: {type(exc).__name__}: {exc}"
            log.error("record_human_action %s", message)
            return self._human_action_failed(
                run_id, db_role, ai_review_id, human_action, message,
            )

        log.info(
            "record_human_action ok run_id=%s ai_review_id=%s action=%s",
            run_id, ai_review_id, human_action,
        )
        return self._human_action_success(
            run_id, db_role, ai_review_id, human_action,
        )

    # ------------------------------------------------------------------ #
    # Validation (no I/O).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_send(
        db_role: str, ai_review_id: str, allowed_roles: tuple[str, ...]
    ) -> None:
        if db_role not in allowed_roles:
            raise _ValidationError(
                f"Unsupported db_role {db_role!r}; allowed: {list(allowed_roles)}."
            )
        if not ai_review_id:
            raise _ValidationError("ai_review_id must be non-empty.")

    @staticmethod
    def _validate_human_action(
        db_role: str, ai_review_id: str, human_action: str
    ) -> None:
        if db_role not in TICKER_ALLOWED_ROLES:
            raise _ValidationError(
                f"Unsupported db_role {db_role!r}; record_human_action targets "
                f"{list(TICKER_ALLOWED_ROLES)}."
            )
        if not ai_review_id:
            raise _ValidationError("ai_review_id must be non-empty.")
        if human_action not in HUMAN_ACTIONS:
            raise _ValidationError(
                f"Invalid human_action {human_action!r}; allowed: "
                f"{list(HUMAN_ACTIONS)}."
            )

    # ------------------------------------------------------------------ #
    # DB read / write (UPDATE-only).
    # ------------------------------------------------------------------ #
    def _read_review_row(
        self, db_role: str, table: str, ai_review_id: str
    ) -> dict[str, Any] | None:
        """Read one review row read-only; ``None`` when no row matches.

        ``table`` is a fixed internal constant; the SQL is fully literal per
        table (no interpolation) so the table name is visible to static scans.
        """
        connection = self._db.connect(db_role)
        try:
            if table == REVIEW_TABLE_SIM:
                # Pre-existing bug fix: sim_ai_reviews has no review_type
                # column (G-SIM-AI-SCHEMA) -- selecting it here would raise
                # against a real DuckDB connection (never caught before since
                # M19's test suite is entirely offline with a fake
                # connection). review_kind exists on both tables.
                cursor = connection.execute(
                    "SELECT ai_review_id, review_kind, prompt_text, "
                    "ai_response_text, provider, model, prompt_version "
                    "FROM sim_ai_reviews WHERE ai_review_id = ?",
                    [ai_review_id],
                )
            else:
                cursor = connection.execute(
                    "SELECT ai_review_id, review_type, review_kind, prompt_text, "
                    "ai_response_text, provider, model, prompt_version "
                    "FROM ai_reviews WHERE ai_review_id = ?",
                    [ai_review_id],
                )
            columns = [d[0] for d in cursor.description]
            fetched = cursor.fetchone()
            if fetched is None:
                return None
            return dict(zip(columns, fetched))
        finally:
            connection.close()

    def _write_ai_response(
        self, db_role: str, table: str, ai_review_id: str, response_text: str
    ) -> int:
        """Conditionally UPDATE ``ai_response_text`` only where still NULL.

        Returns 1 if the row was updated, 0 if it was already set by a
        concurrent caller (lost TOCTOU race). The ``RETURNING ai_review_id``
        clause is part of the same UPDATE statement — no second SQL call is
        issued. ``AND ai_response_text IS NULL`` ensures exactly one writer
        wins under concurrent calls (G-FORCE-RESEND / TOCTOU).
        """
        connection = self._db.connect(db_role)
        try:
            if table == REVIEW_TABLE_SIM:
                row = connection.execute(
                    "UPDATE sim_ai_reviews SET ai_response_text = ? "
                    "WHERE ai_review_id = ? AND ai_response_text IS NULL "
                    "RETURNING ai_review_id",
                    [response_text, ai_review_id],
                ).fetchone()
            else:
                row = connection.execute(
                    "UPDATE ai_reviews SET ai_response_text = ? "
                    "WHERE ai_review_id = ? AND ai_response_text IS NULL "
                    "RETURNING ai_review_id",
                    [response_text, ai_review_id],
                ).fetchone()
            return 1 if row is not None else 0
        finally:
            connection.close()

    def _write_human_action(
        self, db_role: str, ai_review_id: str, human_action: str
    ) -> None:
        """UPDATE only ``human_action`` on the row (ticker table only)."""
        connection = self._db.connect(db_role)
        try:
            connection.execute(
                "UPDATE ai_reviews SET human_action = ? WHERE ai_review_id = ?",
                [human_action, ai_review_id],
            )
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # Result builders — send.
    # ------------------------------------------------------------------ #
    def _send_success(
        self, run_id: str, export_type: str, db_role: str, ai_review_id: str,
        *, provider: str | None, model: str | None, prompt_version: str | None,
        response_chars: int | None, review_kind: str | None = None,
        served_provider: str | None = None, served_model: str | None = None,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata=self._send_metadata(
                run_id, export_type, db_role, ai_review_id, provider, model,
                prompt_version, response_chars, STATUS_SUCCESS_META, None,
                review_kind, served_provider, served_model,
            ),
        )

    def _send_failed(
        self, run_id: str, export_type: str, db_role: str, ai_review_id: str,
        message: str, *, provider: str | None = None, model: str | None = None,
        prompt_version: str | None = None, response_chars: int | None = None,
        review_kind: str | None = None, served_provider: str | None = None,
        served_model: str | None = None,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._send_metadata(
                run_id, export_type, db_role, ai_review_id, provider, model,
                prompt_version, response_chars, STATUS_FAILED_META, message,
                review_kind, served_provider, served_model,
            ),
        )

    @staticmethod
    def _send_metadata(
        run_id: str, export_type: str, db_role: str, ai_review_id: str,
        provider: str | None, model: str | None, prompt_version: str | None,
        response_chars: int | None, status: str, error: str | None,
        review_kind: str | None = None, served_provider: str | None = None,
        served_model: str | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "export_type": export_type,
            "db_role": db_role,
            "ai_review_id": ai_review_id,
            "review_kind": review_kind,
            "provider": provider,
            "model": model,
            "served_provider": served_provider,
            "served_model": served_model,
            "prompt_version": prompt_version,
            "response_chars": response_chars,
            "status": status,
            "error": error,
        }

    # ------------------------------------------------------------------ #
    # Result builders — human action.
    # ------------------------------------------------------------------ #
    def _human_action_success(
        self, run_id: str, db_role: str, ai_review_id: str, human_action: str,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata=self._human_action_metadata(
                run_id, db_role, ai_review_id, human_action,
                STATUS_SUCCESS_META, None,
            ),
        )

    def _human_action_failed(
        self, run_id: str, db_role: str, ai_review_id: str,
        human_action: str | None, message: str,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._human_action_metadata(
                run_id, db_role, ai_review_id, human_action,
                STATUS_FAILED_META, message,
            ),
        )

    @staticmethod
    def _human_action_metadata(
        run_id: str, db_role: str, ai_review_id: str, human_action: str | None,
        status: str, error: str | None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "db_role": db_role,
            "ai_review_id": ai_review_id,
            "human_action": human_action,
            "status": status,
            "error": error,
        }
