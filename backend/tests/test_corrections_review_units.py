"""Each comment belongs to exactly one review unit, and every trigger notes it once.

Regressions for the review of the comments-and-answers corrections change: the
arrival API takes its source kind, a scan fact notes under the active version,
a comment's unit is the ask open when it was made, a note queues no input of
its own, and the scan recovers a unit key the worker never took with the notes
arrival writes.
"""

from __future__ import annotations

import json

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
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
    _worker_takes,
    db,
)
from tests.test_review_bar_answer import _answer as _clear_with_answer, _ask, _legacy_duplicate_ask


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

    monkeypatch.setattr(corrections_recording, "COMMENT_PAGE", page)
    _activate(db, "2000-01-01T00:00:00+00:00")
    *expected, _round_batch = _missed_units(db)

    await corrections_recording.recover_comment_notes("2000-01-01T00:00:00+00:00")

    # The round's comment belongs to its round, so the round's batch writes no note of its own.
    assert sorted(details["note_key"] for details, _ in _correction_notes(db)) == sorted(expected)
    await corrections_recording.recover_comment_notes("2000-01-01T00:00:00+00:00")
    assert len(_correction_notes(db)) == 2  # a repeat pass re-reads the comments; note_key stops the writes


@pytest.mark.asyncio
async def test_recovery_reads_nothing_made_before_activation(db):
    from jobs.corrections_recording import recover_comment_notes

    _missed_units(db)
    await recover_comment_notes("2000-01-01T00:00:00+00:00")  # no active version: no note is owed
    assert _correction_notes(db) == []

    _activate(db, "2999-01-01T00:00:00+00:00")  # the comments were made before this version became active
    await recover_comment_notes("2000-01-01T00:00:00+00:00")
    assert _correction_notes(db) == []


@pytest.mark.asyncio
async def test_recovery_applies_the_scan_facts_exact_activation_instant(db):
    from doceditor import feedback_batches
    from jobs.corrections_recording import recover_comment_notes

    made = {}
    for name, at in (("before", "2026-10-19T12:00:00.250000+00:00"), ("after", "2026-10-19T12:00:00.750000+00:00")):
        artifact = seed_document(db, "owner", name=f"{name}.md")
        comment = _user_comment(db, artifact)
        db.execute("UPDATE doc_comments SET created_at=? WHERE id=?", (at, comment))
        db.commit()
        _record(db, "doc_comment", comment)
        batch = db.execute("SELECT id FROM doc_feedback_batches WHERE artifact_id=?", (artifact,)).fetchone()[0]
        assert feedback_batches.close_batch(db, batch, "send")[0]
        db.commit()
        made[name] = f"doc-feedback:{batch}"
    _drop_queued_keys()
    # Within the same second as both comments: the selector's floor is cut to the second.
    _activate(db, "2026-10-19T12:00:00.500000+00:00")

    await recover_comment_notes("2000-01-01T00:00:00+00:00")

    assert [details["note_key"] for details, _ in _correction_notes(db)] == [made["after"]]


@pytest.mark.asyncio
async def test_a_candidate_versions_backfill_never_notes_a_unit_the_active_version_found_clean(db):
    from doceditor import feedback_batches
    from jobs.corrections_recording import recover_comment_notes

    _activate(db, "2000-01-01T00:00:00+00:00")  # v1 is active
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    _record(db, "doc_comment", comment, payload={**CORRECTION, "label": "not_correction"})
    batch = db.execute("SELECT id FROM doc_feedback_batches WHERE artifact_id=?", (artifact,)).fetchone()[0]
    assert feedback_batches.close_batch(db, batch, "send")[0]
    db.commit()
    _drop_queued_keys()
    # v2 fills its measurement window and disagrees; it is not active.
    assert _record(db, "doc_comment", comment, arrived=False, version=_version("v2")) is True

    await recover_comment_notes("2000-01-01T00:00:00+00:00")

    assert _correction_notes(db) == []


@pytest.mark.asyncio
async def test_a_round_answered_with_no_app_loop_is_noted_by_startups_recovery(db, monkeypatch):
    from infra.db_async import run_in_db
    from jobs import corrections_realtime as realtime
    from jobs.corrections_recording import recover_comment_notes

    worker = realtime.CorrectionsWorker()
    monkeypatch.setattr(worker, "_start_task", lambda coroutine: coroutine.close())
    monkeypatch.setattr(realtime, "notify", worker.notify)  # the real hand-off, not the suite's queue
    _activate(db, "2000-01-01T00:00:00+00:00")
    artifact = seed_document(db, "owner", name="p.md")
    hold = _ask(db, artifact)
    _record(db, "doc_comment", _user_comment(db, artifact))
    before = worker.snapshot()

    # On a DB pool thread with no application loop: the round's key reaches no queue.
    await run_in_db(_clear_with_answer, db, artifact, "changes_requested")

    assert worker.snapshot() == before and worker.queue.empty() and _correction_notes(db) == []
    await recover_comment_notes("2000-01-01T00:00:00+00:00")  # what startup's owed scan runs
    (details, _summary), = _correction_notes(db)
    assert details["note_key"].startswith("review-round:"), (details, hold)


def _note_rows(db) -> list[tuple[str, str, list[str], str]]:
    """Every correction note as ``(recipient, note_key, subjects, summary)``, in a stable order."""
    rows = db.execute(
        "SELECT object_id,details_json,summary FROM context_change_journal WHERE object_kind='thread_context'"
    ).fetchall()
    notes = [(str(row[0]), json.loads(row[1]), str(row[2])) for row in rows]
    return sorted(
        (recipient, details["note_key"], details["correction_subjects"], summary)
        for recipient, details, summary in notes if details.get("kind") == "correction_note"
    )


def _overlapping_asks(db, _monkeypatch) -> None:
    artifact = seed_document(db, "owner", name="overlap.md")
    _ask(db, artifact, title="First")
    first = _user_comment(db, artifact)
    _legacy_duplicate_ask(db, artifact, title="Second")
    second = _user_comment(db, artifact)
    for comment in (first, second):
        _record(db, "doc_comment", comment)
    _clear_with_answer(db, artifact, "changes_requested")


def _round_older_than_the_rounds_read(db, monkeypatch) -> None:
    import plans.user_ask as user_ask

    monkeypatch.setattr(user_ask, "ASK_ROUNDS_LIMIT", 1)  # the first round is read as its batch
    artifact = seed_document(db, "owner", name="older.md")
    for title in ("First", "Second"):
        _ask(db, artifact, title=title)
        _record(db, "doc_comment", _user_comment(db, artifact))
        _clear_with_answer(db, artifact, "changes_requested")


def _reply_to_an_agent_comment(db, _monkeypatch) -> None:
    from doceditor import feedback_batches

    seed_thread(db, "reviewer")
    artifact = seed_document(db, "owner", name="reply.md")
    agent_row, _ = add_comment(db, artifact, author_kind="agent", author_id="reviewer")
    db.commit()
    reply, _ = add_comment(
        db, artifact, author_kind="user", author="Steve", author_id=None, parent_id=str(agent_row["id"]),
    )
    db.commit()
    _record(db, "doc_comment", str(reply["id"]))
    for batch in batches(db, state="open"):  # the reply joined a batch to each of them
        assert feedback_batches.close_batch(db, batch["id"], "send")[0]
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", [_overlapping_asks, _round_older_than_the_rounds_read, _reply_to_an_agent_comment],
)
async def test_recovery_writes_exactly_the_notes_arrival_writes(db, monkeypatch, scenario):
    from jobs.corrections_recording import recover_comment_notes

    _activate(db, "2000-01-01T00:00:00+00:00")
    scenario(db, monkeypatch)
    _worker_takes(db)  # arrival: the facts' notes, then every unit key the commits handed the worker
    arrived = _note_rows(db)
    assert arrived

    db.execute("DELETE FROM context_change_journal WHERE json_extract(details_json,'$.kind')='correction_note'")
    db.commit()
    await recover_comment_notes("2000-01-01T00:00:00+00:00")

    assert _note_rows(db) == arrived


def test_recovery_reads_positive_comment_facts_by_time(db):
    from jobs.corrections_scanning import positive_comment_keys

    import infra.database as database

    plan: list[str] = []
    real_get_db = database.get_db

    class Explaining:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            if "FROM outcome_events e" in sql:
                plan.extend(row[3] for row in self._connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
            return self._connection.execute(sql, params)

        def close(self):
            self._connection.close()

    database.get_db = lambda: Explaining(real_get_db())
    try:
        positive_comment_keys(_version(), "2000-01-01", 5)
    finally:
        database.get_db = real_get_db

    assert any("idx_outcome_events_name_ts" in step and "ts>" in step for step in plan), plan
