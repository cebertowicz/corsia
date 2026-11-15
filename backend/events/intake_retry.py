"""Bounded retries for producers that cannot hand intake outages to their caller."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from events.intake_errors import failure_code


async def admit_with_retry(admit: Callable[..., Awaitable[Any]], *, pauses: Sequence[float],
                           sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, **event: Any) -> Any:
    """Retry only an unreachable intake, always with the original event and id."""
    for pause in (*pauses, None):
        try:
            result = await admit(**event)
        except Exception as exc:
            if pause is None or failure_code(exc) != "intake_unreachable":
                raise
        else:
            if result.refusal is None or result.refusal.code != "intake_unreachable" or pause is None:
                return result
        await sleep(pause)
    raise AssertionError("unreachable")
