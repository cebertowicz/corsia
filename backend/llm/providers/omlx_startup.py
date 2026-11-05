"""Adopt supervised oMLX processes and start the managed upgrade before fan-out."""

from __future__ import annotations

import asyncio

from infra.db_async import run_in_db


def _reconcile_servers(servers: list[dict], logger) -> dict[str, int]:
    """Adopt or clear each oMLX pid record and return adopted server IDs and PIDs."""
    from llm.providers.omlx_supervisor import reconcile

    adopted: dict[str, int] = {}
    for server in servers:
        if server.get("provider") != "omlx":
            continue
        result = reconcile({"server_id": server["id"]})
        if result.get("adopted"):
            adopted[server["id"]] = result["pid"]
            logger.info(f"oMLX supervisor: adopted pid {result['pid']} for {server['id']}")
        elif result.get("cleared"):
            logger.info(f"oMLX supervisor: cleared stale pid {result['pid']} for {server['id']}")
    return adopted


async def adopt_and_upgrade(logger) -> asyncio.Task | None:
    """Adopt and gate an outdated managed server before startup work can run."""
    from llm.local_sources import corsia_omlx_server
    from llm.network_discovery import get_servers
    from llm.providers.omlx_supervisor import schedule_startup_upgrade, watch
    from llm.providers.omlx_upgrade import recorded_version

    servers = await run_in_db(get_servers)
    adopted = await run_in_db(_reconcile_servers, servers or [], logger)
    managed = corsia_omlx_server(servers or [])
    server_id = managed["id"] if managed and managed["id"] in adopted else None
    version = await recorded_version(server_id) if server_id else None
    # A queued exit callback cannot run before the managed admission gate closes.
    for adopted_id, pid in adopted.items():
        watch(adopted_id, pid)
    return schedule_startup_upgrade(server_id, version) if server_id else None
