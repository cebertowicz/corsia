"""Re-checks a thread actor against its thread row inside the write transaction."""

from __future__ import annotations


def revalidate_actor(db, actor):
    """The actor itself once its thread still matches it; a stale identity raises.

    The checked fields equal the row's, so the actor is returned as is and keeps
    everything the row does not hold (its Turn and tool call ids).
    """
    if actor.kind != "thread":
        return actor
    if not actor.thread_id:
        raise PermissionError("thread actor identity is missing")
    row = db.execute(
        "SELECT user_id, project_id FROM threads WHERE id=?", (actor.thread_id,)
    ).fetchone()
    if row is None or row["user_id"] != actor.user_id or row["project_id"] != actor.project_id:
        raise PermissionError("thread actor identity is stale or unavailable")
    return actor
