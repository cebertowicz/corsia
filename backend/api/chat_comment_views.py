"""Post chat comments, read their threads, and record when a person has seen an answer."""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from doceditor.reply_attention import chat_reply_items
from infra.auth import owns_thread, require_user_header_only
from infra.chat_comment_answers import has_comment_cid
from infra.chat_comment_index import attach_comments, comment_only
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_read
from infra.queries import get_thread_for_user

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


class PostRequest(BaseModel):
    input_id: str = ''
    comment: dict | None = None
    viewing_document: dict | None = None
    reasoning_effort: str | None = None
    # The comment input whose delivery ended; its comments are posted again in its place.
    retry_of: str | None = None


# The delivery outcomes chat offers Retry for (UserMessageBubble); a cancelled one is posted again instead.
_RETRYABLE = ('failed', 'ambiguous')


def _retry_metadata(thread_id: str, message_id: str) -> dict | None:
    """What a Retry of this ended comment input posts: its own comments, in its place.

    An uncertain delivery may still have reached the agent, so the agent is told this may repeat it.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT content,metadata,delivery_state FROM messages WHERE id=? AND thread_id=? AND role='user'",
            (message_id, thread_id),
        ).fetchone()
    finally:
        db.close()
    try:
        metadata = json.loads(row[1] or '{}') if row is not None else None
    except ValueError:
        metadata = None
    if row is None or row[2] not in _RETRYABLE or not comment_only(row[0], metadata):
        return None
    reply_context: dict = {'comments': metadata['reply_context']['comments']}
    if row[2] == 'ambiguous':
        reply_context['possible_duplicate'] = True
    return {'reply_context': reply_context, 'retried_from_message_id': message_id}


def _owned_thread(user_id: str, thread_id: str):
    db = get_db()
    try:
        row = get_thread_for_user(thread_id, user_id, db=db)
        return dict(row) if row is not None else None
    finally:
        db.close()


def _posted(user_id: str, thread_id: str, input_id: str) -> dict | None:
    """The stored input's delivery, and the commented rows with their threads as they now stand."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT delivery_state,delivery_error FROM messages WHERE id=? AND thread_id=? AND role='user'",
            (input_id, thread_id),
        ).fetchone()
        targets = [target for (target,) in db.execute(
            "SELECT DISTINCT target_message_id FROM chat_comments WHERE thread_id=? AND user_message_id=?",
            (thread_id, input_id),
        )]
    finally:
        db.close()
    if row is None:
        return None
    return {'message_id': input_id, 'delivery_state': row[0], 'delivery_error': row[1],
            **(_threads(user_id, thread_id, targets) or {'messages': []})}


@router.post('/threads/{thread_id}/chat-comments')
async def post_chat_comment(thread_id: str, body: PostRequest, user: dict = Depends(require_user_header_only)):
    """Post a comment, or a reply in a comment thread, as the thread's next input.

    It is admitted like any input, so the agent's Turn takes it in as before, but it is
    no chat message: its broadcast reaches chat clients as a `chat_comments_changed`
    notice (infra/send_broadcast.py), and chat never draws its row. The response is the
    box's whole answer. A subagent pane posts to the child thread the same way.
    """
    thread_row = await run_in_db(_owned_thread, user['id'], thread_id)
    if thread_row is None:
        raise HTTPException(404, 'chat_not_found')
    from infra.execution_class import is_subagent
    from infra.send_service import SendRejected, send_thread_input

    subagent = is_subagent(thread_row)
    if body.retry_of:
        # One id per ended input, so a second press or a press after a lost response is the same input.
        input_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'corsia:chat-comment-retry:{body.retry_of}'))
        metadata = await run_read(lambda: _retry_metadata(thread_id, body.retry_of))
        if metadata is None:
            raise HTTPException(409, 'comment_not_retryable')
    else:
        input_id = body.input_id.strip()
        if not input_id or body.comment is None:
            raise HTTPException(400, 'comment_missing')
        metadata = {'reply_context': {'comments': [body.comment]}}
        if body.viewing_document is not None:
            metadata['viewing_document'] = body.viewing_document
    try:
        await send_thread_input(
            thread_id, thread_row, '',
            user=user, metadata=metadata, human_origin=True, input_id=input_id,
            native_mode='delegation' if subagent else 'event',
            touch_thread_on_insert=True, trim_content=False,
            reasoning_effort=None if subagent else (body.reasoning_effort or thread_row.get('reasoning_effort')),
        )
    except SendRejected as exc:
        # Stored, then not delivered: the comment is in its thread with that failure and Retry,
        # so the box closes on it rather than offering a second post of the same text.
        stored = exc.input_undeliverable and await run_read(lambda: _posted(user['id'], thread_id, input_id))
        if not stored:
            raise HTTPException(exc.status_code, exc.detail) from exc
        return stored
    result = await run_read(lambda: _posted(user['id'], thread_id, input_id))
    if result is None:
        raise HTTPException(500, 'comment_not_stored')
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
