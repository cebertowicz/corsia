"""Regression coverage for upgrade admission and lifecycle ownership."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import app_lifecycle
from llm.omlx_release import OMLX_VERSION
from llm.providers import omlx_restart, omlx_supervisor, omlx_upgrade
from test_omlx_upgrade import _seat


async def test_version_rechecked_after_wait(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    versions = iter(["old", OMLX_VERSION])
    monkeypatch.setattr("llm.providers.omlx_process.recorded_omlx_version", lambda *_: next(versions))
    result = await omlx_upgrade.upgrade_managed_omlx()
    assert result == {"restarted": False, "reason": "already_current"}
    assert calls == [("loaded", None)]


async def test_upgrade_resets_crash_budget_and_emits_one_event_per_boot(isolated_db, monkeypatch, caplog):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    life.restarts.extend([time.monotonic()] * omlx_restart.MAX_RESTARTS)
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append(action))
    with caplog.at_level(logging.INFO):
        result = await omlx_upgrade.upgrade_managed_omlx()
    assert result["restarted"] is True
    assert [call[0] for call in calls].count("start") == 1
    assert events.count("restart.attempted") == 1
    assert "exited (upgrade)" not in caplog.text


async def test_crash_restart_does_not_hold_provider_admission():
    life = omlx_restart.Life()
    blocker = asyncio.create_task(asyncio.Event().wait())
    life.restart = blocker
    try:
        async def enter():
            async with omlx_restart.request_slot({"seat": life}, "seat"):
                return True
        assert await asyncio.wait_for(enter(), 0.1)
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)


async def test_network_discovery_owns_upgrade_waiter(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def upgrade():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr("llm.network_discovery.get_servers", lambda: [{"id": "seat"}])
    monkeypatch.setattr(app_lifecycle, "run_in_db", AsyncMock(side_effect=lambda fn, *args: fn(*args)))
    monkeypatch.setattr(app_lifecycle, "_reconcile_omlx_servers", lambda *_: {})
    monkeypatch.setattr("llm.network_discovery._sweep_all_servers", AsyncMock())
    monkeypatch.setattr(app_lifecycle, "_discover_network_servers", AsyncMock(return_value=1))
    monkeypatch.setattr(omlx_upgrade, "upgrade_managed_omlx", upgrade)
    task = asyncio.create_task(app_lifecycle._startup_network_discovery())
    await asyncio.wait_for(entered.wait(), 1)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()


async def test_unreadable_state_emits_skip(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old", quiet=False)
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))
    assert (await omlx_upgrade.upgrade_managed_omlx())["reason"] == "not_idle"
    assert calls == []
    assert ("restart.skipped", {"status": "upgrade", "reason": "not_idle"}) in events


async def test_unreadable_loaded_models_emits_skip(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    events = []
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=None))
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))
    assert (await omlx_upgrade.upgrade_managed_omlx())["reason"] == "loaded_unknown"
    assert calls == []
    assert ("restart.skipped", {"status": "upgrade", "reason": "loaded_unknown"}) in events
