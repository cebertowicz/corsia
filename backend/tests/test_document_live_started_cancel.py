"""Real cancellation stops the caller immediately and preserves independent writes."""
import asyncio

import pytest

from doceditor.live_lane import Operation
from tests.test_document_live_lane import open_lane, cut_at


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [None, "foreign preemption"])
async def test_started_hard_cancel_records_result_then_stops_caller(tmp_path, monkeypatch, message):
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    jobs = []
    enqueue = lane._enqueue
    def capture(*args, **kwargs):
        job = enqueue(*args, **kwargs)
        jobs.append(job)
        return job
    monkeypatch.setattr(lane, "_enqueue", capture)
    async def caller():
        await lane.submit(Operation("replace", content="STARTED RESULT"))
        await lane.submit(Operation("replace", content="MUST NOT RUN"))
    task = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel(message)
        done, _ = await asyncio.wait({task}, timeout=.1)
        assert task in done
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert raised.value.args == (() if message is None else (message,))
        assert len(jobs) == 1 and not jobs[0].future.done()
        release.set()
        await lane._worker
        result = jobs[0].future.result()
        assert result.outcome == "landed" and result.text == "STARTED RESULT" and result.server_saved
        assert live.text == live.path.read_text() == "STARTED RESULT"
        assert live.store.pending() is None
        reopened = await type(live).open(live.path, live.store.path)
        try:
            assert reopened.text == "STARTED RESULT"
        finally:
            reopened.close()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await lane.close()
        live.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_started_cancellation_keeps_each_real_request(tmp_path, cancel_count, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    task = asyncio.create_task(lane.submit(Operation("replace", content="STOP RESULT")))
    try:
        await entered.wait()
        for _ in range(cancel_count):
            task.cancel("Stop")
        done, _ = await asyncio.wait({task}, timeout=.1)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelling() == cancel_count
        assert not release.is_set() and not lane._worker.done()
        release.set()
        await lane._worker
        assert live.text == live.path.read_text() == "STOP RESULT"
        assert live.store.pending() is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await lane.close()
        live.close()
