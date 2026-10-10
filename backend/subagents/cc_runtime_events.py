"""Canonical Claude Code event-pump composition surface."""

from __future__ import annotations

import logging

from core.turn_core import TurnIdentity
from infra.db_async import run_in_db
from infra.turn_usage_receipts import UsageReceiptError
from subagents.cc_event_context import (
    CCEventContext,
    release_orphaned_pooled_transaction,
)
from subagents.cc_event_result import (
    on_result,
)
from subagents.cc_event_simple import (
    on_final_call_usage,
    on_init,
    on_reasoning_provenance,
    on_stream_line_dropped,
    on_text_delta,
    on_thinking_delta,
    on_tool_boundary_violation,
)
from subagents.cc_event_preview import (
    ending_call,
    ending_thread,
    on_tool_input_delta,
    on_tool_input_start,
    on_tool_input_stop,
)
from subagents.cc_event_steps import on_assistant_step, on_tool_result
from subagents.cc_process_exit import _CommittedTerminalFailure
from subagents.cc_runtime_state import Broadcast, _Live, settle_resume_verdict
from subagents.cc_stream_control import TerminalWithPlanEffects

log = logging.getLogger(__name__)


async def _on_rate_limit(ctx: CCEventContext, event: dict) -> None:
    """Persist a vendor limit signal on the session's own login, never breaking the pump."""
    from connections.claude_usage import apply_rate_limit_event
    from subagents.vendor_write_admission import session_receipt

    try:
        needs_control_read = await run_in_db(
            apply_rate_limit_event, event.get("rate_limit_info"),
            receipt=session_receipt(ctx.live.session),
        )
        if needs_control_read and ctx.live.session is not None:
            ctx.live.session._corsia_usage_control_read_after_turn = True
    except Exception:
        log.exception("cc_runtime: rate-limit observation failed; event stream continues")


async def _on_final_call_usage(ctx: CCEventContext, event: dict) -> None:
    """Record one call's usage; an accounting failure never ends the event pump.

    What still escapes the ledger is a defect report that could not itself be
    written. The provider result this pump carries outranks it, so it is
    logged and the stream continues.
    """
    try:
        await on_final_call_usage(ctx, event)
    except UsageReceiptError:
        log.exception(
            "cc_runtime: usage accounting failed for %s; event stream continues",
            ctx.child_id,
        )


def _dispatch() -> dict:
    return {
        "thinking_delta": on_thinking_delta,
        "reasoning_provenance": on_reasoning_provenance,
        "final_call_usage": _on_final_call_usage,
        "text_delta": on_text_delta,
        "assistant_step": on_assistant_step,
        "stream_line_dropped": on_stream_line_dropped,
        "tool_result": ending_call(on_tool_result),
        "result": ending_thread(on_result, "turn_ended"),
        "tool_boundary_violation": ending_thread(on_tool_boundary_violation, "failed"),
        "tool_input_start": on_tool_input_start,
        "tool_input_delta": on_tool_input_delta,
        "tool_input_stop": on_tool_input_stop,
        "init": on_init,
        "rate_limit": _on_rate_limit,
    }


def _bind_turn_identity(ctx: CCEventContext, kind: str, event: dict) -> None:
    if kind in {
        "thinking_delta", "final_call_usage", "text_delta",
        "assistant_step", "tool_result", "result",
    }:
        event["_corsia_turn_identity"] = ctx.live.corsia_turn


async def _handle_locked_event(
    ctx: CCEventContext, kind: str, event: dict, handler,
) -> None:
    async with ctx.live.event_lock:
        if ctx.live.input_delivery_unproven:
            log.warning(
                "cc_runtime: dropped provider event whose input delivery was "
                "never proven for %s (%s)",
                ctx.child_id, kind,
            )
            from doceditor.live_preview import previews

            previews.end_where("failed", thread_id=ctx.child_id)
            return
        await handler(ctx, event)


def _make_on_event(live: _Live, child_id: str, bcast: Broadcast, db):
    """Build the serial event pump for one reusable CC process handle."""
    ctx = CCEventContext(live=live, child_id=child_id, bcast=bcast, db=db)
    dispatch = _dispatch()

    async def on_event(event: dict) -> object | None:
        # Any provider output means Claude accepted the resume and may have
        # read the input, so a later exit can no longer prove it unread.
        settle_resume_verdict(live, None)
        kind = event.get("kind")
        if ctx.db is None:
            release_orphaned_pooled_transaction(child_id, str(kind))
        handler = dispatch.get(kind) if isinstance(kind, str) else None
        if handler is None:
            return None
        assert isinstance(kind, str)
        _bind_turn_identity(ctx, kind, event)
        if kind == "init":
            await handler(ctx, event)
            return None
        if kind == "result":
            from subagents.cc_runtime_quiescence import quiesce_result_tools

            if not await quiesce_result_tools(child_id, live, event.get("_corsia_turn_identity")):
                return None
        try:
            await _handle_locked_event(ctx, kind, event, handler)
        except _CommittedTerminalFailure:
            raise
        except BaseException as error:
            settlement = event.pop("_corsia_terminal_settlement", None)
            if settlement is None:
                raise
            raise _CommittedTerminalFailure(settlement, error) from error
        settlement = event.pop("_corsia_terminal_settlement", None)
        effects = event.pop("_corsia_turn_end_effects", None)
        if settlement is not None and effects is not None:
            return TerminalWithPlanEffects(settlement, effects)
        return settlement

    return on_event


__all__ = ["_make_on_event"]
