"""The writer line's own move to another document identity.

Lives beside ``writer_line`` because it applies that module's rules rather than a copy of them: a
saved line is one agent's pending writes on one thread root, its place in line is its ``saved_at``,
and nothing saved or promised in a wake is ever thrown away. ``writer_line.py`` holds the
turn-taking and settlement code and is at the repository's file standard, so the identity move is
its own module instead of an appendage.

A thread's root id is a comment id, so it travels with the comment unchanged. Only a section's root
names the document, and that one is re-keyed by ``writer_turns.rekey`` -- the same helper that
builds the key when a section write is first queued.
"""

from __future__ import annotations

import json

from doceditor import writer_turns


def move_identity(db, *, from_artifact_id: str, to_artifact_id: str) -> int:
    """Carry this document's saved writer lines to the identity that replaces it; returns how many.

    A line whose re-keyed root is free simply moves. Where the target already holds this agent's
    line on that root, the two join: the earlier row keeps its place in line and the later one's
    saved writes follow it, and the row stays woken when either was, because a woken agent must
    still find the text it was shown. ``writer_line.admit`` replaces a woken row's ops only because
    the write in hand *is* that agent's answer to its wake; writes arriving with a moved document
    predate the wake, so both sets stay.
    """
    rows = db.execute(
        """SELECT id, root_id, agent_thread_id, ops, state, saved_at, wakes
           FROM doc_comment_writer_line WHERE artifact_id=?""",
        (from_artifact_id,),
    ).fetchall()
    for row in rows:
        root_id = writer_turns.rekey(
            str(row["root_id"]), from_artifact_id=from_artifact_id, to_artifact_id=to_artifact_id,
        )
        if root_id == str(row["root_id"]):
            # A thread's root is a comment id: it travels with the comment, so nothing can collide.
            db.execute("UPDATE doc_comment_writer_line SET artifact_id=? WHERE id=?", (to_artifact_id, row["id"]))
            continue
        target = db.execute(
            "SELECT * FROM doc_comment_writer_line WHERE root_id=? AND agent_thread_id=? AND id<>?",
            (root_id, str(row["agent_thread_id"]), row["id"]),
        ).fetchone()
        if target is None:
            db.execute(
                "UPDATE doc_comment_writer_line SET root_id=?, artifact_id=? WHERE id=?",
                (root_id, to_artifact_id, row["id"]),
            )
            continue
        first, later = sorted((target, row), key=lambda r: str(r["saved_at"]))
        db.execute(
            """UPDATE doc_comment_writer_line
               SET ops=?, state=?, saved_at=?, wakes=? WHERE id=?""",
            (json.dumps([*json.loads(first["ops"]), *json.loads(later["ops"])]),
             "woken" if str(first["state"]) == "woken" or str(later["state"]) == "woken" else "waiting",
             first["saved_at"], max(int(first["wakes"]), int(later["wakes"])), first["id"]),
        )
        db.execute("DELETE FROM doc_comment_writer_line WHERE id=?", (row["id"],))
    return len(rows)
