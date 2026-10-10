"""Observe Claude's pipe-drain confirmation without losing cancellation evidence."""
from __future__ import annotations

import asyncio


async def confirm_frame_drain(writer, on_accepted):
    drain_task = asyncio.create_task(writer.drain())
    try:
        await asyncio.shield(drain_task)
    except asyncio.CancelledError:
        if drain_task.done():
            # Only a drain that won the cancellation race confirms that
            # the already-observed write reached the provider pipe.
            drain_succeeded = not drain_task.cancelled() and drain_task.exception() is None
            if drain_succeeded and on_accepted is not None:
                on_accepted()
        else:
            drain_task.cancel()
            drain_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        raise
    if on_accepted is not None:
        on_accepted()
