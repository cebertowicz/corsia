"""Wait out what a repository policy transition meets, then run it.

A registration that meets an in-flight effect or a bounded owned process
(``infra.repo_policy_transition``) retries until that work ends, up to
:data:`TRANSITION_WAIT_SECONDS`, then is refused naming what is still running. A
conflict waiting cannot end (a long-running process) is refused at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time
from typing import TypeVar

from infra.repo_policy_transition import RepoPolicyTransitionBusy

_T = TypeVar("_T")

#: How long a registration waits for what it meets: the longest shell call, foreground
#: or background (a long-running process is never waited on).
TRANSITION_WAIT_SECONDS = 600.0
_FIRST_POLL_SECONDS = 0.25
_MAX_POLL_SECONDS = 2.0


def _duration(seconds: float) -> str:
    """A wait bound as a person says it: whole minutes, or seconds under a minute."""
    if seconds < 60:
        count, unit = max(1, round(seconds)), "second"
    else:
        count, unit = round(seconds / 60), "minute"
    return f"{count} {unit}{'' if count == 1 else 's'}"


async def wait_for_transition(
    attempt: Callable[[], Awaitable[_T]],
    *,
    wait_seconds: float | None = None,
) -> _T:
    """Run ``attempt`` until no in-flight effect or process it meets is left.

    Each attempt enters its transition afresh, so it re-reads its claim. Between
    attempts it sleeps on the event loop, holding no thread or lock. A conflict still
    there after ``wait_seconds`` (default :data:`TRANSITION_WAIT_SECONDS`) is refused,
    naming what is still running; any other refusal is raised at once.
    """
    limit = TRANSITION_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + limit
    poll = _FIRST_POLL_SECONDS
    while True:
        try:
            return await attempt()
        except RepoPolicyTransitionBusy as exc:
            remaining = deadline - time.monotonic()
            if not exc.retryable:
                raise
            if remaining <= 0:
                running = exc.running or "another repository change is in progress"
                raise RepoPolicyTransitionBusy(
                    f"Repositories can't change while {running}. It was still going after "
                    f"{_duration(limit)}; try again once it finishes.",
                    running=running,
                ) from exc
            await asyncio.sleep(min(poll, remaining))
            poll = min(poll * 2, _MAX_POLL_SECONDS)
