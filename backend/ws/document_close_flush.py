"""Bound the final viewer's retry of a temporarily busy file write."""
import asyncio
import logging

from doceditor.live_lane import Operation
from doceditor.live_store import StoreRefused

log = logging.getLogger(__name__)
BUSY_CLOSE_DELAYS = (0.5, 1.0, 2.0, 4.0)


async def flush_last_viewer(subscription, artifact_id: str) -> None:
    # Holds owner.lock and comment_mutex during retries (7.5s of delays), delaying reopen or comments.
    for delay in (*BUSY_CLOSE_DELAYS, None):
        try:
            result = await subscription.entry.lane.submit(Operation("flush", actor=subscription.actor))
            result.require_landed()
            return
        except StoreRefused as error:
            if getattr(error, "code", None) != "history_lock_busy":
                raise
            if delay is None:
                log.warning("Final document flush remained busy for artifact %s; edits remain in the live store", artifact_id)
                return
            await asyncio.sleep(delay)
