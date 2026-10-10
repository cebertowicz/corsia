
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _run(coro):
    return asyncio.run(coro)



class TestHandleCodexMainMessage:

    def _make_ws(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        return ws

    def _make_session(self, project_id="proj-1"):
        return {
            "id": "sess-1",
            "project_id": project_id,
            "user_id": "u-1",
            "model": "codex/gpt-5-codex",
            "title": "New Chat",
            "spawned_by": None,
        }

    def test_accepted_turn_does_not_send_error(self):
        import ws as ws_mod

        ws = self._make_ws()

        send = AsyncMock(
            return_value=SimpleNamespace(driver="codex_runtime.feed_main")
        )
        with patch.object(ws_mod, "send_thread_input", send):
            _run(ws_mod._handle_codex_main_message(
                ws, self._make_session(), "sess-1", "hello",
                msg_id="msg-1", now="2026-06-15T00:00:00Z", is_retry=False,
            ))

        ws.send_json.assert_not_called()
        send.assert_awaited_once()
        assert send.await_args.args[:3] == (
            "sess-1", self._make_session(), "hello"
        )
        # #504: broadcast the inbound row to other subscribed clients (cross-device),
        # excluding the sender's own socket so it isn't duplicated.
        assert send.await_args.kwargs["broadcast_input"] is True
        assert send.await_args.kwargs["broadcast_exclude_ws"] is ws
        assert send.await_args.kwargs["touch_thread_on_insert"] is True
        assert send.await_args.kwargs["trim_content"] is False

    def test_rejected_turn_sends_error(self):
        import ws as ws_mod
        from infra.send_service import SendRejected

        ws = self._make_ws()

        send = AsyncMock(
            side_effect=SendRejected(
                409,
                "Codex runtime rejected the reconstructed turn.",
                metadata={
                    "cause": "project_working_directory_unresolved",
                    "repair_owner": "ctx-stability",
                },
            )
        )
        with patch.object(ws_mod, "send_thread_input", send):
            _run(ws_mod._handle_codex_main_message(
                ws, self._make_session(), "sess-1", "hello",
                msg_id="msg-1", now="2026-06-15T00:00:00Z", is_retry=False,
            ))

        ws.send_json.assert_called_once()
        sent = ws.send_json.call_args[0][0]
        assert sent.get("type") == "error"
        assert "sess-1" == sent.get("thread_id")
        assert sent.get("error_type") == "harness_error"
        assert sent.get("event_type") == "harness.error"
        assert sent.get("harness") == "codex"
        assert sent.get("cause") == "project_working_directory_unresolved"
        assert sent.get("repair_owner") == "ctx-stability"

    def test_feed_main_receives_raw_content(self):
        import ws as ws_mod

        ws = self._make_ws()
        captured = {}

        async def fake_send(thread_id, thread_row, content, **kwargs):
            captured["content"] = content
            return SimpleNamespace(driver="codex_runtime.feed_main")

        with patch.object(ws_mod, "send_thread_input", fake_send):
            _run(ws_mod._handle_codex_main_message(
                ws, self._make_session(), "sess-x", "raw user message",
                msg_id="m-1", now="2026-06-15T00:00:00Z", is_retry=False,
            ))

        assert captured["content"] == "raw user message"

    def test_project_id_forwarded(self):
        import ws as ws_mod

        ws = self._make_ws()
        captured = {}

        async def fake_send(thread_id, thread_row, content, **kwargs):
            captured["project_id"] = thread_row["project_id"]
            return SimpleNamespace(driver="codex_runtime.feed_main")

        with patch.object(ws_mod, "send_thread_input", fake_send):
            _run(ws_mod._handle_codex_main_message(
                ws, self._make_session("proj-xyz"), "sess-p",
                "task", msg_id="m-2", now="2026-06-15T00:00:00Z",
                is_retry=False,
            ))

        assert captured["project_id"] == "proj-xyz"

    def test_exception_sends_error_without_propagating(self):
        import ws as ws_mod

        ws = self._make_ws()

        send = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(ws_mod, "send_thread_input", send):
            _run(ws_mod._handle_codex_main_message(
                ws, self._make_session(), "sess-err", "oops",
                msg_id="m-3", now="2026-06-15T00:00:00Z", is_retry=False,
            ))

        ws.send_json.assert_called_once()
        assert ws.send_json.call_args[0][0]["type"] == "error"

    def test_effort_forwarded_to_send_thread_input(self):
        """reasoning_effort is passed through to send_thread_input (#561)."""
        import ws as ws_mod

        ws = self._make_ws()
        captured: list[dict] = []

        async def fake_send(thread_id, thread_row, content, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(driver="codex_runtime.feed_main")

        with patch.object(ws_mod, "send_thread_input", fake_send):
            _run(ws_mod._handle_codex_main_message(
                ws, self._make_session(), "sess-effort", "hi",
                msg_id="m-e", now="2026-06-15T00:00:00Z", is_retry=False,
                reasoning_effort="xhigh",
            ))

        assert captured[0]["reasoning_effort"] == "xhigh"

    def test_no_effort_passes_none(self):
        """Omitting reasoning_effort passes None to send_thread_input (#561)."""
        import ws as ws_mod

        ws = self._make_ws()
        captured: list[dict] = []

        async def fake_send(thread_id, thread_row, content, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(driver="codex_runtime.feed_main")

        with patch.object(ws_mod, "send_thread_input", fake_send):
            _run(ws_mod._handle_codex_main_message(
                ws, self._make_session(), "sess-noeffort", "hi",
                msg_id="m-n", now="2026-06-15T00:00:00Z", is_retry=False,
            ))

        assert captured[0]["reasoning_effort"] is None



class TestDoStopCodexPath:

    def test_stop_calls_codex_runtime_stop_when_live(self):
        from ws import _do_stop

        with (
            patch("subagents.cc_runtime.has_pending_or_live", return_value=False),
            patch("subagents.codex_runtime.has_pending_or_live", return_value=True),
            patch(
                "subagents.codex_runtime.stop",
                new=AsyncMock(return_value=True),
            ) as mock_stop,
            patch("ws._owns_thread", new=AsyncMock(return_value=True)),
            patch(
                "ws.stop_control._commit_stop_terminal",
                return_value=("cancelled", None),
            ),
        ):
            _run(_do_stop(
                {"id": "u-1"}, "sess-codex",
                turn_event=None, llm_task=None,
                expected_turn_id="turn-codex",
            ))

        mock_stop.assert_called_once_with(
            "sess-codex", expected_turn_id="turn-codex", broadcast=ANY,
        )

    def test_stop_does_not_call_codex_stop_when_not_live(self):
        from ws import _do_stop

        with (
            patch("subagents.cc_runtime.has_pending_or_live", return_value=False),
            patch("subagents.codex_runtime.has_pending_or_live", return_value=False),
            patch("subagents.codex_runtime.stop", new=AsyncMock()) as mock_stop,
            patch("ws._owns_thread", new=AsyncMock(return_value=True)),
        ):
            _run(_do_stop(
                {"id": "u-1"}, "sess-not-codex",
                turn_event=None, llm_task=None,
                expected_turn_id="turn-not-codex",
            ))

        mock_stop.assert_not_called()

    def test_stop_calls_codex_owner_during_pending_transition(self):
        from ws import _do_stop

        with (
            patch("subagents.cc_runtime.has_pending_or_live", return_value=False),
            patch("subagents.codex_runtime.has_pending_or_live", return_value=True),
            patch(
                "subagents.codex_runtime.stop",
                new=AsyncMock(return_value=True),
            ) as mock_stop,
            patch("ws._owns_thread", new=AsyncMock(return_value=True)),
            patch(
                "ws.stop_control._commit_stop_terminal",
                return_value=("cancelled", None),
            ),
        ):
            receipt = _run(_do_stop(
                {"id": "u-1"}, "sess-codex-transition",
                turn_event=None, llm_task=None,
                expected_turn_id="turn-codex",
            ))

        assert receipt.outcome == "stopped"
        mock_stop.assert_called_once_with(
            "sess-codex-transition",
            expected_turn_id="turn-codex",
            broadcast=ANY,
        )



class TestCodexSubagentWsRouting:
    @pytest.fixture(autouse=True)
    def _canonical_codex_target(self, monkeypatch):
        from llm.resolution import ExecutionTarget

        monkeypatch.setattr(
            "infra.execution_class.resolve_execution_target",
            lambda model: ExecutionTarget(
                catalog_id=model, harness="codex", provider="codex",
                backend="codex", transport_model="gpt-5-codex",
                expected_reported_model="gpt-5-codex",
                context_window=200000, available=True,
                availability_reason=None,
                display_name=model, supports_reasoning=True,
            ),
        )


    def _codex_subagent_row(self):
        return {
            "id": "cx-child",
            "user_id": "u-1",
            "model": "codex/gpt-5-codex",
            "spawned_by": "parent-1",
            "parent_thread_id": "parent-1",
            "title": "Codex child",
            "project_id": None,
        }

    def test_subscribe_reports_codex_meta_for_codex_model(self):
        from ws import _handle_subscribe

        ws = MagicMock()
        ws.send_json = AsyncMock()

        async def _owned(_user, _thread_id):
            return self._codex_subagent_row()

        with patch("ws._owned_thread", new=_owned), \
             patch(
                 "ws.thread_control.active_turns_for_owner",
                 return_value=[{
                     "thread_id": "cx-child",
                     "turn_id": "turn-cx-child",
                     "turn_order": 4,
                 }],
             ):
            _run(_handle_subscribe(ws, {"id": "u-1"}, "cx-child"))

        meta = [
            call.args[0] for call in ws.send_json.await_args_list
            if call.args[0].get("type") == "subagent_meta"
        ]
        assert meta == [{
            "type": "subagent_meta",
            "thread_id": "cx-child",
            "parent_thread_id": "parent-1",
            "name": "Codex child",
            "provider": "codex",
            "live": True,
            "model": "codex/gpt-5-codex",  # model included since #551 fix
        }]

    def test_feed_uses_codex_runtime_for_codex_model(self):
        import ws as ws_mod
        import subagents.cc_runtime as cc_runtime

        async def _owned(_user, _thread_id):
            return self._codex_subagent_row()

        ws = MagicMock()
        ws.send_json = AsyncMock()
        cc_feed = AsyncMock(return_value=True)
        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(
                driver="codex_runtime.feed", message_id="m-codex-feed",
                created_at="2026-07-14T12:00:00+00:00",
                turn_id="turn-codex-feed", turn_order=5,
            )

        with patch.object(ws_mod, "_owned_thread", new=_owned), \
             patch.object(ws_mod, "send_thread_input", new=_send_thread_input), \
             patch.object(cc_runtime, "feed", cc_feed):
            _run(ws_mod._handle_subagent_feed(
                ws, {"id": "u-1"}, "cx-child", "steer this", [], False,
                "input-codex-feed",
            ))

        cc_feed.assert_not_awaited()
        assert len(calls) == 1
        assert calls[0][0] == "cx-child"
        assert calls[0][1]["model"] == "codex/gpt-5-codex"
        assert calls[0][2] == "steer this"
        assert calls[0][3]["input_id"] == "input-codex-feed"
        assert ws.send_json.await_args.args[0]["type"] == "input_accepted"

    def test_fresh_codex_subagent_feed_starts_first_turn(self):
        import ws as ws_mod

        async def _owned(_user, _thread_id):
            return self._codex_subagent_row()

        ws = MagicMock()
        ws.send_json = AsyncMock()
        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(
                driver="codex_runtime.feed_main", message_id="m-codex-first",
                created_at="2026-07-14T12:00:00+00:00",
                turn_id="turn-codex-first", turn_order=6,
            )

        with patch.object(ws_mod, "_owned_thread", new=_owned), \
             patch.object(ws_mod, "send_thread_input", new=_send_thread_input):
            _run(ws_mod._handle_subagent_feed(
                ws, {"id": "u-1"}, "cx-child", "first turn", [], False,
                "input-codex-first",
            ))

        assert len(calls) == 1
        assert calls[0][0] == "cx-child"
        assert calls[0][2] == "first turn"
        assert calls[0][3]["native_mode"] == "delegation"
        assert calls[0][3]["touch_thread_on_insert"] is True
        assert calls[0][3]["trim_content"] is False
        assert ws.send_json.await_args.args[0]["type"] == "input_accepted"

    def test_codex_subagent_feed_reconstructs_prior_history_without_thread_id(
        self, isolated_db
    ):
        import ws as ws_mod
        import subagents.codex_runtime as codex_runtime
        from core.turn_core import TurnIdentity
        from shared_send_fixtures import _insert_session

        _insert_session(
            "cx-child",
            user_id="u-1",
            model="codex/gpt-5-codex",
            spawned_by="parent-1",
        )

        async def _owned(_user, _thread_id):
            return self._codex_subagent_row()

        ws = MagicMock()
        ws.send_json = AsyncMock()
        feed = AsyncMock(return_value=TurnIdentity("turn-unused", 7))

        async def _accept_successor(*_args, preadmitted_turn, **kwargs):
            await kwargs["bind_input_turn"](preadmitted_turn.turn_id, None)
            kwargs["mark_accepted"]()
            await kwargs["settle_delivery"]()
            return preadmitted_turn

        feed_main = AsyncMock(side_effect=_accept_successor)

        with patch.object(ws_mod, "_owned_thread", new=_owned), \
             patch.object(codex_runtime, "ensure_connection_available",
                          lambda db=None: None), \
             patch.object(codex_runtime, "has_thread_id",
                          AsyncMock(return_value=False)), \
             patch.object(codex_runtime, "has_prior_messages",
                          AsyncMock(return_value=True)), \
             patch("llm.capabilities.get_model_context_window",
                   return_value=200000), \
             patch.object(codex_runtime, "feed", feed), \
             patch.object(codex_runtime, "feed_main", feed_main):
            _run(ws_mod._handle_subagent_feed(
                ws, {"id": "u-1"}, "cx-child", "continue", [], False,
                "input-codex-history",
            ))

        feed.assert_not_awaited()
        feed_main.assert_awaited_once()
        assert ws.send_json.await_args.args[0]["type"] == "input_accepted"

    def test_codex_subagent_feed_connection_unavailable_writes_no_row(self):
        import ws as ws_mod
        from infra.send_service import SendRejected

        async def _owned(_user, _thread_id):
            return self._codex_subagent_row()

        ws = MagicMock()
        ws.send_json = AsyncMock()
        send = AsyncMock(
            side_effect=SendRejected(
                503, "Codex connection is not available: needs_auth"
            )
        )

        with patch.object(ws_mod, "_owned_thread", new=_owned), \
             patch.object(ws_mod, "insert_message") as insert_message, \
             patch.object(ws_mod, "send_thread_input", send):
            _run(ws_mod._handle_subagent_feed(
                ws, {"id": "u-1"}, "cx-child", "continue", [], False,
                "input-codex-unavailable",
            ))

        insert_message.assert_not_called()
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "error"
        assert "Codex connection is not available" in sent["message"]

    def test_stop_uses_codex_runtime_for_codex_model(self):
        import ws as ws_mod
        import subagents.cc_runtime as cc_runtime
        import subagents.codex_runtime as codex_runtime
        from infra import state

        async def _owned(_user, _thread_id):
            return self._codex_subagent_row()

        ws = MagicMock()
        ws.send_json = AsyncMock()
        codex_stop = AsyncMock(return_value=True)
        cc_stop = AsyncMock(return_value=True)
        owner_broadcast = object()

        with patch.object(ws_mod, "_owned_thread", new=_owned), \
             patch.object(state, "owner_broadcast", return_value=owner_broadcast), \
             patch.object(codex_runtime, "has_pending_or_live", return_value=True), \
             patch.object(codex_runtime, "stop", codex_stop), \
             patch.object(cc_runtime, "stop", cc_stop):
            _run(ws_mod._handle_subagent_stop(
                ws, {"id": "u-1"}, "cx-child", "turn-cx-child",
            ))

        codex_stop.assert_awaited_once_with(
            "cx-child", expected_turn_id="turn-cx-child",
            broadcast=owner_broadcast,
        )
        cc_stop.assert_not_awaited()
