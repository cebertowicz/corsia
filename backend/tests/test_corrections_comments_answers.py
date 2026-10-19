"""Document comments and decision answers feed the corrections loop like chat messages.

A positive comment or free-text decision answer writes the same
``measurement.correction`` fact; its agent-only triage note is one per ask round
or feedback batch for comments and one per answer for decisions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db
from infra.schema import SCHEMA_SQL
from plans import decisions
from tests.test_correction_notes import THREAD, _activate, _named, _notes, _version, agent  # noqa: F401 - shared fixture
from tests.test_plan_decision_answer import _human, _raised_decision
from tests.test_plan_decisions import _mid, _seed
from tests.test_review_bar_answer import _answer as _clear_with_answer, _ask

CORRECTION = {"label": "correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}
UNREADABLE = {"label": "unreadable", "truncated": False, "original_tokens": 0, "failure_reason": "timeout"}
OPTIONS = [
    {"id": "A", "label": "Ship 2026", "detail": "Matches the draft."},
    {"id": "B", "label": "Ship 2027", "detail": "Matches the plan."},
]
#: The keys commits handed the corrections worker, in order, until ``_worker_takes`` runs them.
QUEUED: list[tuple[str, str]] = []


@pytest.fixture
def db(isolated_db, monkeypatch):
    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    QUEUED.clear()
    monkeypatch.setattr("jobs.corrections_realtime.notify", lambda kind, key: QUEUED.append((kind, key)))
    connection = get_db()
    seed_thread(connection, "owner", title="Style")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _seed_decision(db, decision_id="dec", *, thread="owner", text="Use 2027, not 2026.", revision=7):
    body = {
        "state": "answered", "thread_id": thread, "revision": revision,
        "answered_at": "2026-10-19T00:00:00Z", "title": "Which date?",
        "context": "Release copy", "tradeoff": "Accuracy against schedule",
        "options": OPTIONS, "recommendation": "B",
        "answer": {"option_id": "other" if text else "A", "text": text},
    }
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES (?,'local','plan-decision',?,'active',?,'t','t')""",
        (f"artifact-{decision_id}", decision_id, json.dumps({"plan_decision": body})),
    )
    db.commit()


def _worker_takes(db) -> None:
    """The worker takes each queued unit key and writes the notes it owes, as ``_arrival`` does."""
    from core.corrections_recording import UNIT_KINDS
    from jobs.corrections_recording import write_unit

    db.commit()
    while QUEUED:
        kind, key = QUEUED.pop(0)
        if kind in UNIT_KINDS:
            write_unit(kind, key)


def _answer(db, artifact_id, verdict, note=None):
    """The person answers the ask; the worker then takes the rounds it ended."""
    result = _clear_with_answer(db, artifact_id, verdict, note)
    _worker_takes(db)
    return result


def _record(db, source_kind, source_id, *, arrived=True, payload=CORRECTION, version=None):
    """One fact for a hydrated source; returns whether it was inserted."""
    from jobs.corrections_recording import _record_fact, candidate_for_source

    db.commit()
    candidate = replace(candidate_for_source(source_kind, source_id), arrived=arrived)
    return _record_fact(candidate, payload, version or _version())


def _facts(db):
    return [
        (row["subject_key"], json.loads(row["attributes"])) for row in db.execute(
            "SELECT subject_key,attributes FROM outcome_events "
            "WHERE event_name='measurement.correction' ORDER BY ts,id"
        )
    ]


def _correction_notes(db):
    """The journaled correction notes as ``(details, summary)``, oldest first."""
    rows = db.execute(
        "SELECT details_json,summary FROM context_change_journal "
        "WHERE object_kind='thread_context' ORDER BY seq"
    ).fetchall()
    return [
        (json.loads(row[0]), row[1]) for row in rows
        if json.loads(row[0]).get("kind") == "correction_note"
    ]


def _wakes(db):
    return [
        row[0] for row in db.execute(
            "SELECT id FROM messages WHERE id LIKE 'correction-wake:%' ORDER BY seq"
        )
    ]


def _user_comment(db, artifact):
    row, _ = add_comment(db, artifact, author_kind="user", author="Steve", author_id=None)
    db.commit()
    return str(row["id"])


def _deliver_batch(db):
    batch = batches(db)[0]
    assert feedback_batches.close_batch(db, batch["id"], "send")[0]
    _worker_takes(db)
    return batch


def test_user_comment_records_one_fact_and_one_lone_batch_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)

    assert _record(db, "doc_comment", comment) is True  # undelivered: no unit to note yet
    assert _correction_notes(db) == []
    batch = _deliver_batch(db)

    (subject, attributes), = _facts(db)
    assert subject == f"doc-comment:{comment}"
    assert attributes["source_kind"] == "doc_comment" and attributes["source_id"] == comment
    assert "body" not in attributes and "content" not in attributes
    (details, summary), = _correction_notes(db)
    assert details == {
        "chip": None, "kind": "correction_note", "note_key": f"doc-feedback:{batch['id']}",
        "correction_subjects": [subject], "reoffer_unwritten": True,
    }
    assert summary.startswith("Correction batch recorded on your artifact") and "1 comment," in summary


def test_a_delivery_only_hands_the_worker_its_key_once_it_commits(db):
    from infra.database import write_transaction

    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    _record(db, "doc_comment", comment)
    batch = batches(db)[0]

    with pytest.raises(RuntimeError, match="rolled back"):
        with write_transaction(db, scope="test_delivery") as connection:
            assert feedback_batches.close_batch(connection, batch["id"], "send")[0]
            raise RuntimeError("rolled back")
    assert QUEUED == []

    assert feedback_batches.close_batch(db, batch["id"], "send")[0]
    db.commit()
    # The delivery writes no note; the worker, sole writer, does once it takes the key.
    assert QUEUED == [("doc-feedback", batch["id"])] and _correction_notes(db) == []
    _worker_takes(db)
    assert [details["note_key"] for details, _ in _correction_notes(db)] == [f"doc-feedback:{batch['id']}"]


def test_an_answer_only_hands_the_worker_its_rounds_and_writes_no_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    _record(db, "doc_comment", _user_comment(db, artifact))

    _clear_with_answer(db, artifact, "changes_requested")

    hold = db.execute("SELECT id FROM plan_node_holds WHERE owner_kind='user'").fetchone()[0]
    # The answer also delivered the round's batch; its comment is still noted with the round.
    assert ("review-round", hold) in QUEUED and _correction_notes(db) == []
    _worker_takes(db)
    assert [details["note_key"] for details, _ in _correction_notes(db)] == [f"review-round:{hold}"]


def test_ask_round_comments_record_each_fact_and_one_batch_note_with_all_subjects(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    comments = [_user_comment(db, artifact) for _ in range(3)]
    for comment in comments:
        assert _record(db, "doc_comment", comment) is True  # the round is still open
    assert _correction_notes(db) == []

    _answer(db, artifact, "changes_requested")

    assert [subject for subject, _ in _facts(db)] == [f"doc-comment:{comment}" for comment in comments]
    (details, summary), = _correction_notes(db)  # one note, not one per comment
    assert details["note_key"].startswith("review-round:")
    assert sorted(details["correction_subjects"]) == sorted(f"doc-comment:{comment}" for comment in comments)
    assert "3 comments" in summary
    assert "Re-check the whole artifact for untagged instances of that cause" in summary


def test_ask_approved_without_comments_records_no_fact_or_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)

    _answer(db, artifact, "approved", "Ship it.")

    assert _facts(db) == [] and _correction_notes(db) == [] and _wakes(db) == []


def test_agent_comment_is_never_candidate(db):
    from jobs.corrections_recording import candidate_for_source
    from jobs.corrections_scanning import pending_source_candidate_keys

    artifact = seed_document(db, "owner", name="p.md")
    row, _ = add_comment(db, artifact, author_kind="agent", author_id="owner")
    db.commit()

    assert candidate_for_source("doc_comment", str(row["id"])) is None
    assert pending_source_candidate_keys(_version(), 10) == []


def test_comment_and_answer_replay_are_deduplicated(db):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    _seed_decision(db)

    assert _record(db, "doc_comment", comment)
    assert not _record(db, "doc_comment", comment)
    assert _record(db, "doc_comment", comment, version=_version("v2"))
    batch = _deliver_batch(db)
    from jobs.corrections_recording import write_unit

    write_unit("doc-feedback", batch["id"])  # the delivery replay writes nothing

    assert _record(db, "plan_decision_answer", "dec") is True
    assert _record(db, "plan_decision_answer", "dec") is False
    # A later classifier version records its own fact and never a second note.
    assert _record(db, "plan_decision_answer", "dec", version=_version("v2")) is True

    assert len(_facts(db)) == 4
    assert [details["note_key"] for details, _ in _correction_notes(db)] == [
        f"doc-feedback:{batch['id']}", "decision-answer:dec:7",
    ]


@pytest.mark.parametrize("source", ["doc_comment", "plan_decision_answer"])
@pytest.mark.parametrize("target", ["missing", "archived", "ui"])
def test_missing_archived_or_ui_target_keeps_fact_without_note(db, source, target):
    from jobs.corrections_recording import write_unit

    thread = {"missing": "gone", "archived": "owner", "ui": "ui"}[target]
    if target == "archived":
        db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id='owner'")
    if source == "doc_comment":
        comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
        outcome = _record(db, "doc_comment", comment)
        batch_id = batches(db)[0]["id"]
        db.execute("UPDATE doc_feedback_batches SET recipient_id=?,state='delivered' WHERE id=?", (thread, batch_id))
        db.commit()
        write_unit("doc-feedback", batch_id)
    else:
        _seed_decision(db, thread=thread)
        outcome = _record(db, "plan_decision_answer", "dec")

    assert outcome is True and len(_facts(db)) == 1
    assert _correction_notes(db) == [] and _wakes(db) == []


@pytest.mark.parametrize("active", [False, True])
def test_scan_facts_note_only_under_the_active_version(db, active):
    from jobs.corrections_recording import _record_fact
    from jobs.corrections_scanning import hydrate_candidates, pending_source_candidate_keys

    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    batch = _deliver_batch(db)  # the batch left before the scan found its comment
    _seed_decision(db)
    if active:  # a later scan pass; without it, the activation backfill
        _activate(db, "2000-01-01T00:00:00+00:00")

    candidates = hydrate_candidates(pending_source_candidate_keys(_version(), 10))

    assert {(item.source_kind, item.arrived) for item in candidates} == {
        ("doc_comment", False), ("plan_decision_answer", False),
    }
    assert all(_record_fact(item, CORRECTION, _version()) for item in candidates)
    assert {subject for subject, _ in _facts(db)} == {f"doc-comment:{comment}", "plan-decision-answer:dec:7"}
    expected = {f"doc-feedback:{batch['id']}", "decision-answer:dec:7"} if active else set()
    assert {details["note_key"] for details, _ in _correction_notes(db)} == expected


def test_decision_other_text_records_fact_and_note_with_question(db):
    from jobs.corrections_recording import candidate_for_source

    _seed_decision(db)
    candidate = candidate_for_source("plan_decision_answer", "dec")

    assert candidate.source_id == "dec:7" and candidate.thread_id == "owner" and candidate.context == ()
    assert candidate.content.startswith("Decision question: Which date?\nContext: Release copy\n")
    assert "B — Ship 2027 — Matches the plan." in candidate.content
    assert candidate.content.endswith("Owner answer: Use 2027, not 2026.")

    assert _record(db, "plan_decision_answer", "dec") is True

    (subject, attributes), = _facts(db)
    assert subject == "plan-decision-answer:dec:7" and attributes["source_id"] == "dec:7"
    (details, summary), = _correction_notes(db)
    assert details["note_key"] == "decision-answer:dec:7"
    assert details["correction_subjects"] == [subject]
    assert summary.startswith("Correction recorded on your plan decision dec, sent ")
    assert "“Use 2027, not 2026.”" in summary and "Proposed tier: none yet; you set it." in summary


def test_decision_listed_pick_without_text_records_nothing(db):
    from jobs.corrections_recording import candidate_for_source
    from jobs.corrections_scanning import pending_source_candidate_keys

    _seed_decision(db, text=None)

    assert candidate_for_source("plan_decision_answer", "dec") is None
    assert pending_source_candidate_keys(_version(), 10) == []
    assert _facts(db) == [] and _correction_notes(db) == []


@pytest.mark.parametrize(
    ("answer", "enqueued"),
    [({"option_id": "A"}, False), ({"option_id": "other", "other_text": "Neither."}, True)],
)
def test_decision_answer_is_enqueued_only_when_it_carries_text(monkeypatch, answer, enqueued):
    seen = []
    monkeypatch.setattr(
        "jobs.corrections_realtime.notify", lambda kind, source: seen.append((kind, source)),
    )
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    try:
        _plan, _source, decision = _raised_decision(connection)
        decisions.answer(
            decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
            actor=_human(), provenance="p", db=connection, **answer,
        )
        connection.commit()
    finally:
        connection.close()

    assert seen == ([("plan_decision_answer", decision["id"])] if enqueued else [])


@pytest.mark.asyncio
async def test_typed_source_arrival_is_classified_as_arrived_through_the_shared_queue(monkeypatch):
    from dataclasses import replace as with_fields
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.corrections_recording import CorrectionCandidate
    from jobs import corrections_realtime, corrections_recording

    source = CorrectionCandidate("doc_comment", "comment", "owner", None, "Wrong.", "2026-10-19T00:00:00Z")
    seen = []
    monkeypatch.setattr(
        corrections_recording, "candidate_for_source",
        lambda source_kind, source_id: seen.append((source_kind, source_id)) or with_fields(source, arrived=True),
    )
    worker = corrections_realtime.CorrectionsWorker()
    classify = AsyncMock(return_value=SimpleNamespace(kind="recorded"))
    monkeypatch.setattr(worker, "_classify", classify)
    worker.queue.put_nowait(("doc_comment", "comment", 0.0))  # what notify enqueues
    kind, source_id, enqueued_at = worker.queue.get_nowait()

    await worker._arrival(kind, source_id, enqueued_at)

    assert seen == [("doc_comment", "comment")]
    assert classify.await_args.args[0].arrived is True
    assert worker.queue.empty()


def test_a_note_written_during_a_running_turn_rides_the_next_input_and_starts_no_turn(agent, main_db):
    _seed_decision(main_db, thread=THREAD)
    assert agent.say("m1", "Start the work.") is not None  # a turn is now running

    assert _record(main_db, "plan_decision_answer", "dec") is True

    assert _wakes(main_db) == []
    assert agent.deliver() is None  # the running turn ends with no successor of its own
    assert _notes(agent.say("m2")) == ["plan-decision-answer:dec:7"]


def _queued(db) -> list[str]:
    return [row[0] for row in db.execute("SELECT id FROM messages WHERE delivery_state='queued' ORDER BY seq")]


def test_notes_queue_no_input_of_their_own(db):
    _seed_decision(db)
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    for comment in [_user_comment(db, artifact) for _ in range(2)]:
        _record(db, "doc_comment", comment)
    _clear_with_answer(db, artifact, "changes_requested")  # the answer's own inputs, before any note
    before = _queued(db)

    for version in ("v1", "v1", "v2"):
        _record(db, "plan_decision_answer", "dec", version=_version(version))
    _worker_takes(db)  # the round's note

    assert sorted(details["note_key"].split(":")[0] for details, _ in _correction_notes(db)) == [
        "decision-answer", "review-round",
    ]
    assert _queued(db) == before and _wakes(db) == []


def test_backfill_facts_write_no_note(db):
    _seed_decision(db)
    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))

    assert _record(db, "plan_decision_answer", "dec", arrived=False) is True
    assert _record(db, "doc_comment", comment, arrived=False) is True
    assert _record(db, "plan_decision_answer", "dec", arrived=False, payload={**CORRECTION, "label": "not_correction"},
                   version=_version("v2")) is True

    assert _wakes(db) == [] and _correction_notes(db) == []
