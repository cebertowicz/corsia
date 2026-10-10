from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from core.turn_core import TurnIdentity
from delegation_fixtures import _TOKEN, client, deleg_db, insert_thread


def _start_acknowledging_execute_mock() -> AsyncMock:
    async def _execute(
        *_args,
        start_ack=None,
        turn_id=None,
        persist_input_once=None,
        settle_delivery=None,
        **_kwargs,
    ):
        if persist_input_once is not None:
            await persist_input_once()
        if settle_delivery is not None:
            await settle_delivery()
        if start_ack is not None and not start_ack.done():
            start_ack.set_result(TurnIdentity(turn_id, 1))

    return AsyncMock(side_effect=_execute)


class TestSendEngineTurns:
    def test_send_404_for_unknown_thread(self, client, deleg_db):
        r = client.post(
            "/api/delegation/threads/nope/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "sender-self"},
        )
        assert r.status_code == 404

    def test_send_400_for_empty_message(self, client, deleg_db):
        insert_thread(deleg_db, "s2", model="claude-code/sonnet",
                           spawned_by="parent")
        r = client.post("/api/delegation/threads/s2/send",
                        params={"token": _TOKEN}, json={"message": "   "})
        assert r.status_code == 400

    def test_send_reports_durable_normal_inbox_queue_as_success(
        self,
        client,
        deleg_db,
        monkeypatch,
    ):
        """A busy /send must not turn a retained message into a 500."""
        insert_thread(
            deleg_db,
            "busy-inbox-target",
            model="codex/gpt-5-codex",
            spawned_by="parent",
        )
        from api import delegation
        from infra.send_types import InboxQueuedInput
        from infra.send_types import InputReceipt

        send = AsyncMock(return_value=InboxQueuedInput(InputReceipt(
            thread_id="busy-inbox-target",
            message_id="queued-send-message",
            input_order=4,
            delivery_revision=1,
            created_at="t4",
        )))
        monkeypatch.setattr(delegation, "send_thread_input", send)

        response = client.post(
            "/api/delegation/threads/busy-inbox-target/send",
            params={"token": _TOKEN},
            json={"message": "do this after your current work", "from_thread_id": "sender-self"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "status": "queued",
            "queue_kind": "messages",
            "thread_id": "busy-inbox-target",
            "duplicate": False,
            "queued": {
                "message_id": "queued-send-message",
                "queue_order": 4,
                "created_at": "t4",
                "delivery_revision": 1,
            },
        }
        send.assert_awaited_once()

    def test_send_tags_agent_metadata_on_cc_sub(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "ccsub1", model="claude-code/sonnet",
                           spawned_by="parent")
        insert_thread(
            deleg_db, "sender2", model="claude-code/opus", title="Coordinator",
        )

        from subagents import cc_runtime
        async def _accept_cc(*_args, **kwargs):
            await kwargs["persist_input_once"]()
            kwargs["mark_accepted"]()
            await kwargs["settle_delivery"]()
            return TurnIdentity(kwargs["turn_id"], 1)

        feed_mock = AsyncMock(side_effect=_accept_cc)
        monkeypatch.setattr(cc_runtime, "feed", feed_mock)

        r = client.post(
            "/api/delegation/threads/ccsub1/send",
            params={"token": _TOKEN},
            json={
                "message": "delegated cc task",
                "from_thread_id": "sender2",
                "from_title": "SPOOFED",
            },
        )
        assert r.status_code == 200
        assert r.json()["driver"] == "cc_runtime.feed"

        feed_mock.assert_awaited_once()
        _, kwargs = feed_mock.call_args
        assert kwargs.get("agent_meta") == {
            "sender_kind": "agent",
            "from_thread": "sender2",
            "from_title": "Coordinator",
            "scope": "workspace",
        }

    def test_cc_feed_unfeedable_is_409(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "sub2", model="claude-code/sonnet",
                           spawned_by="parent")
        from subagents import cc_runtime
        monkeypatch.setattr(cc_runtime, "feed", AsyncMock(return_value=False))
        r = client.post(
            "/api/delegation/threads/sub2/send",
            params={"token": _TOKEN},
            json={"message": "go", "from_thread_id": "sender-self"},
        )
        assert r.status_code == 409

    def test_cc_sub_dead_process_resume_failure_is_502_not_500(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "sub-dead", model="claude-code/sonnet",
                           spawned_by="parent")
        from subagents import cc_runtime
        monkeypatch.setattr(
            cc_runtime, "feed",
            AsyncMock(side_effect=FileNotFoundError("[Errno 2] claude: not found")),
        )
        r = client.post(
            "/api/delegation/threads/sub-dead/send",
            params={"token": _TOKEN},
            json={"message": "again", "from_thread_id": "sender-self"},
        )
        assert r.status_code == 502, r.text
        assert "Claude Code turn failed" in r.json()["detail"]

    def test_cc_sub_stale_cwd_returns_502(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "sub-stalecwd", model="claude-code/sonnet",
                           spawned_by="parent")
        from subagents import cc_runtime
        from subagents.cc_runtime_helpers import CCWorkingDirMissing
        monkeypatch.setattr(
            cc_runtime, "feed",
            AsyncMock(side_effect=CCWorkingDirMissing("/stale/path", "proj-x")),
        )
        r = client.post(
            "/api/delegation/threads/sub-stalecwd/send",
            params={"token": _TOKEN},
            json={"message": "again", "from_thread_id": "sender-self"},
        )
        assert r.status_code == 502, r.text
        assert "Claude Code turn failed" in r.json()["detail"]

    def test_cc_sub_unexpected_exception_is_not_masked_as_502(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "sub-bug", model="claude-code/sonnet",
                           spawned_by="parent")
        from subagents import cc_runtime
        monkeypatch.setattr(
            cc_runtime, "feed",
            AsyncMock(side_effect=ValueError("unexpected programming bug")),
        )
        with pytest.raises(ValueError, match="unexpected programming bug"):
            client.post(
                "/api/delegation/threads/sub-bug/send",
                params={"token": _TOKEN},
                json={"message": "x", "from_thread_id": "sender-self"},
            )

    def test_cc_main_spawn_failure_returns_502(self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "main-dead", model="claude-code/sonnet",
                           project_id=None)
        from subagents import cc_runtime
        monkeypatch.setattr(
            cc_runtime, "feed_main",
            AsyncMock(side_effect=FileNotFoundError("[Errno 2] claude: not found")),
        )
        r = client.post(
            "/api/delegation/threads/main-dead/send",
            params={"token": _TOKEN},
            json={"message": "steer", "from_thread_id": "sender-self"},
        )
        assert r.status_code == 502, r.text
        assert "Claude Code turn failed" in r.json()["detail"]

    def test_cc_main_unexpected_exception_surfaces_as_500(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "main-bug", model="claude-code/sonnet",
                           project_id=None)
        from subagents import cc_runtime
        monkeypatch.setattr(
            cc_runtime, "feed_main",
            AsyncMock(side_effect=ValueError("unexpected bug in feed_main")),
        )
        with pytest.raises(ValueError, match="unexpected bug in feed_main"):
            client.post(
                "/api/delegation/threads/main-bug/send",
                params={"token": _TOKEN},
                json={"message": "steer", "from_thread_id": "sender-self"},
            )

    def test_engine_turn_routes_and_persists_message(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "eng1", model="anthropic/claude-3-5-sonnet",
                           spawned_by="parent")

        execute_mock = _start_acknowledging_execute_mock()
        import ws.turn as ws_turn
        monkeypatch.setattr(ws_turn, "execute_turn", execute_mock)

        import asyncio
        created_tasks = []
        original_create_task = asyncio.create_task

        def capturing_create_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            created_tasks.append(task)
            return task

        monkeypatch.setattr(asyncio, "create_task", capturing_create_task)

        r = client.post("/api/delegation/threads/eng1/send",
                        params={"token": _TOKEN},
                        json={"message": "run the tests",
                              "from_thread_id": "sender-self"})
        assert r.status_code == 200
        body = r.json()
        assert body["driver"] == "turn_engine"
        assert execute_mock.await_args.kwargs["ingress"] == "delegation"

        row = deleg_db.execute(
            "SELECT role, content FROM messages WHERE thread_id = 'eng1' "
            "AND role='user'"
        ).fetchone()
        assert row is not None
        assert row["role"] == "user"
        assert row["content"] == "run the tests"

    def test_send_tags_agent_metadata_on_engine_turn(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "eng3", model="anthropic/claude-3-5-sonnet",
                           spawned_by="parent")
        insert_thread(
            deleg_db, "sender1", model="claude-code/opus", title="Conductor",
        )

        execute_mock = _start_acknowledging_execute_mock()
        import ws.turn as ws_turn
        monkeypatch.setattr(ws_turn, "execute_turn", execute_mock)

        r = client.post(
            "/api/delegation/threads/eng3/send",
            params={"token": _TOKEN},
            json={
                "message": "delegated task",
                "from_thread_id": "sender1",
                "from_title": "SPOOFED_LABEL",
            },
        )
        assert r.status_code == 200

        # Native driver (turn engine): the model-bound text is framed with the
        # envelope; the reply id is the sender session, not the spoofed label.
        content = execute_mock.call_args.kwargs["content"]
        assert content.startswith(
            "CORSIA is delivering 2 queued inbound inputs together."
        )
        assert "[1/2]\n[PASSIVE CONTEXT DIGEST]" in content
        assert content.endswith(
            "[2/2]\n"
            '[AGENT-SEND — from "Conductor" (thread sender1), scope workspace]\n'
            "delegated task\n"
            '[To reply, call delegate_send(thread_id="sender1", message="<your reply>")]'
        )

        row = deleg_db.execute(
            "SELECT role, content, metadata FROM messages WHERE thread_id = 'eng3' "
            "AND role='user'"
        ).fetchone()
        assert row is not None
        assert row["role"] == "user"
        assert row["content"] == "delegated task"  # display row stays raw
        meta = json.loads(row["metadata"])
        assert meta["sender_kind"] == "agent"
        assert meta["from_thread"] == "sender1"
        assert meta["from_title"] == "Conductor"
        assert meta["scope"] == "workspace"

    def test_send_inserts_sender_side_dispatch_breadcrumb(
            self, client, deleg_db, monkeypatch):
        insert_thread(
            deleg_db,
            "target1",
            model="anthropic/claude-3-5-sonnet",
            spawned_by="parent",
            title="Worker Target",
        )
        insert_thread(
            deleg_db,
            "sender-bc",
            model="claude-code/opus",
            title="Project Conductor",
        )

        broadcasts = []
        monkeypatch.setattr(
            "infra.state.broadcast_to_thread",
            lambda sid, payload: broadcasts.append((sid, payload)),
        )

        execute_mock = _start_acknowledging_execute_mock()
        import ws.turn as ws_turn
        monkeypatch.setattr(ws_turn, "execute_turn", execute_mock)

        r = client.post(
            "/api/delegation/threads/target1/send",
            params={"token": _TOKEN},
            json={"message": "please investigate", "from_thread_id": "sender-bc"},
        )
        assert r.status_code == 200, r.text

        rows = deleg_db.execute(
            "SELECT role, content, metadata FROM messages "
            "WHERE thread_id = 'sender-bc'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "system"
        assert row["content"] == "Sent to Worker Target"
        meta = json.loads(row["metadata"])
        assert meta == {
            "type": "send_ref",
            "target_thread_id": "target1",
            "target_title": "Worker Target",
            "target_kind": "subagent",
        }
        assert any(
            sid == "sender-bc"
            and payload["type"] == "message_added"
            and payload["message"]["metadata"]["type"] == "send_ref"
            and payload["message"]["metadata"]["target_thread_id"] == "target1"
            for sid, payload in broadcasts
        )

    def test_queued_send_defers_sender_side_dispatch_breadcrumb(
            self, client, deleg_db, monkeypatch):
        insert_thread(
            deleg_db,
            "target-queued",
            model="anthropic/claude-3-5-sonnet",
            spawned_by="parent",
            title="Queued Target",
        )
        insert_thread(
            deleg_db,
            "sender-queued",
            model="claude-code/opus",
            title="Project Conductor",
        )
        from infra.input_admission import accept_thread_input
        from infra.thread_inputs import start_idle_turn

        accept_thread_input(
            "target-queued",
            role="user",
            content="already running",
            intent="apply_now",
            message_id="target-queued-active-input",
            db=deleg_db,
        )
        assert start_idle_turn(
            "target-queued",
            turn_id="target-queued-active-turn",
            boot_epoch="test",
            started_at="now",
            db=deleg_db,
        ) is not None
        deleg_db.commit()

        response = client.post(
            "/api/delegation/threads/target-queued/send",
            params={"token": _TOKEN},
            json={
                "message": "wait for promotion",
                "from_thread_id": "sender-queued",
                "idempotency_key": "queued-delegation",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"
        assert deleg_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='sender-queued'"
        ).fetchone()[0] == 0
        # One queue: the held input is an ordinary queued message row in the
        # target's transcript, not a private side-table entry only a chip stack
        # could render.
        queued = deleg_db.execute(
            "SELECT content,delivery_state FROM messages "
            "WHERE thread_id='target-queued' AND active_turn_id IS NULL "
            "AND role='user' "
            "ORDER BY input_order"
        ).fetchone()
        assert queued is not None
        assert queued["content"] == "wait for promotion"
        assert queued["delivery_state"] == "queued"

    def test_send_agent_meta_403_for_unowned_sender(
            self, client, deleg_db, monkeypatch):
        insert_thread(deleg_db, "eng4", model="anthropic/claude-3-5-sonnet",
                           spawned_by="parent")
        insert_thread(
            deleg_db,
            "other-sender",
            user_id="other-user",
            model="claude-code/opus",
        )

        execute_mock = AsyncMock()
        import ws.turn as ws_turn
        monkeypatch.setattr(ws_turn, "execute_turn", execute_mock)

        r = client.post(
            "/api/delegation/threads/eng4/send",
            params={"token": _TOKEN},
            json={"message": "x", "from_thread_id": "other-sender"},
        )
        assert r.status_code == 403
        assert "from_thread_id" in r.json()["detail"]
        row = deleg_db.execute(
            "SELECT id FROM messages WHERE thread_id = 'eng4'"
        ).fetchone()
        assert row is None

    def test_send_without_attribution_rejected_before_persist(
            self, client, deleg_db, monkeypatch):
        # Bug #362: an un-attributed /send used to persist a role="user" row with
        # null metadata, which the renderer treats as a user-authored message.
        # It must now reject loud (400) BEFORE any persistence or dispatch.
        insert_thread(deleg_db, "eng5", model="anthropic/claude-3-5-sonnet",
                           spawned_by="parent")

        execute_mock = AsyncMock()
        import ws.turn as ws_turn
        monkeypatch.setattr(ws_turn, "execute_turn", execute_mock)

        r = client.post("/api/delegation/threads/eng5/send",
                        params={"token": _TOKEN}, json={"message": "plain"})
        assert r.status_code == 400
        assert "from_thread_id is required" in r.json()["detail"]

        # Nothing persisted, nothing dispatched.
        row = deleg_db.execute(
            "SELECT id FROM messages WHERE thread_id = 'eng5'"
        ).fetchone()
        assert row is None
        execute_mock.assert_not_called()

    def test_engine_busy_turn_queues_durable_input(
            self, client, deleg_db, monkeypatch):
        from core.turn_core import persist_turn_terminal
        from infra.input_admission import accept_thread_input
        from infra.thread_inputs import start_idle_turn

        insert_thread(deleg_db, "eng2", model="anthropic/claude-3-5-sonnet",
                           spawned_by="parent")

        accept_thread_input(
            "eng2",
            role="user",
            content="already running",
            intent="apply_now",
            message_id="eng2-active-input",
            db=deleg_db,
        )
        assert start_idle_turn(
            "eng2",
            turn_id="eng2-active-turn",
            boot_epoch="test",
            started_at="now",
            db=deleg_db,
        ) is not None
        deleg_db.commit()

        try:
            execute_mock = AsyncMock()
            import ws.turn as ws_turn
            monkeypatch.setattr(ws_turn, "execute_turn", execute_mock)

            r = client.post("/api/delegation/threads/eng2/send",
                            params={"token": _TOKEN},
                            json={"message": "concurrent",
                                  "from_thread_id": "sender-self"})
            assert r.status_code == 200
            assert r.json()["status"] == "queued"
            assert r.json()["queue_kind"] == "messages"
            execute_mock.assert_not_awaited()
            queued = deleg_db.execute(
                "SELECT content,delivery_state,delivery_error,seq FROM messages "
                "WHERE thread_id='eng2' AND active_turn_id IS NULL AND role='user'"
            ).fetchone()
            assert dict(queued) == {
                "content": "concurrent",
                "delivery_state": "queued",
                "delivery_error": None,
                "seq": None,
            }
        finally:
            persist_turn_terminal(
                "eng2-active-turn", "cancelled", db=deleg_db,
            )
            deleg_db.commit()
