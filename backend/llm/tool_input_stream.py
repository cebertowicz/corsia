"""Where a streaming adapter hands each tool-input fragment as it arrives.

The Native turn binds a sink for the one provider call it starts; the task that
runs the call inherits it, so the adapter reaches it without another argument
through every routing layer. The sink is synchronous and must not block: it is
called on the adapter's own stream loop. Unbound, nothing is sent.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from llm.router import LLMResponse


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


def forward_tool_input(call: dict) -> None:
    """Send what an accumulating call's ``arguments`` gained since its last send.

    For adapters that assemble a call in a dict (``id``, ``name``, ``arguments``):
    nothing is sent until the call's id and name are both known, then the whole
    backlog goes at once; ``sent`` records how much has gone.
    """
    arguments, sent = call.get("arguments") or "", call.get("sent", 0)
    if call.get("id") and call.get("name") and isinstance(arguments, str) and len(arguments) > sent:
        send_tool_input(call["id"], call["name"], arguments[sent:])
        call["sent"] = len(arguments)


def forwarded_ids(calls: dict[int, dict]) -> list[str]:
    """The ids of the assembled calls ``forward_tool_input`` has sent input for."""
    return [call["id"] for call in calls.values() if call.get("sent")]


async def abandoning_unreturned(attempt: Awaitable[LLMResponse], streamed: Callable[[], Iterable[object]]) -> LLMResponse:
    """Await one provider attempt; abandon every streamed call it failed, was cut off before, or did not return.

    ``streamed`` is read after the attempt, so it sees every call the attempt streamed.
    """
    try:
        response = await attempt
    except Exception:
        abandon_tool_input(streamed())
        raise
    returned = {call.id for call in response.tool_calls}
    abandon_tool_input(call_id for call_id in streamed() if call_id not in returned)
    return response


def _sink_failed() -> None:
    from loguru import logger

    logger.opt(exception=True).warning("tool input preview sink failed; the stream continues")
