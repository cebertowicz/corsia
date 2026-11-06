"""A decided suggestion stays an open conversation while it has replies, whenever the reply arrives."""

from __future__ import annotations

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_actions, comments as store
from infra.database import get_db, write_transaction


@pytest.fixture
def scope(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        yield db, seed_document(db, "owner")
    finally:
        db.close()


def _decide(db, suggestion: dict, decision: str) -> None:
    with write_transaction(db, scope="test_decided_replies") as connection:
        comment_actions.record_decision(
            suggestion["id"], decision=decision, decided_by="Robin", decided_by_thread_id=None, db=connection,
        )


def _state(db, comment_id: str) -> tuple[str | None, int]:
    row = store.get_comment(comment_id, db=db)
    return row["decision"], row["resolved"]


@pytest.mark.parametrize("decision, recorded", [("accept", "accepted"), ("reject", "rejected")])
def test_a_decision_on_a_suggestion_with_replies_leaves_it_open(scope, decision, recorded):
    db, artifact_id = scope
    suggestion, _ = add_comment(db, artifact_id, replacement="better wording")
    add_comment(db, artifact_id, parent_id=suggestion["id"], author_kind="user", author_id=None, author="Robin")

    _decide(db, suggestion, decision)

    assert _state(db, suggestion["id"]) == (recorded, 0)


def test_a_decision_on_a_suggestion_with_no_replies_still_resolves_it(scope):
    db, artifact_id = scope
    suggestion, _ = add_comment(db, artifact_id, replacement="better wording")

    _decide(db, suggestion, "accept")

    assert _state(db, suggestion["id"]) == ("accepted", 1)


def test_any_reply_to_a_resolved_decided_suggestion_reopens_it_and_keeps_its_decision(scope):
    db, artifact_id = scope
    suggestion, _ = add_comment(db, artifact_id, replacement="better wording")
    _decide(db, suggestion, "reject")
    assert _state(db, suggestion["id"]) == ("rejected", 1)

    add_comment(db, artifact_id, parent_id=suggestion["id"], author_kind="user", author_id=None, author="Robin")
    assert _state(db, suggestion["id"]) == ("rejected", 0)

    # Resolved on purpose, it reopens again on the next reply, as a resolved comment does in Google Docs.
    store.update(suggestion["id"], {"resolved": True}, db=db)
    db.execute("UPDATE doc_comments SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (suggestion["id"],))
    db.commit()
    add_comment(db, artifact_id, parent_id=suggestion["id"], author_kind="user", author_id=None, author="Robin")
    assert _state(db, suggestion["id"]) == ("rejected", 0)
    # The reopen is an ordinary update: it moves the thread's update time like every other change to `resolved`.
    assert store.get_comment(suggestion["id"], db=db)["updated_at"] > "2000-01-01T00:00:00+00:00"


def test_a_reply_to_an_undecided_comment_changes_nothing_about_it(scope):
    db, artifact_id = scope
    suggestion, _ = add_comment(db, artifact_id, replacement="better wording")

    add_comment(db, artifact_id, parent_id=suggestion["id"], author_kind="user", author_id=None, author="Robin")

    assert _state(db, suggestion["id"]) == (None, 0)
