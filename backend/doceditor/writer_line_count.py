"""How many agent writes wait in a comment thread's line, and telling the people on the thread."""

from __future__ import annotations

from doceditor import writer_turns
from infra.write_effects import after_commit


def count(db, root_id: str) -> int:
    return counts(db, [root_id]).get(root_id, 0)


def counts(db, root_ids) -> dict[str, int]:
    """How many writes wait on each of these threads, in one grouped read."""
    roots = sorted(set(root_ids))
    if not roots:
        return {}
    rows = db.execute(
        f"""SELECT root_id, COUNT(*) AS waiting FROM doc_comment_writer_line
            WHERE root_id IN ({",".join("?" * len(roots))}) GROUP BY root_id""",
        roots,
    ).fetchall()
    return {str(row["root_id"]): int(row["waiting"]) for row in rows}


def publish_after_commit(db, root_id: str, artifact_id: str | None) -> None:
    """Tell the people on the thread how many agents wait, once this write commits."""
    after_commit(db, lambda: publish(root_id, artifact_id))


def publish(root_id: str, artifact_id: str | None, *, user_ids: set[str] | None = None) -> None:
    from infra import state
    from infra.database import get_db

    users = user_ids if user_ids is not None else writer_turns.people_on(root_id)
    if not users or writer_turns.section_heading(root_id) is not None:
        # A section's line is not shown to people yet; only a comment thread's count is.
        return
    db = get_db()
    try:
        waiting = count(db, root_id)
        if artifact_id is None:
            row = db.execute("SELECT artifact_id FROM doc_comments WHERE id=?", (root_id,)).fetchone()
            artifact_id = row["artifact_id"] if row else None
    finally:
        db.close()
    for user_id in users:
        state.broadcast_to_user(user_id, {
            "type": "comment_line", "artifact_id": artifact_id, "comment_id": root_id, "waiting": waiting,
        })
