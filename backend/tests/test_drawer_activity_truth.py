"""#576 — drawer activity/unread truth.

A turn TERMINAL (``turn_complete`` / ``turn_cancelled`` / ``turn_interrupted`` /
``error``) must reach all of a user's connected clients so off-thread drawer
spinners stop and unread surfaces — not just the socket viewing the thread.
``error`` was the violation (an errored turn emits no ``turn_complete``), and an
event-woken native thread broadcast session-scoped only (off-thread = nobody).
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock

import pytest

from core.turn_core import TurnIdentity

def _native_target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id="mock/model", harness="native", provider="mock",
        backend="api", transport_model="mock/model",
        expected_reported_model=None, context_window=200_000,
        available=True, availability_reason=None,
        display_name="Mock Model", supports_reasoning=False,
    )


def _codex_target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id="codex/gpt-5-codex", harness="codex", provider="codex",
        backend="codex", transport_model="gpt-5-codex",
        expected_reported_model="gpt-5-codex", context_window=200_000,
        available=True, availability_reason=None,
        display_name="GPT-5 Codex", supports_reasoning=True,
    )

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from cc_runtime_fixtures import (  # noqa: E402,F401
    _seed_parent,
    _wait,
    cc_target,
    child_db,
    fake_claude,
)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class _FailingWS:
    async def send_json(self, _message: dict) -> None:
        raise RuntimeError("socket closed")


class _ExclusiveWS:
    """A real WebSocket permits only one writer at a time."""

    def __init__(self) -> None:
        self.sending = False
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        if self.sending:
            raise RuntimeError("concurrent websocket send")
        self.sending = True
        await asyncio.sleep(0)
        self.sent.append(message)
        self.sending = False


@pytest.fixture
def ws_buckets():
    from infra.state import ws as ws_state

    sess = dict(ws_state._ws_thread_clients)
    usr = dict(ws_state._ws_user_clients)
    ws_state._ws_thread_clients.clear()
    ws_state._ws_user_clients.clear()
    try:
        yield ws_state
    finally:
        ws_state._ws_thread_clients.clear()
        ws_state._ws_thread_clients.update(sess)
        ws_state._ws_user_clients.clear()
        ws_state._ws_user_clients.update(usr)


@pytest.mark.asyncio
async def test_error_terminal_reaches_off_thread_clients(ws_buckets):
    """``error`` fans out to the user's off-thread clients exactly once."""
    viewer = _FakeWS()   # viewing the errored thread
    other = _FakeWS()    # same user, looking at a different thread
    ws_buckets._ws_thread_clients["s1"] = [viewer]
    ws_buckets._ws_user_clients["u1"] = [viewer, other]

    frame = {"type": "error", "thread_id": "s1", "message": "boom"}
    ws_buckets.broadcast_event("s1", "u1", frame)
    await asyncio.sleep(0)

    assert viewer.sent == [frame]          # session leg
    assert other.sent == [frame]           # cross-tab leg, no duplicate


@pytest.mark.asyncio
async def test_explicit_turn_activity_fans_out_once_and_chunks_synthesize_none(
    ws_buckets,
):
    viewer = _FakeWS()
    other = _FakeWS()
    ws_buckets._ws_thread_clients["s-activity"] = [viewer]
    ws_buckets._ws_user_clients["u-activity"] = [viewer, other]
    activity = {
        "type": "turn_activity",
        "thread_id": "s-activity",
        "turn_id": "turn-activity",
        "turn_order": 7,
    }

    ws_buckets.broadcast_event("s-activity", "u-activity", activity)
    for event_type in (
        "text_delta", "step_reasoning", "step_text", "tool_start", "tool_done",
    ):
        ws_buckets.broadcast_event(
            "s-activity",
            "u-activity",
            {
                "type": event_type,
                "thread_id": "s-activity",
                "turn_id": "turn-activity",
                "turn_order": 7,
            },
        )
    await asyncio.sleep(0)

    assert [message for message in viewer.sent
            if message.get("type") == "turn_activity"] == [activity]
    assert [message for message in other.sent
            if message.get("type") == "turn_activity"] == [activity]


@pytest.mark.asyncio
async def test_owner_rejection_terminal_reaches_source_and_peer_with_same_identity(
    ws_buckets,
):
    source = _FakeWS()
    peer = _FakeWS()
    ws_buckets._ws_thread_clients["rejected-thread"] = [source]
    ws_buckets._ws_user_clients["owner"] = [source, peer]
    identity = TurnIdentity("rejected-turn", 8)
    activity = {
        "type": "turn_activity",
        "thread_id": "rejected-thread",
        **identity.frame_fields(),
    }
    terminal = {
        "type": "turn_complete",
        "thread_id": "rejected-thread",
        **identity.frame_fields(),
    }
    broadcast = ws_buckets.owner_broadcast("owner")

    broadcast("rejected-thread", activity)
    broadcast("rejected-thread", terminal)
    await asyncio.sleep(0)

    assert source.sent == [activity, terminal]
    assert peer.sent == [activity, terminal]


@pytest.mark.asyncio
async def test_native_ws_send_emits_one_explicit_start_and_no_chunk_activity(
    ws_buckets,
):
    from ws.turn_helpers import make_ws_send

    viewer = _FakeWS()
    other = _FakeWS()
    identity = TurnIdentity("native-activity", 9)
    ws_buckets._ws_thread_clients["native-session"] = [viewer]
    ws_buckets._ws_user_clients["native-owner"] = [viewer, other]
    send, _closed = make_ws_send(
        viewer,
        "native-owner",
        "native-session",
        identity,
    )

    await send({"type": "turn_activity", "thread_id": "native-session"})
    for event_type in (
        "text_delta", "step_reasoning", "step_text", "tool_start", "tool_done",
    ):
        await send({
            "type": event_type,
            "thread_id": "native-session",
            "content": "chunk",
            "tool_id": "tool-1",
        })
    await asyncio.sleep(0)

    expected = {
        "type": "turn_activity",
        "thread_id": "native-session",
        "turn_id": "native-activity",
        "turn_order": 9,
    }
    assert [message for message in viewer.sent
            if message.get("type") == "turn_activity"] == [expected]
    assert [message for message in other.sent
            if message.get("type") == "turn_activity"] == [expected]


@pytest.mark.asyncio
async def test_native_step_text_keeps_transcript_and_stream_sequences_distinct(
    ws_buckets,
):
    from infra.state import turn_buffers
    from ws.turn_helpers import make_ws_send

    viewer = _FakeWS()
    identity = TurnIdentity("native-step", 10)
    ws_buckets._ws_thread_clients["native-session"] = [viewer]
    turn_buffers.init("native-session", identity)
    try:
        send, _closed = make_ws_send(
            viewer,
            "native-owner",
            "native-session",
            identity,
        )

        await send({
            "type": "step_text",
            "thread_id": "native-session",
            "message_id": "assistant-row",
            "content": "answer",
            "seq": 42,
            "finalized_tool_ids": [],
        })
        await asyncio.sleep(0)

        step = next(message for message in viewer.sent
                    if message.get("type") == "step_text")
        assert step["seq"] == 42
        assert step["stream_seq"] == 1
    finally:
        turn_buffers.clear("native-session", identity)


@pytest.mark.asyncio
async def test_broadcast_observes_failed_ws_send_tasks(ws_buckets, monkeypatch):
    """Closed sockets are best-effort failures, not unhandled task exceptions."""
    created: list[asyncio.Task] = []
    original_create_task = asyncio.create_task

    def _capture_task(coro, *args, **kwargs):
        task = original_create_task(coro, *args, **kwargs)
        created.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", _capture_task)
    failing = _FailingWS()
    ws_buckets._ws_thread_clients["s-send-fails"] = [failing]

    ws_buckets.broadcast_to_thread("s-send-fails", {"type": "ping"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(created) == 1
    assert created[0].done()
    assert getattr(created[0], "_log_traceback", False) is False


@pytest.mark.asyncio
async def test_broadcast_serializes_frames_for_each_websocket(ws_buckets):
    """A final transcript row cannot be dropped behind an in-flight delta."""
    socket = _ExclusiveWS()
    ws_buckets._ws_thread_clients["ordered-thread"] = [socket]
    step = {"type": "step_text", "message_id": "assistant-1"}
    terminal = {"type": "turn_complete", "turn_id": "turn-1"}

    ws_buckets.broadcast_to_thread("ordered-thread", step)
    ws_buckets.broadcast_to_thread("ordered-thread", terminal)
    for _ in range(4):
        await asyncio.sleep(0)

    assert socket.sent == [step, terminal]


@pytest.mark.asyncio
async def test_broadcast_reserves_order_before_a_direct_send(ws_buckets):
    """An awaited control frame cannot overtake an earlier transcript frame."""
    socket = _ExclusiveWS()
    ws_buckets._ws_thread_clients["ordered-thread"] = [socket]
    transcript = {"type": "step_text", "message_id": "assistant-1"}
    control = {"type": "ping"}

    ws_buckets.broadcast_to_thread("ordered-thread", transcript)
    await ws_buckets.send_to_client(socket, control)

    assert socket.sent == [transcript, control]


@pytest.mark.asyncio
async def test_event_dispatch_wires_cross_tab_owner_broadcast(ws_buckets, monkeypatch):
    """An event wake uses the owner's headless shared-engine context."""
    import ws.turn as ws_turn

    captured: dict = {}

    async def fake_execute_turn(**kwargs):
        captured.update(kwargs)
        kwargs["start_ack"].set_result(TurnIdentity(kwargs["turn_id"], 1))

    monkeypatch.setattr(ws_turn, "execute_turn", fake_execute_turn)

    from infra.send_dispatch import dispatch_native_event

    thread_row = {
        "user_id": "owner", "model": "mock/model", "project_id": None,
        "reasoning_effort": "medium",
    }
    target = _native_target()
    driver, turn_task, identity = await dispatch_native_event(
        "s1", thread_row, "hi", None, target=target, turn_id="event-turn-1",
    )
    await turn_task
    assert driver == "turn_engine"
    assert identity.turn_id == "event-turn-1"
    assert captured["ingress"] == "event"
    assert captured["user"] == {"id": "owner"}
    assert captured["user_id"] == "owner"
    assert captured["ws"].user_id == "owner"
    assert captured["target"] is target
    assert captured["attachments"] == []
    assert captured["is_retry"] is False
    assert captured["reasoning_effort"] == "medium"
    assert captured["step_metadata"] is None

    # The wired broadcast must deliver a terminal to the owner's off-thread client.
    other = _FakeWS()
    ws_buckets._ws_user_clients["owner"] = [other]
    # broadcast is a one-arg async callable matching StepHandler's contract.
    await captured["broadcast"]({"type": "turn_complete", "thread_id": "s1"})
    await asyncio.sleep(0)

    assert other.sent == [{"type": "turn_complete", "thread_id": "s1"}]


@pytest.mark.asyncio
async def test_event_dispatch_broadcast_one_arg_contract(ws_buckets, monkeypatch):
    """_broadcast must accept exactly ONE arg (the message dict) — the contract
    StepHandler uses. Calling with (sid, message) or (sid, message, exclude_ws)
    was the arity bug #607 that broke native/local-model event delivery."""
    import ws.turn as ws_turn
    import inspect

    captured: dict = {}

    async def fake_execute_turn(**kwargs):
        captured.update(kwargs)
        kwargs["start_ack"].set_result(TurnIdentity(kwargs["turn_id"], 1))

    monkeypatch.setattr(ws_turn, "execute_turn", fake_execute_turn)

    from infra.send_dispatch import dispatch_native_event

    thread_row = {"user_id": "owner2", "model": "mock/model", "project_id": None}
    await dispatch_native_event(
        "s2", thread_row, "hi", None, target=_native_target(),
        turn_id="event-turn-2",
    )

    assert captured["reasoning_effort"] is None  # Missing persisted field remains compatible.
    broadcast = captured["broadcast"]
    # Must be async (StepHandler awaits it).
    assert inspect.iscoroutinefunction(broadcast), "_broadcast must be async"
    # Must accept exactly one positional arg — calling with two must TypeError.
    sig = inspect.signature(broadcast)
    params = [
        p for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    assert len(params) == 1, (
        f"_broadcast must have exactly 1 required positional param, got {len(params)}: {params}"
    )

    # Calling it as StepHandler does (one-arg) must reach the owner's off-thread WS.
    other = _FakeWS()
    ws_buckets._ws_user_clients["owner2"] = [other]

    await broadcast({"type": "turn_complete", "thread_id": "s2"})
    await asyncio.sleep(0)

    assert other.sent == [{"type": "turn_complete", "thread_id": "s2"}]


# --- Section A: CC/Codex parity — default broadcast is owner-bound cross-tab ---


@pytest.mark.asyncio
async def test_codex_dispatch_wires_owner_broadcast(ws_buckets, monkeypatch):
    """``dispatch_codex`` bakes an owner-bound cross-tab broadcast into the turn
    (from the loaded thread row's ``user_id``), so a Codex terminal reaches the
    owner's off-thread clients — not just sockets viewing the thread."""
    from subagents import codex_runtime

    monkeypatch.setattr(codex_runtime, "ensure_connection_available",
                        lambda db=None: None)
    monkeypatch.setattr(codex_runtime, "has_thread_id",
                        _coro_returning(False))
    monkeypatch.setattr(codex_runtime, "has_prior_messages",
                        _coro_returning(False))

    captured: dict = {}
    bind_input_turn = AsyncMock()

    async def fake_feed_main(thread_id, content, **kwargs):
        captured.update(kwargs)
        await kwargs["bind_input_turn"](kwargs["successor_turn_id"], None)
        return TurnIdentity(kwargs["successor_turn_id"], 7)

    monkeypatch.setattr(codex_runtime, "feed_main", fake_feed_main)

    from infra.send_dispatch import dispatch_codex
    from shared_send_fixtures import prepared_context_with_fit_ledger

    thread_row = {"user_id": "owner", "project_id": None}
    driver, identity = await dispatch_codex(
        "cx1", thread_row, "hi", "codex_main", None,
        target=_codex_target(),
        successor_turn_id="turn-codex",
        bind_input_turn=bind_input_turn,
        allow_fresh_start=True,
        prepared_context=prepared_context_with_fit_ledger(_codex_target()))
    assert driver == "codex_runtime.feed_main"
    assert identity.turn_id == "turn-codex"
    bind_input_turn.assert_awaited_once_with("turn-codex", None)

    # The wired broadcast delivers a terminal to the owner's off-thread client.
    other = _FakeWS()
    ws_buckets._ws_user_clients["owner"] = [other]
    captured["broadcast"]("cx1", {
        "type": "turn_complete", "thread_id": "cx1",
        **identity.frame_fields(),
    })
    await asyncio.sleep(0)

    assert other.sent == [{
        "type": "turn_complete", "thread_id": "cx1",
        "turn_id": "turn-codex", "turn_order": 7,
    }]


@pytest.mark.asyncio
async def test_cc_default_broadcast_reaches_owner_off_thread(
    ws_buckets, child_db, fake_claude
):
    """A CC turn spawned WITHOUT a broadcast override (the production default)
    reaches the spawner's off-thread client cross-tab: spinner-start activity AND
    the terminal — proving the default broadcast is owner-bound, not session-only."""
    import subagents.cc_runtime as rt

    _seed_parent(child_db, sid="parent", user="steve")
    other = _FakeWS()                       # steve, looking at another thread
    ws_buckets._ws_user_clients["steve"] = [other]

    child_id = await rt.spawn(
        "parent", "do it", binary=fake_claude,
        target=cc_target("sonnet"))
    try:
        got_terminal = await _wait(
            lambda: any(m.get("type") == "turn_complete"
                        and m.get("thread_id") == child_id for m in other.sent))
        await asyncio.sleep(0)
        assert got_terminal, "owner off-thread client never received the CC terminal"
        # Activity reached it too (spinner start) — synthesized turn_activity off-thread.
        assert any(m.get("type") == "turn_activity" for m in other.sent)
        # Exactly one terminal (no duplicate): owner client isn't in the session list.
        terminals = [m for m in other.sent if m.get("type") == "turn_complete"]
        assert len(terminals) == 1
    finally:
        await rt.retire(child_id)


def _coro_returning(value):
    async def _f(*_a, **_k):
        return value
    return _f
