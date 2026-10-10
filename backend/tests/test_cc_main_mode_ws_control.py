from __future__ import annotations

import asyncio
import os
import stat
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from cc_runtime_fixtures import (
    cc_target,
    feed_main_with_persisted_input,
    prepared_cc_input,
)
from tests.conftest import _wait

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


_FAKE_STALLED_READER = textwrap.dedent(
    '''\
    #!{python}
    import json, sys, time
    def emit(value):
        sys.stdout.write(json.dumps(value) + "\\n"); sys.stdout.flush()
    def visible_content(content):
        marker = "User message:\\n"
        return content.split(marker, 1)[1] if marker in content else content
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("type") != "user":
            continue
        content = visible_content(message.get("message", {{}}).get("content", ""))
        if "HANG-NOW" in content:
            emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"partial"}}]}}}})
            while True:
                time.sleep(60)
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo:" + content}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"done","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
        break
    '''
)


@pytest.fixture
def fake_claude_stalled_reader(tmp_path):
    script = tmp_path / "fake_claude_stalled_reader"
    script.write_text(_FAKE_STALLED_READER.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _turn_messages(casts, turn_id: str, message_type: str | None = None):
    return [
        message for _, message in casts
        if message.get("turn_id") == turn_id
        and (message_type is None or message.get("type") == message_type)
    ]


class TestStopHandler:
    @pytest.fixture(autouse=True)
    def _clear_turn_state(self):
        from infra import state
        import subagents.cc_runtime as rt
        state._active_turns.clear()
        state._active_llm_tasks.clear()
        rt._LIVE.reset()
        yield
        state._active_turns.clear()
        state._active_llm_tasks.clear()
        rt._LIVE.reset()

    @pytest.mark.asyncio
    async def test_stop_fires_cc_close_for_live_main(self, monkeypatch):
        import ws as ws_mod
        import subagents.cc_runtime as rt
        from infra import state

        stop_mock = AsyncMock(return_value=True)
        owner_broadcast = object()
        monkeypatch.setattr(rt, "stop", stop_mock)
        monkeypatch.setattr(rt, "has_pending_or_live", lambda s: True)
        monkeypatch.setattr(
            state, "owner_broadcast", lambda user_id: (
                owner_broadcast if user_id == "test-user" else None
            ),
        )

        async def _owns(_u, _s):
            return True
        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)

        ws_mod._handle_stop({"id": "test-user"}, "cc-main", "turn-cc")
        assert await _wait(lambda: stop_mock.await_count == 1)
        stop_mock.assert_awaited_once_with(
            "cc-main", expected_turn_id="turn-cc", broadcast=owner_broadcast,
        )

    @pytest.mark.asyncio
    async def test_stop_fires_during_spawn_race_window(self, monkeypatch):
        import ws as ws_mod
        import subagents.cc_runtime as rt

        lock = rt._LIVE.lock("racing")
        await lock.acquire()
        try:
            stop_mock = AsyncMock(return_value=True)
            monkeypatch.setattr(rt, "stop", stop_mock)

            async def _owns(_u, _s):
                return True
            monkeypatch.setattr(ws_mod, "_owns_thread", _owns)

            assert rt.is_live("racing") is False
            assert rt.has_pending_or_live("racing") is True
            ws_mod._handle_stop({"id": "test-user"}, "racing", "turn-racing")
            assert await _wait(lambda: stop_mock.await_count == 1)
            stop_mock.assert_awaited_once_with(
                "racing", expected_turn_id="turn-racing", broadcast=ANY,
            )
        finally:
            lock.release()

    @pytest.mark.asyncio
    async def test_stop_skips_cc_for_plain_corsia_turn(self, monkeypatch):
        import ws as ws_mod
        import subagents.cc_runtime as rt
        from infra import state

        stop_mock = AsyncMock(return_value=False)
        monkeypatch.setattr(rt, "stop", stop_mock)

        async def _owns(_u, _s):
            return True
        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)

        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent

        ev = TurnCancelEvent()
        ev.bind_turn_identity(TurnIdentity("turn-plain", 1))
        state._active_turns["plain"] = ev
        ws_mod._handle_stop({"id": "test-user"}, "plain", "turn-plain")
        assert await _wait(ev.is_set)
        stop_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_cross_user_rejected(self, monkeypatch):
        import ws as ws_mod
        import subagents.cc_runtime as rt
        from infra import state

        stop_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(rt, "stop", stop_mock)
        monkeypatch.setattr(rt, "has_pending_or_live", lambda s: True)

        async def _not_owns(_u, _s):
            return False
        monkeypatch.setattr(ws_mod, "_owns_thread", _not_owns)

        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent

        ev = TurnCancelEvent()
        ev.bind_turn_identity(TurnIdentity("turn-victim", 1))
        state._active_turns["victim"] = ev
        ws_mod._handle_stop({"id": "attacker"}, "victim", "turn-victim")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        stop_mock.assert_not_awaited()
        assert not ev.is_set(), "must not cancel another user's CORSIA turn"

    @pytest.mark.asyncio
    async def test_stop_cancels_corsia_turn_alongside_cc(self, monkeypatch):
        import ws as ws_mod
        import subagents.cc_runtime as rt
        from infra import state

        stop_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(rt, "stop", stop_mock)
        monkeypatch.setattr(rt, "has_pending_or_live", lambda s: True)

        async def _owns(_u, _s):
            return True
        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)

        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent

        ev = TurnCancelEvent()
        ev.bind_turn_identity(TurnIdentity("turn-wrapped", 1))
        ev.completion.set_result(True)
        state._active_turns["wrapped"] = ev
        ws_mod._handle_stop({"id": "test-user"}, "wrapped", "turn-wrapped")
        assert await _wait(lambda: stop_mock.await_count == 1)
        stop_mock.assert_awaited_once_with(
            "wrapped", expected_turn_id="turn-wrapped", broadcast=ANY,
        )
        assert ev.is_set()

    @pytest.mark.asyncio
    async def test_stop_does_not_cancel_new_turn_after_resend(self, monkeypatch):
        import ws as ws_mod
        from infra import state
        import subagents.cc_runtime as rt

        monkeypatch.setattr(rt, "has_pending_or_live", lambda s: False)

        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent

        old_ev = TurnCancelEvent()
        old_ev.bind_turn_identity(TurnIdentity("turn-old", 1))
        state._active_turns["resend-sid"] = old_ev

        new_ev = TurnCancelEvent()
        new_ev.bind_turn_identity(TurnIdentity("turn-new", 2))

        async def _owns_then_replace(_u, _s):
            state._active_turns["resend-sid"] = new_ev
            return True

        monkeypatch.setattr(ws_mod, "_owns_thread", _owns_then_replace)

        ws_mod._handle_stop({"id": "test-user"}, "resend-sid", "turn-old")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not new_ev.is_set(), "_do_stop must not cancel the new turn"

    @pytest.mark.asyncio
    async def test_stale_native_stop_cannot_cancel_successor(self, monkeypatch):
        import ws as ws_mod
        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent
        from infra import state
        from subagents import cc_runtime, codex_runtime

        async def _owns(_user, _thread_id):
            return True

        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)
        monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
        monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
        stale = TurnCancelEvent()
        stale.bind_turn_identity(TurnIdentity("turn-A", 1))
        successor = TurnCancelEvent()
        successor.bind_turn_identity(TurnIdentity("turn-B", 2))
        state._active_turns["native"] = successor

        await ws_mod._do_stop(
            {"id": "test-user"}, "native", stale, None, "turn-A",
        )

        assert not successor.is_set()

    @pytest.mark.asyncio
    async def test_stop_cleans_exact_native_process_before_harnesses(
        self,
        monkeypatch,
    ):
        import ws as ws_mod
        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent
        from host_access import processes
        from infra import state
        from subagents import cc_runtime, codex_runtime

        events = []
        identity = TurnIdentity("turn-native", 4)
        turn_event = TurnCancelEvent()
        turn_event.bind_turn_identity(identity)
        llm_task = asyncio.create_task(asyncio.Event().wait())
        state._active_turns["native"] = turn_event
        state._active_llm_tasks["native"] = llm_task

        async def _owns(_user, _thread_id):
            return True

        class _ProcessOwner:
            async def terminate_turn(
                self,
                thread_id,
                turn_id,
                *,
                foreground_only,
                reason,
            ):
                assert turn_event.is_set()
                assert llm_task.cancelling()
                events.append((
                    "native",
                    thread_id,
                    turn_id,
                    foreground_only,
                    reason,
                ))
                state._active_turns.pop(thread_id, None)
                state._active_llm_tasks.pop(thread_id, None)
                turn_event.completion.set_result(True)

        async def _stop_cc(*_args, **_kwargs):
            events.append("cc")
            return True

        async def _stop_codex(*_args, **_kwargs):
            events.append("codex")
            return True

        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)
        monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
        monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: True)
        monkeypatch.setattr(cc_runtime, "stop", _stop_cc)
        monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: True)
        monkeypatch.setattr(codex_runtime, "stop", _stop_codex)
        monkeypatch.setattr(
            "ws.stop_control._commit_stop_terminal",
            lambda *_args, **_kwargs: ("cancelled", None),
        )

        await ws_mod._do_stop(
            {"id": "test-user"},
            "native",
            turn_event,
            llm_task,
            identity.turn_id,
        )

        assert events == [
            (
                "native",
                "native",
                "turn-native",
                True,
                "native_turn_interrupted",
            ),
            "cc",
            "codex",
        ]
        with pytest.raises(asyncio.CancelledError):
            await llm_task

    @pytest.mark.asyncio
    async def test_stop_cleanup_failures_never_become_the_stop_result(
        self,
        monkeypatch,
    ):
        import ws as ws_mod
        from core.turn_core import TurnIdentity
        from core.native_turn_control import TurnCancelEvent
        from host_access import processes
        from infra import state
        from subagents import cc_runtime, codex_runtime

        events = []
        identity = TurnIdentity("turn-native", 4)
        turn_event = TurnCancelEvent()
        turn_event.bind_turn_identity(identity)
        state._active_turns["native"] = turn_event
        native_failure = RuntimeError("native process cleanup failed")

        async def _owns(_user, _thread_id):
            return True

        class _ProcessOwner:
            async def terminate_turn(self, *_args, **_kwargs):
                events.append("native")
                raise native_failure

        async def _stop_cc(*_args, **_kwargs):
            events.append("cc")
            raise RuntimeError("cc cleanup failed")

        async def _stop_codex(*_args, **_kwargs):
            events.append("codex")
            return True

        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)
        monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
        monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: True)
        monkeypatch.setattr(cc_runtime, "stop", _stop_cc)
        monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: True)
        monkeypatch.setattr(codex_runtime, "stop", _stop_codex)
        monkeypatch.setattr(
            "ws.stop_control._commit_stop_terminal",
            lambda *_args, **_kwargs: ("cancelled", None),
        )

        receipt = await ws_mod._do_stop(
            {"id": "test-user"},
            "native",
            turn_event,
            None,
            identity.turn_id,
        )

        assert (receipt.outcome, receipt.turn_id) == ("stopped", identity.turn_id)
        assert events == ["native", "cc", "codex"]

    @pytest.mark.asyncio
    async def test_stop_never_signals_native_handle_without_exact_identity(
        self,
        monkeypatch,
        main_db,
    ):
        import ws as ws_mod
        from infra import state
        from subagents import cc_runtime, codex_runtime

        turn_event = asyncio.Event()
        state._active_turns["native"] = turn_event

        async def _owns(_user, _thread_id):
            return True

        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)
        monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
        monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)

        receipt = await ws_mod._do_stop(
            {"id": "test-user"},
            "native",
            turn_event,
            None,
            "turn-native",
        )

        assert receipt.outcome == "no_active_turn"
        assert turn_event.is_set() is False

    def test_stop_noop_for_missing_sid(self):
        import ws as ws_mod
        ws_mod._handle_stop({"id": "test-user"}, None)


class TestSubagentFeedEdited:
    @pytest.fixture(autouse=True)
    def _catalog_target(self, monkeypatch):
        monkeypatch.setattr(
            "infra.execution_class.resolve_execution_target",
            lambda _model: cc_target("sonnet"),
        )

    def _cc_subagent_row(self):
        return {
            "id": "child-1",
            "model": "claude-code/sonnet",
            "spawned_by": "parent",
        }

    @pytest.mark.asyncio
    async def test_malformed_subagent_input_rejection_is_input_scoped_not_terminal(
        self, monkeypatch
    ):
        import ws as ws_mod

        async def _owned(_user, _thread_id):
            raise AssertionError("malformed input must fail before authorization")

        monkeypatch.setattr(ws_mod, "_owned_thread", _owned)

        class _WS:
            def __init__(self):
                self.sent: list[dict] = []

            async def send_json(self, message):
                self.sent.append(message)

        ws = _WS()
        await ws_mod._handle_subagent_feed(
            ws, {"id": "test-user"}, "child-1", "", [], False,
            "input-malformed-child",
        )

        assert ws.sent == [{
            "type": "error",
            "thread_id": "child-1",
            "message": "Malformed subagent input",
            "input_id": "input-malformed-child",
        }]
        assert "turn_id" not in ws.sent[0]
        assert "turn_order" not in ws.sent[0]

    @pytest.mark.asyncio
    async def test_unauthorized_subagent_input_rejection_is_input_scoped_not_terminal(
        self, monkeypatch
    ):
        import ws as ws_mod

        async def _owned(_user, _thread_id):
            return None

        monkeypatch.setattr(ws_mod, "_owned_thread", _owned)

        class _WS:
            def __init__(self):
                self.sent: list[dict] = []

            async def send_json(self, message):
                self.sent.append(message)

        ws = _WS()
        await ws_mod._handle_subagent_feed(
            ws, {"id": "attacker"}, "child-1", "steal", [], False,
            "input-unauthorized-child",
        )

        assert ws.sent == [{
            "type": "error",
            "thread_id": "child-1",
            "message": "Thread not found",
            "input_id": "input-unauthorized-child",
        }]
        assert "turn_id" not in ws.sent[0]
        assert "turn_order" not in ws.sent[0]

    @pytest.mark.asyncio
    async def test_feed_handler_propagates_edited(self, monkeypatch):
        import ws as ws_mod
        import subagents.cc_runtime as rt

        feed_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(rt, "feed", feed_mock)
        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(
                driver="cc_runtime.feed", message_id="m1",
                created_at="2026-07-14T12:01:00+00:00",
                turn_id="turn-child", turn_order=9,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input)

        async def _owned(_u, _s):
            return self._cc_subagent_row()
        monkeypatch.setattr(ws_mod, "_owned_thread", _owned)

        class _WS:
            async def send_json(self, m):
                pass

        await ws_mod._handle_subagent_feed(
            _WS(), {"id": "test-user"}, "child-1", "edited text", [], True,
            "input-edited",
        )
        feed_mock.assert_not_awaited()
        assert calls[0][2] == "edited text"
        assert calls[0][3]["input_id"] == "input-edited"
        assert calls[0][3]["metadata"]["edited"] is True

    @pytest.mark.asyncio
    async def test_feed_handler_default_not_edited(self, monkeypatch):
        import ws as ws_mod
        import subagents.cc_runtime as rt

        feed_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(rt, "feed", feed_mock)
        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(
                driver="cc_runtime.feed", message_id="m1",
                created_at="2026-07-14T12:01:00+00:00",
                turn_id="turn-child", turn_order=9,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input)

        async def _owned(_u, _s):
            return self._cc_subagent_row()
        monkeypatch.setattr(ws_mod, "_owned_thread", _owned)

        class _WS:
            async def send_json(self, m):
                pass

        await ws_mod._handle_subagent_feed(
            _WS(), {"id": "test-user"}, "child-1", "plain text", [], False,
            "input-plain",
        )
        feed_mock.assert_not_awaited()
        assert calls[0][2] == "plain text"
        assert calls[0][3]["metadata"] is None

    @pytest.mark.asyncio
    async def test_feed_with_attachments_records_edited_frame(self, monkeypatch, tmp_path):
        import ws as ws_mod
        import subagents.cc_runtime as rt
        import infra.config as cfg

        uid = "test-user"
        (tmp_path / uid / "fid1").mkdir(parents=True)
        (tmp_path / uid / "fid1" / "x.png").write_bytes(b"img")
        monkeypatch.setattr(cfg, "UPLOADS_DIR", tmp_path)

        feed_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(rt, "feed", feed_mock)
        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(
                driver="cc_runtime.feed", message_id="m1",
                created_at="2026-07-14T12:01:00+00:00",
                turn_id="turn-child", turn_order=9,
            )

        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input)

        async def _owned(_u, _s):
            return self._cc_subagent_row()
        monkeypatch.setattr(ws_mod, "_owned_thread", _owned)

        class _WS:
            async def send_json(self, m):
                pass

        await ws_mod._handle_subagent_feed(
            _WS(), {"id": uid}, "child-1", "edited text",
            [{"file_id": "fid1", "filename": "x.png"}], True,
            "input-attachment",
        )

        feed_mock.assert_not_awaited()
        assert len(calls) == 1
        kwargs = calls[0][3]
        assert kwargs["metadata"]["edited"] is True
        assert kwargs["metadata"]["attachments"] == [
            {"file_id": "fid1", "filename": "x.png"}
        ]
        assert kwargs["broadcast_input"] is False
        assert kwargs["attachments"] == [
            {"file_id": "fid1", "filename": "x.png"}
        ]
        assert "dispatch_content" not in kwargs

    @pytest.mark.asyncio
    async def test_feed_handler_uses_shared_send_service_for_cc_child(
        self, monkeypatch, tmp_path
    ):
        import ws as ws_mod
        import infra.config as cfg
        import subagents.cc_runtime as rt

        uid = "test-user"
        (tmp_path / uid / "fid1").mkdir(parents=True)
        (tmp_path / uid / "fid1" / "x.png").write_bytes(b"img")
        monkeypatch.setattr(cfg, "UPLOADS_DIR", tmp_path)

        async def _owned(_u, _s):
            return self._cc_subagent_row()
        monkeypatch.setattr(ws_mod, "_owned_thread", _owned)

        calls: list[tuple[str, object, str, dict]] = []

        async def _send_thread_input(thread_id, thread_row, content, **kwargs):
            calls.append((thread_id, thread_row, content, kwargs))
            return SimpleNamespace(
                driver="cc_runtime.feed", message_id="m1",
                created_at="2026-07-14T12:01:00+00:00",
                turn_id="turn-child", turn_order=9,
            )

        feed_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(ws_mod, "send_thread_input", _send_thread_input, raising=False)
        monkeypatch.setattr(rt, "feed", feed_mock)

        class _WS:
            def __init__(self):
                self.sent = []

            async def send_json(self, m):
                self.sent.append(m)

        ws = _WS()
        await ws_mod._handle_subagent_feed(
            ws, {"id": uid}, "child-1", "edited text",
            [{"file_id": "fid1", "filename": "x.png"}], True,
            "input-child",
        )

        feed_mock.assert_not_awaited()
        assert ws.sent == [{
            "type": "input_accepted", "thread_id": "child-1",
            "input_id": "input-child", "message_id": "m1",
            "created_at": "2026-07-14T12:01:00+00:00",
            "turn_id": "turn-child", "turn_order": 9,
        }]
        assert len(calls) == 1
        sid, session, clean_content, kwargs = calls[0]
        assert sid == "child-1"
        assert session["spawned_by"] is not None
        assert clean_content == "edited text"
        assert kwargs["native_mode"] == "delegation"
        assert kwargs["broadcast_input"] is False
        assert kwargs["touch_thread_on_insert"] is True
        assert kwargs["trim_content"] is False
        assert kwargs["metadata"]["edited"] is True
        assert kwargs["attachments"] == [
            {"file_id": "fid1", "filename": "x.png"}
        ]
        assert "dispatch_content" not in kwargs


class TestWsHandlerL2:
    @pytest.mark.asyncio
    async def test_shared_stop_reaps_stalled_cc_and_thread_accepts_successor(
        self, main_db, fake_claude_stalled_reader, tmp_path, monkeypatch,
    ):
        import ws as ws_mod
        from infra import state
        from subagents import cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        target = cc_target("opus")
        claude_dir = str(tmp_path / "empty-home")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", claude_dir)

        async def _owns(_user, _thread_id):
            return True

        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)
        monkeypatch.setattr(
            state,
            "owner_broadcast",
            lambda _uid: lambda sid, message: casts.append((sid, message)),
        )
        first_turn = "turn-cc-stalled-stop"
        await feed_main_with_persisted_input(
            "main-1", "HANG-NOW", target=target, turn_id=first_turn,
            binary=fake_claude_stalled_reader,
            claude_dir=claude_dir,
            broadcast=lambda sid, message: casts.append((sid, message)),
        )
        assert await _wait(lambda: any(
            message.get("type") == "step_text" for _, message in casts
        ))
        stopped_session = rt._LIVE.get("main-1").session

        ws = SimpleNamespace(send_json=AsyncMock())
        ws_mod._handle_stop(
            {"id": "steve"}, "main-1", first_turn, ws=ws,
        )

        assert await _wait(lambda: ws.send_json.await_count == 1)
        ws.send_json.assert_awaited_once_with({
            "type": "stop_receipt", "thread_id": "main-1",
            "outcome": "stopped", "turn_id": first_turn,
        })
        assert stopped_session.returncode is not None
        assert stopped_session._reader.done() and not stopped_session._reader.cancelled()
        terminal = _turn_messages(casts, first_turn, "turn_cancelled")
        assert len(terminal) == 1
        assert main_db.execute(
            "SELECT status FROM active_turns WHERE id=?", (first_turn,),
        ).fetchone()["status"] == "cancelled"
        first_turn_frames = len(_turn_messages(casts, first_turn))
        await asyncio.sleep(0.05)
        assert len(_turn_messages(casts, first_turn)) == first_turn_frames

        successor = "turn-cc-after-stop"
        await feed_main_with_persisted_input(
            "main-1", "after stop", target=target, turn_id=successor,
            binary=fake_claude_stalled_reader,
            claude_dir=claude_dir,
            broadcast=lambda sid, message: casts.append((sid, message)),
        )
        assert await _wait(lambda: main_db.execute(
            "SELECT status FROM active_turns WHERE id=?", (successor,),
        ).fetchone()["status"] == "completed")
        assert await _wait(lambda: not rt.is_live("main-1"))

    @pytest.mark.asyncio
    async def test_handle_stop_closes_live_cc_main(
        self, main_db, fake_claude, tmp_path, monkeypatch
    ):
        import ws as ws_mod
        import subagents.cc_runtime as rt

        target = cc_target("opus")
        await feed_main_with_persisted_input(
            "main-1", "hello",
            target=target,
            turn_id="turn-cc-main-stop",
            binary=fake_claude,
            claude_dir=str(tmp_path / "empty-home"),
            broadcast=lambda s, m: None,
        )
        assert await _wait(lambda: rt.is_live("main-1"))
        live = rt._LIVE.get("main-1")
        assert live is not None and live.corsia_turn is not None
        expected_turn_id = live.corsia_turn.turn_id

        async def _owns(_u, _s):
            return True
        monkeypatch.setattr(ws_mod, "_owns_thread", _owns)

        ws_mod._handle_stop(
            {"id": "test-user"}, "main-1", expected_turn_id,
        )
        assert await _wait(lambda: not rt.is_live("main-1"), tries=200), \
            "_handle_stop did not terminate the live CC main process"
