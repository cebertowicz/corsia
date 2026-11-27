"""The application owns and reaps its worker pool through lifespan teardown."""

import pytest
from fastapi import FastAPI

import app_lifecycle
from infra import agent_pool
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
