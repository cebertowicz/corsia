"""Move an adopted managed oMLX onto the bundled build at startup, preserving its loaded models.

Only the spawn record proves which build is running: the executable path survives app updates and oMLX
reports no version over HTTP. A legacy record without a version needs the same upgrade. The supervisor's
existing Life owns Stop, shutdown and the restart; this module adds no lifecycle registry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from llm.providers import omlx_restart as restart
from llm.providers.omlx_supervisor import MANAGED_HEALTH_STARTUP_CAP_S

log = logging.getLogger("llm.providers.omlx.upgrade")
STAGE_TIMEOUT_S = MANAGED_HEALTH_STARTUP_CAP_S


async def recorded_version(server_id: str) -> str | None:
    """Read the running build from the supervisor's authoritative spawn record."""
    from infra.config import RUNTIME_DIR
    from infra.db_async import run_in_db
    from llm.providers.omlx_process import recorded_omlx_version

    return await run_in_db(recorded_omlx_version, RUNTIME_DIR, server_id)


async def _state(server_id: str, life: restart.Life) -> str:
    from infra.carry_in_capacity import UNREADABLE, omlx_state

    if life.stopping or (life.restart is not None and life.restart is not asyncio.current_task()):
        return UNREADABLE
    return await omlx_state(server_id, reads=2)


def schedule_upgrade(
    server_id: str, life: restart.Life | None, recorded: str | None, wanted: str,
    stop: Callable[[dict], Awaitable[dict[str, Any]]], start: Callable[[dict], Awaitable[dict[str, Any]]],
    tail_log: Callable[[str], str],
) -> asyncio.Task | None:
    """Close admission synchronously, before yielding to other startup work."""
    if life is None or life.stopping or recorded == wanted:
        return None
    if life.restart is not None:
        return life.restart
    return restart.schedule(
        server_id, life, _replace(server_id, life, wanted, stop, start, tail_log), hold_requests=True,
    )


async def _replace(
    server_id: str, life: restart.Life, wanted: str,
    stop: Callable[[dict], Awaitable[dict[str, Any]]], start: Callable[[dict], Awaitable[dict[str, Any]]],
    tail_log: Callable[[str], str],
) -> dict[str, Any]:
    from infra.carry_in_capacity import BUSY, IDLE
    from llm.local_keep_loaded import reload_after_restart
    from llm.providers.omlx_lifecycle import read_loaded

    # schedule() closed admission synchronously. Drain provider scopes, never
    # whole Turns (which may need another call), then prove idle inside that boundary.
    if life.requests:
        _, pending = await asyncio.wait(life.requests, timeout=STAGE_TIMEOUT_S)
        if pending:
            log.warning("Local oMLX upgrade failed: admitted calls did not finish")
            restart.emit(server_id, "restart.failed", status="upgrade", reason="drain_timeout")
            return {"restarted": False, "reason": "drain_timeout"}
    async with asyncio.timeout(STAGE_TIMEOUT_S):
        state = await _state(server_id, life)
    if state != IDLE:
        if state == BUSY:
            log.warning("Local oMLX upgrade failed: the server has outside activity")
            restart.emit(server_id, "restart.failed", status="upgrade", reason="busy")
            return {"restarted": False, "reason": "busy"}
        log.warning("Local oMLX upgrade skipped until next startup: idle status could not be established")
        restart.emit(server_id, "restart.skipped", status="upgrade", reason="not_idle")
        return {"restarted": False, "reason": "not_idle"}
    rows = await asyncio.wait_for(read_loaded({"server_id": server_id}), STAGE_TIMEOUT_S)
    if rows is None:
        log.warning("Local oMLX upgrade skipped until next startup: loaded models could not be read")
        restart.emit(server_id, "restart.skipped", status="upgrade", reason="loaded_unknown")
        return {"restarted": False, "reason": "loaded_unknown"}
    loaded = sorted({row["identifier"] for row in rows})
    if await asyncio.wait_for(recorded_version(server_id), STAGE_TIMEOUT_S) == wanted:
        return {"restarted": False, "reason": "already_current"}
    life.stopping = True  # the exit caused by our internal stop is intentional
    try:
        stopped = await asyncio.wait_for(stop({"server_id": server_id}), STAGE_TIMEOUT_S)
    except Exception:
        life.stopping = False  # a failed stop leaves the existing crash watch responsible
        raise
    if not stopped.get("ok"):
        life.stopping = False
        log.warning("Local oMLX upgrade stop failed: %s", stopped.get("message"))
        restart.emit(server_id, "restart.failed", status="upgrade", reason="stop_failed")
        return {"restarted": False, "reason": "stop_failed"}
    life.drop_watch()
    life.stopping = False
    life.restarts.clear()  # intentional upgrade has the same fresh budget as a dashboard restart
    async with asyncio.timeout(STAGE_TIMEOUT_S * (restart.MAX_RESTARTS + 1)):
        started = await restart.start_with_recovery(server_id, life, "upgrade", start, tail_log)
    if not started:
        return {"restarted": False, "reason": "start_failed"}
    async with asyncio.timeout(STAGE_TIMEOUT_S * (len(loaded) + 1)):
        await reload_after_restart(server_id, loaded)
    restored = await asyncio.wait_for(read_loaded({"server_id": server_id}), STAGE_TIMEOUT_S)
    missing = sorted(set(loaded) - {row["identifier"] for row in restored or []})
    if restored is None or missing:
        log.error("Local oMLX moved to %s but model restoration was not confirmed: %s", wanted, missing)
        restart.emit(server_id, "restart.failed", status="upgrade", reason="reload_failed", missing=missing)
        return {"restarted": True, "reason": "reload_failed", "missing": missing or loaded}
    restart.emit(server_id, "restart.completed", status="upgrade", version=wanted)
    log.info("Local oMLX moved to %s and restored %d model(s)", wanted, len(loaded))
    return {"restarted": True, "loaded": loaded}
