"""Leave a document comment thread through the agent tool."""

import json

from doceditor import comment_recipients, comments as store
from infra.auth import may_access_artifact
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from registry import models as registry_models


async def leave_comment_thread(comment_id: str, actor_thread_id: str, user_id: str) -> str:
    def _leave():
        db = get_db()
        try:
            with write_transaction(db, scope="leave_comment_thread") as connection:
                comment = store.get_comment(comment_id, db=connection)
                art = registry_models.get_artifact(comment["artifact_id"], db=connection) if comment else None
                if art is None or not may_access_artifact(art["parent_thread_id"], user_id, db=connection):
                    return None
                comment_recipients.leave(connection, comment, actor_thread_id)
                return comment_recipients.root_id(comment)
        finally:
            db.close()

    try:
        root = await run_in_db(_leave)
    except ValueError as exc:
        return f"Error: {exc}"
    return json.dumps({"left": True, "comment_id": root}, sort_keys=True) if root else "Error: Comment not found"
