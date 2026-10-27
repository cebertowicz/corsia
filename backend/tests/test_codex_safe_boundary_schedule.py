"""Codex safe-boundary wakes do not block their caller."""

import asyncio
import logging

import pytest

from infra.send_service import request_safe_boundary
from subagents import codex_inbox_batch, codex_runtime_event_delivery as delivery


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True])
async def test_repeated_wakes_join_one_stalled_steer(monkeypatch, expires):
    from subagents import codex_app_server
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_app_server_errors import CodexAppServerError

    session = CodexAppServerSession(binary="codex", task=None)
    session.thread_id = "provider-thread"
    session._track_active_turn("provider-turn")
    written = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Stdin:
        def write(self, _payload):
            written.set()

        async def drain(self):
            await release.wait()
            session._handle_response({"id": 1, "result": {}})

    class Proc:
        stdin = Stdin()
        returncode = None

        def kill(self):
            self.returncode = -9

    session._proc = Proc()
    monkeypatch.setattr(codex_app_server, "STEER_REQUEST_TIMEOUT", 0.1 if expires else 2)

    async def deliver(thread_id):
        calls.append(thread_id)
        return await session.steer("queued input")

    monkeypatch.setattr(codex_inbox_batch, "deliver_live_inbox_batch", deliver)
    thread_id = "repeated-stalled-steer"
    try:
        await request_safe_boundary(thread_id, "codex")
        await asyncio.wait_for(written.wait(), 1)
        for _ in range(10):
            await request_safe_boundary(thread_id, "codex")
            delivery.schedule_safe_boundary(thread_id)
        await asyncio.sleep(0)
        tasks = [task for task in asyncio.all_tasks()
                 if task.get_name() == f"codex-safe-boundary:{thread_id}"]
        assert len(tasks) == 1
        assert calls == [thread_id]
        if not expires:
            release.set()
        done, _ = await asyncio.wait(tasks, timeout=1)
        assert done == set(tasks), "boundary outlived the transport deadline"
        if expires:
            with pytest.raises(CodexAppServerError, match="turn/steer"):
                tasks[0].result()
        else:
            assert tasks[0].result() is True
        await asyncio.sleep(0)
        assert not [task for task in asyncio.all_tasks()
                    if task.get_name() == f"codex-safe-boundary:{thread_id}"]
    finally:
        tasks = [task for task in asyncio.all_tasks()
                 if task.get_name() == f"codex-safe-boundary:{thread_id}"]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_boundary_schedules_retained_delivery(monkeypatch, caplog, fails):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def deliver(thread_id):
        assert thread_id == "thread-1"
        started.set()
        await release.wait()
        if fails:
            raise RuntimeError("delivery failed")
        return True

    monkeypatch.setattr(codex_inbox_batch, "deliver_live_inbox_batch", deliver)
    with caplog.at_level(logging.ERROR, logger=delivery.__name__):
        await request_safe_boundary("thread-1", "codex")
        await asyncio.wait_for(started.wait(), 2)
        tasks = [task for task in delivery._SAFE_BOUNDARY_TASKS.values() if task.get_name() == "codex-safe-boundary:thread-1"]
        assert len(tasks) == 1 and not tasks[0].done()
        tasks[0].add_done_callback(lambda _: finished.set())
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert "thread-1" not in delivery._SAFE_BOUNDARY_TASKS
        failures = [record for record in caplog.records if "deferred provider event failed" in record.message]
        assert len(failures) == int(fails)
        if fails:
            assert failures[0].exc_info[1].args == ("delivery failed",)
        else:
            assert tasks[0].result() is True
