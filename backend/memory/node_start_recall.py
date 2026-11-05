"""Whether the person's words in one thread may reach another thread.

What remains of the task-start recall: its scope rule, still used by quoting
and input wakes. It goes with its last callers.
"""

from __future__ import annotations

import json
import sqlite3

from llm.locality import cloud_off_project_ids
from memory.checkpoint_search import caller_user_id


def _owned_threads(alias: str) -> str:
    return f"{alias}.user_id=? AND {alias}.purged_at IS NULL"


def _source_scope(alias: str, timestamp: str, caller: sqlite3.Row, cloud_off: set[str], cutoff: str | None) -> tuple[str, list[object]]:
    """Return the locality and start-window predicates a source reader applies before its limit."""
    return (
        f"""AND (COALESCE({alias}.local_only, 0)=0 OR ?=1)
              AND ({alias}.project_id IS NULL OR {alias}.project_id=?
                   OR {alias}.project_id NOT IN (SELECT value FROM json_each(?)))
              AND (? IS NULL OR {timestamp}<=?)""",
        [int(bool(caller["local_only"])), caller["project_id"], json.dumps(sorted(cloud_off)), cutoff, cutoff],
    )


def source_reaches_thread(db: sqlite3.Connection, source_thread_id: str, recipient_thread_id: str) -> bool:
    """Whether the person's words in one thread may reach another thread, by the rule every source reader applies."""
    user_id = caller_user_id(db, recipient_thread_id)
    recipient = db.execute("SELECT project_id,local_only FROM threads WHERE id=?", (recipient_thread_id,)).fetchone()
    if user_id is None or recipient is None:
        return False
    source_scope, params = _source_scope("t", "t.id", recipient, cloud_off_project_ids(db=db), None)
    return db.execute(
        f"SELECT 1 FROM threads t WHERE t.id=? AND {_owned_threads('t')} {source_scope}",
        [source_thread_id, user_id, *params],
    ).fetchone() is not None
