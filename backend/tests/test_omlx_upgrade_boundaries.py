"""Production recovery keeps its snapshot and owner across preparation exits."""

import asyncio

import pytest

from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor, omlx_upgrade
from llm import server_observation
from test_omlx_upgrade import _seat
from test_omlx_upgrade_startup_order import _finish


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_exit_during_failed_recheck_preserves_complete_snapshot(isolated_db, monkeypatch, failure):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _sid: {"stale"})
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])
    monkeypatch.setattr(omlx_supervisor, "MANAGED_HEALTH_STARTUP_CAP_S", 0.03)
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", lambda _cfg: _rows())

    async def recheck(_sid):
        omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
        if failure == "error":
            raise OSError("recheck failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(omlx_upgrade, "recorded_version", recheck)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    try:
        result = await asyncio.wait_for(owner, 2)
        assert result == {"restarted": True, "loaded": ["exact"]}
        assert ("reload", "seat", ["exact"]) in calls
        assert not any(call[0] == "stop" for call in calls)
    finally:
        await _finish(owner)


async def _rows():
    return [{"identifier": "exact"}]


@pytest.mark.parametrize("boundary", ["version", "drain", "idle", "snapshot", "recheck"])
async def test_exit_at_preparation_boundary_skips_later_preparation(isolated_db, monkeypatch, boundary):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    steps = []
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _sid: {"exact"})

    def exited():
        omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))

    async def version(_sid):
        stage = "version" if not steps else "recheck"
        steps.append(stage)
        if boundary == stage:
            exited()
        if boundary == "drain" and stage == "version":
            asyncio.get_running_loop().call_soon(drained)
        return "old"

    async def idle(_sid, *, reads):
        steps.append("idle")
        if boundary == "idle":
            exited()
        return "idle"

    async def snapshot(_cfg):
        if life.exit_generation:
            return [{"identifier": "exact"}]
        steps.append("snapshot")
        if boundary == "snapshot":
            exited()
        return [{"identifier": "exact"}]

    def drained():
        steps.append("drain")
        exited()
        pending.set_result(None)

    monkeypatch.setattr(omlx_upgrade, "recorded_version", version)
    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", idle)
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", snapshot)
    if boundary == "drain":
        pending = asyncio.get_running_loop().create_future()
        life.requests.add(pending)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", None)
    try:
        result = await asyncio.wait_for(owner, 2)
        if boundary in {"version", "drain", "idle"}:
            assert result == {"restarted": True, "reason": "loaded_unknown"}
        else:
            assert result["restarted"]
        stages = ["version", "idle", "snapshot", "recheck"]
        expected = ["version", "drain"] if boundary == "drain" else stages[:stages.index(boundary) + 1]
        assert steps == expected
        assert not any(call[0] == "stop" for call in calls)
        assert ("reload", "seat", ["exact"]) in calls
    finally:
        await _finish(owner)


async def test_scheduling_uses_existing_exit_generation_as_baseline(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    life.exit_generation = 7
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    try:
        assert (await asyncio.wait_for(owner, 2))["restarted"]
        assert ("stop", "seat") in calls
    finally:
        await _finish(owner)
