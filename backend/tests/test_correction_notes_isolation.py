"""A failing correction note never fails the person's answer or the feedback delivery it rides on."""

from __future__ import annotations

import pytest

from doc_feedback_helpers import seed_document
from tests.test_corrections_comments_answers import _correction_notes, _deliver_batch, _record, _user_comment, db  # noqa: F401
from tests.test_review_bar_answer import _answer, _ask


@pytest.fixture
def failing_wake(monkeypatch):
    """The note is appended, then its wake fails: the savepoint must take the note back too."""
    def fail(*_args):
        raise RuntimeError("wake refused")

    monkeypatch.setattr("core.correction_notes._wake", fail)


def test_a_failing_round_note_leaves_the_answer_committed(db, failing_wake):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    comment = _user_comment(db, artifact)
    assert _record(db, "doc_comment", comment) == (True, [])

    _answer(db, artifact, "changes_requested")

    assert db.execute("SELECT COUNT(*) FROM plan_node_holds WHERE cleared_at IS NULL").fetchone()[0] == 0
    assert _correction_notes(db) == []


def test_a_failing_batch_note_leaves_the_delivery_committed(db, failing_wake):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    assert _record(db, "doc_comment", comment) == (True, [])

    batch = _deliver_batch(db)

    row = db.execute("SELECT delivery_outcome FROM doc_feedback_batches WHERE id=?", (batch["id"],)).fetchone()
    assert row[0] is not None
    assert _correction_notes(db) == []
