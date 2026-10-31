
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.turn_core import TurnIdentity
from shared_send_fixtures import _install_fake_native_context_agent
from tests.event_routing_helpers import mk_session as _mk_session

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


async def _accept_cc_bound_turn(*_args, **kwargs):
    await kwargs["persist_input_once"]()
    kwargs["mark_accepted"]()
    await kwargs["settle_delivery"]()
    return kwargs.get("preadmitted_turn") or TurnIdentity(kwargs["turn_id"], 1)


async def _accept_codex_successor(*_args, **kwargs):
    turn_id = kwargs["successor_turn_id"]
    await kwargs["bind_input_turn"](turn_id, None)
    kwargs["mark_accepted"]()
    await kwargs["settle_delivery"]()
    return kwargs.get("preadmitted_turn") or TurnIdentity(turn_id, 1)


def _accept_codex_active(owner_turn_id: str):
    async def _accept(*_args, **kwargs):
        identity = kwargs["preadmitted_turn"]
        await kwargs["bind_input_turn"](identity.turn_id, None)
        kwargs["mark_accepted"]()
        await kwargs["settle_delivery"]()
        return identity

    return _accept


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


@pytest.fixture(autouse=True)
def _native_context_agent(monkeypatch):
    _install_fake_native_context_agent(monkeypatch)


@pytest.fixture(autouse=True)
def _codex_connection_available(monkeypatch):
    import subagents.codex_runtime as codex_runtime
    monkeypatch.setattr(
        codex_runtime, "ensure_connection_available", lambda db=None: None
    )



class TestDelivery:
    def test_event_idempotency_key_distinguishes_delivery_instances(self):
        from events.delivery import _event_idempotency_key

        details = {
            "repo": "o/r",
            "external_id": "o/r#1140",
            "check_suite_id": 41,
            "head_sha": "head-a",
            "check_suite_updated_at": "2026-08-16T10:00:00Z",
        }
        replay = _event_idempotency_key(
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            details,
            delivery_id="delivery-1",
        )
        assert replay == _event_idempotency_key(
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            details,
            delivery_id="delivery-1",
        )
        assert replay != _event_idempotency_key(
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            details,
            delivery_id="delivery-2",
        )
        assert _event_idempotency_key(
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            details,
        ) != _event_idempotency_key(
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            {**details, "head_sha": "head-b"},
        )

    def test_target_validity_uses_canonical_lifecycle(self, isolated_db):
        from events.delivery import thread_exists_and_active

        _mk_session("dormancy-target")
        _mk_session("user-target")
        _mk_session("purged-target")
        conn = sqlite3.connect(str(isolated_db))
        conn.execute(
            "UPDATE threads SET lifecycle_state='archived', "
            "archived_reason='dormancy' WHERE id='dormancy-target'"
        )
        conn.execute(
            "UPDATE threads SET lifecycle_state='archived', "
            "archived_reason='user' WHERE id='user-target'"
        )
        conn.execute(
            "UPDATE threads SET lifecycle_state='purged' WHERE id='purged-target'"
        )
        conn.commit()
        conn.close()

        assert thread_exists_and_active("dormancy-target") is True
        assert thread_exists_and_active("user-target") is False
        assert thread_exists_and_active("purged-target") is False
        assert thread_exists_and_active("missing-target") is False

    def test_event_input_is_self_describing(self):
        from events.delivery import format_event_input
        text = format_event_input(
            "webhook.github.pr_opened", "o/r#1 opened: Fix bug",
            {"repo": "o/r", "head_sha": "deadbeef"})
        assert text.startswith("[EVENT: webhook.github.pr_opened]")
        assert "Summary: o/r#1 opened" in text
        assert "deadbeef" in text  # structured details included

    def test_event_wake_materializes_one_visible_typed_system_input(self, isolated_db):
        """A FIFO wake is a system event chip with event dispatch semantics."""
        from events.delivery import enqueue_event_wake, format_event_input
        from infra.database import get_db

        _mk_session("queued-event-wake", model="codex/gpt-5-codex")
        details = {"guidance": "Verify durable state before repeating effects."}
        db = get_db()
        try:
            receipt = enqueue_event_wake(
                "queued-event-wake",
                "system.restart.resume",
                "Instance restarted — resuming registered work",
                details,
                metadata={"restart_registration_id": "registration-1"},
                message_id="restart-event-input",
                created_at="2026-08-04T00:00:00+00:00",
                db=db,
            )
            row = db.execute(
                "SELECT m.role,m.content,m.metadata,m.delivery_state,m.seq,"
                "m.target_model,m.delivery_options FROM messages AS m "
                "WHERE m.id=?",
                (receipt.message_id,),
            ).fetchone()
        finally:
            db.close()

        assert receipt.message_id == "restart-event-input"
        assert row is not None
        assert row["role"] == "system"
        assert row["content"] == format_event_input(
            "system.restart.resume",
            "Instance restarted — resuming registered work",
            details,
        )
        assert row["delivery_state"] == "queued"
        assert row["seq"] is None
        assert json.loads(row["metadata"]) == {
            "type": "context_event",
            "event_type": "system.restart.resume",
            "event_disposition": "wake",
            "summary": "Instance restarted — resuming registered work",
            "url": None,
            "restart_registration_id": "registration-1",
            "routed_event": "system.restart.resume",
        }
        assert row["target_model"] == "codex/gpt-5-codex"
        assert json.loads(row["delivery_options"]) == {
            "native_mode": "event",
            "attachments": [],
        }

    def test_event_wake_records_the_model_identity_delivery_compares_against(
        self, isolated_db,
    ):
        """The RESOLVED catalog id, not the raw string on the session row.

        These two spellings of one model are routinely different — a session
        can carry a transport string (``mock/test-model``) whose catalog row id
        is ``mock-test-model``. The delivery boundary compares the descriptor
        against the RESOLVED target, so storing the raw string made an event
        wake permanently unselectable: it sat at the head of the FIFO, every
        later user message queued silently behind it, and promotion declined
        `terminal_prefix_unplannable` on every sweep forever (journey J10).

        The pre-existing wake test cannot catch this — its model spells the
        same both ways, so it passes either way. This one differs on purpose.
        """
        from events.delivery import enqueue_event_wake
        from infra.database import get_db
        from infra.thread_inputs import start_idle_turn
        from llm.resolution import resolve_execution_target

        db = get_db()
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,"
            "litellm_model,display_name,backend,source,enabled,available,"
            "context_window,metadata_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("divergent-catalog-id", "native", "anthropic", "divergent",
             "raw/divergent-transport", "Divergent", "api", "test", 1, 1,
             200000, "{}", "t", "t"),
        )
        db.commit()
        db.close()
        # The session holds the TRANSPORT spelling, which is legal and common.
        _mk_session("divergent-wake", model="raw/divergent-transport")

        db = get_db()
        try:
            receipt = enqueue_event_wake(
                "divergent-wake",
                "system.restart.recovery",
                "Instance restarted — recovering an interrupted Turn",
                {"guidance": "Verify durable state."},
                metadata={"kind": "restart_recovery"},
                message_id="divergent-event-input",
                created_at="2026-08-10T00:00:00+00:00",
                db=db,
            )
            selected = start_idle_turn(
                "divergent-wake",
                turn_id="divergent-event-turn",
                boot_epoch="test",
                started_at="2026-08-10T00:00:01+00:00",
                db=db,
            )
            assert selected is not None
            target = resolve_execution_target(selected.target_model, db=db)
        finally:
            db.close()

        assert selected.target_model == "raw/divergent-transport"
        assert target.catalog_id == "divergent-catalog-id"
        assert len(selected.items) == 2
        assert "[PASSIVE CONTEXT DIGEST]" in selected.items[0].content
        assert selected.items[1].message_id == receipt.message_id

    @pytest.mark.asyncio
    async def test_cc_main_thread_delivered_via_feed_main(self, isolated_db):
        _mk_session("cc-main", model="claude-code/opus")
        import subagents.cc_runtime as cc_runtime
        from events.delivery import deliver_to_thread

        with patch.object(cc_runtime, "feed_main") as mock_feed_main, \
             patch.object(cc_runtime, "feed") as mock_feed:
            mock_feed_main.side_effect = _accept_cc_bound_turn
            ok = await deliver_to_thread("cc-main", "webhook.github.pr_opened",
                                         "summary", {"x": 1})
        assert ok is True
        mock_feed_main.assert_called_once()
        mock_feed.assert_not_called()

    @pytest.mark.asyncio
    async def test_cc_subagent_delivered_via_feed(self, isolated_db):
        _mk_session("cc-sub", spawned_by="parent", model="claude-code/sonnet")
        import subagents.cc_runtime as cc_runtime
        from events.delivery import deliver_to_thread

        with patch.object(
            cc_runtime, "feed", side_effect=_accept_cc_bound_turn,
        ) as mock_feed, \
             patch.object(cc_runtime, "feed_main") as mock_feed_main:
            ok = await deliver_to_thread("cc-sub", "webhook.github.pr_comment",
                                         "summary", None)
        assert ok is True
        mock_feed.assert_called_once()
        assert mock_feed.call_args.kwargs["agent_meta"] == {
            "type": "context_event",
            "event_type": "webhook.github.pr_comment",
            "event_disposition": "wake",
            "summary": "summary",
            "url": None,
            "routed_event": "webhook.github.pr_comment",
        }
        mock_feed_main.assert_not_called()

    @pytest.mark.asyncio
    async def test_codex_main_thread_delivered_via_feed_main(self, isolated_db):
        _mk_session("cx-main", model="codex/gpt-5-codex")
        import subagents.codex_runtime as codex_runtime
        from events.delivery import deliver_to_thread

        with patch.object(codex_runtime, "has_thread_id", AsyncMock(return_value=False)), \
             patch.object(codex_runtime, "has_prior_messages", AsyncMock(return_value=False)), \
             patch.object(codex_runtime, "feed_main", side_effect=_accept_codex_successor) as mock_feed_main, \
             patch.object(codex_runtime, "feed") as mock_feed:
            ok = await deliver_to_thread("cx-main", "webhook.github.pr_opened",
                                         "summary", {"x": 1})
        assert ok is True
        mock_feed_main.assert_called_once()
        mock_feed.assert_not_called()
        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        inputs = conn.execute(
            "SELECT content, metadata FROM messages "
            "WHERE thread_id='cx-main' AND role='system'"
        ).fetchall()
        conn.close()
        assert len(inputs) == 2
        assert json.loads(inputs[0]["metadata"])["event_type"] == "context.digest"
        event = next(row for row in inputs if "[EVENT:" in row["content"])
        assert event["content"].startswith("[EVENT: webhook.github.pr_opened]")
        assert json.loads(event["metadata"])["routed_event"] == "webhook.github.pr_opened"

    @pytest.mark.asyncio
    async def test_resumable_codex_subagent_delivered_via_feed(self, isolated_db):
        _mk_session("cx-sub", spawned_by="parent", model="codex/gpt-5-codex")
        import subagents.codex_runtime as codex_runtime
        from events.delivery import deliver_to_thread

        with patch.dict(
            codex_runtime._CODEX_THREADS,
            {"cx-sub": "provider-thread-cx-sub"},
        ), \
             patch.object(codex_runtime, "feed", side_effect=_accept_codex_successor) as mock_feed, \
             patch.object(codex_runtime, "feed_main") as mock_feed_main:
            ok = await deliver_to_thread("cx-sub", "webhook.github.pr_comment",
                                         "summary", None)
        assert ok is True
        mock_feed.assert_called_once()
        mock_feed_main.assert_not_called()
        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        inputs = conn.execute(
            "SELECT content, metadata FROM messages "
            "WHERE thread_id='cx-sub' AND role='system'"
        ).fetchall()
        conn.close()
        assert len(inputs) == 2
        assert json.loads(inputs[0]["metadata"])["event_type"] == "context.digest"
        event = next(row for row in inputs if "[EVENT:" in row["content"])
        assert event["content"].startswith("[EVENT: webhook.github.pr_comment]")
        assert json.loads(event["metadata"])["routed_event"] == "webhook.github.pr_comment"

    @pytest.mark.asyncio
    async def test_new_codex_subagent_event_spawns_first_turn(self, isolated_db):
        _mk_session("cx-new-sub", spawned_by="parent", model="codex/gpt-5-codex")
        import subagents.codex_runtime as codex_runtime
        from events.delivery import deliver_to_thread

        with patch.object(codex_runtime, "has_thread_id", new=AsyncMock(return_value=False)), \
             patch.object(codex_runtime, "has_prior_messages",
                          new=AsyncMock(return_value=False)), \
             patch.object(codex_runtime, "feed") as mock_feed, \
             patch.object(codex_runtime, "feed_main", side_effect=_accept_codex_successor) as mock_feed_main:
            ok = await deliver_to_thread("cx-new-sub", "webhook.github.pr_opened",
                                         "summary", None)
        assert ok is True
        mock_feed.assert_not_called()
        mock_feed_main.assert_called_once()
        assert mock_feed_main.call_args.kwargs["project_id"] is None
        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        inputs = conn.execute(
            "SELECT content, metadata FROM messages "
            "WHERE thread_id='cx-new-sub' AND role='system'"
        ).fetchall()
        conn.close()
        assert len(inputs) == 2
        assert json.loads(inputs[0]["metadata"])["event_type"] == "context.digest"
        event = next(row for row in inputs if "[EVENT:" in row["content"])
        assert event["content"].startswith("[EVENT: webhook.github.pr_opened]")
        assert json.loads(event["metadata"])["routed_event"] == "webhook.github.pr_opened"

    @pytest.mark.asyncio
    async def test_existing_codex_subagent_event_without_thread_id_reconstructs(
            self, isolated_db):
        _mk_session("parent", model="opus")
        _mk_session("cx-lost-sub", spawned_by="parent", model="codex/gpt-5-codex")
        from infra.input_writes import begin_batch_write, settle_batch_write
        from infra.queries import insert_message
        from infra.thread_driver import admit_input, finish_turn

        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        admission = admit_input(
            "cx-lost-sub",
            role="user",
            content="prior input",
            intent="apply_now",
            message_id="m-prior-user",
            turn_id="prior-turn",
            boot_epoch="test",
            admitted_at="t0",
            db=conn,
        )
        assert admission.batch is not None
        begin_batch_write(
            admission.batch,
            attempt_id="prior-write",
            started_at="t0",
            db=conn,
        )
        settle_batch_write("prior-write", outcome="accepted", db=conn)
        insert_message(
            "cx-lost-sub",
            "assistant",
            "prior output",
            metadata={"turn_id": "prior-turn"},
            msg_id="m-prior",
            db=conn,
            created_at="now",
        )
        finish_turn(
            "prior-turn",
            outcome="completed",
            ended_at="t1",
            successor_turn_id="unused-turn",
            boot_epoch="test",
            db=conn,
        )
        conn.commit()
        conn.close()
        import subagents.codex_runtime as codex_runtime
        from events.delivery import deliver_to_thread

        with patch.object(codex_runtime, "has_thread_id", AsyncMock(return_value=False)), \
             patch.object(codex_runtime, "has_prior_messages", AsyncMock(return_value=True)), \
             patch.object(codex_runtime, "feed") as mock_feed, \
            patch.object(
                codex_runtime,
                "feed_main",
                new=AsyncMock(side_effect=_accept_codex_successor),
            ) as mock_feed_main:
            ok = await deliver_to_thread("cx-lost-sub", "webhook.github.pr_opened",
                                         "summary", None)
        assert ok is True
        mock_feed.assert_not_called()
        mock_feed_main.assert_called_once()
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE thread_id='cx-lost-sub' "
            "ORDER BY rowid"
        ).fetchall()
        conn.close()
        assert ("assistant", "prior output") in [
            (row[0], row[1]) for row in rows
        ]
        assert (
            "system",
            "[EVENT: webhook.github.pr_opened]\nSummary: summary",
        ) in [(r[0], r[1]) for r in rows]

    @pytest.mark.asyncio
    async def test_codex_refusal_leaves_visible_failed_event_row(self, isolated_db):
        _mk_session("cx-dead", spawned_by="parent", model="codex/gpt-5-codex")
        import subagents.codex_runtime as codex_runtime
        from events.delivery import deliver_to_thread

        async def _reject(*a, **k):
            return False
        with patch.dict(
            codex_runtime._CODEX_THREADS,
            {"cx-dead": "provider-thread-cx-dead"},
        ), \
             patch.object(codex_runtime, "feed", side_effect=_reject):
            ok = await deliver_to_thread("cx-dead", "webhook.github.pr_opened",
                                         "summary", None)
        assert ok is False
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT delivery_state, delivery_error, seq FROM messages "
            "WHERE thread_id='cx-dead' AND role='system' "
            "AND json_extract(metadata,'$.routed_event') IS NOT NULL"
        ).fetchall()
        conn.close()
        assert [tuple(r) for r in rows] == [(
            "failed", "Codex runtime rejected the resume turn.", 2,
        )]

    @pytest.mark.asyncio
    async def test_codex_connection_failure_leaves_visible_failed_event_row(
        self, isolated_db,
    ):
        _mk_session("cx-no-conn", spawned_by="parent", model="codex/gpt-5-codex")
        import subagents.codex_runtime as codex_runtime
        from events.delivery import deliver_to_thread

        err = codex_runtime.CodexConnectionUnavailable(
            "Codex connection is not available: needs_auth"
        )
        feed = AsyncMock(return_value=True)
        with patch.object(codex_runtime, "ensure_connection_available",
                          side_effect=err), \
             patch.object(codex_runtime, "feed", new=feed):
            ok = await deliver_to_thread("cx-no-conn", "webhook.github.pr_opened",
                                         "summary", None)
        assert ok is False
        feed.assert_not_awaited()
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT delivery_state, delivery_error, seq FROM messages "
            "WHERE thread_id='cx-no-conn' AND role='system' "
            "AND json_extract(metadata,'$.routed_event') IS NOT NULL"
        ).fetchall()
        conn.close()
        assert [tuple(r) for r in rows] == [(
            "failed", "Codex connection is not available: needs_auth", 2,
        )]

    @pytest.mark.asyncio
    async def test_live_codex_event_steers_and_persists_input(self, isolated_db):
        _mk_session("cx-busy", spawned_by="parent", model="codex/gpt-5-codex")
        import subagents.codex_runtime as codex_runtime
        from events.delivery import deliver_to_thread

        feed = AsyncMock(side_effect=_accept_codex_active("turn-event"))
        with patch.object(codex_runtime, "is_live", return_value=True), \
             patch.dict(
                 codex_runtime._CODEX_THREADS,
                 {"cx-busy": "provider-thread-cx-busy"},
             ), \
             patch.object(codex_runtime, "feed", feed):
            ok = await deliver_to_thread("cx-busy", "webhook.github.pr_opened",
                                         "summary", None)

        assert ok is True
        feed.assert_awaited_once()
        conn = sqlite3.connect(str(isolated_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT content, metadata FROM messages "
            "WHERE thread_id='cx-busy' AND role='system'"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert json.loads(rows[0]["metadata"])["event_type"] == "context.digest"
        event = next(row for row in rows if "[EVENT:" in row["content"])
        assert event["content"].startswith("[EVENT: webhook.github.pr_opened]")
        assert json.loads(event["metadata"])["routed_event"] == "webhook.github.pr_opened"

    @pytest.mark.asyncio
    async def test_native_thread_delivered_via_execute_turn(self, isolated_db):
        _mk_session("native-1", model="opus")
        from events.delivery import deliver_to_thread

        async def _ok(*a, **k):
            await k["persist_input_once"]()
            k["start_ack"].set_result(TurnIdentity(k["turn_id"], 1))
            return None
        with patch("ws.turn.execute_turn", side_effect=_ok) as mock_turn:
            ok = await deliver_to_thread("native-1", "webhook.github.pr_opened",
                                         "summary", None)
        assert ok is True
        mock_turn.assert_called_once()
        assert mock_turn.call_args.kwargs["ingress"] == "event"
        assert mock_turn.call_args.kwargs["user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_missing_thread_does_not_deliver(self, isolated_db):
        from events.delivery import deliver_to_thread
        ok = await deliver_to_thread("ghost", "webhook.github.pr_opened",
                                     "summary", None)
        assert ok is False

    @pytest.mark.asyncio
    async def test_claude_code_model_subagent_routes_via_feed_not_native(
        self, isolated_db
    ):
        from infra.database import get_db

        now = datetime.now(timezone.utc).isoformat()
        db = get_db()
        db.execute(
            """INSERT INTO threads
               (id, user_id, title, spawned_by, model,
                project_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("cc-model-subagent", "user-1", "T", "parent",
             "claude-code/sonnet", None, now, now),
        )
        db.commit()
        db.close()

        import subagents.cc_runtime as cc_runtime
        from events.delivery import deliver_to_thread

        with patch.object(
            cc_runtime, "feed", side_effect=_accept_cc_bound_turn,
        ) as mock_feed, \
             patch.object(cc_runtime, "feed_main") as mock_feed_main, \
             patch("ws.turn.execute_turn") as mock_execute_turn:
            ok = await deliver_to_thread(
                "cc-model-subagent", "webhook.github.pr_comment", "summary", None
            )

        assert ok is True
        mock_feed.assert_called_once()
        mock_feed_main.assert_not_called()
        mock_execute_turn.assert_not_called()
