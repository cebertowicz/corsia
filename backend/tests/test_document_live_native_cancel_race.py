"""Native's event wait must never consume a real cancellation count."""
import asyncio

import pytest

from doceditor.live_lane import Operation
from tests.test_document_live_lane import open_lane, cut_at


@pytest.mark.asyncio
async def test_native_event_and_hard_cancel_same_drain_step_preserve_cancel_count(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    event = asyncio.Event()
    settle = lane._settle_native
    requested = []
    async def same_step(*args):
        # At the real boundary between event observation and the value wait,
        # request a hard cancellation before any future wait.
        task = asyncio.current_task()
        event.set()
        task.cancel("hard preemption")
        requested.append(task.cancelling())
        return await settle(*args)
    monkeypatch.setattr(lane, "_settle_native", same_step)
    call = asyncio.create_task(lane.submit(Operation("replace", content="LANDED LATER"), cancel_event=event))
    try:
        await entered.wait()
        event.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await call
        assert type(caught.value) is asyncio.CancelledError
        assert caught.value.args == ("hard preemption",)
        assert requested == [1]
        assert call.cancelling() == 1
        assert not lane._worker.done()
        release.set()
        await lane._worker
        assert live.path.read_text() == "LANDED LATER"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()
