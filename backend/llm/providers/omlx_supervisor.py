"""Manage CORSIA-owned oMLX server subprocesses."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import time
from typing import Any

import httpx

from infra.config import RUNTIME_DIR
from infra.db_async import run_in_db
from llm.network_endpoints import lan_base, models_probe_url
from llm.providers import omlx_process as process
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

# A first spawn pays Gatekeeper's scan of CORSIA's signed bundle and a cold Metal
# start; ordinary user-managed servers retain the caller's shorter timeout.
MANAGED_HEALTH_STARTUP_CAP_S = 600.0
DEFAULT_HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_INTERVAL_S = 0.5
SIGTERM_GRACE_S = 5.0


def is_corsia_process(pid: int) -> bool:
    """Match an observed process to the supervisor's durable spawn records, regardless of argv."""
    for path in RUNTIME_DIR.glob("omlx-*.pid"):
        if process.read_pid(path.parent, path.stem.removeprefix("omlx-"), preserve_legacy=True) == pid:
            return True
    return False


def _pid_path(server_id: str):
    return process.pid_path(RUNTIME_DIR, server_id)


def _log_path(server_id: str):
    return process.log_path(RUNTIME_DIR, server_id)


def _read_pid(server_id: str, *, host: str | None = None, port: int | None = None) -> int | None:
    owner = (_port_owner_pid(host, port) if host is not None and port is not None
             else process.PORT_OWNER_UNKNOWN)
    return process.read_pid(RUNTIME_DIR, server_id, legacy_port_owner_pid=owner)


def _write_pid(server_id: str, pid: int) -> None:
    process.write_pid(RUNTIME_DIR, server_id, pid)


def _clear_pid(server_id: str) -> None:
    process.clear_pid(RUNTIME_DIR, server_id)


def _process_alive(pid: int) -> bool:
    return process.process_alive(pid)


def _process_exited(pid: int) -> bool:
    """Return true for a dead process, including an exited child awaiting reap."""
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return not _process_alive(pid)
    return waited_pid == pid or not _process_alive(pid)


def _port_owner_pid(host: str, port: int) -> process.PortOwnerPid:
    return process.port_owner_pid(host, port)


def _build_argv(
    host: str,
    port: int,
    settings: dict,
    *,
    executable: str | None = None,
) -> list[str]:
    return process.build_argv(host, port, settings, executable=executable)


def _is_managed_omlx_server(server_id: str) -> bool:
    from llm.managed_omlx_binary import is_corsia_managed_server
    from llm.network_discovery import get_server

    return is_corsia_managed_server(get_server(server_id) or {})


def _managed_omlx_argv(
    host: str, port: int, settings: dict, *, managed: bool,
) -> list[str] | dict[str, Any]:
    """Resolve the bundled executable only for CORSIA's setup-created row."""
    if managed:
        install_error = process.managed_omlx_install_error()
        if install_error:
            return {"ok": False, "message": install_error}
        executable = process.managed_omlx_executable()
    else:
        executable = None
    executable = executable or shutil.which("omlx")
    if not executable:
        return {
            "ok": False,
            "message": (
                "omlx binary not found on PATH. Install oMLX "
                "(`brew install omlx` or pip install omlx) and retry."
            ),
        }
    return _build_argv(host, port, settings, executable=executable)


async def _wait_for_healthy(
    host: str, port: int, *, timeout_s: float, server_id: str | None = None, pid: int | None = None,
) -> str:
    """Return whether oMLX became healthy, its child exited, or the timeout elapsed."""
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


def _spawn_process(argv: list[str], log_path) -> subprocess.Popen | dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_fh = log_path.open("ab", buffering=0)
        return subprocess.Popen(
            argv,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "message": f"Failed to spawn omlx: {exc}"}
    except Exception as exc:
        return {"ok": False, "message": f"Failed to spawn omlx: {exc}"}


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


async def start_server(cfg: dict) -> dict[str, Any]:
    """Spawn ``omlx serve`` for the given network-servers row. Idempotent."""
    sid = cfg.get("server_id")
    if not sid:
        return {"ok": False, "message": "start_server: missing server_id in cfg"}

    host, port = await run_in_db(_resolve_endpoint, cfg)
    if not host or not port:
        return {"ok": False, "message": "start_server: cfg has no host/port"}
    host = _listen_host(host)

    existing_pid = _read_pid(sid, host=host, port=int(port))
    if existing_pid and _process_alive(existing_pid):
        owner = _port_owner_pid(host, port)
        if owner == existing_pid:
            await run_in_db(_mark_endpoint_alive, sid, host, int(port))
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
    argv = _managed_omlx_argv(host, int(port), settings, managed=managed)
    if isinstance(argv, dict):
        return argv

    log_path = _log_path(sid)
    spawned = _spawn_process(argv, log_path)
    if isinstance(spawned, dict):
        return spawned
    proc = spawned

    _write_pid(sid, proc.pid)
    log.info("start_server: spawned omlx pid=%s for server_id=%s on %s:%s",
             proc.pid, sid, host, port)

    timeout_s = MANAGED_HEALTH_STARTUP_CAP_S if managed else DEFAULT_HEALTH_TIMEOUT_S
    health = await _wait_for_healthy(
        host, int(port), timeout_s=timeout_s, server_id=sid, pid=proc.pid,
    )
    if health == "healthy":
        await run_in_db(_mark_endpoint_alive, sid, host, int(port))
    else:
        return _failed_boot_result(sid, host, int(port), proc.pid, reason=health, timeout_s=timeout_s)

    return {
        "ok": True,
        "pid": proc.pid,
        "host": host,
        "port": int(port),
        "log_path": str(log_path),
        "message": f"oMLX started on {host}:{port} (pid {proc.pid})",
    }


async def stop_server(cfg: dict) -> dict[str, Any]:
    """Stop the oMLX subprocess we spawned for this row."""
    sid = cfg.get("server_id")
    if not sid:
        return {"ok": False, "message": "stop_server: missing server_id in cfg"}

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
    except Exception as e:
        return {"ok": False, "message": f"SIGTERM failed: {e}"}

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
        except Exception as e:
            return {"ok": False, "message": f"SIGKILL failed: {e}"}
        else:
            exited = _process_exited(pid)

    if not exited:
        return {"ok": False, "message": f"oMLX failed to stop (pid {pid})"}

    _clear_pid(sid)
    return {"ok": True, "pid": pid, "message": f"oMLX stopped (pid {pid})"}


async def stop_all_servers() -> list[dict[str, Any]]:
    """Stop every oMLX this supervisor spawned, found by its own pid records; a foreign oMLX has none."""
    results = []
    for path in sorted(RUNTIME_DIR.glob("omlx-*.pid")):
        results.append(await stop_server({"server_id": path.stem.removeprefix("omlx-")}))
    return results


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
