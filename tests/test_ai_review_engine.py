"""Tests for Module 19 — AI Review Engine.

All tests run fully offline with no real provider, network, or DuckDB
dependency. An in-memory ``FakeDbManager`` (backed by a plain dict store) hands
out fake connections that understand exactly the ``SELECT`` and ``UPDATE ...
RETURNING`` statements the engine emits, and a ``FakeAiClient`` records calls
and returns a canned ``(response_text, model_used)`` without touching the
network. The ``DefaultAiClient`` is never invoked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.ai_review import ai_review_engine as are
from app.services.ai_review.ai_review_engine import AiReviewEngine
from app.utils import service_result

MODULE_PATH = Path(are.__file__)
SEND_KEYS = frozenset(are.SEND_METADATA_KEYS)
HA_KEYS = frozenset(are.HUMAN_ACTION_METADATA_KEYS)

# Review-row columns in the engine's SELECT order.
_ROW_COLUMNS = (
    "ai_review_id",
    "review_type",
    "prompt_text",
    "ai_response_text",
    "provider",
    "model",
    "prompt_version",
)


# --------------------------------------------------------------------------- #
# In-memory fake DB (no duckdb).
# --------------------------------------------------------------------------- #
class Store:
    """Shared per-role table store: {table: {ai_review_id: row dict}}."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict]] = {
            "ai_reviews": {},
            "sim_ai_reviews": {},
        }

    def seed(self, table: str, **row) -> None:
        full = {c: row.get(c) for c in _ROW_COLUMNS}
        full["human_action"] = row.get("human_action")
        self.tables[table][full["ai_review_id"]] = full


class _Cursor:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """Understands SELECT, conditional UPDATE with RETURNING, plain UPDATE."""

    def __init__(self, store: Store, read_only: bool, fail_write: bool = False):
        self._store = store
        self._read_only = read_only
        self._fail_write = fail_write
        self.closed = False

    def execute(self, sql: str, params=None):
        params = list(params or [])
        head = sql.strip().upper()

        if head.startswith("SELECT"):
            table = self._table_from(sql)
            review_id = params[0]
            row = self._store.tables[table].get(review_id)
            description = [(c,) for c in _ROW_COLUMNS]
            if row is None:
                return _Cursor(description, [])
            tup = tuple(row[c] for c in _ROW_COLUMNS)
            return _Cursor(description, [tup])

        if head.startswith("UPDATE"):
            if self._fail_write:
                raise RuntimeError("simulated review-row write failure")
            table = self._table_from(sql)
            # Extract target column from SET clause.
            set_clause = head.split(" SET ", 1)[1].split(" WHERE ")[0].strip()
            column = set_clause.split("=")[0].strip().lower()
            is_conditional = "AND AI_RESPONSE_TEXT IS NULL" in head
            has_returning = "RETURNING" in head
            value, review_id = params[0], params[1]
            row = self._store.tables[table].get(review_id)

            updated = False
            if row is None:
                pass  # 0 rows matched
            elif is_conditional and row.get("ai_response_text") is not None:
                pass  # conditional guard blocked the write
            else:
                row[column] = value
                updated = True

            if has_returning:
                if updated:
                    return _Cursor([("ai_review_id",)], [(review_id,)])
                return _Cursor([("ai_review_id",)], [])
            return _Cursor([], [])

        raise AssertionError(f"unexpected SQL in fake connection: {sql!r}")

    @staticmethod
    def _table_from(sql: str) -> str:
        return "sim_ai_reviews" if "SIM_AI_REVIEWS" in sql.upper() else "ai_reviews"

    def close(self):
        self.closed = True


class FakeDbManager:
    """Hands out fake connections over a shared store."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.connections: list[FakeConnection] = []

    def connect(self, db_role: str, read_only: bool = False):
        conn = FakeConnection(self._store, read_only)
        self.connections.append(conn)
        return conn


class FailingWriteDbManager(FakeDbManager):
    """Read-only connections pass through; write connections fail on UPDATE."""

    def connect(self, db_role: str, read_only: bool = False):
        conn = FakeConnection(self._store, read_only, fail_write=not read_only)
        self.connections.append(conn)
        return conn


class FailingReadDbManager:
    """First connection raises on execute (simulated read failure before write)."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._call_count = 0

    def connect(self, db_role: str, read_only: bool = False):
        self._call_count += 1
        if self._call_count == 1:
            class _Boom:
                def execute(self, *a, **k):
                    raise RuntimeError("simulated read failure")

                def close(self):
                    pass

            return _Boom()
        return FakeConnection(self._store, read_only)


class _RaceWriteConn(FakeConnection):
    """Simulates a concurrent write that lands between our guard and UPDATE.

    On any conditional ``UPDATE ai_response_text ... AND ai_response_text IS
    NULL``, it first sets the store's ``ai_response_text`` to "race winner"
    before delegating to the normal conditional check. The parent's check then
    sees a non-NULL value and returns 0 rows changed, correctly simulating the
    lost-race path without requiring real concurrency.
    """

    def execute(self, sql: str, params=None):
        head = sql.strip().upper()
        if head.startswith("UPDATE") and "AI_RESPONSE_TEXT IS NULL" in head:
            table = self._table_from(sql)
            params_list = list(params or [])
            review_id = params_list[1] if len(params_list) > 1 else None
            if review_id and review_id in self._store.tables.get(table, {}):
                # Concurrent winner writes first.
                self._store.tables[table][review_id]["ai_response_text"] = "race winner"
        return super().execute(sql, params)


class RaceDbManager:
    """Read-only connects return normal fake; write connects use _RaceWriteConn."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.connections: list = []

    def connect(self, db_role: str, read_only: bool = False):
        if read_only:
            conn = FakeConnection(self._store, read_only=True)
        else:
            conn = _RaceWriteConn(self._store, read_only=False)
        self.connections.append(conn)
        return conn


# --------------------------------------------------------------------------- #
# Fake AI clients.
# --------------------------------------------------------------------------- #
class FakeAiClient:
    """Records calls; returns a canned (response_text, model_used)."""

    def __init__(self, response: str = "AI says: proceed with caution."):
        self.response = response
        self.calls: list[tuple[str, str, str]] = []

    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]:
        self.calls.append((prompt, provider, model))
        return self.response, f"{model}-resolved"


class FailingAiClient:
    """Raises on send; records call count."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, prompt: str, provider: str, model: str) -> tuple[str, str]:
        self.calls += 1
        raise RuntimeError("simulated AI provider error")


# --------------------------------------------------------------------------- #
# Fixtures / helpers.
# --------------------------------------------------------------------------- #
@pytest.fixture
def store() -> Store:
    return Store()


def _engine(store: Store, ai_client=None, db_cls=FakeDbManager):
    db = db_cls(store)
    client = ai_client if ai_client is not None else FakeAiClient()
    return AiReviewEngine(db_manager=db, ai_client=client), db, client


def _seed_ticker(store: Store, review_id="rev-1", sent=False, action=None):
    store.seed(
        "ai_reviews",
        ai_review_id=review_id,
        review_type="ticker_review",
        prompt_text="[TICKER REVIEW] assess these proposals",
        ai_response_text="prior response" if sent else None,
        provider="anthropic",
        model="claude-3",
        prompt_version="v1",
        human_action=action,
    )


def _seed_sim(store: Store, review_id="sim-1", sent=False):
    store.seed(
        "sim_ai_reviews",
        ai_review_id=review_id,
        review_type="simulation_review",
        prompt_text="[SIMULATION REVIEW] assess configs",
        ai_response_text="prior" if sent else None,
        provider="openai",
        model="gpt-4o",
        prompt_version="v1",
    )


# =========================================================================== #
# 1. ServiceResult + run_id mint/preserve for all public methods.
# =========================================================================== #
def test_run_id_minted_when_none(store):
    _seed_ticker(store)
    engine, _, _ = _engine(store)
    result = engine.send_ticker_review("rev-1")
    assert result.run_id
    assert result.run_id == result.metadata["run_id"]


def test_run_id_preserved_when_supplied(store):
    _seed_ticker(store)
    _seed_sim(store)
    engine, _, _ = _engine(store)
    assert engine.send_ticker_review("rev-1", run_id="fixed-1").run_id == "fixed-1"
    _seed_ticker(store, review_id="rev-2", sent=True)
    assert (
        engine.record_human_action("rev-2", "ignored", run_id="fixed-2").run_id
        == "fixed-2"
    )
    assert (
        engine.send_simulation_review("sim-1", run_id="fixed-3").run_id == "fixed-3"
    )


def test_send_metadata_keys_exact_on_success_and_failure(store):
    _seed_ticker(store)
    engine, _, _ = _engine(store)
    ok = engine.send_ticker_review("rev-1")
    assert frozenset(ok.metadata) == SEND_KEYS
    bad = engine.send_ticker_review("", db_role="prod")
    assert frozenset(bad.metadata) == SEND_KEYS


def test_human_action_metadata_keys_exact(store):
    _seed_ticker(store, sent=True)
    engine, _, _ = _engine(store)
    ok = engine.record_human_action("rev-1", "accepted")
    assert frozenset(ok.metadata) == HA_KEYS
    bad = engine.record_human_action("rev-1", "bogus")
    assert frozenset(bad.metadata) == HA_KEYS


# =========================================================================== #
# 2. Pre-DB validation before any DB / AI access.
# =========================================================================== #
def test_send_ticker_invalid_role_fails_before_db_ai(store):
    _seed_ticker(store)
    client = FakeAiClient()
    engine, db, _ = _engine(store, ai_client=client)
    result = engine.send_ticker_review("rev-1", db_role="simulation")
    assert result.status == service_result.STATUS_FAILED
    assert client.calls == []
    assert db.connections == []
    assert result.metadata["provider"] is None


def test_send_simulation_invalid_role_fails_before_db_ai(store):
    _seed_sim(store)
    client = FakeAiClient()
    engine, db, _ = _engine(store, ai_client=client)
    result = engine.send_simulation_review("sim-1", db_role="prod")
    assert result.status == service_result.STATUS_FAILED
    assert client.calls == []
    assert db.connections == []


def test_send_ticker_empty_id_fails_before_db_ai(store):
    client = FakeAiClient()
    engine, db, _ = _engine(store, ai_client=client)
    result = engine.send_ticker_review("", db_role="debug")
    assert result.status == service_result.STATUS_FAILED
    assert client.calls == []
    assert db.connections == []


def test_send_simulation_empty_id_fails_before_db_ai(store):
    """send_simulation_review('') must fail before DB and AI."""
    client = FakeAiClient()
    engine, db, _ = _engine(store, ai_client=client)
    result = engine.send_simulation_review("")
    assert result.status == service_result.STATUS_FAILED
    assert client.calls == []
    assert db.connections == []


def test_record_human_action_invalid_enum_fails_before_db(store):
    _seed_ticker(store, sent=True)
    engine, db, _ = _engine(store)
    result = engine.record_human_action("rev-1", "maybe")
    assert result.status == service_result.STATUS_FAILED
    assert db.connections == []
    assert "Invalid human_action" in result.errors[0]


def test_record_human_action_invalid_role_fails_before_db(store):
    _seed_ticker(store, sent=True)
    engine, db, _ = _engine(store)
    result = engine.record_human_action("rev-1", "accepted", db_role="simulation")
    assert result.status == service_result.STATUS_FAILED
    assert db.connections == []


def test_record_human_action_empty_id_fails_before_db(store):
    """record_human_action('', …) must fail before any DB access."""
    engine, db, _ = _engine(store)
    result = engine.record_human_action("", "accepted")
    assert result.status == service_result.STATUS_FAILED
    assert db.connections == []


# =========================================================================== #
# 3. Row not found: failed; no AI call; no write.
# =========================================================================== #
def test_send_row_not_found(store):
    client = FakeAiClient()
    engine, _, _ = _engine(store, ai_client=client)
    result = engine.send_ticker_review("missing")
    assert result.status == service_result.STATUS_FAILED
    assert "not found" in result.errors[0]
    assert client.calls == []


def test_record_human_action_row_not_found(store):
    engine, _, _ = _engine(store)
    result = engine.record_human_action("missing", "accepted")
    assert result.status == service_result.STATUS_FAILED
    assert "not found" in result.errors[0]


# =========================================================================== #
# 4. Double-send guard: existing ai_response_text fails before AI call / write.
# =========================================================================== #
def test_double_send_blocked_ticker(store):
    _seed_ticker(store, sent=True)
    client = FakeAiClient()
    engine, _, _ = _engine(store, ai_client=client)
    result = engine.send_ticker_review("rev-1")
    assert result.status == service_result.STATUS_FAILED
    assert result.errors[0] == are.ERROR_ALREADY_SENT
    assert client.calls == []
    assert result.metadata["provider"] == "anthropic"
    assert result.metadata["model"] == "claude-3"
    assert result.metadata["prompt_version"] == "v1"
    assert store.tables["ai_reviews"]["rev-1"]["ai_response_text"] == "prior response"


def test_double_send_blocked_simulation(store):
    """Simulation double-send: AI must not be called; sim row must be unchanged."""
    _seed_sim(store, sent=True)
    client = FakeAiClient()
    engine, _, _ = _engine(store, ai_client=client)
    result = engine.send_simulation_review("sim-1")
    assert result.status == service_result.STATUS_FAILED
    assert result.errors[0] == are.ERROR_ALREADY_SENT
    assert client.calls == []
    assert store.tables["sim_ai_reviews"]["sim-1"]["ai_response_text"] == "prior"


# =========================================================================== #
# 5. Successful ticker send.
# =========================================================================== #
def test_ticker_send_success_writes_and_echoes(store):
    _seed_ticker(store)
    client = FakeAiClient(response="ok " * 5)
    engine, _, _ = _engine(store, ai_client=client)
    result = engine.send_ticker_review("rev-1", db_role="prod")
    assert result.status == service_result.STATUS_SUCCESS
    assert result.rows_processed == 1
    assert client.calls[0] == (
        "[TICKER REVIEW] assess these proposals", "anthropic", "claude-3"
    )
    assert store.tables["ai_reviews"]["rev-1"]["ai_response_text"] == "ok " * 5
    assert result.metadata["response_chars"] == len("ok " * 5)
    assert result.metadata["provider"] == "anthropic"
    assert result.metadata["model"] == "claude-3"
    assert result.metadata["prompt_version"] == "v1"
    assert result.metadata["export_type"] == are.EXPORT_TYPE_TICKER
    assert result.metadata["error"] is None


def test_ticker_send_debug_role_allowed(store):
    _seed_ticker(store)
    engine, _, _ = _engine(store)
    assert engine.send_ticker_review("rev-1", db_role="debug").status == (
        service_result.STATUS_SUCCESS
    )


# =========================================================================== #
# 6. Successful simulation send uses sim_ai_reviews.
# =========================================================================== #
def test_simulation_send_success_uses_sim_table(store):
    _seed_sim(store)
    client = FakeAiClient(response="sim review")
    engine, _, _ = _engine(store, ai_client=client)
    result = engine.send_simulation_review("sim-1")
    assert result.status == service_result.STATUS_SUCCESS
    assert client.calls[0] == (
        "[SIMULATION REVIEW] assess configs", "openai", "gpt-4o"
    )
    assert store.tables["sim_ai_reviews"]["sim-1"]["ai_response_text"] == "sim review"
    assert result.metadata["export_type"] == are.EXPORT_TYPE_SIM
    assert store.tables["ai_reviews"] == {}  # ticker table untouched


# =========================================================================== #
# 7. AI call failure: failed; no DB write.
# =========================================================================== #
def test_ai_call_failure_no_write_ticker(store):
    _seed_ticker(store)
    client = FailingAiClient()
    engine, _, _ = _engine(store, ai_client=client)
    result = engine.send_ticker_review("rev-1")
    assert result.status == service_result.STATUS_FAILED
    assert client.calls == 1
    assert "ai call failed" in result.errors[0]
    assert store.tables["ai_reviews"]["rev-1"]["ai_response_text"] is None
    assert result.metadata["response_chars"] is None
    assert result.metadata["provider"] == "anthropic"


def test_ai_call_failure_no_write_simulation(store):
    """Simulation AI failure: sim_ai_reviews must stay unchanged."""
    _seed_sim(store)
    client = FailingAiClient()
    engine, _, _ = _engine(store, ai_client=client)
    result = engine.send_simulation_review("sim-1")
    assert result.status == service_result.STATUS_FAILED
    assert client.calls == 1
    assert "ai call failed" in result.errors[0]
    assert store.tables["sim_ai_reviews"]["sim-1"]["ai_response_text"] is None


# =========================================================================== #
# 8. DB write failure after AI call: response-orphan path.
# =========================================================================== #
def test_write_failure_after_ai_call_orphan_ticker(store):
    _seed_ticker(store)
    client = FakeAiClient(response="generated")
    engine, _, _ = _engine(store, ai_client=client, db_cls=FailingWriteDbManager)
    result = engine.send_ticker_review("rev-1")
    assert result.status == service_result.STATUS_FAILED
    assert client.calls and "G-RESPONSE-ORPHAN" in result.errors[0]
    assert result.metadata["response_chars"] == len("generated")
    assert store.tables["ai_reviews"]["rev-1"]["ai_response_text"] is None


def test_write_failure_after_ai_call_orphan_simulation(store):
    """Simulation write-failure-after-AI: orphan path, sim row must be unchanged."""
    _seed_sim(store)
    client = FakeAiClient(response="generated")
    engine, _, _ = _engine(store, ai_client=client, db_cls=FailingWriteDbManager)
    result = engine.send_simulation_review("sim-1")
    assert result.status == service_result.STATUS_FAILED
    assert "G-RESPONSE-ORPHAN" in result.errors[0]
    assert store.tables["sim_ai_reviews"]["sim-1"]["ai_response_text"] is None


def test_read_failure_surfaced(store):
    _seed_ticker(store)
    engine, _, _ = _engine(store, db_cls=FailingReadDbManager)
    result = engine.send_ticker_review("rev-1")
    assert result.status == service_result.STATUS_FAILED
    assert "read failed" in result.errors[0]


# =========================================================================== #
# 8b. TOCTOU race: conditional UPDATE returns 0 rows.
# =========================================================================== #
def test_lost_race_conditional_update_fails(store):
    """AI is called but loses the write race; winner's value must be preserved."""
    _seed_ticker(store)
    client = FakeAiClient(response="our response")
    engine = AiReviewEngine(db_manager=RaceDbManager(store), ai_client=client)
    result = engine.send_ticker_review("rev-1")
    # AI was called (guard passed on read; race hit at write time).
    assert len(client.calls) == 1
    assert client.calls[0] == (
        "[TICKER REVIEW] assess these proposals", "anthropic", "claude-3"
    )
    # Conditional UPDATE returned 0 rows → failed with already-sent error.
    assert result.status == service_result.STATUS_FAILED
    assert result.errors[0] == are.ERROR_ALREADY_SENT
    # Race winner's value preserved; our response was not written.
    assert store.tables["ai_reviews"]["rev-1"]["ai_response_text"] == "race winner"
    # response_chars reflects the (orphaned) AI response we obtained.
    assert result.metadata["response_chars"] == len("our response")


# =========================================================================== #
# 9. record_human_action success writes valid enum + exact metadata.
# =========================================================================== #
@pytest.mark.parametrize("action", are.HUMAN_ACTIONS)
def test_record_human_action_success(store, action):
    _seed_ticker(store, sent=True)
    engine, _, _ = _engine(store)
    result = engine.record_human_action("rev-1", action)
    assert result.status == service_result.STATUS_SUCCESS
    assert result.rows_processed == 1
    assert store.tables["ai_reviews"]["rev-1"]["human_action"] == action
    assert result.metadata["human_action"] == action
    assert result.metadata["error"] is None


# =========================================================================== #
# 10. record_human_action blocked when ai_response_text IS NULL.
# =========================================================================== #
def test_record_human_action_blocked_before_send(store):
    _seed_ticker(store, sent=False)
    engine, _, _ = _engine(store)
    result = engine.record_human_action("rev-1", "accepted")
    assert result.status == service_result.STATUS_FAILED
    assert result.errors[0] == are.ERROR_ACTION_BEFORE_SEND
    assert store.tables["ai_reviews"]["rev-1"]["human_action"] is None


def test_record_human_action_read_failure_returns_failed(store):
    """DB read failure on record_human_action must return failed ServiceResult."""
    _seed_ticker(store, sent=True)
    engine, _, _ = _engine(store, db_cls=FailingReadDbManager)
    result = engine.record_human_action("rev-1", "accepted")
    assert result.status == service_result.STATUS_FAILED
    assert "read failed" in result.errors[0]


def test_record_human_action_write_failure_returns_failed(store):
    """DB write failure on human_action UPDATE: failed result, row unchanged."""
    _seed_ticker(store, sent=True)
    engine, _, _ = _engine(store, db_cls=FailingWriteDbManager)
    result = engine.record_human_action("rev-1", "accepted")
    assert result.status == service_result.STATUS_FAILED
    assert "human_action write failed" in result.errors[0]
    assert store.tables["ai_reviews"]["rev-1"]["human_action"] is None


# =========================================================================== #
# 11. Static boundary scans.
# =========================================================================== #
def _execute_sql_strings(tree: ast.AST) -> list[str]:
    """Collect string-literal SQL passed to any ``.execute(...)`` call."""
    sql: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                sql.append(first.value)
    return sql


def test_no_forbidden_imports_or_print():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "duckdb"
                assert "providers" not in alias.name
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module.split(".")[0] != "duckdb"
            assert "providers" not in module
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_ddl_or_attach_in_executed_sql():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for sql in _execute_sql_strings(tree):
        upper = sql.upper()
        for token in (
            "CREATE TABLE", "CREATE VIEW", "CREATE INDEX",
            "DROP ", "ALTER ", "ATTACH ",
        ):
            assert token not in upper, f"forbidden SQL token: {token}"


def test_only_update_no_insert_or_delete():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    sql_strings = _execute_sql_strings(tree)
    update_targets: list[str] = []
    for sql in sql_strings:
        upper = sql.upper()
        assert "INSERT INTO" not in upper, "Module 19 must not INSERT"
        assert not upper.strip().startswith("DELETE"), "Module 19 must not DELETE"
        if upper.startswith("UPDATE "):
            update_targets.append(upper.split("UPDATE", 1)[1].strip().split()[0])
    assert update_targets, "expected at least one UPDATE in the module"
    assert set(update_targets) <= {"AI_REVIEWS", "SIM_AI_REVIEWS"}


def test_only_allowed_columns_in_set_clauses():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for sql in _execute_sql_strings(tree):
        upper = sql.upper()
        if upper.startswith("UPDATE ") and " SET " in upper:
            set_clause = upper.split(" SET ", 1)[1].split(" WHERE ")[0].strip()
            column = set_clause.split("=")[0].strip()
            assert column in {"AI_RESPONSE_TEXT", "HUMAN_ACTION"}, column


def test_no_provider_sdk_import_at_module_level():
    """SDK imports must be lazy (inside functions), never at module top level."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                assert name.split(".")[0] not in {"anthropic", "openai"}


def test_no_unused_settings_import():
    """The `settings` module must not be imported (fix 2)."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "config" in module:
                imported_names = [a.name for a in node.names]
                assert "settings" not in imported_names, (
                    "settings is unused; it must not be imported"
                )


def test_no_select_changes_in_module_sql():
    """SELECT changes() must not appear in any executed SQL literal."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for sql in _execute_sql_strings(tree):
        assert "changes()" not in sql.lower(), (
            f"SELECT changes() found in executed SQL: {sql!r}"
        )


def test_conditional_update_uses_returning_not_changes():
    """Both ai_response_text UPDATE statements must use conditional RETURNING.

    Exactly 2 response-UPDATE literals must exist — one for ai_reviews and one
    for sim_ai_reviews — each using ``AND ai_response_text IS NULL RETURNING``
    as a single statement with no second SQL call.
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    response_updates = [
        sql for sql in _execute_sql_strings(tree)
        if sql.strip().upper().startswith("UPDATE")
        and "AI_RESPONSE_TEXT" in sql.upper()
        and " SET " in sql.upper()
    ]
    assert len(response_updates) == 2, (
        f"expected 2 ai_response_text UPDATE statements, got {len(response_updates)}"
    )
    tables_seen: set[str] = set()
    for sql in response_updates:
        upper = sql.upper()
        assert "AND AI_RESPONSE_TEXT IS NULL" in upper, (
            f"conditional guard missing in: {sql!r}"
        )
        assert "RETURNING" in upper, f"RETURNING clause missing in: {sql!r}"
        assert "CHANGES()" not in upper
        tables_seen.add("sim_ai_reviews" if "SIM_AI_REVIEWS" in upper else "ai_reviews")
    # One literal per table.
    assert tables_seen == {"ai_reviews", "sim_ai_reviews"}
