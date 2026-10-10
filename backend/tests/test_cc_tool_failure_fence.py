"""Loopback failures fence their provider before becoming consumable MCP input."""

import asyncio
import json
import sys

import pytest

from test_cc_tool_result_echo_binding import _seed_thread, _tool_use


async def _live_with_result_step(db, identity, broadcast, calls, *, with_step=True):
    from subagents.cc_handle_close import finalize
    from subagents.cc_process_exit import handle_process_exit
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE, _Live
    from subagents.cc_stream import CCStreamSession

    class Registry:
        def get(self, _name):
            return None

        async def execute(self, name, arguments):
            calls.append((name, arguments))
            return "canonical completed result"

    session = CCStreamSession(binary="unused", session_id="failure", corsia_thread_id="thread-1")
    session._proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import signal; signal.pause()",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    live = _Live(session=session, parent_thread_id=None, name="cc", corsia_turn=identity,
                 tool_registry=Registry(), tool_token="exact-token", turn_active=True,
                 catalog_model_id="claude-code/sonnet")
    _LIVE.put("thread-1", live)

    async def exited(code, stderr):
        await handle_process_exit("thread-1", live, code, stderr, broadcast, finalize_callback=finalize)

    session._on_exit = exited
    session._on_event = _make_on_event(live, "thread-1", broadcast, db)
    session._reader = asyncio.create_task(session._read_loop())
    if with_step:
        await session._on_event(_tool_use("call", "file.txt"))
    db.commit()
    return live


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["failure", "stop", "natural_exit", "late_result"])
async def test_post_result_failure_fences_before_reply_and_keeps_one_canonical_result(
    isolated_db, monkeypatch, order,
):
    from api import cc_attach
    from infra.database import get_db
    from subagents import harness_tool_bridge
    from subagents.cc_runtime_state import _LIVE

    db = get_db()
    identity = _seed_thread(db)
    frames, calls = [], []
    broadcast = lambda _thread, frame: frames.append(frame)
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    live = await _live_with_result_step(db, identity, broadcast, calls)
    session = live.session
    stop_first = order == "stop"

    async def projection_failed(*_args):
        if stop_first:
            live.checkpoint_wait_scope.request_stop()
        elif order == "natural_exit":
            session._proc.kill()
        raise RuntimeError("projection infrastructure unavailable")

    monkeypatch.setattr(harness_tool_bridge, "_media_delivery", projection_failed)
    try:
        from tools.recorded_call import CompletedToolFailure

        with pytest.raises(CompletedToolFailure) as raised:
            await cc_attach._execute_admitted_tool(live, "thread-1", {
                "name": "read_file", "arguments": {"path": "file.txt"},
                "invocation_id": "invocation", "provider_tool_call_id": "call",
            })
        assert session._closing, "consumable error escaped before the provider was fenced"
        [completed] = raised.value.completed
        assert completed.result == "canonical completed result"
        assert completed.canonical is not None
        assert completed.invocation_id == "invocation"
        if order == "late_result":
            await session._dispatch_raw_event({"type": "result", "is_error": False, "result": "late success"})
            await asyncio.wait_for(asyncio.shield(session._terminal_event), 2)
        if stop_first:
            from subagents.cc_stop import stop

            assert await asyncio.wait_for(stop("thread-1", expected_turn_id=identity.turn_id), 2)
        assert live.tool_token == "" and live.tool_registry is None
        assert not live.admitted_tool_tasks
        if not stop_first:
            assert session._terminal_driver is not None
            await asyncio.wait_for(asyncio.shield(session._terminal_driver), 2)
        assert session.returncode is not None
        assert _LIVE.get("thread-1") is None
        await session._on_exit(session.returncode, "late duplicate process exit")
        turn = db.execute("SELECT status,error FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()
        expected = ({"status": "cancelled", "error": None} if stop_first else
                    {"status": "error", "error": str(raised.value)})
        assert dict(turn) == expected
        results = db.execute("SELECT content,metadata FROM messages WHERE thread_id='thread-1' AND role='tool'").fetchall()
        assert [row["content"] for row in results] == ["canonical completed result"]
        metadata = json.loads(results[0]["metadata"])
        assert metadata["tool_invocation_id"] == "invocation"
        assert not metadata.get("synthetic_tool_result")
        assert calls == [("read_file", {"path": "file.txt"})]
        terminals = [frame for frame in frames if frame["type"] in {"turn_complete", "turn_cancelled"}]
        assert len(terminals) == 1
        assert terminals[0]["type"] == ("turn_cancelled" if stop_first else "turn_complete")
    finally:
        await session.terminate()
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["reply", "persistence"])
async def test_cc_reply_settlement_failure_retains_completed_result(isolated_db, monkeypatch, fault):
    from api import cc_attach
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE
    from tools.recorded_call import CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    frames, calls = [], []
    broadcast = lambda _thread, frame: frames.append(frame)
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    live = await _live_with_result_step(db, identity, broadcast, calls)
    session = live.session

    async def failed(*args, **kwargs):
        raise RuntimeError("api_key=synthetic-private-settlement")

    if fault == "reply":
        monkeypatch.setattr(session, "record_tool_reply", failed)
    else:
        monkeypatch.setattr("subagents.harness_tool_bridge._persist_delivery_off_loop", failed)
    try:
        with pytest.raises(CompletedToolFailure) as raised:
            await cc_attach._execute_admitted_tool(live, "thread-1", {
                "name": "read_file", "arguments": {"path": "file.txt"},
                "invocation_id": "invocation", "provider_tool_call_id": "call",
            })
        [completed] = raised.value.completed
        assert completed.result == "canonical completed result"
        assert (completed.canonical is not None) == (fault == "reply")
        assert completed.invocation_id == "invocation"
        assert session._closing
        await asyncio.wait_for(asyncio.shield(session._terminal_driver), 2)
        turn = db.execute("SELECT status,error FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()
        assert turn["status"] == "error" and "synthetic-private-settlement" not in turn["error"]
        assert calls == [("read_file", {"path": "file.txt"})]
        assert len([f for f in frames if f["type"] == "turn_complete"]) == 1
        assert [r[0] for r in db.execute("SELECT content FROM messages WHERE role='tool'")] == ["canonical completed result"]
    finally:
        await session.terminate()
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_concurrent_cc_failure_retains_peer_cancelled_after_effect(
    isolated_db, monkeypatch,
):
    """One failing callback cannot discard a peer's already-returned effect."""
    from api import cc_attach
    from infra.database import get_db
    from subagents import harness_tool_bridge
    from subagents.cc_runtime_state import _LIVE
    from tools.recorded_call import CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    frames, calls = [], []
    broadcast = lambda _thread, frame: frames.append(frame)
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    live = await _live_with_result_step(db, identity, broadcast, calls)
    session = live.session
    await session._on_event(_tool_use("call-two", "two.txt"))

    class Registry:
        def get(self, _name):
            return None

        async def execute(self, name, arguments):
            calls.append((name, dict(arguments)))
            return f"{arguments['path']} completed result"

    live.tool_registry = Registry()
    second_persisting = asyncio.Event()

    async def persistence_boundary(**kwargs):
        path = kwargs["arguments"]["path"]
        if path == "two.txt":
            second_persisting.set()
            await asyncio.Event().wait()
        await second_persisting.wait()
        raise RuntimeError("first result persistence unavailable")

    monkeypatch.setattr(harness_tool_bridge, "_persist_delivery_off_loop", persistence_boundary)
    second = asyncio.create_task(cc_attach._execute_admitted_tool(live, "thread-1", {
        "name": "read_file", "arguments": {"path": "two.txt"},
        "invocation_id": "invocation-two", "provider_tool_call_id": "call-two",
    }))
    await asyncio.wait_for(second_persisting.wait(), 1)
    first = asyncio.create_task(cc_attach._execute_admitted_tool(live, "thread-1", {
        "name": "read_file", "arguments": {"path": "file.txt"},
        "invocation_id": "invocation-one", "provider_tool_call_id": "call",
    }))
    try:
        first_error, second_error = await asyncio.gather(
            first, second, return_exceptions=True,
        )
        assert isinstance(first_error, CompletedToolFailure)
        assert isinstance(second_error, CompletedToolFailure)
        assert session._terminal_driver is not None
        await asyncio.wait_for(asyncio.shield(session._terminal_driver), 2)
        assert calls == [
            ("read_file", {"path": "two.txt"}),
            ("read_file", {"path": "file.txt"}),
        ]
        rows = db.execute(
            "SELECT content,metadata FROM messages WHERE role='tool' ORDER BY content"
        ).fetchall()
        assert [row["content"] for row in rows] == [
            "file.txt completed result", "two.txt completed result",
        ]
        invocation_ids = sorted(json.loads(row["metadata"])["tool_invocation_id"] for row in rows)
        assert invocation_ids == ["invocation-one", "invocation-two"]
        assert len([frame for frame in frames if frame["type"] == "turn_complete"]) == 1
    finally:
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await session.terminate()
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_late_failure_does_not_fence_successor_or_reused_turn():
    from core.turn_core import TurnIdentity
    from subagents.cc_stop import stop
    from subagents.cc_runtime_quiescence import fence_tool_failure, finish_tool_failure
    from subagents.cc_runtime_state import _LIVE, _Live
    from test_cc_receipt_liveness import _session

    identity = TurnIdentity("old-turn", 1)
    old = _Live(session=_session(), parent_thread_id=None, name="old", corsia_turn=identity, turn_active=True)
    successor = _Live(session=_session(), parent_thread_id=None, name="new",
                      corsia_turn=TurnIdentity("new-turn", 2), turn_active=True)
    _LIVE.put("thread-1", successor)
    try:
        assert not fence_tool_failure("thread-1", old, identity, RuntimeError("late"))
        await finish_tool_failure("thread-1", old, identity)
        assert not await stop("thread-1", expected_turn_id=identity.turn_id)
        assert _LIVE.get("thread-1") is successor
        assert successor.session.returncode is None
        _LIVE.put("thread-1", old)
        old.corsia_turn = successor.corsia_turn
        assert not fence_tool_failure("thread-1", old, identity, RuntimeError("stale Turn"))
        assert old.session.returncode is None
    finally:
        _LIVE.pop("thread-1", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_error", [False, True])
@pytest.mark.parametrize("stop_first", [False, True])
async def test_result_owner_cancels_unknown_receipt_after_canonical_completion(
    isolated_db, monkeypatch, provider_error, stop_first,
):
    from api import cc_attach
    from infra.database import get_db
    from subagents import harness_tool_bridge
    from subagents.cc_runtime_state import _LIVE

    db = get_db()
    identity = _seed_thread(db)
    frames, calls = [], []
    broadcast = lambda _thread, frame: frames.append(frame)
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    live = await _live_with_result_step(db, identity, broadcast, calls)
    session = live.session
    session.tool_context.finished = True
    session.tool_context.tool_ids = {"previous-call"}
    projected = asyncio.Event()
    original = harness_tool_bridge._media_delivery

    async def projection(*args, **kwargs):
        result = await original(*args, **kwargs)
        projected.set()
        return result

    monkeypatch.setattr(harness_tool_bridge, "_media_delivery", projection)
    pending = asyncio.create_task(cc_attach._execute_admitted_tool(live, "thread-1", {
        "name": "read_file", "arguments": {"path": "file.txt"},
        "invocation_id": "invocation", "provider_tool_call_id": "call",
    }))
    try:
        await asyncio.wait_for(projected.wait(), 1)
        assert not pending.done()
        if stop_first:
            live.checkpoint_wait_scope.request_stop()
        await session._dispatch_raw_event({"type": "result", "is_error": provider_error, "result": "provider result"})
        await asyncio.wait_for(asyncio.shield(session._terminal_event), 1)
        if stop_first:
            from subagents.cc_stop import stop

            assert await asyncio.wait_for(stop("thread-1", expected_turn_id=identity.turn_id), 2)
        assert pending.done(), "terminal left an admitted unknown receipt waiting on a live process"
        assert pending.cancelled()
        assert not live.admitted_tool_tasks
        if session._terminal_driver is not None:
            await asyncio.wait_for(asyncio.shield(session._terminal_driver), 2)
        turn = db.execute("SELECT status,error FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()
        assert turn["status"] == ("cancelled" if stop_first else "error")
        assert turn["error"] is None if stop_first else "tool context" in turn["error"]
        assert session._closing and session.returncode is not None
        assert _LIVE.get("thread-1") is None
        results = db.execute("SELECT content FROM messages WHERE thread_id='thread-1' AND role='tool'").fetchall()
        assert [row["content"] for row in results] == ["canonical completed result"]
        assert calls == [("read_file", {"path": "file.txt"})]
        terminals = [frame for frame in frames if frame["type"] in {"turn_complete", "turn_cancelled"}]
        assert len(terminals) == 1
        assert terminals[0]["type"] == ("turn_cancelled" if stop_first else "turn_complete")
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await session.terminate()
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_result_without_admitted_callbacks_remains_successful(isolated_db):
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    db = get_db()
    identity = _seed_thread(db)
    frames, calls = [], []
    live = await _live_with_result_step(
        db, identity, lambda _thread, frame: frames.append(frame), calls, with_step=False,
    )
    session = live.session
    try:
        await session._dispatch_raw_event({"type": "result", "is_error": False, "result": "done"})
        await asyncio.wait_for(asyncio.shield(session._terminal_event), 1)
        turn = db.execute("SELECT status FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()
        assert turn["status"] == "completed"
        assert session.returncode is None and not session._closing
        assert _LIVE.get("thread-1") is live
        assert not calls
        assert len([frame for frame in frames if frame["type"] == "turn_complete"]) == 1
    finally:
        await session.terminate()
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_result_quiescence_does_not_fail_a_reused_live_generation():
    from core.turn_core import TurnIdentity
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live
    from test_cc_receipt_liveness import _session

    live = _Live(session=_session(), parent_thread_id=None, name="reused",
                 corsia_turn=TurnIdentity("old", 1), turn_active=True)
    session = live.session
    session._on_event = _make_on_event(live, "thread-1", lambda *_: None, object())
    cancelled, release = asyncio.Event(), asyncio.Event()

    async def admitted():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise
        finally:
            live.admitted_tool_tasks.discard(asyncio.current_task())

    previous = asyncio.create_task(admitted())
    live.admitted_tool_tasks.add(previous)
    await asyncio.sleep(0)
    successor = None
    try:
        await session._dispatch_raw_event({"type": "result", "is_error": True, "result": "old result"})
        await asyncio.wait_for(cancelled.wait(), 1)
        live.corsia_turn = TurnIdentity("successor", 2)
        successor = asyncio.create_task(admitted())
        live.admitted_tool_tasks.add(successor)
        release.set()
        await asyncio.wait_for(asyncio.shield(session._terminal_event), 1)
        assert not successor.done() and session.returncode is None
        assert live.turn_active
    finally:
        release.set()
        for task in (previous, successor):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (previous, successor) if task is not None), return_exceptions=True)
