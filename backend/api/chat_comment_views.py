"""Record when a person has seen a chat-comment answer."""

from fastapi import APIRouter, Depends, HTTPException

from infra.auth import owns_thread, require_user_header_only
from infra.chat_comment_answers import has_comment_cid
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db

router = APIRouter(prefix="/api", tags=["chat-comments"])


def _stamp(user_id: str, thread_id: str, message_id: str, cid: str) -> bool:
    db = get_db()
    try:
        with write_transaction(db, scope="chat_comment_view"):
            if not owns_thread(thread_id, user_id, db=db):
                return False
            row = db.execute(
                "SELECT metadata FROM messages WHERE id=? AND thread_id=? AND role='user'",
                (message_id, thread_id),
            ).fetchone()
            if row is None or not has_comment_cid(row[0], cid):
                return False
            # Stamp the newest answer this view could see, never the clock: an answer
            # timed before its commit would otherwise read as seen.
            db.execute(
                "INSERT INTO chat_comment_views "
                "SELECT ?, ?, ?, MAX(answered_at) FROM chat_comment_answers WHERE user_message_id=? AND cid=? "
                "HAVING MAX(answered_at) IS NOT NULL "
                "ON CONFLICT(user_message_id, cid, user_id) DO UPDATE SET seen_at=MAX(seen_at, excluded.seen_at)",
                (user_id, message_id, cid, message_id, cid),
            )
            return True
    finally:
        db.close()


@router.post("/threads/{thread_id}/messages/{message_id}/comments/{cid}/viewed")
async def mark_chat_comment_viewed(
    thread_id: str, message_id: str, cid: str, user: dict = Depends(require_user_header_only),
):
    if not await run_in_db(_stamp, user["id"], thread_id, message_id, cid):
        raise HTTPException(404, "Comment not found")
    return {"cid": cid}
