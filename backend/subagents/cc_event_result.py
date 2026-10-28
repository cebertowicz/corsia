"""Claude Code result-event terminal ownership and postprocessing."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from automation.turn_end_effects import TurnEndEffects, record_external_turn_end
from connections.auth_failure import auth_failure_kind
from core.turn_core import TurnIdentity, TurnTerminalResult
from infra.db_async import run_in_db_unless_owned
from infra.queries import insert_message
from infra.send_broadcast import broadcast_stored_message
from llm.error_cause import cause_fields
import subagents.cc_event_tool_boundary as tool_boundary
import subagents.cc_trust as cc_trust
from subagents.cc_event_context import CCEventContext, claim_terminal, event_identity, frame
from subagents.cc_event_context_usage import (
    consume_and_attach_terminal_context_usage,
)
from subagents.cc_event_steps import on_assistant_step
from subagents.cc_event_tool_settlement import settle_missing_tool_results
from subagents.cc_event_usage import record_terminal_llm_usage
from subagents.cc_runtime_errors import (
    is_empty_text_block_rejection,
    reconcile_expired_login,
)
from subagents.terminal_marker_writer import insert_terminal_marker

log = logging.getLogger("subagents.cc_runtime_events")


async def _flush_pending_reasoning(
    ctx: CCEventContext, identity: TurnIdentity,
) -> None:
    blocks = (
        ctx.live.reasoning_provenance.get("native_blocks", [])
        if isinstance(ctx.live.reasoning_provenance, dict)
        else []
    )
    has_redacted = any(
        isinstance(block, dict)
        and block.get("type") == "redacted_thinking"
        and isinstance(block.get("data"), str)
        and block["data"]
        for block in blocks
    )
    if has_redacted:
        await on_assistant_step(ctx, {
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [],
            "reasoning_provenance": ctx.live.reasoning_provenance,
            "provider_message_id": ctx.live.reasoning_provider_message_id,
            "_corsia_turn_identity": identity,
        })
    ctx.live.thinking_buf = ""
    ctx.live.reasoning_provenance = None
    ctx.live.reasoning_provider_message_id = None


def _result_metadata(
    ctx: CCEventContext,
    ev: dict,
    *,
    identity: TurnIdentity,
    apply_now_interrupt_owns_result: bool,
) -> tuple[bool, dict]:
    is_error = bool(ev.get("is_error"))
    metadata: dict = {
        "cc": True,
        "type": "result",
        "is_error": is_error,
        "turn_id": identity.turn_id,
    }
    if ev.get("stop_reason"):
        metadata["stop_reason"] = ev["stop_reason"]
    if apply_now_interrupt_owns_result:
        if not ev.get("stop_reason"):
            metadata["stop_reason"] = "interrupted"
        if metadata.get("stop_reason") == "interrupted":
            is_error = False
            metadata.update({
                "is_error": False,
                "interrupted": True,
                "cause": "superseded_by_input",
            })
    elif ctx.live.interrupt_requested and metadata.get("stop_reason") != "end_turn":
        is_error = True
        metadata["is_error"] = True
        if not ev.get("stop_reason"):
            metadata["stop_reason"] = "interrupted"
    if is_error and (cause := auth_failure_kind("claude-code", ev.get("result"))):
        metadata["cause"] = cause
    if tool_boundary._is_context_limit_result(ev):
        cc_trust.distrust(ctx.live)
        metadata.update({
            "cause": "context_window_exceeded",
            "repair_owner": "ctx-stability",
        })
    if is_error and metadata.get("stop_reason") != "interrupted":
        metadata.update(cause_fields(
            "claude-code", metadata.get("cause"), ev.get("result"), None, ctx.live.model,
        ))
    return is_error, metadata


async def _retire_poisoned_session(ctx: CCEventContext, metadata: dict) -> None:
    """Stop resuming the provider session whose transcript the API rejects.

    Only the exact registered session is retired; the next input is then
    prepared as an exact replay of CORSIA's own (empty-safe) history.
    """
    from infra.db_async import run_in_db
    from subagents.cc_resume_transition import retire_rejected_provider_session

    cc_trust.distrust(ctx.live)
    metadata.update(cause_fields("claude-code", "provider_transcript_poisoned"))
    session = ctx.live.session
    session_id = (
        getattr(session, "resume_session_id", None)
        or getattr(session, "session_id", None)
    )
    try:
        await run_in_db(
            retire_rejected_provider_session, ctx.child_id, session_id, None,
        )
    except Exception:
        # The Turn must still end visibly; a failed retirement leaves the
        # session registered, so the next rejection retires it again.
        log.exception(
            "cc_runtime: %s failed to retire its poisoned provider session",
            ctx.child_id,
        )


def _marker_content(
    ctx: CCEventContext,
    ev: dict,
    *,
    is_error: bool,
    metadata: dict,
) -> str:
    suppress = (
        ctx.live.turn_has_assistant_row
        and not is_error
        and not ctx.live.dropped_line_this_turn
    )
    return "" if suppress else (ev.get("result") or "")


def _result_outcome(is_error: bool, metadata: dict) -> str:
    if is_error:
        return "error"
    return "completed"


def _clear_terminal_live_state(ctx: CCEventContext) -> None:
    ctx.live.input_delivery_unproven = False
    ctx.live.turn_has_assistant_row = False
    ctx.live.dropped_line_this_turn = False
    ctx.live.turn_active = False
    ctx.live.interrupt_requested = False
    ctx.live.apply_now_interrupt_turn = None


async def _insert_result_marker(
    ctx: CCEventContext, content: str, metadata: dict,
) -> tuple[str | None, str]:
    candidate = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        await run_in_db_unless_owned(
            ctx.db,
            insert_terminal_marker,
            insert_message, ctx.child_id, content, metadata,
            msg_id=candidate, created_at=created_at, db=ctx.db,
        )
    except Exception as exc:
        log.error("cc_runtime: result marker failed for %s: %s", ctx.child_id, exc)
        return None, created_at
    return candidate, created_at


def _announce_result_marker(
    ctx: CCEventContext,
    marker_id: str | None,
    context_message_id: str | None,
    *,
    content: str,
    metadata: dict,
    created_at: str,
) -> None:
    if marker_id is None or context_message_id != marker_id:
        return
    try:
        broadcast_stored_message(
            ctx.child_id, marker_id, db=ctx.db, broadcast=ctx.bcast,
        )
    except Exception as exc:
        log.error(
            "cc_runtime: result marker announcement failed for %s: %s",
            ctx.child_id, exc,
        )


async def _finish_result_postprocessing(
    ctx: CCEventContext,
    payload: dict,
    context_message_id: str | None,
    *,
    quiet_if_unmeasured: bool,
) -> None:
    await record_terminal_llm_usage(
        ctx.child_id, ctx.live, ctx.live.last_call_usage,
        quiet_if_unmeasured=quiet_if_unmeasured,
    )
    await consume_and_attach_terminal_context_usage(
        live=ctx.live, child_id=ctx.child_id, payload=payload,
        message_id=context_message_id, db=ctx.db,
    )
    ctx.bcast(ctx.child_id, payload)
    if ctx.live.parent_thread_id:
        ctx.bcast(ctx.live.parent_thread_id, {
            "type": "subagent_status",
            "thread_id": ctx.live.parent_thread_id,
            "child_thread_id": ctx.child_id,
            "status": "ready",
        })


async def _record_plan_completion(ctx: CCEventContext) -> TurnEndEffects:
    """Commit and prepare plan effects through the canonical terminal owner."""

    return await record_external_turn_end(ctx.child_id)


async def _record_plan_error(
    ctx: CCEventContext, *, error: str, turn_id: str,
) -> TurnEndEffects:
    """Commit and prepare failed-Turn effects through the canonical owner."""

    return await record_external_turn_end(
        ctx.child_id,
        outcome="error",
        error=error,
        turn_id=turn_id,
    )


def _schedule_sparse_usage_control_read(ctx: CCEventContext) -> None:
    """After a terminal event, repair a sparse live meter through the canonical check owner."""
    session = ctx.live.session
    if session is None or not getattr(session, "_corsia_usage_control_read_after_turn", False):
        return
    session._corsia_usage_control_read_after_turn = False
    from connections.harness_usage_check import schedule_account_check
    from subagents.vendor_write_admission import session_receipt

    receipt = session_receipt(session)
    if receipt is not None:
        schedule_account_check("claude-code", receipt.account_id)


async def _handle_result_error(
    ctx: CCEventContext, ev: dict, metadata: dict, *, is_error: bool,
) -> None:
    if not is_error:
        return
    if auth_failure_kind("claude-code", ev.get("result")):
        from subagents.turn_account_error import turn_account

        account = await run_in_db_unless_owned(
            ctx.db, turn_account, metadata.get("turn_id"), "claude-code", db=ctx.db,
        )
        reconcile_expired_login(ev.get("result"), log, account)
    if is_empty_text_block_rejection(ev.get("result")):
        await _retire_poisoned_session(ctx, metadata)


async def on_result(ctx: CCEventContext, ev: dict) -> None:
    ingress_event = ev
    if not ctx.live.turn_active:
        ctx.live.last_call_usage = None
        ctx.live.thinking_buf = ""
        ctx.live.reasoning_provenance = None
        ctx.live.reasoning_provider_message_id = None
        log.debug("cc_runtime: ignoring pre-input result for %s", ctx.child_id)
        return
    identity = event_identity(ctx, ev)
    if ctx.live.corsia_turn != identity:
        log.warning(
            "cc_runtime: ignored stale result generation for %s: %s != %s",
            ctx.child_id, identity, ctx.live.corsia_turn,
        )
        return
    apply_now = ctx.live.apply_now_interrupt_turn == identity
    await _flush_pending_reasoning(ctx, identity)
    if ctx.live.tool_step_ids:
        await settle_missing_tool_results(
            live=ctx.live, child_id=ctx.child_id,
            identity=event_identity(ctx, ev), bcast=ctx.bcast, db=ctx.db,
        )
    is_error, metadata = _result_metadata(
        ctx,
        ev,
        identity=identity,
        apply_now_interrupt_owns_result=apply_now,
    )
    content = _marker_content(
        ctx, ev, is_error=is_error, metadata=metadata,
    )
    outcome = _result_outcome(is_error, metadata)
    await _handle_result_error(ctx, ev, metadata, is_error=is_error)
    settlement = await claim_terminal(
        identity, outcome,
        (ev.get("result") or "Claude Code turn failed") if outcome == "error" else None,
    )
    if settlement.result is not TurnTerminalResult.APPLIED:
        log.debug(
            "cc_runtime: suppressed non-owning terminal for %s: %s",
            ctx.child_id, settlement.result.value,
        )
        return
    ingress_event["_corsia_terminal_committed"] = True
    ingress_event["_corsia_terminal_settlement"] = settlement
    _clear_terminal_live_state(ctx)
    marker_id, created_at = await _insert_result_marker(ctx, content, metadata)
    context_message_id = ctx.live.last_assistant_message_id or marker_id
    payload_body = {"type": "turn_complete", "thread_id": ctx.child_id}
    if context_message_id is not None:
        payload_body["context_message_id"] = context_message_id
    payload = frame(ctx, ev, payload_body)
    # The returned terminal carrier keeps plan effects with the settlement so
    # the stream owner can deliver them only after the terminal callback exits.
    effects = None
    try:
        _announce_result_marker(
            ctx, marker_id, context_message_id, content=content,
            metadata=metadata, created_at=created_at,
        )
        await _finish_result_postprocessing(
            ctx, payload, context_message_id,
            quiet_if_unmeasured=is_error and ctx.live.last_assistant_message_id is None,
        )
        if metadata.get("stop_reason") != "interrupted":
            if is_error:
                effects = await _record_plan_error(
                    ctx,
                    error=content,
                    turn_id=identity.turn_id,
                )
            else:
                effects = await _record_plan_completion(ctx)
    finally:
        if ctx.live.turn_terminal is not None:
            ctx.live.turn_terminal.set()
    _schedule_sparse_usage_control_read(ctx)
    if effects is not None:
        ingress_event["_corsia_turn_end_effects"] = effects


def notify_parent(live, child_id: str, content: str, bcast, db) -> None:
    """Publish the retained logical-close notice for a user-driven child."""
    message_id = insert_message(
        live.parent_thread_id,
        "system",
        content,
        {"type": "subagent_done", "child_thread_id": child_id},
        msg_id=str(uuid.uuid4()),
        db=db,
    )
    broadcast_stored_message(
        live.parent_thread_id, message_id, db=db, broadcast=bcast,
    )
