"""Retry a final viewer's busy flush without holding lifetime locks during backoff."""
import asyncio

BUSY_CLOSE_DELAYS = (0.5, 1.0, 2.0, 4.0)
CLOSE_LOCK_WAIT_SECONDS = 4.0


async def flush_last_viewer(manager, subscription, artifact_id, flush, can_evict) -> None:
    async def retry() -> None:
        for delay in (*BUSY_CLOSE_DELAYS, None):
            busy = await manager.evict(artifact_id, subscription.entry, can_evict=can_evict,
                                       before_close=flush, retain_busy=delay is not None)
            if not busy or delay is None:
                return
            await asyncio.sleep(delay)

    # A disconnect cancellation must not abandon retained edits or cleanup.
    await asyncio.shield(asyncio.create_task(retry()))
