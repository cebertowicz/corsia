"""Mark Activity notification documents read."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.doc_comment_access import _artifact_for_read
from doceditor import comment_thread_views
from infra.auth import require_user_header_only
from infra.database import get_db
from infra.db_async import run_in_db

router = APIRouter()


class NotificationDocument(BaseModel):
    artifact_id: str
    through_reply_id: str


class NotificationsReadBody(BaseModel):
    documents: list[NotificationDocument]


def _mark_notifications_read(user_id: str, documents: list[NotificationDocument]) -> None:
    db = get_db()
    try:
        # Authorize every document before anything is written.
        live = [(_artifact_for_read(doc.artifact_id, user_id, db), doc.through_reply_id) for doc in documents]
    finally:
        db.close()
    try:
        comment_thread_views.mark_documents_read(user_id, live)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="reply not found") from exc


@router.post("/notifications/read")
async def post_notifications_read(body: NotificationsReadBody,
                                  user: dict = Depends(require_user_header_only)):
    """Mark documents read up to the replies shown; later replies remain unread."""
    from api.attention import QUEUE_ITEMS_CAP

    if len(body.documents) > QUEUE_ITEMS_CAP:
        raise HTTPException(status_code=422, detail="Too many documents")
    await run_in_db(lambda: _mark_notifications_read(user["id"], body.documents))
    return {"read": len(body.documents)}
