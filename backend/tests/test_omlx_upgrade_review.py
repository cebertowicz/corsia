"""Regression coverage for upgrade admission and lifecycle ownership."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock

from llm.omlx_release import OMLX_VERSION
from llm.providers import omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat, _upgrade


async def test_version_rechecked_after_wait(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    versions = iter(["old", OMLX_VERSION])
    monkeypatch.setattr("llm.providers.omlx_process.recorded_omlx_version", lambda *_: next(versions))
    result = await _upgrade()
    assert result == {"restarted": False, "reason": "already_current"}
    assert calls == [("loaded", None)]


async def test_upgrade_resets_crash_budget_and_emits_one_event_per_boot(isolated_db, monkeypatch, caplog):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    life.restarts.extend([time.monotonic()] * omlx_restart.MAX_RESTARTS)
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append(action))
    with caplog.at_level(logging.INFO):
        result = await _upgrade()
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


async def test_unreadable_state_emits_skip(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", AsyncMock(return_value="unreadable"))
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))
    assert (await _upgrade())["reason"] == "not_idle"
    assert calls == []
    assert ("restart.skipped", {"status": "upgrade", "reason": "not_idle"}) in events


async def test_unreadable_loaded_models_emits_skip(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    events = []
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=None))
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))
    assert (await _upgrade())["reason"] == "loaded_unknown"
    assert calls == []
    assert ("restart.skipped", {"status": "upgrade", "reason": "loaded_unknown"}) in events
