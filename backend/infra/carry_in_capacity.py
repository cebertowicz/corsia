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
from collections.abc import Awaitable, Callable

from infra.db_async import run_in_db, run_off_db

IDLE_GAP_SECONDS = 10.0  # the workspace local-admission contract's two-read spacing
STATUS_TIMEOUT_SECONDS = 2.0


def _ready_row() -> tuple[str, str, set[str]] | None:
    """``(model, server_id, names)`` of the one ready local oMLX Native row: a lasting answer, or None."""
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
    return str(rows[0]["litellm_model"]), server_id, names


def _turn_running(db, names: set[str], exclude_thread: str | None) -> bool:
    """Whether another local Turn runs now: a transient answer."""
    marks = ",".join("?" for _ in names)
    return db.execute(
        f"SELECT 1 FROM active_turns WHERE status IN ('running','waiting_retry') "
        f"AND (model IN ({marks}) OR model LIKE 'omlx@%') AND thread_id IS NOT ? LIMIT 1",
        (*names, exclude_thread),
    ).fetchone() is not None


def _local_state(exclude_thread: str | None) -> tuple[tuple[str, str] | None, bool]:
    """``(row, running)``: the ready local row (None when there is not exactly one) and whether another local Turn runs."""
    from infra.database import get_db

    row = _ready_row()
    return (None, False) if row is None else (row[:2], _turn_running(get_db(), row[2], exclude_thread))


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


async def wait_until_quiet(read: Callable[[], Awaitable[str]], *, timeout: float, interval: float) -> bool:
    """The one wait for a quiet seat: retry ``read`` (an :func:`omlx_state`-shaped probe) every ``interval`` until ``idle``.

    True once idle; False when ``timeout`` passes first (``timeout`` 0 is one try) or a try reads
    ``unreadable``, which returns at once: there is nothing to wait for. A prefill-guard resend
    waits out memory already held with one status reading; admitting new work needs two and the
    local-Turn check on every try (:func:`ready_idle`).
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while (state := await read()) != IDLE:
        if state == UNREADABLE or asyncio.get_running_loop().time() + interval > deadline:
            return False
        await asyncio.sleep(interval)
    return True


async def ready_idle(bound: str | None = None, exclude_thread: str | None = None, wait: float = 0.0) -> str | None:
    """The one ready local oMLX Native model, proved idle by two reads; ``bound`` when given, never another row.

    ``wait`` seconds is how long a busy seat or another local Turn may take to clear; each try (one
    proof, not a wait followed by another check) re-reads the row, the local Turns and the status.
    None when it does not clear, when there is no single ready row or it is not ``bound``, or when
    the status is unreadable.
    """
    from llm.provider_overflow import GUARD_QUIET_INTERVAL_SECONDS

    model: list[str] = []

    async def read() -> str:
        """One try: a lost or other row ends the wait, a running local Turn is busy, else two status reads."""
        row, running = await run_off_db(_local_state, exclude_thread)
        if row is None or (bound is not None and row[0] != bound):
            return UNREADABLE
        if running:
            return BUSY
        state = await omlx_state(row[1], reads=2)
        model[:] = [row[0]] if state == IDLE else []
        return state

    quiet = await wait_until_quiet(read, timeout=wait, interval=GUARD_QUIET_INTERVAL_SECONDS)
    return model[0] if quiet else None
