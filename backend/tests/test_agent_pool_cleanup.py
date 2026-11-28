"""Pool cleanup retains capacity and recovers from failed reaping."""
import asyncio
import logging

import pytest

from infra import agent_pool, worker_wait_timing


@pytest.mark.asyncio
async def test_idle_reap_failure_retains_capacity_and_shutdown_retries(monkeypatch, caplog):
    pool = agent_pool.AgentPool(limit=1, idle_seconds=0.02, shutdown_timeout=1)
    monkeypatch.setattr(worker_wait_timing, "record_worker_wait", lambda *_: None)
    original = pool._reap
    failed = asyncio.Event()
    attempts = 0

    async def reap(worker):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            failed.set()
            raise RuntimeError("reap failed")
        await original(worker)

    monkeypatch.setattr(pool, "_reap", reap)
    try:
        with caplog.at_level(logging.ERROR, logger=agent_pool.__name__):
            assert await pool.call("fixture.echo", "ok", agent_id="a", thread_id="t",
                                   queue_timeout=1, execution_timeout=1) == "ok"
            worker = next(iter(pool._workers.values()))
            await asyncio.wait_for(failed.wait(), 1)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert worker.state is agent_pool.WorkerState.REAP_FAILED
            with pytest.raises(asyncio.TimeoutError):
                await pool.call("fixture.echo", "next", agent_id="a", thread_id="t",
                                queue_timeout=0.03, execution_timeout=1)
            assert "Worker reap failed: RuntimeError" in caplog.text
        await pool.close()
        assert attempts == 2
        assert worker.proc.returncode is not None
        assert not pool._workers
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_cancelled_reap_waiter_does_not_cancel_cleanup(monkeypatch):
    pool = agent_pool.AgentPool(limit=1, idle_seconds=10)
    monkeypatch.setattr(worker_wait_timing, "record_worker_wait", lambda *_: None)
    await pool.call("fixture.echo", "ok", agent_id="a", thread_id="t",
                    queue_timeout=1, execution_timeout=1)
    worker = next(iter(pool._workers.values()))
    worker.timer.cancel()
    entered, release = asyncio.Event(), asyncio.Event()
    original = pool._reap

    async def delayed(worker):
        entered.set()
        await release.wait()
        await original(worker)

    monkeypatch.setattr(pool, "_reap", delayed)
    waiter = asyncio.create_task(pool._discard(worker))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert worker.state is agent_pool.WorkerState.REAPING
    release.set()
    await pool.close()
    assert worker.proc.returncode is not None
    assert not pool._workers
