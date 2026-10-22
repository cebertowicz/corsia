from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket
from loguru import logger

from infra import state
from infra.send_service import SendRejected
from llm.resolution import ExecutionTarget
from ws.ingress import (
    reserve_admission_receipt as _send_admission_receipt,
    send_input_accepted as _send_input_accepted,
)


async def handle_message(
    ws: WebSocket,
    user: dict,
    user_id: str,
    data: dict,
    *,
    get_db_fn: Callable[[], Any],
    run_in_db_fn: Callable[[Callable[[], Any]], Awaitable[Any]],
    get_thread_for_user_fn: Callable[..., Any],
    touch_thread_fn: Callable[..., Any],
    send_thread_input_fn: Callable[..., Awaitable[Any]],
    handle_cc_main_message_fn: Callable[..., Awaitable[None]],
    handle_codex_main_message_fn: Callable[..., Awaitable[None]],
    admission_complete: Callable[[], None] | None = None,
) -> None:
    admission_released = False

    def _release_admission() -> None:
        nonlocal admission_released
        if admission_released:
            return
        admission_released = True
        if admission_complete is not None:
            admission_complete()

    try:
        await _handle_message(
            ws,
            user,
            data,
            get_db_fn=get_db_fn,
            run_in_db_fn=run_in_db_fn,
            get_thread_for_user_fn=get_thread_for_user_fn,
            touch_thread_fn=touch_thread_fn,
            send_thread_input_fn=send_thread_input_fn,
            release_admission=_release_admission,
        )
    finally:
        _release_admission()


async def _handle_message(
    ws: WebSocket,
    user: dict,
    data: dict,
    *,
    get_db_fn: Callable[[], Any],
    run_in_db_fn: Callable[[Callable[[], Any]], Awaitable[Any]],
    get_thread_for_user_fn: Callable[..., Any],
    touch_thread_fn: Callable[..., Any],
    send_thread_input_fn: Callable[..., Awaitable[Any]],
    release_admission: Callable[[], None],
) -> None:
    thread_id = data.get("thread_id")
    input_id = data.get("input_id")
    content = data.get("content", "")
    attachments = data.get("attachments", [])
    if not isinstance(input_id, str) or not input_id.strip():
        await _send_error(ws, thread_id or "", "Missing input_id for message")
        return
    input_id = input_id.strip()
    if not thread_id or (not content and not attachments):
        await _send_error(
            ws, thread_id or "", "Malformed message input",
            input_id=input_id,
        )
        return

    def _fetch_thread_row():
        db = get_db_fn()
        thread_row = get_thread_for_user_fn(thread_id, user["id"], db=db)
        db.close()
        return thread_row

    thread_row = await run_in_db_fn(_fetch_thread_row)
    if not thread_row:
        await _send_error(ws, thread_id, "Thread not found", input_id=input_id)
        return

    await _send_native_input(
        ws, user, thread_id, input_id, content, attachments, thread_row, data,
        run_in_db_fn=run_in_db_fn,
        touch_thread_fn=touch_thread_fn,
        send_thread_input_fn=send_thread_input_fn,
        release_admission=release_admission,
    )


async def _send_native_input(
    ws: WebSocket, user: dict, thread_id: str, input_id: str,
    content: str, attachments: list, thread_row: Any, data: dict,
    *,
    run_in_db_fn: Callable[[Callable[[], Any]], Awaitable[Any]],
    touch_thread_fn: Callable[..., Any],
    send_thread_input_fn: Callable[..., Awaitable[Any]],
    release_admission: Callable[[], None],
) -> None:
    is_retry = data.get("is_retry", False)
    requested_model = (data.get("model_id") or "").strip()
    effective_effort = data.get("reasoning_effort") or (
        thread_row["reasoning_effort"] if "reasoning_effort" in thread_row.keys() else None
    )
    # Queue intent is trusted only from the protocol field, never caller metadata.
    delivery_intent = (
        "queue_after_turn"
        if data.get("delivery_intent") == "queue_after_turn"
        else "apply_now"
    )
    metadata: dict = {}
    if attachments:
        metadata["attachments"] = attachments
    if data.get("edited", False):
        metadata["edited"] = True
    if "viewing_document" in data:
        metadata["viewing_document"] = data["viewing_document"]
    if "reply_context" in data:
        metadata["reply_context"] = data["reply_context"]

    async def _after_persist_before_dispatch(_final_target: ExecutionTarget) -> None:
        if is_retry:
            def _touch_retry():
                touch_thread_fn(thread_id)

            await run_in_db_fn(_touch_retry)

        # The edited input is the first valid provider-prefix divergence.

    admission_receipt_sent = False

    async def _on_admitted(result: Any) -> None:
        nonlocal admission_receipt_sent
        admission_receipt_sent = True
        try:
            _send_admission_receipt(ws, thread_id, input_id, result)
        finally:
            release_admission()

    try:
        result = await send_thread_input_fn(
            thread_id, thread_row, content,
            user=user, metadata=metadata or None,
            delivery_intent=delivery_intent, input_id=input_id,
            persist_input=not is_retry, broadcast_input=True,
            touch_thread_on_insert=True, trim_content=False,
            allow_empty_content=bool(attachments), native_mode="ws", ws=ws,
            requested_model_id=requested_model or None, attachments=attachments,
            is_retry=is_retry, reasoning_effort=effective_effort,
            after_persist_before_dispatch=_after_persist_before_dispatch,
            broadcast_exclude_ws=ws,
            accepted_effects={
                "title": not is_retry,
                "uploads": bool(attachments),
            },
            on_admitted=_on_admitted,
        )
        if not admission_receipt_sent:
            await _send_input_accepted(ws, thread_id, input_id, result)
            release_admission()
    except SendRejected as e:
        logger.warning("Native WebSocket send rejected for {}: {}", thread_id, e.detail)
        await _send_error(
            ws, thread_id, e.detail, metadata=e.metadata, input_id=input_id,
        )
    except Exception as e:
        logger.error(f"Native WebSocket turn failed for {thread_id}: {e}")
        await _send_error(ws, thread_id, f"Turn failed: {e}", input_id=input_id)


async def handle_cc_main_message(
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
    delivery_intent: str = "apply_now",
    send_thread_input_fn: Callable[..., Awaitable[Any]],
    cc_attachment_block_fn: Callable[[list, str], str],
) -> None:
    metadata: dict = {}
    if attachments:
        metadata["attachments"] = attachments
    if is_edited:
        metadata["edited"] = True

    try:
        # #504: broadcast the inbound row to OTHER subscribed clients (e.g. a
        # second device on this thread) while excluding the sender's own socket
        # so it isn't duplicated against the optimistic render. Same contract as
        # the native path (handle_native_ws_message). Do NOT flip back to
        # broadcast_input=False — that skips cross-device viewers.
        result = await send_thread_input_fn(
            thread_id,
            session,
            content,
            metadata=metadata or None,
            delivery_intent=delivery_intent,
            input_id=input_id,
            attachments=attachments,
            persist_input=not is_retry,
            broadcast_input=True,
            touch_thread_on_insert=True,
            trim_content=False,
            allow_empty_content=bool(attachments),
            native_mode="delegation",
            ws=ws,
            requested_model_id=requested_model_id,
            target=target,
            reasoning_effort=reasoning_effort,
            broadcast_exclude_ws=ws,
            accepted_effects={
                "title": not is_retry,
                "uploads": bool(attachments),
            },
        )
        if input_id is not None:
            await _send_input_accepted(ws, thread_id, input_id, result)
    except SendRejected as e:
        logger.warning("CC main-chat turn rejected for {}: {}", thread_id, e.detail)
        await _send_error(
            ws,
            thread_id,
            e.detail,
            error_type="harness_error",
            harness="claude-code",
            metadata=e.metadata,
            input_id=input_id,
        )
        return
    except Exception as e:
        logger.error(f"CC main-chat turn failed for {thread_id}: {e}")
        await _send_error(
            ws,
            thread_id,
            f"Claude Code turn failed: {e}",
            error_type="harness_error",
            harness="claude-code",
            input_id=input_id,
        )
        return

async def handle_codex_main_message(
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
    delivery_intent: str = "apply_now",
    send_thread_input_fn: Callable[..., Awaitable[Any]],
) -> None:
    try:
        # #504: cross-device live sync — broadcast the inbound row to other
        # subscribed clients, excluding the sender's socket (no self-duplicate).
        # Same contract as the native path; do NOT revert to broadcast_input=False.
        result = await send_thread_input_fn(
            thread_id,
            session,
            content,
            delivery_intent=delivery_intent,
            input_id=input_id,
            persist_input=not is_retry,
            broadcast_input=True,
            touch_thread_on_insert=True,
            trim_content=False,
            native_mode="delegation",
            ws=ws,
            requested_model_id=requested_model_id,
            target=target,
            reasoning_effort=reasoning_effort,
            broadcast_exclude_ws=ws,
            accepted_effects={"title": not is_retry},
        )
        if input_id is not None:
            await _send_input_accepted(ws, thread_id, input_id, result)
    except SendRejected as e:
        await _send_error(
            ws,
            thread_id,
            e.detail,
            error_type="harness_error",
            harness="codex",
            metadata=e.metadata,
            input_id=input_id,
        )
        return
    except Exception as e:
        logger.error(f"Codex main-chat turn failed for {thread_id}: {e}")
        await _send_error(
            ws,
            thread_id,
            f"Codex turn failed: {e}",
            error_type="harness_error",
            harness="codex",
            input_id=input_id,
        )
        return

async def _send_error(
    ws: WebSocket,
    thread_id: str,
    message: str,
    *,
    error_type: str | None = None,
    harness: str | None = None,
    metadata: dict | None = None,
    input_id: str | None = None,
) -> None:
    try:
        payload = {
            "type": "error",
            "thread_id": thread_id,
            "message": message,
        }
        if error_type:
            payload["error_type"] = error_type
        if harness:
            payload["harness"] = harness
            payload["event_type"] = "harness.error"
        if metadata:
            payload.update(metadata)
        if input_id:
            payload["input_id"] = input_id
        await state.send_to_client(ws, payload)
    except (RuntimeError, ConnectionError, AttributeError):
        pass
