"""Claude Code Assistant-Step and tool-result event handlers."""

from __future__ import annotations

import json
from functools import partial
from typing import cast

from infra.database import get_db
from infra.db_async import run_in_db_unless_owned
from core.reasoning_provenance import merge_reasoning_provenance_evidence
from core.tool_result_binding import bind_tool_result_echo, read_bound_tool_result
from registry.registration_writer import RegistrationOrigin
from subagents.assistant_step_display import assistant_step_payload
import subagents.cc_event_tool_boundary as tool_boundary
from tools.tool_display import SettledResult, tool_done_frame, tool_start_frame
from subagents.cc_event_context import (
    CCEventContext,
    event_identity,
    frame,
)
from subagents.cc_event_simple import on_tool_boundary_violation
from subagents.cc_event_tool_settlement import (
    emit_finalized_step,
    observe_tool_result,
    settle_missing_tool_results,
    settle_undispatched_tool_calls,
    tool_hint,
)
from subagents.cc_event_usage import provider_reported_model_id
from subagents.cc_runtime_state import _LIVE
from subagents.child_thread import (
    load_assistant_step_display,
    persist_assistant_step,
)


def _is_malformed_tool_use(tool: object) -> bool:
    if not isinstance(tool, dict):
        return True
    return (
        bool(tool.get("malformed"))
        or not isinstance(tool.get("id"), str)
        or not tool.get("id")
        or not isinstance(tool.get("name"), str)
        or not isinstance(tool.get("input"), dict)
    )


def _non_corsia_tool_names(raw_uses: list) -> list[str]:
    return sorted({
        str(tool.get("name") or "<unnamed>")
        if isinstance(tool, dict) else "<malformed>"
        for tool in raw_uses
        if not isinstance(tool, dict)
        or not tool_boundary._is_corsia_mcp_tool(tool.get("name"))
    })


def _project_tool_uses(raw_uses: list[dict]) -> list[dict]:
    uses: list[dict] = []
    for tool in raw_uses:
        tool_id = tool.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            raise RuntimeError("CC Assistant Step has a tool call without an id")
        name = tool.get("name")
        arguments = tool.get("input") or {}
        uses.append({
            "id": tool_id, "name": name, "arguments": arguments,
            "hint": tool_hint(name or "", arguments),
        })
    return uses


def _reasoning_provenance(ctx: CCEventContext, ev: dict) -> dict | None:
    provenance = merge_reasoning_provenance_evidence(
        ctx.live.reasoning_provenance,
        ev.get("reasoning_provenance"),
    )
    if (
        provenance is not None
        and not provenance.get("source_effective_model")
        and isinstance(ctx.live.provider_reported_model, str)
        and ctx.live.provider_reported_model.strip()
    ):
        provenance["source_effective_model"] = ctx.live.provider_reported_model.strip()
    return provenance or None


def _persist_and_load_assistant_step(
    child_id: str, *, turn_id: str | None, content: str, tool_uses: list[dict],
    reasoning: str | None, reasoning_provenance: dict | None, model: str | None,
    expected_model: str | None, provider_message_id: str | None, db=None,
) -> tuple[str, dict]:
    """Persist and project one step on one connection before its broadcast."""
    connection = db if db is not None else get_db()
    try:
        message_id = persist_assistant_step(
            child_id, turn_id=turn_id, content=content, tool_uses=tool_uses,
            reasoning=reasoning, reasoning_provenance=reasoning_provenance,
            db=connection, model=model, expected_model=expected_model,
            provider_message_id=provider_message_id,
        )
        return message_id, load_assistant_step_display(
            child_id, message_id, db=connection,
        )
    finally:
        if db is None:
            connection.close()


def _broadcast_tool_starts(
    ctx: CCEventContext, ev: dict, uses: list[dict], step_id: str,
) -> None:
    for tool in uses:
        tool_id = tool["id"]
        name = tool.get("name")
        ctx.live.tool_names[tool_id] = name
        ctx.live.tool_step_ids[tool_id] = step_id
        args_cap = 20000 if name in (
            "Edit", "MultiEdit", "NotebookEdit", "Write"
        ) else 400
        ctx.bcast(ctx.child_id, frame(ctx, ev, tool_start_frame(
            ctx.child_id, tool_id, name, tool["hint"], tool["arguments"],
            json.dumps(tool["arguments"])[:args_cap],
        )))
    if uses:
        ctx.live.tool_steps_ready.set()


async def on_assistant_step(ctx: CCEventContext, ev: dict) -> None:
    """Persist one Assistant Step and take ownership of its tool calls.

    A malformed call ends the turn at the capability boundary. A well-formed
    call naming a tool CORSIA does not serve is answered at once; the rest
    wait for the bridge.
    """
    raw_uses = ev.get("tool_uses") or []
    if any(_is_malformed_tool_use(tool) for tool in raw_uses):
        await on_tool_boundary_violation(ctx, {
            "kind": "tool_boundary_violation",
            "capability": "malformed_tool",
            "native_tools": _non_corsia_tool_names(raw_uses),
        })
    admitted = tool_boundary.admitted_native_tools(ctx.live)
    if admitted:
        foreign = tool_boundary.unadmitted_tool_names(raw_uses, admitted)
        if foreign:
            await on_tool_boundary_violation(ctx, {
                "kind": "tool_boundary_violation",
                "capability": "native_tool", "native_tools": foreign,
            })
        raw_uses = []
    identity = event_identity(ctx, ev)
    uses = _project_tool_uses(raw_uses)
    text = ev.get("text", "")
    reasoning = ev.get("reasoning") or ctx.live.thinking_buf or None
    provenance = _reasoning_provenance(ctx, ev)
    if admitted and not (text or reasoning or provenance):
        return
    message_id, step = await run_in_db_unless_owned(ctx.db, partial(
        _persist_and_load_assistant_step,
        ctx.child_id, turn_id=identity.turn_id, content=text,
        tool_uses=uses, reasoning=reasoning,
        reasoning_provenance=provenance, db=ctx.db,
        model=provider_reported_model_id(ctx.live),
        expected_model=ctx.live.expected_reported_model,
        provider_message_id=ev.get("provider_message_id"),
    ))
    ctx.bcast(ctx.child_id, frame(ctx, ev, {
        "type": "step_text", "thread_id": ctx.child_id,
        **assistant_step_payload(step, message_id=message_id),
        "finalized_tool_ids": [],
    }))
    _broadcast_tool_starts(ctx, ev, uses, message_id)
    if uses:
        ctx.live.tools_settled.clear()
        # Only a call to a served CORSIA tool can reach the bridge; decide from
        # the name Claude Code declared, before anything resolves it. The bridge
        # lists the frozen catalog of the thread's *current* handle, so a
        # superseded handle cannot vouch for what its process was served, so the
        # prefix alone decides there: in doubt a call stays owed to the bridge.
        served = (
            ctx.live.tool_definitions if _LIVE.get(ctx.child_id) is ctx.live else None
        )
        await settle_undispatched_tool_calls(
            live=ctx.live, child_id=ctx.child_id, identity=identity,
            step_id=message_id, bcast=ctx.bcast, db=ctx.db,
            tool_ids=[
                tool["id"] for tool in uses
                if not tool_boundary._is_served_corsia_tool(tool["name"], served)
            ],
        )
        if ctx.live.interrupt_requested:
            await settle_missing_tool_results(
                live=ctx.live, child_id=ctx.child_id,
                identity=event_identity(ctx, ev), bcast=ctx.bcast, db=ctx.db,
            )
    ctx.live.last_text = text or ctx.live.last_text
    ctx.live.last_assistant_message_id = message_id
    ctx.live.thinking_buf = ""
    ctx.live.reasoning_provenance = None
    ctx.live.reasoning_provider_message_id = None
    ctx.live.turn_has_assistant_row = True


async def _recover_result_owner(ctx: CCEventContext, tool_id: str):
    step_id = ctx.live.tool_step_ids.get(tool_id)
    name = ctx.live.tool_names.get(tool_id)
    if step_id is not None:
        return step_id, name
    candidate = ctx.live.last_assistant_message_id
    if candidate is not None:
        projected = await run_in_db_unless_owned(ctx.db, partial(
            load_assistant_step_display, ctx.child_id, candidate, db=ctx.db,
        ))
        calls = (projected.get("metadata") or {}).get("tool_calls") or []
        matching = [
            call for call in calls
            if isinstance(call, dict) and call.get("id") == tool_id
        ]
        if len(matching) == 1:
            return candidate, name or matching[0].get("name")
    settled = read_bound_tool_result(
        thread_id=ctx.child_id,
        provider_tool_call_id=tool_id,
        db=ctx.db,
    )
    if settled is not None:
        return settled.assistant_step_id, name or settled.name
    return None, name


async def on_tool_result(ctx: CCEventContext, ev: dict) -> None:
    if tool_boundary.admitted_native_tools(ctx.live):
        return  # a confined launch's results are its provider's own reads
    tool_id = cast(str, ev.get("tool_use_id"))
    step_id, name = await _recover_result_owner(ctx, tool_id)
    if step_id is None:
        raise RuntimeError(f"CC tool result {tool_id!r} has no Assistant Step")
    already_settled = tool_id not in ctx.live.tool_step_ids
    canonical = await run_in_db_unless_owned(ctx.db, partial(
        bind_tool_result_echo,
        thread_id=ctx.child_id, provider_tool_call_id=tool_id,
        assistant_step_id=step_id, echoed_content=ev.get("content", ""),
        db=ctx.db,
    ))
    if already_settled:
        return
    ctx.live.tool_names.pop(tool_id, None)
    ctx.live.tool_step_ids.pop(tool_id, None)
    if not ctx.live.tool_step_ids:
        ctx.live.tools_settled.set()
    metadata = {
        "tool_call_id": tool_id, "assistant_step_id": step_id,
        "name": canonical.name,
    }
    if canonical.is_error:
        metadata["is_error"] = True
    identity = event_identity(ctx, ev)
    await observe_tool_result(
        ctx.child_id,
        canonical.content,
        metadata,
        ctx.db,
        origin=RegistrationOrigin(ctx.child_id, identity.turn_id, "cc_observer"),
    )
    ctx.bcast(ctx.child_id, frame(ctx, ev, tool_done_frame(
        ctx.child_id, tool_id, canonical.name,
        SettledResult.from_result(canonical), (canonical.content or "")[:8000],
    )))
    if step_id not in ctx.live.tool_step_ids.values():
        await emit_finalized_step(
            child_id=ctx.child_id, step_id=step_id,
            identity=event_identity(ctx, ev), bcast=ctx.bcast, db=ctx.db,
        )
