"""Single and batch suggestion paths share the insertion sibling predicate."""

import sqlite3

from doceditor import comments
from doceditor.comment_batch_actions import _attach_insertion_siblings


def test_batch_groups_insertions_not_rejected_by_artifact_and_anchor(monkeypatch):
    def no_query(*args, **kwargs):
        raise AssertionError("batch should reuse loaded rows")

    monkeypatch.setattr(comments, "insertion_siblings", no_query)
    pending = {"artifact_id": "doc", "id": "zero", "anchor_text": "Heading", "insert_after": True}
    accepted = {**pending, "id": "one", "decision": "accepted"}
    rejected = {**pending, "id": "two", "decision": "rejected"}
    other = {**accepted, "id": "three", "anchor_text": "Other"}
    _attach_insertion_siblings([pending], [pending, accepted, rejected, other])
    assert pending["_insertion_siblings"] == [pending, accepted]


def test_single_accept_reads_open_and_accepted_insertions_for_the_same_anchor():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE doc_comments (id TEXT, artifact_id TEXT, anchor_text TEXT, insert_after INTEGER, decision TEXT, created_at TEXT)")
    db.executemany("INSERT INTO doc_comments VALUES (?, ?, ?, ?, ?, ?)", [
        ("open", "doc", "Heading", 1, None, "1"),
        ("accepted", "doc", "Heading", 1, "accepted", "2"),
        ("rejected", "doc", "Heading", 1, "rejected", "3"),
        ("replacement", "doc", "Heading", 0, "accepted", "4"),
        ("other-anchor", "doc", "Other", 1, "accepted", "5"),
        ("other-doc", "elsewhere", "Heading", 1, "accepted", "6"),
    ])
    assert [row["id"] for row in comments.insertion_siblings("doc", "Heading", db=db)] == ["open", "accepted"]
