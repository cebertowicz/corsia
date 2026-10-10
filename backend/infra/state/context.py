"""Per-turn context state — task-local tool identity and thread tracking."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

__all__ = [
    "_current_thread_id", "_current_turn_identity", "_current_tool_invocation_id",
    "_current_provider_call_id", "tool_invocation_context",
    "_browser_thread_last_activity", "_active_thread_models",
    "_active_warm_tasks",
]

_current_thread_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_thread_id", default=None
)

# The value is runtime-validated as ``core.turn_core.TurnIdentity`` at the
# tool-registry boundary.  Keeping this leaf state module independent of
# ``core`` avoids an infra -> core -> infra import cycle.
_current_turn_identity: contextvars.ContextVar[object | None] = (
    contextvars.ContextVar("_current_turn_identity", default=None)
)

_current_tool_invocation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_tool_invocation_id", default=None
)

# The provider's own id for the call being executed: what a streamed preview of
# its input was keyed by, so the write it makes can take that preview over.
_current_provider_call_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_provider_call_id", default=None
)


@contextmanager
def tool_invocation_context(
    invocation_id: str | None, *, checkpoint_id: str | None = None,
    provider_call_id: str | None = None,
) -> Iterator[None]:
    """Bind the caller's exact invocation during execution, never a fallback.

    This shared scope is for bridge, native and checkpoint-private callers.
    A checkpoint must clear an inherited foreground id. The shell receives
    the value through registry injection, not by reading task context in an
    executor thread. This helper alone does not wire those callers or export
    an environment value.
    """
    current = invocation_id if isinstance(invocation_id, str) and invocation_id.strip() else None
    if checkpoint_id is not None:
        current = None
    reset_handle = _current_tool_invocation_id.set(current)
    call_handle = _current_provider_call_id.set(provider_call_id if checkpoint_id is None else None)
    try:
        yield
    finally:
        _current_provider_call_id.reset(call_handle)
        _current_tool_invocation_id.reset(reset_handle)


_browser_thread_last_activity: dict[str, datetime] = {}

_active_thread_models: dict[str, dict] = {}

_active_warm_tasks: dict[str, asyncio.Task] = {}
