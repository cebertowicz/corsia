"""One durable budget and failure reporting path for document-owned transitions."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from doceditor.live_store import LiveStore, StoreRefused

log = logging.getLogger(__name__)


class FailureBudget:
    def __init__(self, store: LiveStore, kind: str, limit: int, report: Callable[[str, str, dict], None]):
        self.store, self.kind, self.limit, self.report = store, kind, limit, report

    async def blocked_reason(self) -> str:
        failure = await asyncio.to_thread(self.store.failure, self.kind)
        return failure.message if failure is not None and failure.attempts >= self.limit else ""

    async def clear(self) -> None:
        threshold = await asyncio.to_thread(self.store.clear_failure, self.kind)
        self.report(self.kind, "", {"clears_through": threshold})

    async def record(self, error: Exception) -> str:
        """Persist the specific reason; only a spent budget reports a blocking failure."""
        log.warning("Document %s transition remains owed: %s", self.kind, error)
        kind, message = self.kind, str(error)
        provenance = {}
        try:
            failure = await asyncio.to_thread(self.store.record_failure, kind, message)
            attempts = failure.attempts
            provenance = {"failure_generation": failure.generation}
        except StoreRefused as record_error:
            kind, message = "store_refused", f"{error}; failure record refused: {record_error}"
            attempts = self.limit
        if attempts < self.limit:
            return ""
        from infra.product_events import emit_product_event
        await asyncio.to_thread(emit_product_event, "documents", kind,
            properties={"store_id": self.store.store_id, "attempts": attempts, "reason": message})
        self.report(kind, message, provenance)
        return message
