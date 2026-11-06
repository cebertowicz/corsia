"""A held recovery batch has a finite restart budget even across long stages."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from llm import server_observation
from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat
from test_omlx_upgrade_startup_order import _finish, forbid_external_process_and_network  # noqa: F401


async def test_repeated_exits_do_not_refresh_held_budget(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    now = [1.0]
    monkeypatch.setattr(omlx_restart, "time", SimpleNamespace(monotonic=lambda: now[0]))
    entered = asyncio.Queue()
    release = asyncio.Queue()

    async def reload(_sid, _models):
        await entered.put(True)
        await release.get()

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", AsyncMock(return_value=[{"identifier": "model"}]))
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload)
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    try:
        for index in range(omlx_restart.MAX_RESTARTS):
            await asyncio.wait_for(entered.get(), 2)
            now[0] += omlx_restart.RESTART_WINDOW_S + 1
            omlx_restart.on_exit("seat", life, 123 + index, omlx_supervisor.start_server, lambda _sid: "", ("exit", False))
            await release.put(True)
        result = await asyncio.wait_for(asyncio.shield(owner), 2)
        assert result["reason"] == "start_failed"
    finally:
        await _finish(owner)
