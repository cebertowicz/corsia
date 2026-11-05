"""A watched exit during an upgrade remains owned and keeps admission closed."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from llm import server_observation
from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor
from test_omlx_upgrade import _seat
from test_omlx_upgrade_startup_order import _finish, forbid_external_process_and_network  # noqa: F401


@pytest.mark.parametrize("window", ["snapshot", "restoration"])
@pytest.mark.parametrize("clean", [False, True])
async def test_exit_during_upgrade_recovers_before_held_call(isolated_db, monkeypatch, window, clean):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    entered, release = asyncio.Event(), asyncio.Event()
    recovering, finish_recovery = asyncio.Event(), asyncio.Event()
    reads = 0

    async def loaded(_cfg):
        nonlocal reads
        reads += 1
        if window == "snapshot" and reads == 1:
            entered.set()
            await release.wait()
        return [{"identifier": "exact-model"}]

    async def reload(_sid, models):
        assert models == ["exact-model"]
        if window == "restoration" and not entered.is_set():
            entered.set()
            await release.wait()
        else:
            recovering.set()
            await finish_recovery.wait()

    async def start(_cfg):
        return {"ok": True}

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", loaded)
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload)
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: [])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    upgrade = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 2)

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                return "admitted"

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        omlx_restart.on_exit("seat", life, 123, start, lambda _sid: "", ("exit", clean))
        assert life.restart is upgrade
        release.set()
        await asyncio.wait_for(recovering.wait(), 2)
        assert not waiter.done()
        finish_recovery.set()
        assert await asyncio.wait_for(waiter, 2) == "admitted"
    finally:
        release.set()
        finish_recovery.set()
        await _finish(waiter, upgrade, life.restart)


async def test_stop_cancels_exit_recovery_and_releases_waiter(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    checking, recovering, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def state(_sid, *, reads):
        checking.set()
        await release.wait()
        return "unreadable"

    async def start(_cfg):
        recovering.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(omlx_supervisor, "start_server", start)
    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", state)
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: ["exact-model"])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    upgrade = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    waiter = None
    try:
        await asyncio.wait_for(checking.wait(), 2)

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                pytest.fail("stopped recovery admitted a call")

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        omlx_restart.on_exit("seat", life, 123, start, lambda _sid: "", ("crash", False))
        release.set()
        await asyncio.wait_for(recovering.wait(), 2)
        successor = life.restart
        life.announce_stop()
        with pytest.raises(RuntimeError, match="cancelled"):
            await asyncio.wait_for(waiter, 2)
        assert successor is not None and successor.cancelled()
    finally:
        await _finish(waiter, upgrade, life.restart)


async def test_failed_exit_recovery_reports_error_to_held_call(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    monkeypatch.setattr(omlx_supervisor, "start_server", AsyncMock(return_value={"ok": False}))
    life = omlx_supervisor._life["seat"]
    checking, release = asyncio.Event(), asyncio.Event()

    async def state(_sid, *, reads):
        checking.set()
        await release.wait()
        return "unreadable"

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", state)
    monkeypatch.setattr(server_observation, "forget_loaded_on_exit", lambda _sid: ["exact-model"])
    monkeypatch.setattr(omlx_restart, "emit", lambda *_args, **_kwargs: None)
    upgrade = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    waiter = None
    try:
        await asyncio.wait_for(checking.wait(), 2)

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                pytest.fail("a call passed failed recovery")

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        omlx_restart.on_exit("seat", life, 123, AsyncMock(return_value={"ok": False}), lambda _sid: "", ("crash", False))
        release.set()
        with pytest.raises(RuntimeError, match="start_failed"):
            await asyncio.wait_for(waiter, 2)
    finally:
        release.set()
        await _finish(waiter, upgrade, life.restart)


async def test_cancelled_readiness_reports_cleanup_failure(monkeypatch):
    started = asyncio.Event()

    async def never_ready(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(omlx_supervisor, "_wait_for_healthy", never_ready)
    monkeypatch.setattr(omlx_supervisor, "_stop_server", AsyncMock(return_value={"ok": False}))
    task = asyncio.create_task(omlx_supervisor._confirm_ready(
        "seat", "127.0.0.1", 9, 123, timeout_s=1, require_preload=True,
    ))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(RuntimeError, match="cleanup"):
        await task


async def test_cancelled_readiness_cleans_up_the_spawned_process(monkeypatch):
    life = omlx_restart.Life(hold_requests=True)
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": life})
    started = asyncio.Event()

    async def never_ready(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    stop = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(omlx_supervisor, "_wait_for_healthy", never_ready)
    monkeypatch.setattr(omlx_supervisor, "_stop_server", stop)
    task = asyncio.create_task(omlx_supervisor._confirm_ready(
        "seat", "127.0.0.1", 9, 123, timeout_s=1, require_preload=True,
    ))
    life.restart = task
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stop.assert_awaited_once_with({"server_id": "seat", "host": "127.0.0.1", "port": 9})
    finally:
        await _finish(task)


async def test_repeated_cancel_waits_for_readiness_cleanup(monkeypatch):
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def never_ready(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def stop(_cfg):
        cleaning.set()
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr(omlx_supervisor, "_wait_for_healthy", never_ready)
    monkeypatch.setattr(omlx_supervisor, "_stop_server", stop)
    task = asyncio.create_task(omlx_supervisor._confirm_ready(
        "seat", "127.0.0.1", 9, 123, timeout_s=1, require_preload=True,
    ))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        release.set()
        await _finish(task)


async def test_timed_out_readiness_joins_process_cleanup(monkeypatch):
    stop = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(omlx_supervisor, "_stop_server", stop)
    monkeypatch.setattr(omlx_supervisor, "_wait_for_healthy", AsyncMock(return_value="timed_out"))
    monkeypatch.setattr(omlx_supervisor, "_failed_boot_result", lambda *_args, **_kwargs: {"ok": False})
    result = await omlx_supervisor._confirm_ready("seat", "127.0.0.1", 9, 123, timeout_s=1, require_preload=True)
    assert result == {"ok": False}
    stop.assert_awaited_once_with({"server_id": "seat", "host": "127.0.0.1", "port": 9})
