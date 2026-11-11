"""The sidebar's last-message preview, shared by the thread list and live updates."""

from infra.chat_comment_answers import VISIBLE_CONTENT, VISIBLE_CONTENT_JOIN, strip_answer_tags
from infra.database import get_db

# The first 100 characters of thread ``s``'s newest user or assistant message, as chat shows it.
# An answer-only message shows nothing in chat (its saved chat text is empty), so it is skipped.
# Callers strip the tags of sections that were not placed.
LAST_MESSAGE_PREVIEW = f"""(SELECT substr({VISIBLE_CONTENT}, 1, 100) FROM messages m {VISIBLE_CONTENT_JOIN}
    WHERE m.thread_id=s.id AND m.role IN ('user','assistant') AND (cv.content IS NULL OR cv.content != '')
    AND NOT (m.role='user' AND trim(m.content)='' AND CASE WHEN json_valid(m.metadata) THEN
        COALESCE(json_array_length(m.metadata, '$.reply_context.comments'), 0) > 0
        AND COALESCE(json_array_length(m.metadata, '$.attachments'), 0) = 0 ELSE 0 END)
    ORDER BY m.created_at DESC LIMIT 1)"""


def sidebar_update(thread_id: str) -> dict | None:
    """One thread's live sidebar title, time and preview."""
    db = get_db()
    try:
        row = db.execute(
            f"SELECT title, updated_at, {LAST_MESSAGE_PREVIEW} AS last_message FROM threads s WHERE s.id=?",
            (thread_id,),
        ).fetchone()
    finally:
        db.close()
    return {**dict(row), "last_message": strip_answer_tags(row["last_message"])} if row else None
