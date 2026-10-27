"""WebSocket endpoint — connection lifecycle, auth, and message dispatch.

Thin entry point. Turn execution lives in ``ws.turn``.
Post-turn work (titles, summaries) lives in ``ws.post_turn``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from infra import state
from infra.database import get_db
from infra.db_async import run_in_db
from infra.send_service import send_thread_input
from infra.queries import get_thread_for_user, insert_message, touch_thread
from llm.resolution import ExecutionTarget
from ws.message_dispatch import (
    handle_cc_main_message as _dispatch_cc_main_message,
    handle_codex_main_message as _dispatch_codex_main_message,
    handle_message as _dispatch_message,
)
from ws.ingress import (
    background_tasks as _background_tasks,
    process_messages as _process_messages,
)
from ws.thread_control import (
    authenticate_ws as _authenticate_impl,
    cleanup_ws as _cleanup_ws_impl,
    handle_subscribe as _handle_subscribe_impl,
    handle_unsubscribe as _handle_unsubscribe_impl,
    owned_thread as _owned_thread_impl,
)
from ws.presence_control import handle_presence as _handle_presence_impl
from ws.stop_control import (
    StopReceipt,
    do_stop as _do_stop_impl,
    resolve_active_turn_id as _resolve_active_turn_id,
)
from ws.subagent_control import (
    cc_attachment_block as _cc_attachment_block_impl,
    handle_subagent_stop as _handle_subagent_stop_impl,
)

from ws.subagent_ingress import dispatch_subagent_feed as _handle_subagent_feed
from ws.document_control import FRAME_TYPES, schedule_frame, disconnect as disconnect_documents

_MISSING_TURN_ID = object()


async def _receive_frames(
    ws: WebSocket,
    user: dict,
    message_queue: asyncio.Queue,
) -> None:
    """Read frames into the turn queue while handling control frames inline."""
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            if msg_type in FRAME_TYPES:
                schedule_frame(ws, user, data)
                continue
            if msg_type == "stop":
                expected_turn_id = (
                    data["turn_id"] if "turn_id" in data else _MISSING_TURN_ID
                )
                _handle_stop(user, data.get("thread_id"), expected_turn_id, ws=ws)
                continue
            if msg_type == "subscribe_session":
                await _handle_subscribe(ws, user, data.get("thread_id", ""))
                continue
            if msg_type == "unsubscribe_session":
                _handle_unsubscribe(ws, data.get("thread_id", ""))
                continue
            if msg_type == "presence":
                await _handle_presence_impl(ws, user, data, owned_thread_fn=_owned_thread)
                continue
            if msg_type == "subagent_feed":
                task = asyncio.create_task(_handle_subagent_feed(
                    ws, user, data.get("thread_id", ""), data.get("content", ""),
                    data.get("attachments") or [], data.get("edited", False),
                    data.get("input_id"), data.get("delivery_intent"),
                    data.get("viewing_document"), data.get("reply_context")))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
                continue
            if msg_type == "subagent_stop":
                task = asyncio.create_task(_handle_subagent_stop(
                    ws, user, data.get("thread_id", ""), data.get("turn_id")))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
                continue
            await message_queue.put(data)
    except WebSocketDisconnect:
        await message_queue.put(None)


async def _heartbeat(ws: WebSocket) -> None:
    """Keep the socket warm and detect disconnected clients."""
    try:
        while True:
            await asyncio.sleep(30)
            try:
                await state.send_to_client(ws, {"type": "ping"})
            except Exception:
                break
    except asyncio.CancelledError:
        pass


async def websocket_endpoint(ws: WebSocket) -> None:
    """Authenticate a WebSocket and route chat/control frames."""
    await ws.accept()
    user = await _authenticate(ws)
    if not user:
        return

    user_id = user["id"]
    message_queue: asyncio.Queue = asyncio.Queue()
    receiver_task = asyncio.create_task(_receive_frames(ws, user, message_queue))
    processor_task = asyncio.create_task(
        _process_messages(ws, user, user_id, message_queue, _handle_message)
    )
    heartbeat_task = asyncio.create_task(_heartbeat(ws))

    try:
        await asyncio.gather(receiver_task, processor_task)
    except WebSocketDisconnect:
        pass
    finally:
        receiver_task.cancel()
        processor_task.cancel()
        heartbeat_task.cancel()
        _cleanup_ws(user_id, ws)
        await disconnect_documents(ws)


async def _authenticate(ws: WebSocket) -> dict | None:
    """Handle the auth handshake. Returns user dict or None on failure."""
    return await _authenticate_impl(ws)


def _handle_stop(
    user: dict,
    sid: str | None,
    expected_turn_id: object = _MISSING_TURN_ID,
    *,
    ws: WebSocket | None = None,
) -> None:
    """Route an exact Stop, resolving a missed client identity when needed.

    Known IDs snapshot runtime handles synchronously. An omitted ID first
    resolves exactly one current durable Turn, then takes the same snapshot.
    ``_do_stop`` keeps the exact-ID and object-identity guards, so a successor
    that appears during either path is never cancelled by a stale Stop.
    """
    if not sid:
        return
    requested_turn_id = None
    if expected_turn_id is _MISSING_TURN_ID:
        action = _resolve_and_do_stop(user, sid)
    elif isinstance(expected_turn_id, str) and expected_turn_id:
        requested_turn_id = expected_turn_id
        turn_event = state._active_turns.get(sid)
        llm_task = state._active_llm_tasks.get(sid)
        action = _do_stop(user, sid, turn_event, llm_task, expected_turn_id)
    else:
        action = _immediate_stop_receipt(sid)
    task = asyncio.create_task(
        _send_stop_receipt(ws, sid, action, requested_turn_id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _resolve_and_do_stop(user: dict, sid: str) -> StopReceipt:
    """Resolve a missed client turn identity before applying an exact Stop."""
    expected_turn_id = await _resolve_active_turn_id(user, sid)
    if expected_turn_id is None:
        return StopReceipt(sid, "no_active_turn")
    turn_event = state._active_turns.get(sid)
    llm_task = state._active_llm_tasks.get(sid)
    return await _do_stop(user, sid, turn_event, llm_task, expected_turn_id)


async def _do_stop(
    user: dict,
    sid: str,
    turn_event: "asyncio.Event | None",
    llm_task: "asyncio.Task | None",
    expected_turn_id: str,
) -> StopReceipt:
    """Stop the captured Native owner, then live CC and Codex owners.

    ``turn_event`` and ``llm_task`` are captured before entering this function.
    After the ownership check, each handle is cancelled only if it is still the
    live object at ``state._active_turns[sid]`` / ``state._active_llm_tasks[sid]`` — a new
    turn that registered while the ownership check was in flight will have
    replaced the slot with a new object, so the identity check fails and the
    new turn is left running.

    Native cancellation runs first so it is never delayed by harness-stop
    serialization. Exact foreground process cleanup follows the signal, then
    both live harness owners are attempted. The first cleanup failure is logged
    and re-raised only after all applicable owners have been attempted.
    """
    return await _do_stop_impl(
        user, sid, turn_event, llm_task, expected_turn_id,
        owns_thread_fn=_owns_thread,
        log=logger,
    )


async def _immediate_stop_receipt(sid: str) -> StopReceipt:
    return StopReceipt(sid, "no_active_turn")


async def _send_stop_receipt(
    ws: WebSocket | None,
    sid: str,
    action: Awaitable[StopReceipt],
    requested_turn_id: str | None = None,
) -> None:
    try:
        receipt = await action
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Stop action failed")
        if ws is None:
            return
        try:
            payload = {
                "type": "error",
                "thread_id": sid,
                "error_type": "stop_failed",
                "message": f"Stop failed: {exc}",
            }
            if requested_turn_id is not None:
                payload["turn_id"] = requested_turn_id
            await state.send_to_client(ws, payload)
        except (WebSocketDisconnect, RuntimeError, ConnectionError, AttributeError):
            pass
        return
    if ws is None:
        return
    try:
        await state.send_to_client(ws, receipt.payload())
    except (WebSocketDisconnect, RuntimeError, ConnectionError, AttributeError):
        pass


async def _handle_message(
    ws: WebSocket,
    user: dict,
    user_id: str,
    data: dict,
    *,
    admission_complete: Callable[[], None] | None = None,
) -> None:
    await _dispatch_message(
        ws,
        user,
        user_id,
        data,
        get_db_fn=get_db,
        run_in_db_fn=run_in_db,
        get_thread_for_user_fn=get_thread_for_user,
        touch_thread_fn=touch_thread,
        send_thread_input_fn=send_thread_input,
        handle_cc_main_message_fn=_handle_cc_main_message,
        handle_codex_main_message_fn=_handle_codex_main_message,
        admission_complete=admission_complete,
    )


async def _handle_cc_main_message(
    ws: WebSocket,
    session,
    thread_id: str,
    content: str,
    attachments: list,
    *,
    msg_id: str,
    now: str,
    is_retry: bool,
    is_edited: bool,
    reasoning_effort: str | None = None,
    input_id: str | None = None,
    requested_model_id: str | None = None,
    target: ExecutionTarget | None = None,
) -> None:
    await _dispatch_cc_main_message(
        ws,
        session,
        thread_id,
        content,
        attachments,
        msg_id=msg_id,
        now=now,
        is_retry=is_retry,
        is_edited=is_edited,
        reasoning_effort=reasoning_effort,
        input_id=input_id,
        requested_model_id=requested_model_id,
        target=target,
        send_thread_input_fn=send_thread_input,
        cc_attachment_block_fn=_cc_attachment_block,
    )


async def _handle_codex_main_message(
    ws: WebSocket,
    session,
    thread_id: str,
    content: str,
    *,
    msg_id: str,
    now: str,
    is_retry: bool,
    reasoning_effort: str | None = None,
    input_id: str | None = None,
    requested_model_id: str | None = None,
    target: ExecutionTarget | None = None,
) -> None:
    await _dispatch_codex_main_message(
        ws,
        session,
        thread_id,
        content,
        msg_id=msg_id,
        now=now,
        is_retry=is_retry,
        reasoning_effort=reasoning_effort,
        input_id=input_id,
        requested_model_id=requested_model_id,
        target=target,
        send_thread_input_fn=send_thread_input,
    )


def _cleanup_ws(user_id: str, ws: WebSocket) -> None:
    """Unregister WebSocket from user tracking on disconnect."""
    _cleanup_ws_impl(user_id, ws)


async def _handle_subscribe(ws: WebSocket, user: dict, thread_id: str) -> None:
    """Subscribe this ws to streaming events for ``thread_id`` and respond
    with a snapshot of the in-flight turn buffer (or null if no active turn).

    Ordering: subscribe first, then snapshot. Deltas that fire between
    subscribe and snapshot arrival reach the client with seq > snapshot.seq —
    the client dedupes/orders via seq. See infra.state.turn_buffers docstring.
    """
    await _handle_subscribe_impl(
        ws, user, thread_id, owned_thread_fn=_owned_thread,
    )


def _handle_unsubscribe(ws: WebSocket, thread_id: str) -> None:
    """Remove this ws from the session's subscriber list. Idempotent."""
    _handle_unsubscribe_impl(ws, thread_id)


async def _owned_thread(user: dict, thread_id: str):
    """Return the owned session row, or None when missing/unauthorized."""
    return await _owned_thread_impl(user, thread_id)


async def _owns_thread(user: dict, thread_id: str) -> bool:
    """True if the user owns the session (incl. delegated child sessions)."""
    return await _owned_thread(user, thread_id) is not None


def _cc_attachment_block(attachments: list, user_id: str) -> str:
    return _cc_attachment_block_impl(attachments, user_id)


async def _handle_subagent_stop(
    ws: WebSocket,
    user: dict,
    child_thread_id: str,
    expected_turn_id: str | None = None,
) -> None:
    await _handle_subagent_stop_impl(
        ws, user, child_thread_id, expected_turn_id,
        owned_thread_fn=_owned_thread,
    )
