"""Mark Activity notifications read."""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.doc_comment_access import _artifact_for_read
from doceditor import comment_thread_views
from infra.auth import require_user_header_only
from infra.database import get_db, write_transaction
from doceditor.activity_chat_reply_rows import chat_detail, mark_chats_read
from infra.db_async import run_in_db
from infra.thread_visibility import VISIBLE_THREAD_SQL

router = APIRouter()


class NotificationDocument(BaseModel):
    artifact_id: str
    through_reply_id: str


class NotificationChat(BaseModel):
    thread_id: str
    through: str


class NotificationsReadBody(BaseModel):
    documents: list[NotificationDocument]
    chats: list[NotificationChat] = []


def _require_visible_chats(db, user_id: str, ids: set[str]) -> None:
    if ids:
        count = db.execute(f'''SELECT COUNT(*) FROM threads t
            LEFT JOIN projects p ON p.id=t.project_id
            WHERE t.user_id=? AND t.id IN (SELECT value FROM json_each(?)) AND {VISIBLE_THREAD_SQL}''',
            (user_id, json.dumps(list(ids)))).fetchone()[0]
        if count != len(ids):
            raise HTTPException(status_code=404, detail='chat not found')


def _mark_notifications_read(user_id: str, documents: list[NotificationDocument], chats: list[NotificationChat]) -> None:
    db = get_db()
    try:
        # Authorize every document before anything is written.
        live = [(_artifact_for_read(doc.artifact_id, user_id, db), doc.through_reply_id) for doc in documents]
        _require_visible_chats(db, user_id, {chat.thread_id for chat in chats})
    finally:
        db.close()
    try:
        comment_thread_views.mark_documents_read(user_id, live)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="reply not found") from exc
    if chats:
        db = get_db()
        try:
            with write_transaction(db, scope='activity_chat_replies_read'):
                mark_chats_read(db, user_id, [chat.model_dump() for chat in chats])
        finally:
            db.close()


def _chat_detail(user_id: str, thread_id: str) -> dict:
    db = get_db()
    try:
        _require_visible_chats(db, user_id, {thread_id})
        return {'messages': chat_detail(db, user_id, thread_id)}
    finally:
        db.close()


@router.get('/notifications/chats/{thread_id}')
async def get_notification_chat(thread_id: str, user: dict = Depends(require_user_header_only)):
    return await run_in_db(lambda: _chat_detail(user['id'], thread_id))


@router.post("/notifications/read")
async def post_notifications_read(body: NotificationsReadBody,
                                  user: dict = Depends(require_user_header_only)):
    """Mark documents read up to the replies shown; later replies remain unread."""
    from api.attention import QUEUE_ITEMS_CAP

    if len(body.documents) + len(body.chats) > QUEUE_ITEMS_CAP:
        raise HTTPException(status_code=422, detail="Too many notifications")
    await run_in_db(lambda: _mark_notifications_read(user["id"], body.documents, body.chats))
    return {"read": len(body.documents) + len(body.chats)}
