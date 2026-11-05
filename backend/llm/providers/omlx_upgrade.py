"""Move an adopted managed oMLX onto the bundled build once idle, preserving its loaded models.

Only the spawn record proves which build is running: the executable path survives app updates and oMLX
reports no version over HTTP. A legacy record without a version needs the same upgrade. The supervisor's
existing Life owns Stop, shutdown and the restart; this module adds no lifecycle registry.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from llm.providers import omlx_restart as restart

log = logging.getLogger("llm.providers.omlx.upgrade")
QUIET_INTERVAL_S = 60.0


async def upgrade_managed_omlx() -> dict[str, Any] | None:
    """Consider only CORSIA's online managed server after startup discovery has adopted it."""
    from infra.db_async import run_in_db
    from llm.local_sources import corsia_omlx_server
    from llm.providers.omlx_supervisor import upgrade_server

    try:
        server = await run_in_db(corsia_omlx_server)
        if not server or server.get("status") != "online":
            return None
        return await upgrade_server(server["id"])
    except Exception:
        log.exception("The local model server's upgrade did not run")
        return {"restarted": False, "reason": "error"}


async def _state(server_id: str, life: restart.Life) -> str:
    from infra.carry_in_capacity import UNREADABLE, omlx_state

    if life.stopping or life.restart is not None:
        return UNREADABLE
    return await omlx_state(server_id, reads=2)


async def restart_when_idle(
    server_id: str, life: restart.Life | None, recorded: str | None, wanted: str,
    stop: Callable[[dict], Awaitable[dict[str, Any]]], start: Callable[[dict], Awaitable[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Wait for idle, then let the supervisor's existing restart task replace the adopted process."""
    from infra.carry_in_capacity import wait_until_quiet
    from llm.providers.omlx_lifecycle import read_loaded

    if life is None or life.stopping:
        return {"restarted": False, "reason": "not_running"}
    if recorded == wanted:
        return None
    log.info("Local oMLX is %s; waiting for idle to run bundled %s", recorded or "unrecorded", wanted)
    quiet = await wait_until_quiet(partial(_state, server_id, life), timeout=float("inf"), interval=QUIET_INTERVAL_S)
    if life.stopping or life.restart is not None:
        return {"restarted": False, "reason": "stopped_or_restarting"}
    if not quiet:
        log.warning("Local oMLX upgrade deferred: idle status could not be established")
        return {"restarted": False, "reason": "not_idle"}
    rows = await read_loaded({"server_id": server_id})
    if rows is None:
        log.warning("Local oMLX upgrade deferred: loaded models could not be read")
        return {"restarted": False, "reason": "loaded_unknown"}
    if life.stopping or life.restart is not None:
        return {"restarted": False, "reason": "stopped_or_restarting"}
    loaded = sorted({row["identifier"] for row in rows})
    return await restart.schedule(server_id, life, _replace(server_id, life, loaded, wanted, stop, start))


async def _replace(
    server_id: str, life: restart.Life, loaded: list[str], wanted: str,
    stop: Callable[[dict], Awaitable[dict[str, Any]]], start: Callable[[dict], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    from llm.local_keep_loaded import reload_after_restart
    from llm.providers.omlx_lifecycle import read_loaded

    restart.emit(server_id, "restart.attempted", status="upgrade", version=wanted)
    life.stopping = True  # the exit caused by our internal stop is intentional
    try:
        stopped = await stop({"server_id": server_id})
    except Exception:
        life.stopping = False  # a failed stop leaves the existing crash watch responsible
        raise
    if not stopped.get("ok"):
        life.stopping = False
        log.warning("Local oMLX upgrade stop failed: %s", stopped.get("message"))
        return {"restarted": False, "reason": "stop_failed"}
    life.drop_watch()
    life.stopping = False
    started = await start({"server_id": server_id})
    if not started.get("ok"):
        log.error("Local oMLX upgrade start failed: %s", started.get("message"))
        return {"restarted": False, "reason": "start_failed"}
    await reload_after_restart(server_id, loaded)
    restored = await read_loaded({"server_id": server_id})
    missing = sorted(set(loaded) - {row["identifier"] for row in restored or []})
    if restored is None or missing:
        log.error("Local oMLX moved to %s but model restoration was not confirmed: %s", wanted, missing)
        restart.emit(server_id, "restart.failed", status="upgrade", reason="reload_failed", missing=missing)
        return {"restarted": True, "reason": "reload_failed", "missing": missing or loaded}
    restart.emit(server_id, "restart.completed", status="upgrade", version=wanted)
    log.info("Local oMLX moved to %s and restored %d model(s)", wanted, len(loaded))
    return {"restarted": True, "loaded": loaded}
