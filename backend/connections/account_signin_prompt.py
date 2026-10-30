"""A vendor sign-in's prompt (a device code): read from output that is otherwise dropped, and waited on."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from connections.account_profiles import profile_for

if TYPE_CHECKING:
    from connections.account_signin import SignIn

# Only this much of a sign-in's output is ever held, to find a prompt split across reads.
_WINDOW = 4096


async def discard(stream: asyncio.StreamReader, operation: SignIn) -> None:
    """Drop the vendor's output, keeping only the prompt its profile reads from it."""
    reader = profile_for(operation.provider).signin_prompt
    window = ""
    while chunk := await stream.read(65536):
        if reader is None or operation.prompt is not None:
            continue
        window = (window + chunk.decode("utf-8", "replace"))[-_WINDOW:]
        if (prompt := reader(window)) is not None:
            operation.prompt = prompt
            # A later complete prompt supersedes a prior bounded-wait diagnosis.
            operation.cause = None
            operation.prompted.set()


async def wait(operation: SignIn, timeout: float) -> None:
    """Return once the vendor printed its prompt, the sign-in finished, or ``timeout`` passed."""
    waits = [asyncio.ensure_future(operation.prompted.wait())]
    if operation.task is not None:
        waits.append(asyncio.ensure_future(asyncio.shield(operation.task)))
    await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    for waiting in waits:
        waiting.cancel()
    # The sign-in's own outcome is read from its record, never from these waits.
    await asyncio.gather(*waits, return_exceptions=True)
    if operation.status == "waiting" and operation.prompt is None:
        operation.cause = "prompt_unrecognized"
