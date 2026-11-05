"""Whether the person's words in one thread may reach another thread."""

from __future__ import annotations

import json
import sqlite3

from llm.locality import cloud_off_project_ids
from memory.checkpoint_search import caller_user_id


def source_reaches_thread(db: sqlite3.Connection, source_thread_id: str, recipient_thread_id: str) -> bool:
    """Whether the source is the recipient's user's live thread and its locality allows the recipient to read it.

    A local-only source reaches only a local-only recipient, and a source in a
    cloud-off project reaches only threads in that same project.
    """
    user_id = caller_user_id(db, recipient_thread_id)
    recipient = db.execute("SELECT project_id,local_only FROM threads WHERE id=?", (recipient_thread_id,)).fetchone()
    if user_id is None or recipient is None:
        return False
    return db.execute(
        """SELECT 1 FROM threads t
           WHERE t.id=? AND t.user_id=? AND t.purged_at IS NULL
             AND (COALESCE(t.local_only, 0)=0 OR ?=1)
             AND (t.project_id IS NULL OR t.project_id=?
                  OR t.project_id NOT IN (SELECT value FROM json_each(?)))""",
        [source_thread_id, user_id, int(bool(recipient["local_only"])), recipient["project_id"],
         json.dumps(sorted(cloud_off_project_ids(db=db)))],
    ).fetchone() is not None
