"""Per-turn context state — task-local tool identity and thread tracking."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

__all__ = [
    "_current_thread_id", "_current_turn_identity", "_current_tool_invocation_id",
    "_current_provider_call_id", "_current_checkpoint_generation_id", "tool_invocation_context",
    "running_call_ids",
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

# The install that supplied this Turn's checkpoint prefix. It is bound by the
# runtime/bridge that owns that delivery; tools must never infer it from a
# current database row because a later install could race the read.
_current_checkpoint_generation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_checkpoint_generation_id", default=None
)


@contextmanager
def tool_invocation_context(
    invocation_id: str | None, *, fork_id: str | None = None,
    provider_call_id: str | None = None, checkpoint_generation_id: str | None = None,
) -> Iterator[None]:
    """Bind the caller's exact invocation during execution, never a fallback.

    This shared scope is for bridge, native and fork-private callers.
    A fork must clear an inherited foreground id. The shell receives
    the value through registry injection, not by reading task context in an
    executor thread. This helper alone does not wire those callers or export
    an environment value.
    """
    current = invocation_id if isinstance(invocation_id, str) and invocation_id.strip() else None
    if fork_id is not None:
        current = None
    reset_handle = _current_tool_invocation_id.set(current)
    call_handle = _current_provider_call_id.set(provider_call_id if fork_id is None else None)
    generation_handle = _current_checkpoint_generation_id.set(checkpoint_generation_id)
    try:
        yield
    finally:
        _current_checkpoint_generation_id.reset(generation_handle)
        _current_provider_call_id.reset(call_handle)
        _current_tool_invocation_id.reset(reset_handle)


def running_call_ids() -> dict[str, str]:
    """The ids the executing tool call is known by, keyed ``tool_invocation_id`` and ``provider_call_id``.

    Only the ids that are set appear, so the result spreads into a ``PlanActor``.
    """
    found = {
        "tool_invocation_id": _current_tool_invocation_id.get(),
        "provider_call_id": _current_provider_call_id.get(),
    }
    return {key: value for key, value in found.items() if isinstance(value, str) and value}


_browser_thread_last_activity: dict[str, datetime] = {}

_active_thread_models: dict[str, dict] = {}

_active_warm_tasks: dict[str, asyncio.Task] = {}
