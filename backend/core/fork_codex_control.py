"""Bound completed Codex items through the same kind contract as other adapters."""

from __future__ import annotations

import asyncio


class ForkItemLimitReached(RuntimeError):
    """The fork exhausted its tool-call budget, or a tool identity was missing."""

    def __init__(self, message: str, *, identity_missing: bool = False):
        super().__init__(message)
        self.identity_missing = identity_missing


class CompletedItemLimit:
    def __init__(self, kind, request):
        self.limit = kind.step_limit("codex", request)
        self.kind_name = kind.name
        self.seen: set[str] = set()
        self.reached = asyncio.Event()
        self.identity_missing = False

    async def observe(self, session, frame):
        if self.limit is None or frame.get("method") != "item/completed":
            return
        params = frame.get("params") or {}
        if not session._is_active_provider_boundary(params.get("threadId"), params.get("turnId")):
            return
        item = params.get("item") or {}
        # Reasoning and agent messages consume no tools and must not starve
        # an ordinary review's list/read/comment/submit journey.
        if item.get("type") not in {"dynamicToolCall", "mcpToolCall"}:
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            self.identity_missing = True
            session.revoke_dynamic_tool_admission()
            self.reached.set()
            session._request_terminate()
            return
        self.seen.add(item_id)
        if len(self.seen) >= self.limit:
            session.revoke_dynamic_tool_admission()
            self.reached.set()
            session._request_terminate()

    def failure(self):
        if self.identity_missing:
            return ForkItemLimitReached("fork_completed_item_missing_identity", identity_missing=True)
        return ForkItemLimitReached(
            f"fork_step_limit_reached: {self.kind_name} completed {self.limit} Codex tool calls"
        )


async def run_session(session, capture, item_limit, cancel_event):
    from core.fork_types import ProviderForkRejected
    from core.fork_failures import ForkBinaryResolutionError

    async def run():
        if cancel_event.is_set():
            raise asyncio.CancelledError
        try:
            accepted = await session.start()
        except (FileNotFoundError, PermissionError) as error:
            if not error.filename or error.filename != getattr(session, "binary", None):
                raise
            raise ForkBinaryResolutionError("Codex harness executable is unavailable") from error
        if not accepted:
            raise ProviderForkRejected(f"Codex {item_limit.kind_name} was not accepted")
        await capture.terminal

    operation = asyncio.create_task(run())
    stopped = asyncio.create_task(cancel_event.wait())
    limited = asyncio.create_task(item_limit.reached.wait())
    try:
        await asyncio.wait({operation, stopped, limited}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if item_limit.reached.is_set():
            raise item_limit.failure()
        await operation
    finally:
        for task in (operation, stopped, limited):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, stopped, limited, return_exceptions=True)
        await session.terminate()
    return capture.result()
