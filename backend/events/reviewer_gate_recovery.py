"""Restart recovery for eligible exact-head reviewer runs."""

from __future__ import annotations

import asyncio

from loguru import logger as log

from events import reviewer_gate_state


def recover_pending_reviews(loop: asyncio.AbstractEventLoop | None = None) -> int:
    """Re-dispatch reviewer runs interrupted by a backend restart."""
    from events.reviewer_gate import _PENDING_RETRIES, _evaluate_review_gate

    dispatched = 0
    for pr_key, head_sha, internal_event in reviewer_gate_state.get_pending():
        repo, separator, number = pr_key.rpartition("#")
        if not separator or not number.isdigit():
            log.warning("reviewer-gate: ignoring malformed pending key {!r}", pr_key)
            continue
        if loop is None or loop.is_closed():
            continue
        task = asyncio.create_task(asyncio.to_thread(
            _evaluate_review_gate,
            repo, head_sha, int(number), internal_event, loop,
        ))
        _PENDING_RETRIES.add(task)
        task.add_done_callback(_PENDING_RETRIES.discard)
        dispatched += 1
    return dispatched
