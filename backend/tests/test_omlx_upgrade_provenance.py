"""Pre-snapshot observations are recovery hints, not complete targets."""

import asyncio

import httpx
import pytest

from llm import network_discovery, server_observation
from llm.providers import omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat
from test_omlx_upgrade_startup_order import _finish


@pytest.mark.parametrize("stage", ["first", "idle", "incremental"])
async def test_unknown_original_state_fails_held_call(isolated_db, monkeypatch, stage):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    network_discovery._LOADED_BY_SERVER.pop("seat", None)
    entered, release = asyncio.Event(), asyncio.Event()
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    async def idle(_sid, *, reads):
        entered.set()
        await release.wait()
        return "idle"

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", idle)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    waiter = None
    try:
        if stage != "first":
            await asyncio.wait_for(entered.wait(), 2)
        if stage == "incremental":
            network_discovery.notify_cached_model_loaded("seat", "partial", server_observation.observation_generation("seat"))

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                return "admitted"

        waiter = asyncio.create_task(request())
        if stage != "first":
            await asyncio.sleep(0)
        omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _: "", ("crash", False))
        assert life.restart is owner
        release.set()
        result = await asyncio.wait_for(owner, 2)
        assert result["reason"] == "loaded_unknown"
        with pytest.raises(omlx_restart.UpgradeAdmissionError, match="determine which models were loaded"):
            await asyncio.wait_for(waiter, 2)
        assert not any(action == "restart.completed" for action, _ in events)
        if stage == "incremental":
            assert ("reload", "seat", ["partial"]) in calls
    finally:
        release.set()
        await _finish(waiter, owner)


async def test_sweep_observation_reaches_reload_but_does_not_prove_completeness(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    network_discovery._LOADED_BY_SERVER.pop("seat", None)
    entered, release = asyncio.Event(), asyncio.Event()

    async def idle(_sid, *, reads):
        entered.set()
        await release.wait()
        return "idle"

    async def db(fn, *args, **kwargs):
        return [{"host": "test"}] if fn is network_discovery.get_endpoints else None

    async def headers(*args, **kwargs):
        return {}

    async def settle(*args):
        pass

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", idle)
    monkeypatch.setattr(network_discovery, "run_in_db", db)
    monkeypatch.setattr(server_observation, "run_in_db", db)
    monkeypatch.setattr("llm.providers.omlx_public_probe.public_headers", headers)
    monkeypatch.setattr("llm.local_setup.settle_load_failure", settle)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok", "loaded_models": ["later"]}))
        async with httpx.AsyncClient(transport=transport) as client:
            await network_discovery._sweep_managed(client, {"id": "seat", "port": 1234, "status": "online"})
        assert network_discovery.get_loaded_model_ids("seat") == {"later"}
        omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _: "", ("crash", False))
        assert not network_discovery.get_loaded_model_ids("seat")
        release.set()
        assert await asyncio.wait_for(owner, 2) == {"restarted": False, "reason": "loaded_unknown"}
        assert ("reload", "seat", ["later"]) in calls
    finally:
        release.set()
        await _finish(owner)


async def test_first_exit_hints_survive_later_replacement_exit(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    network_discovery._LOADED_BY_SERVER.pop("seat", None)
    started, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def start(_config):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await release.wait()
        return {"ok": True}

    monkeypatch.setattr(omlx_supervisor, "_start_recovery", start)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    generation = server_observation.observation_generation("seat")
    for model in ("first", "second"):
        network_discovery.notify_cached_model_loaded("seat", model, generation)
    omlx_restart.on_exit("seat", life, 123, start, lambda _: "", ("original", False))
    try:
        await asyncio.wait_for(started.wait(), 2)
        generation = server_observation.observation_generation("seat")
        network_discovery.notify_cached_model_loaded("seat", "first", generation)
        omlx_restart.on_exit("seat", life, 124, start, lambda _: "", ("replacement", False))
        assert life.restart is owner
        release.set()
        assert await asyncio.wait_for(owner, 2) == {"restarted": False, "reason": "loaded_unknown"}
        assert ("reload", "seat", ["first", "second"]) in calls
        assert attempts == 2 and len(life.restarts) == 2
    finally:
        release.set()
        await _finish(owner)


async def test_complete_empty_snapshot_overrides_hint_and_survives_exit(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    monkeypatch.setattr(network_discovery, "get_loaded_model_ids", lambda _sid: {"stale"})
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))
    reads = 0

    async def read(_config):
        nonlocal reads
        reads += 1
        if reads == 1:
            omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _: "", ("crash", False))
        return []

    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", read)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")

    async def request():
        async with omlx_supervisor.request_slot("seat"):
            return "admitted"

    waiter = asyncio.create_task(request())
    try:
        assert await asyncio.wait_for(owner, 2) == {"restarted": True, "loaded": []}
        assert await asyncio.wait_for(waiter, 2) == "admitted"
        assert ("reload", "seat", []) in calls
        assert any(action == "restart.completed" for action, _ in events)
    finally:
        await _finish(waiter, owner)


@pytest.mark.parametrize("stage", ["idle", "start"])
async def test_lifecycle_timeout_has_specific_held_call_message(isolated_db, monkeypatch, stage):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    monkeypatch.setattr(omlx_supervisor, "MANAGED_HEALTH_STARTUP_CAP_S", 0.01)
    entered = asyncio.Event()

    async def never(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if stage == "idle":
        monkeypatch.setattr("infra.carry_in_capacity.omlx_state", never)
    else:
        monkeypatch.setattr(omlx_supervisor, "_start_recovery", never)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async def request():
            async with omlx_supervisor.request_slot("seat"):
                pytest.fail("timed out upgrade admitted a call")
        waiter = asyncio.create_task(request())
        with pytest.raises(omlx_restart.UpgradeAdmissionError, match="upgrade timed out"):
            await asyncio.wait_for(waiter, 2)
        with pytest.raises(TimeoutError):
            await owner
    finally:
        await _finish(waiter, owner)


async def test_timeout_has_specific_held_call_message():
    async def operation():
        raise TimeoutError()
    task = asyncio.create_task(operation())
    life = omlx_restart.Life(restart=task, hold_requests=True)
    with pytest.raises(omlx_restart.UpgradeAdmissionError, match="timed out"):
        async with omlx_restart.request_slot({"seat": life}, "seat"):
            pass


async def test_unrelated_upgrade_exception_keeps_generic_held_call_message():
    async def operation():
        raise ValueError("internal detail")

    task = asyncio.create_task(operation())
    life = omlx_restart.Life(restart=task, hold_requests=True)
    with pytest.raises(omlx_restart.UpgradeAdmissionError) as error:
        async with omlx_restart.request_slot({"seat": life}, "seat"):
            pytest.fail("failed upgrade admitted a call")
    assert str(error.value) == (
        "Couldn’t upgrade the local model server. Check the server in Settings. "
        "Restart CORSIA to retry the upgrade."
    )


async def test_exhausted_start_budget_takes_precedence_over_unknown_target(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    network_discovery._LOADED_BY_SERVER.pop("seat", None)
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))

    async def failed_start(_payload):
        return {"ok": False}

    monkeypatch.setattr(omlx_supervisor, "_start_recovery", failed_start)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    omlx_restart.on_exit("seat", life, 123, failed_start, lambda _: "", ("crash", False))

    async def request():
        async with omlx_supervisor.request_slot("seat"):
            pytest.fail("unknown target admitted a call")

    waiter = asyncio.create_task(request())
    try:
        assert (await asyncio.wait_for(owner, 2))["reason"] == "start_failed"
        with pytest.raises(omlx_restart.UpgradeAdmissionError, match="Check the server in Settings"):
            await asyncio.wait_for(waiter, 2)
        assert len(life.restarts) == omlx_restart.MAX_RESTARTS
        assert sum(action == "restart.attempted" for action, _ in events) == omlx_restart.MAX_RESTARTS
        assert not any(action == "restart.completed" for action, _ in events)
    finally:
        await _finish(waiter, owner)
