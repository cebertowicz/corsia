"""Prepare an adopted managed oMLX for replacement under the recovery owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from llm.providers import omlx_restart as restart


async def recorded_version(server_id: str) -> str | None:
    """Read the running build from the supervisor's authoritative spawn record."""
    from infra.config import RUNTIME_DIR
    from infra.db_async import run_in_db
    from llm.providers.omlx_process import recorded_omlx_version

    return await run_in_db(recorded_omlx_version, RUNTIME_DIR, server_id)


def schedule_upgrade(
    server_id: str, life: restart.Life | None, recorded: str | None, wanted: str,
    stop: Callable[[dict], Awaitable[dict[str, Any]]], start: Callable[[dict], Awaitable[dict[str, Any]]],
    tail_log: Callable[[str], str], timeout: float,
) -> asyncio.Task | None:
    """Close admission and capture the observation before yielding."""
    from llm.network_discovery import get_loaded_model_ids

    if life is None or life.stopping or recorded == wanted:
        return None
    if life.restart is not None:
        return life.restart
    loaded = sorted(get_loaded_model_ids(server_id))
    generation = life.exit_generation
    restart.prune_restart_window(life)

    prepare = partial(_prepare, server_id, life, recorded, wanted, stop, timeout)
    return restart.schedule(
        server_id, life,
        restart.recover(server_id, life, "upgrade", loaded, generation, start, tail_log,
                        timeout=timeout, prepare=prepare, version=wanted),
        hold_requests=True,
    )


async def _prepare(
    server_id: str, life: restart.Life, recorded: str | None, wanted: str,
    stop: Callable[[dict], Awaitable[dict[str, Any]]], timeout: float, context: restart.Recovery,
) -> dict[str, Any] | None:
    from infra.carry_in_capacity import BUSY, IDLE, omlx_state
    from llm.providers.omlx_lifecycle import read_loaded

    current = recorded
    if current is None:
        async with asyncio.timeout(timeout):
            current = await recorded_version(server_id)
    if context.exited(life):
        return None
    if current == wanted:
        return {"restarted": False, "reason": "already_current"}
    if life.requests:
        _, pending = await asyncio.wait(life.requests, timeout=timeout)
        if context.exited(life):
            return None
        if pending:
            restart.emit(server_id, "restart.failed", status="upgrade", reason="drain_timeout")
            return {"restarted": False, "reason": "drain_timeout"}
    async with asyncio.timeout(timeout):
        state = await omlx_state(server_id, reads=2)
    if context.exited(life):
        return None
    if state != IDLE:
        reason = "busy" if state == BUSY else "not_idle"
        restart.emit(server_id, "restart.failed" if state == BUSY else "restart.skipped", status="upgrade", reason=reason)
        return {"restarted": False, "reason": reason}
    async with asyncio.timeout(timeout):
        rows = await read_loaded({"server_id": server_id})
    if rows is not None:
        context.models = sorted({row["identifier"] for row in rows})
    if context.exited(life):
        return None
    if rows is None:
        restart.emit(server_id, "restart.skipped", status="upgrade", reason="loaded_unknown")
        return {"restarted": False, "reason": "loaded_unknown"}
    async with asyncio.timeout(timeout):
        current = await recorded_version(server_id)
    if context.exited(life):
        return None
    if current == wanted:
        return {"restarted": False, "reason": "already_current"}
    life.stopping = True
    try:
        async with asyncio.timeout(timeout):
            stopped = await stop({"server_id": server_id})
    except Exception:
        life.stopping = False
        raise
    if not stopped.get("ok"):
        life.stopping = False
        restart.emit(server_id, "restart.failed", status="upgrade", reason="stop_failed")
        return {"restarted": False, "reason": "stop_failed"}
    life.drop_watch()
    life.stopping = False
    life.restarts.clear()
    return None
