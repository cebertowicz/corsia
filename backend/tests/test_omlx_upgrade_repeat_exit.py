"""Repeated watched exits stay with the original held upgrade."""

import asyncio
from unittest.mock import AsyncMock

from llm import server_observation
from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat
from test_omlx_upgrade_startup_order import _finish, forbid_external_process_and_network  # noqa: F401


async def test_repeated_exit_restores_original_snapshot_before_admission(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    entered = asyncio.Queue()
    releases = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    calls = 0

    async def reload(_sid, models):
        nonlocal calls
        assert models == ["exact-model"]
        index = calls
        calls += 1
        await entered.put(index)
        await releases[index].wait()

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", AsyncMock(return_value=[{"identifier": "exact-model"}]))
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload)
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    waiter = None
    try:
        assert await asyncio.wait_for(entered.get(), 2) == 0

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                return "admitted"

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        for index in (0, 1):
            omlx_restart.on_exit("seat", life, 123 + index, omlx_supervisor.start_server, lambda _sid: "", ("exit", False))
            assert life.restart is owner and not waiter.done()
            releases[index].set()
            assert await asyncio.wait_for(entered.get(), 2) == index + 1
        assert not waiter.done()
        releases[2].set()
        assert await asyncio.wait_for(waiter, 2) == "admitted"
    finally:
        for release in releases:
            release.set()
        await _finish(waiter, owner)
