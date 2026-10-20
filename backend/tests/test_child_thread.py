
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import uuid

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_TURN_ID = "11111111-2222-4333-8444-555555555555"


class _TestNoOpClose:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def close(self):
        try:
            object.__getattribute__(self, "_conn").rollback()
        except sqlite3.Error:
            pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


class _PlanCapturingConnection(_TestNoOpClose):
    """Capture the provider-step lookup plan without changing its SQL."""

    def __init__(self, conn):
        super().__init__(conn)
        object.__setattr__(self, "provider_step_plans", [])

    def execute(self, sql, parameters=()):
        if (
            sql.startswith("SELECT id FROM messages")
            and "json_extract(metadata,'$.provider_turn_id')" in sql
        ):
            plan = self._conn.execute(
                "EXPLAIN QUERY PLAN " + sql, parameters,
            ).fetchall()
            self.provider_step_plans.append([row[3] for row in plan])
        return self._conn.execute(sql, parameters)


# Account holds read these queue columns; account admission reads the account tables.
_ACCOUNT_HOLD_MESSAGE_COLUMNS = (
    "input_order", "delivery_state", "active_turn_id", "write_started_at",
    "delivery_attempt_id", "write_outcome", "target_model", "delivery_error",
    "delivery_revision",
)


def _add_canonical_message_columns(conn, names):
    """Add ``names`` exactly as the canonical messages DDL declares them, so they cannot drift."""
    from infra.message_schema import MESSAGE_SCHEMA_SQL

    canonical = sqlite3.connect(":memory:")
    canonical.executescript(MESSAGE_SCHEMA_SQL)
    declared = {row[1]: row for row in canonical.execute("PRAGMA table_info(messages)")}
    canonical.close()
    for name in names:
        _, _, kind, not_null, default, _ = declared[name]
        conn.execute(
            f"ALTER TABLE messages ADD COLUMN {name} {kind}"
            + (" NOT NULL" if not_null else "")
            + (f" DEFAULT {default}" if default is not None else "")
        )


@pytest.fixture
def child_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT DEFAULT 'New Chat',
            model TEXT,
            project_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            parent_thread_id TEXT REFERENCES threads(id),
            spawned_by TEXT REFERENCES threads(id),
            local_only INTEGER NOT NULL DEFAULT 0,
            local_only_root_thread_id TEXT REFERENCES threads(id),
            history_epoch INTEGER NOT NULL DEFAULT 0,
            next_message_seq INTEGER NOT NULL DEFAULT 1,
            last_context_change_seq INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES threads(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            seq INTEGER
        );
        CREATE UNIQUE INDEX idx_messages_session_seq ON messages(thread_id, seq);
        CREATE INDEX idx_messages_turn_calls
            ON messages(thread_id, role, json_extract(metadata, '$.turn_id'))
            WHERE role='assistant' AND json_valid(metadata);
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            path TEXT
        );
        CREATE TABLE IF NOT EXISTS project_inference_policies (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            cloud_models_off INTEGER NOT NULL DEFAULT 0 CHECK (cloud_models_off IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS connections (
            id TEXT PRIMARY KEY,
            type TEXT,
            provider TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS models (
            id TEXT PRIMARY KEY,
            harness TEXT,
            provider TEXT,
            provider_model_id TEXT,
            litellm_model TEXT,
            display_name TEXT,
            backend TEXT,
            source TEXT,
            enabled INTEGER,
            available INTEGER,
            context_window INTEGER,
            metadata_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS active_turns (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            model TEXT,
            status TEXT NOT NULL,
            started_at TEXT
        );
        CREATE TABLE IF NOT EXISTS context_change_journal (
            seq INTEGER PRIMARY KEY AUTOINCREMENT
        );
        """
    )
    from infra.vendor_policy_schema import VENDOR_SUBSCRIPTION_SCHEMA_SQL

    conn.executescript(VENDOR_SUBSCRIPTION_SCHEMA_SQL)
    _add_canonical_message_columns(conn, _ACCOUNT_HOLD_MESSAGE_COLUMNS)
    # Every row below is persisted BY a Turn that is still running. A declared
    # tool call is admitted only while its Turn can still answer it, so the
    # fixture has to hold that Turn open (infra.queries.
    # assert_turn_accepts_tool_calls).
    conn.execute(
        "INSERT INTO active_turns (id, thread_id, model, status, started_at) "
        "VALUES (?, 'parent', 'claude-code/sonnet', 'running', 't')",
        (_TURN_ID,),
    )
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("proj-1", "Project 1", "/tmp/proj-1"),
    )
    conn.execute(
        "INSERT INTO connections (id, type, provider, status, created_at, updated_at) "
        "VALUES ('cc', 'cli', 'claude-code', 'available', 't', 't')"
    )
    conn.execute(
        "INSERT INTO models "
        "(id, harness, provider, provider_model_id, litellm_model, display_name, "
        "backend, source, enabled, available, context_window, metadata_json, "
        "created_at, updated_at) "
        "VALUES ('claude-code/sonnet', 'claude-code', 'claude-code', 'sonnet', "
        "'claude-code/sonnet', 'Sonnet', 'cc', 'test', 1, 1, 1000000, '{}', 't', 't')"
    )
    conn.commit()
    wrapper = _TestNoOpClose(conn)

    from infra import database
    from subagents import child_thread

    factory = lambda: wrapper  # noqa: E731
    monkeypatch.setattr(database, "get_db", factory)
    monkeypatch.setattr(child_thread, "get_db", factory)

    yield wrapper
    conn.close()


def _rows(db, sid):
    rows = db.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at, id", (sid,)
    ).fetchall()
    return [dict(r) for r in rows]


def _meta(row):
    return json.loads(row["metadata"]) if row["metadata"] else None


def _tool_rows(db, sid):
    return [row for row in _rows(db, sid) if row["role"] == "tool"]


def _tool_step(db, sid, tool_id, *, name="Bash"):
    from subagents.child_thread import persist_tool_use

    return persist_tool_use(
        sid,
        turn_id=_TURN_ID,
        tool_id=tool_id,
        name=name,
        arguments={},
        db=db,
    )


def _seed_parent(db, sid="parent"):
    db.execute(
        "INSERT INTO threads (id, user_id, created_at, updated_at) VALUES (?,?,?,?)",
        (sid, "steve", "t", "t"),
    )
    db.commit()


class TestCreateChildSession:
    def test_creates_subagent_with_parent_linkage(self, child_db):
        from infra.execution_class import is_subagent
        from subagents.child_thread import create_child_thread

        _seed_parent(child_db)
        cid = create_child_thread(
            user_id="steve", parent_thread_id="parent",
            title="pr-fixer", model="claude-code/sonnet",
            project_id="proj-1", db=child_db,
        )
        row = dict(child_db.execute("SELECT * FROM threads WHERE id = ?", (cid,)).fetchone())
        # Parent presence (spawned_by) is the single source of truth: a child is
        # a subagent (sidebar-excluded) exactly because it has a parent.
        assert row["spawned_by"] == "parent"
        assert is_subagent(row) is True
        assert row["parent_thread_id"] == "parent"
        assert row["model"] == "claude-code/sonnet"
        assert row["project_id"] == "proj-1"
        assert row["title"] == "pr-fixer"

    def test_premint_id_is_resume_key(self, child_db):
        from subagents.child_thread import create_child_thread

        _seed_parent(child_db, "p2")
        cid = create_child_thread(
            user_id="steve", parent_thread_id="p2",
            thread_id="11111111-2222-3333-4444-555555555555",
            model="claude-code/sonnet", db=child_db,
        )
        assert cid == "11111111-2222-3333-4444-555555555555"


class TestCanonicalPersistence:
    def _child(self, db):
        from subagents.child_thread import create_child_thread
        _seed_parent(db)
        return create_child_thread(
            user_id="steve",
            parent_thread_id="parent",
            model="claude-code/sonnet",
            db=db,
        )

    def _modelled_child(self, db):
        from subagents.child_thread import create_child_thread

        _seed_parent(db)
        return create_child_thread(
            user_id="steve",
            parent_thread_id="parent",
            model="claude-code/tiny",
            db=db,
        )

    def test_assistant_text_with_reasoning(self, child_db):
        from subagents.child_thread import persist_assistant_text
        cid = self._child(child_db)
        persist_assistant_text(
            cid, "hello **md**", turn_id=_TURN_ID,
            reasoning="thought hard", db=child_db,
        )
        m = _rows(child_db, cid)[0]
        assert m["role"] == "assistant"
        assert m["content"] == "hello **md**"
        assert _meta(m)["reasoning"] == "thought hard"
        assert _meta(m)["cc"] is True

    def test_assistant_text_without_reasoning(self, child_db):
        from subagents.child_thread import persist_assistant_text
        cid = self._child(child_db)
        persist_assistant_text(cid, "plain", turn_id=_TURN_ID, db=child_db)
        assert "reasoning" not in (_meta(_rows(child_db, cid)[0]) or {})

    def test_assistant_text_can_mark_codex_provider(self, child_db):
        from subagents.child_thread import persist_assistant_text
        cid = self._child(child_db)
        persist_assistant_text(
            cid, "plain", turn_id=_TURN_ID, db=child_db, provider="codex",
        )
        meta = _meta(_rows(child_db, cid)[0])
        assert meta["codex"] is True
        assert "cc" not in meta

    def test_assistant_metadata_uses_reported_identity_and_exposes_mismatch(self):
        from subagents.child_thread import provider_message_metadata

        meta = provider_message_metadata(
            "cc",
            model="claude-opus-4-8",
            expected_model="claude-fable-5",
        )

        assert meta["model"] == "claude-code/claude-opus-4-8"
        assert meta["provider_reported_model"] == "claude-code/claude-opus-4-8"
        assert meta["expected_provider_model"] == "claude-code/claude-fable-5"
        assert meta["model_identity_mismatch"] is True

    def test_assistant_metadata_marks_missing_provider_identity(self):
        from subagents.child_thread import provider_message_metadata

        meta = provider_message_metadata(
            "codex", model=None, expected_model="gpt-5.6-sol",
        )

        assert "model" not in meta
        assert meta["expected_provider_model"] == "codex/gpt-5.6-sol"
        assert meta["provider_reported_model_missing"] is True

    def test_codex_metadata_marker_is_not_the_legacy_cc_marker(self, child_db):
        from subagents.child_thread import persist_assistant_text, persist_tool_use

        cid = self._child(child_db)
        persist_assistant_text(
            cid, "plain", turn_id=_TURN_ID, db=child_db, provider="codex",
        )
        persist_tool_use(
            cid, turn_id=_TURN_ID, tool_id="toolu_1", name="Bash",
            arguments={"command": "ls"}, db=child_db, provider="codex",
        )

        rows = _rows(child_db, cid)
        for row in rows:
            meta = _meta(row)
            assert meta["codex"] is True
            assert "cc" not in meta

    def test_tool_use_canonical_shape(self, child_db):
        from subagents.child_thread import persist_tool_use
        cid = self._child(child_db)
        persist_tool_use(
            cid, turn_id=_TURN_ID, tool_id="toolu_1", name="Bash",
            arguments={"command": "ls"}, hint="list files", db=child_db,
        )
        m = _rows(child_db, cid)[0]
        meta = _meta(m)
        assert m["role"] == "assistant" and m["content"] == ""
        assert meta["step_type"] == "tool_calls"
        tc = meta["llm_tool_calls"][0]
        assert tc["id"] == "toolu_1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "Bash"
        assert json.loads(tc["function"]["arguments"]) == {"command": "ls"}
        assert meta["tool_hints"] == ["list files"]

    def test_mixed_assistant_step_persists_text_and_tool_batch_once(self, child_db):
        from subagents.child_thread import persist_assistant_step

        cid = self._child(child_db)
        step_id = persist_assistant_step(
            cid,
            turn_id=_TURN_ID,
            content="I will inspect both files.",
            tool_uses=[
                {
                    "id": "toolu_1",
                    "name": "Read",
                    "arguments": {"file_path": "/a.md"},
                    "hint": "/a.md",
                },
                {
                    "id": "toolu_2",
                    "name": "Read",
                    "arguments": {"file_path": "/b.md"},
                    "hint": "/b.md",
                },
            ],
            reasoning="checking",
            db=child_db,
        )

        rows = _rows(child_db, cid)
        assert len(rows) == 1
        assert rows[0]["id"] == step_id
        assert rows[0]["role"] == "assistant"
        assert rows[0]["content"] == "I will inspect both files."
        meta = _meta(rows[0])
        assert meta["step_type"] == "tool_calls"
        assert [call["id"] for call in meta["llm_tool_calls"]] == [
            "toolu_1", "toolu_2",
        ]
        assert meta["reasoning"] == "checking"

    def test_tool_use_can_mark_codex_provider(self, child_db):
        from subagents.child_thread import persist_tool_use
        cid = self._child(child_db)
        persist_tool_use(
            cid, turn_id=_TURN_ID, tool_id="toolu_1", name="Bash",
            arguments={"command": "ls"}, hint="list files", db=child_db,
            provider="codex",
            model="gpt-5.6-sol",
            expected_model="gpt-5.6-terra",
        )
        meta = _meta(_rows(child_db, cid)[0])
        assert meta["codex"] is True
        assert "cc" not in meta
        assert meta["provider_reported_model"] == "codex/gpt-5.6-sol"
        assert meta["expected_provider_model"] == "codex/gpt-5.6-terra"
        assert meta["model_identity_mismatch"] is True

    def test_tool_result_canonical_shape(self, child_db):
        from subagents.child_thread import persist_tool_result
        cid = self._child(child_db)
        step_id = _tool_step(child_db, cid, "toolu_1")
        persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="toolu_1", name="Bash",
            content="file.txt", assistant_step_id=step_id, db=child_db,
        )
        m = _tool_rows(child_db, cid)[0]
        assert m["role"] == "tool"
        assert m["content"] == "file.txt"
        meta = _meta(m)
        assert meta["tool_call_id"] == "toolu_1"
        assert meta["provider_tool_call_id"] == "toolu_1"
        assert meta["name"] == "Bash"
        assert meta["tool_invocation_id"] == "provider:toolu_1"

    def test_oversized_tool_result_is_byte_exact_with_integrity_stamp(
        self, isolated_db,
    ):
        from infra.database import get_db
        from subagents.child_thread import persist_tool_result

        db = get_db()
        cid = self._modelled_child(db)
        content = "H" * 256 + "T" * 256
        step_id = _tool_step(db, cid, "toolu_1")
        persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="toolu_1", name="Bash",
            content=content, assistant_step_id=step_id, db=db,
        )

        row = _tool_rows(db, cid)[0]
        assert row["content"] == content
        meta = _meta(row)
        assert meta["tool_result_byte_count"] == len(content.encode("utf-8"))
        assert meta["tool_result_sha256"] == hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()

    def test_tool_result_at_former_cap_is_byte_identical(
        self, isolated_db,
    ):
        from infra.database import get_db
        from subagents.child_thread import persist_tool_result

        db = get_db()
        cid = self._modelled_child(db)
        content = "x" * 128
        step_id = _tool_step(db, cid, "toolu_1")
        persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="toolu_1", name="Bash",
            content=content, assistant_step_id=step_id, db=db,
        )

        assert _tool_rows(db, cid)[0]["content"] == content

    def test_normal_tool_result_is_byte_identical(self, isolated_db):
        from infra.database import get_db
        from subagents.child_thread import persist_tool_result

        db = get_db()
        cid = self._modelled_child(db)
        content = "file.txt\n"
        step_id = _tool_step(db, cid, "toolu_1")
        persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="toolu_1", name="Bash",
            content=content, assistant_step_id=step_id, db=db,
        )

        assert _tool_rows(db, cid)[0]["content"] == content

    def test_tool_result_persistence_does_not_resolve_model_capacity(
        self, child_db, monkeypatch,
    ):
        from llm import resolution
        from subagents.child_thread import persist_tool_result

        def forbidden(*_args, **_kwargs):
            raise AssertionError("persistence must not resolve model capacity")

        monkeypatch.setattr(resolution, "resolve_execution_target", forbidden)
        cid = self._child(child_db)
        content = "H" * 256 + "T" * 256
        step_id = _tool_step(child_db, cid, "toolu_1")
        persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="toolu_1", name="Bash",
            content=content, assistant_step_id=step_id, db=child_db,
        )

        row = _tool_rows(child_db, cid)[0]
        assert row["content"] == content
        assert _meta(row)["tool_result_byte_count"] == len(content.encode("utf-8"))

    def test_tool_call_persistence_is_deduped_by_stable_corsia_id(self, child_db):
        from subagents.child_thread import persist_tool_use

        cid = self._child(child_db)
        first = persist_tool_use(
            cid, turn_id=_TURN_ID, tool_id="provider-tool-1", name="Bash",
            arguments={"command": "ls"}, hint="list files", db=child_db,
            provider="codex",
        )
        second = persist_tool_use(
            cid, turn_id=_TURN_ID, tool_id="provider-tool-1", name="Bash",
            arguments={"command": "ls"}, hint="list files", db=child_db,
            provider="codex",
        )

        rows = _rows(child_db, cid)
        assert len(rows) == 1
        assert first == second == rows[0]["id"]
        meta = _meta(rows[0])
        assert meta["corsia_tool_call_id"].startswith("mtc_")
        assert meta["provider_tool_call_id"] == "provider-tool-1"

    def test_provider_tool_use_seeks_turn_index_and_appends_matching_step(self, child_db):
        from subagents.child_thread import persist_tool_use

        cid = self._child(child_db)
        target_id = "provider-step"
        target_metadata = json.dumps({
            "turn_id": _TURN_ID,
            "provider_turn_id": "provider-turn",
            "llm_tool_calls": [],
        })
        noise_metadata = json.dumps({
            "turn_id": _TURN_ID,
            "provider_turn_id": "another-provider-turn",
            "llm_tool_calls": [],
        })
        child_db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (target_id, cid, "assistant", "", target_metadata, "t", 1),
        )
        child_db.executemany(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (f"noise-step-{number}", cid, "assistant", "", noise_metadata, "t", number)
                for number in range(2, 2_002)
            ],
        )
        child_db.commit()
        probe = _PlanCapturingConnection(child_db._conn)

        step_id = persist_tool_use(
            cid,
            turn_id=_TURN_ID,
            provider_turn_id="provider-turn",
            tool_id="long-thread-tool",
            name="Read",
            arguments={},
            db=probe,
        )

        assert step_id == target_id
        assert probe.provider_step_plans
        plan = "\n".join(probe.provider_step_plans[0])
        assert "SEARCH messages USING INDEX idx_messages_turn_calls" in plan
        assert "idx_messages_session_seq" not in plan
        target = next(row for row in _rows(child_db, cid) if row["id"] == target_id)
        assert _meta(target)["llm_tool_calls"][0]["id"] == "long-thread-tool"

    def test_tool_result_persistence_is_deduped_by_stable_corsia_id(self, child_db):
        from subagents.child_thread import persist_tool_result

        cid = self._child(child_db)
        step_id = _tool_step(child_db, cid, "provider-tool-1")
        first = persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="provider-tool-1", name="Bash",
            content="first", assistant_step_id=step_id, db=child_db,
        )
        second = persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="provider-tool-1", name="Bash",
            content="first", assistant_step_id=step_id, db=child_db,
        )

        rows = _tool_rows(child_db, cid)
        assert len(rows) == 1
        assert first == second == rows[0]["id"]
        assert rows[0]["content"] == "first"
        meta = _meta(rows[0])
        assert meta["tool_invocation_id"] == "provider:provider-tool-1"
        assert meta["provider_tool_call_id"] == "provider-tool-1"

    def test_external_stable_rows_use_uuid5_without_burning_sequence(self, child_db):
        from subagents.child_thread import persist_tool_result, persist_tool_use

        cid = self._child(child_db)
        call_id = persist_tool_use(
            cid,
            turn_id=_TURN_ID,
            tool_id="provider-stable-1",
            name="Bash",
            arguments={"command": "pwd"},
            db=child_db,
        )
        replayed_call_id = persist_tool_use(
            cid,
            turn_id=_TURN_ID,
            tool_id="provider-stable-1",
            name="Bash",
            arguments={"command": "pwd"},
            db=child_db,
        )
        result_id = persist_tool_result(
            cid,
            turn_id=_TURN_ID,
            tool_use_id="provider-stable-1",
            name="Bash",
            content="/workspace",
            assistant_step_id=call_id,
            db=child_db,
        )
        replayed_result_id = persist_tool_result(
            cid,
            turn_id=_TURN_ID,
            tool_use_id="provider-stable-1",
            name="Bash",
            content="/workspace",
            assistant_step_id=call_id,
            db=child_db,
        )

        assert replayed_call_id == call_id
        assert replayed_result_id == result_id
        assert uuid.UUID(call_id).version == 5
        assert uuid.UUID(result_id).version == 5
        rows = child_db.execute(
            "SELECT id,seq FROM messages WHERE thread_id=? ORDER BY seq", (cid,)
        ).fetchall()
        assert [(row["id"], row["seq"]) for row in rows] == [
            (call_id, 1),
            (result_id, 2),
        ]
        assert child_db.execute(
            "SELECT next_message_seq FROM threads WHERE id=?", (cid,)
        ).fetchone()[0] == 3

    def test_owned_replay_insert_rejects_dirty_pooled_connection(self, child_db):
        from subagents.child_thread import persist_tool_result

        cid = self._child(child_db)
        step_id = _tool_step(child_db, cid, "dirty-new-result", name="Read")
        child_db.commit()
        child_db.execute(
            "UPDATE threads SET title='owned elsewhere' WHERE id=?", (cid,)
        )

        with pytest.raises(RuntimeError, match="clean pooled connection"):
            persist_tool_result(
                cid,
                turn_id=_TURN_ID,
                tool_use_id="dirty-new-result",
                name="Read",
                content="must not commit caller work",
                assistant_step_id=step_id,
            )

        assert child_db.in_transaction is True
        assert child_db.execute(
            "SELECT title FROM threads WHERE id=?", (cid,)
        ).fetchone()[0] == "owned elsewhere"
        assert child_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?", (cid,)
        ).fetchone()[0] == 1
        child_db.rollback()

    def test_owned_replay_lookup_rejects_dirty_pooled_connection(self, child_db):
        from subagents.child_thread import persist_tool_result

        cid = self._child(child_db)
        step_id = _tool_step(child_db, cid, "dirty-existing-result", name="Read")
        result_id = persist_tool_result(
            cid,
            turn_id=_TURN_ID,
            tool_use_id="dirty-existing-result",
            name="Read",
            content="durable",
            assistant_step_id=step_id,
            db=child_db,
        )
        child_db.commit()
        child_db.execute(
            "UPDATE threads SET title='owned elsewhere' WHERE id=?", (cid,)
        )

        with pytest.raises(RuntimeError, match="clean pooled connection"):
            persist_tool_result(
                cid,
                turn_id=_TURN_ID,
                tool_use_id="dirty-existing-result",
                name="Read",
                content="durable",
                assistant_step_id=step_id,
            )

        assert child_db.in_transaction is True
        assert child_db.execute(
            "SELECT title FROM threads WHERE id=?", (cid,)
        ).fetchone()[0] == "owned elsewhere"
        stored = child_db.execute(
            "SELECT id,content FROM messages WHERE thread_id=? AND role='tool'",
            (cid,),
        ).fetchone()
        assert (stored["id"], stored["content"]) == (result_id, "durable")
        child_db.rollback()

    def test_legacy_external_ids_remain_discoverable_without_rewriting(self, child_db):
        from subagents.child_thread import (
            corsia_tool_call_id,
            persist_assistant_step,
            persist_legacy_import_tool_result,
            persist_tool_use,
        )

        cid = self._child(child_db)
        mtc_id = corsia_tool_call_id(
            cid, provider_tool_id="legacy-provider-1", name="Read"
        )
        assert mtc_id is not None
        legacy_call_id = f"{mtc_id}_call"
        legacy_result_id = f"{mtc_id}_result"
        batch_ids = ("legacy-batch-1", "legacy-batch-2")
        batch_key = "\0".join(batch_ids)
        batch_digest = hashlib.sha256(
            f"{cid}\0assistant-step\0{batch_key}".encode()
        ).hexdigest()[:24]
        legacy_batch_id = f"mas_{batch_digest}"
        child_db.executemany(
            "INSERT INTO messages "
            "(id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (legacy_call_id, cid, "assistant", "", "{}", "t1", 1),
                (legacy_result_id, cid, "tool", "legacy", "{}", "t2", 2),
                (legacy_batch_id, cid, "assistant", "batch", "{}", "t3", 3),
            ],
        )
        child_db.execute(
            "UPDATE threads SET next_message_seq=4 WHERE id=?", (cid,)
        )
        child_db.commit()

        assert persist_tool_use(
            cid,
            turn_id=_TURN_ID,
            tool_id="legacy-provider-1",
            name="Read",
            arguments={"file_path": "/legacy"},
            db=child_db,
        ) == legacy_call_id
        assert persist_legacy_import_tool_result(
            cid,
            tool_use_id="legacy-provider-1",
            name="Read",
            content="replayed",
            assistant_step_id=legacy_call_id,
            db=child_db,
        ) == legacy_result_id
        assert persist_assistant_step(
            cid,
            turn_id=_TURN_ID,
            content="batch replay",
            tool_uses=[
                {"id": batch_ids[0], "name": "Read", "arguments": {}},
                {"id": batch_ids[1], "name": "Read", "arguments": {}},
            ],
            db=child_db,
        ) == legacy_batch_id

        assert child_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?", (cid,)
        ).fetchone()[0] == 3
        assert child_db.execute(
            "SELECT next_message_seq FROM threads WHERE id=?", (cid,)
        ).fetchone()[0] == 4

    def test_live_result_after_legacy_import_uses_canonical_invocation_identity(self, child_db):
        from subagents.child_thread import (
            load_assistant_step_display,
            persist_legacy_import_tool_result,
            persist_tool_result,
            persist_tool_use,
        )

        cid = self._child(child_db)
        step_id = persist_tool_use(
            cid,
            turn_id=_TURN_ID,
            tool_id="legacy-replay-1",
            name="Read",
            arguments={"file_path": "/legacy.md"},
            db=child_db,
        )
        legacy_result_id = persist_legacy_import_tool_result(
            cid,
            tool_use_id="legacy-replay-1",
            name="Read",
            content="legacy contents",
            assistant_step_id=step_id,
            db=child_db,
        )
        replayed_id = persist_tool_result(
            cid,
            turn_id=_TURN_ID,
            tool_use_id="legacy-replay-1",
            name="Read",
            content="legacy contents",
            assistant_step_id=step_id,
            db=child_db,
        )

        assert replayed_id != legacy_result_id
        step = load_assistant_step_display(cid, step_id, db=child_db)
        assert step["metadata"]["tool_calls"][0]["result"] == "legacy contents"

    def test_seed_and_user_frame(self, child_db):
        from subagents.child_thread import record_seed, record_user_frame
        cid = self._child(child_db)
        record_seed(cid, "you are a subagent", db=child_db)
        record_user_frame(cid, "do the task", turn_id=_TURN_ID, db=child_db)
        rows = _rows(child_db, cid)
        assert rows[0]["role"] == "system" and _meta(rows[0])["type"] == "seed"
        assert rows[1]["role"] == "user" and _meta(rows[1])["cc"] is True
        assert "edited" not in _meta(rows[1])

    def test_user_frame_edited_flag(self, child_db):
        from subagents.child_thread import record_user_frame
        cid = self._child(child_db)
        record_user_frame(
            cid, "edited reply", turn_id=_TURN_ID, db=child_db, edited=True,
        )
        m = _rows(child_db, cid)[0]
        assert m["role"] == "user"
        meta = _meta(m)
        assert meta["cc"] is True and meta["edited"] is True

    def test_record_user_frame_extra_meta_merges_without_clobbering_cc(self, child_db):
        from subagents.child_thread import record_user_frame
        cid = self._child(child_db)
        attribution = {
            "sender_kind": "agent",
            "from_thread": "conductor-1",
            "from_title": "Conductor",
        }
        record_user_frame(
            cid, "run the build", turn_id=_TURN_ID,
            extra_meta=attribution, db=child_db,
        )
        rows = _rows(child_db, cid)
        assert len(rows) == 1
        meta = _meta(rows[0])
        assert meta["cc"] is True
        assert meta["sender_kind"] == "agent"
        assert meta["from_thread"] == "conductor-1"
        assert meta["from_title"] == "Conductor"

    def test_display_roundtrip_collapses_tools(self, child_db):
        from core.message_display import llm_to_display
        from subagents.child_thread import (
            persist_assistant_text, persist_tool_result, persist_tool_use,
        )
        cid = self._child(child_db)
        step_id = persist_tool_use(
            cid, turn_id=_TURN_ID, tool_id="t1", name="Read",
            arguments={"file_path": "/a.md"}, hint="/a.md", db=child_db,
        )
        persist_tool_result(
            cid, turn_id=_TURN_ID, tool_use_id="t1", name="Read", content="contents",
            assistant_step_id=step_id, db=child_db,
        )
        persist_assistant_text(
            cid, "done reading", turn_id=_TURN_ID, db=child_db,
        )

        display = llm_to_display(_rows(child_db, cid))
        assistants = [d for d in display if d.get("role") == "assistant"]
        assert len(assistants) == 2
        meta = json.loads(assistants[0]["metadata"])
        assert assistants[0]["content"] == ""
        assert meta["tool_calls"][0]["id"] == "t1"
        assert meta["tool_calls"][0]["result"] == "contents"
        assert assistants[1]["content"] == "done reading"

        result_meta = _meta(next(row for row in _rows(child_db, cid) if row["role"] == "tool"))
        assert result_meta["assistant_step_id"] == step_id
