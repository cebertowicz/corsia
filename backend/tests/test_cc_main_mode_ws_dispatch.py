from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

HUMAN_FOOTER = (
    '[To reply, call post_update(text="<your reply>")]\n'
    "If no reply is needed, do not send one."
)

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


class _CaptureWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, message):
        self.sent.append(message)


async def _inline_run_in_db(fn):
    return fn()


def _configure_dispatch_db(monkeypatch, ws_mod, main_db) -> None:
    monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
    monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)


def _broadcast_messages(payload: dict) -> list[dict]:
    if payload.get("type") == "messages_added":
        return list(payload["messages"])
    message = payload.get("message")
    return [message] if message is not None else []


def _select_main_model(main_db, model: str, *, title: str = "Override") -> None:
    main_db.execute(
        "UPDATE threads SET model = ?, title = ? WHERE id = 'main-1'",
        (model, title),
    )
    main_db.commit()


class TestWsDispatch:
    @pytest.mark.asyncio
    async def test_malformed_main_input_rejection_is_input_scoped_not_terminal(self):
        import ws as ws_mod

        class _FakeWS:
            def __init__(self):
                self.sent: list[dict] = []

            async def send_json(self, message):
                self.sent.append(message)

        ws = _FakeWS()
        await ws_mod._handle_message(
            ws,
            {"id": "steve"},
            "steve",
            {"thread_id": "main-1", "input_id": "input-malformed"},
        )

        assert ws.sent == [{
            "type": "error",
            "thread_id": "main-1",
            "message": "Malformed message input",
            "input_id": "input-malformed",
        }]
        assert "turn_id" not in ws.sent[0]
        assert "turn_order" not in ws.sent[0]

    @pytest.mark.asyncio
    async def test_unauthorized_main_input_rejection_is_input_scoped_not_terminal(
        self, main_db, monkeypatch
    ):
        import ws as ws_mod

        async def _inline_run_in_db(fn):
            return fn()

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)

        class _FakeWS:
            def __init__(self):
                self.sent: list[dict] = []

            async def send_json(self, message):
                self.sent.append(message)

        ws = _FakeWS()
        await ws_mod._handle_message(
            ws,
            {"id": "attacker"},
            "attacker",
            {
                "thread_id": "main-1",
                "input_id": "input-unauthorized",
                "content": "steal this turn",
            },
        )

        assert ws.sent == [{
            "type": "error",
            "thread_id": "main-1",
            "message": "Thread not found",
            "input_id": "input-unauthorized",
        }]
        assert "turn_id" not in ws.sent[0]
        assert "turn_order" not in ws.sent[0]

    @pytest.mark.asyncio
    async def test_unroutable_native_model_fails_loud_not_silent(
        self, main_db, monkeypatch
    ):
        # A thread whose model is a non-dispatchable form (catalog selection id
        # omlx/<id> rather than its dispatch form omlx@<server>/<model>) must
        # fail loud at the native dispatch ingress instead of silently no-oping
        # downstream (EAS11/PS25). Safety net for any thread that reached
        # dispatch with such a model.
        import ws as ws_mod

        main_db.execute(
            """INSERT INTO threads
               (id, user_id, title, model, reasoning_effort,
                project_id, created_at, updated_at, parent_thread_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("unroutable-1", "steve", "Doomed", "omlx/abc123", "medium",
             "proj-x", "t", "t", None),
        )
        main_db.commit()

        async def _inline_run_in_db(fn):
            return fn()

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)

        send_calls: list = []

        async def _send_thread_input(*args, **kwargs):
            from infra.send_types import SendRejected

            send_calls.append((args, kwargs))
            raise SendRejected(404, "model not found in catalog")

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input)

        class _FakeWS:
            def __init__(self):
                self.sent: list[dict] = []

            async def send_json(self, m):
                self.sent.append(m)

        ws = _FakeWS()
        await ws_mod._handle_message(
            ws, {"id": "steve"}, "steve",
            {
                "thread_id": "unroutable-1",
                "input_id": "input-unroutable",
                "content": "hello",
            },
        )

        # Resolution now lives behind the atomic shared admission seam. The
        # service is entered once, rejects before provider dispatch, and the
        # error is surfaced with repair guidance.
        assert len(send_calls) == 1
        errors = [m for m in ws.sent if m.get("type") == "error"]
        assert len(errors) == 1
        assert "not found in catalog" in errors[0]["message"].lower()

    @pytest.mark.asyncio
    async def test_native_model_routes_through_shared_send_service(
        self, main_db, monkeypatch
    ):
        import ws as ws_mod

        main_db.execute(
            """INSERT INTO threads
               (id, user_id, title, model, reasoning_effort, spawned_by,
                project_id, created_at, updated_at, parent_thread_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "native-1",
                "steve",
                "Native",
                "omlx@local/qwen",
                "medium",
                None,
                "proj-1",
                "t",
                "t",
                None,
            ),
        )
        main_db.commit()

        async def _inline_run_in_db(fn):
            return fn()

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)
        direct_execute = AsyncMock()
        monkeypatch.setattr(ws_mod, "execute_turn", direct_execute, raising=False)

        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(
                driver="turn_engine",
                message_id="native-msg",
                created_at="2026-07-14T12:00:00+00:00",
                turn_id="turn-native",
                turn_order=42,
                seq=17,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input)

        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def send_json(self, m):
                self.sent.append(m)

        attachments = [{"file_id": "fid1", "filename": "photo.png"}]
        ws = _FakeWS()
        await ws_mod._handle_message(
            ws,
            {"id": "steve"},
            "steve",
            {
                "thread_id": "native-1",
                "input_id": "input-native",
                "content": "native text",
                "model_id": "omlx@local/qwen",
                "attachments": attachments,
                "edited": True,
                "reasoning_effort": "high",
            },
        )

        direct_execute.assert_not_called()
        assert len(calls) == 1
        sid, thread_row, clean_content, kwargs = calls[0]
        assert sid == "native-1"
        assert thread_row["id"] == "native-1"
        assert clean_content == "native text"
        assert kwargs["native_mode"] == "ws"
        assert kwargs["input_id"] == "input-native"
        assert kwargs["user"] == {"id": "steve"}
        assert kwargs["ws"] is ws
        assert kwargs["requested_model_id"] == "omlx@local/qwen"
        assert kwargs["attachments"] == attachments
        assert kwargs["is_retry"] is False
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["persist_input"] is True
        assert kwargs["broadcast_input"] is True
        assert ws.sent == [{
            "type": "input_accepted",
            "thread_id": "native-1",
            "input_id": "input-native",
            "message_id": "native-msg",
            "created_at": "2026-07-14T12:00:00+00:00",
            "turn_id": "turn-native",
            "turn_order": 42,
            "seq": 17,
        }]
        assert kwargs["broadcast_exclude_ws"] is ws
        assert kwargs["touch_thread_on_insert"] is True
        assert kwargs["trim_content"] is False
        assert kwargs["allow_empty_content"] is True
        assert "before_persist_before_dispatch" not in kwargs
        assert callable(kwargs["after_persist_before_dispatch"])
        assert kwargs["metadata"]["attachments"] == attachments
        assert kwargs["metadata"]["edited"] is True
        assert "runtime_context" not in kwargs["metadata"]
        assert "dispatch_content" not in kwargs

    @pytest.mark.asyncio
    async def test_native_model_handler_persists_and_broadcasts_via_shared_send(
        self, main_db, monkeypatch
    ):
        import ws as ws_mod
        import infra.send_service as send_service
        from infra import state, send_preflight

        main_db.execute(
            """INSERT INTO threads
               (id, user_id, title, model, reasoning_effort, spawned_by,
                project_id, created_at, updated_at, parent_thread_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "native-2",
                "steve",
                "Native",
                "omlx@local/qwen",
                "medium",
                None,
                "proj-2",
                "t",
                "t",
                None,
            ),
        )
        main_db.commit()

        async def _inline_run_in_db(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)
        monkeypatch.setattr("infra.db_async.run_in_db", _inline_run_in_db)

        async def _preflight(**kwargs):
            from shared_send_fixtures import prepared_context_with_fit_ledger

            return {
                "status": "allowed",
                "continuity_mode": "full",
                "model_context_window": kwargs["target"].context_window,
                "total_tokens": 1,
                "prepared_context": prepared_context_with_fit_ledger(kwargs["target"]),
            }

        monkeypatch.setattr(
        "infra.send_preflight.context_preflight", _preflight)
        broadcasts: list[tuple[str, dict, object | None]] = []
        monkeypatch.setattr(
            state,
            "broadcast_to_thread",
            lambda thread_id, payload, exclude_ws=None: broadcasts.append(
                (thread_id, payload, exclude_ws)
            ),
        )

        class _FakeWS:
            async def send_json(self, m):
                pass

        async def _execute_turn(
            *,
            start_ack=None,
            turn_id=None,
            target=None,
            persist_input_once=None,
            settle_delivery=None,
            after_persist_before_dispatch=None,
            preadmitted_turn=None,
            provider_acceptance=None,
            **_kwargs,
        ):
            assert persist_input_once is not None
            await persist_input_once()
            assert after_persist_before_dispatch is None
            assert settle_delivery is not None
            await settle_delivery()
            if provider_acceptance is not None and not provider_acceptance.done():
                provider_acceptance.set_result(True)
            if start_ack is not None and not start_ack.done():
                start_ack.set_result(preadmitted_turn)

        with patch("ws.turn.execute_turn", side_effect=_execute_turn) as execute_turn:
            ws = _FakeWS()
            await ws_mod._handle_message(
                ws,
                {"id": "steve"},
                "steve",
                {
                    "thread_id": "native-2",
                    "input_id": "input-native-2",
                    "content": "run native",
                },
            )
            await asyncio.sleep(0)

        row = main_db.execute(
            "SELECT * FROM messages WHERE thread_id = 'native-2' "
            "AND role = 'user'"
        ).fetchone()
        assert row["content"] == "run native"
        assert row["delivery_state"] == "delivered"
        assert row["delivery_error"] is None
        assert row["delivery_revision"] == 2
        metadata = json.loads(row["metadata"]) if row["metadata"] else None
        # Dynamic Native context belongs only to this provider dispatch.  It
        # must not become durable message history, where a later replay would
        # re-use a stale tail and inflate every future reconstruction.
        assert "runtime_context" not in (metadata or {})
        public_metadata = (
            {
                key: value for key, value in metadata.items()
                if not key.startswith("_corsia_") and key != "reply_via_post_update"
            }
            if metadata is not None
            else None
        )
        user_broadcasts = [
            (thread_id, message, exclude_ws)
            for thread_id, payload, exclude_ws in broadcasts
            for message in _broadcast_messages(payload)
            if message["content"] == "run native"
        ]
        assert len(user_broadcasts) == 3
        claimed_turn_id = user_broadcasts[1][1]["active_turn_id"]
        assert claimed_turn_id
        assert [
            (
                message["delivery_state"],
                message["delivery_revision"],
                message["active_turn_id"],
            )
            for _sid, message, _exclude_ws in user_broadcasts
        ] == [
            ("queued", 1, None),
            ("queued", 1, claimed_turn_id),
            ("delivered", 2, claimed_turn_id),
        ]
        for index, (sid, message, exclude_ws) in enumerate(user_broadcasts):
            assert sid == "native-2"
            # The sender already owns its optimistic queued row. Claimed and
            # delivered revisions return to every viewer, including the sender.
            assert exclude_ws is (ws if index == 0 else None)
            assert message["id"] == row["id"]
            assert message["content"] == "run native"
            assert (message["metadata"] or None) == (public_metadata or None)
        execute_turn.assert_awaited_once()
        assert "run native" in execute_turn.await_args.kwargs["content"]
        assert "Current time:" in execute_turn.await_args.kwargs["content"]
        assert "native-2" not in state._active_turn_tasks

    @pytest.mark.asyncio
    async def test_replace_cancel_joins_owner_without_duplicate_publication(
        self, monkeypatch
    ):
        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent
        from infra import state
        from ws import turn_cancel

        thread_id = "native-replace"
        cancel_event = TurnCancelEvent()
        cancel_event.bind_turn_identity(
            TurnIdentity("native-replace-turn", 1)
        )
        state._active_turns[thread_id] = cancel_event

        async def _active_turn():
            await cancel_event.wait()
            if not cancel_event.completion.done():
                cancel_event.completion.set_result(True)
            return True

        turn_task = asyncio.create_task(_active_turn())
        state._active_turn_tasks[thread_id] = turn_task
        broadcasts: list[tuple[str, dict, object | None]] = []
        monkeypatch.setattr(
            state,
            "broadcast_to_thread",
            lambda sid, payload, exclude_ws=None: broadcasts.append(
                (sid, payload, exclude_ws)
            ),
        )

        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def send_json(self, m):
                self.sent.append(m)

        ws = _FakeWS()
        try:
            await turn_cancel.cancel_active_turn(ws, thread_id)
        finally:
            state._active_turns.pop(thread_id, None)
            state._active_llm_tasks.pop(thread_id, None)
            state._active_turn_tasks.pop(thread_id, None)

        assert cancel_event.is_set()
        assert turn_task.done()
        assert ws.sent == []
        assert broadcasts == []

    @pytest.mark.asyncio
    async def test_cc_model_routes_through_shared_send_admission(
        self, main_db, monkeypatch
    ):
        import ws as ws_mod

        async def _inline_run_in_db(fn):
            return fn()

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)
        cc_handler = AsyncMock()
        shared_send = AsyncMock(return_value=SimpleNamespace(
            driver="cc_runtime.feed_main",
            message_id="cc-message",
            created_at="t",
            turn_id="cc-turn",
            turn_order=1,
        ))
        turn_mock = AsyncMock()
        monkeypatch.setattr(ws_mod, "_handle_cc_main_message", cc_handler)
        monkeypatch.setattr(ws_mod, "send_thread_input", shared_send)
        monkeypatch.setattr(ws_mod, "execute_turn", turn_mock, raising=False)

        class _FakeWS:
            async def send_json(self, m):
                pass

        await ws_mod._handle_message(
            _FakeWS(), {"id": "steve"}, "steve",
            {
                "thread_id": "main-1",
                "input_id": "input-main-route",
                "content": "hi",
            },
        )
        cc_handler.assert_not_awaited()
        shared_send.assert_awaited_once()
        assert shared_send.await_args.kwargs["native_mode"] == "ws"
        turn_mock.assert_not_called()

    @pytest.mark.parametrize(
        ("session_model", "requested_model_id", "route"),
        [
            pytest.param(
                "anthropic/claude-sonnet-4-6",
                "claude-code/sonnet",
                "cc",
                id="native-to-claude-code",
            ),
            pytest.param(
                "anthropic/claude-sonnet-4-6",
                "codex/gpt-5-codex",
                "codex",
                id="native-to-codex",
            ),
            pytest.param(
                "claude-code/opus",
                "anthropic/claude-sonnet-4-6",
                "native",
                id="claude-code-to-native",
            ),
            pytest.param(
                "codex/gpt-5-codex",
                "anthropic/claude-sonnet-4-6",
                "native",
                id="codex-to-native",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_requested_model_routes_cross_harness_behind_shared_admission(
        self,
        main_db,
        monkeypatch,
        session_model,
        requested_model_id,
        route,
    ):
        import ws as ws_mod

        _select_main_model(main_db, session_model)
        _configure_dispatch_db(monkeypatch, ws_mod, main_db)
        send_calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(sid, thread_row, content, **kwargs):
            send_calls.append((sid, thread_row, content, kwargs))
            return SimpleNamespace(
                driver=route,
                message_id=f"message-{route}",
                created_at="t",
                turn_id=f"turn-{route}",
                turn_order=1,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input)
        ws = _CaptureWS()
        await ws_mod._handle_message(
            ws,
            {"id": "steve"},
            "steve",
            {
                "thread_id": "main-1",
                "input_id": f"input-{route}",
                "content": "switch model",
                "model_id": requested_model_id,
            },
        )

        assert len(send_calls) == 1
        sid, _session, content, kwargs = send_calls[0]
        assert sid == "main-1"
        assert content == "switch model"
        assert kwargs["native_mode"] == "ws"
        assert kwargs["requested_model_id"] == requested_model_id
        assert "target" not in kwargs
        assert "dispatch_content" not in kwargs
        assert kwargs["user"] == {"id": "steve"}
        assert kwargs["ws"] is ws
        assert kwargs["metadata"] is None

    @pytest.mark.asyncio
    async def test_edited_requested_omlx_preserves_cache_prefix_for_final_refreshed_target(
        self, main_db, monkeypatch
    ):
        import httpx
        import ws as ws_mod
        import ws.message_dispatch as message_dispatch

        requested_model_id = "managed/edit-model"
        stale_transport_model = "omlx@srv-edit-stale/Qwen-Edit"
        refreshed_transport_model = "omlx@srv-edit-fresh/Qwen-Edit"
        _select_main_model(
            main_db,
            "anthropic/claude-sonnet-4-6",
            title="Edited override",
        )
        main_db.execute(
            """INSERT INTO models
               (id, harness, provider, provider_model_id, litellm_model,
                display_name, backend, source, enabled, available,
                context_window, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)""",
            (
                requested_model_id,
                "native",
                "omlx",
                "Qwen-Edit",
                stale_transport_model,
                "Qwen Edit",
                "network",
                "test",
                200_000,
                "t",
                "t",
            ),
        )
        main_db.commit()

        _configure_dispatch_db(monkeypatch, ws_mod, main_db)
        from llm.resolution import resolve_execution_target

        initial_target = resolve_execution_target(requested_model_id, db=main_db)
        cache_delete = AsyncMock()

        class _Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            delete = cache_delete

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        monkeypatch.setattr(
            "llm.capabilities.make_provider_params",
            lambda _model: {"api_base": "http://cache.invalid/v1"},
        )
        send_kwargs: dict = {}

        async def _send_thread_input(_sid, _thread_row, _content, **kwargs):
            send_kwargs.update(kwargs)
            refreshed_target = replace(
                initial_target,
                transport_model=refreshed_transport_model,
                context_window=262_144,
            )
            send_kwargs["dispatch_target"] = refreshed_target
            await kwargs["after_persist_before_dispatch"](refreshed_target)
            return SimpleNamespace(
                driver="turn_engine",
                message_id="edited-override-message",
                created_at="t",
                turn_id="edited-override-turn",
                turn_order=1,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input)

        await ws_mod._handle_message(
            _CaptureWS(),
            {"id": "steve"},
            "steve",
            {
                "thread_id": "main-1",
                "input_id": "input-edited-override",
                "content": "edit with override",
                "model_id": requested_model_id,
                "edited": True,
            },
        )

        cache_delete.assert_not_awaited()
        assert not hasattr(message_dispatch, "_bust_network_cache")
        assert "target" not in send_kwargs
        assert send_kwargs["requested_model_id"] == requested_model_id
        assert send_kwargs["dispatch_target"].catalog_id == requested_model_id
        assert send_kwargs["dispatch_target"].transport_model == refreshed_transport_model

    @pytest.mark.asyncio
    async def test_cc_main_handler_inserts_row_and_feeds(
        self, main_db, monkeypatch
    ):
        import ws as ws_mod
        import infra.send_service as send_service
        import subagents.cc_runtime as rt

        async def _inline_run_in_db(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)
        monkeypatch.setattr("infra.db_async.run_in_db", _inline_run_in_db)
        async def _accept_cc(*_args, **kwargs):
            await kwargs["persist_input_once"]()
            await kwargs["settle_delivery"]()
            return kwargs["preadmitted_turn"]

        feed_mock = AsyncMock(side_effect=_accept_cc)
        monkeypatch.setattr(rt, "feed_main", feed_mock)
        # Title generation is separately tested and runs fire-and-forget. This
        # dispatch test owns neither that task nor its event-loop lifecycle.
        monkeypatch.setattr("infra.send_effects._schedule_title_effect", lambda *_args: None)

        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def send_json(self, m):
                self.sent.append(m)

        session = main_db.execute(
            "SELECT * FROM threads WHERE id = 'main-1'").fetchone()
        await ws_mod._handle_cc_main_message(
            _FakeWS(), session, "main-1", "do it", [],
            msg_id="m1", now="t1",
            is_retry=False, is_edited=False,
        )
        row = main_db.execute(
            "SELECT * FROM messages WHERE thread_id = 'main-1' "
            "AND role = 'user' ORDER BY created_at DESC").fetchone()
        assert row["role"] == "user" and row["content"] == "do it"
        feed_mock.assert_awaited_once()
        assert feed_mock.call_args.args[0] == "main-1"
        assert "[PASSIVE CONTEXT DIGEST]" in feed_mock.call_args.args[1]
        assert "Current time:" in feed_mock.call_args.args[1]
        assert feed_mock.call_args.args[1].endswith(f"[2/2]\ndo it\n{HUMAN_FOOTER}")
        dispatched_turn_id = feed_mock.call_args.kwargs["turn_id"]
        assert dispatched_turn_id
        assert row["active_turn_id"] == dispatched_turn_id
        target = feed_mock.call_args.kwargs["target"]
        assert target.catalog_id == "claude-code/opus"
        assert target.transport_model == "opus"

    @pytest.mark.asyncio
    async def test_cc_main_handler_uses_shared_send_service(
        self, main_db, monkeypatch, tmp_path
    ):
        import ws as ws_mod
        import infra.config as cfg
        import subagents.cc_runtime as rt

        monkeypatch.setattr(cfg, "UPLOADS_DIR", tmp_path)
        (tmp_path / "steve" / "f").mkdir(parents=True)
        (tmp_path / "steve" / "f" / "a.png").write_bytes(b"img")

        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(driver="cc_runtime.feed_main", message_id="m1")

        feed_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input, raising=False)
        monkeypatch.setattr(rt, "feed_main", feed_mock)

        class _FakeWS:
            def __init__(self):
                self.sent = []

            async def send_json(self, m):
                self.sent.append(m)

        session = main_db.execute(
            "SELECT * FROM threads WHERE id = 'main-1'").fetchone()
        sender_ws = _FakeWS()
        await ws_mod._handle_cc_main_message(
            sender_ws, session, "main-1", "look at this",
            [{"file_id": "f", "filename": "a.png"}],
            msg_id="m1", now="t1",
            is_retry=False, is_edited=True,
        )

        feed_mock.assert_not_awaited()
        assert len(calls) == 1
        sid, called_thread_row, clean_content, kwargs = calls[0]
        assert sid == "main-1"
        assert called_thread_row["id"] == "main-1"
        assert clean_content == "look at this"
        assert kwargs["native_mode"] == "delegation"
        # #504: broadcast the inbound row to other subscribed clients while
        # excluding the sender's socket (no self-duplicate) — same contract as
        # the native handler.
        assert kwargs["broadcast_input"] is True
        assert kwargs["broadcast_exclude_ws"] is sender_ws
        assert kwargs["touch_thread_on_insert"] is True
        assert kwargs["trim_content"] is False
        assert kwargs["metadata"]["edited"] is True
        assert kwargs["metadata"]["attachments"] == [
            {"file_id": "f", "filename": "a.png"}
        ]
        assert kwargs["attachments"] == [
            {"file_id": "f", "filename": "a.png"}
        ]
        assert "dispatch_content" not in kwargs

    @pytest.mark.asyncio
    async def test_cc_handler_forwards_reasoning_effort(self, monkeypatch):
        """reasoning_effort passed to send_thread_input when provided (#561)."""
        import ws as ws_mod

        calls: list[dict] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(driver="cc_runtime.feed_main", message_id="m1")

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input, raising=False)

        class _FakeWS:
            async def send_json(self, m):
                pass

        session = {"id": "s1", "model": "claude-code/sonnet", "user_id": "u",
                   "project_id": None, "reasoning_effort": None, "title": "Chat"}
        await ws_mod._handle_cc_main_message(
            _FakeWS(), session, "s1", "hi", [],
            msg_id="", now="", is_retry=False, is_edited=False,
            reasoning_effort="xhigh",
        )
        assert calls[0]["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_cc_handler_no_effort_passes_none(self, monkeypatch):
        """reasoning_effort=None is passed through when not set (#561)."""
        import ws as ws_mod

        calls: list[dict] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(driver="cc_runtime.feed_main", message_id="m1")

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input, raising=False)

        class _FakeWS:
            async def send_json(self, m):
                pass

        session = {"id": "s1", "model": "claude-code/sonnet", "user_id": "u",
                   "project_id": None, "reasoning_effort": None, "title": "Chat"}
        await ws_mod._handle_cc_main_message(
            _FakeWS(), session, "s1", "hi", [],
            msg_id="", now="", is_retry=False, is_edited=False,
        )
        assert calls[0]["reasoning_effort"] is None

    @pytest.mark.asyncio
    async def test_ws_dispatch_reads_session_effort_for_cc(self, main_db, monkeypatch):
        """Saved effort reaches shared admission when a WS message omits it."""
        import ws as ws_mod

        main_db.execute(
            "INSERT OR REPLACE INTO threads "
            "(id, user_id, model, reasoning_effort, created_at, updated_at) "
            "VALUES ('cc-effort-1', 'steve', 'claude-code/sonnet', 'xhigh', 't', 't')"
        )
        main_db.commit()

        async def _inline_run_in_db(fn):
            return fn()

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)

        captured: list[dict] = []

        async def _shared_send(*_args, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                message_id="effort-1-message",
                created_at="t",
                turn_id="effort-1-turn",
                turn_order=1,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _shared_send)

        class _FakeWS:
            async def send_json(self, m):
                pass

        await ws_mod._handle_message(
            _FakeWS(), {"id": "steve"}, "steve",
            {
                "thread_id": "cc-effort-1",
                "input_id": "input-effort-1",
                "content": "hello",
            },
        )

        assert captured, "shared send service must be called"
        assert captured[0]["reasoning_effort"] == "xhigh"

    @pytest.mark.asyncio
    async def test_ws_dispatch_ws_message_effort_overrides_session_for_cc(
        self, main_db, monkeypatch
    ):
        """Per-message reasoning_effort beats the session's saved effort for CC."""
        import ws as ws_mod

        main_db.execute(
            "INSERT OR REPLACE INTO threads "
            "(id, user_id, model, reasoning_effort, created_at, updated_at) "
            "VALUES ('cc-effort-2', 'steve', 'claude-code/sonnet', 'low', 't', 't')"
        )
        main_db.commit()

        async def _inline_run_in_db(fn):
            return fn()

        monkeypatch.setattr(ws_mod, "run_in_db", _inline_run_in_db)
        monkeypatch.setattr(ws_mod, "get_db", lambda: main_db)

        captured: list[dict] = []

        async def _shared_send(*_args, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                message_id="effort-2-message",
                created_at="t",
                turn_id="effort-2-turn",
                turn_order=1,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _shared_send)

        class _FakeWS:
            async def send_json(self, m):
                pass

        await ws_mod._handle_message(
            _FakeWS(), {"id": "steve"}, "steve",
            {
                "thread_id": "cc-effort-2",
                "input_id": "input-effort-2",
                "content": "hello",
                "reasoning_effort": "max",
            },
        )

        assert captured[0]["reasoning_effort"] == "max"
