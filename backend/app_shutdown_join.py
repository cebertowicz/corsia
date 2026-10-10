"""Join owned shutdown awaitables without losing an inbound cancellation.

Shared by the lifespan shutdown orchestrator and each owner-specific
teardown module, so a cancellation delivered while one owner is being
joined defers to the end of teardown instead of skipping later owners.
"""

import asyncio


async def join_cleanup(awaitables) -> tuple[list[object], asyncio.CancelledError | None]:
    """Join owned awaitables while deferring cancellation of this cleanup."""
    join = asyncio.gather(*awaitables, return_exceptions=True)
    pending_cancel: asyncio.CancelledError | None = None
    while not join.done():
        try:
            await asyncio.shield(join)
        except asyncio.CancelledError as exc:
            pending_cancel = pending_cancel or exc
    return list(join.result()), pending_cancel


async def join_cleanup_owner(awaitable) -> tuple[object, asyncio.CancelledError | None]:
    """Join one cleanup owner and return its isolated result."""
    results, pending_cancel = await join_cleanup((awaitable,))
    return results[0], pending_cancel


def count_shutdown_owner(label: str, counter, logger) -> tuple[int, Exception | None]:
    """Check one joined owner's remaining count without skipping other owners."""
    try:
        return counter(), None
    except Exception as exc:
        logger.warning(f"shutdown: {label} owner check failed: {exc}")
        return 0, exc
