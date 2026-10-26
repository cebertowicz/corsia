"""Waiting out a database writer held outside this process, for GitHub's record step."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: How long the record step waits out a writer held outside this process.
RECORD_WAIT_SECONDS = 60.0
_PAUSE_SECONDS = 0.5


async def record_when_writer_free(attempt: Callable[[bool], Awaitable[T]], delivery_id: str) -> T:
    """Run ``attempt(first)``, again while another process holds the database writer.

    Only GitHub's one record transaction goes through here. A refused lock commits
    nothing (that transaction opens with ``BEGIN IMMEDIATE`` and rolls back whole),
    so it is safe to run again; scheduling and settling are never re-run. ``first``
    is False on a re-run, so a delivery's one-time side effects (its ``received``
    event) are not repeated.

    This is not the retry ``infra.write_admission`` rules out. That ticket queues
    writers inside this process; the holder here is outside it, so SQLite's own
    ``busy_timeout`` (5 s) is all that guards the call, and R31 requires a delivery
    to be admitted after a longer hold. The pause is an ``asyncio`` sleep, so no
    worker thread sits in a sleep loop and the event loop stays free. Past
    ``RECORD_WAIT_SECONDS`` the lock error propagates as any other fault.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RECORD_WAIT_SECONDS
    first = True
    while True:
        try:
            return await attempt(first)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or loop.time() >= deadline:
                raise
        first = False
        log.warning("GitHub delivery %s waiting on a held database writer", delivery_id)
        await asyncio.sleep(_PAUSE_SECONDS)
