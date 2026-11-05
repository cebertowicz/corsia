"""Outside traffic cannot hold admission closed; replacement uses bounded startup recovery."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from infra import carry_in_capacity as capacity
from llm.providers import omlx_restart, omlx_supervisor, omlx_upgrade
from test_omlx_upgrade import _seat
from test_omlx_upgrade_admission import _call, _cancel, _transport


async def test_busy_after_admission_reopens_calls_and_waits_outside_restart(isolated_db, monkeypatch):
    real_wait = capacity.wait_until_quiet
    calls, _ = _seat(monkeypatch, recorded="old")
    entered, finish = _transport(monkeypatch)
    checking, release_check = asyncio.Event(), asyncio.Event()
    outside_wait, allow_idle = asyncio.Event(), asyncio.Event()
    final_checks = 0

    async def state(_sid, *, reads):
        nonlocal final_checks
        assert reads == 2
        if omlx_supervisor.is_restarting("seat"):
            final_checks += 1
            if final_checks == 1:
                checking.set()
                await release_check.wait()
                return capacity.BUSY
            return capacity.IDLE
        if final_checks:
            outside_wait.set()
            await allow_idle.wait()
        return capacity.IDLE

    monkeypatch.setattr(capacity, "wait_until_quiet", real_wait)
    monkeypatch.setattr(capacity, "omlx_state", state)
    upgrade = asyncio.create_task(omlx_upgrade.upgrade_managed_omlx())
    await asyncio.wait_for(checking.wait(), 2)
    request = asyncio.create_task(_call())
    try:
        await asyncio.sleep(0)
        assert not entered.is_set()
        release_check.set()
        await asyncio.wait_for(outside_wait.wait(), 2)
        await asyncio.wait_for(entered.wait(), 2)
        assert not omlx_supervisor.is_restarting("seat")
        assert calls == []  # the busy direct client was not stopped
        finish.set()
        assert (await asyncio.wait_for(request, 2)).content == "answered"
        allow_idle.set()
        assert (await asyncio.wait_for(upgrade, 2))["restarted"] is True
        assert final_checks == 2
    finally:
        await _cancel(request, upgrade)


async def test_unreadable_final_proof_defers_without_stop(isolated_db, monkeypatch):
    real_wait = capacity.wait_until_quiet
    calls, _ = _seat(monkeypatch, recorded="old")
    monkeypatch.setattr(capacity, "wait_until_quiet", real_wait)
    monkeypatch.setattr(capacity, "omlx_state", AsyncMock(side_effect=[capacity.IDLE, capacity.UNREADABLE]))

    assert await omlx_upgrade.upgrade_managed_omlx() == {"restarted": False, "reason": "not_idle"}
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

    monkeypatch.setattr(omlx_supervisor, "start_server", start)
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    assert (await omlx_upgrade.upgrade_managed_omlx())["restarted"] is True
    assert [call[0] for call in calls] == ["loaded", "stop", "start", "start", "reload", "loaded"]
    assert [action for action, _ in events].count("restart.completed") == 1
    assert [action for action, _ in events].count("restart.attempted") == 2
    assert "restart.gave_up" not in [action for action, _ in events]


async def test_exhausted_replacement_starts_emit_terminal_failure(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old", start_ok=False)
    events = []
    monkeypatch.setattr(omlx_supervisor, "_tail_log", lambda _sid: "test boot failure")
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    assert await omlx_upgrade.upgrade_managed_omlx() == {"restarted": False, "reason": "start_failed"}
    assert [call[0] for call in calls] == ["loaded", "stop", "start", "start", "start"]
    assert events[-1][0] == "restart.gave_up"
    assert not omlx_supervisor.is_restarting("seat")


async def test_failed_upgrade_stop_emits_terminal_failure(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    events = []
    monkeypatch.setattr(omlx_supervisor, "_stop_server", AsyncMock(return_value={"ok": False}))
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    assert await omlx_upgrade.upgrade_managed_omlx() == {"restarted": False, "reason": "stop_failed"}
    assert events[-1] == ("restart.failed", {"status": "upgrade", "reason": "stop_failed"})
    assert omlx_supervisor._life["seat"].stopping is False
