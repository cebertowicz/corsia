"""The re-drive after a checkpoint belongs to its thread and to process shutdown."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from loguru import logger

from core.fork_types import ProviderForkResult
from tests.test_checkpoint_runtime import _checkpoint, _provider, _seed


@pytest.fixture(autouse=True)
async def _clean_runtime():
    from core import checkpoint_runtime

    await checkpoint_runtime.shutdown_fork_tasks()
    yield
    await checkpoint_runtime.shutdown_fork_tasks()


async def _install(monkeypatch, thread_id: str, drive) -> None:
    from core import checkpoint_runtime as runtime
    from infra import thread_execution

    async def invoke(_request, _cancel):
        return ProviderForkResult("body", None)

    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(thread_execution, "drive_thread", drive)
    _seed(thread_id)
    await runtime.request_checkpoint(
        _checkpoint(thread_id, "cp-1"), _provider(thread_id, "cp-1"),
    )
    assert (await runtime.await_checkpoint("cp-1")).status == "installed"


@pytest.mark.asyncio
async def test_failed_wake_is_logged_for_its_thread_and_leaves_health(
    isolated_db, monkeypatch
):
    from main import app

    async def drive(_thread_id):
        raise RuntimeError("drive refused")

    logged = asyncio.Event()

    def sink(message) -> None:
        if "checkpoint completion wake failed: thread-wake" in message:
            logged.set()

    sink_id = logger.add(sink, level="ERROR", format="{message}")
    try:
        await _install(monkeypatch, "thread-wake", drive)
        await asyncio.wait_for(logged.wait(), 5)
    finally:
        logger.remove(sink_id)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_wake_is_held_until_done_and_shutdown_joins_it(
    isolated_db, monkeypatch
):
    import gc

    from core import checkpoint_runtime as runtime

    started = asyncio.Event()
    cancelled = False

    async def drive(_thread_id):
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    await _install(monkeypatch, "thread-held", drive)
    await asyncio.wait_for(started.wait(), 5)
    gc.collect()

    (wake,) = tuple(runtime._wakes)
    assert wake.get_name() == "checkpoint-wake:thread-held"
    assert not wake.done()

    await runtime.shutdown_fork_tasks()

    assert cancelled
    assert wake.done()
    assert not runtime._wakes
