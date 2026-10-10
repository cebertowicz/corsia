"""Retry a Turn's terminal write until the database lock lets it commit.

A Turn ends only when its terminal write commits. One refused because another
writer holds the lock leaves the Turn running with nothing else to end it, so
any finite number of attempts that then gives up strands it. The terminal
writers retry here until the write commits; a lock held indefinitely is its
own defect (#1077), reported by the holder log in ``infra.database``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

log = logging.getLogger(__name__)

_T = TypeVar("_T")
FIRST_DELAY_S = 1.0
MAX_DELAY_S = 30.0


@dataclass(frozen=True, slots=True)
class LockRefused:
    """A terminal write the lock refused before anything it wrote committed.

    The write classifies its own refusal: only it knows whether the refusal
    came before its commit. A refusal after the commit is raised instead, since
    retrying would repeat a write that already landed.
    """

    error: sqlite3.OperationalError


async def retry_until_committed(
    attempt: Callable[[], Awaitable[_T | LockRefused]], *, subject: str,
) -> _T:
    """Repeat ``attempt`` with capped backoff until it commits.

    Concurrent settlers of one Turn need no handshake: the first to commit
    wins, and every later attempt reads the committed terminal and returns.
    """
    delay = FIRST_DELAY_S
    for tries in itertools.count(1):
        result = await attempt()
        if not isinstance(result, LockRefused):
            if tries > 1:
                log.warning("terminal write for %s committed after %d attempts", subject, tries)
            return result
        if tries == 1:
            log.error(
                "terminal write for %s refused by the database lock; retrying "
                "until it commits: %s", subject, result.error,
            )
        elif tries <= 5 or tries % 20 == 0:
            log.warning("terminal write for %s still refused after %d attempts", subject, tries)
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_DELAY_S)
    raise AssertionError("unreachable")
