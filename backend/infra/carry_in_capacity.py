"""Which local model a C1 carry-in sample may use, and whether it is idle right now.

A sample runs only on the one ready local oMLX Native catalog row, never a
substitute, and only while no other local Turn runs and that server's own
authenticated ``/api/status`` proves it idle under the workspace's local
admission contract. ``infra.carry_in_drain`` asks :func:`ready_idle` before each
claim, waiting up to the prefill-guard bound for the seat's other clients to go
quiet, and once more just before each sample's provider call; ``llm.provider_overflow``
resends a prefill-guard refusal through the same :func:`wait_until_quiet`.
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


IDLE, BUSY, UNREADABLE = "idle", "busy", "unreadable"


async def omlx_state(server_id: str, *, reads: int = 1) -> str:
    """``idle``, ``busy`` or ``unreadable`` from ``reads`` authenticated ``/api/status`` reads.

    Each read needs both counts, and no active or waiting request; ``IDLE_GAP_SECONDS`` apart,
    more than one read also needs a request total that did not move. A missing address, an
    unreachable or unauthenticated status, or a reading without its counts (or, for
    several reads, its total) is ``unreadable``: nothing is proved, and waiting for it is blind.
    """
    import httpx

    from llm.providers.omlx_public_probe import probe_registered_status

    address = await run_in_db(_status_address, server_id)
    if address is None:
        return UNREADABLE
    totals = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(STATUS_TIMEOUT_SECONDS)) as client:
        for index in range(reads):
            if index:
                await asyncio.sleep(IDLE_GAP_SECONDS)
            probed = await probe_registered_status(client, server_id, *address)
            payload = (probed[2] if probed else None) or {}
            counts = (payload.get("active_requests"), payload.get("waiting_requests"))
            total = payload.get("total_requests")
            if None in counts or (reads > 1 and not isinstance(total, int)):
                return UNREADABLE
            if counts != (0, 0):
                return BUSY
            totals.append(total)
    return IDLE if len(set(totals)) == 1 else BUSY


async def wait_until_quiet(server_id: str, *, timeout: float, interval: float, reads: int = 1) -> bool:
    """The one wait for a quiet seat: retry :func:`omlx_state` every ``interval`` until ``idle``.

    True once idle; False when ``timeout`` passes first (``timeout`` 0 is one try) or the status
    is ``unreadable``, which returns at once. A prefill-guard resend waits out memory already
    held and needs one reading; admitting new work needs two (``reads=2``, :func:`ready_idle`).
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while (state := await omlx_state(server_id, reads=reads)) != IDLE:
        if state == UNREADABLE or asyncio.get_running_loop().time() + interval > deadline:
            return False
        await asyncio.sleep(interval)
    return True


async def ready_idle(bound: str | None = None, exclude_thread: str | None = None, wait: float = 0.0) -> str | None:
    """The one ready local oMLX Native model, proved idle by two reads; ``bound`` when given, never another row.

    ``wait`` seconds is how long a busy seat may take to go quiet (one proof per try, not a
    wait followed by another check); None when it does not, or its status is unreadable.
    """
    from llm.provider_overflow import GUARD_QUIET_INTERVAL_SECONDS

    local = await run_off_db(_local_row_now, exclude_thread)
    if local is None or (bound is not None and local[0] != bound):
        return None
    quiet = await wait_until_quiet(local[1], timeout=wait, interval=GUARD_QUIET_INTERVAL_SECONDS, reads=2)
    return local[0] if quiet else None
