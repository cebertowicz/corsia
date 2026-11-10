"""Public projections for saved native turn steps and sidebar previews."""

from infra.chat_comment_answers import projected_saved_content
from infra.database import get_db
from infra.db_async import run_in_db
from subagents.assistant_step_display import answer_projection


async def step_answer_projection(thread_id: str, message_id: str, content: str) -> dict:
    def read():
        db = get_db()
        try:
            return answer_projection(db, thread_id, message_id, content)
        finally:
            db.close()

    return await run_in_db(read)


def turn_sidebar_projection(thread_id: str) -> dict | None:
    db = get_db()
    try:
        row = db.execute(
            """SELECT s.title, s.updated_at,
               (SELECT m.content FROM messages m WHERE m.thread_id=s.id AND m.role IN ('user','assistant')
                ORDER BY m.created_at DESC LIMIT 1) AS last_message,
               (SELECT m.id FROM messages m WHERE m.thread_id=s.id AND m.role IN ('user','assistant')
                ORDER BY m.created_at DESC LIMIT 1) AS last_message_id
               FROM threads s WHERE s.id=?""", (thread_id,),
        ).fetchone()
        result = dict(row) if row else None
        if result and result['last_message_id']:
            result['last_message'] = projected_saved_content(
                db, thread_id, result['last_message_id'], result['last_message'],
            )
        return result
    finally:
        db.close()
