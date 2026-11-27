"""Pool cleanup remains bounded and recoverable after interrupted calls."""

import asyncio
import logging

import pytest

from infra import agent_pool, request_timing


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["timeout", "cancel"])
async def test_interrupted_call_returns_before_reaping_but_keeps_capacity(monkeypatch, interrupt):
    pool = agent_pool.AgentPool(limit=1, shutdown_timeout=0.05)
    # A subprocess object is hashable; use a stand-in with the same property.
    proc = type("Process", (), {"returncode": None})()
    pool._workers.add(proc)
    reaping = asyncio.Event()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def acquire():
        return proc

    async def stalled_io(_proc, operation):
        operation.close()
        entered.set()
        await asyncio.Event().wait()

    async def reap(_proc):
        reaping.set()
        await release.wait()
        async with pool._condition:
            pool._workers.discard(proc)
            pool._condition.notify_all()

    monkeypatch.setattr(pool, "_acquire", acquire)
    monkeypatch.setattr(pool, "_run_io", stalled_io)
    monkeypatch.setattr(pool, "_reap", reap)
    monkeypatch.setattr(request_timing, "record_worker_wait", lambda *_: None)
    call = asyncio.create_task(pool.call(
        "fixture.echo", "x", agent_id="a", thread_id="t", queue_timeout=1,
        execution_timeout=0.03 if interrupt == "timeout" else 10,
    ))
    await asyncio.wait_for(entered.wait(), 1)
    if interrupt == "cancel":
        call.cancel()
    with pytest.raises(asyncio.TimeoutError if interrupt == "timeout" else asyncio.CancelledError):
        await asyncio.wait_for(call, 0.3)
    await asyncio.wait_for(reaping.wait(), 1)
    assert proc in pool._workers
    with pytest.raises(agent_pool.PoolError, match="shutdown timed out"):
        await asyncio.wait_for(pool.close(), 0.3)
    assert proc in pool._workers
    release.set()
    await asyncio.wait_for(pool.close(), 1)
    assert not pool._workers


@pytest.mark.asyncio
async def test_idle_timer_observes_failure_and_shutdown_retries(monkeypatch, caplog):
    pool = agent_pool.AgentPool(limit=1, idle_seconds=0.01, shutdown_timeout=1)
    proc = type("Process", (), {"returncode": None})()
    pool._workers.add(proc)
    attempts = 0

    async def acquire():
        return proc

    async def complete(_proc, operation):
        operation.close()
        return "ok"

    async def reap(_proc):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("idle reap failed")
        async with pool._condition:
            pool._workers.discard(proc)
            pool._condition.notify_all()

    monkeypatch.setattr(pool, "_acquire", acquire)
    monkeypatch.setattr(pool, "_run_io", complete)
    monkeypatch.setattr(pool, "_reap", reap)
    monkeypatch.setattr(request_timing, "record_worker_wait", lambda *_: None)
    with caplog.at_level(logging.ERROR, logger=agent_pool.__name__):
        assert await pool.call("fixture.echo", "x", agent_id="a", thread_id="t",
                               queue_timeout=1, execution_timeout=1) == "ok"
        async with asyncio.timeout(1):
            while pool._timer_tasks:
                await asyncio.sleep(0.001)
    assert "Worker idle cleanup failed: RuntimeError" in caplog.text
    assert proc in pool._workers and proc not in pool._reapers
    await pool.close()
    assert attempts == 2 and not pool._workers


@pytest.mark.asyncio
async def test_reaper_failure_after_waiter_cancel_is_reported_and_retryable(monkeypatch, caplog):
    pool = agent_pool.AgentPool(limit=1)
    proc = object()
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def reap(_proc):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("reap failed after waiter left")

    monkeypatch.setattr(pool, "_reap", reap)
    with caplog.at_level(logging.ERROR, logger=agent_pool.__name__):
        waiter = asyncio.create_task(pool._discard(proc))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert "Worker reap failed: RuntimeError" in caplog.text
    assert proc not in pool._reapers
    await pool._discard(proc)
    assert attempts == 2


@pytest.mark.asyncio
async def test_failed_background_reap_is_reported_and_close_can_retry(monkeypatch, caplog):
    pool = agent_pool.AgentPool(limit=1, shutdown_timeout=1)
    proc = object()
    pool._workers.add(proc)
    attempts = 0

    async def reap(_proc):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first reap failed")
        async with pool._condition:
            pool._workers.discard(proc)
            pool._condition.notify_all()

    monkeypatch.setattr(pool, "_reap", reap)
    with caplog.at_level(logging.ERROR, logger=agent_pool.__name__):
        pool._track_cleanup(pool._discard(proc))
        await asyncio.gather(*list(pool._pending_cleanup), return_exceptions=True)
        await asyncio.sleep(0)
    assert "Worker cleanup failed: RuntimeError" in caplog.text
    assert proc in pool._workers
    assert proc not in pool._reapers
    await pool.close()
    assert attempts == 2
    assert not pool._workers
