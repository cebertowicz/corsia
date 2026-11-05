"""An app update must not undo Stop or restart without knowing which models to restore."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from infra import carry_in_capacity

from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor, omlx_upgrade
from test_omlx_upgrade import _seat


async def test_stop_during_idle_wait_wins(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    life = omlx_restart.Life()
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": life})

    async def wait(read, **_kwargs):
        # The real lifecycle owner records a person's Stop before any signal.
        life.announce_stop()
        await read()
        return True

    monkeypatch.setattr("infra.carry_in_capacity.wait_until_quiet", wait)

    result = await omlx_upgrade.upgrade_managed_omlx()

    assert result["restarted"] is False
    assert calls == []


async def test_an_unadopted_server_is_not_restarted(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    monkeypatch.setattr(omlx_supervisor, "_life", {})

    result = await omlx_upgrade.upgrade_managed_omlx()

    assert result["restarted"] is False
    assert calls == []


async def test_unreadable_loaded_models_prevents_stop(isolated_db, monkeypatch):
    original_read = omlx_lifecycle.read_loaded
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": omlx_restart.Life()})
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", original_read)
    monkeypatch.setattr(omlx_lifecycle, "_loaded_snapshot", AsyncMock(return_value=([], False)))

    result = await omlx_upgrade.upgrade_managed_omlx()

    assert result["restarted"] is False
    assert calls == []


async def test_active_and_waiting_requests_clear_before_restart(isolated_db, monkeypatch):
    real_wait, real_state = carry_in_capacity.wait_until_quiet, carry_in_capacity.omlx_state
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    snapshots = iter([
        {"active_requests": 1, "waiting_requests": 0, "total_requests": 8},
        {"active_requests": 0, "waiting_requests": 1, "total_requests": 9},
        {"active_requests": 0, "waiting_requests": 0, "total_requests": 10},
        {"active_requests": 0, "waiting_requests": 0, "total_requests": 10},
    ])
    observed = []

    async def probe(_client, server_id, host, port):
        assert (server_id, host, port) == ("seat", "127.0.0.1", 9)
        assert calls == []  # no stop while either request count is nonzero
        payload = next(snapshots)
        observed.append(payload)
        return None, None, payload

    monkeypatch.setattr(carry_in_capacity, "wait_until_quiet", real_wait)
    monkeypatch.setattr(carry_in_capacity, "omlx_state", real_state)
    monkeypatch.setattr(carry_in_capacity, "_status_address", lambda _sid: ("127.0.0.1", 9))
    monkeypatch.setattr(carry_in_capacity.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("llm.providers.omlx_public_probe.probe_registered_status", probe)

    assert (await omlx_upgrade.upgrade_managed_omlx())["restarted"] is True
    assert len(observed) == 4
    assert [call[0] for call in calls] == ["loaded", "stop", "start", "reload", "loaded"]


async def test_stop_cancels_boot_before_model_reload(isolated_db, monkeypatch, tmp_path):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    booting, blocked = asyncio.Event(), asyncio.Event()
    record = tmp_path / "spawned"
    monkeypatch.setattr(omlx_supervisor, "_pid_path", lambda _sid: record)

    async def start(_cfg):
        record.write_text("test spawn")
        calls.append(("start", "seat"))
        assert omlx_supervisor.is_restarting("seat") is True
        booting.set()
        await blocked.wait()
        return {"ok": True}

    monkeypatch.setattr(omlx_supervisor, "start_server", start)
    upgrade = asyncio.create_task(omlx_upgrade.upgrade_managed_omlx())
    await asyncio.wait_for(booting.wait(), timeout=2)

    stopped = await omlx_supervisor.stop_server({"server_id": "seat"})

    with pytest.raises(asyncio.CancelledError):
        await upgrade
    assert stopped["ok"] is True
    assert [call[0] for call in calls] == ["loaded", "stop", "start", "stop"]
    assert omlx_supervisor.is_restarting("seat") is False


async def test_failed_stop_does_not_start_or_reload(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    monkeypatch.setattr(omlx_supervisor, "_stop_server", AsyncMock(return_value={"ok": False}))

    result = await omlx_upgrade.upgrade_managed_omlx()

    assert result == {"restarted": False, "reason": "stop_failed"}
    assert [call[0] for call in calls] == ["loaded"]
    assert omlx_supervisor._life["seat"].stopping is False
