"""Manage CORSIA-owned oMLX server subprocesses: the pid records, and in ``_life`` one ``restart.Life`` each."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx

from infra.config import RUNTIME_DIR
from infra.db_async import run_in_db
from llm.network_endpoints import lan_base, models_probe_url
from llm.omlx_release import OMLX_VERSION
from llm.providers import omlx_process as process
from llm.providers import omlx_restart as restart
from llm.providers.omlx_endpoints import _resolve_endpoint as _resolve_endpoint_via_freshness
from llm.providers.omlx_supervisor_config import (
    listen_host as _listen_host,
    managed_launch_settings as _managed_launch_settings,
    resolve_endpoint as _resolve_endpoint,
    resolve_settings as _resolve_settings,
)

log = logging.getLogger("llm.providers.omlx.supervisor")


def _mark_endpoint_alive(server_id: str, host: str, port: int) -> None:
    """Refresh endpoint freshness after supervisor-owned health succeeds."""
    try:
        from llm.network_endpoints import upsert_endpoint
        from llm.network_discovery import update_server_status
        upsert_endpoint(server_id, host, ok=True, latency_ms=0)
        update_server_status(server_id, status="online", latency_ms=0)
    except Exception as e:
        log.warning("_mark_endpoint_alive failed for %s: %s", server_id, e)

# Managed first spawn pays Gatekeeper and cold Metal startup; other servers keep the caller's timeout.
MANAGED_HEALTH_STARTUP_CAP_S = 600.0
DEFAULT_HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_INTERVAL_S = 0.5
SIGTERM_GRACE_S = 5.0
_life: dict[str, restart.Life] = {}


def is_corsia_process(pid: int) -> bool:
    """Match an observed process to the supervisor's durable spawn records, regardless of argv."""
    return process.is_recorded_process(RUNTIME_DIR, pid)


def _pid_path(server_id: str):
    return process.pid_path(RUNTIME_DIR, server_id)


def _read_pid(server_id: str, *, host: str | None = None, port: int | None = None) -> int | None:
    owner = (_port_owner_pid(host, port) if host is not None and port is not None
             else process.PORT_OWNER_UNKNOWN)
    return process.read_pid(RUNTIME_DIR, server_id, legacy_port_owner_pid=owner)


def _write_pid(server_id: str, pid: int, *, omlx_version: str | None = None) -> None:
    process.write_pid(RUNTIME_DIR, server_id, pid, omlx_version=omlx_version)


def schedule_startup_upgrade(server_id: str, recorded: str | None = None) -> asyncio.Task | None:
    """Close admission for an adopted outdated server without yielding."""
    from llm.providers.omlx_upgrade import schedule_upgrade

    return schedule_upgrade(server_id, _life.get(server_id), recorded, OMLX_VERSION, _stop_server,
                            _start_recovery, _tail_log, MANAGED_HEALTH_STARTUP_CAP_S)


def is_restarting(server_id: str) -> bool:
    """Whether the supervisor currently owns a restart, including model restoration."""
    life = _life.get(server_id)
    return life is not None and life.restart is not None


def request_slot(server_id: str | None) -> AbstractAsyncContextManager[None]:
    """Admit a provider call through this supervisor's existing server lifecycle."""
    return restart.request_slot(_life, server_id)


def _clear_pid(server_id: str) -> None:
    process.clear_pid(RUNTIME_DIR, server_id)


_process_alive = process.process_alive
_port_owner_pid = process.port_owner_pid


def _process_exited(pid: int) -> bool:
    """Return true for a dead process, including an exited child awaiting reap."""
    return process.process_exited(pid, _process_alive)


def _is_managed_omlx_server(server_id: str) -> bool:
    from llm.managed_omlx_binary import is_corsia_managed_server
    from llm.network_discovery import get_server

    return is_corsia_managed_server(get_server(server_id) or {})


async def _wait_for_healthy(
    host: str, port: int, *, timeout_s: float, server_id: str | None = None, pid: int | None = None,
    require_preload: bool = False,
) -> str:
    """Return whether oMLX became healthy, its child exited, or the timeout elapsed."""
    if require_preload:
        from llm.providers.omlx_endpoints import health_url
        url = health_url(host, port)
    else:
        url = models_probe_url(lan_base(host, port))
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() - started < timeout_s:
            if pid is not None and _process_exited(pid):
                return "exited"
            try:
                headers = {}
                if server_id:
                    from llm.providers.omlx_public_auth import public_headers
                    headers = await public_headers({"server_id": server_id}, url=url)
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    return "healthy"
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(HEALTH_POLL_INTERVAL_S)
    return "timed_out"


def _tail_log(server_id: str, lines: int = 20) -> str:
    return process.tail_log(RUNTIME_DIR, server_id, lines=lines)


_spawn_process = process.spawn_process


def _failed_boot_result(
    server_id: str,
    host: str,
    port: int,
    pid: int,
    *,
    reason: str = "timed_out",
    timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
) -> dict[str, Any]:
    # An exited child is already reaped, and the OS may reuse its pid; only a live one is signalled.
    if reason != "exited" and _read_pid(server_id) == pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError) as exc:
            log.warning(
                "SIGTERM after failed boot for %s pid=%s: %s",
                server_id,
                pid,
                exc,
            )
    if _process_exited(pid):
        _clear_pid(server_id)
    # The log tail is for diagnosis, so it goes to the backend log; the person gets a next step.
    log.warning(
        "oMLX boot failed for %s on %s:%s (%s after %ss). Last log lines:\n%s",
        server_id, host, port, reason, int(timeout_s), _tail_log(server_id, lines=20) or "(empty log)",
    )
    return {
        "ok": False,
        "message": (
            "The local model stopped while it was starting. Start it again; "
            "if it stops again, restart this computer, then start it."
            if reason == "exited"
            else f"Couldn’t start the local model within {_duration(timeout_s)}. Start it again."
        ),
    }


def _duration(seconds: float) -> str:
    whole = int(seconds)
    if whole >= 120 and whole % 60 == 0:
        return f"{whole // 60} minutes"
    return f"{whole} seconds"


def watch(server_id: str, pid: int) -> None:
    life = _life.setdefault(server_id, restart.Life())
    asked = restart.restart_logged_from_now(RUNTIME_DIR, server_id)
    life.watch(pid, lambda status: restart.on_exit(
        server_id, life, pid, _start_recovery, _tail_log, status, asked,
        timeout=MANAGED_HEALTH_STARTUP_CAP_S,
    ))


def stop_watching() -> None:  # the backend is shutting down: an oMLX exit from here on is not a crash
    for life in _life.values():
        life.shutdown()


async def _confirm_ready(
    sid: str, host: str, port: int, pid: int, *, timeout_s: float | None = None, require_preload: bool = False,
) -> dict[str, Any] | None:
    """Complete startup or adoption through the same readiness and watch owner."""
    try:
        if timeout_s is not None:
            health = await _wait_for_healthy(
                host, port, timeout_s=timeout_s, server_id=sid, pid=pid,
                require_preload=require_preload,
            )
            if health != "healthy":
                if require_preload and health == "timed_out":
                    await restart.require_stopped(lambda: _stop_server({"server_id": sid, "host": host, "port": port}))
                return _failed_boot_result(sid, host, port, pid, reason=health, timeout_s=timeout_s)
        await run_in_db(_mark_endpoint_alive, sid, host, port)
        watch(sid, pid)
        return None
    except asyncio.CancelledError:
        if require_preload:
            await restart.require_stopped(lambda: _stop_server({"server_id": sid, "host": host, "port": port}))
        raise


async def start_server(cfg: dict) -> dict[str, Any]:
    """Spawn ``omlx serve`` for the given network-servers row. Idempotent."""
    sid = cfg.get("server_id")
    if not sid:
        return {"ok": False, "message": "start_server: missing server_id in cfg"}
    try:
        async with request_slot(sid):
            return await _start_server(cfg, sid, require_preload=False)
    except restart.UpgradeAdmissionError as exc:
        return {"ok": False, "message": str(exc)}


async def _start_recovery(cfg: dict) -> dict[str, Any]:
    sid = cfg["server_id"]
    life = _life.get(sid)
    return await _start_server(cfg, sid, require_preload=bool(life and life.hold_requests))


async def _start_server(cfg: dict, sid: str, *, require_preload: bool) -> dict[str, Any]:
    """Resolve and start a server after its admission gate has opened."""
    host, port = await run_in_db(_resolve_endpoint, cfg)
    if not host or not port:
        return {"ok": False, "message": "start_server: cfg has no host/port"}
    host = _listen_host(host)

    existing_pid = _read_pid(sid, host=host, port=int(port))
    if existing_pid and _process_alive(existing_pid):
        owner = _port_owner_pid(host, port)
        if owner == existing_pid:
            failure = await _confirm_ready(
                sid, host, int(port), existing_pid,
                timeout_s=MANAGED_HEALTH_STARTUP_CAP_S if require_preload else None, require_preload=require_preload,
            )
            if failure is not None:
                return failure
            return {
                "ok": True,
                "pid": existing_pid,
                "already_running": True,
                "message": f"oMLX already running on {host}:{port} (pid {existing_pid})",
            }
        return {"ok": False, "pid": existing_pid,
                "message": "The local model isn't responding. Stop it in Settings › Providers, then retry."}

    owner = _port_owner_pid(host, port)
    if isinstance(owner, int) and (existing_pid is None or owner != existing_pid):
        return {
            "ok": False,
            "message": (
                f"Port {port} on {host} is already in use by pid {owner}. "
                "Free the port or change the server config before starting oMLX."
            ),
        }

    settings = await run_in_db(_resolve_settings, cfg)
    managed = await run_in_db(_is_managed_omlx_server, sid)
    if managed:
        settings = _managed_launch_settings(settings)
    argv = process.managed_omlx_argv(host, int(port), settings, managed=managed)
    if isinstance(argv, dict):
        return argv

    log_path = process.log_path(RUNTIME_DIR, sid)
    spawned = _spawn_process(argv, log_path)
    if isinstance(spawned, dict):
        return spawned
    proc = spawned

    _write_pid(sid, proc.pid, omlx_version=OMLX_VERSION if managed else None)
    log.info("start_server: spawned omlx pid=%s for server_id=%s on %s:%s",
             proc.pid, sid, host, port)

    timeout_s = MANAGED_HEALTH_STARTUP_CAP_S if managed else DEFAULT_HEALTH_TIMEOUT_S
    failure = await _confirm_ready(
        sid, host, int(port), proc.pid, timeout_s=timeout_s, require_preload=bool(managed and require_preload),
    )
    if failure is not None:
        return failure

    return {
        "ok": True,
        "pid": proc.pid,
        "host": host,
        "port": int(port),
        "log_path": str(log_path),
        "message": f"oMLX started on {host}:{port} (pid {proc.pid})",
    }


async def stop_server(cfg: dict) -> dict[str, Any]:
    """Stop the oMLX subprocess we spawned for this row; a stop is never followed by a crash restart."""
    sid = cfg.get("server_id")
    if not sid:
        return {"ok": False, "message": "stop_server: missing server_id in cfg"}
    return await restart.stop(_life, sid, lambda: _pid_path(sid).exists(), lambda: _stop_server(cfg))


async def _stop_server(cfg: dict) -> dict[str, Any]:
    sid = cfg["server_id"]
    host, port = await run_in_db(_resolve_endpoint, cfg)
    host = _listen_host(host) if host else None
    had_record = _pid_path(sid).exists()
    pid = _read_pid(sid, host=host, port=int(port) if port else None)
    if not pid:
        return {"ok": had_record, "already_stopped": had_record, "message": "Not running"}

    if not _process_alive(pid):
        _clear_pid(sid)
        return {"ok": True, "already_stopped": True, "message": "Already stopped (stale pid cleared)"}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(sid)
        return {"ok": True, "already_stopped": True, "message": "Already stopped"}
    except Exception as exc:
        log.exception("SIGTERM failed")
        return {"ok": False, "message": "Couldn’t stop the model server. Try again.", "error": f"SIGTERM failed: {exc}"}

    exited = False
    deadline = time.monotonic() + SIGTERM_GRACE_S
    while time.monotonic() < deadline:
        exited = _process_exited(pid)
        if exited:
            break
        await asyncio.sleep(0.2)

    if not exited:
        exited = _process_exited(pid)

    if not exited and _read_pid(sid, host=host, port=int(port) if port else None) != pid:
        exited = True
    if not exited:
        try:
            os.kill(pid, signal.SIGKILL)
            await asyncio.sleep(0.2)
        except ProcessLookupError:
            exited = True
        except Exception as exc:
            log.exception("SIGKILL failed")
            return {"ok": False, "message": "Couldn’t stop the model server. Try again.", "error": f"SIGKILL failed: {exc}"}
        else:
            exited = _process_exited(pid)

    if not exited:
        return {"ok": False, "message": f"oMLX failed to stop (pid {pid})"}

    _clear_pid(sid)
    return {"ok": True, "pid": pid, "message": f"oMLX stopped (pid {pid})"}


async def stop_all_servers() -> list[dict[str, Any]]:
    """Stop every oMLX this supervisor spawned (its pid records, or a restart in flight); a foreign oMLX has none."""
    ids = sorted({p.stem.removeprefix("omlx-") for p in RUNTIME_DIR.glob("omlx-*.pid")} | restart.restarting(_life))
    for sid in ids:
        _life.setdefault(sid, restart.Life()).announce_stop()  # before the first stop yields to the loop
    return [await stop_server({"server_id": sid}) for sid in ids]


def reconcile(cfg: dict) -> dict[str, Any]:
    """Adopt or clear this server's pid file during backend startup."""
    sid = cfg.get("server_id")
    if not sid:
        return {"adopted": False, "cleared": False, "pid": None}
    had_record = _pid_path(sid).exists()
    host, port = _resolve_endpoint_via_freshness(cfg)
    pid = _read_pid(sid, host=host, port=int(port) if port else None)
    if pid is None:
        return {"adopted": False, "cleared": had_record, "pid": None}
    if _process_alive(pid):
        if host and port:
            _mark_endpoint_alive(sid, host, int(port))
        return {"adopted": True, "cleared": False, "pid": pid}
    _clear_pid(sid)
    return {"adopted": False, "cleared": True, "pid": pid}
