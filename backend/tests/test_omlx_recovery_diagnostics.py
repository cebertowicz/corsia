"""An exit observed alongside a failed stage keeps the failure in diagnostics."""

import asyncio
import logging

import pytest

from llm import server_observation
from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat
from test_omlx_upgrade_startup_order import _finish


@pytest.mark.parametrize("stage", ["prepare", "start", "reload", "readback"])
async def test_exit_with_stage_exception_keeps_owner_models_and_diagnostics(isolated_db, monkeypatch, caplog, stage):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _sid: {"exact"})
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: ["exact"])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    failed = False
    owner = None

    def fail_once(current):
        nonlocal failed
        if stage == current and not failed:
            failed = True
            assert life.restart is owner
            omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
            assert life.restart is owner
            raise OSError(f"{stage} diagnostic")

    async def idle(_sid, *, reads):
        fail_once("prepare")
        return "idle"

    async def start(_cfg):
        fail_once("start")
        calls.append(("start", "seat"))
        return {"ok": True}

    async def reload(_sid, models):
        fail_once("reload")
        calls.append(("reload", "seat", list(models)))

    reads = 0

    async def read(_cfg):
        nonlocal reads
        reads += 1
        if reads > 1:
            fail_once("readback")
        return [{"identifier": "exact"}]

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", idle)
    monkeypatch.setattr(omlx_supervisor, "_start_recovery", start)
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload)
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", read)
    with caplog.at_level(logging.WARNING, logger=omlx_restart.log.name):
        owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
        try:
            result = await asyncio.wait_for(owner, 2)
            if stage == "prepare":
                assert result == {"restarted": True, "reason": "loaded_unknown"}
            else:
                assert result == {"restarted": True, "loaded": ["exact"]}
            assert ("reload", "seat", ["exact"]) in calls
            assert failed
            assert len(life.restarts) == (1 if stage == "prepare" else 2)
            assert any(record.exc_info and f"{stage} diagnostic" in str(record.exc_info[1]) for record in caplog.records)
        finally:
            await _finish(owner)


@pytest.mark.parametrize("stage", ["prepare", "start"])
async def test_stage_exception_without_exit_propagates(isolated_db, monkeypatch, stage):
    _seat(monkeypatch, recorded="old")

    async def fail(*_args, **_kwargs):
        raise OSError(f"{stage} failure")

    if stage == "prepare":
        monkeypatch.setattr("infra.carry_in_capacity.omlx_state", fail)
    else:
        monkeypatch.setattr(omlx_supervisor, "_start_recovery", fail)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    try:
        with pytest.raises(OSError, match=f"{stage} failure"):
            await asyncio.wait_for(owner, 2)
    finally:
        await _finish(owner)
