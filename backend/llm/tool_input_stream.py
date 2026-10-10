"""Where a streaming adapter hands each tool-input fragment as it arrives.

The Native turn binds a sink for the one provider call it starts; the task that
runs the call inherits it, so the adapter reaches it without another argument
through every routing layer. The sink is synchronous and must not block: it is
called on the adapter's own stream loop. Unbound, nothing is sent.
"""
from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from typing import Protocol


class ToolInputSink(Protocol):
    def on_tool_input(self, call_id: str, name: str, fragment: str) -> None:
        """One more fragment of a call's input."""

    def on_tool_abandoned(self, call_id: str) -> None:
        """The attempt that streamed this call ended without returning it, so it never runs."""


tool_input_sink: ContextVar[ToolInputSink | None] = ContextVar("tool_input_sink", default=None)


def send_tool_input(call_id: object, name: object, fragment: object) -> None:
    """Pass one fragment to the bound sink; a sink failure never breaks the stream."""
    sink = tool_input_sink.get()
    if sink is None or not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(fragment, str):
        return
    try:
        sink.on_tool_input(call_id, name, fragment)
    except Exception:
        _sink_failed()


def abandon_tool_input(call_ids: Iterable[object]) -> None:
    """Tell the bound sink these streamed calls will never run; a sink failure never breaks the stream."""
    sink = tool_input_sink.get()
    if sink is None:
        return
    for call_id in call_ids:
        if isinstance(call_id, str) and call_id:
            try:
                sink.on_tool_abandoned(call_id)
            except Exception:
                _sink_failed()


def _sink_failed() -> None:
    from loguru import logger

    logger.opt(exception=True).warning("tool input preview sink failed; the stream continues")
