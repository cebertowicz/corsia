"""Outside traffic prevents replacement; startup recovery is bounded."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from infra import carry_in_capacity as capacity
from llm.providers import omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat, _upgrade
# Calls are exercised through the supervisor's real admission slot.


async def test_busy_external_activity_fails_held_calls_without_stopping(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    checking, release_check = asyncio.Event(), asyncio.Event()

    async def state(_sid, *, reads):
        assert reads == 2
        checking.set()
        await release_check.wait()
        return capacity.BUSY

    monkeypatch.setattr(capacity, "omlx_state", state)
    upgrade = asyncio.create_task(_upgrade())
    await asyncio.wait_for(checking.wait(), 2)

    async def request():
        async with omlx_supervisor.request_slot("seat"):
            pytest.fail("a held call entered after the failed upgrade")

    waiter = asyncio.create_task(request())
    await asyncio.sleep(0)
    release_check.set()
    assert (await asyncio.wait_for(upgrade, 2))["reason"] == "busy"
    with pytest.raises(RuntimeError, match="Check the server in Settings and try again"):
        await waiter
    assert calls == []


async def test_unreadable_final_proof_defers_without_stop(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    monkeypatch.setattr(capacity, "omlx_state", AsyncMock(return_value=capacity.UNREADABLE))

    assert await _upgrade() == {"restarted": False, "reason": "not_idle"}
    assert calls == []
    assert not omlx_supervisor.is_restarting("seat")


async def test_failed_replacement_start_recovers_then_restores_exact_models(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    attempts = 0
    events = []

    async def start(_cfg):
        nonlocal attempts
        attempts += 1
        calls.append(("start", "seat"))
        return {"ok": attempts == 2, "message": "boot failed"}

    monkeypatch.setattr(omlx_supervisor, "_start_recovery", start)
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    assert (await _upgrade())["restarted"] is True
    assert [call[0] for call in calls] == ["loaded", "stop", "start", "start", "reload", "loaded"]
    assert [action for action, _ in events].count("restart.completed") == 1
    assert [action for action, _ in events].count("restart.attempted") == 2
    assert "restart.gave_up" not in [action for action, _ in events]


async def test_exhausted_replacement_starts_emit_terminal_failure(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old", start_ok=False)
    events = []
    monkeypatch.setattr(omlx_supervisor, "_tail_log", lambda _sid: "test boot failure")
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    assert await _upgrade() == {"restarted": False, "reason": "start_failed"}
    assert [call[0] for call in calls] == ["loaded", "stop", "start", "start", "start"]
    assert events[-1][0] == "restart.gave_up"
    assert not omlx_supervisor.is_restarting("seat")


async def test_failed_upgrade_stop_emits_terminal_failure(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    events = []
    monkeypatch.setattr(omlx_supervisor, "_stop_server", AsyncMock(return_value={"ok": False}))
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    assert await _upgrade() == {"restarted": False, "reason": "stop_failed"}
    assert events[-1] == ("restart.failed", {"status": "upgrade", "reason": "stop_failed"})
    assert omlx_supervisor._life["seat"].stopping is False
