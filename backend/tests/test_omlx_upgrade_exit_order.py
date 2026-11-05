"""An exit during adoption cannot replace the held startup upgrade."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm import network_discovery
from llm.providers import omlx_restart, omlx_startup, omlx_supervisor, omlx_upgrade
from test_omlx_upgrade_startup_order import _finish, forbid_external_process_and_network  # noqa: F401


@pytest.mark.parametrize("clean", [False, True])
async def test_exit_during_version_read_cannot_bypass_upgrade_admission(monkeypatch, clean):
    from llm import server_observation

    version_read, release = asyncio.Event(), asyncio.Event()
    callback_done, state_release = asyncio.Event(), asyncio.Event()
    watched = []
    monkeypatch.setattr(omlx_supervisor, "_life", {})
    monkeypatch.setattr(omlx_supervisor, "start_server", AsyncMock(return_value={"ok": False}))
    monkeypatch.setattr(omlx_supervisor, "_stop_server", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(omlx_supervisor, "_tail_log", lambda _sid: "")
    monkeypatch.setattr(omlx_startup, "run_in_db", AsyncMock(side_effect=lambda fn, *args: fn(*args)))
    monkeypatch.setattr(network_discovery, "get_servers", lambda: [{"id": "seat", "provider": "omlx"}])
    monkeypatch.setattr(omlx_startup, "_reconcile_servers", lambda *_args: {"seat": 123})
    monkeypatch.setattr("llm.local_sources.corsia_omlx_server", lambda _servers: {"id": "seat"})
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])

    async def state(_sid, *, reads):
        await state_release.wait()
        return "unreadable"

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", state)
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)

    async def version(_sid):
        version_read.set()
        await release.wait()
        return "old"

    def watch_exit(_pid, callback):
        watched.append(True)

        def exit_callback():
            callback(("exited during version read", clean))
            callback_done.set()

        asyncio.get_running_loop().call_soon(exit_callback)
        return lambda: None

    monkeypatch.setattr(omlx_upgrade, "recorded_version", version)
    monkeypatch.setattr("llm.providers.omlx_process.watch_exit", watch_exit)
    startup = asyncio.create_task(omlx_startup.adopt_and_upgrade(SimpleNamespace(info=lambda *_args: None)))
    task = waiter = None
    try:
        await asyncio.wait_for(version_read.wait(), 2)
        assert not watched
        release.set()
        task = await startup
        assert task is not None
        await asyncio.wait_for(callback_done.wait(), 2)
        assert omlx_supervisor._life["seat"].restart is task
        assert omlx_supervisor._life["seat"].hold_requests

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                pytest.fail("a crash restart bypassed the held upgrade")

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        state_release.set()
        with pytest.raises(RuntimeError, match="upgrade failed"):
            await asyncio.wait_for(waiter, 2)
    finally:
        release.set()
        state_release.set()
        await _finish(waiter, task, startup)
