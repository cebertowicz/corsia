"""A comment fact that lands after its round was answered or its batch delivered still notes once.

The owner comments and presses Changes requested within seconds, while the
classifier may be unloaded or queued, so the final fact retries the round's
or batch's note key and the note rides the corrected agent's next input.
"""

from __future__ import annotations

import pytest

from doc_feedback_helpers import seed_document
from registry import attention
from tests.test_correction_notes import _version  # noqa: F401
from tests.test_corrections_comments_answers import (  # noqa: F401 - shared fixture and helpers
    CORRECTION,
    _answer,
    _ask,
    _backfilling,
    _correction_notes,
    _deliver_batch,
    _facts,
    _record,
    _user_comment,
    _wakes,
    _worker_takes,
    db,
)

NOT_CORRECTION = {**CORRECTION, "label": "not_correction"}


def _riding(db, note_key: str) -> int:
    """Queued passive digests carrying ``note_key``."""
    return db.execute(
        "SELECT COUNT(*) FROM messages WHERE id LIKE 'context-digest:%' AND delivery_state='queued'"
        " AND metadata LIKE ?", (f"%{note_key}%",),
    ).fetchone()[0]


def test_late_fact_after_answer_writes_one_round_note_without_a_wake(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    comment = _user_comment(db, artifact)

    _answer(db, artifact, "changes_requested")
    assert _correction_notes(db) == [] and _wakes(db) == []  # the fact is still pending

    assert _record(db, "doc_comment", comment) is True

    (details, summary), = _correction_notes(db)
    assert details["note_key"].startswith("review-round:")
    assert details["correction_subjects"] == [f"doc-comment:{comment}"]
    assert "Re-check the whole artifact" in summary
    # No wake: it rides the owner's next input, not the answer's already queued one.
    assert _wakes(db) == [] and _riding(db, details["note_key"]) == 0
    # A replay under another classifier version adds no second note.
    assert _record(db, "doc_comment", comment, version=_version("v2")) is True
    assert len(_correction_notes(db)) == 1


def test_two_late_facts_for_one_round_note_once_after_the_last(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    first, second = _user_comment(db, artifact), _user_comment(db, artifact)
    _answer(db, artifact, "changes_requested")

    assert _record(db, "doc_comment", first) is True  # the second is still pending
    assert _correction_notes(db) == [] and _wakes(db) == []

    assert _record(db, "doc_comment", second) is True
    (details, _summary), = _correction_notes(db)
    assert sorted(details["correction_subjects"]) == sorted(f"doc-comment:{c}" for c in (first, second))
    assert _wakes(db) == []


def test_negative_late_fact_with_no_other_positive_writes_no_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    first, second = _user_comment(db, artifact), _user_comment(db, artifact)
    _answer(db, artifact, "changes_requested")

    _record(db, "doc_comment", first, payload=NOT_CORRECTION)
    assert _record(db, "doc_comment", second, payload=NOT_CORRECTION) is True

    assert len(_facts(db)) == 2 and _correction_notes(db) == [] and _wakes(db) == []


def test_late_fact_for_a_delivered_lone_batch_writes_one_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    batch = _deliver_batch(db)  # delivered before the classifier answered
    assert _correction_notes(db) == []

    assert _record(db, "doc_comment", comment) is True

    (details, _summary), = _correction_notes(db)
    assert details["note_key"] == f"doc-feedback:{batch['id']}"
    assert _wakes(db) == []


def test_a_backfill_fact_never_notes_a_delivered_batch(db):
    _backfilling(db)
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    _deliver_batch(db)

    assert _record(db, "doc_comment", comment, arrived=False) is True

    assert _correction_notes(db) == [] and _wakes(db) == []


def _answered_round_with_no_other_input(db, monkeypatch):
    """A round whose answer queues no input of its own."""
    monkeypatch.setattr("plans.plan_wake.append", lambda *_args, **_kwargs: None)  # the asker gets no wake
    artifact = seed_document(db, "owner", name="q.md")
    _ask(db, artifact)
    _record(db, "doc_comment", _user_comment(db, artifact))
    _deliver_batch(db)  # delivered during the raised ask, so the answer closes no batch of its own
    return artifact


def test_an_answer_with_no_other_input_notes_without_a_wake(db, monkeypatch):
    artifact = _answered_round_with_no_other_input(db, monkeypatch)

    attention.clear_attention(artifact, outcome="answer", verdict="changes_requested", db=db)
    _worker_takes(db)

    # The note waits for the owner's next input: no note starts a turn.
    (details, _summary), = _correction_notes(db)
    assert details["note_key"].startswith("review-round:") and _wakes(db) == []
    assert db.execute("SELECT COUNT(*) FROM messages WHERE delivery_state='queued'").fetchone()[0] == 0
