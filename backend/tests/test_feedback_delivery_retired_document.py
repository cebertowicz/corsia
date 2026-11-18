"""A feedback batch whose document was retired is delivered on the live document, or settles once (#2617).

Closing a batch links its task to the document, which needs an active identity. A batch left on a retired one
used to raise at every sweep and stay open for good, so its agent never heard about a person's comment.
"""

from __future__ import annotations

import logging

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches, feedback_delivery
from infra.database import get_db
from registry import models

FAR_FUTURE = "9999-12-31T00:00:00+00:00"
UNREACHABLE_TEXT = "was not delivered: the document was deleted or can no longer be reached."


@pytest.fixture
def db(isolated_db):
    connection = get_db()
    seed_thread(connection, "owner")
    seed_thread(connection, "author")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def notes(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(feedback_delivery, "append_thread_context_note", lambda db, **kw: sent.append(kw))
    return sent


def _retire(db, source: str, successor: str | None, cause: str) -> None:
    db.execute("UPDATE artifact_identities SET state='tombstone' WHERE id=?", (source,))
    db.execute(
        "UPDATE artifact_identities SET metadata=json_set(COALESCE(NULLIF(metadata,''),'{}'),'$.retired_cause',?) WHERE id=?",
        (cause, source),
    )
    if successor:
        db.execute("UPDATE artifact_identities SET metadata=json_set(metadata,'$.successor_id',?) WHERE id=?",
                   (successor, source))
    db.commit()


def test_a_batch_on_a_retired_identity_is_delivered_on_the_live_document(db):
    source, live = seed_document(db, "owner"), seed_document(db, "owner")
    add_comment(db, source, author_kind="user", author="Steve")
    _retire(db, source, live, "promoted")
    feedback_batches.close_due(FAR_FUTURE, db=db)
    (batch,) = batches(db)
    assert (batch["artifact_id"], batch["state"], batch["delivery_outcome"]) == (live, "delivered", "node")


def test_a_batch_on_a_document_with_no_live_identity_settles_once_and_its_agent_is_told_once(db, notes, caplog):
    source = seed_document(db, "owner")
    comment, _ = add_comment(db, source, author_kind="agent", author_id="author", author="Author")
    _retire(db, source, None, "file_gone")
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            feedback_batches.close_due(FAR_FUTURE, db=db)
    (batch,) = batches(db)
    assert (batch["state"], batch["undelivered_reason"]) == ("undelivered", feedback_delivery.UNREACHABLE)
    assert not [record for record in caplog.records if "failed to close" in record.getMessage()]
    assert len(notes) == 1 and notes[0]["thread_id"] == "author"
    assert notes[0]["summary"].endswith(UNREACHABLE_TEXT) and comment["id"] in notes[0]["summary"]
    assert "Retry" not in notes[0]["summary"]


def test_a_chain_that_ends_in_a_deleted_document_settles_the_same_way(db, notes):
    source, middle = seed_document(db, "owner"), seed_document(db, "owner")
    add_comment(db, source, author_kind="agent", author_id="author", author="Author")
    _retire(db, source, middle, "promoted")
    models.retire(middle, tombstone=True, cause="file_gone", db=db)
    feedback_batches.close_due(FAR_FUTURE, db=db)
    assert [b["state"] for b in batches(db)] == ["undelivered"] and len(notes) == 1


def test_a_persons_batch_on_a_document_with_no_live_identity_settles_silently(db, notes):
    source = seed_document(db, "owner")
    add_comment(db, source, author_kind="user", author="Steve")
    _retire(db, source, None, "file_gone")
    feedback_batches.close_due(FAR_FUTURE, db=db)
    assert [b["state"] for b in batches(db)] == ["undelivered"] and notes == []
