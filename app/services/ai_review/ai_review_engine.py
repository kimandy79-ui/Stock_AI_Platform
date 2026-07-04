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
import uuid
from typing import Any, Final, Protocol, runtime_checkable

from app.config import env
from app.database import duckdb_manager
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
SEND_METADATA_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "export_type",
    "db_role",
    "ai_review_id",
    "review_kind",
    "provider",
    "model",
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

        # Lazy env load + key lookup (call time only).
        env.load_environment()
        api_key = env.get_str(env_var)
        if not api_key:
            raise RuntimeError(
                f"DefaultAiClient: missing API key env var {env_var} for "
                f"provider {provider!r} (G-API-KEY-ENV)"
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
# AI review engine.
# --------------------------------------------------------------------------- #
class AiReviewEngine:
    """Send recorded review rows to an AI provider and record human actions.

    ``db_manager`` / ``ai_client`` are injected for testing; when ``None`` the
    approved :mod:`app.database.duckdb_manager` and :class:`DefaultAiClient` are
    used. The default client resolves config/env/SDK lazily at call time, never
    at import time.
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
            ai_client if ai_client is not None else DefaultAiClient()
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
        try:
            response_text, _model_used = self._ai.send(
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
            )

        log.info(
            "%s send ok run_id=%s ai_review_id=%s chars=%s",
            export_type, run_id, ai_review_id, response_chars,
        )
        return self._send_success(
            run_id, export_type, db_role, ai_review_id,
            provider=provider, model=model, prompt_version=prompt_version,
            response_chars=response_chars, review_kind=review_kind,
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
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata=self._send_metadata(
                run_id, export_type, db_role, ai_review_id, provider, model,
                prompt_version, response_chars, STATUS_SUCCESS_META, None,
                review_kind,
            ),
        )

    def _send_failed(
        self, run_id: str, export_type: str, db_role: str, ai_review_id: str,
        message: str, *, provider: str | None = None, model: str | None = None,
        prompt_version: str | None = None, response_chars: int | None = None,
        review_kind: str | None = None,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._send_metadata(
                run_id, export_type, db_role, ai_review_id, provider, model,
                prompt_version, response_chars, STATUS_FAILED_META, message,
                review_kind,
            ),
        )

    @staticmethod
    def _send_metadata(
        run_id: str, export_type: str, db_role: str, ai_review_id: str,
        provider: str | None, model: str | None, prompt_version: str | None,
        response_chars: int | None, status: str, error: str | None,
        review_kind: str | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "export_type": export_type,
            "db_role": db_role,
            "ai_review_id": ai_review_id,
            "review_kind": review_kind,
            "provider": provider,
            "model": model,
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
