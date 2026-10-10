"""A thread's plan update cards and its "Since your last visit" divider.

Cards are read-only: each is the title snapshotted when the plan outcome
happened plus that outcome, never a raw reason, evidence, feedback or note
(AS17). Reading them changes no seen state and resolves no owed item (AS11).
The divider is read once per open, from the committed presence row before
this visit's own entry commits; the client keeps it for the rest of the visit.
"""

from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query

from api.thread_cards import hydrate_update_asks
from infra import thread_updates
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user

router = APIRouter()


def _read(thread_id: str, user_id: str, from_seq: int, divider: bool) -> dict | None:
    db = get_db()
    try:
        thread = get_thread_for_user(thread_id, user_id, db=db)
        if thread is None or thread["purged_at"] is not None:
            return None
        cards, has_more = thread_updates.read_newest_cards(db, thread_id, min_seq=from_seq)
        hydrate_update_asks(db, cards)
        return {
            "cards": cards,
            # The fixed window omits only older cards, never a forward page.
            "has_more": has_more,
            "divider": thread_updates.visit_divider(db, user_id, thread_id) if divider else None,
        }
    finally:
        db.close()


@router.get("/threads/{thread_id}/updates")
async def read_thread_update_cards(
    thread_id: str,
    from_seq: int = Query(0, ge=0),
    divider: bool = False,
    user: dict = Depends(require_user),
):
    """The newest bounded card window from ``from_seq``, oldest first."""
    result = await run_in_db(partial(_read, thread_id, user["id"], from_seq, divider))
    if result is None:
        raise HTTPException(404, "Thread not found")
    return result
