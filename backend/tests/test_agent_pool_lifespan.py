"""The application owns and reaps its worker pool through lifespan teardown."""

import pytest
from fastapi import FastAPI

import app_lifecycle
from infra import agent_pool, worker_wait_timing


@pytest.mark.asyncio
async def test_untouched_lifespan_does_not_start_pool_or_wait_flusher(monkeypatch):
    _isolate_lifespan(monkeypatch)
    async def unexpected_spawn(*args, **kwargs):
        pytest.fail("untouched lifespan spawned a child")
    monkeypatch.setattr(agent_pool.asyncio, "create_subprocess_exec", unexpected_spawn)
    assert agent_pool._pool is None
    assert worker_wait_timing._task is None
    async with app_lifecycle.lifespan(FastAPI()):
        pool = agent_pool._pool
        assert pool is not None
        assert not pool._workers and not pool._pending_cleanup
        assert worker_wait_timing._task is None
    assert not pool._workers and not pool._pending_cleanup
    assert agent_pool._pool is None
    assert worker_wait_timing._task is None
from tests.test_app_lifecycle_shutdown import _isolate_lifespan


@pytest.mark.asyncio
async def test_lifespan_reaps_an_assigned_worker(monkeypatch):
    _isolate_lifespan(monkeypatch)
    async with app_lifecycle.lifespan(FastAPI()):
        pool = agent_pool.start()
        result = await pool.call(
            "fixture.echo", {"value": 7}, agent_id="test", thread_id="test",
            queue_timeout=10, execution_timeout=10,
        )
        assert result == {"value": 7}
        worker, = pool._workers
        assert worker.returncode is None
    assert worker.returncode is not None
    assert not pool._workers
