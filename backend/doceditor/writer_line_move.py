"""The writer-line owner's identity re-key for a promote.

Lives beside `writer_line`, whose rules it does not touch: no line is admitted, settled,
served or dropped here, and a moved line keeps its place in line, its saved text and its
delivery state. The file is separate because `writer_line.py` sits at its size ratchet and
this is a re-key. `doceditor.comment_transfer` calls `move_identity` from
`registry.models.retire`, so the moved lines commit with the tombstone and the successor link,
and only after it has proved the successor holds no comment state of its own -- which is why
nothing here merges two lines or re-orders anyone.
"""

from __future__ import annotations

from doceditor import writer_turns


def holds_rows(db, artifact_id: str) -> bool:
    """True when this document already has lines in its writers' turn-taking line."""
    return db.execute(
        "SELECT 1 FROM doc_comment_writer_line WHERE artifact_id=? LIMIT 1", (artifact_id,)
    ).fetchone() is not None


def move_identity(db, *, from_artifact_id: str, to_artifact_id: str) -> int:
    """Carry this document's writer lines to the identity that replaces it; returns how many.

    A line is worth moving because its agent may still be woken to answer a comment on the
    promoted document and must find the text it was shown, its place in line and its state
    unchanged -- `writer_line.serve_waiting` and `recover` settle a moved row as they would
    have settled it on the source. A comment thread's line is keyed by its root comment, whose
    id does not change; a section's line is keyed by the document's own identity, so the key is
    retaken from the same heading through `writer_turns.rekey`. Nothing is merged and nobody's
    turn moves: the successor holds no line of its own.
    """
    rows = db.execute(
        "SELECT id, root_id FROM doc_comment_writer_line WHERE artifact_id=?", (from_artifact_id,)
    ).fetchall()
    for row in rows:
        db.execute(
            "UPDATE doc_comment_writer_line SET artifact_id=?, root_id=? WHERE id=?",
            (to_artifact_id,
             writer_turns.rekey(str(row["root_id"]), from_artifact_id=from_artifact_id,
                                to_artifact_id=to_artifact_id),
             row["id"]),
        )
    return len(rows)
