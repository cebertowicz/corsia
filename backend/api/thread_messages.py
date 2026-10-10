"""Session message actions, paging, and tool-result routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from infra.auth import authenticate
from infra.database import get_db
from infra.db_async import run_in_db, run_read
from infra.queries import get_thread_for_user
from api.thread_inbox_actions import (
    retry_thread_delivery_now,
    run_inbox_message_action,
)
from api.thread_focus_page import (
    InvertedRange,
    read_authorized_focus_page,
    read_authorized_run_range,
)
from api.thread_paging import (
    CursorNotFound,
    load_tool_result,
    read_authorized_page_with_summaries,
)

router = APIRouter(prefix="/api", tags=["threads"])

@router.post("/threads/{thread_id}/messages/{message_id}/cancel")
async def cancel_inbox_message(thread_id: str, message_id: str, token: str = ""):
    """Cancel an unclaimed queued message without deleting the user's thought."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    return await run_inbox_message_action(
        thread_id,
        message_id,
        user["id"],
        action="cancel",
    )


@router.post("/threads/{thread_id}/messages/{message_id}/retry")
async def retry_inbox_message(
    thread_id: str,
    message_id: str,
    token: str = "",
):
    """Re-probe a waiting thread now rather than sitting out its backoff.

    The queued head is what delivers, so this performs no message transition,
    but the named message is verified: it must exist in this thread and still
    be waiting, so an unknown or already-ended id cannot report success.
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    return await retry_thread_delivery_now(thread_id, message_id, user["id"])


@router.get("/threads/{thread_id}/messages")
async def get_messages(
    thread_id: str,
    token: str,
    limit: int | None = None,
    before: str | None = None,
    view: str | None = None,
    from_id: str | None = None,
    to_id: str | None = None,
):
    """Fetch one display page of messages for a session (cursor pagination).

    - limit: max DISPLAY messages to return (after tool-row collapse). When
      omitted without `before`, the full transcript is returned. `before`
      requires `limit`.
    - before: message-ID cursor — return display messages strictly older than
      this one. An unknown cursor id returns an empty page (spec §3.6), not 404.

    Returns: {messages, has_more, oldest_id}. `has_more` is true when raw rows
    older than the oldest returned message exist — i.e. a scroll-up can fetch
    another page. `oldest_id` is the cursor the frontend sends for that page.
    """
    before = before or None  # treat empty string as absent (aligns with page_messages truthiness check)
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if before is not None and (limit is None or limit < 1):
        raise HTTPException(400, detail="`before` cursor requires `limit` ≥ 1 (spec §1.8)")

    if view not in (None, "focus"):
        raise HTTPException(400, detail="`view` must be `focus` when given")
    if (from_id is None) != (to_id is None):
        raise HTTPException(400, detail="`from_id` and `to_id` are given together")
    if (view or from_id) and (limit is None or limit < 1):
        raise HTTPException(400, detail="`view` and `from_id` require `limit` ≥ 1")
    if view and from_id:
        raise HTTPException(400, detail="`view` and `from_id` are separate reads")

    def _query():
        db = get_db()
        try:
            if from_id is not None and to_id is not None:
                return read_authorized_run_range(
                    db, thread_id, limit, from_id, to_id, before, user_id=user["id"],
                )
            if view == "focus":
                return read_authorized_focus_page(
                    db, thread_id, limit, before, user_id=user["id"],
                )
            # The ownership check shares the page read's snapshot, so a thread
            # this user loses in between cannot be paginated from an older one.
            return read_authorized_page_with_summaries(
                db, thread_id, limit, before, user_id=user["id"],
            )
        finally:
            db.close()

    try:
        result = await run_read(_query)
    except InvertedRange as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except CursorNotFound:
        # Unknown `before` cursor → empty page (frontend reads this as
        # "no more older messages"), NOT a 404. Spec §3.6.
        return {"messages": [], "has_more": False, "oldest_id": None, "turn_summaries": {}}
    if result is None:
        raise HTTPException(404)
    if isinstance(result, dict):
        return result

    display_msgs, has_more, oldest_id, turn_summaries = result
    return {
        "messages": display_msgs,
        "has_more": has_more,
        "oldest_id": oldest_id,
        "turn_summaries": turn_summaries,
    }


@router.get(
    "/threads/{thread_id}/messages/{message_id}/tool-results/{tool_call_id}"
)
async def get_tool_result(
    thread_id: str,
    message_id: str,
    tool_call_id: str,
    token: str,
):
    """Fetch one click-gated tool result from an authorized session."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _query():
        db = get_db()
        try:
            if not get_thread_for_user(thread_id, user["id"], db=db):
                return None
            return load_tool_result(db, thread_id, message_id, tool_call_id)
        finally:
            db.close()

    result = await run_in_db(_query)
    if result is None:
        raise HTTPException(404)
    return {"result": result}
