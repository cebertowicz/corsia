"""WebSocket turn helpers."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, NamedTuple

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from infra import state
from infra.config import DOCUMENT_MIME_TYPES
from infra.state.ws import CROSS_TAB_EVENTS
from infra.time import utc_now
from core.turn_core import (
    TurnIdentity,
    settle_owned_turn_terminal,
    terminal_error_detail,
)
from core.turn_iteration import TurnProviderError
from core.native_turn_runtime import retire_failed_native_runtime


_STREAMING_EVENTS = frozenset(
    {
        "text_delta",
        "step_reasoning",
        "step_text",
        "tool_start",
        "tool_done",
    }
)


class NativeTurnRequest(NamedTuple):
    """Immutable inputs shared by the Native Turn execution phases."""

    ws: Any
    user: dict
    user_id: str
    thread_id: str
    thread_row: object
    content: str
    attachments: list[dict]
    is_retry: bool
    target: Any
    turn_id: str
    ingress: str
    reasoning_effort: str | None
    start_ack: asyncio.Future | None
    broadcast: Callable[[dict], Awaitable[None]] | None
    step_metadata: dict | None
    persist_input_once: Callable[[], Awaitable[None]] | None
    settle_delivery: Callable[[], Awaitable[None]] | None
    after_persist_before_dispatch: Callable[[Any], Awaitable[None]] | None
    prepared_context: Any
    preadmitted_turn: TurnIdentity | None
    provider_acceptance: asyncio.Future[bool] | None
    settle_delivery_terminal: (
        Callable[[str, str | BaseException | None], Awaitable[None]] | None
    )
    media_projection: Any
    person_send: bool = False
    input_ids: tuple[str, ...] = ()


def resolve_future(future: asyncio.Future | None, value: Any) -> None:
    if future is not None and not future.done():
        future.set_result(value)


def reject_future(future: asyncio.Future | None, error: BaseException) -> None:
    if future is not None and not future.done():
        future.set_exception(error)


async def settle_preadmitted_failure(
    request: NativeTurnRequest,
    error: BaseException,
    identity: TurnIdentity,
    *,
    release: Callable[[], None] | None = None,
) -> None:
    """Settle failed delivery and terminalize its preadmitted turn in order."""
    outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
    detail = None if outcome == "cancelled" else terminal_error_detail(error)
    terminal_failure: BaseException | None = None
    try:
        if request.settle_delivery_terminal is not None:
            await request.settle_delivery_terminal(outcome, detail)
    except BaseException as receipt_error:
        terminal_failure = receipt_error
    try:
        await settle_owned_turn_terminal(identity.turn_id, outcome, detail, release=release)
    except BaseException as settlement_error:
        terminal_failure = terminal_failure or settlement_error
    if terminal_failure is not None:
        resolve_future(request.provider_acceptance, False)
        reject_future(request.start_ack, error)
        raise terminal_failure from error


def build_agent_content(content: str, attachments: list[dict]) -> tuple[str, list[str]]:
    """Build attachment notes; validated image bytes travel separately."""
    if not attachments:
        return content, []

    image_paths: list[str] = []
    attachment_notes: list[str] = []

    for att in attachments:
        mime = att.get("mime_type")
        fname = att.get("filename", "file")
        mime = mime if isinstance(mime, str) else ""

        if mime.startswith("image/"):
            attachment_notes.append(f"[Image attached: {fname}]")
        elif mime in DOCUMENT_MIME_TYPES:
            attachment_notes.append(f"[Document attached: {fname}]")
        else:
            attachment_notes.append(f"[File attached: {fname}]")

    if attachment_notes:
        agent_content = (
            (content + "\n\n" + "\n".join(attachment_notes))
            if content
            else "\n".join(attachment_notes)
        )
    else:
        agent_content = content

    return agent_content, image_paths


def make_deferred_send() -> tuple[
    Callable[[dict], Awaitable[None]],
    list[Callable[[dict], Awaitable[None]] | None],
]:
    """Create a broadcaster that becomes usable only after input acceptance."""
    send_cell: list[Callable[[dict], Awaitable[None]] | None] = [None]

    async def _send(message: dict) -> None:
        send = send_cell[0]
        if send is None:
            raise RuntimeError("native broadcaster used before Turn acceptance")
        await send(message)

    return _send, send_cell


def record_browser_turn_activity(
    thread_id: str,
    user_id: str,
    model_id: str,
) -> None:
    """Record the active browser/model projection for one admitted turn."""
    now = utc_now()
    state._browser_thread_last_activity[f"web:{user_id}:{thread_id}"] = now
    state._active_thread_models[thread_id] = {
        "model_id": model_id,
        "last_active": now,
        "ws_connected": True,
    }


async def send_turn_error(
    ws: WebSocket,
    ws_send: Callable[[dict], Awaitable[None]] | None,
    thread_id: str,
    identity: TurnIdentity | None,
    error: Exception,
) -> None:
    """Best-effort presentation of a nonterminal browser turn failure."""
    frame = {"type": "error", "thread_id": thread_id, "message": str(error)}
    if identity is not None:
        frame.update(identity.frame_fields())
    try:
        await (ws_send or ws.send_json)(frame)
    except (WebSocketDisconnect, RuntimeError, ConnectionError) as send_error:
        logger.debug(
            "ws.turn: error-frame send failed for {}: {}",
            thread_id,
            send_error,
        )


def emit_terminal_product_event(
    request: NativeTurnRequest,
    identity: TurnIdentity,
    model: str,
    cancelled: bool,
    started_at: Any,
    emit: Callable[..., None],
) -> None:
    """Record the product terminal for one exact Native Turn."""
    duration_ms = int((utc_now() - started_at).total_seconds() * 1000)
    emit(
        "turn",
        "cancelled" if cancelled else "completed",
        thread_id=request.thread_id,
        user_id=request.user["id"],
        properties={
            "turn_id": identity.turn_id,
            "model": model,
            "duration_ms": duration_ms,
        },
    )


def _emit_error_product_event(
    request: NativeTurnRequest,
    identity: TurnIdentity | None,
    error: Exception,
    emit: Callable[..., None],
) -> None:
    emit(
        "turn",
        "errored",
        thread_id=request.thread_id,
        user_id=request.user["id"],
        properties={
            "turn_id": identity.turn_id if identity else None,
            "error_type": type(error).__name__,
            "error_message": str(error)[:200],
        },
    )


async def handle_turn_error(
    request: NativeTurnRequest,
    identity: TurnIdentity | None,
    cancel_event: Any | None,
    ws_send: Callable[[dict], Awaitable[None]] | None,
    product_terminal: bool,
    error: Exception,
    emit: Callable[..., None],
) -> bool | None:
    """Clean up and present one non-cancellation execution failure."""
    resolve_future(request.provider_acceptance, False)
    failed_before_ack = request.start_ack is not None and not request.start_ack.done()
    reject_future(request.start_ack, error)
    if request.ingress != "event":
        import traceback

        traceback.print_exc()
    retire_failed_native_runtime(
        request.thread_id,
        request.ingress,
        cancel_event,
        identity,
    )
    if request.ingress != "event" or (identity is not None and not product_terminal):
        _emit_error_product_event(request, identity, error, emit)
    if request.ingress == "event" or failed_before_ack:
        return None
    if isinstance(error, TurnProviderError) or getattr(
        error,
        "_corsia_terminal_effect_emitted",
        False,
    ):
        return False
    await send_turn_error(request.ws, ws_send, request.thread_id, identity, error)
    return False


def _update_turn_buffer(
    thread_id: str,
    identity: TurnIdentity,
    data: dict,
) -> None:
    """Apply one streaming event to the exact Turn buffer."""
    from infra.state import turn_buffers

    msg_type = data.get("type")
    if msg_type == "step_reasoning":
        seq = turn_buffers.append_reasoning(
            thread_id,
            identity,
            data.get("content", ""),
        )
    elif msg_type == "text_delta":
        seq = turn_buffers.append_content(
            thread_id,
            identity,
            data.get("content", ""),
        )
    elif msg_type == "step_text":
        finalized = data.get("finalized_tool_ids")
        seq = turn_buffers.finalize_step(
            thread_id,
            identity,
            finalized if isinstance(finalized, list) else [],
        )
    else:
        seq = _update_tool_buffer(thread_id, identity, data)
    if not seq:
        return
    if msg_type == "step_text":
        data["stream_seq"] = seq
    else:
        data["seq"] = seq


def _update_tool_buffer(
    thread_id: str,
    identity: TurnIdentity,
    data: dict,
) -> int | None:
    """Apply a tool event, or advance the buffer for another stream event."""
    from infra.state import turn_buffers

    buffer = turn_buffers.get(thread_id, identity)
    if buffer is None:
        return None
    if data.get("type") == "tool_start":
        buffer.active_tools.append(
            {
                "id": data.get("tool_id", ""),
                "name": data.get("name", ""),
                "hint": data.get("hint", ""),
                "phrase": data.get("phrase") or None,
                "object": data.get("object") or None,
                "status": "running",
                "args": data.get("args") or None,
            }
        )
    elif data.get("type") == "tool_done":
        for tool in buffer.active_tools:
            if tool.get("id") != data.get("tool_id"):
                continue
            tool["status"] = "done"
            if data.get("result") is not None:
                tool["result"] = data.get("result")
            for key in ("phrase", "object", "outcome", "error_line"):
                if data.get(key):
                    tool[key] = data[key]
            break
    buffer.seq += 1
    return buffer.seq


def make_ws_send(
    ws: WebSocket,
    user_id: str,
    thread_id: str,
    identity: TurnIdentity,
) -> tuple[Callable[[dict], Awaitable[None]], list[bool]]:
    """Create this turn's broadcast function and disconnect flag."""
    ws_closed = [False]

    async def _ws_send(data: dict) -> None:
        msg_type = data.get("type")
        if msg_type in _STREAMING_EVENTS or msg_type in {
            "turn_complete",
            "turn_cancelled",
            "turn_interrupted",
            "error",
        }:
            data.update(identity.frame_fields())

        if msg_type in _STREAMING_EVENTS:
            _update_turn_buffer(thread_id, identity, data)
            state.broadcast_to_thread(thread_id, data)
            return

        if msg_type == "turn_activity":
            data.update(identity.frame_fields())
            state.broadcast_to_user(user_id, data)
            return

        if msg_type in CROSS_TAB_EVENTS:
            state.broadcast_to_user(user_id, data)
            return

        if not ws_closed[0]:
            try:
                await state.send_to_client(ws, data)
            except (RuntimeError, WebSocketDisconnect, Exception) as e:
                if (
                    "close" in str(e).lower()
                    or "disconnect" in str(e).lower()
                    or isinstance(e, WebSocketDisconnect)
                ):
                    ws_closed[0] = True
                    logger.info(
                        f"WS disconnected during turn for session {thread_id}, turn continues in background"
                    )
                else:
                    raise

    return _ws_send, ws_closed
