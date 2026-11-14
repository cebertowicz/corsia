"""The artifact pane's reply marker reads only the pane's own documents.

It used to read every comment in the database on each pane request.
"""

import sqlite3

from doceditor.feedback_state import _SCOPE_CHUNK, annotate_pane, unread_comment_rows, unread_item_ids
from infra.schema import SCHEMA_SQL


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    return db


def _reply_waiting(db, document: str, n: int = 0) -> None:
    """A person's comment with an agent reply nobody has read."""
    db.execute(
        "INSERT INTO doc_comments (id, artifact_id, author, author_kind, body, created_at, updated_at)"
        " VALUES (?,?,'ui','user','why?','2026-11-14T00:00:00','2026-11-14T00:00:00')",
        (f"root-{document}-{n}", document),
    )
    db.execute(
        "INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, body, created_at, updated_at)"
        " VALUES (?,?,?,'agent','agent','because','2026-11-14T00:00:01','2026-11-14T00:00:01')",
        (f"reply-{document}-{n}", document, f"root-{document}-{n}"),
    )


def test_the_pane_query_reads_its_documents_through_their_index():
    db = _db()
    sql, params = unread_comment_rows("owner", ("doc-a", "doc-b"))
    plan = " | ".join(row[3] for row in db.execute(f"EXPLAIN QUERY PLAN SELECT DISTINCT root.artifact_id {sql}", params))
    assert "SCAN item" not in plan, plan
    assert "idx_doc_comments_artifact (artifact_id=?)" in plan, plan


def test_the_pane_marks_its_own_documents_the_same_as_before():
    db = _db()
    _reply_waiting(db, "doc-a")
    _reply_waiting(db, "elsewhere")
    pane = {"nodes": [
        {"kind": "artifact", "id": "doc-a"},
        {"kind": "group", "children": [{"kind": "artifact", "id": "doc-b"}]},
    ]}

    annotate_pane(pane, "owner", db=db)

    assert pane["nodes"][0]["reply_waiting"] is True
    assert pane["nodes"][1]["children"][0]["reply_waiting"] is False
    assert unread_item_ids(db, "owner") == {"doc-a", "elsewhere"}
    assert unread_item_ids(db, "owner", ["doc-a", "doc-b"]) == {"doc-a"}
    assert unread_item_ids(db, "owner", []) == set()


def test_a_pane_larger_than_one_query_is_read_in_parts():
    db = _db()
    documents = [f"doc-{n}" for n in range(_SCOPE_CHUNK * 2 + 5)]
    for document in documents[::97]:
        _reply_waiting(db, document)

    assert unread_item_ids(db, "owner", documents) == set(documents[::97])
