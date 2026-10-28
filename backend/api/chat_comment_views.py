"""Record when a person has seen a chat-comment answer."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from doceditor.reply_attention import chat_reply_items
from infra.auth import owns_thread, require_user_header_only
from infra.chat_comment_answers import has_comment_cid
from infra.chat_comment_index import attach_comments
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_read

router = APIRouter(prefix="/api", tags=["chat-comments"])


class ThreadsRequest(BaseModel):
    message_ids: list[str]


def _threads(user_id: str, thread_id: str, message_ids: list[str]) -> dict | None:
    db = get_db()
    try:
        if not owns_thread(thread_id, user_id, db=db):
            return None
        targets = [{'id': message_id, 'role': 'assistant'} for message_id in message_ids]
        attach_comments(db, thread_id, user_id, targets)
        replies = chat_reply_items(db, user_id, thread_id)
        return {'messages': targets, **({'chat_reply_items': replies} if replies else {})}
    finally:
        db.close()


@router.post('/threads/{thread_id}/chat-comment-threads')
async def chat_comment_threads(thread_id: str, body: ThreadsRequest, user: dict = Depends(require_user_header_only)):
    result = await run_read(lambda: _threads(user['id'], thread_id, body.message_ids))
    if result is None:
        raise HTTPException(404)
    return result


class ViewRequest(BaseModel):
    assistant_message_id: str


def _stamp(user_id: str, thread_id: str, message_id: str, cid: str, assistant_message_id: str) -> bool:
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
            # Only the answer whose element completed its dwell can advance the stamp.
            db.execute(
                "INSERT INTO chat_comment_views "
                "SELECT ?, ?, ?, answered_at FROM chat_comment_answers "
                "WHERE thread_id=? AND user_message_id=? AND cid=? AND assistant_message_id=? "
                "ON CONFLICT(user_message_id, cid, user_id) DO UPDATE SET seen_at=MAX(seen_at, excluded.seen_at)",
                (user_id, message_id, cid, thread_id, message_id, cid, assistant_message_id),
            )
            return True
    finally:
        db.close()


@router.post("/threads/{thread_id}/messages/{message_id}/comments/{cid}/viewed")
async def mark_chat_comment_viewed(
    thread_id: str, message_id: str, cid: str, body: ViewRequest, user: dict = Depends(require_user_header_only),
):
    if not await run_in_db(_stamp, user["id"], thread_id, message_id, cid, body.assistant_message_id):
        raise HTTPException(404, "Comment not found")
    return {"cid": cid}
