"""Bounded retries for producers that cannot hand intake outages to their caller."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from events.intake_errors import failure_code

log = logging.getLogger(__name__)


async def admit_with_retry(admit: Callable[..., Awaitable[Any]], *, pauses: Sequence[float],
                           sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, **event: Any) -> Any:
    """Retry only an unreachable intake, always with the original event and id."""
    tries = len(pauses) + 1
    for attempt, pause in enumerate((*pauses, None), start=1):
        try:
            return await admit(**event)
        except Exception as exc:  # the intake reports an outage only by raising
            if pause is None or failure_code(exc) != "intake_unreachable":
                raise
            log.warning("intake unreachable for %s %s, try %s of %s: %s",
                        event.get("event_type"), event.get("event_id"), attempt, tries, exc)
        await sleep(pause)
    raise AssertionError("unreachable")
