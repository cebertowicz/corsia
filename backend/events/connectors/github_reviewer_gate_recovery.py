"""Re-dispatch reviewer runs a backend restart cut off."""

from __future__ import annotations

import asyncio

from loguru import logger as log

from events import reviewer_gate, reviewer_gate_state
from infra.db_async import run_in_db

_RECOVERED: set[asyncio.Task[None]] = set()


async def recover_pending_reviews(loop: asyncio.AbstractEventLoop) -> int:
    """Re-evaluate each pending exact-head run through the normal gate; return how many."""
    dispatched = 0
    for pr_key, head_sha, internal_event in await run_in_db(reviewer_gate_state.get_pending):
        repo, separator, number = pr_key.rpartition("#")
        if not separator or not number.isdigit():
            log.warning("reviewer-gate: ignoring malformed pending key {!r}", pr_key)
            continue
        task = asyncio.create_task(asyncio.to_thread(
            reviewer_gate.on_pull_request_ready_for_review,
            repo, head_sha, int(number), internal_event, loop,
        ))
        _RECOVERED.add(task)
        task.add_done_callback(_RECOVERED.discard)
        dispatched += 1
    return dispatched
