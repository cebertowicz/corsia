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

import json

from doceditor import writer_turns


def holds_rows(db, artifact_id: str) -> bool:
    """True when this document already has lines in its writers' turn-taking line."""
    return db.execute(
        "SELECT 1 FROM doc_comment_writer_line WHERE artifact_id=? LIMIT 1", (artifact_id,)
    ).fetchone() is not None


def _repoint(ops_json: str, old_path: str | None, new_path: str | None) -> str:
    """The saved ops with each call's `path` taken to the file's new path, the call otherwise verbatim.

    A saved edit or write names the file by the path its agent used, and the wake asks the agent to
    repeat that call. The file now lives at its new path, so the repeat must name it; only a
    `path` that names the old file (as stored, or as its trailing part) is replaced.
    """
    if not old_path or not new_path:
        return ops_json
    ops = json.loads(ops_json)
    changed = False
    for op in ops:
        call = op.get("call") or {}
        path = call.get("path")
        if isinstance(path, str) and (path == old_path or old_path.endswith("/" + path)):
            call["path"] = new_path
            changed = True
    return json.dumps(ops) if changed else ops_json


def repoint_lines(db, *, artifact_id: str, old_path: str, new_path: str) -> int:
    """Point this document's saved calls at the path it moved to, its identity unchanged; returns how many lines.

    Any identity path move (a promote that carries the identity, `artifact_relocate`, a file moved outside
    the tools) changes only the path, so no line is re-keyed; the wake still asks the agent to repeat a
    saved call, and that call must name the file where it now is. `registry.relocate` calls this from the
    one writer of an identity's path move.

    Cost: one scan of this table by `artifact_id` (no index covers it) per actual path move, never per
    save, then one UPDATE per changed row. A row exists only while an agent waits in line and `settle`
    deletes it, so the rows read are the agents waiting on this one document. `holds_rows` and
    `move_identity` in this file scan the table the same way.
    """
    changed = 0
    for row in db.execute("SELECT id, ops FROM doc_comment_writer_line WHERE artifact_id=?", (artifact_id,)).fetchall():
        ops = _repoint(str(row["ops"]), old_path, new_path)
        if ops != str(row["ops"]):
            db.execute("UPDATE doc_comment_writer_line SET ops=? WHERE id=?", (ops, row["id"]))
            changed += 1
    return changed


def move_identity(db, *, from_artifact_id: str, to_artifact_id: str) -> int:
    """Carry this document's writer lines to the identity that replaces it; returns how many.

    A line is worth moving because its agent may still be woken to answer a comment on the
    promoted document and must find the text it was shown, its place in line and its state
    unchanged -- `writer_line.serve_waiting` and `recover` settle a moved row as they would
    have settled it on the source. A comment thread's line is keyed by its root comment, whose
    id does not change; a section's line is keyed by the document's own identity, so the key is
    retaken from the same heading through `writer_turns.rekey`, and its saved calls are pointed at
    the promoted file. Nothing is merged and nobody's turn moves: the successor holds no line of its
    own.
    """
    from registry import models

    rows = db.execute(
        "SELECT id, root_id, ops FROM doc_comment_writer_line WHERE artifact_id=?", (from_artifact_id,)
    ).fetchall()
    if not rows:
        return 0
    old_path = (models.get_artifact(from_artifact_id, db=db) or {}).get("path")
    new_path = (models.get_artifact(to_artifact_id, db=db) or {}).get("path")
    for row in rows:
        db.execute(
            "UPDATE doc_comment_writer_line SET artifact_id=?, root_id=?, ops=? WHERE id=?",
            (to_artifact_id,
             writer_turns.rekey(str(row["root_id"]), from_artifact_id=from_artifact_id,
                                to_artifact_id=to_artifact_id),
             _repoint(str(row["ops"]), old_path, new_path),
             row["id"]),
        )
    return len(rows)
