"""Start CORSIA's own oMLX at service startup when its process is gone."""

from __future__ import annotations

import asyncio

_tasks: set[asyncio.Task] = set()


def reconcile_omlx_servers(servers: list[dict], logger) -> list[str]:
    """Adopt or clear each oMLX's pid; return CORSIA's own servers that must be started.

    Only CORSIA's managed server qualifies, and only when its process is gone, its model
    is on disk and the person did not stop it: a move or a restart then never leaves the
    local model off. A person's own oMLX is never started.
    """
    from llm.managed_omlx_binary import is_corsia_managed_server, managed_model_on_disk
    from llm.providers.omlx_supervisor import person_stopped, reconcile

    to_start: list[str] = []
    for server in servers:
        if server.get("provider") != "omlx":
            continue
        result = reconcile({"server_id": server["id"]})
        if result.get("adopted"):
            logger.info(
                f"oMLX supervisor: adopted pid {result['pid']} for {server['id']}"
            )
            continue
        if result.get("cleared"):
            logger.info(
                f"oMLX supervisor: cleared stale pid {result['pid']} for {server['id']}"
            )
        if (is_corsia_managed_server(server) and server.get("status") != "disabled"
                and not person_stopped(server["id"]) and managed_model_on_disk()):
            to_start.append(server["id"])
    return to_start


async def start_managed_omlx(server_ids: list[str], logger) -> None:
    from llm.providers.omlx_supervisor import start_server

    for server_id in server_ids:
        try:
            result = await start_server({"server_id": server_id})
        except Exception as exc:
            logger.warning(f"oMLX supervisor: start failed for {server_id}: {exc}")
            continue
        if not result.get("ok"):
            logger.warning(f"oMLX supervisor: start failed for {server_id}: {result.get('message')}")
            continue
        logger.info(f"oMLX supervisor: started {server_id} (pid {result.get('pid')})")
        from llm.network_discovery import _discover_and_notify

        await _discover_and_notify(server_id)


def start_in_background(server_ids: list[str], logger) -> None:
    """Its own task: a cold start can take minutes and must not delay discovery or Ready."""
    _tasks.add(task := asyncio.create_task(start_managed_omlx(server_ids, logger)))
    task.add_done_callback(_tasks.discard)
