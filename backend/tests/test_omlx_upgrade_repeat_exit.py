"""Repeated watched exits stay with the original held upgrade."""

import asyncio
from unittest.mock import AsyncMock

from llm import server_observation
from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat
from test_omlx_upgrade_startup_order import _finish, forbid_external_process_and_network  # noqa: F401


async def test_crash_during_crash_recovery_keeps_original_owner_and_models(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    entered, release = asyncio.Event(), asyncio.Event()
    restored = []
    observed = iter((["exact-model"], []))

    async def reload(_sid, models):
        restored.append(list(models))
        if len(restored) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: next(observed))
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", AsyncMock(return_value=[{"identifier": "exact-model"}]))
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload)
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
    owner = life.restart
    try:
        await asyncio.wait_for(entered.wait(), 2)
        omlx_restart.on_exit("seat", life, 124, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
        assert life.restart is owner
        release.set()
        await asyncio.wait_for(owner, 2)
        assert restored == [["exact-model"], ["exact-model"]]
    finally:
        release.set()
        await _finish(owner, life.restart)


async def test_exit_after_recovery_finishes_before_done_callback_starts_new_owner(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    entered, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    observed = loop.create_future()
    reads = 0

    async def read(_cfg):
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            await release.wait()
        else:
            await finish.wait()
        return [{"identifier": "exact-model"}]

    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: ["exact-model"])
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", read)
    omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
    owner = life.restart

    def next_exit():
        finished = owner.done()
        omlx_restart.on_exit("seat", life, 124, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
        observed.set_result((finished, life.restart))

    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()  # Queue completion before the exit, whose callback precedes _done.
        loop.call_soon(next_exit)
        finished, successor = await asyncio.wait_for(observed, 2)
        assert finished and not owner.cancelled()
        assert successor is not owner
        await asyncio.sleep(0)
        assert life.restart is successor  # The old owner's done callback must leave it intact.
        finish.set()
        assert (await asyncio.wait_for(successor, 2))["loaded"] == ["exact-model"]
        assert sum(call[0] == "start" for call in calls) == 2
    finally:
        release.set()
        finish.set()
        await _finish(owner, life.restart)


async def test_crash_readback_shortfall_is_reported_without_completion(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    events = []
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: ["exact-model"])
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", AsyncMock(return_value=[]))
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))
    omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
    owner = life.restart
    try:
        assert await owner == {"restarted": True, "reason": "reload_failed", "missing": ["exact-model"]}
        assert not any(action == "restart.completed" for action, _ in events)
    finally:
        await _finish(owner)


async def test_exit_during_failed_snapshot_keeps_preexisting_models(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    reads = 0

    async def read(_cfg):
        nonlocal reads
        reads += 1
        if reads == 1:
            omlx_restart.on_exit("seat", life, 123, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
            raise OSError("snapshot interrupted")
        return [{"identifier": "exact-model"}]

    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _sid: {"exact-model"})
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", read)
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    try:
        assert (await asyncio.wait_for(owner, 2)) == {"restarted": True, "reason": "loaded_unknown"}
        assert ("reload", "seat", ["exact-model"]) in calls
        assert not any(call[0] == "stop" for call in calls)
    finally:
        await _finish(owner)


async def test_repeated_exit_restores_original_snapshot_before_admission(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    entered = asyncio.Queue()
    releases = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    calls = 0

    async def reload(_sid, models):
        nonlocal calls
        assert models == ["exact-model"]
        index = calls
        calls += 1
        await entered.put(index)
        await releases[index].wait()

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", AsyncMock(return_value=[{"identifier": "exact-model"}]))
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload)
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    owner = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    waiter = None
    try:
        assert await asyncio.wait_for(entered.get(), 2) == 0

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                return "admitted"

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        for index in (0, 1):
            omlx_restart.on_exit("seat", life, 123 + index, omlx_supervisor._start_recovery, lambda _sid: "", ("exit", False))
            assert life.restart is owner and not waiter.done()
            releases[index].set()
            assert await asyncio.wait_for(entered.get(), 2) == index + 1
        assert not waiter.done()
        releases[2].set()
        assert await asyncio.wait_for(waiter, 2) == "admitted"
    finally:
        for release in releases:
            release.set()
        await _finish(waiter, owner)
