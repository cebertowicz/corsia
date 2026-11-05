"""A held exit remains owned through real application lifespan shutdown."""

import asyncio

from fastapi import FastAPI
import pytest

import app_lifecycle
from llm import server_observation
from llm.providers import omlx_restart, omlx_supervisor
from test_app_lifecycle_shutdown import _isolate_lifespan
from test_omlx_upgrade import _seat


async def test_reconciliation_failure_allows_cloud_startup(monkeypatch, isolated_db):
    _isolate_lifespan(monkeypatch)
    cloud_started = asyncio.Event()
    shutdown_tasks = []
    original_shutdown = app_lifecycle._shutdown_owned_tasks

    async def fail(_logger):
        raise OSError("reconciliation failed")

    async def cloud():
        cloud_started.set()

    async def capture_shutdown(tasks, logger):
        shutdown_tasks.extend(tasks)
        return await original_shutdown(tasks, logger)

    monkeypatch.setattr(app_lifecycle, "adopt_and_upgrade", fail)
    monkeypatch.setattr(app_lifecycle, "_startup_cloud_discovery", cloud)
    monkeypatch.setattr(app_lifecycle, "_shutdown_owned_tasks", capture_shutdown)
    async with app_lifecycle.lifespan(FastAPI()):
        await asyncio.wait_for(cloud_started.wait(), 5)
    assert len(shutdown_tasks) == 6 and shutdown_tasks[5] is None


async def test_version_read_failure_keeps_all_adopted_watches_and_cloud_startup(monkeypatch, isolated_db):
    from unittest.mock import AsyncMock
    from llm import network_discovery
    from llm.providers import omlx_startup, omlx_upgrade

    _isolate_lifespan(monkeypatch)
    monkeypatch.setattr(app_lifecycle, "adopt_and_upgrade", omlx_startup.adopt_and_upgrade)
    monkeypatch.setattr(omlx_startup, "run_in_db", AsyncMock(side_effect=lambda fn, *args: fn(*args)))
    monkeypatch.setattr(network_discovery, "get_servers", lambda: [{"id": "seat"}, {"id": "other"}])
    monkeypatch.setattr(omlx_startup, "_reconcile_servers", lambda *_args: {"seat": 123, "other": 124})
    monkeypatch.setattr("llm.local_sources.corsia_omlx_server", lambda _servers: {"id": "seat"})
    watched = []
    monkeypatch.setattr(omlx_supervisor, "_life", {})

    def watch(sid, pid):
        watched.append((sid, pid))
        omlx_supervisor._life.setdefault(sid, omlx_restart.Life())

    monkeypatch.setattr(omlx_supervisor, "watch", watch)
    monkeypatch.setattr(omlx_upgrade, "recorded_version", AsyncMock(side_effect=OSError("version unavailable")))
    cloud_started = asyncio.Event()

    async def cloud():
        cloud_started.set()

    monkeypatch.setattr(app_lifecycle, "_startup_cloud_discovery", cloud)
    monkeypatch.setattr(app_lifecycle, "_startup_network_discovery", AsyncMock())
    async with app_lifecycle.lifespan(FastAPI()):
        await asyncio.wait_for(cloud_started.wait(), 5)
        omlx_upgrade.recorded_version.assert_awaited_once_with("seat")
        assert set(watched) == {("seat", 123), ("other", 124)}


async def test_reconciliation_cancellation_propagates(monkeypatch, isolated_db):
    _isolate_lifespan(monkeypatch)

    async def cancel(_logger):
        raise asyncio.CancelledError

    monkeypatch.setattr(app_lifecycle, "adopt_and_upgrade", cancel)
    with pytest.raises(asyncio.CancelledError):
        async with app_lifecycle.lifespan(FastAPI()):
            pytest.fail("cancelled startup yielded")


@pytest.mark.parametrize("broadcast_failure", [False, True])
async def test_shutdown_joins_upgrade_after_watched_exit(monkeypatch, isolated_db, broadcast_failure):
    _isolate_lifespan(monkeypatch)
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    stage, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = None

    async def state(_sid, *, reads):
        stage.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async def adopt(_logger):
        nonlocal original
        if broadcast_failure:
            def fail_broadcast(_message):
                raise RuntimeError("broadcast failed")

            with monkeypatch.context() as patch:
                patch.setattr("infra.state.ws.broadcast_all", fail_broadcast)
                original = omlx_supervisor.schedule_startup_upgrade("seat", "old")
        else:
            original = omlx_supervisor.schedule_startup_upgrade("seat", "old")
        return original

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", state)
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: ["model"])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_lifecycle, "adopt_and_upgrade", adopt)

    async def run_lifespan():
        async with app_lifecycle.lifespan(FastAPI()):
            await asyncio.wait_for(stage.wait(), 5)
            omlx_restart.on_exit("seat", life, 123, omlx_supervisor.start_server, lambda _sid: "", ("exit", False))
            assert life.restart is original

    lifespan = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(cleaning.wait(), 10)
        assert not lifespan.done()
        release.set()
        await asyncio.wait_for(lifespan, 10)
        assert original is not None and original.done()
    finally:
        release.set()
        if not lifespan.done():
            lifespan.cancel()
        await asyncio.gather(lifespan, return_exceptions=True)
