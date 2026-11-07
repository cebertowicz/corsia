from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import WebSocket
from loguru import logger as default_logger

from infra import state
from infra.send_service import SendRejected
from llm.resolution import ModelUnavailableError
from ws.message_dispatch import _send_error, _send_input_accepted


OwnedThread = Callable[[dict, str], Awaitable[object | None]]


class _FeedReceipt:
    def __init__(self, ws: WebSocket, thread_id: str, input_id: str):
        self.ws, self.thread_id, self.input_id = ws, thread_id, input_id
        self.sent = False

    async def publish_held(self, result) -> None:
        # A held input must be acknowledged before its usage control read.
        # Non-held admission is provisional; delivery owns its final receipt.
        if result.held is not None or result.admission_hold is not None:
            await self.publish(result)

    async def publish(self, result) -> None:
        if not self.sent:
            await _send_input_accepted(self.ws, self.thread_id, self.input_id, result)
            self.sent = True


async def _deliver_subagent_input(
    ws: WebSocket, child_thread_id: str, session: object, content: str,
    attachments: list | None, is_edited: bool, input_id: str,
    delivery_intent: str, send_thread_input_fn: Callable,
    provider_name: str, log, viewing_document: dict | None, reply_context: object,
) -> str | None:
    metadata: dict = {}
    if attachments:
        metadata["attachments"] = attachments
    if is_edited:
        metadata["edited"] = True
    if viewing_document is not None:
        metadata["viewing_document"] = viewing_document
    if reply_context is not None:
        metadata["reply_context"] = reply_context
    receipt = _FeedReceipt(ws, child_thread_id, input_id)
    try:
        result = await send_thread_input_fn(
            child_thread_id,
            session,
            content,
            metadata=metadata or None,
            delivery_intent=delivery_intent,
            input_id=input_id,
            attachments=attachments or [],
            broadcast_input=False,
            touch_thread_on_insert=True,
            trim_content=False,
            native_mode="delegation",
            human_origin=True,
            on_admitted=receipt.publish_held,
        )
        await receipt.publish(result)
        return None
    except SendRejected as exc:
        return exc.detail
    except Exception as exc:
        log.error(
            "{} subagent feed failed for {}: {}",
            provider_name, child_thread_id, exc,
        )
        return f"{provider_name} turn failed: {exc}"


async def handle_subagent_feed(
    ws: WebSocket,
    user: dict,
    child_thread_id: str,
    content: str,
    attachments: list | None = None,
    is_edited: bool = False,
    input_id: str | None = None,
    delivery_intent: str = "apply_now",
    viewing_document: dict | None = None,
    reply_context: object = None,
    *,
    owned_thread_fn: OwnedThread,
    send_thread_input_fn: Callable,
    log=default_logger,
) -> None:
    if not isinstance(input_id, str) or not input_id.strip():
        await _send_error(
            ws, child_thread_id or "", "Missing input_id for subagent input",
        )
        return
    input_id = input_id.strip()
    if delivery_intent not in {"apply_now", "queue_after_turn"}:
        delivery_intent = "apply_now"
    from infra.reply_context import has_comment_candidates

    if not child_thread_id or (not content and not has_comment_candidates(reply_context)):
        await _send_error(
            ws, child_thread_id or "", "Malformed subagent input",
            input_id=input_id,
        )
        return
    session = await owned_thread_fn(user, child_thread_id)
    if session is None:
        await _send_error(
            ws, child_thread_id, "Thread not found", input_id=input_id,
        )
        return

    from infra.execution_class import execution_class, is_subagent
    error_message = "This conversation can't be continued as a subagent."
    try:
        klass = execution_class(session)
    except ModelUnavailableError as exc:
        klass = None
        error_message = f"{exc.reason}. Choose another model."
    provider_name = {
        "cc_sub": "Claude Code",
        "codex_sub": "Codex",
        # ``native`` also classifies main threads; only a subagent takes this feed.
        "native": "Model" if is_subagent(session) else None,
    }.get(klass)
    if provider_name is not None:
        error_message = await _deliver_subagent_input(
            ws, child_thread_id, session, content, attachments, is_edited,
            input_id, delivery_intent, send_thread_input_fn, provider_name, log,
            viewing_document, reply_context,
        )
        if error_message is None:
            return

    try:
        await state.send_to_client(ws, {
            "type": "error", "thread_id": child_thread_id,
            "message": error_message,
            "input_id": input_id,
        })
    except (RuntimeError, ConnectionError, AttributeError):
        pass


async def handle_subagent_stop(
    ws: WebSocket,
    user: dict,
    child_thread_id: str,
    expected_turn_id: str | None,
    *,
    owned_thread_fn: OwnedThread,
) -> None:
    """Return the same exact-owner Stop receipt as a main conversation."""
    from infra import state
    from ws.stop_control import StopReceipt, do_stop

    turn_event = state._active_turns.get(child_thread_id)
    llm_task = state._active_llm_tasks.get(child_thread_id)
    try:
        if not child_thread_id or not expected_turn_id:
            receipt = StopReceipt(child_thread_id or "", "no_active_turn")
        else:
            receipt = await do_stop(
                user,
                child_thread_id,
                turn_event,
                llm_task,
                expected_turn_id,
                owns_thread_fn=owned_thread_fn,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        default_logger.exception("Subagent Stop action failed")
        try:
            await state.send_to_client(ws, {
                "type": "error",
                "thread_id": child_thread_id,
                "error_type": "stop_failed",
                "turn_id": expected_turn_id,
                "message": f"Stop failed: {exc}",
            })
        except (RuntimeError, ConnectionError, AttributeError):
            pass
        return
    try:
        await state.send_to_client(ws, receipt.payload())
    except (RuntimeError, ConnectionError, AttributeError):
        pass
