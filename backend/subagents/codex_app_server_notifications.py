"""Codex app-server notification routing at provider-owned boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from subagents import codex_app_server_events as ev
from subagents import codex_usage_observer as usage_observer
from subagents.codex_durable_receipts import usage_identity

log = logging.getLogger(__name__)


async def record_usage(session, turn_id: str, token_usage: dict) -> None:
    """Occupancy and billing share one monotonically observed provider receipt."""
    if turn_id != session.active_turn_id:
        return  # Resume replays historical totals before the new Turn exists.
    previous = session._usage_by_turn.get(turn_id)
    if previous is not None:
        before, after = usage_identity(previous), usage_identity(token_usage)
        if before == after or sum(after[:2]) < sum(before[:2]):
            return
    session._usage_by_turn[turn_id] = token_usage
    # General provider notifications may legitimately omit output or cumulative
    # counts.  They remain useful for billing, but only a complete receipt can
    # replace the conservative context floor.  Managed tool callbacks provide
    # their strict, complete receipt through the durable rollout path.
    last, total = token_usage.get("last"), token_usage.get("total")
    if all(
        isinstance(bucket, dict)
        and "inputTokens" in bucket
        and "outputTokens" in bucket
        for bucket in (last, total)
    ):
        session.tool_context.record_receipt(token_usage)
    spent = session._observe_turn_volume(turn_id, token_usage)
    if spent is not None:
        await session._emit({
            "kind": "call_usage", "turn_id": turn_id,
            "ordinal": session._turn_volume.spent_readings(turn_id),
            "usage": spent.ledger_row(),
        })


async def respond(
    session,
    msg_id: int,
    *,
    result: Any = None,
    error: dict | None = None,
) -> None:
    """Write one response unless intentional teardown already owns the pipe."""
    from subagents.codex_app_server import REQUEST_TIMEOUT
    from subagents.codex_requests import drain

    if session._proc is None or session._proc.stdin is None:
        return
    payload = {"id": msg_id, "error" if error is not None else "result": (
        error if error is not None else result
    )}
    async with session._write_lock:
        if session._closing:
            return
        try:
            session._proc.stdin.write((json.dumps(payload) + "\n").encode())
            async with asyncio.timeout(REQUEST_TIMEOUT) as deadline:
                await drain(session, deadline)
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            if not session._closing:
                raise


async def _handle_thread_state(session, method: str, params: dict) -> bool:
    if method == "thread/started":
        thread_id = (params.get("thread") or {}).get("id")
        if (
            isinstance(thread_id, str)
            and thread_id
            and session.thread_id not in (None, thread_id)
        ):
            log.warning(
                "codex_app_server: ignored provider thread identity replacement "
                "observed=%r owned=%r",
                thread_id,
                session.thread_id,
            )
        return True
    if method in ("thread/settings/updated", "model/rerouted"):
        model = (
            (params.get("threadSettings") or {}).get("model")
            if method == "thread/settings/updated"
            else params.get("toModel")
        )
        if model:
            await session._record_reported_model(str(model))
        return True
    return False


async def _handle_turn_state(session, method: str, params: dict) -> bool:
    if method == "turn/started":
        turn_id = (params.get("turn") or {}).get("id")
        if isinstance(turn_id, str) and turn_id and turn_id != session._active_turn_id:
            log.warning(
                "codex_app_server: ignored provider turn identity replacement "
                "observed=%r owned=%r thread=%r",
                turn_id,
                session._active_turn_id,
                params.get("threadId"),
            )
        return True
    if method == "thread/tokenUsage/updated":
        turn_id = params.get("turnId")
        if turn_id and params.get("threadId") == session.thread_id:
            token_usage = params.get("tokenUsage") or {}
            await record_usage(session, turn_id, token_usage)
        return True
    return False


async def handle_thread_notification(session, method: str, params: dict) -> bool:
    if method == "thread/compacted":
        await session._emit(
            ev.provider_compaction_breach_event(
                source="json-rpc-notification",
                provider_thread_id=params.get("threadId"),
                provider_turn_id=params.get("turnId"),
            )
        )
        return True
    if method in {"thread/started", "thread/settings/updated", "model/rerouted"}:
        return await _handle_thread_state(session, method, params)
    if method in {"turn/started", "thread/tokenUsage/updated"}:
        return await _handle_turn_state(session, method, params)
    return False


async def _emit_reasoning_summary(session, params: dict) -> None:
    item_id = params.get("itemId")
    delta = params.get("delta", "")
    if not isinstance(delta, str):
        delta = ""
    summary_index = params.get("summaryIndex", 0)
    if not isinstance(summary_index, int) or isinstance(summary_index, bool):
        summary_index = 0
    parts = session._reasoning_summary_parts.setdefault(str(item_id or ""), {})
    starts_new_item = bool(
        session._last_reasoning_summary_item_id is not None
        and item_id != session._last_reasoning_summary_item_id
    )
    starts_new_part = summary_index not in parts and any(parts.values())
    parts[summary_index] = parts.get(summary_index, "") + delta
    if isinstance(item_id, str) and item_id:
        session._last_reasoning_summary_item_id = item_id
    separator = "\n\n" if (starts_new_item or starts_new_part) and delta else ""
    await session._emit(
        {
            "kind": "step_reasoning",
            "text": separator + delta,
            "provenance": ev.reasoning_provenance(item_id),
        }
    )


async def _emit_opaque_reasoning(session, params: dict) -> None:
    item_id = params.get("itemId")
    if isinstance(item_id, str) and item_id:
        session._opaque_reasoning_references.add(item_id)
    await session._emit(
        {
            "kind": "step_reasoning",
            "text": "",
            "provenance": ev.reasoning_provenance(
                item_id, representation_kind="opaque"
            ),
        }
    )


async def handle_reasoning_notification(session, method: str, params: dict) -> bool:
    if method == "item/reasoning/summaryTextDelta":
        await _emit_reasoning_summary(session, params)
        return True
    if method == "item/reasoning/textDelta":
        await _emit_opaque_reasoning(session, params)
        return True
    return False


def _correlated_dynamic_tool_item(
    session,
    method: str,
    item: dict,
    params: dict,
) -> bool:
    if item.get("type") != "dynamicToolCall" or session._is_active_provider_boundary(
        params.get("threadId"),
        params.get("turnId"),
    ):
        return True
    log.warning(
        "codex_app_server: ignored uncorrelated dynamic tool %s thread=%r "
        "expected=%r turn=%r expected_turn=%r",
        "start" if method == "item/started" else "result",
        params.get("threadId"),
        session.thread_id,
        params.get("turnId"),
        session._active_turn_id,
    )
    return False


async def handle_item_notification(session, method: str, params: dict) -> bool:
    if method not in ("item/started", "item/completed"):
        return False
    item = params.get("item") or {}
    if not _correlated_dynamic_tool_item(session, method, item, params):
        return True
    handler = (
        session._handle_item_started
        if method == "item/started"
        else session._handle_item_completed
    )
    await handler(
        item,
        provider_thread_id=params.get("threadId"),
        provider_turn_id=params.get("turnId"),
    )
    return True


async def handle_error_notification(session, method: str, params: dict) -> bool:
    if await usage_observer.handle_rate_limits_notification(session, method, params):
        return True
    if method != "error":
        return False
    error = params.get("error") or {}
    if params.get("willRetry"):
        return True
    await usage_observer.observe_turn_error(session, error)
    turn_id = params.get("turnId")
    is_current_turn = bool(turn_id and turn_id == session._active_turn_id)
    if is_current_turn and session._tool_requests is not None:
        await session._tool_requests.close()
    usage = ev.usage_payload(
        session._usage_by_turn.pop(turn_id, None) if isinstance(turn_id, str) else None
    )
    terminal_event = ev.failed_event(error)
    if usage is not None:
        terminal_event["usage"] = usage
    terminal_event["token_volume"] = session._turn_volume.take(turn_id)
    terminal_event["turn_id"] = turn_id
    terminal_event["is_current_turn"] = is_current_turn
    if is_current_turn:
        session._active_turn_id = None
        session._active_turn_started = None
        session._clear_reasoning_observations()
    await session._emit(terminal_event)
    if isinstance(turn_id, str):
        waiter = session._completed_turns.pop(turn_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result({"id": turn_id, "status": "failed", "error": error})
    return True


async def handle_turn_completed(session, turn: dict) -> None:
    """Cancel receipt waits and settle the owned terminal before acknowledging it."""
    turn_id = turn.get("id")
    status = turn.get("status")
    is_current_turn = bool(turn_id and session._active_turn_id == turn_id)
    started = None
    if is_current_turn:
        if session._tool_requests is not None:
            await session._tool_requests.close()
        session._active_turn_id = None
        # Only the turn CORSIA started can vouch for the account; a stray id cannot.
        owned, session._active_turn_started = session._active_turn_started, None
        started = owned[1] if owned and owned[0] == turn_id else None
    usage = session._usage_by_turn.pop(turn_id, None) if turn_id else None
    if status == "failed":
        await usage_observer.observe_turn_error(session, turn.get("error"))
        terminal_event = ev.failed_event(turn.get("error") or {}, usage)
    else:
        if status == "completed":
            await usage_observer.observe_turn_success(session, started)
        terminal_event = ev.result_event(turn, usage)
    terminal_event["token_volume"] = session._turn_volume.take(turn_id)
    terminal_event["turn_id"] = turn_id
    terminal_event["is_current_turn"] = is_current_turn
    await session._emit(terminal_event)
    # Resolve only after persistence/finalization finishes. Resolving early
    # lets a successor re-register this handle while its old terminal runs.
    # If emission fails, interrupt retains its existing timeout/preempt owner.
    if turn_id:
        waiter = session._completed_turns.pop(turn_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(turn)
    if is_current_turn:
        session._clear_reasoning_observations()
