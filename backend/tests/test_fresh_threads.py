
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

from tests.project_ref_helpers import project_ref, select_workspace

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


class _NoClose:
    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_c", conn)

    def close(self) -> None:
        try:
            object.__getattribute__(self, "_c").rollback()
        except sqlite3.Error:
            pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_c"), name)


def _target(model: str):
    from llm.resolution import ExecutionTarget

    if model in {"claude-code/sonnet", "claude-code/opus"}:
        harness, backend = "claude-code", "cc"
    elif model in {"codex/gpt-5", "codex/gpt-4o"}:
        harness, backend = "codex", "codex"
    else:
        harness, backend = "native", "api"
    return ExecutionTarget(
        catalog_id=model,
        harness=harness,
        provider=harness,
        backend=backend,
        transport_model=model,
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name=model,
        supports_reasoning=False,
    )


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "fresh.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, profile TEXT,
            title TEXT, model TEXT,
            project_id TEXT, parent_thread_id TEXT,
            spawned_by TEXT REFERENCES threads(id),
            created_at TEXT, updated_at TEXT,
            history_epoch INTEGER NOT NULL DEFAULT 0,
            next_message_seq INTEGER NOT NULL DEFAULT 1,
            last_context_change_seq INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL,
            seq INTEGER, delivery_state TEXT NOT NULL DEFAULT 'delivered',
            delivery_error TEXT, delivery_revision INTEGER NOT NULL DEFAULT 1,
            active_turn_id TEXT, write_outcome TEXT, delivery_intent TEXT
        );
        CREATE UNIQUE INDEX idx_messages_session_seq ON messages(thread_id, seq);
        CREATE TABLE active_turns (thread_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT, path TEXT
        );
        CREATE TABLE context_change_journal (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL
        );
        """
    )
    from infra.connection_schema import CONNECTION_SCHEMA_SQL
    from infra.schema import SCHEMA_SQL
    from infra.vendor_account_schema import VENDOR_ACCOUNT_SCHEMA_SQL
    from infra.vendor_policy_schema import VENDOR_POLICY_SCHEMA_SQL
    from connections.vendor_accounts import ensure_default_account

    model_ddl = "CREATE TABLE IF NOT EXISTS models" + SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS models", 1)[1].split(";", 1)[0] + ";"
    conn.executescript(model_ddl + CONNECTION_SCHEMA_SQL + VENDOR_ACCOUNT_SCHEMA_SQL + VENDOR_POLICY_SCHEMA_SQL)
    for column in ("target_model TEXT", "delivery_attempt_id TEXT", "write_started_at TEXT", "input_order INTEGER"):
        conn.execute("ALTER TABLE messages ADD COLUMN " + column)
    for provider in ("claude-code", "codex"):
        conn.execute("INSERT INTO connections(id,type,provider,status,created_at,updated_at) VALUES (?,'cli',?,'available','t','t')",
                     (provider, provider))
        ensure_default_account(provider, db=conn)
    for model in ("claude-code/sonnet", "claude-code/opus", "codex/gpt-5", "codex/gpt-4o",
                  "anthropic/claude-3-5-sonnet", "omlx@server/Qwen-35B"):
        target = _target(model)
        conn.execute("INSERT INTO models(id,harness,provider,provider_model_id,litellm_model,display_name,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,'t','t')", (model, target.harness, target.provider, model, model, model))
    conn.commit()
    wrapped = _NoClose(conn)

    def factory():
        return wrapped

    import infra.fresh_threads as fresh_threads
    monkeypatch.setattr(
        "infra.execution_class.resolve_execution_target",
        lambda model, db=None: _target(model),
    )
    monkeypatch.setattr(fresh_threads, "get_db", factory)
    monkeypatch.setattr("infra.send_broadcast.get_db", factory)
    monkeypatch.setattr("infra.database._thread_local.conn", conn, raising=False)
    monkeypatch.setattr(
        fresh_threads,
        "run_in_db",
        _ImmediateRunInDb(),
        raising=False,
    )
    monkeypatch.setattr("infra.state.broadcast_to_user", lambda *a, **k: None)
    yield conn
    conn.close()


class _ImmediateRunInDb:
    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def test_fresh_thread_starts_after_existing_context_changes(fresh_db):
    """A new task must not replay context changes that predate its creation."""
    from infra import fresh_threads

    fresh_db.executemany(
        "INSERT INTO context_change_journal (summary) VALUES (?)",
        (("older",), ("current",)),
    )
    session = fresh_threads.create_fresh_thread(
        user_id="u-test",
        thread_id="fresh-after-changes",
        created_at="2026-06-17T00:00:00",
        db=fresh_db,
    )

    cursor = fresh_db.execute(
        "SELECT last_context_change_seq FROM threads WHERE id=?",
        (session["id"],),
    ).fetchone()["last_context_change_seq"]
    assert cursor == 2


@pytest.mark.asyncio
async def test_create_visible_fresh_thread_emits_session_created(fresh_db, monkeypatch):
    from infra import fresh_threads

    events = []
    monkeypatch.setattr(
        "infra.state.broadcast_to_user",
        lambda user_id, payload: events.append((user_id, payload)),
    )

    session = await fresh_threads.create_visible_fresh_thread(
        user_id="u-test",
        profile="personal",
        title="Fresh Chat",
        model="anthropic/claude-3-5-sonnet",
        created_at="2026-06-17T00:00:00",
    )

    assert session["title"] == "Fresh Chat"
    assert session["model"] == "anthropic/claude-3-5-sonnet"
    assert session["spawned_by"] is None
    assert events == [
        ("u-test", {"type": "session_created", "session": session})
    ]


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_fail_committed_thread(fresh_db, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("ws down")

    monkeypatch.setattr("infra.state.broadcast_to_user", _boom)

    from infra import fresh_threads

    session = await fresh_threads.create_visible_fresh_thread(
        user_id="u-test",
        title="Durable Chat",
        created_at="2026-06-17T00:00:00",
    )

    assert fresh_db.execute(
        "SELECT title FROM threads WHERE id=?", (session["id"],)
    ).fetchone()["title"] == "Durable Chat"


@pytest.mark.asyncio
async def test_create_visible_fresh_thread_links_spawn_and_emits_chip(fresh_db, monkeypatch):
    from infra import fresh_threads

    fresh_db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
        "VALUES ('parent-1', 'u-test', 'Parent', 't', 't')"
    )
    fresh_db.commit()
    broadcasts = []
    monkeypatch.setattr(
        "infra.state.broadcast_to_thread",
        lambda sid, payload: broadcasts.append((sid, payload)),
    )

    session = await fresh_threads.create_visible_fresh_thread(
        user_id="u-test",
        profile="personal",
        title="Worker",
        model="claude-code/sonnet",
        thread_kind="subagent",
        spawned_by="parent-1",
        emit_spawn_chip=True,
        created_at="2026-06-17T00:00:00",
    )

    row = fresh_db.execute(
        "SELECT spawned_by FROM threads WHERE id = ?",
        (session["id"],),
    ).fetchone()
    assert row["spawned_by"] == "parent-1"  # parent presence ⇒ subagent

    chip = fresh_db.execute(
        "SELECT role, content, metadata FROM messages WHERE thread_id = 'parent-1'"
    ).fetchone()
    assert chip["role"] == "system"
    assert chip["content"] == "Spawned: Worker"
    meta = json.loads(chip["metadata"])
    assert meta == {
            "type": "subagent_ref",
            "child_thread_id": session["id"],
            "name": "Worker",
            "provider": "cc",  # model is claude-code/sonnet → cc (#551 fix)
            "thread_kind": "subagent",
            "model": "claude-code/sonnet",
        }
    assert broadcasts[0][0] == "parent-1"
    assert broadcasts[0][1]["type"] == "message_added"


@pytest.mark.asyncio
async def test_create_visible_fresh_thread_rejects_unowned_spawner(fresh_db, monkeypatch):
    from infra import fresh_threads

    fresh_db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
        "VALUES ('other-parent', 'u-other', 'Other', 't', 't')"
    )
    fresh_db.commit()
    with pytest.raises(fresh_threads.SpawnVisibilityRejected):
        await fresh_threads.create_visible_fresh_thread(
            user_id="u-test",
            title="Worker",
            thread_kind="subagent",
            spawned_by="other-parent",
            emit_spawn_chip=True,
        )

    rows = fresh_db.execute("SELECT id FROM threads WHERE title = 'Worker'").fetchall()
    assert rows == []


@pytest.mark.parametrize("model,expected_provider", [
    ("claude-code/sonnet", "cc"),
    ("claude-code/opus", "cc"),
    ("codex/gpt-5", "codex"),
    ("codex/gpt-4o", "codex"),
    ("omlx@server/Qwen-35B", "native"),
    ("anthropic/claude-3-5-sonnet", "native"),
    ("anthropic/claude-opus-4-8", "native"),
    (None, "native"),
    ("", "native"),
])
def test_provider_from_model(model, expected_provider):
    """provider_label must map model ids to the correct spawn-chip provider.

    Regression guard for #551: before the fix, create_visible_fresh_thread
    always passed provider=source ('delegation') instead of deriving from the
    model id, so native/oMLX chip markers carried provider='delegation' which
    the frontend ADAPTER mapped to 'claude-code'.
    """
    from infra.execution_class import provider_label
    target = _target(model) if model else None
    assert provider_label(model, target=target) == expected_provider


@pytest.mark.asyncio
async def test_spawn_chip_uses_model_derived_provider_for_native(fresh_db, monkeypatch):
    """A native/oMLX spawn chip must carry provider='native', not 'delegation'."""
    import json
    from infra import fresh_threads

    fresh_db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
        "VALUES ('parent-n', 'u-test', 'Parent', 't', 't')"
    )
    fresh_db.commit()
    monkeypatch.setattr(
        "infra.state.broadcast_to_thread",
        lambda sid, payload: None,
    )

    await fresh_threads.create_visible_fresh_thread(
        user_id="u-test",
        profile="personal",
        title="Native Worker",
        model="omlx@server/Qwen-35B",
        thread_kind="subagent",
        spawned_by="parent-n",
        emit_spawn_chip=True,
        created_at="2026-06-26T00:00:00",
    )

    chip = fresh_db.execute(
        "SELECT metadata FROM messages WHERE thread_id = 'parent-n'"
    ).fetchone()
    meta = json.loads(chip["metadata"])
    assert meta["provider"] == "native"


@pytest.mark.asyncio
async def test_spawn_chip_uses_cc_provider_for_cc_model(fresh_db, monkeypatch):
    """A CC model spawn chip must carry provider='cc'."""
    import json
    from infra import fresh_threads

    fresh_db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
        "VALUES ('parent-cc', 'u-test', 'Parent', 't', 't')"
    )
    fresh_db.commit()
    monkeypatch.setattr(
        "infra.state.broadcast_to_thread",
        lambda sid, payload: None,
    )

    await fresh_threads.create_visible_fresh_thread(
        user_id="u-test",
        profile="personal",
        title="CC Worker",
        model="claude-code/opus",
        thread_kind="subagent",
        spawned_by="parent-cc",
        emit_spawn_chip=True,
        created_at="2026-06-26T00:00:00",
    )

    chip = fresh_db.execute(
        "SELECT metadata FROM messages WHERE thread_id = 'parent-cc'"
    ).fetchone()
    meta = json.loads(chip["metadata"])
    assert meta["provider"] == "cc"


def test_resolve_spawn_working_dir_uses_parent_project_path(fresh_db, tmp_path, monkeypatch):
    from infra.fresh_threads import resolve_spawn_working_dir

    import infra.fresh_threads as fresh_threads

    select_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(fresh_threads, "WORKSPACE", tmp_path.resolve())

    fresh_db.execute("INSERT INTO projects VALUES ('proj-1', 'Project', '" + project_ref("proj-1") + "')")
    fresh_db.execute(
        "INSERT INTO threads (id, user_id, project_id, created_at, updated_at) "
        "VALUES ('parent-1', 'u-test', 'proj-1', 't', 't')"
    )
    fresh_db.commit()

    working_dir, project_id = resolve_spawn_working_dir("parent-1")

    assert working_dir == str(tmp_path.resolve() / "projects" / "proj-1")
    assert project_id == "proj-1"
