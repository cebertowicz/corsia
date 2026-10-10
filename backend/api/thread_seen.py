"""Mark read / Mark unread: the user's server-side seen state for one thread.

The seen state lives on the thread's presence row (``infra.presence`` is its
only writer), so it is the same on every device. A thread the user never
opened has no row: the mark is refused rather than fabricating a visit. After
commit every device is nudged to re-read that thread's chat-list row.
"""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from infra import presence, product_events, thread_updates
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from infra.time import utc_now

router = APIRouter()


class SeenBody(BaseModel):
    unread: bool


def _mark(thread_id: str, user_id: str, unread: bool) -> str:
    db = get_db()
    try:
        thread = get_thread_for_user(thread_id, user_id, db=db)
        if thread is None or thread["purged_at"] is not None:
            return "missing"
        marked = presence.mark_seen(
            db, user_id=user_id, thread_id=thread_id, unread=unread, now=utc_now(),
        )
        return "marked" if marked else "never_opened"
    finally:
        db.close()


def _report(thread_id: str, user_id: str, unread: bool, outcome: str, reason: str | None = None) -> None:
    """Emit the one ``thread.seen_mark`` event: ids in their fields, bounded reason only."""
    properties = {"mark": "unread" if unread else "read", "outcome": outcome}
    if reason:
        properties["reason"] = reason
    product_events.emit_product_event(
        "thread", "seen_mark", thread_id=thread_id, user_id=user_id, properties=properties,
    )


def _mark_reported(thread_id: str, user_id: str, unread: bool) -> str:
    """Run the mark in the database worker and report it there, off the event loop."""
    try:
        result = _mark(thread_id, user_id, unread)
    except Exception:
        _report(thread_id, user_id, unread, "failed", "internal_error")
        raise
    if result == "marked":
        _report(thread_id, user_id, unread, "succeeded")
    else:
        _report(thread_id, user_id, unread, "refused", result)
    return result


@router.post("/threads/{thread_id}/seen")
async def mark_thread_seen(thread_id: str, body: SeenBody, user: dict = Depends(require_user)):
    """Mark one of the user's threads read (seen through now) or unread."""
    result = await run_in_db(partial(_mark_reported, thread_id, user["id"], body.unread))
    if result == "missing":
        raise HTTPException(404, "Thread not found")
    if result == "never_opened":
        raise HTTPException(409, "This thread has never been opened")
    thread_updates.notify_changed(user["id"], [thread_id])
    return {"ok": True, "unread": body.unread}
