"""Persist normalized Codex events and broadcast CORSIA WS updates."""

from __future__ import annotations

import functools
import logging
import uuid
from datetime import datetime, timezone

from automation.turn_end_effects import (
    deliver_turn_end_after,
    record_external_turn_end,
)
from core.reasoning_provenance import merge_reasoning_provenance_evidence
from infra.delivery_states import PROVIDER_MISSING_RESULT_REASON
from core.turn_core import (
    TurnIdentity,
    TurnTerminalResult,
    commit_turn_terminal,
)
from infra.db_async import run_in_db
from infra.queries import insert_message
from infra.send_broadcast import broadcast_message_rows, broadcast_stored_message
from llm.error_cause import codex_failure_meta
from registry.observer import observe_settled_tool_result
from registry.registration_writer import RegistrationOrigin
from registry.service import notify_changed, register_codex_thread
from subagents.assistant_step_display import assistant_step_payload
from subagents.codex_runtime_support import (
    CodexOnEventContext as _CodexOnEventContext,
    reasoning_representation_kind as _reasoning_representation_kind,
)
from tools.tool_display import SettledResult, tool_done_frame, tool_start_frame
from subagents.codex_runtime_terminal import (
    provider_reported_model_id as _provider_reported_model_id,
    publish_claimed_terminal as _publish_claimed_terminal,
    release_and_drive_terminal,
    record_terminal_llm_usage as _record_terminal_llm_usage,
    settle_failed_turn_usage as _settle_failed_turn_usage,
)
from subagents.codex_usage_calls import on_call_usage as _on_call_usage
from subagents import terminal_marker_writer

log = logging.getLogger(__name__)

_PROVIDER_MISSING_TOOL_RESULT = (
    "Codex did not provide a result for this tool call. "
    "CORSIA will rebuild provider context before the next turn."
)


async def _observe_tool_result(
    thread_id: str, content: str, metadata: dict, *, origin: RegistrationOrigin | None = None,
) -> None:
    """Observe one committed Codex result without gating primary delivery."""
    await observe_settled_tool_result(thread_id, content, metadata, origin=origin)


def _turn_frame(identity: TurnIdentity, payload: dict) -> dict:
    return {**payload, **identity.frame_fields()}


async def _emit_finalized_step(
    *, thread_id: str, step_id: str, identity: TurnIdentity, bcast,
) -> None:
    from subagents.child_thread import load_assistant_step_display

    step = await run_in_db(
        load_assistant_step_display, thread_id, step_id,
    )
    calls = (step.get("metadata") or {}).get("tool_calls") or []
    bcast(thread_id, _turn_frame(identity, {
        "type": "step_text",
        "thread_id": thread_id,
        **assistant_step_payload(step, message_id=step_id),
        "finalized_tool_ids": [
            call["id"] for call in calls
            if isinstance(call, dict) and isinstance(call.get("id"), str)
        ],
    }))


async def settle_committed_tool_result(
    *,
    live,
    thread_id: str,
    identity: TurnIdentity,
    canonical,
    bcast=None,
) -> bool:
    """Settle one exact Codex debt after its canonical result row commits."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import read_bound_tool_result

    if live.corsia_turn != identity:
        raise ToolResultBindingError(
            "canonical tool result no longer owns the active CORSIA Turn"
        )
    tool_id = canonical.provider_tool_call_id
    if not isinstance(tool_id, str) or not tool_id:
        raise ToolResultBindingError("canonical Codex tool result lacks provider call identity")
    step_id = live.tool_step_ids.get(tool_id)
    if step_id is None:
        existing = await run_in_db(
            read_bound_tool_result,
            thread_id=thread_id,
            provider_tool_call_id=tool_id,
        )
        if (
            existing is not None
            and existing.message_id == canonical.message_id
            and existing.assistant_step_id == canonical.assistant_step_id
        ):
            return False
        raise ToolResultBindingError(
            "canonical Codex tool result lacks its exact live debt"
        )
    if step_id != canonical.assistant_step_id:
        raise ToolResultBindingError(
            "canonical Codex tool result changed Assistant Step ownership"
        )
    name = live.tool_names.pop(tool_id, canonical.name)
    live.tool_step_ids.pop(tool_id)
    if live.open_tool_step_id == step_id and step_id not in live.tool_step_ids.values():
        live.open_tool_step_id = None
    if not live.tool_step_ids:
        live.tools_settled.set()
    meta = {
        "tool_call_id": tool_id,
        "assistant_step_id": step_id,
        "name": canonical.name,
    }
    if canonical.is_error:
        meta["is_error"] = True
    await _observe_tool_result(
        thread_id, canonical.content,
        meta,
        origin=RegistrationOrigin(thread_id, identity.turn_id, "codex_observer"),
    )
    if bcast is None:
        import infra.state as state

        bcast = state.broadcast_to_thread
    bcast(thread_id, _turn_frame(identity, tool_done_frame(
        thread_id, tool_id, name,
        SettledResult.from_result(canonical), canonical.content[:8000],
    )))
    await _emit_finalized_step(
        thread_id=thread_id,
        step_id=step_id,
        identity=identity,
        bcast=bcast,
    )
    return True


async def settle_missing_tool_results(
    *, live, thread_id: str, identity: TurnIdentity, bcast,
) -> None:
    """Persist explicit missing results for durable Codex tool ownership."""
    from subagents.child_thread import persist_tool_result

    if not live.tool_step_ids:
        live.tools_settled.set()
        return
    live.provider_context_untrusted = True
    missing = SettledResult(
        _PROVIDER_MISSING_TOOL_RESULT, is_error=True,
        synthetic_reason=PROVIDER_MISSING_RESULT_REASON,
    )
    calls_by_step: dict[str, list[tuple[str, str]]] = {}
    for tool_id, step_id in live.tool_step_ids.items():
        calls_by_step.setdefault(step_id, []).append(
            (tool_id, live.tool_names.get(tool_id, "tool"))
        )
    for step_id, calls in calls_by_step.items():
        for tool_id, name in calls:
            await run_in_db(
                persist_tool_result,
                thread_id,
                turn_id=identity.turn_id,
                tool_use_id=tool_id,
                name=name,
                content=_PROVIDER_MISSING_TOOL_RESULT,
                is_error=True,
                assistant_step_id=step_id,
                synthetic_tool_result=True,
                synthetic_reason=PROVIDER_MISSING_RESULT_REASON,
            )
        for tool_id, name in calls:
            bcast(thread_id, _turn_frame(identity, tool_done_frame(
                thread_id, tool_id, name, missing, _PROVIDER_MISSING_TOOL_RESULT,
            )))
        await _emit_finalized_step(
            thread_id=thread_id,
            step_id=step_id,
            identity=identity,
            bcast=bcast,
        )
        for tool_id, _name in calls:
            live.tool_names.pop(tool_id, None)
            live.tool_step_ids.pop(tool_id, None)
        if live.open_tool_step_id == step_id and step_id not in live.tool_step_ids.values():
            live.open_tool_step_id = None
    live.tools_settled.set()


def make_on_event(
    live,
    thread_id: str,
    bcast,
    *,
    dead_set: set,
    thread_registry: dict,
):
    """Build the normalized-event coroutine for one Codex turn subprocess."""
    from core.tool_result_binding import (
        bind_tool_result_echo,
        read_bound_tool_result,
    )
    from subagents.child_thread import (
        load_assistant_step_display,
        persist_assistant_text,
        persist_tool_use,
    )

    ctx = _CodexOnEventContext(
        live=live,
        thread_id=thread_id,
        bcast=bcast,
        dead_set=dead_set,
        thread_registry=thread_registry,
        bind_tool_result_echo=bind_tool_result_echo,
        read_bound_tool_result=read_bound_tool_result,
        load_assistant_step_display=load_assistant_step_display,
        persist_assistant_text=persist_assistant_text,
        persist_tool_use=persist_tool_use,
    )
    return functools.partial(_dispatch_on_event, ctx)


def _event_identity(ctx, ev: dict) -> TurnIdentity:
    identity = ev.get("_corsia_turn_identity")
    if not isinstance(identity, TurnIdentity):
        raise RuntimeError(
            f"Codex event for {ctx.thread_id} has no CORSIA turn generation"
        )
    return identity


def _frame(ctx, ev: dict, payload: dict) -> dict:
    return {**payload, **_event_identity(ctx, ev).frame_fields()}


def _is_current_provider_event(ctx, ev: dict, *, label: str) -> bool:
    provider_thread_id = ev.get("provider_thread_id")
    provider_turn_id = ev.get("provider_turn_id")
    expected_thread_id = ctx.thread_registry.get(ctx.thread_id) or getattr(
        ctx.live,
        "codex_thread_id",
        None,
    )
    expected_provider_turn_id = getattr(
        getattr(ctx.live, "session", None),
        "active_turn_id",
        None,
    )
    is_current = (
        isinstance(provider_thread_id, str)
        and bool(provider_thread_id)
        and provider_thread_id == expected_thread_id
        and isinstance(provider_turn_id, str)
        and bool(provider_turn_id)
        and provider_turn_id == expected_provider_turn_id
        and isinstance(ctx.live.corsia_turn, TurnIdentity)
    )
    if not is_current:
        log.warning(
            "codex_runtime: ignored uncorrelated %s for %s "
            "thread=%r expected=%r turn=%r expected_turn=%r",
            label,
            ctx.thread_id,
            provider_thread_id,
            expected_thread_id,
            provider_turn_id,
            expected_provider_turn_id,
        )
    return is_current


async def _on_thread_started(ctx, ev: dict) -> None:
    codex_thread_id = ev.get("thread_id")
    if not codex_thread_id:
        return
    rollout_path = ev.get("rollout_path")
    if not isinstance(rollout_path, str) or not rollout_path:
        rollout_path = None
    ctx.live.codex_thread_id = codex_thread_id
    ctx.thread_registry[ctx.thread_id] = codex_thread_id
    log.info(
        "codex_runtime: thread_id=%s captured for session=%s (rollout=%s)",
        codex_thread_id, ctx.thread_id, rollout_path)
    from subagents.codex_runtime_support import thread_registration_metadata

    artifact = await run_in_db(
        register_codex_thread, ctx.thread_id, codex_thread_id,
        **thread_registration_metadata(
            ctx.live.session, rollout_path, ev.get("_corsia_checkpoint_generation_id")),
    )
    if artifact:
        notify_changed(artifact)


async def _on_text_delta(ctx, ev: dict) -> None:
    ctx.bcast(ctx.thread_id, _frame(ctx, ev, {
        "type": "text_delta", "thread_id": ctx.thread_id,
        "content": ev.get("text", ""),
    }))


async def _on_assistant_text(ctx, ev: dict) -> None:
    text = ev.get("text", "")
    identity = _event_identity(ctx, ev)
    model_id = _provider_reported_model_id(ctx.live)
    mid = await run_in_db(
        ctx.persist_assistant_text, ctx.thread_id, text,
        turn_id=identity.turn_id,
        reasoning=ctx.live.thinking_buf or None,
        reasoning_provenance=ctx.live.thinking_provenance,
        provider="codex",
        model=model_id,
        expected_model=getattr(ctx.live, "expected_reported_model", None),
        provider_turn_id=ev.get("provider_turn_id"),
        provider_message_id=ev.get("provider_message_id"),
    )
    step = await run_in_db(ctx.load_assistant_step_display, ctx.thread_id, mid)
    ctx.bcast(ctx.thread_id, _frame(ctx, ev, {
        "type": "step_text", "thread_id": ctx.thread_id,
        **assistant_step_payload(step, message_id=mid),
        "finalized_tool_ids": [],
    }))
    ctx.live.last_text = text
    ctx.live.last_assistant_message_id = mid
    ctx.live.open_tool_step_id = mid
    ctx.live.thinking_buf = ""
    ctx.live.thinking_provenance = None
    ctx.live.turn_has_assistant_row = True


async def _on_step_reasoning(ctx, ev: dict) -> None:
    ctx.live.thinking_buf += ev.get("text", "")
    incoming_provenance = ev.get("provenance")
    if isinstance(incoming_provenance, dict):
        ctx.live.thinking_provenance = merge_reasoning_provenance_evidence(
            ctx.live.thinking_provenance,
            incoming_provenance,
            representation_kind=_reasoning_representation_kind(
                ctx.live.thinking_provenance,
                incoming_provenance,
            ),
        )
    ctx.bcast(ctx.thread_id, _frame(ctx, ev, {
        "type": "step_reasoning", "thread_id": ctx.thread_id,
        "content": ev.get("text", ""),
    }))


async def _on_stream_line_dropped(ctx, _ev: dict) -> None:
    if not ctx.live.turn_has_assistant_row:
        ctx.live.dropped_line_this_turn = True


async def _on_tool_use(ctx, ev: dict) -> None:
    if not _is_current_provider_event(ctx, ev, label="dynamic tool start"):
        return
    identity = _event_identity(ctx, ev)
    name = ev.get("name", "")
    tool_id = ev.get("id") or str(uuid.uuid4())
    args = ev.get("input") or {}
    hint = name[:80]
    model_id = _provider_reported_model_id(ctx.live)
    # Pending calls share the open Step until the last result settles.
    append_step_id = ctx.live.open_tool_step_id
    step_id = await run_in_db(
        ctx.persist_tool_use,
        ctx.thread_id, tool_id=tool_id, name=name,
        turn_id=identity.turn_id,
        arguments=args, hint=hint,
        provider_turn_id=ev["provider_turn_id"],
        reasoning=ctx.live.thinking_buf or None,
        reasoning_provenance=ctx.live.thinking_provenance,
        provider="codex",
        model=model_id,
        expected_model=getattr(ctx.live, "expected_reported_model", None),
        assistant_step_id=append_step_id,
    )
    ctx.live.thinking_buf = ""
    ctx.live.thinking_provenance = None
    ctx.live.open_tool_step_id = step_id
    ctx.live.tool_names[tool_id] = name
    ctx.live.tool_step_ids[tool_id] = step_id
    ctx.live.tools_settled.clear()
    ctx.live.last_assistant_message_id = step_id
    ctx.bcast(ctx.thread_id, _frame(ctx, ev, tool_start_frame(
        ctx.thread_id, tool_id, name, hint, args, str(args)[:400],
    )))


async def _on_tool_result(ctx, ev: dict) -> None:
    if not _is_current_provider_event(ctx, ev, label="dynamic tool result"):
        return
    _event_identity(ctx, ev)
    tool_id = ev.get("tool_use_id", "")
    step_id = ctx.live.tool_step_ids.get(tool_id)
    if step_id is None:
        settled = await run_in_db(
            ctx.read_bound_tool_result,
            thread_id=ctx.thread_id,
            provider_tool_call_id=tool_id,
        )
        if settled is None:
            raise RuntimeError(
                f"Codex tool result {tool_id!r} has no Assistant Step"
            )
        await run_in_db(
            ctx.bind_tool_result_echo,
            thread_id=ctx.thread_id,
            provider_tool_call_id=tool_id,
            assistant_step_id=settled.assistant_step_id,
            echoed_content=ev.get("content", ""),
        )
        return
    canonical = await run_in_db(
        ctx.bind_tool_result_echo,
        thread_id=ctx.thread_id,
        provider_tool_call_id=tool_id,
        assistant_step_id=step_id,
        echoed_content=ev.get("content", ""),
    )
    await settle_committed_tool_result(
        live=ctx.live,
        thread_id=ctx.thread_id,
        identity=_event_identity(ctx, ev),
        canonical=canonical,
        bcast=ctx.bcast,
    )


async def record_terminal_marker(
    thread_id: str,
    content: str,
    metadata: dict,
    bcast,
    db=None,
    *,
    label: str = "terminal",
    announce: bool | None = None,
    created_at: str | None = None,
    best_effort_announcement: bool = False,
) -> str | None:
    """Persist one terminal marker and announce the same row when requested."""
    marker_id = str(uuid.uuid4())
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    insert_kwargs = {"msg_id": marker_id, "created_at": created_at}
    try:
        if db is None:
            marker_id = await run_in_db(
                terminal_marker_writer.insert_terminal_marker,
                insert_message, thread_id, content, metadata,
                **insert_kwargs,
            )
        else:
            marker_id = terminal_marker_writer.insert_terminal_marker(
                insert_message, thread_id, content, metadata, db=db, **insert_kwargs,
            )
    except Exception as exc:
        log.error("codex_runtime: %s marker failed for %s: %s", label, thread_id, exc)
        return None
    if announce is None:
        announce = bool(content)
    if not announce:
        return marker_id
    try:
        if db is None:
            await broadcast_message_rows(thread_id, (marker_id,), broadcast=bcast)
        else:
            broadcast_stored_message(thread_id, marker_id, db=db, broadcast=bcast)
    except Exception as exc:
        if not best_effort_announcement:
            raise
        log.error(
            "codex_runtime: %s marker announcement failed for %s: %s",
            label, thread_id, exc,
        )
    return marker_id


async def _on_result(ctx, ev: dict) -> None:
    identity = _event_identity(ctx, ev)
    is_error = bool(ev.get("is_error"))
    meta_r = {"codex": True, "type": "result", "is_error": is_error, "turn_id": identity.turn_id}
    if ev.get("stop_reason"):
        meta_r["stop_reason"] = ev["stop_reason"]
    outcome = "error" if is_error else "completed"
    settlement = await commit_turn_terminal(
        identity.turn_id,
        outcome,
        (ev.get("result") or "Codex turn failed") if outcome == "error" else None,
    )
    if settlement.result is not TurnTerminalResult.APPLIED:
        return
    # A later best-effort terminal effect may fail, but the surrounding
    # runtime must still retire this committed generation and wake its
    # durable inbox head.
    ev["_corsia_terminal_committed"] = True
    effects = None
    try:
        meta_r.update(await _record_terminal_llm_usage(
            ctx.thread_id, ctx.live, ev.get("token_volume"),
            quiet_if_unmeasured=is_error and ctx.live.last_assistant_message_id is None,
        ))
        marker_content = "" if (
            ctx.live.turn_has_assistant_row and not is_error
            and not ctx.live.dropped_line_this_turn
        ) else (
            ev.get("result") or ""
        )
        ctx.live.turn_has_assistant_row = False
        ctx.live.dropped_line_this_turn = False
        marker_id = await record_terminal_marker(
            ctx.thread_id,
            content=marker_content,
            metadata=meta_r,
            bcast=ctx.bcast,
            label="result",
            announce=ctx.live.last_assistant_message_id is None,
            created_at=datetime.now(timezone.utc).isoformat(),
            best_effort_announcement=True,
        )
        await _publish_claimed_terminal(
            thread_id=ctx.thread_id,
            live=ctx.live,
            identity=identity,
            bcast=ctx.bcast,
            source="codex-app-server:turn/result",
            marker_id=marker_id,
            marker_is_context_owner=True,
            usage=ev.get("usage"),
        )
        if meta_r.get("stop_reason") != "interrupted":
            failure = dict(outcome="error", error=ev.get("result") or "Codex turn failed",
                           turn_id=identity.turn_id) if is_error else {}
            effects = await record_external_turn_end(ctx.thread_id, **failure)
    finally:
        await deliver_turn_end_after(
            effects, lambda: release_and_drive_terminal(ctx, settlement))


async def _on_turn_failed(ctx, ev: dict) -> None:
    identity = _event_identity(ctx, ev)
    cause = ev.get("cause", "process_exited")
    reset_at = ev.get("reset_at")
    msg = ev.get("message") or f"Codex turn failed: {cause}"
    settlement = await commit_turn_terminal(identity.turn_id, "error", msg)
    if settlement.result is not TurnTerminalResult.APPLIED:
        return
    ev["_corsia_terminal_committed"] = True
    effects = None
    try:
        unrecorded = await _settle_failed_turn_usage(ctx.thread_id, ctx.live, ev.get("token_volume"))
        if cause in ("quota_exhausted", "auth_failed"):
            ctx.dead_set.add(ctx.thread_id)
        if cause == "context_window_exceeded":
            from subagents.codex_runtime_state import mark_provider_context_untrusted

            mark_provider_context_untrusted(ctx.thread_id)
            ctx.thread_registry.pop(ctx.thread_id, None)
        meta_f = {
            **codex_failure_meta(cause, msg, reset_at, ctx.live.model, unrecorded),
            "turn_id": identity.turn_id,
        }
        marker_id = await record_terminal_marker(
            ctx.thread_id,
            content=f"⚠️ {msg}",
            metadata=meta_f,
            bcast=ctx.bcast,
            label="failure",
            best_effort_announcement=True,
        )
        await _publish_claimed_terminal(
            thread_id=ctx.thread_id,
            live=ctx.live,
            identity=identity,
            bcast=ctx.bcast,
            source="codex-app-server:turn/failed",
            marker_id=marker_id,
            marker_is_context_owner=False,
            usage=ev.get("usage"),
        )
        # Last, so a plan-side failure can never hide the failure marker.
        effects = await record_external_turn_end(
            ctx.thread_id,
            outcome="error",
            error=msg,
            turn_id=identity.turn_id,
            clears_at=reset_at,
        )
    finally:
        await deliver_turn_end_after(
            effects, lambda: release_and_drive_terminal(ctx, settlement))


async def _on_provider_tool_violation(ctx, ev: dict) -> None:
    await terminal_marker_writer.record_provider_tool_violation(
        ctx, ev, _is_current_provider_event,
    )


async def _on_model_reported(ctx, ev: dict) -> None:
    ctx.live.provider_reported_model = ev.get("model") or None
_dispatch = {
    "thread_started": _on_thread_started,
    "text_delta": _on_text_delta,
    "assistant_text": _on_assistant_text,
    "step_reasoning": _on_step_reasoning,
    "stream_line_dropped": _on_stream_line_dropped,
    "tool_use": _on_tool_use,
    "tool_result": _on_tool_result,
    "result": _on_result,
    "turn_failed": _on_turn_failed,
    "provider_tool_violation": _on_provider_tool_violation,
    "model_reported": _on_model_reported, "call_usage": _on_call_usage,
}


async def _dispatch_on_event(ctx, ev: dict) -> None:
    """Dispatch a normalized Codex runtime event to its persistence handler."""
    kind = ev.get("kind")
    handler = _dispatch.get(kind) if isinstance(kind, str) else None
    if handler is not None:
        if kind in {"result", "turn_failed"}:
            if not isinstance(ctx.live.corsia_turn, TurnIdentity):
                raise RuntimeError(
                    f"Codex terminal for {ctx.thread_id} has no CORSIA turn "
                    "generation"
                )
            # A natural terminal owes its own declared calls their durable
            # results exactly like every destructive one: one settlement.
            from subagents.codex_runtime import _settle_live_tool_debt

            await _settle_live_tool_debt(ctx.thread_id, ctx.live, ctx.bcast)
        if kind in {
            "text_delta", "assistant_text", "step_reasoning",
            "tool_use", "tool_result", "result", "turn_failed",
        }:
            ev.setdefault("_corsia_turn_identity", ctx.live.corsia_turn)
        await handler(ctx, ev)
