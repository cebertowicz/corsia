"""A decided suggestion never reads as orphaned; an accepted one is found where its replacement now sits."""

from __future__ import annotations

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_actions, comments as store
from doceditor.comment_anchoring import anchored_threads
from infra.database import get_db, write_transaction

BEFORE = "The quoted source stays here."
AFTER = "The better wording stays here."


@pytest.fixture
def scope(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        yield db, seed_document(db, "owner")
    finally:
        db.close()


def _suggest(db, artifact_id: str, *, replacement: str = "better wording") -> dict:
    row, _ = add_comment(db, artifact_id, replacement=replacement)
    # The helper anchors every comment at the start of the text; put it where BEFORE has its quote.
    store.update_positions(artifact_id, [{
        "id": row["id"], "from_off": 4, "to_off": 17, "anchor_prefix": "The ", "anchor_suffix": " stays here.",
    }], db=db)
    return row


def _decide(db, suggestion: dict, decision: str) -> None:
    with write_transaction(db, scope="test_decided_anchoring") as connection:
        comment_actions.record_decision(
            suggestion["id"], decision=decision, decided_by="Robin", decided_by_thread_id=None, db=connection,
        )


def _read(db, artifact_id: str, text: str, comment_id: str) -> dict:
    threads, checked = anchored_threads(artifact_id, None, db, text=text)
    assert checked
    return next(thread for thread in threads if thread["id"] == comment_id)


def test_a_pending_suggestion_whose_text_changed_still_reads_orphaned(scope):
    db, artifact_id = scope
    suggestion = _suggest(db, artifact_id)

    assert _read(db, artifact_id, BEFORE, suggestion["id"])["orphaned"] is False
    assert _read(db, artifact_id, AFTER, suggestion["id"])["orphaned"] is True


def test_an_accepted_suggestion_is_found_on_its_replacement(scope):
    db, artifact_id = scope
    suggestion = _suggest(db, artifact_id)
    _decide(db, suggestion, "accept")

    row = _read(db, artifact_id, AFTER, suggestion["id"])

    assert row["orphaned"] is False
    assert (row["from_off"], row["to_off"]) == (4, 4 + len("better wording"))
    persisted = store.get_comment(suggestion["id"], db=db)
    assert (persisted["from_off"], persisted["to_off"]) == (4, 4 + len("better wording"))


def test_an_accepted_suggestion_whose_replacement_cannot_anchor_keeps_its_offsets(scope):
    db, artifact_id = scope
    suggestion = _suggest(db, artifact_id)
    _decide(db, suggestion, "accept")

    row = _read(db, artifact_id, "Everything was rewritten.", suggestion["id"])

    assert row["orphaned"] is False
    assert (row["from_off"], row["to_off"]) == (4, 17)


def test_an_accepted_deletion_reads_not_orphaned(scope):
    db, artifact_id = scope
    suggestion = _suggest(db, artifact_id, replacement="")
    _decide(db, suggestion, "accept")

    assert _read(db, artifact_id, "The  stays here.", suggestion["id"])["orphaned"] is False


def test_a_rejected_suggestion_anchors_on_its_quote_and_never_reads_orphaned(scope):
    db, artifact_id = scope
    suggestion = _suggest(db, artifact_id)
    _decide(db, suggestion, "reject")

    on_quote = _read(db, artifact_id, "Intro. " + BEFORE, suggestion["id"])
    assert on_quote["orphaned"] is False
    assert (on_quote["from_off"], on_quote["to_off"]) == (11, 24)
    assert _read(db, artifact_id, AFTER, suggestion["id"])["orphaned"] is False


def test_a_reopened_rejected_suggestion_anchors_normally_again(scope):
    db, artifact_id = scope
    suggestion = _suggest(db, artifact_id)
    _decide(db, suggestion, "reject")
    rejected = store.get_comment(suggestion["id"], db=db)
    store.update(suggestion["id"], comment_actions.reopen_fields(rejected, {"resolved": False}), db=db)

    assert _read(db, artifact_id, BEFORE, suggestion["id"])["orphaned"] is False
    assert _read(db, artifact_id, AFTER, suggestion["id"])["orphaned"] is True
