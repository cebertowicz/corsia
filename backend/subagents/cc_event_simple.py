"""Small Claude Code event handlers and capability-boundary projection."""

from __future__ import annotations

import uuid

from core.reasoning_provenance import merge_reasoning_provenance_evidence
from infra import provider_usage
from infra.context_receipts import live_context_usage_payload
from infra.turn_errors import insert_turn_error
from infra.queries import insert_message
from infra.send_broadcast import broadcast_stored_message
import subagents.cc_event_tool_boundary as tool_boundary
import subagents.cc_trust as cc_trust
from subagents.cc_event_context import (
    CCEventContext,
    frame,
)
from subagents.cc_event_context_usage import live_measured_context_usage
from subagents.cc_runtime_state import Broadcast, _Live


def record_tool_boundary_violation(
    child_id: str, bcast: Broadcast, db, metadata: dict, content: str,
    *, turn_id: str | None = None, live: _Live | None = None,
) -> str:
    """Write the boundary error; ``live`` marks it as the Turn's terminal row."""
    message_id = insert_turn_error(
        insert_message, child_id, content, metadata,
        turn_id=turn_id, msg_id=str(uuid.uuid4()), db=db,
    )
    if live is not None:
        live.tool_boundary_marker_id = message_id
    broadcast_stored_message(child_id, message_id, db=db, broadcast=bcast)
    return message_id


def _turn_id(ctx: CCEventContext) -> str | None:
    return getattr(getattr(ctx.live, "corsia_turn", None), "turn_id", None)


async def on_thinking_delta(ctx: CCEventContext, ev: dict) -> None:
    ctx.live.thinking_buf += ev.get("text", "")
    ctx.bcast(ctx.child_id, frame(ctx, ev, {
        "type": "step_reasoning", "thread_id": ctx.child_id,
        "content": ev.get("text", ""),
    }))


async def on_reasoning_provenance(ctx: CCEventContext, ev: dict) -> None:
    ctx.live.reasoning_provenance = merge_reasoning_provenance_evidence(
        ctx.live.reasoning_provenance,
        ev.get("reasoning_provenance"),
    )
    provider_message_id = ev.get("provider_message_id")
    if isinstance(provider_message_id, str) and provider_message_id:
        ctx.live.reasoning_provider_message_id = provider_message_id


async def on_final_call_usage(ctx: CCEventContext, ev: dict) -> None:
    """One model call's usage: its own ledger receipt, under its message id.

    The provider's authoritative total for a call only exists once its
    stream ends -- carried here, not on the call's own assistant record,
    which is a snapshot taken before generation finished. This fires once
    per call, so it is the ledger's sole per-call receipt point.

    ``CCStreamSession._dispatch_raw_event`` merges this call's measured
    identity (``message_id``, ``model``, corrected ``usage``) onto the event
    only once both input and output were fully observed; a dropped or
    reordered frame can leave that merge skipped, and the bare normalized
    event then carries no ``message_id``. Receipting that partial payload
    would write a wrong row (missing cache fields) or, with no usage at all,
    a ``measurement_missing`` defect indistinguishable from a call the
    provider genuinely measured nothing for. Receipt only once the merge
    proves the identity; otherwise record the call as its own distinct
    defect.
    """
    from subagents.cc_event_usage import record_call_llm_usage, report_incomplete_call_usage

    usage = ev.get("usage")
    if not ctx.live.turn_active:
        return
    ctx.live.last_call_usage = dict(usage) if isinstance(usage, dict) else None
    measured = provider_usage.from_anthropic(usage)
    if measured is not None:
        ctx.live.turn_usage = (
            ctx.live.turn_usage or provider_usage.TokenUsage()
        ) + measured
    message_id = ev.get("message_id")
    if isinstance(message_id, str) and message_id:
        await record_call_llm_usage(
            ctx.child_id, ctx.live, message_id=message_id, usage=usage,
            model=ev.get("model"),
        )
    else:
        await report_incomplete_call_usage(ctx.child_id, ctx.live)
    context_usage = live_measured_context_usage(ctx.child_id, ctx.live, usage)
    if context_usage is not None:
        ctx.bcast(ctx.child_id, frame(ctx, ev, {
            "type": "context_usage_updated",
            "thread_id": ctx.child_id,
            "context_usage": live_context_usage_payload(context_usage),
        }))


async def on_text_delta(ctx: CCEventContext, ev: dict) -> None:
    ctx.bcast(ctx.child_id, frame(ctx, ev, {
        "type": "text_delta", "thread_id": ctx.child_id,
        "content": ev.get("text", ""),
    }))


async def on_stream_line_dropped(ctx: CCEventContext, _ev: dict) -> None:
    if not ctx.live.turn_has_assistant_row:
        ctx.live.dropped_line_this_turn = True


async def on_tool_boundary_violation(ctx: CCEventContext, ev: dict) -> None:
    cc_trust.distrust(ctx.live)
    payload = ev.get("_boundary_payload")
    if isinstance(payload, tuple) and len(payload) == 2:
        metadata, content = payload
    else:
        native_tools = [
            str(name) for name in (ev.get("native_tools") or []) if str(name)
        ]
        metadata, content = tool_boundary._tool_boundary_violation_payload(
            capability=str(ev.get("capability") or "unknown"),
            native_tools=native_tools,
        )
    if not ctx.live.tool_boundary_violation_recorded:
        ctx.live.tool_boundary_violation_recorded = True
        record_tool_boundary_violation(
            ctx.child_id, ctx.bcast, ctx.db, metadata, content,
            turn_id=_turn_id(ctx), live=ctx.live,
        )
    raise RuntimeError("Claude Code capability boundary violated")


async def on_init(ctx: CCEventContext, ev: dict) -> None:
    ctx.live.initialized = True
    ctx.live.provider_reported_model = ev.get("model") or None
    violation = tool_boundary._tool_boundary_violation_from_init(ev)
    if violation is not None:
        metadata, content = violation
        await on_tool_boundary_violation(ctx, {
            "kind": "tool_boundary_violation",
            "_boundary_payload": (metadata, content),
        })
    else:
        unavailable = tool_boundary._tool_bridge_unavailable_from_init(ev)
        if unavailable is not None:
            metadata, content = unavailable
            # Not the Turn's terminal row: the process exit still surfaces.
            record_tool_boundary_violation(
                ctx.child_id, ctx.bcast, ctx.db, metadata, content,
                turn_id=_turn_id(ctx),
            )
            # Never run a Turn toolless: ending the handle here makes the
            # Turn fail visibly and the next input start a fresh bridge.
            cc_trust.distrust(ctx.live)
            raise RuntimeError("Claude Code CORSIA tool bridge is unavailable")
    ctx.bcast(ctx.child_id, {
        "type": "subagent_init", "thread_id": ctx.child_id,
        "model": ev.get("model"), "cwd": ev.get("cwd"),
        "tools_count": len(ev.get("tools") or []),
    })
