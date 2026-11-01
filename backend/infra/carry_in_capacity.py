"""Which local model a C1 carry-in sample may use, and whether it is idle right now.

A sample runs only on the one ready local oMLX Native catalog row, never a
substitute, and only while no other local Turn runs and that server's own
authenticated ``/api/status`` proves it idle under the workspace's local
admission contract. ``infra.carry_in_drain`` asks before each claim and again
just before each sample's provider call. ``llm.provider_overflow`` reads the
same status to resend a prefill-guard refusal once the seat has gone quiet.
"""

from __future__ import annotations

import asyncio

from infra.db_async import run_in_db, run_off_db

IDLE_GAP_SECONDS = 10.0  # the workspace local-admission contract's two-read spacing
STATUS_TIMEOUT_SECONDS = 2.0


def local_row(db, exclude_thread: str | None = None) -> tuple[str, str] | None:
    """``(model, server_id)`` of the one ready local oMLX Native row while no other local Turn runs."""
    from llm.locality import row_server_id
    from llm.model_status import get_model_status_rows
    from llm.resolution import resolve_execution_target

    rows = [
        row for row in get_model_status_rows()
        if row.get("status") == "ready" and row.get("provider") == "omlx" and row.get("is_local")
    ]
    if len(rows) != 1 or not (server_id := row_server_id(rows[0])):
        return None
    target = resolve_execution_target(str(rows[0]["litellm_model"]))
    if not (target.harness == "native" and target.provider == "omlx" and target.available):
        return None
    names = {str(rows[0].get("id") or ""), str(rows[0]["litellm_model"]), target.catalog_id}
    marks = ",".join("?" for _ in names)
    busy = db.execute(
        f"SELECT 1 FROM active_turns WHERE status IN ('running','waiting_retry') "
        f"AND (model IN ({marks}) OR model LIKE 'omlx@%') AND thread_id IS NOT ? LIMIT 1",
        (*names, exclude_thread),
    ).fetchone()
    return None if busy else (str(rows[0]["litellm_model"]), server_id)


def _local_row_now(exclude_thread: str | None) -> tuple[str, str] | None:
    from infra.database import get_db

    return local_row(get_db(), exclude_thread)


def _status_address(server_id: str) -> tuple[str, int] | None:
    from llm.network_discovery import get_server
    from llm.network_endpoints import get_active_endpoint

    server, endpoint = get_server(server_id), get_active_endpoint(server_id)
    if not server or not endpoint or not server.get("port"):
        return None
    return str(endpoint["host"]), int(server["port"])


def idle_reading(payload: dict | None) -> int | None:
    """The request total of an idle ``/api/status`` reading; None when busy or unreadable."""
    payload = payload or {}
    total = payload.get("total_requests")
    idle = payload.get("active_requests") == 0 and payload.get("waiting_requests") == 0
    return total if idle and isinstance(total, int) else None


async def omlx_idle(server_id: str) -> bool:
    """Two authenticated ``/api/status`` reads ``IDLE_GAP_SECONDS`` apart, each with no
    active or waiting request, and no request started between them; anything else is busy."""
    import httpx

    from llm.providers.omlx_public_probe import probe_registered_status

    address = await run_in_db(_status_address, server_id)
    if address is None:
        return False
    totals = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(STATUS_TIMEOUT_SECONDS)) as client:
        for index in range(2):
            if index:
                await asyncio.sleep(IDLE_GAP_SECONDS)
            probed = await probe_registered_status(client, server_id, *address)
            if (total := idle_reading(probed[2] if probed else None)) is None:
                return False
            totals.append(total)
    return totals[0] == totals[1]


async def wait_until_quiet(server_id: str, *, timeout: float, interval: float) -> bool:
    """Wait until one ``/api/status`` reading shows no active or waiting request.

    True once quiet; False when the timeout passes first or the status can't be read
    or lacks either count. One reading is enough here because the caller only waits
    out memory already held; the two-reading rule in :func:`omlx_idle` admits new work.
    """
    import httpx

    from llm.providers.omlx_public_probe import probe_registered_status

    address = await run_in_db(_status_address, server_id)
    if address is None:
        return False
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(timeout=httpx.Timeout(STATUS_TIMEOUT_SECONDS)) as client:
        while True:
            probed = await probe_registered_status(client, server_id, *address)
            payload = probed[2] if probed else None
            counts = [(payload or {}).get(key) for key in ("active_requests", "waiting_requests")]
            if None in counts:
                return False
            if counts == [0, 0]:
                return True
            if asyncio.get_running_loop().time() + interval > deadline:
                return False
            await asyncio.sleep(interval)


async def ready_idle(bound: str | None = None, exclude_thread: str | None = None) -> str | None:
    """The one ready local oMLX Native model, proved idle; ``bound`` when given, never another row."""
    local = await run_off_db(_local_row_now, exclude_thread)
    if local is None or (bound is not None and local[0] != bound):
        return None
    return local[0] if await omlx_idle(local[1]) else None
