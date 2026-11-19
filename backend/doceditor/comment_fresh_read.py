"""The fresh read before an agent's comment, reply or suggested change reaches the person.

A comment is a door when the recipient set the code already computes includes the person
(``doceditor.feedback_comments``: their document, a thread they commented in or were tagged
in, an ask raised to them). A comment whose recipients are agents only is not handed to the
person and needs no read. The read sees the comment's text, the passage it is anchored to and
the comments above it; the pass binds the comment's own text (and a suggestion's replacement).
"""

from __future__ import annotations

from typing import Any

from core import fresh_read
from core.fresh_read import Item
from doceditor.comment_action_errors import CommentActionError
from infra.database import get_db
from infra.db_async import run_in_db


def _person_receives(db, comment: dict[str, Any]) -> bool:
    from doceditor.feedback_comments import participants, recipients
    from doceditor.feedback_state import USER_AUTHOR_ID

    if comment.get("tagged_user_id"):
        return True
    if not comment.get("artifact_id"):
        return False
    if USER_AUTHOR_ID in recipients(db, comment):
        return True
    root = comment.get("parent_id")
    return bool(root) and USER_AUTHOR_ID in participants(db, str(root))


def _thread_above(db, parent_id: str | None) -> str | None:
    if not parent_id:
        return None
    rows = db.execute(
        "SELECT author_kind, body FROM doc_comments WHERE id=? OR parent_id=? ORDER BY created_at",
        (parent_id, parent_id),
    ).fetchall()
    return "\n".join(f"- {'person' if row['author_kind'] == 'user' else 'agent'}: {row['body']}" for row in rows) or None


def _shown(body: str, quote: str | None, replacement: str | None) -> str:
    if replacement is None:
        return body
    return f'{body}\n\nSuggested change: replace "{quote or ""}" with "{replacement}"'


def _where(db, quote: str | None, parent_id: str | None) -> str | None:
    parts = []
    if quote:
        parts.append(f"Anchored to this passage of the document:\n{quote}")
    above = _thread_above(db, parent_id)
    if above:
        parts.append(f"The comments above it in its thread:\n{above}")
    return "\n\n".join(parts) or None


def _new_comment_item(request, comment_id: str) -> Item | None:
    """Blocking. The item a new agent comment hands the person, or None when it reaches agents only
    or this id already delivered it (a retry replays the comment; it delivers nothing new)."""
    comment = {
        "id": "", "artifact_id": request.artifact_id, "parent_id": request.parent_id, "author_kind": "agent",
        "author_thread_id": request.author_thread_id, "tagged_agent_ids": list(request.tagged_agent_ids or ()),
        "tagged_user_id": request.user_id if request.tag_person else None,
    }
    db = get_db()
    try:
        if db.execute("SELECT 1 FROM doc_comments WHERE id=?", (comment_id,)).fetchone() is not None:
            return None
        if not _person_receives(db, comment):
            return None
        return Item(kind="comment", item_ref=comment_id,
                    shown=_shown(request.body, request.quote, request.replacement),
                    writer_thread_id=request.author_thread_id, user_id=request.user_id,
                    where=_where(db, request.quote, request.parent_id))
    finally:
        db.close()


async def check_new_comment(request, comment_id: str) -> str | None:
    """Raise ``not_delivered`` unless an agent comment that reaches the person passed its read.

    Returns the passing ``fresh_read_id``, or None when no read was needed.
    """
    if request.author_kind != "agent":
        return None
    item = await run_in_db(_new_comment_item, request, comment_id)
    if item is None:
        return None
    verdict = await fresh_read.check(item)
    if not verdict.passed:
        raise CommentActionError("not_delivered", verdict.message or "Not delivered.")
    return verdict.fresh_read_id


def _edit_item(comment_id: str, fields: dict[str, Any], thread_id: str) -> Item | None:
    """Blocking. The item an agent's edit of its comment's text hands the person, or None."""
    from doceditor import comments as store

    db = get_db()
    try:
        comment = store.get_comment(comment_id, db=db)
        if comment is None or comment.get("author_kind") != "agent" or fields.get("body") in (None, comment.get("body")):
            return None
        if not _person_receives(db, dict(comment)):
            return None
        quote = fields.get("anchor_text", comment.get("anchor_text"))
        return Item(kind="comment_edit", item_ref=comment_id,
                    shown=_shown(str(fields["body"]), quote, comment.get("replacement")),
                    writer_thread_id=thread_id, user_id=None, where=_where(db, quote, comment.get("parent_id")))
    finally:
        db.close()


async def check_comment_edit(comment_id: str, fields: dict[str, Any], thread_id: str) -> None:
    """Raise ``not_delivered`` unless an edit of a comment's text that reaches the person passed its read."""
    item = await run_in_db(_edit_item, comment_id, fields, thread_id)
    if item is None:
        return
    verdict = await fresh_read.check(item)
    if not verdict.passed:
        raise CommentActionError("not_delivered", verdict.message or "Not delivered.")
