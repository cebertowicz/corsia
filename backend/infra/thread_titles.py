"""Shared title writes and standalone title-only roster transitions.

Combined roster mutations record their whole transition in their own transaction.
"""

from infra.passive_changes import append_agent_roster_transition


def write_thread_title(db, thread_id: str, title: str, now: str) -> None:
    db.execute(
        'UPDATE threads SET title = ?, updated_at = ? WHERE id = ?',
        (title, now, thread_id),
    )


def retitle_thread(db, before, title: str, now: str) -> None:
    """Write a new title and record the roster transition it causes."""
    write_thread_title(db, before['id'], title, now)
    after = db.execute('SELECT * FROM threads WHERE id = ?', (before['id'],)).fetchone()
    append_agent_roster_transition(
        db, before=before, after=after,
        summary=f"Agent responsibility map updated: {after['title'] or 'Untitled'}",
    )
