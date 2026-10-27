"""A timed-out steer settles its write and leaves the session usable."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from subagents import codex_inbox_batch as inbox
from tests.test_codex_inbox_steer import (
    _clear_live, _enqueue, _input_row, _install_live, _seed_live_turn,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["ws", "api", "tool_result"])
@pytest.mark.parametrize("write_observed", [False, True])
async def test_owner_timeout_releases_lock_for_every_nudge(
    isolated_db, monkeypatch, caplog, source, write_observed,
):
    from infra.database import get_db
    from infra.send_service import InboxQueuedInput, send_thread_input
    from subagents import codex_runtime_event_delivery as events
    from subagents.codex_runtime_state import _LIVE
    from ws.ingress import background_tasks

    thread_id = f"timeout-{source}-{write_observed}"
    from infra.db_async import run_in_db
    identity = await run_in_db(_seed_live_turn, thread_id)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Session:
        def peek_active_usage(self):
            return None

        async def steer(self, text, *, on_write_observed, **kwargs):
            if write_observed:
                on_write_observed()
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(inbox, "INTERRUPT_RESPONSE_TIMEOUT_SECONDS", 0.05, raising=False)
    live = _install_live(thread_id, identity, Session())
    previous = set(background_tasks)
    send = None
    try:
        if source == "tool_result":
            # Exercise the actual tool-result handler and its nudge scheduler;
            # the tool's already-committed result is outside this steer test.
            async def settled_result(event):
                return None
            monkeypatch.setattr(events.codex_runtime_events, "make_on_event", lambda *a, **k: settled_result)
            await run_in_db(_enqueue, thread_id, message_id="timeout-input")
            await events.runtime_event_handler(live, thread_id, lambda *a: None)({"kind": "tool_result"})
        else:
            with get_db() as db:
                row = dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
            send = asyncio.create_task(send_thread_input(
                thread_id, row, "timeout input", input_id="timeout-input",
                native_mode=source, ws=object() if source == "ws" else None,
            ))
            done, _ = await asyncio.wait({send}, timeout=1)
            assert send in done, "apply-now send waited for the timing nudge"
            assert isinstance(send.result(), InboxQueuedInput)
        await asyncio.wait_for(entered.wait(), 1)
        tasks = set(background_tasks) - previous
        assert tasks, "safe-boundary scheduler must retain its work"
        done, pending = await asyncio.wait(tasks, timeout=1)
        assert not pending, "steer exceeded its owner response bound"
        await asyncio.gather(*done, return_exceptions=True)
        assert cancelled.is_set()
        async with asyncio.timeout(1):
            async with _LIVE.lock(thread_id):
                pass
        row = _input_row("timeout-input")
        assert row["delivery_state"] == ("ambiguous" if write_observed else "queued")
        assert "steer timed out" in caplog.text
    finally:
        tasks = set(background_tasks) - previous
        if send is not None:
            tasks.add(send)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _clear_live(thread_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["drain", "ack"])
async def test_late_ack_after_owner_timeout_leaves_session_usable(
    isolated_db, monkeypatch, blocked_phase,
):
    from infra.db_async import run_in_db
    from subagents import codex_app_server as server

    thread_id = f"late-ack-{blocked_phase}"
    identity = await run_in_db(_seed_live_turn, thread_id)
    await run_in_db(_enqueue, thread_id, message_id="first-steer")
    session = server.CodexAppServerSession(binary="codex", task=None)
    session.thread_id = "provider-thread"
    session._track_active_turn("provider-turn")
    requests = []

    class Transport:
        def write(self, data):
            message = json.loads(data)
            requests.append(message)
            if len(requests) > 1:
                asyncio.get_running_loop().call_soon(session._handle_response, {"id": message["id"], "result": {}})

        async def drain(self):
            if blocked_phase == "drain" and len(requests) == 1:
                await asyncio.Event().wait()

    session._proc = SimpleNamespace(stdin=Transport(), returncode=None)
    monkeypatch.setattr(inbox, "INTERRUPT_RESPONSE_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(server, "STEER_ACK_TIMEOUT", 10)
    _install_live(thread_id, identity, session)
    task = asyncio.create_task(inbox.deliver_live_inbox_batch(thread_id))
    try:
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "canonical steer did not time out"
        with pytest.raises(TimeoutError):
            task.result()
        assert _input_row("first-steer")["delivery_state"] == "ambiguous"
        assert session._pending == session._pending_methods == {}
        await session._handle_message({"id": requests[0]["id"], "result": {}})
        assert session._active_turn_id == "provider-turn"
        await run_in_db(_enqueue, thread_id, message_id="following-steer")
        assert await inbox.deliver_live_inbox_batch(thread_id) is True
        assert _input_row("following-steer")["delivery_state"] == "delivered"
        assert _input_row("first-steer")["delivery_state"] == "ambiguous"
        assert len(requests) == 2
        assert session._pending == session._pending_methods == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _clear_live(thread_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_observed", [False, True])
async def test_cancelled_steer_propagates_after_write_settlement(isolated_db, write_observed):
    from infra.db_async import run_in_db
    from subagents.codex_runtime_state import _LIVE

    thread_id = f"cancel-steer-{write_observed}"
    identity = await run_in_db(_seed_live_turn, thread_id)
    await run_in_db(_enqueue, thread_id, message_id="cancelled-steer")
    entered = asyncio.Event()

    class Session:
        async def steer(self, text, *, on_write_observed, **kwargs):
            if write_observed:
                on_write_observed()
            entered.set()
            await asyncio.Event().wait()

    _install_live(thread_id, identity, Session())
    task = asyncio.create_task(inbox.deliver_live_inbox_batch(thread_id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not _LIVE.lock(thread_id).locked()
        assert _input_row("cancelled-steer")["delivery_state"] == ("ambiguous" if write_observed else "queued")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _clear_live(thread_id)
