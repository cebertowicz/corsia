"""Claude Code's streamed tool input, fed to the document preview route.

Claude Code streams a tool call's input by content-block index. The index is
bound to the call when its block opens and released when the block closes, so a
later block reusing the index cannot write into a call that already finished.
The handlers only parse and broadcast: no database, no await of other work.
"""
from __future__ import annotations

from doceditor.live_preview import preview_tool, previews
from subagents.cc_event_context import CCEventContext


async def on_tool_input_start(ctx: CCEventContext, ev: dict) -> None:
    tool = preview_tool(ev.get("name"))
    call_id, index = ev.get("call_id"), ev.get("index")
    if tool is None or not isinstance(call_id, str) or not isinstance(index, int):
        return
    registry = ctx.live.tool_registry

    def workspace_of(name: str):
        bound = registry.get(name) if registry is not None else None
        return getattr(bound, "workspace", None)

    turn = ctx.live.corsia_turn
    ctx.live.preview_blocks[index] = call_id
    previews.start(call_id, thread_id=ctx.child_id, turn_id=turn.turn_id if turn else None, tool=tool,
                   workspace=workspace_of(tool), workspace_for=workspace_of)


async def on_tool_input_delta(ctx: CCEventContext, ev: dict) -> None:
    previews.update(ctx.live.preview_blocks.get(ev.get("index")), ev.get("text"))


async def on_tool_input_stop(ctx: CCEventContext, ev: dict) -> None:
    ctx.live.preview_blocks.pop(ev.get("index"), None)


def ending_call(handler):
    """A tool result ends its call's preview: settled, or failed when the result is an error."""
    async def wrapped(ctx: CCEventContext, ev: dict) -> None:
        previews.end(ev.get("tool_use_id"), "failed" if ev.get("is_error") else "settled")
        return await handler(ctx, ev)
    return wrapped


def ending_thread(handler, outcome: str):
    """A Turn's result or a boundary violation ends every preview this thread still has open."""
    async def wrapped(ctx: CCEventContext, ev: dict) -> None:
        ctx.live.preview_blocks.clear()
        previews.end_where(outcome, thread_id=ctx.child_id)
        return await handler(ctx, ev)
    return wrapped
