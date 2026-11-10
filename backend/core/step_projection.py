"""Public projections for saved native turn steps and sidebar previews."""

from infra.sidebar_preview import LATEST_MESSAGE_JOIN, PREVIEW_CONTENT, project_previews
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
            f"""SELECT s.title, s.updated_at, {PREVIEW_CONTENT} AS last_message,
               m.id AS last_message_id
               FROM threads s {LATEST_MESSAGE_JOIN}
               WHERE s.id=?""", (thread_id, ),
        ).fetchone()
        result = dict(row) if row else None
        if result:
            result['last_message'] = project_previews(
                db, [(thread_id, result['last_message_id'], result['last_message'])],
            )[0]
        return result
    finally:
        db.close()
