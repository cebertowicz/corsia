"""Canonical Stop owners must finish even while a document write is stalled."""
import asyncio
from types import SimpleNamespace

import pytest

from doceditor import live_lane
from doceditor.live_lane import Operation
from tests.test_document_live_lane import open_lane, cut_at


def start_owner_close(harness, call, *, deadline):
    if harness == "claude-code":
        from subagents.cc_runtime_quiescence import cancel_admitted_tool_tasks, join_admitted_tool_tasks
        owner = SimpleNamespace(admitted_tool_tasks={call})
        call.add_done_callback(owner.admitted_tool_tasks.discard)
        tasks = cancel_admitted_tool_tasks(owner)
        async def join():
            # Let the real task completion callbacks update the owner's set.
            await asyncio.wait(tasks, timeout=max(0, deadline - asyncio.get_running_loop().time()))
            return await join_admitted_tool_tasks(owner, tasks, deadline=deadline)
        return asyncio.create_task(join())
    from subagents.codex_tool_requests import ToolRequests
    owner = ToolRequests(SimpleNamespace())
    owner.tasks.add(call)
    return asyncio.create_task(owner.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_stop_owner_cancels_caller_and_keeps_started_write(tmp_path, harness, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    call = asyncio.create_task(lane.submit(Operation("replace", content="ACCEPTED")))
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        close = start_owner_close(harness, call, deadline=asyncio.get_running_loop().time() + 2)
        assert await asyncio.wait_for(asyncio.shield(close), .3) is not False
        with pytest.raises(asyncio.CancelledError):
            await call
        assert call.cancelling() == 1
        assert not release.is_set() and not lane._worker.done()
        release.set()
        await lane._worker
        assert live.text == live.path.read_text() == "ACCEPTED"
    finally:
        release.set()
        await asyncio.gather(call, *([close] if close else []), return_exceptions=True)
        await lane.close()
        live.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("stage", ["record", "write", "promote"])
async def test_stalled_write_cannot_stall_canonical_stop_owner(tmp_path, monkeypatch, harness, stage):
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(current):
        if current == stage:
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    if stage == "write":
        writer = lane.write
        async def stalled_writer(text, prior_hash):
            entered.set()
            await release.wait()
            await writer(text, prior_hash)
        monkeypatch.setattr(lane, "write", stalled_writer)
    jobs = []
    enqueue = lane._enqueue
    def capture(*args, **kwargs):
        job = enqueue(*args, **kwargs)
        jobs.append(job)
        return job
    monkeypatch.setattr(lane, "_enqueue", capture)
    call = asyncio.create_task(lane.submit(Operation("replace", content="LATE RESULT")))
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        deadline = asyncio.get_running_loop().time() + 0.3
        close = start_owner_close(harness, call, deadline=deadline)
        assert await asyncio.wait_for(asyncio.shield(close), 0.3) is not False
        assert call.cancelled()
        assert not jobs[0].future.done() and not release.is_set()
        release.set()
        await asyncio.wait_for(asyncio.shield(lane._worker), 2)
        result = jobs[0].future.result()
        assert result.text == "LATE RESULT" and result.server_saved
        assert live.text == live.path.read_text() == "LATE RESULT"
        assert live.store.pending() is None
    finally:
        release.set()
        await asyncio.gather(call, *([close] if close else []), return_exceptions=True)
        await lane.close()
        live.close()


def test_native_settlement_budget_is_below_each_canonical_close_budget():
    from subagents.cc_runtime_state import _CLOSE_LIVE_TIMEOUT_S
    from subagents.codex_app_server_reader import PROCESS_EXIT_TIMEOUT_SECONDS
    from subagents.codex_runtime import _DYNAMIC_TOOL_DRAIN_TIMEOUT_S
    budget = getattr(live_lane, "NATIVE_SETTLE_TIMEOUT_S", float("inf"))
    assert 0 < budget < min(_CLOSE_LIVE_TIMEOUT_S, PROCESS_EXIT_TIMEOUT_SECONDS, _DYNAMIC_TOOL_DRAIN_TIMEOUT_S)


@pytest.mark.asyncio
async def test_repeated_stop_cannot_delay_immediate_cancellation(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    call = asyncio.create_task(lane.submit(Operation("replace", content="EVENTUALLY")))
    async def repeat():
        while not call.done():
            call.cancel("Stop")
            await asyncio.sleep(.005)
    repeats = None
    try:
        await entered.wait()
        repeats = asyncio.create_task(repeat())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(call), .25)
        assert not release.is_set() and not lane._worker.done()
        release.set()
        await lane._worker
        assert live.path.read_text() == "EVENTUALLY"
    finally:
        release.set()
        await asyncio.gather(call, *([repeats] if repeats else []), return_exceptions=True)
        await lane.close()
        live.close()
