"""A held exit remains owned through real application lifespan shutdown."""

import asyncio

from fastapi import FastAPI

import app_lifecycle
from llm import server_observation
from llm.providers import omlx_restart, omlx_supervisor
from test_app_lifecycle_shutdown import _isolate_lifespan
from test_omlx_upgrade import _seat


async def test_shutdown_joins_upgrade_after_watched_exit(monkeypatch, isolated_db):
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
