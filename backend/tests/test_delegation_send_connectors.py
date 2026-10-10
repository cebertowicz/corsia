from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from core.turn_core import TurnIdentity
from delegation_fixtures import _TOKEN, client, deleg_db, insert_thread


def _accepted(turn_id: str) -> TurnIdentity:
    return TurnIdentity(turn_id, 1)


def _cc_accepting() -> AsyncMock:
    async def _accept(*_args, **kwargs):
        await kwargs["persist_input_once"]()
        kwargs["mark_accepted"]()
        await kwargs["settle_delivery"]()
        return kwargs.get("preadmitted_turn") or _accepted(kwargs["turn_id"])

    return AsyncMock(side_effect=_accept)


def _codex_accepting(owner_turn_id: str | None = None) -> AsyncMock:
    async def _accept(*_args, **kwargs):
        successor_turn_id = kwargs["successor_turn_id"]
        preadmitted_turn = kwargs.get("preadmitted_turn")
        accepted_turn_id = (
            preadmitted_turn.turn_id
            if preadmitted_turn is not None
            else owner_turn_id or successor_turn_id
        )
        await kwargs["bind_input_turn"](
            accepted_turn_id,
            successor_turn_id if accepted_turn_id != successor_turn_id else None,
        )
        kwargs["mark_accepted"]()
        await kwargs["settle_delivery"]()
        return preadmitted_turn or _accepted(accepted_turn_id)

    return AsyncMock(side_effect=_accept)


def _assert_visible_failed_row(db, thread_id: str, content: str) -> None:
    """A refused send remains visible and ends with proved-no-write truth."""
    rows = db.execute(
        "SELECT m.role,m.content,m.delivery_state,m.delivery_error,m.seq,"
        "m.write_outcome,t.status AS turn_status FROM messages m "
        "LEFT JOIN active_turns t ON t.id=m.active_turn_id "
        "WHERE m.thread_id=? AND m.role='user'",
        (thread_id,),
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["content"] == content
    assert row["delivery_state"] == "failed"
    assert row["delivery_error"]
    assert row["write_outcome"] == "proved_no_write"
    assert isinstance(row["seq"], int)
    assert row["turn_status"] == "error"


class TestSendConnectorTurns:
    @pytest.fixture(autouse=True)
    def _codex_connection_available(self, monkeypatch):
        from subagents import codex_runtime
        monkeypatch.setattr(
            codex_runtime, "ensure_connection_available", lambda db=None: None
        )

    @pytest.fixture(autouse=True)
    def _isolated_cc_home(self, monkeypatch, tmp_path):
        # Send preflight (prepare_cc_destination_payload) reaches
        # subagents.cc_sessions.resolve_claude_dir() even when cc_runtime.feed
        # itself is mocked. Isolate it, or it reads/writes the developer's
        # real ~/.claude.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    def test_send_into_cc_subagent_feeds(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "sub1", model="claude-code/sonnet",
                           spawned_by="parent")
        from subagents import cc_runtime
        feed = _cc_accepting()
        feed_main = AsyncMock()
        monkeypatch.setattr(cc_runtime, "feed", feed)
        monkeypatch.setattr(cc_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/sub1/send",
            params={"token": _TOKEN},
            json={"message": "go", "from_thread_id": "sender-self"})
        assert r.status_code == 200, r.text
        assert r.json()["driver"] == "cc_runtime.feed"
        feed.assert_awaited_once()
        feed_main.assert_not_awaited()

    def test_codex_missing_project_cwd_blocks_before_provider(
            self, client, deleg_db, monkeypatch):
        # A real project whose working directory cannot resolve: a relative
        # legacy path names no project folder. A missing row is held earlier
        # by account admission (test_vendor_account_lifecycle_fifo).
        deleg_db.execute(
            "INSERT INTO projects (id, name, path, created_at, updated_at) "
            "VALUES ('missing-project', 'Missing project', 'relative/missing-project', 'now', 'now')"
        )
        insert_thread(
            deleg_db,
            "codex-missing-cwd",
            model="codex/gpt-5-codex",
            project_id="missing-project",
            spawned_by=None,
        )
        deleg_db.execute(
            "UPDATE threads SET project_id = ? WHERE id = 'sender-self'",
            ("missing-project",),
        )
        deleg_db.commit()
        from subagents import codex_runtime

        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=False))
        feed_main = AsyncMock(return_value=_accepted("turn-codex-fresh"))
        monkeypatch.setattr(codex_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/codex-missing-cwd/send",
            params={"token": _TOKEN},
            json={"message": "go", "from_thread_id": "sender-self"},
        )

        assert r.status_code == 422
        assert "Codex turn blocked" in r.json()["detail"]
        assert "Project working directory unresolved" in r.json()["detail"]
        feed_main.assert_not_awaited()
        _assert_visible_failed_row(deleg_db, "codex-missing-cwd", "go")

    def test_unaccepted_provider_rejection_retains_queued_row_without_system_row(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "tiny-cc-fail", model="claude-code/sonnet",
                           project_id=None)
        from subagents import cc_runtime
        async def _persist_then_reject(*_args, **kwargs):
            await kwargs["persist_input_once"]()
            raise FileNotFoundError("claude missing")

        feed_main = AsyncMock(side_effect=_persist_then_reject)
        monkeypatch.setattr(cc_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/tiny-cc-fail/send",
            params={"token": _TOKEN},
            json={"message": "continue", "from_thread_id": "sender-self"},
        )

        assert r.status_code == 502
        # The user's thought is never deleted; the proved-no-write failure
        # releases the delivery fence so the same row stays queued/retryable.
        _assert_visible_failed_row(deleg_db, "tiny-cc-fail", "continue")
        non_user = deleg_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = 'tiny-cc-fail' "
            "AND role != 'user'"
        ).fetchall()
        assert len(non_user) == 1
        assert json.loads(non_user[0]["metadata"])["event_type"] == "context.digest"

    def test_send_into_cc_main_feeds_main(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "main1", model="claude-code/opus",
                           project_id=None)
        from subagents import cc_runtime
        feed_main = _cc_accepting()
        monkeypatch.setattr(cc_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/main1/send",
            params={"token": _TOKEN},
            json={"message": "hello", "from_thread_id": "sender-self"})
        assert r.status_code == 200
        assert r.json()["driver"] == "cc_runtime.feed_main"
        feed_main.assert_awaited_once()
        _, kwargs = feed_main.call_args
        assert kwargs["target"].catalog_id == "claude-code/opus"
        assert kwargs["target"].transport_model == "opus"

    def test_send_cc_main_persists_attributed_user_row_before_feed_main(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "ccmain2", model="claude-code/opus",
                           project_id=None)
        deleg_db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, title, created_at, updated_at) "
            "VALUES ('sender-ccmain', 'u-test', 'claude-code/opus', "
            "'Conductor', 'now', 'now')"
        )
        deleg_db.commit()

        from subagents import cc_runtime
        feed_main = _cc_accepting()
        monkeypatch.setattr(cc_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/ccmain2/send",
            params={"token": _TOKEN},
            json={
                "message": "steer the conductor",
                "from_thread_id": "sender-ccmain",
                "from_title": "SPOOFED",
            },
        )
        assert r.status_code == 200
        assert r.json()["driver"] == "cc_runtime.feed_main"
        feed_main.assert_awaited_once()

        rows = deleg_db.execute(
            "SELECT content, metadata FROM messages WHERE thread_id = 'ccmain2' "
            "AND role='user'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["content"] == "steer the conductor"
        meta = json.loads(rows[0]["metadata"])
        assert meta["sender_kind"] == "agent"
        assert meta["from_thread"] == "sender-ccmain"
        assert meta["from_title"] == "Conductor"

    def test_send_into_resumable_codex_subagent_feeds(
        self, client, deleg_db, monkeypatch, tmp_path
    ):
        from infra.config import WORKSPACE
        from tests.project_ref_helpers import project_ref, select_workspace

        select_workspace(monkeypatch, WORKSPACE)
        context = WORKSPACE / "projects" / "project-1" / "context"
        context.mkdir(parents=True, exist_ok=True)
        (context / "PROJECT.md").write_text("project", encoding="utf-8")
        (context / "COORDINATION-MODEL.md").write_text("model", encoding="utf-8")
        deleg_db.execute(
            "INSERT INTO projects (id, name, path, created_at, updated_at) "
            "VALUES ('project-1', 'Project 1', ?, 'now', 'now')",
            (project_ref("project-1"),),
        )
        insert_thread(
            deleg_db,
            "cxsub",
            model="codex/gpt-5-codex",
            project_id="project-1",
            spawned_by="parent",
        )
        deleg_db.execute(
            "UPDATE threads SET project_id='project-1' WHERE id='sender-self'"
        )
        deleg_db.commit()
        from subagents import codex_runtime
        feed = _codex_accepting()
        feed_main = _codex_accepting()
        monkeypatch.setitem(
            codex_runtime._CODEX_THREADS, "cxsub", "provider-thread-cxsub"
        )
        monkeypatch.setattr(codex_runtime, "feed", feed)
        monkeypatch.setattr(codex_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/cxsub/send",
            params={"token": _TOKEN},
            json={"message": "do the doc work", "from_thread_id": "sender-self"})
        assert r.status_code == 200, r.text
        assert r.json()["driver"] == "codex_runtime.feed"
        feed.assert_awaited_once()
        feed_main.assert_not_awaited()
        args, kwargs = feed.call_args
        # Attributed send → model-bound text is framed; display row stays raw.
        assert args[0] == "cxsub"
        assert (
            '[AGENT-SEND — from "Sender Thread" (thread sender-self), '
            "scope project-1]" in args[1]
        )
        assert "do the doc work" in args[1]
        assert kwargs["project_id"] == "project-1"
        rows = deleg_db.execute(
            "SELECT content, role, metadata FROM messages WHERE thread_id = 'cxsub' "
            "AND role='user'").fetchall()
        assert len(rows) == 1
        assert rows[0]["role"] == "user" and rows[0]["content"] == "do the doc work"
        assert json.loads(rows[0]["metadata"])["from_thread"] == "sender-self"

    def test_send_new_codex_subagent_spawns_first_turn(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "par-codex-smoke", model="anthropic/claude-sonnet-4-6")
        monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *a, **k: None)
        r = client.post(
            "/api/delegation/threads",
            params={"token": _TOKEN},
            json={
                "title": "Codex smoke",
                "thread_kind": "subagent",
                "model": "codex/gpt-5-codex",
                "spawned_by": "par-codex-smoke",
            },
        )
        assert r.status_code == 200
        thread_id = r.json()["thread_id"]

        from subagents import codex_runtime
        feed = _codex_accepting()
        feed_main = _codex_accepting()
        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "feed", feed)
        monkeypatch.setattr(codex_runtime, "feed_main", feed_main)

        r = client.post(
            f"/api/delegation/threads/{thread_id}/send",
            params={"token": _TOKEN},
            json={"message": "first turn", "from_thread_id": "sender-self"})
        assert r.status_code == 200
        assert r.json()["driver"] == "codex_runtime.feed_main"
        feed.assert_not_awaited()
        feed_main.assert_awaited_once()
        args, kwargs = feed_main.call_args
        assert args[0] == thread_id and "first turn" in args[1]
        assert kwargs["project_id"] is None
        rows = deleg_db.execute(
            "SELECT content, role FROM messages WHERE thread_id = ? AND role='user'",
            (thread_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["role"] == "user" and rows[0]["content"] == "first turn"

    def test_send_existing_codex_subagent_without_thread_id_reconstructs(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cx-lost", model="codex/gpt-5-codex",
                           spawned_by="parent")
        from infra.queries import insert_message

        insert_message(
            "cx-lost",
            "assistant",
            "prior output",
            metadata={"turn_unbound_reason": "legacy_import"},
            msg_id="m-prior",
            db=deleg_db,
        )
        deleg_db.commit()

        from subagents import codex_runtime
        feed = _codex_accepting()
        feed_main = _codex_accepting()
        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=True))
        monkeypatch.setattr(codex_runtime, "feed", feed)
        monkeypatch.setattr(codex_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/cx-lost/send",
            params={"token": _TOKEN},
            json={"message": "continue", "from_thread_id": "sender-self"})
        assert r.status_code == 200
        assert r.json()["driver"] == "codex_runtime.feed_main"
        feed.assert_not_awaited()
        feed_main.assert_awaited_once()
        rows = deleg_db.execute(
            "SELECT role, content FROM messages WHERE thread_id = 'cx-lost' "
            "ORDER BY rowid"
        ).fetchall()
        assert [(r["role"], r["content"]) for r in rows] == [
            ("assistant", "prior output"),
            ("system", rows[1]["content"]),
            ("user", "continue"),
        ]
        assert "[PASSIVE CONTEXT DIGEST]" in rows[1]["content"]

    def test_send_into_codex_main_feeds_main(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cxmain", model="codex/gpt-5-codex",
                           project_id=None)
        from subagents import codex_runtime
        feed_main = _codex_accepting()
        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/cxmain/send",
            params={"token": _TOKEN},
            json={"message": "kick off", "from_thread_id": "sender-self"})
        assert r.status_code == 200
        assert r.json()["driver"] == "codex_runtime.feed_main"
        feed_main.assert_awaited_once()
        args, _ = feed_main.call_args
        assert "kick off" in args[1]
        rows = deleg_db.execute(
            "SELECT content FROM messages WHERE thread_id = 'cxmain' "
            "AND role='user'").fetchall()
        assert len(rows) == 1 and rows[0]["content"] == "kick off"

    def test_send_codex_not_resumable_returns_409(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cxdead", model="codex/gpt-5-codex",
                           spawned_by="parent")
        from subagents import codex_runtime
        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=True))
        monkeypatch.setitem(
            codex_runtime._CODEX_THREADS, "cxdead", "provider-thread-cxdead"
        )
        monkeypatch.setattr(codex_runtime, "feed", AsyncMock(return_value=False))
        r = client.post(
            "/api/delegation/threads/cxdead/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "sender-self"})
        assert r.status_code == 409
        _assert_visible_failed_row(deleg_db, "cxdead", "x")

    def test_send_codex_busy_steers_and_persists_row(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cxbusy", model="codex/gpt-5-codex",
                           spawned_by="parent")
        from subagents import codex_runtime
        feed = _codex_accepting("turn-codex-busy")
        monkeypatch.setattr(codex_runtime, "is_live", lambda _sid: True)
        monkeypatch.setitem(
            codex_runtime._CODEX_THREADS, "cxbusy", "provider-thread-cxbusy"
        )
        monkeypatch.setattr(codex_runtime, "feed", feed)

        r = client.post(
            "/api/delegation/threads/cxbusy/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "sender-self"})
        assert r.status_code == 200, r.text
        assert r.json()["driver"] == "codex_runtime.feed"
        feed.assert_awaited_once()
        rows = deleg_db.execute(
            "SELECT content, role FROM messages WHERE thread_id = 'cxbusy' "
            "AND role='user'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["role"] == "user" and rows[0]["content"] == "x"

    def test_send_codex_busy_steers_without_cc(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cxstale", model="codex/gpt-5-codex",
                           spawned_by="parent")
        from subagents import cc_runtime, codex_runtime
        codex_feed = _codex_accepting("turn-codex-stale")
        cc_feed = _cc_accepting()
        monkeypatch.setattr(codex_runtime, "is_live", lambda _sid: True)
        monkeypatch.setitem(
            codex_runtime._CODEX_THREADS, "cxstale", "provider-thread-cxstale"
        )
        monkeypatch.setattr(codex_runtime, "feed", codex_feed)
        monkeypatch.setattr(cc_runtime, "feed", cc_feed)

        r = client.post(
            "/api/delegation/threads/cxstale/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "sender-self"})
        assert r.status_code == 200, r.text
        assert r.json()["driver"] == "codex_runtime.feed"
        codex_feed.assert_awaited_once()
        cc_feed.assert_not_awaited()
        rows = deleg_db.execute(
            "SELECT content, role FROM messages WHERE thread_id = 'cxstale' "
            "AND role='user'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["role"] == "user" and rows[0]["content"] == "x"

    def test_send_codex_main_not_resumable_returns_409(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cxmaindead", model="codex/gpt-5-codex",
                           project_id=None)
        from subagents import codex_runtime
        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "feed_main", AsyncMock(return_value=False))
        r = client.post(
            "/api/delegation/threads/cxmaindead/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "sender-self"})
        assert r.status_code == 409
        _assert_visible_failed_row(deleg_db, "cxmaindead", "x")

    def test_send_codex_connection_unavailable_returns_503_retaining_queued_row(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cx-no-conn", model="codex/gpt-5-codex",
                           project_id=None)
        from subagents import codex_runtime
        feed_main = AsyncMock(return_value=_accepted("turn-no-connection"))

        def _unavailable(db=None):
            raise codex_runtime.CodexConnectionUnavailable(
                "Codex connection is not available: needs_auth"
            )

        monkeypatch.setattr(codex_runtime, "ensure_connection_available",
                            _unavailable)
        monkeypatch.setattr(codex_runtime, "feed_main", feed_main)

        r = client.post(
            "/api/delegation/threads/cx-no-conn/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "sender-self"})
        assert r.status_code == 503
        assert "Codex connection is not available" in r.json()["detail"]
        feed_main.assert_not_awaited()
        _assert_visible_failed_row(deleg_db, "cx-no-conn", "x")

    def test_send_codex_app_server_error_returns_502_retaining_queued_row(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "cx-turn-mismatch",
                           model="codex/gpt-5-codex",
                           project_id=None)
        from subagents import codex_runtime
        from subagents.codex_app_server import CodexAppServerError

        payload = {
            "code": -32600,
            "message": (
                "expected active turn id `old-turn` but found `new-turn`"
            ),
        }
        monkeypatch.setattr(codex_runtime, "ensure_connection_available",
                            lambda db=None: None)
        monkeypatch.setattr(codex_runtime, "has_thread_id",
                            AsyncMock(return_value=False))
        monkeypatch.setattr(codex_runtime, "has_prior_messages",
                            AsyncMock(return_value=False))
        monkeypatch.setattr(
            codex_runtime,
            "feed_main",
            AsyncMock(side_effect=CodexAppServerError(str(payload), payload)),
        )

        r = client.post(
            "/api/delegation/threads/cx-turn-mismatch/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "sender-self"})

        assert r.status_code == 502
        assert "Codex turn failed" in r.json()["detail"]
        _assert_visible_failed_row(deleg_db, "cx-turn-mismatch", "x")
