"""Each comment belongs to exactly one review unit, and every trigger notes it once.

Regressions for the review of the comments-and-answers corrections change: the
arrival API takes its source kind, a scan fact notes under the active version,
a comment's unit is the ask open when it was made, and a note written after a
queued input rides that input instead of waking a second turn.
"""

from __future__ import annotations

import pytest

from doc_feedback_helpers import seed_document
from tests.test_correction_notes import _version  # noqa: F401
from tests.test_corrections_comments_answers import (  # noqa: F401 - shared fixture and helpers
    CORRECTION,
    _correction_notes,
    _deliver_batch,
    _record,
    _seed_decision,
    _user_comment,
    _wakes,
    db,
)
from tests.test_review_bar_answer import _answer, _ask, _legacy_duplicate_ask


def test_notify_requires_the_source_kind():
    from jobs import corrections_realtime

    with pytest.raises(TypeError):
        corrections_realtime.notify("message-id")  # type: ignore[call-arg]


def test_a_scan_fact_under_the_active_version_writes_the_round_note(db):
    from core.corrections_recording import ACTIVE_VERSION_SETTING, version_key
    from infra.settings import set_setting

    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    comment = _user_comment(db, artifact)
    _answer(db, artifact, "changes_requested")  # the classifier was down: no fact yet
    set_setting(ACTIVE_VERSION_SETTING, {"key": version_key(_version())}, db=db)

    assert _record(db, "doc_comment", comment, arrived=False) == (True, ["owner"])

    (details, _summary), = _correction_notes(db)
    assert details["note_key"].startswith("review-round:")
    assert details["correction_subjects"] == [f"doc-comment:{comment}"]


def test_a_comment_made_before_a_later_ask_keeps_its_batch_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    batch = _deliver_batch(db)
    _ask(db, artifact)  # raised after the comment: not its round
    _answer(db, artifact, "approved")

    assert _record(db, "doc_comment", comment) == (True, ["owner"])

    (details, _summary), = _correction_notes(db)
    assert details["note_key"] == f"doc-feedback:{batch['id']}"


def test_two_asks_cleared_together_write_one_note_per_round(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact, title="First")
    first = _user_comment(db, artifact)
    _legacy_duplicate_ask(db, artifact, title="Second")
    second = _user_comment(db, artifact)
    for comment in (first, second):
        _record(db, "doc_comment", comment)

    _answer(db, artifact, "changes_requested")

    notes = {details["note_key"]: details["correction_subjects"] for details, _ in _correction_notes(db)}
    assert len(notes) == 2 and all(key.startswith("review-round:") for key in notes)
    assert sorted(notes.values()) == sorted([[f"doc-comment:{first}"], [f"doc-comment:{second}"]])


def test_a_note_after_an_input_this_transaction_queued_rides_it_instead_of_waking(db):
    from core.correction_notes import input_floor, queue_fact_notes
    from events.delivery import enqueue_event_wake
    from jobs.corrections_recording import candidate_for_source

    _seed_decision(db)
    candidate = candidate_for_source("plan_decision_answer", "dec")
    floor = input_floor(db)
    enqueue_event_wake("owner", "webhook.github.pr_merged", "PR merged", message_id="event-1", db=db)
    wakes: list[str] = []

    queue_fact_notes(db, candidate, "correction", _version(), "event-0", floor=floor, wakes=wakes)
    db.commit()

    assert wakes == [] and _wakes(db) == []
    carrying = [
        row[0] for row in db.execute(
            "SELECT id FROM messages WHERE id LIKE 'context-digest:%' AND delivery_state='queued'"
            " AND metadata LIKE '%decision-answer:dec:7%'"
        )
    ]
    assert len(carrying) == 1


def _steps(db, action) -> int:
    """SQLite VM steps ``action`` takes: a deterministic stand-in for its read cost."""
    count = [0]

    def tick():
        count[0] += 1
        return 0

    db.set_progress_handler(tick, 1000)
    try:
        action()
    finally:
        db.set_progress_handler(None, 0)
    return count[0] * 1000


def _round_clear_steps(db, *, journal_rows: int, name: str) -> int:
    if journal_rows:
        db.executemany(
            "INSERT INTO context_change_journal (object_kind,object_id,change_kind,summary,details_json,created_at)"
            " VALUES ('thread_context',?,'note','x','{\"kind\":\"correction_note\",\"note_key\":\"k\"}','t')",
            ((f"other-{index % 50}",) for index in range(journal_rows)),
        )
        # A live thread's digest cursor is near the head; only its own rows are read.
        db.execute("UPDATE threads SET last_context_change_seq=(SELECT MAX(seq) FROM context_change_journal)")
        db.commit()
    artifact = seed_document(db, "owner", name=name)
    _ask(db, artifact)
    for _ in range(20):
        _record(db, "doc_comment", _user_comment(db, artifact))
    before = len(_correction_notes(db))
    steps = _steps(db, lambda: _answer(db, artifact, "changes_requested"))
    assert len(_correction_notes(db)) == before + 1  # one round note for 20 comments
    return steps


def test_the_round_clear_does_not_read_other_threads_journal_rows(db):
    baseline = _round_clear_steps(db, journal_rows=0, name="a.md")

    loaded = _round_clear_steps(db, journal_rows=50_000, name="b.md")

    # Linear in the owner's own notes, not in the journal: 50k unrelated rows add almost nothing.
    assert loaded < baseline * 1.5, (baseline, loaded)
