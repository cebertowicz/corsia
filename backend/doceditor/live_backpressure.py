"""Admit a lane write only while its owed publications are bounded and drainable."""
from __future__ import annotations

import asyncio

OWED_ROW_BOUND = 16
"""Owed rows at which the next write waits. Measured healthy peak: 9 rows, over 24
runs of 40 alternating user and agent writes on 100 KB and 1 MB documents."""
DRAIN_WAIT_S = 15.0
"""Longest a write waits for one owed row to drain. Slowest measured healthy
registration of one row: 1.48 s, on the 1 MB document."""
SLOW_DRAIN_MESSAGE = "Earlier changes are still being saved on the server. Try again in a moment."


class PublicationGate:
    """Count owed rows when a write would start, under the lane's single flight.

    A waiting write is still queued: it is not started or shielded and holds
    no store transaction, commit lock or application connection, so Stop
    takes the queued path. Only admission, a ``publication_failed`` refusal or
    the item leaving the queue ends the wait.
    """

    def __init__(self, publisher):
        self.publisher = publisher
        self.waiting = 0

    async def admit(self, left: asyncio.Future, cancel_event: asyncio.Event | None) -> bool:
        """True to start the write; False when it left the queue while waiting."""
        from doceditor.document_save import DocumentSaveError
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRAIN_WAIT_S
        while True:
            # Taken before reading, so a drain between the read and the wait still wakes it.
            changed = self.publisher.changed
            self.publisher.require_running()
            reason = await self.publisher.budget.blocked_reason()
            if reason:
                raise DocumentSaveError("publication_failed", reason)
            count = await asyncio.to_thread(self.publisher.store.owed_count)
            # Stop can land while either read is off-loop, even below the bound.
            if left.done() or (cancel_event is not None and cancel_event.is_set()):
                return False
            self.publisher.require_running()
            if count < OWED_ROW_BOUND:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DocumentSaveError("publication_failed", SLOW_DRAIN_MESSAGE)
            if not await self._wait(changed, left, cancel_event, remaining):
                return False

    async def _wait(self, changed: asyncio.Event, left: asyncio.Future,
                    cancel_event: asyncio.Event | None, timeout: float) -> bool:
        wakes = {asyncio.ensure_future(changed.wait())}
        if cancel_event is not None:
            wakes.add(asyncio.ensure_future(cancel_event.wait()))
        self.waiting += 1
        try:
            await asyncio.wait({left, *wakes}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.waiting -= 1
            for wake in wakes:
                wake.cancel()
            await asyncio.gather(*wakes, return_exceptions=True)
        return not left.done() and not (cancel_event is not None and cancel_event.is_set())
