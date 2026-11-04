"""The person's decision on a held reply.

- ``POST /api/held-replies/{id}/approve`` — deliver the stored text as its sender would have
- ``POST /api/held-replies/{id}/decline`` — deliver nothing

A decision moves the row out of ``held`` before anything is delivered, so a second decision, from
another page or a double click, gets a 409 and nothing is delivered twice. If delivery fails
unexpectedly the row goes back to ``held`` with an error line, and Approve tries again.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

import infra.held_replies as held_replies
from infra.auth import authenticate
from infra.database import get_db
from infra.db_async import run_in_db
from infra.held_reply_messages import (
    error_line, gone_line, note_asker, notify_changed, recipient_name, tell_sender,
)
from infra.queries import get_thread_for_user
from plans import public
from tools import delegate_send, delegation

router = APIRouter(prefix="/api/held-replies", tags=["held-replies"])


class DecisionBody(BaseModel):
    token: str


def _load(held_id: str, user_id: str) -> tuple[dict | None, dict | None, dict | None, str]:
    """The held row, its sender's and recipient's thread rows, and the recipient as a person reads it."""
    db = get_db()
    try:
        held = held_replies.get(held_id, user_id, db=db)
        if held is None:
            return None, None, None, ""
        recipient = get_thread_for_user(held["recipient_thread_id"], user_id, db=db)
        return (
            held,
            delegation._current_thread_context(held["sender_thread_id"]),
            None if recipient is None else dict(recipient),
            recipient_name(held["recipient_thread_id"], db=db),
        )
    finally:
        db.close()


def _settle(held_id: str, expect: str, state: str, error: str | None = None) -> bool:
    return held_replies.settle(held_id, expect=expect, state=state, error=error)


async def _claim(body: DecisionBody, held_id: str, state: str):
    user = authenticate(body.token)
    if user is None:
        raise HTTPException(401, "invalid token")
    held, sender, recipient, name = await run_in_db(_load, held_id, user["id"])
    if held is None:
        raise HTTPException(404, "Held reply not found")
    if not await run_in_db(_settle, held_id, held_replies.HELD, state):
        raise HTTPException(409, "This reply was already decided")
    return user, held, sender, recipient, name


@router.post("/{held_id}/decline")
async def decline(held_id: str, body: DecisionBody) -> dict[str, Any]:
    """Deliver nothing; the sender and the asker are told it was not sent."""
    user, held, _sender, _recipient, name = await _claim(body, held_id, held_replies.DECLINED)
    notify_changed(user["id"], held["sender_thread_id"])
    await tell_sender(held, held_replies.DECLINED, recipient=name)
    await note_asker(held, held_replies.DECLINED)
    return {"ok": True, "state": held_replies.DECLINED}


@router.post("/{held_id}/approve")
async def approve(held_id: str, body: DecisionBody) -> dict[str, Any]:
    """Deliver the stored text as its sender would have, then tell the sender and the asker."""
    user, held, sender, recipient, name = await _claim(body, held_id, held_replies.APPROVED)
    if recipient is None or sender is None:
        await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.NOT_SENT)
        notify_changed(user["id"], held["sender_thread_id"])
        await tell_sender(held, held_replies.NOT_SENT, recipient=name)
        return {"ok": True, "state": held_replies.NOT_SENT, "line": gone_line(name)}
    try:
        sent = await _deliver(held, sender, recipient)
    except Exception:
        logger.exception("held reply {}: delivery failed", held_id)
        sent = "Error: delivery failed"
    if isinstance(sent, str):
        line = error_line(name)
        await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.HELD, line)
        notify_changed(user["id"], held["sender_thread_id"])
        raise HTTPException(502, line)
    await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.SENT)
    notify_changed(user["id"], held["sender_thread_id"])
    await tell_sender(held, held_replies.SENT, recipient=name)
    await note_asker(held, "queued" if sent.get("held") else held_replies.SENT)
    return {"ok": True, "state": held_replies.SENT}


async def _deliver(held: dict, sender: dict, recipient: dict) -> dict[str, Any] | str:
    """Run the send the sender would have run, on the stored bytes."""
    kwargs = {"idempotency_key": held["idempotency_key"]}
    step = None
    requested = held["plan_node_id"]
    if held["via"] == "tool":
        try:
            step = await run_in_db(
                public.choose_reused_worker_step,
                held["sender_thread_id"], held["recipient_thread_id"], requested,
            )
        except public.PlanInputError as exc:
            return f"Error: invalid request - {exc}"
    return await delegate_send.deliver(
        step, held["text"], held["recipient_thread_id"], recipient,
        held["sender_thread_id"], sender, requested, kwargs,
    )
