"""Waiting on a held vendor login: it exits, or the vendor's status already says signed in."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

# How often a held login's vendor status is read, for a vendor whose profile lets signed in end the login.
STATUS_POLL_SECONDS = 3.0
# A login that stored its credentials is given this long to exit by itself before it is stopped.
LINGER_SECONDS = 3.0


async def login_done(
    process: asyncio.subprocess.Process, poll_status: Callable[[], Awaitable[bool | None]] | None = None,
) -> None:
    """Return when the vendor login exits, or, given ``poll_status``, when status reads signed in.

    A login that has stored its credentials but not exited is complete: the person did sign in.
    Status must read signed in twice running, so a login in the middle of storing them is not
    caught, and the login then gets ``LINGER_SECONDS`` to exit by itself.
    """
    if poll_status is None:
        await process.wait()
        return
    exited = asyncio.ensure_future(process.wait())
    try:
        seen = 0
        while not exited.done():
            await asyncio.wait({exited}, timeout=STATUS_POLL_SECONDS)
            if exited.done():
                return
            seen = seen + 1 if await poll_status() is True else 0
            if seen >= 2:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(LINGER_SECONDS):
                        await exited
                return
    finally:
        exited.cancel()


async def stop_login(process: asyncio.subprocess.Process) -> None:
    """Stop a vendor login that signed in but has not exited."""
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()
