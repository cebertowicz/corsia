"""The person's decision on a held reply.

- ``POST /api/held-replies/{id}/approve`` — deliver the stored text as its sender would have
- ``POST /api/held-replies/{id}/decline`` — deliver nothing

A decision moves the row out of ``held`` before anything is delivered, so a second decision, from
another page or a double click, gets a 409 and nothing is delivered twice. If delivery fails
unexpectedly the row goes back to ``held`` with an error line, and Approve tries again.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

import infra.held_replies as held_replies
from infra.auth import authenticate, is_person_session
from infra.database import get_db
from infra.db_async import run_in_db
from infra.failure_reference import new_error_reference
from infra.held_reply_messages import (
    SCOPE_NOT_SENT, error_line, gone_line, note_asker,
    notify_hold_changed, recipient_name, scope_line, tell_sender,
)
from infra.queries import get_thread_for_user
from plans import public
from tools import delegate_send, delegation
from tools.delegation_send_context import fetch_send_target, native_send_scope_error

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


def _settle(held_id: str, expect: str, state: str, error: str | None = None, error_ref: str | None = None) -> bool:
    return held_replies.settle(held_id, expect=expect, state=state, error=error, error_ref=error_ref)


async def _claim(body: DecisionBody, held_id: str, state: str):
    user = authenticate(body.token)
    if user is None:
        raise HTTPException(401, "invalid token")
    if not is_person_session(user):
        raise HTTPException(403, "This change needs the signed-in person: open CORSIA and make it there.")
    held, sender, recipient, name = await run_in_db(_load, held_id, user["id"])
    if held is None:
        raise HTTPException(404, "Held reply not found")
    if not await run_in_db(_settle, held_id, held_replies.HELD, state):
        raise HTTPException(409, "This reply was already decided")
    return user, held, sender, recipient, name


@router.post("/{held_id}/decline")
async def decline(held_id: str, body: DecisionBody) -> dict[str, Any]:
    """Deliver nothing; the sender and the asker are told it was not sent."""
    return await asyncio.shield(_claim_and_decline(held_id, body))


async def _claim_and_decline(held_id: str, body: DecisionBody) -> dict[str, Any]:
    _user, held, _sender, _recipient, name = await _claim(body, held_id, held_replies.DECLINED)
    await notify_hold_changed(held)
    await tell_sender(held, held_replies.DECLINED, recipient=name)
    await note_asker(held, held_replies.DECLINED)
    return {"ok": True, "state": held_replies.DECLINED}


@router.post("/{held_id}/approve")
async def approve(held_id: str, body: DecisionBody) -> Any:
    """Deliver the stored text as its sender would have, then tell the sender and the asker."""
    return await asyncio.shield(_claim_and_approve(held_id, body))


async def _claim_and_approve(held_id: str, body: DecisionBody) -> Any:
    user, held, sender, recipient, name = await _claim(body, held_id, held_replies.APPROVED)
    return await _finish_approval(user, held, sender, recipient, name)


async def _finish_approval(user: dict, held: dict, sender: dict | None, recipient: dict | None, name: str) -> Any:
    held_id = held["id"]
    error_ref = None
    if recipient is None or sender is None:
        await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.NOT_SENT)
        await notify_hold_changed(held)
        await tell_sender(held, held_replies.NOT_SENT, recipient=name)
        return {"ok": True, "state": held_replies.NOT_SENT, "line": gone_line(name)}
    try:
        target = await run_in_db(fetch_send_target, held["recipient_thread_id"], user["id"], sender.get("project_id"))
        if target is None:
            await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.NOT_SENT)
            await notify_hold_changed(held)
            await tell_sender(held, held_replies.NOT_SENT, recipient=name)
            return {"ok": True, "state": held_replies.NOT_SENT, "line": gone_line(name)}
        scope_error = native_send_scope_error(
            current=sender, target_payload=target, from_thread_id=held["sender_thread_id"],
            target_thread_id=held["recipient_thread_id"], requested_scope_id=None,
            backend_bound=held["via"] == "tool",
        )
        if scope_error:
            await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.NOT_SENT, SCOPE_NOT_SENT)
            await notify_hold_changed(held)
            await tell_sender(held, SCOPE_NOT_SENT, recipient=name)
            await note_asker(held, SCOPE_NOT_SENT)
            return {"ok": True, "state": held_replies.NOT_SENT, "line": scope_line(name)}
        sent = await _deliver(held, sender, target["thread_row"])
    except HTTPException as exc:
        sent = f"Error: {exc.detail}"
    except Exception as exc:
        error_ref = new_error_reference()
        logger.exception("held reply {}: delivery failed (reference {}): {}", held_id, error_ref, exc)
        sent = "Error: delivery failed"
    if isinstance(sent, str):
        error_ref = error_ref or new_error_reference()
        logger.warning("held reply {}: delivery refused (reference {}): {}", held_id, error_ref, sent)
        line = error_line(name)
        await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.HELD, line, error_ref)
        await notify_hold_changed(held)
        return JSONResponse(status_code=502, content={"detail": line, "ref": error_ref})
    await run_in_db(_settle, held_id, held_replies.APPROVED, held_replies.SENT)
    await notify_hold_changed(held)
    await tell_sender(held, held_replies.SENT, recipient=name)
    # Delivered as any send is: a recipient held on its account limit shows that hold itself.
    await note_asker(held, held_replies.SENT)
    return {"ok": True, "state": held_replies.SENT}


async def _deliver(held: dict, sender: dict, recipient: dict) -> dict[str, Any] | str:
    """Run the send the sender would have run, on the stored bytes."""
    kwargs = {"idempotency_key": held["idempotency_key"] or f"held-reply:{held['id']}"}
    if held["via"] == "api":
        from api.delegation import _delegated_target, deliver_send

        target = await run_in_db(_delegated_target, held["recipient_thread_id"], held["user_id"])
        if target is None:
            return "Error: recipient is unavailable"
        metadata = {"sender_kind": "agent", "from_thread": held["sender_thread_id"],
                    "from_title": sender.get("title") or "", "scope": sender.get("project_id") or "workspace"}
        return await deliver_send(held["recipient_thread_id"], target, held["text"],
                                  {"id": held["user_id"]}, sender, metadata, kwargs["idempotency_key"])
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
