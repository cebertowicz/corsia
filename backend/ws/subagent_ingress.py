"""Bind the child composer protocol to the endpoint's current scoped dependencies."""
from __future__ import annotations

from fastapi import WebSocket

from ws.subagent_control import handle_subagent_feed


async def dispatch_subagent_feed(
    socket: WebSocket,
    user: dict,
    child_thread_id: str,
    content: str,
    attachments: list | None = None,
    is_edited: bool = False,
    input_id: str | None = None,
    delivery_intent: str | None = None,
    viewing_document: dict | None = None,
) -> None:
    # Endpoint dependencies stay late-bound for the same live/scoped routing
    # contract the other endpoint adapters use.
    from ws import _owned_thread, logger, send_thread_input

    await handle_subagent_feed(
        socket, user, child_thread_id, content, attachments, is_edited, input_id,
        delivery_intent or "apply_now", viewing_document,
        owned_thread_fn=_owned_thread, send_thread_input_fn=send_thread_input, log=logger,
    )
