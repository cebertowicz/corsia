"""The sidebar's last-message preview, shared by the thread list and live updates."""

from infra.chat_comment_answers import VISIBLE_CONTENT, VISIBLE_CONTENT_JOIN, strip_answer_tags
from infra.database import get_db

# The first 100 characters of thread ``s``'s newest user or assistant message, as chat shows it.
# Callers strip the tags of sections that were not placed.
LAST_MESSAGE_PREVIEW = f"""(SELECT substr({VISIBLE_CONTENT}, 1, 100) FROM messages m {VISIBLE_CONTENT_JOIN}
    WHERE m.thread_id=s.id AND m.role IN ('user','assistant') ORDER BY m.created_at DESC LIMIT 1)"""


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
