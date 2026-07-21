"""Tests for Module 19 send-time provider routing (Gemini primary / Claude fallback).

Covers the 2026-07-20 coder note: ``GeminiClient`` (raw REST, no SDK),
``FallbackAiClient`` (configured primary -> fallback -> row provider chain),
and the ``served_provider`` / ``served_model`` observability the engine now
returns.

Every test is fully offline: the Gemini HTTP call goes through an injected
``fetch`` that returns canned ``(status, body)`` tuples, no keyring backend is
touched (the key is supplied via monkeypatched env), and no DuckDB file is
created. ``time.sleep`` is always injected so pacing never actually blocks.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.services.ai_review import ai_review_engine as are
from app.services.ai_review.ai_review_engine import (
    AiReviewEngine,
    FallbackAiClient,
    GeminiClient,
)
from app.services.config import default_configs
from app.utils import service_result

MODULE_PATH = Path(are.__file__)


# --------------------------------------------------------------------------- #
# Helpers: canned Gemini payloads.
# --------------------------------------------------------------------------- #
def _gemini_ok_body(text: str = "assessment text", model: str = "gemini-3.5-flash") -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": "STOP",
                }
            ],
            "modelVersion": model,
        }
    )


def _fetch_returning(status: int, body: str):
    """Build a fetch that records its calls and returns a canned response."""
    calls: list[tuple[str, dict[str, str], str, float]] = []

    def fetch(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return status, body

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


@pytest.fixture
def gemini_key(monkeypatch):
    """Supply GEMINI_API_KEY via env so keyring is never consulted."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    return "test-gemini-key"


def _no_sleep_client(**kwargs) -> FallbackAiClient:
    """FallbackAiClient with pacing stubbed to a monotonic clock we control."""
    ticks = {"t": 0.0}

    def monotonic() -> float:
        return ticks["t"]

    def sleep(seconds: float) -> None:
        ticks["t"] += seconds

    client = FallbackAiClient(sleep=sleep, monotonic=monotonic, **kwargs)
    client._test_ticks = ticks  # type: ignore[attr-defined]
    return client


class _StubClient:
    """Minimal AiClientProtocol stub: succeeds, or raises a chosen error."""

    def __init__(self, response: str | None = "ok", error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]:
        self.calls.append((prompt, provider, model))
        if self.error is not None:
            raise self.error
        return self.response, f"{model}-resolved"


# =========================================================================== #
# 1. GeminiClient — success path.
# =========================================================================== #
def test_gemini_success_returns_text_and_model(gemini_key):
    fetch = _fetch_returning(200, _gemini_ok_body("proceed with caution"))
    text, model_used = GeminiClient(fetch=fetch).send(
        "prompt body", "gemini", "gemini-3.5-flash"
    )
    assert text == "proceed with caution"
    assert model_used == "gemini-3.5-flash"


def test_gemini_request_shape(gemini_key):
    fetch = _fetch_returning(200, _gemini_ok_body())
    GeminiClient(fetch=fetch).send("prompt body", "gemini", "gemini-3.5-flash")

    url, headers, payload, timeout = fetch.calls[0]
    assert url.endswith("/models/gemini-3.5-flash:generateContent")
    assert url.startswith("https://generativelanguage.googleapis.com/")
    assert headers["x-goog-api-key"] == "test-gemini-key"
    assert headers["Content-Type"] == "application/json"
    assert timeout == are.GEMINI_TIMEOUT_S

    body = json.loads(payload)
    assert body["contents"][0]["parts"][0]["text"] == "prompt body"
    assert body["generationConfig"]["maxOutputTokens"] > 0


def test_gemini_concatenates_multiple_parts(gemini_key):
    body = json.dumps(
        {
            "candidates": [
                {"content": {"parts": [{"text": "part-a "}, {"text": "part-b"}]}}
            ]
        }
    )
    text, _ = GeminiClient(fetch=_fetch_returning(200, body)).send(
        "p", "gemini", "gemini-3.5-flash"
    )
    assert text == "part-a part-b"


def test_gemini_falls_back_to_requested_model_when_no_modelversion(gemini_key):
    body = json.dumps({"candidates": [{"content": {"parts": [{"text": "x"}]}}]})
    _, model_used = GeminiClient(fetch=_fetch_returning(200, body)).send(
        "p", "gemini", "gemini-3.5-flash"
    )
    assert model_used == "gemini-3.5-flash"


# =========================================================================== #
# 2. GeminiClient — failure modes (each must raise, never return junk).
# =========================================================================== #
def test_gemini_missing_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Force the keyring leg to report "no credential" without touching a backend.
    monkeypatch.setattr(are, "_resolve_api_key", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="missing API key GEMINI_API_KEY"):
        GeminiClient(fetch=_fetch_returning(200, _gemini_ok_body())).send(
            "p", "gemini", "gemini-3.5-flash"
        )


def test_gemini_rate_limit_429_raises_classified(gemini_key):
    fetch = _fetch_returning(429, '{"error": {"message": "Quota exceeded"}}')
    with pytest.raises(RuntimeError, match="rate limited \\(HTTP 429\\)"):
        GeminiClient(fetch=fetch).send("p", "gemini", "gemini-3.5-flash")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
def test_gemini_non_2xx_raises(gemini_key, status):
    fetch = _fetch_returning(status, '{"error": "nope"}')
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        GeminiClient(fetch=fetch).send("p", "gemini", "gemini-3.5-flash")


def test_gemini_malformed_json_raises(gemini_key):
    fetch = _fetch_returning(200, "<html>502 Bad Gateway</html>")
    with pytest.raises(RuntimeError, match="unparseable JSON response"):
        GeminiClient(fetch=fetch).send("p", "gemini", "gemini-3.5-flash")


def test_gemini_non_object_json_raises(gemini_key):
    fetch = _fetch_returning(200, "[1, 2, 3]")
    with pytest.raises(RuntimeError, match="expected a JSON object"):
        GeminiClient(fetch=fetch).send("p", "gemini", "gemini-3.5-flash")


@pytest.mark.parametrize(
    "body",
    [
        "{}",                                                    # no candidates
        '{"candidates": []}',                                    # empty candidates
        '{"candidates": [{"finishReason": "SAFETY"}]}',          # blocked, no content
        '{"candidates": [{"content": {"parts": []}}]}',          # no parts
        '{"candidates": [{"content": {"parts": [{"x": 1}]}}]}',  # parts without text
    ],
)
def test_gemini_no_usable_text_raises(gemini_key, body):
    with pytest.raises(RuntimeError, match="no candidate text"):
        GeminiClient(fetch=_fetch_returning(200, body)).send(
            "p", "gemini", "gemini-3.5-flash"
        )


def test_gemini_transport_exception_propagates(gemini_key):
    def exploding_fetch(url, headers, payload, timeout):
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError):
        GeminiClient(fetch=exploding_fetch).send("p", "gemini", "gemini-3.5-flash")


# =========================================================================== #
# 3. FallbackAiClient — chain construction + routing.
# =========================================================================== #
def test_primary_success_never_calls_fallback():
    primary, fallback = _StubClient("from gemini"), _StubClient("from claude")
    client = _no_sleep_client(
        clients={"gemini": primary, "anthropic": fallback},
    )
    text, model_used = client.send("prompt", "anthropic", "claude-sonnet-5")

    assert text == "from gemini"
    assert client.last_served_provider == "gemini"
    assert model_used == client.last_served_model
    assert len(primary.calls) == 1
    assert fallback.calls == []


def test_gemini_failure_triggers_claude_fallback_with_same_prompt():
    primary = _StubClient(error=RuntimeError("GeminiClient: HTTP 429"))
    fallback = _StubClient("from claude")
    client = _no_sleep_client(clients={"gemini": primary, "anthropic": fallback})

    text, _ = client.send("the exact prompt", "anthropic", "claude-sonnet-5")

    assert text == "from claude"
    assert client.last_served_provider == "anthropic"
    # Same prompt handed to both legs.
    assert primary.calls[0][0] == "the exact prompt"
    assert fallback.calls[0][0] == "the exact prompt"
    # Fallback used the ROW's model, since anthropic is the row's provider.
    assert fallback.calls[0][2] == "claude-sonnet-5"


def test_non_row_provider_leg_uses_configured_model_not_row_model():
    """Gemini must never be handed a Claude model name."""
    primary, fallback = _StubClient("g"), _StubClient("c")
    client = _no_sleep_client(clients={"gemini": primary, "anthropic": fallback})
    client.send("p", "anthropic", "claude-sonnet-5")

    assert primary.calls[0][2] == "gemini-3.5-flash"
    assert primary.calls[0][2] != "claude-sonnet-5"


def test_row_provider_appended_as_last_resort():
    """An openai contrarian row stays reachable behind gemini+anthropic."""
    gemini = _StubClient(error=RuntimeError("gemini down"))
    anthropic = _StubClient(error=RuntimeError("anthropic down"))
    openai = _StubClient("from openai")
    client = _no_sleep_client(
        clients={"gemini": gemini, "anthropic": anthropic, "openai": openai},
    )
    text, _ = client.send("p", "openai", "gpt-4o")

    assert text == "from openai"
    assert client.last_served_provider == "openai"
    assert openai.calls[0][2] == "gpt-4o"  # row's own model
    assert [p for p, _ in client.last_attempts] == ["gemini", "anthropic", "openai"]


def test_chain_deduplicates_when_row_provider_already_in_chain():
    gemini = _StubClient(error=RuntimeError("down"))
    anthropic = _StubClient("c")
    client = _no_sleep_client(clients={"gemini": gemini, "anthropic": anthropic})
    client.send("p", "anthropic", "claude-sonnet-5")

    assert [p for p, _ in client.last_attempts] == ["gemini", "anthropic"]
    assert len(anthropic.calls) == 1


def test_manual_row_calls_nobody():
    """provider='manual' (multi_pass disabled) must not be auto-billed."""
    gemini, anthropic = _StubClient("g"), _StubClient("c")
    client = _no_sleep_client(clients={"gemini": gemini, "anthropic": anthropic})

    with pytest.raises(RuntimeError, match="manual export"):
        client.send("p", "manual", "none")

    assert gemini.calls == []
    assert anthropic.calls == []


def test_all_providers_failed_raises_once_naming_each_leg():
    gemini = _StubClient(error=RuntimeError("gemini boom"))
    anthropic = _StubClient(error=ValueError("claude boom"))
    client = _no_sleep_client(clients={"gemini": gemini, "anthropic": anthropic})

    with pytest.raises(RuntimeError) as excinfo:
        client.send("p", "anthropic", "claude-sonnet-5")

    message = str(excinfo.value)
    assert "all providers failed" in message
    assert "gemini boom" in message
    assert "claude boom" in message
    # Exactly one attempt each — no unbounded retry.
    assert len(gemini.calls) == 1
    assert len(anthropic.calls) == 1
    assert client.last_served_provider is None


def test_unregistered_provider_is_skipped_not_fatal():
    anthropic = _StubClient("c")
    client = _no_sleep_client(clients={"anthropic": anthropic})  # no gemini client
    text, _ = client.send("p", "anthropic", "claude-sonnet-5")

    assert text == "c"
    assert client.last_served_provider == "anthropic"
    assert client.last_attempts[0][0] == "gemini"
    assert "no client registered" in client.last_attempts[0][1]


def test_leg_without_configured_model_is_skipped():
    config = {
        "routing": {
            "primary": "gemini",
            "fallback": "anthropic",
            "per_provider": {"anthropic": {"model": "claude-sonnet-5"}},
        }
    }
    gemini, anthropic = _StubClient("g"), _StubClient("c")
    client = _no_sleep_client(
        ai_review_config=config,
        clients={"gemini": gemini, "anthropic": anthropic},
    )
    text, _ = client.send("p", "openai", "gpt-4o")

    assert text == "c"
    assert gemini.calls == []
    assert "no model configured" in client.last_attempts[0][1]


def test_config_knob_can_invert_primary_and_fallback():
    """Provider order is config, not hardcoded."""
    config = {
        "routing": {
            "primary": "anthropic",
            "fallback": "gemini",
            "per_provider": {
                "gemini": {"model": "gemini-3.5-flash"},
                "anthropic": {"model": "claude-sonnet-5"},
            },
        }
    }
    gemini, anthropic = _StubClient("g"), _StubClient("c")
    client = _no_sleep_client(
        ai_review_config=config,
        clients={"gemini": gemini, "anthropic": anthropic},
    )
    text, _ = client.send("p", "openai", "gpt-4o")

    assert text == "c"
    assert client.last_served_provider == "anthropic"
    assert gemini.calls == []


def test_empty_chain_raises_without_calling_anyone():
    config = {"routing": {"primary": "", "fallback": "", "per_provider": {}}}
    gemini = _StubClient("g")
    client = _no_sleep_client(ai_review_config=config, clients={"gemini": gemini})

    with pytest.raises(RuntimeError, match="empty provider chain"):
        client.send("p", "", "")
    assert gemini.calls == []


# =========================================================================== #
# 4. Missing-Gemini-key degradation (regression guard for the note's item 5).
# =========================================================================== #
def test_missing_gemini_key_degrades_cleanly_to_anthropic(monkeypatch, gemini_key):
    """No Gemini credential => the real GeminiClient raises => Claude serves."""
    monkeypatch.setattr(are, "_resolve_api_key", lambda *a, **k: None)
    anthropic = _StubClient("from claude")
    client = _no_sleep_client(
        clients={"gemini": GeminiClient(fetch=_fetch_returning(200, _gemini_ok_body())),
                 "anthropic": anthropic},
    )
    text, _ = client.send("p", "anthropic", "claude-sonnet-5")

    assert text == "from claude"
    assert client.last_served_provider == "anthropic"
    assert "missing API key" in client.last_attempts[0][1]


# =========================================================================== #
# 5. Rate-limit pacing.
# =========================================================================== #
def test_second_gemini_call_is_paced_to_min_interval():
    are._LAST_CALL_MONOTONIC.clear()
    gemini = _StubClient("g")
    client = _no_sleep_client(clients={"gemini": gemini})

    client.send("p1", "gemini", "gemini-3.5-flash")
    first_clock = client._test_ticks["t"]
    client.send("p2", "gemini", "gemini-3.5-flash")

    # Default routing config paces gemini at 6.0s (== 10 RPM).
    assert client._test_ticks["t"] - first_clock == pytest.approx(6.0)
    assert len(gemini.calls) == 2


def test_zero_min_interval_provider_is_not_paced():
    are._LAST_CALL_MONOTONIC.clear()
    anthropic = _StubClient("c")
    client = _no_sleep_client(clients={"anthropic": anthropic})

    client.send("p1", "anthropic", "claude-sonnet-5")
    client.send("p2", "anthropic", "claude-sonnet-5")

    assert client._test_ticks["t"] == 0.0


# =========================================================================== #
# 6. Engine integration: served_provider / served_model metadata.
# =========================================================================== #
class _Cursor:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Connection:
    """Understands exactly the SELECT and conditional UPDATE the engine emits."""

    def __init__(self, row: dict) -> None:
        self._row = row

    def execute(self, sql: str, params: list):
        if sql.strip().upper().startswith("SELECT"):
            columns = (
                "ai_review_id", "review_type", "review_kind", "prompt_text",
                "ai_response_text", "provider", "model", "prompt_version",
            )
            description = [(c,) for c in columns]
            if self._row["ai_review_id"] != params[0]:
                return _Cursor(description, [])
            return _Cursor(description, [tuple(self._row[c] for c in columns)])
        # UPDATE ... SET ai_response_text = ? WHERE ... IS NULL RETURNING
        if self._row["ai_response_text"] is None:
            self._row["ai_response_text"] = params[0]
            return _Cursor([("ai_review_id",)], [(params[1],)])
        return _Cursor([("ai_review_id",)], [])

    def close(self) -> None:
        return None


class _DbManager:
    def __init__(self, row: dict) -> None:
        self.row = row

    def connect(self, db_role: str, read_only: bool = False):
        return _Connection(self.row)


@pytest.fixture
def review_row() -> dict:
    return {
        "ai_review_id": "rev-1",
        "review_type": "ticker_review",
        "review_kind": "thesis",
        "prompt_text": "[TICKER REVIEW] assess",
        "ai_response_text": None,
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "prompt_version": "v1",
    }


def test_metadata_keys_include_served_fields():
    assert "served_provider" in are.SEND_METADATA_KEYS
    assert "served_model" in are.SEND_METADATA_KEYS


def test_engine_records_primary_provider_that_served(review_row):
    client = _no_sleep_client(
        clients={"gemini": _StubClient("g"), "anthropic": _StubClient("c")},
    )
    engine = AiReviewEngine(db_manager=_DbManager(review_row), ai_client=client)
    result = engine.send_ticker_review("rev-1")

    assert result.status == service_result.STATUS_SUCCESS
    assert frozenset(result.metadata) == frozenset(are.SEND_METADATA_KEYS)
    # Row still records what M18 wrote...
    assert result.metadata["provider"] == "anthropic"
    assert result.metadata["model"] == "claude-sonnet-5"
    # ...while served_* reports who was actually billed.
    assert result.metadata["served_provider"] == "gemini"
    assert result.metadata["served_model"] == "gemini-3.5-flash-resolved"


def test_engine_records_fallback_provider_that_served(review_row):
    client = _no_sleep_client(
        clients={
            "gemini": _StubClient(error=RuntimeError("429")),
            "anthropic": _StubClient("c"),
        },
    )
    engine = AiReviewEngine(db_manager=_DbManager(review_row), ai_client=client)
    result = engine.send_ticker_review("rev-1")

    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["provider"] == "anthropic"
    assert result.metadata["served_provider"] == "anthropic"
    assert result.metadata["served_model"] == "claude-sonnet-5-resolved"
    assert review_row["ai_response_text"] == "c"


def test_engine_both_providers_fail_is_clean_failure_no_write(review_row):
    client = _no_sleep_client(
        clients={
            "gemini": _StubClient(error=RuntimeError("gemini boom")),
            "anthropic": _StubClient(error=RuntimeError("claude boom")),
        },
    )
    engine = AiReviewEngine(db_manager=_DbManager(review_row), ai_client=client)
    result = engine.send_ticker_review("rev-1")

    assert result.status == service_result.STATUS_FAILED
    assert result.rows_processed == 0
    assert review_row["ai_response_text"] is None  # nothing persisted
    assert "all providers failed" in result.errors[0]
    assert "gemini boom" in result.errors[0]
    assert "claude boom" in result.errors[0]
    assert result.metadata["served_provider"] is None
    assert result.metadata["served_model"] is None


def test_engine_with_plain_default_client_reports_row_provider(review_row):
    """A non-routing client (no last_served_provider) keeps old behavior."""
    engine = AiReviewEngine(
        db_manager=_DbManager(review_row), ai_client=_StubClient("legacy"),
    )
    result = engine.send_ticker_review("rev-1")

    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["served_provider"] == "anthropic"
    assert result.metadata["served_model"] == "claude-sonnet-5-resolved"


# =========================================================================== #
# 7. Config + boundary scans.
# =========================================================================== #
def test_default_routing_config_shape():
    routing = default_configs.get_default_runtime_configs()["ai_review"]["routing"]
    assert routing["primary"] == "gemini"
    assert routing["fallback"] == "anthropic"
    per_provider = routing["per_provider"]
    assert set(per_provider) == {"gemini", "anthropic", "openai"}
    assert per_provider["gemini"]["model"] == "gemini-3.5-flash"
    # 6.0s == 10 RPM, the conservative published Gemini free-tier Flash floor.
    assert per_provider["gemini"]["min_interval_s"] == 6.0


def test_no_provider_sdk_or_http_import_at_module_level():
    """requests / keyring / google must stay lazy, like anthropic and openai."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    forbidden = {"anthropic", "openai", "requests", "keyring", "google"}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                assert name.split(".")[0] not in forbidden, name


def test_gemini_endpoint_is_the_official_host():
    assert are.GEMINI_ENDPOINT_TEMPLATE.startswith(
        "https://generativelanguage.googleapis.com/"
    )
    assert ":generateContent" in are.GEMINI_ENDPOINT_TEMPLATE


def test_default_engine_client_is_the_routing_client():
    engine = AiReviewEngine(db_manager=_DbManager({}))
    assert isinstance(engine._ai, FallbackAiClient)
