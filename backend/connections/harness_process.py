"""One-shot harness CLI probes never outlive the call that started them.

A probe that times out or is cancelled is killed and reaped here. The reap is
bounded: on Python 3.11 ``communicate()`` also waits for the pipes to close,
and a descendant the CLI started can hold them open indefinitely (#2792).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

log = logging.getLogger(__name__)
REAP_SECONDS = 5.0


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    try:
        async with asyncio.timeout(REAP_SECONDS):
            await process.communicate()
    except TimeoutError:
        log.warning("harness probe %s not reaped within %ss; its pipes stay open",
                    getattr(process, "pid", None), REAP_SECONDS)


@contextlib.asynccontextmanager
async def killed_on_exit(process: asyncio.subprocess.Process) -> AsyncIterator[None]:
    """Kill and reap ``process`` if the body times out, fails or is cancelled."""
    try:
        yield
    except BaseException:
        await _kill_and_reap(process)
        raise
