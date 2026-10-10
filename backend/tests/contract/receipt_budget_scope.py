"""Give only named call ids the short stale-receipt budget, test-side only.

``TOOL_RECEIPT_TIMEOUT_SECONDS`` (subagents/codex_tool_requests.py) is a
single process-wide budget, read fresh at each wait. A test that needs a
short budget for one deliberately-stale call must not also shorten every
other receipt wait in the same process — including a later probe that
proves the conversation still works after the stale call's failure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection


def scoped_receipt_budget(
    stale_call_ids: Collection[str], *, stale_budget_seconds: float = 0.1,
) -> Callable[..., Awaitable[dict]]:
    """Return a ``ToolRequests._usage`` replacement scoping the short budget.

    Only an RPC frame whose ``callId``/``id`` is in ``stale_call_ids`` gets
    ``stale_budget_seconds``; every other call keeps whatever
    ``TOOL_RECEIPT_TIMEOUT_SECONDS`` already is at call time.
    """
    from subagents import codex_tool_requests

    frozen_stale_ids = frozenset(stale_call_ids)
    original_usage = codex_tool_requests.ToolRequests._usage

    async def _scoped_usage(self, frame: dict) -> dict:
        params = frame.get("params") or {}
        call_id = params.get("callId") or params.get("id")
        if call_id not in frozen_stale_ids:
            return await original_usage(self, frame)
        previous = codex_tool_requests.TOOL_RECEIPT_TIMEOUT_SECONDS
        codex_tool_requests.TOOL_RECEIPT_TIMEOUT_SECONDS = stale_budget_seconds
        try:
            return await original_usage(self, frame)
        finally:
            codex_tool_requests.TOOL_RECEIPT_TIMEOUT_SECONDS = previous

    return _scoped_usage


def install_scoped_receipt_budget(
    stale_call_ids: Collection[str], *, stale_budget_seconds: float = 0.1,
) -> None:
    """Install the scoped budget for the rest of the current process's life.

    Only for a disposable, single-purpose subprocess (a sitecustomize.py
    bootstrap): it replaces the class method with no restore, since that
    process never outlives the one test that started it.
    """
    from subagents import codex_tool_requests

    codex_tool_requests.ToolRequests._usage = scoped_receipt_budget(
        stale_call_ids, stale_budget_seconds=stale_budget_seconds,
    )
