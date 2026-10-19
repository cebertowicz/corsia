"""Each comment belongs to exactly one review unit, and every trigger notes it once.

Regressions for the review of the comments-and-answers corrections change: the
arrival API takes its source kind, a scan fact notes under the active version,
a comment's unit is the ask open when it was made, a note queues no input of
its own, and the scan recovers a unit key the worker never took.
"""

from __future__ import annotations

import pytest

from doc_feedback_helpers import seed_document
from tests.test_correction_notes import _activate, _version  # noqa: F401
from tests.test_corrections_comments_answers import (  # noqa: F401 - shared fixture and helpers
    CORRECTION,
    QUEUED,
    _answer,
    _correction_notes,
    _deliver_batch,
    _record,
    _seed_decision,
    _user_comment,
    _wakes,
    db,
)
from tests.test_review_bar_answer import _ask, _legacy_duplicate_ask


def test_notify_requires_the_source_kind():
    from jobs import corrections_realtime

    with pytest.raises(TypeError):
        corrections_realtime.notify("message-id")  # type: ignore[call-arg]


def test_a_scan_fact_under_the_active_version_writes_the_round_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    comment = _user_comment(db, artifact)
    _answer(db, artifact, "changes_requested")  # the classifier was down: no fact yet
    _activate(db, "2000-01-01T00:00:00+00:00")

    assert _record(db, "doc_comment", comment, arrived=False) is True

    (details, _summary), = _correction_notes(db)
    assert details["note_key"].startswith("review-round:")
    assert details["correction_subjects"] == [f"doc-comment:{comment}"]


def test_a_comment_made_before_a_later_ask_keeps_its_batch_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    batch = _deliver_batch(db)
    _ask(db, artifact)  # raised after the comment: not its round
    _answer(db, artifact, "approved")

    assert _record(db, "doc_comment", comment) is True

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


def test_a_note_for_a_thread_with_queued_input_queues_nothing_of_its_own(db):
    from core.correction_notes import write_fact_notes
    from events.delivery import enqueue_event_wake
    from jobs.corrections_recording import candidate_for_source

    _seed_decision(db)
    candidate = candidate_for_source("plan_decision_answer", "dec")
    enqueue_event_wake("owner", "webhook.github.pr_merged", "PR merged", message_id="event-1", db=db)
    db.commit()
    queued = db.execute("SELECT COUNT(*) FROM messages WHERE delivery_state='queued'").fetchone()[0]

    write_fact_notes(db, candidate, "correction", _version(), "event-0")
    db.commit()

    (details, _summary), = _correction_notes(db)
    assert details["note_key"] == "decision-answer:dec:7" and _wakes(db) == []
    assert db.execute("SELECT COUNT(*) FROM messages WHERE delivery_state='queued'").fetchone()[0] == queued


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


def _drop_queued_keys() -> None:
    """The worker never took the commits' unit keys: a stop, a full queue or a failed write."""
    QUEUED.clear()


def _missed_units(db) -> tuple[str, str, str]:
    """A delivered batch and an answered round whose keys the worker lost: their note keys, then the round's batch."""
    from doceditor import feedback_batches

    lone = seed_document(db, "owner", name="lone.md")
    _record(db, "doc_comment", _user_comment(db, lone))
    batch = next(row for row in db.execute("SELECT id,artifact_id FROM doc_feedback_batches WHERE state='open'"))
    assert feedback_batches.close_batch(db, batch["id"], "send")[0]
    db.commit()
    asked = seed_document(db, "owner", name="asked.md")
    _ask(db, asked)
    _record(db, "doc_comment", _user_comment(db, asked))
    from tests.test_review_bar_answer import _answer as clear_with_answer

    clear_with_answer(db, asked, "changes_requested")
    hold = db.execute("SELECT id FROM plan_node_holds WHERE owner_kind='user'").fetchone()[0]
    round_batch = db.execute(
        "SELECT id FROM doc_feedback_batches WHERE id<>? AND state='delivered'", (batch["id"],),
    ).fetchone()[0]
    _drop_queued_keys()
    assert _correction_notes(db) == []
    return f"doc-feedback:{batch['id']}", f"review-round:{hold}", f"doc-feedback:{round_batch}"


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [1, 20])
async def test_the_scan_recovers_each_unit_whose_key_the_worker_never_took(db, monkeypatch, page):
    from jobs import corrections_recording
    from jobs.corrections_scanning import completed_unit_keys

    monkeypatch.setattr(corrections_recording, "UNIT_PAGE", page)
    _activate(db, "2000-01-01T00:00:00+00:00")
    *expected, round_batch = _missed_units(db)

    def selected() -> set[str]:
        return {f"{unit.kind}:{unit.unit_id}" for unit in completed_unit_keys("2000-01-01", 50)}

    assert selected() == {*expected, round_batch}

    await corrections_recording.recover_unit_notes("2000-01-01T00:00:00+00:00")

    assert {details["note_key"] for details, _ in _correction_notes(db)} == set(expected)
    # A noted unit is never selected again. The round's batch holds only the round's comment, so
    # it writes no note and stays selected until the window passes it: bounded, a known residual.
    assert selected() == {round_batch}
    await corrections_recording.recover_unit_notes("2000-01-01T00:00:00+00:00")
    assert len(_correction_notes(db)) == 2


@pytest.mark.asyncio
async def test_recovery_reads_nothing_completed_before_activation(db):
    from jobs.corrections_recording import recover_unit_notes

    _missed_units(db)
    await recover_unit_notes("2000-01-01T00:00:00+00:00")  # no active version: no note is owed
    assert _correction_notes(db) == []

    _activate(db, "2999-01-01T00:00:00+00:00")  # the units completed before this version became active
    await recover_unit_notes("2000-01-01T00:00:00+00:00")
    assert _correction_notes(db) == []


def test_recovery_finds_a_noted_unit_by_its_note_key_index(db):
    from jobs.corrections_scanning import completed_unit_keys

    import infra.database as database

    plan: list[str] = []
    real_get_db = database.get_db

    class Explaining:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            if "WITH rounds AS" in sql:
                plan.extend(row[3] for row in self._connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
            return self._connection.execute(sql, params)

        def close(self):
            self._connection.close()

    database.get_db = lambda: Explaining(real_get_db())
    try:
        completed_unit_keys("2000-01-01", 5)
    finally:
        database.get_db = real_get_db

    assert any("idx_context_changes_note_key" in step for step in plan), plan
