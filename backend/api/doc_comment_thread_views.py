"""HTTP surface for recording the reply a user actually rendered in one thread."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.doc_comment_access import _artifact_for_read
from doceditor import comment_thread_views
from infra.auth import require_user_header_only
from infra.database import get_db
from infra.db_async import run_in_db

router = APIRouter(prefix="/api", tags=["doc-comments"])


class CommentThreadViewed(BaseModel):
    seen_reply_id: str


def _check_access(artifact_id: str, user_id: str) -> str:
    """Apply the comment routes' single ownership gate off the event loop; returns the document's live id."""
    db = get_db()
    try:
        return _artifact_for_read(artifact_id, user_id, db)
    finally:
        db.close()


@router.post("/artifacts/{artifact_id}/comments/dismiss-all")
async def dismiss_comment_attention(artifact_id: str, user: dict = Depends(require_user_header_only)):
    """Clear comment attention while leaving suggestions undecided."""
    live_id = await run_in_db(_check_access, artifact_id, user["id"])
    await run_in_db(comment_thread_views.dismiss_all, user["id"], live_id)
    return {"dismissed": True}


@router.post("/artifacts/{artifact_id}/comments/{root_id}/viewed")
async def mark_comment_thread_viewed(
    artifact_id: str, root_id: str, body: CommentThreadViewed,
    user: dict = Depends(require_user_header_only),
):
    """Store the newest reply actually rendered in one disclosed thread."""
    live_id = await run_in_db(_check_access, artifact_id, user["id"])
    remaining = await run_in_db(
        comment_thread_views.mark_viewed, user["id"], live_id, root_id, body.seen_reply_id,
    )
    if remaining is None:
        raise HTTPException(404, "Comment thread or reply not found")
    return {"root_id": root_id, "new_reply_ids": remaining}
