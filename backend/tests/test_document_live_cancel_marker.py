"""Canonical Stop owners use ordinary task cancellation without a special marker."""
import asyncio
from types import SimpleNamespace

import pytest
from tests.test_document_live_lane import cut_at


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["claude-code", "codex"])
async def test_stop_owner_delivers_ordinary_cancel_at_awaited_inner_future(owner):
    entered = asyncio.Event()
    inner = asyncio.get_running_loop().create_future()
    seen = []
    async def callback():
        entered.set()
        try:
            await inner
        except asyncio.CancelledError as error:
            seen.append(error.args)
            raise
    task = asyncio.create_task(callback())
    await entered.wait()
    if owner == "claude-code":
        from subagents.cc_runtime_quiescence import cancel_admitted_tool_tasks
        cancel_admitted_tool_tasks(SimpleNamespace(admitted_tool_tasks={task}))
        await asyncio.gather(task, return_exceptions=True)
    else:
        from subagents.codex_tool_requests import ToolRequests
        requests = ToolRequests(SimpleNamespace())
        requests.tasks.add(task)
        await requests.close()
    assert inner.cancelled()
    assert seen == [()]


@pytest.mark.asyncio
async def test_real_cancel_message_reaches_lane_shield_without_waiting(tmp_path, monkeypatch):
    from doceditor.live_lane import Operation
    from tests.test_document_live_lane import open_lane
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    task = asyncio.create_task(lane.submit(Operation("replace", content="accepted")))
    try:
        await entered.wait()
        task.cancel("caller cancellation")
        done, _ = await asyncio.wait({task}, timeout=.1)
        assert task in done
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("caller cancellation",)
        assert not lane._worker.done() and not release.is_set()
        release.set()
        await lane._worker
        assert live.text == live.path.read_text() == "accepted"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await lane.close()
        live.close()
