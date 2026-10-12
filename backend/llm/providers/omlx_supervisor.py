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

HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_INTERVAL_S = 0.5
SIGTERM_GRACE_S = 5.0


def _pid_path(server_id: str):
    return process.pid_path(RUNTIME_DIR, server_id)


def _log_path(server_id: str):
    return process.log_path(RUNTIME_DIR, server_id)


def _read_pid(server_id: str) -> int | None:
    return process.read_pid(RUNTIME_DIR, server_id)


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


def _port_owner_pid(host: str, port: int) -> int | None:
    return process.port_owner_pid(host, port)


def _build_argv(host: str, port: int, settings: dict) -> list[str]:
    return process.build_argv(host, port, settings)

async def _wait_for_healthy(
    host: str, port: int, timeout_s: float, *, server_id: str | None = None,
) -> bool:
    """Poll the same model-list surface the provider uses for readiness."""
    url = models_probe_url(lan_base(host, port))
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                headers = {}
                if server_id:
                    from llm.providers.omlx_public_auth import public_headers
                    headers = await public_headers({"server_id": server_id}, url=url)
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    return True
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(HEALTH_POLL_INTERVAL_S)
    return False


def _tail_log(server_id: str, lines: int = 20) -> str:
    return process.tail_log(RUNTIME_DIR, server_id, lines=lines)


def _resolve_settings(cfg: dict) -> dict:
    """Merge the row's metadata_json and stored key with ``cfg`` so explicit cfg keys win."""
    sid = cfg.get("server_id")
    base: dict = {}
    if sid:
        try:
            from infra.key_store_servers import resolve_server
            from llm.network_discovery import get_server_metadata
            base = get_server_metadata(sid) or {}
            if stored_key := resolve_server(sid):
                base["api_key"] = stored_key
        except Exception as e:
            log.warning(f"server metadata lookup failed for {sid}: {e}")
            base = {}
    for k in ("api_key", "model_dir", "max_model_memory"):
        if cfg.get(k) is not None:
            base[k] = cfg[k]
    return base


def _resolve_stale_endpoint(
    server_id: str,
    host: str | None,
    port: int | str | None,
) -> tuple[str | None, int | None]:
    try:
        from llm.network_discovery import get_server
        from llm.network_endpoints import get_endpoints
        server = get_server(server_id)
        if not server:
            return None, None
        port = port or (int(server["port"]) if server.get("port") else None)
        if not host:
            for ep in get_endpoints(server_id) or []:
                if ep.get("host") in ("127.0.0.1", "localhost"):
                    host = ep["host"]
                    break
            if not host:
                host = "127.0.0.1"
    except Exception as e:
        log.warning(f"endpoint resolution failed for {server_id}: {e}")
    return host, int(port) if port else None


def _resolve_endpoint(cfg: dict) -> tuple[str | None, int | None]:
    """Resolve (host, port), including offline rows with stale endpoints."""
    host, port = cfg.get("host"), cfg.get("port")
    if host and port:
        return host, int(port)

    fresh_host, fresh_port = _resolve_endpoint_via_freshness(cfg)
    if fresh_host and fresh_port:
        return fresh_host, int(fresh_port)

    server_id = cfg.get("server_id")
    if not server_id:
        return None, None
    return _resolve_stale_endpoint(server_id, host, port)


def _listen_host(resolved: str) -> str:
    """Keep the managed oMLX listener inside the exposure the user chose.

    A local setup binds CORSIA to loopback, so oMLX must not listen on a LAN
    address either. A server or pre-choice install keeps the endpoint host.
    The running service never refuses here: an unreadable choice falls back
    to loopback, the narrowest exposure.
    """
    from infra.bind_host import LOCAL_BIND, BindHostError, resolve_bind_host
    from infra.config import BOOTSTRAP_CONFIG_PATH

    try:
        bind = resolve_bind_host(BOOTSTRAP_CONFIG_PATH)
    except (BindHostError, OSError) as exc:
        log.warning("oMLX bind host unreadable (%s); using %s", exc, LOCAL_BIND)
        bind = LOCAL_BIND
    return LOCAL_BIND if bind == LOCAL_BIND else resolved


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
) -> dict[str, Any]:
    if _process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError) as exc:
            log.warning(
                "SIGTERM after failed boot for %s pid=%s: %s",
                server_id,
                pid,
                exc,
            )
    _clear_pid(server_id)
    tail = _tail_log(server_id, lines=20)
    return {
        "ok": False,
        "message": (
            f"oMLX did not become healthy on {host}:{port} within "
            f"{int(HEALTH_TIMEOUT_S)}s. Last log lines:\n{tail or '(empty log)'}"
        ),
    }


async def start_server(cfg: dict) -> dict[str, Any]:
    """Spawn ``omlx serve`` for the given network-servers row. Idempotent."""
    sid = cfg.get("server_id")
    if not sid:
        return {"ok": False, "message": "start_server: missing server_id in cfg"}

    host, port = await run_in_db(_resolve_endpoint, cfg)
    if not host or not port:
        return {"ok": False, "message": "start_server: cfg has no host/port"}
    host = _listen_host(host)

    existing_pid = _read_pid(sid)
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
        log.info("start_server: stale pid %s for %s — port owned by %s",
                 existing_pid, sid, owner)
        _clear_pid(sid)

    owner = _port_owner_pid(host, port)
    if owner is not None and (existing_pid is None or owner != existing_pid):
        return {
            "ok": False,
            "message": (
                f"Port {port} on {host} is already in use by pid {owner}. "
                "Free the port or change the server config before starting oMLX."
            ),
        }

    omlx_bin = shutil.which("omlx")
    if not omlx_bin:
        return {
            "ok": False,
            "message": (
                "omlx binary not found on PATH. Install oMLX "
                "(`brew install omlx` or pip install omlx) and retry."
            ),
        }

    settings = await run_in_db(_resolve_settings, cfg)
    argv = _build_argv(host, int(port), settings)
    argv[0] = omlx_bin

    log_path = _log_path(sid)
    spawned = _spawn_process(argv, log_path)
    if isinstance(spawned, dict):
        return spawned
    proc = spawned

    _write_pid(sid, proc.pid)
    log.info("start_server: spawned omlx pid=%s for server_id=%s on %s:%s",
             proc.pid, sid, host, port)

    healthy = await _wait_for_healthy(
        host, int(port), HEALTH_TIMEOUT_S, server_id=sid,
    )
    if healthy:
        await run_in_db(_mark_endpoint_alive, sid, host, int(port))
    if not healthy:
        return _failed_boot_result(sid, host, int(port), proc.pid)

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

    pid = _read_pid(sid)
    if not pid:
        return {"ok": False, "message": "Not running (no pid file)"}

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


def reconcile(cfg: dict) -> dict[str, Any]:
    """Adopt or clear this server's pid file during backend startup."""
    sid = cfg.get("server_id")
    if not sid:
        return {"adopted": False, "cleared": False, "pid": None}
    pid = _read_pid(sid)
    if pid is None:
        return {"adopted": False, "cleared": False, "pid": None}
    if _process_alive(pid):
        host, port = _resolve_endpoint_via_freshness(cfg)
        if host and port:
            _mark_endpoint_alive(sid, host, int(port))
        return {"adopted": True, "cleared": False, "pid": pid}
    _clear_pid(sid)
    return {"adopted": False, "cleared": True, "pid": pid}
