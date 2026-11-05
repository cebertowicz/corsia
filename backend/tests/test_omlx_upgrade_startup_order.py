"""The startup upgrade owns local admission while recovered work resumes."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import app_lifecycle
from automation import restart_startup
from llm import base, lifecycle, network_discovery
from llm.providers import omlx_restart, omlx_supervisor, omlx_upgrade
from test_app_lifecycle_shutdown import _isolate_lifespan
from test_omlx_upgrade import _seat

_HTTPX_CLIENT = httpx.AsyncClient


async def _finish(*tasks):
    owned = [task for task in tasks if task is not None]
    for task in owned:
        if not task.done():
            task.cancel()
    await asyncio.gather(*owned, return_exceptions=True)


@pytest.fixture(autouse=True)
def forbid_external_process_and_network(monkeypatch):
    """Every test must explicitly fake each external process or network boundary."""
    from infra.state import ws
    from llm.providers import omlx_process

    monkeypatch.setattr(ws, "notify_models_changed", lambda **_kwargs: None)
    monkeypatch.setattr(ws, "broadcast_all", lambda *_args, **_kwargs: None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected external process or network access")

    class SafeOS:
        kill = staticmethod(forbidden)

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(omlx_supervisor, "os", SafeOS())
    monkeypatch.setattr(omlx_process, "os", SafeOS())
    for name in ("_spawn_process", "_stop_server", "_process_exited", "_process_alive",
                 "_port_owner_pid", "_read_pid", "_write_pid", "_clear_pid", "_listen_host"):
        monkeypatch.setattr(omlx_supervisor, name, forbidden)
    for name in ("watch_exit", "process_alive", "process_exited", "port_owner_pid", "reap_status"):
        monkeypatch.setattr(omlx_process, name, forbidden)
    monkeypatch.setattr(omlx_restart, "restart_logged_from_now", lambda *_args: lambda: False)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)


async def test_lifespan_holds_new_and_recovered_calls_until_models_are_restored(isolated_db, monkeypatch):
    # Bind the drain module before the lifespan fixture substitutes its process owner.
    import infra.drain  # noqa: F401

    _isolate_lifespan(monkeypatch)
    monkeypatch.setattr("host_access.browser_orphan.close_dead_sessions", lambda *_args: None)
    monkeypatch.setattr("tools.agent_shell_tools.start_prefetch", lambda: None)
    monkeypatch.setattr("connections.account_leftovers.remove_startup_leftovers", AsyncMock())
    monkeypatch.setattr("tools.mcp_runtime.start_connected_app_runtime", AsyncMock())
    monkeypatch.setattr(app_lifecycle, "_start_runtime_services", AsyncMock())
    monkeypatch.setattr(app_lifecycle, "_startup_cloud_discovery", AsyncMock())
    monkeypatch.setattr(app_lifecycle, "_startup_harness_discovery", AsyncMock(return_value=[]))
    monkeypatch.setattr(app_lifecycle, "_start_otlp_listener", AsyncMock())
    monkeypatch.setattr(app_lifecycle, "resume_local_setup", AsyncMock())
    monkeypatch.setattr("core.devices_runtime.runtime", SimpleNamespace(restore=AsyncMock(), disable=AsyncMock()))
    monkeypatch.setattr("doceditor.live_runtime.start_documents", AsyncMock())
    monkeypatch.setattr("events.connectors.github_reviewer_gate_recovery.recover_pending_reviews", AsyncMock())
    monkeypatch.setattr("api.usage_rollup.schedule_refresh", lambda: None)
    _seat(monkeypatch, recorded="old")
    restoring, release = asyncio.Event(), asyncio.Event()
    discovery_release = asyncio.Event()
    arrivals = [asyncio.Event() for _ in range(6)]
    admitted = [asyncio.Event() for _ in range(6)]
    recovered = SimpleNamespace(
        batches=tuple(SimpleNamespace(thread_id=str(i)) for i in range(3)), settlements=(), ended_turns=(),
    )

    async def restore(*_args):
        restoring.set()
        await release.wait()

    async def wire(**kwargs):
        if kwargs["model"] != "cloud":
            admitted[int(kwargs["model"])].set()
        return object()

    monkeypatch.setattr(base, "_omlx_loaded_only_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(base, "acompletion", wire)
    monkeypatch.setattr(base, "parse_response", lambda *_args, **_kwargs: "answered")

    async def enter(index):
        arrivals[index].set()
        return await completion(str(index), "seat")

    async def completion(model, server_id):
        return await base._completion_with_retries(
            {"model": model}, model=model, use_streaming=False,
            on_reasoning_chunk=None, on_content_chunk=None, on_tool_call_chunk=None,
            on_response_started=None, retry_transient_errors=False, omlx_server_id=server_id,
        )

    async def admit(item):
        await enter(int(item.thread_id))

    async def reconcile(_logger):
        return recovered, (), None

    monkeypatch.setattr(network_discovery, "get_servers", lambda **_kwargs: [{"id": "seat", "provider": "omlx"}])
    monkeypatch.setattr("llm.local_sources.corsia_omlx_server", lambda _servers: {"id": "seat"})
    monkeypatch.setattr(omlx_supervisor, "reconcile", lambda _cfg: {"adopted": True, "pid": 123})
    monkeypatch.setattr("llm.providers.omlx_process.watch_exit", lambda *_args: lambda: None)
    monkeypatch.setattr(network_discovery, "_sweep_all_servers", discovery_release.wait)
    monkeypatch.setattr(app_lifecycle, "_discover_network_servers", AsyncMock(return_value=1))
    monkeypatch.setattr(lifecycle, "autostart_local_providers", AsyncMock())
    monkeypatch.setattr(restart_startup, "run_startup_reconcile", reconcile)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", AsyncMock())
    monkeypatch.setattr("infra.turn_admission_driver.drive_turn_admission", admit)
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", restore)
    requests = []
    try:
        async with app_lifecycle.lifespan(FastAPI()):
            requests = [asyncio.create_task(enter(i)) for i in range(3, 6)]
            await asyncio.wait_for(restoring.wait(), 5)
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in arrivals)), 5)
            assert not any(event.is_set() for event in admitted)
            assert await asyncio.wait_for(completion("cloud", None), 2) == "answered"
            release.set()
            await asyncio.wait_for(asyncio.gather(*requests, *(event.wait() for event in admitted)), 5)
    finally:
        release.set()
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)


async def test_never_ready_upgrade_fails_held_call_in_finite_time(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    booting = asyncio.Event()

    async def start(_cfg):
        booting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(omlx_supervisor, "_start_recovery", start)
    monkeypatch.setattr(omlx_supervisor, "MANAGED_HEALTH_STARTUP_CAP_S", 0.05)
    upgrade = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    assert upgrade is not None
    waiter = None
    try:
        await asyncio.wait_for(booting.wait(), 2)

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                pytest.fail("a call entered after the boot timed out")

        waiter = asyncio.create_task(request())
        with pytest.raises(RuntimeError, match="Couldn’t upgrade the local model server"):
            await asyncio.wait_for(waiter, 2)
        assert upgrade.done()
    finally:
        await _finish(waiter, upgrade)


async def test_preload_probe_waits_for_health_not_model_list(monkeypatch):
    seen = []
    client_type = _HTTPX_CLIENT

    def respond(request):
        seen.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(503 if len(seen) == 1 else 200)
        return httpx.Response(200)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(omlx_supervisor.httpx, "AsyncClient", lambda **_kwargs: client_type(transport=transport))
    monkeypatch.setattr(omlx_supervisor, "HEALTH_POLL_INTERVAL_S", 0)
    assert await omlx_supervisor._wait_for_healthy("127.0.0.1", 1, timeout_s=1, require_preload=True) == "healthy"
    assert seen == ["/health", "/health"]


async def test_replacement_does_not_adopt_a_failed_boot_as_ready(monkeypatch):
    from llm.providers.omlx_restart import Life

    life = Life(hold_requests=True)
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": life})
    monkeypatch.setattr(omlx_supervisor, "run_in_db", AsyncMock(return_value=("127.0.0.1", 9)))
    monkeypatch.setattr(omlx_supervisor, "_read_pid", lambda *_args, **_kwargs: 123)
    monkeypatch.setattr(omlx_supervisor, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(omlx_supervisor, "_process_exited", lambda _pid: False)
    monkeypatch.setattr(omlx_supervisor, "_port_owner_pid", lambda *_args: 123)
    monkeypatch.setattr(omlx_supervisor, "_listen_host", lambda host: host)
    monkeypatch.setattr(omlx_supervisor, "watch", lambda *_args: None)
    health = AsyncMock(return_value="timed_out")
    monkeypatch.setattr(omlx_supervisor, "_wait_for_healthy", health)
    monkeypatch.setattr(omlx_supervisor, "_stop_server", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(omlx_supervisor, "_failed_boot_result", lambda *_args, **_kwargs: {"ok": False})
    task = asyncio.create_task(omlx_supervisor._start_recovery({"server_id": "seat"}))
    life.restart = task
    try:
        result = await task
        assert result["ok"] is False
        assert health.await_args.kwargs["require_preload"] is True
    finally:
        await _finish(task)


async def test_never_healthy_retry_does_not_admit_held_call(isolated_db, monkeypatch):
    actual_start = omlx_supervisor._start_recovery
    _seat(monkeypatch, recorded="old")
    monkeypatch.setattr(omlx_supervisor, "_start_recovery", actual_start)
    monkeypatch.setattr(omlx_supervisor, "run_in_db", AsyncMock(return_value=("127.0.0.1", 9)))
    monkeypatch.setattr(omlx_supervisor, "_listen_host", lambda host: host)
    monkeypatch.setattr(omlx_supervisor, "_read_pid", lambda *_args, **_kwargs: 123)
    monkeypatch.setattr(omlx_supervisor, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(omlx_supervisor, "_process_exited", lambda _pid: False)
    monkeypatch.setattr(omlx_supervisor, "_port_owner_pid", lambda *_args: 123)
    monkeypatch.setattr(omlx_supervisor, "watch", lambda *_args: None)
    monkeypatch.setattr(omlx_supervisor, "_failed_boot_result", lambda *_args, **_kwargs: {"ok": False})
    monkeypatch.setattr(omlx_supervisor, "MANAGED_HEALTH_STARTUP_CAP_S", 0.03)
    monkeypatch.setattr(omlx_supervisor, "HEALTH_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr("llm.providers.omlx_public_auth.public_headers", AsyncMock(return_value={}))
    seen = []
    client_type = _HTTPX_CLIENT

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(503 if request.url.path == "/health" else 200)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(omlx_supervisor.httpx, "AsyncClient", lambda **_kwargs: client_type(transport=transport))
    upgrade = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    assert upgrade is not None

    async def request():
        async with omlx_supervisor.request_slot("seat"):
            pytest.fail("a call passed an unhealthy replacement")

    waiter = asyncio.create_task(request())
    try:
        with pytest.raises(RuntimeError, match="Check the server in Settings. Restart CORSIA to retry the upgrade"):
            await asyncio.wait_for(waiter, 3)
        assert (await upgrade)["reason"] == "start_failed"
        assert seen and set(seen) == {"/health"}
    finally:
        await _finish(waiter, upgrade)


async def test_already_current_recheck_releases_held_call(monkeypatch):
    life = omlx_restart.Life()
    release = asyncio.Event()

    async def no_change():
        await release.wait()
        return {"restarted": False, "reason": "already_current"}

    task = omlx_restart.schedule("seat", life, no_change(), hold_requests=True)

    async def request():
        async with omlx_restart.request_slot({"seat": life}, "seat"):
            return "admitted"

    waiter = asyncio.create_task(request())
    try:
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()
        assert await asyncio.wait_for(waiter, 1) == "admitted"
        assert (await task)["reason"] == "already_current"
    finally:
        release.set()
        await _finish(waiter, task)


async def test_scope_drain_is_bounded_and_fails_the_held_call(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    life = omlx_supervisor._life["seat"]
    events = []
    monkeypatch.setattr(omlx_restart, "emit", lambda _sid, action, **props: events.append((action, props)))
    outstanding = asyncio.get_running_loop().create_future()
    life.requests.add(outstanding)
    monkeypatch.setattr(omlx_supervisor, "MANAGED_HEALTH_STARTUP_CAP_S", 0.02)
    task = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    assert task is not None
    try:
        with pytest.raises(RuntimeError, match="active requests did not finish in time. Wait for them to finish. Restart CORSIA to retry the upgrade"):
            async with omlx_supervisor.request_slot("seat"):
                pytest.fail("the call passed a timed-out drain")
        assert (await task)["reason"] == "drain_timeout"
        assert ("restart.failed", {"status": "upgrade", "reason": "drain_timeout"}) in events
        assert calls == []
    finally:
        life.requests.remove(outstanding)
        outstanding.set_result(None)
        await _finish(task)


async def test_stop_cancels_upgrade_and_releases_held_call(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    checking = asyncio.Event()

    async def state(_sid, *, reads):
        checking.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", state)
    task = omlx_supervisor.schedule_startup_upgrade("seat", "old")
    assert task is not None
    waiter = None
    try:
        await asyncio.wait_for(checking.wait(), 2)

        async def request():
            async with omlx_supervisor.request_slot("seat"):
                pytest.fail("the call passed a cancelled upgrade")

        waiter = asyncio.create_task(request())
        await asyncio.sleep(0)
        omlx_supervisor._life["seat"].announce_stop()
        with pytest.raises(RuntimeError, match="upgrade was cancelled"):
            await asyncio.wait_for(waiter, 2)
        assert task.cancelled()
    finally:
        await _finish(waiter, task)


async def test_autostart_waits_for_existing_upgrade_before_using_process(monkeypatch):
    life = omlx_restart.Life()
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": life})
    release, checked = asyncio.Event(), asyncio.Event()

    async def replace():
        await release.wait()
        return {"restarted": True}

    async def in_db(fn, *_args):
        checked.set()
        if fn is omlx_supervisor._resolve_endpoint:
            return ("127.0.0.1", 9)
        return None

    monkeypatch.setattr(omlx_supervisor, "run_in_db", in_db)
    monkeypatch.setattr(omlx_supervisor, "_listen_host", lambda host: host)
    monkeypatch.setattr(omlx_supervisor, "_read_pid", lambda *_args, **_kwargs: 123)
    monkeypatch.setattr(omlx_supervisor, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(omlx_supervisor, "_process_exited", lambda _pid: False)
    monkeypatch.setattr(omlx_supervisor, "_port_owner_pid", lambda *_args: 123)
    monkeypatch.setattr(omlx_supervisor, "watch", lambda *_args: None)
    task = omlx_restart.schedule("seat", life, replace(), hold_requests=True)
    auto = asyncio.create_task(omlx_supervisor.start_server({"server_id": "seat"}))
    try:
        await asyncio.sleep(0)
        assert not checked.is_set()
        release.set()
        assert (await asyncio.wait_for(auto, 2))["already_running"] is True
        assert (await task)["restarted"] is True
    finally:
        release.set()
        await _finish(auto, task)

