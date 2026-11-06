"""An app update must not undo Stop or restart without knowing which models to restore."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from infra import carry_in_capacity

from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat, _upgrade


async def test_stop_during_idle_proof_cancels_replacement(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    life = omlx_restart.Life()
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": life})
    checking = asyncio.Event()

    async def state(_server, *, reads):
        checking.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(carry_in_capacity, "omlx_state", state)
    upgrade = asyncio.create_task(_upgrade())
    await asyncio.wait_for(checking.wait(), 2)
    life.announce_stop()
    with pytest.raises(asyncio.CancelledError):
        await upgrade
    assert calls == []


async def test_stop_during_internal_stop_keeps_stop_intent(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    stopping = asyncio.Event()

    async def held_stop(_cfg):
        stopping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(omlx_supervisor, "_stop_server", held_stop)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    await asyncio.wait_for(stopping.wait(), 2)
    assert life.announce_stop() is owner
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert life.stopping
    assert not any(call[0] == "start" for call in calls)


async def test_an_unadopted_server_is_not_restarted(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    monkeypatch.setattr(omlx_supervisor, "_life", {})

    result = await _upgrade()

    assert result["restarted"] is False
    assert calls == []


async def test_unreadable_loaded_models_prevents_stop(isolated_db, monkeypatch):
    original_read = omlx_lifecycle.read_loaded
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": omlx_restart.Life()})
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", original_read)
    monkeypatch.setattr(omlx_lifecycle, "_loaded_snapshot", AsyncMock(return_value=([], False)))

    result = await _upgrade()

    assert result["restarted"] is False
    assert calls == []


@pytest.mark.parametrize("counts", [(1, 0), (0, 1)])
async def test_external_requests_prevent_restart_without_waiting(isolated_db, monkeypatch, counts):
    real_state = carry_in_capacity.omlx_state
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    observed = []

    async def probe(_client, server_id, host, port):
        assert (server_id, host, port) == ("seat", "127.0.0.1", 9)
        assert calls == []
        observed.append(counts)
        return None, None, {"active_requests": counts[0], "waiting_requests": counts[1], "total_requests": 8}

    monkeypatch.setattr(carry_in_capacity, "omlx_state", real_state)
    monkeypatch.setattr(carry_in_capacity, "_status_address", lambda _sid: ("127.0.0.1", 9))
    monkeypatch.setattr("llm.providers.omlx_public_probe.probe_registered_status", probe)
    assert (await _upgrade())["reason"] == "busy"
    assert observed == [counts]
    assert calls == []


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

    monkeypatch.setattr(omlx_supervisor, "_start_recovery", start)
    upgrade = asyncio.create_task(_upgrade())
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

    result = await _upgrade()

    assert result == {"restarted": False, "reason": "stop_failed"}
    assert [call[0] for call in calls] == ["loaded"]
    assert omlx_supervisor._life["seat"].stopping is False
