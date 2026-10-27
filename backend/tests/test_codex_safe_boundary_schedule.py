"""Codex safe-boundary wakes do not block their caller."""

import asyncio
import logging

import pytest

from infra.send_service import request_safe_boundary
from subagents import codex_inbox_batch, codex_runtime_event_delivery as delivery


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
        tasks = [task for task in delivery._SAFE_BOUNDARY_TASKS if task.get_name() == "codex-safe-boundary:thread-1"]
        assert len(tasks) == 1 and not tasks[0].done()
        tasks[0].add_done_callback(lambda _: finished.set())
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert tasks[0] not in delivery._SAFE_BOUNDARY_TASKS
        failures = [record for record in caplog.records if "deferred provider event failed" in record.message]
        assert len(failures) == int(fails)
        if fails:
            assert failures[0].exc_info[1].args == ("delivery failed",)
        else:
            assert tasks[0].result() is True
