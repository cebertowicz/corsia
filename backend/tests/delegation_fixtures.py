from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from auth_fixtures import seed_account
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.schema import SCHEMA_SQL
from tests.conftest import _reset_db_pool

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_TOKEN = "deleg-token-01"


class _InlineExecutor(concurrent.futures.Executor):
    """Run ``run_in_db`` work on the calling thread instead of the DB pool.

    ``get_db()`` is thread-local, so work handed to the real pool would read a
    *different* connection than the request thread and could not see a seed this
    fixture has not committed. Keeping that work on the calling thread keeps one
    connection, one transaction, and one set of savepoints.

    This must be done by replacing the executor, not by patching ``run_in_db``
    per module: several modules bind that name at import time and keep the real
    one. ``infra.db_async``'s three executors (``_pool`` and the read and bulk
    lanes) are the single point every caller shares.
    """

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        if not future.set_running_or_notify_cancel():
            return future
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


@pytest.fixture
def deleg_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    # Isolate below ``get_db()`` rather than in front of it. Redirecting
    # ``DB_PATH`` and resetting the pool is what every other fixture in this
    # tree does, and it holds for every module that reads -- including ones
    # added later. Patching ``get_db`` per module instead would isolate only
    # the modules named here, and an unlisted one would read whatever
    # ``DB_PATH`` pointed at.
    db_path = tmp_path / "deleg.db"
    from infra import config as config_mod, database
    monkeypatch.setattr(config_mod, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    _reset_db_pool(database)
    # The pooled thread-local connection *is* the fixture's connection, so a
    # seed this fixture has not committed is still visible to the code under
    # test, and savepoint nesting behaves as it does in production. That shared
    # identity is what ``write_transaction``'s declared ownership uses.
    conn = database.get_db()
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO model_profiles (model_id, context_window) VALUES (?, ?)",
        [
            ("anthropic/claude-3-5-sonnet", 200000),
            ("anthropic/claude-3-5-opus", 200000),
            ("anthropic/claude-sonnet-4-6", 200000),
            ("claude-code/opus", 1_000_000),
            ("claude-code/sonnet", 200000),
            ("claude-code/tiny", 128),
            ("codex/gpt-5-codex", 200000),
            ("omlx@local/qwen", 200000),
            ("oMLX/qwen", 200000),
        ],
    )
    conn.executemany(
        "INSERT INTO models "
        "(id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,enabled,available,context_window,metadata_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("anthropic/claude-3-5-sonnet", "native", "anthropic",
             "claude-3-5-sonnet", "anthropic/claude-3-5-sonnet",
             "Claude 3.5 Sonnet", "api", "test", 1, 1, 200000, "{}", "t", "t"),
            ("anthropic/claude-3-5-opus", "native", "anthropic",
             "claude-3-5-opus", "anthropic/claude-3-5-opus",
             "Claude 3.5 Opus", "api", "test", 1, 1, 200000, "{}", "t", "t"),
            ("anthropic/tiny", "native", "anthropic", "tiny", "anthropic/tiny",
             "Tiny", "api", "test", 1, 1, 128, "{}", "t", "t"),
            ("anthropic/large", "native", "anthropic", "large", "anthropic/large",
             "Large", "api", "test", 1, 1, 200000, "{}", "t", "t"),
            ("claude-code/opus", "claude-code", "claude-code", "opus",
             "claude-code/opus", "Opus", "cc", "test", 1, 1, 1000000,
             '{"resolved_identity":"claude-opus-4-8"}', "t", "t"),
            ("claude-code/sonnet", "claude-code", "claude-code", "sonnet",
             "claude-code/sonnet", "Sonnet", "cc", "test", 1, 1, 200000,
             '{"resolved_identity":"claude-sonnet-4-8"}', "t", "t"),
            ("claude-code/tiny", "claude-code", "claude-code", "tiny",
             "claude-code/tiny", "Tiny CC", "cc", "test", 1, 1, 128,
             '{"resolved_identity":"tiny"}', "t", "t"),
            ("codex/gpt-5-codex", "codex", "codex", "gpt-5-codex",
             "codex/gpt-5-codex", "Codex", "codex", "test", 1, 1, 200000,
             '{"resolved_identity":"gpt-5-codex"}', "t", "t"),
            ("omlx@local/qwen", "native", "omlx", "qwen",
             "omlx@local/qwen", "Qwen", "network", "test", 1, 1, 200000,
             "{}", "t", "t"),
        ],
    )
    conn.executemany(
        "INSERT INTO connections (id,type,provider,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("test-cc", "cli", "claude-code", "available", "t", "t"),
            ("test-codex", "cli", "codex", "available", "t", "t"),
        ],
    )
    # Shared sender thread owned by the test user. Delegated /send now REQUIRES
    # verified sender attribution (bug #362), so tests that exercise a successful
    # send pass ``from_thread_id="sender-self"``.
    conn.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('sender-self','u-test','claude-code/opus','Sender Thread','t','t')"
    )
    conn.commit()
    from core import agent as agent_mod
    from infra import settings as settings_mod
    import llm.model_status as model_status
    monkeypatch.setattr(model_status, "_cloud_providers_active", lambda: {"anthropic"})
    import api.delegation as deleg
    import api.delegation_model as deleg_model
    import infra.delegation_markers as delegation_markers
    import infra.fresh_threads as fresh_threads
    import infra.delegation as deleg_resolve
    fake_native_agent = SimpleNamespace(
        max_tokens=0,
        tools=SimpleNamespace(get_definitions=lambda: []),
        context=SimpleNamespace(
            build_system_prompt=lambda **_kwargs: "test system prompt",
            _build_user_content=lambda content, media, **_kwargs: [
                {"type": "text", "text": content},
                *[
                    {"type": "image_url", "image_url": {"url": item}}
                    for item in media
                ],
            ],
        ),
        _connect_mcp=AsyncMock(),
    )
    monkeypatch.setattr(
        agent_mod,
        "get_agent",
        AsyncMock(return_value=fake_native_agent),
    )
    monkeypatch.setattr(deleg_resolve, "get_setting", settings_mod.get_setting)
    monkeypatch.setattr(deleg_resolve, "set_setting", settings_mod.set_setting)
    from infra import db_async
    for pool in ("_pool", "_read_pool", "_bulk_pool"):
        monkeypatch.setattr(db_async, pool, _InlineExecutor())
    monkeypatch.setattr(deleg, "run_in_db",
                        AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k)))
    monkeypatch.setattr(delegation_markers, "run_in_db",
                        AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k)))
    monkeypatch.setattr(deleg_model, "run_in_db",
                        AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k)))
    monkeypatch.setattr(fresh_threads, "run_in_db",
                        AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k)))
    monkeypatch.setattr("infra.state.broadcast_to_user", lambda *a, **k: None)
    monkeypatch.setattr(deleg, "emit_product_event", lambda *a, **k: None)
    yield conn
    # Symmetric with the reset at setup, and it belongs here rather than in the
    # callers: this fixture is what moved the pool, so it is what owns putting
    # it back. ``monkeypatch`` restores ``DB_PATH`` but knows nothing about the
    # connection opened while the redirect was live -- left behind, that
    # connection stays bound to a ``tmp_path`` database pytest then deletes, and
    # the next test on this worker thread reads it instead of its own.
    # Teardown order matters and pytest gives us the right one: this runs before
    # monkeypatch restores ``DB_PATH``, so the next ``get_db()`` opens against
    # the restored path rather than reviving this one.
    _reset_db_pool(database)


@pytest.fixture
def client(deleg_db, monkeypatch):
    seed_account("u-test", _TOKEN)
    import api.delegation as deleg
    import api.delegation_model as deleg_model
    _auth_stub = lambda t: {"id": "u-test"} if t == _TOKEN else None
    monkeypatch.setattr(deleg, "authenticate", _auth_stub)
    monkeypatch.setattr(deleg_model, "authenticate", _auth_stub)
    app = FastAPI()
    app.include_router(deleg.router)
    app.include_router(deleg_model.router)
    return TestClient(app)


def insert_thread(db, thread_id, *, user_id="u-test", **cols):
    """Insert one threads row with the timestamps these tests expect."""
    keys = ", ".join((*cols, "created_at", "updated_at"))
    marks = ", ".join("?" for _ in range(len(cols) + 2))
    db.execute(
        f"INSERT INTO threads (id,user_id,{keys}) VALUES (?,?,{marks})",
        (thread_id, user_id, *cols.values(), "now", "now"),
    )
    db.commit()
