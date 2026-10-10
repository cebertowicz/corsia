"""Bound automatic recovery; retain the owed transition until explicit Retry."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)
RECOVERY_ATTEMPTS = 3


class LaneRecoveryRetry:
    def __init__(self, lane):
        self.lane = lane
        self.timer: asyncio.TimerHandle | None = None
        self.delay = 1.0
        self.closed = False
        self.blocked_reason = ""
        from doceditor.live_failure_budget import FailureBudget
        self.budget = FailureBudget(lane.live.store, "store_refused", RECOVERY_ATTEMPTS,
                                    lambda kind, message, provenance: self._block(message, provenance))

    def notify(self) -> None:
        """A started operation never waits for this retry or its backoff."""
        if self.closed or self.blocked_reason or self.timer is not None:
            return
        self.timer = asyncio.get_running_loop().call_later(self.delay, self._enqueue)
        self.delay = min(self.delay * 2, 30.0)

    def _enqueue(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        self.timer = None
        if self.closed or self.blocked_reason:
            return
        job = self.lane._enqueue(self._recover, recovery=True)
        job.future.add_done_callback(self._settled)

    def _block(self, message: str, provenance: dict) -> None:
        self.blocked_reason = message
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.lane.live.failure_changed("store_refused", message, provenance)

    async def exhausted(self) -> bool:
        failure = await asyncio.to_thread(self.budget.store.failure, self.budget.kind)
        if failure is not None and failure.attempts >= self.budget.limit:
            self._block(failure.message, {"failure_generation": failure.generation})
        return bool(self.blocked_reason)

    async def _recover(self) -> None:
        from doceditor.live_recovery import recover
        if await self.exhausted():
            return
        try:
            await recover(self.lane)
            await self.budget.clear()
        except Exception as error:
            await self.record_failure(error)
            return
        self.delay = 1.0

    async def record_failure(self, error: Exception) -> None:
        """Only real store failures spend recovery's shared durable budget."""
        reason = await self.budget.record(error)
        if not reason:
            self.notify()

    def cleared(self, clears_through: int) -> None:
        """A landed write cleared the durable failure it disproved."""
        self.blocked_reason = ""
        self.delay = 1.0
        self.lane.live.failure_changed("store_refused", "", {"clears_through": clears_through})

    def _settled(self, future) -> None:
        if not future.cancelled() and future.exception() is not None:
            log.error("Document recovery could not inspect its failure budget: %s", future.exception())
            self._block(str(future.exception()), {})

    async def retry_blocked(self) -> None:
        """Only explicit Retry resets the existing metadata budget."""
        await self.budget.clear()
        self.blocked_reason = ""
        self.delay = 1.0
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        job = self.lane._enqueue(self._recover, recovery=True)
        await asyncio.shield(job.future)

    def close(self) -> None:
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
