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
from tests.test_correction_notes import THREAD, _named, _notes, _version, agent  # noqa: F401 - shared fixture
from tests.test_plan_decision_answer import _human, _raised_decision
from tests.test_plan_decisions import _mid, _seed
from tests.test_review_bar_answer import _answer, _ask

CORRECTION = {"label": "correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}
UNREADABLE = {"label": "unreadable", "truncated": False, "original_tokens": 0, "failure_reason": "timeout"}
OPTIONS = [
    {"id": "A", "label": "Ship 2026", "detail": "Matches the draft."},
    {"id": "B", "label": "Ship 2027", "detail": "Matches the plan."},
]


@pytest.fixture
def db(isolated_db, monkeypatch):
    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
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


def _record(db, source_kind, source_id, *, arrived=True, payload=CORRECTION, version=None):
    """One fact for a hydrated source; returns ``(inserted, wake_target)``."""
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
    db.commit()
    return batch


def test_user_comment_records_one_fact_and_one_lone_batch_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)

    assert _record(db, "doc_comment", comment) == (True, None)  # a comment alone never notes
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
    assert summary.startswith("Correction batch recorded on your artifact") and "1 comments" in summary


def test_ask_round_comments_record_each_fact_and_one_batch_note_with_all_subjects(db):
    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    comments = [_user_comment(db, artifact) for _ in range(3)]
    for comment in comments:
        assert _record(db, "doc_comment", comment) == (True, None)

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

    assert _record(db, "doc_comment", comment)[0]
    assert not _record(db, "doc_comment", comment)[0]
    assert _record(db, "doc_comment", comment, version=_version("v2"))[0]
    batch = _deliver_batch(db)
    from core.correction_notes import queue_feedback_batch_note

    assert queue_feedback_batch_note(db, batch) is None  # the delivery replay writes nothing

    assert _record(db, "plan_decision_answer", "dec") == (True, "owner")
    assert _record(db, "plan_decision_answer", "dec") == (False, None)
    # A later classifier version records its own fact and never a second note.
    assert _record(db, "plan_decision_answer", "dec", version=_version("v2")) == (True, None)

    assert len(_facts(db)) == 4
    assert [details["note_key"] for details, _ in _correction_notes(db)] == [
        f"doc-feedback:{batch['id']}", "decision-answer:dec:7",
    ]


@pytest.mark.parametrize("source", ["doc_comment", "plan_decision_answer"])
@pytest.mark.parametrize("target", ["missing", "archived", "ui"])
def test_missing_archived_or_ui_target_keeps_fact_without_note(db, source, target):
    from core.correction_notes import queue_feedback_batch_note

    thread = {"missing": "gone", "archived": "owner", "ui": "ui"}[target]
    if target == "archived":
        db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id='owner'")
    if source == "doc_comment":
        comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
        outcome = _record(db, "doc_comment", comment)
        batch = {**batches(db)[0], "recipient_id": thread}
        assert queue_feedback_batch_note(db, batch) is None
    else:
        _seed_decision(db, thread=thread)
        outcome = _record(db, "plan_decision_answer", "dec")

    assert outcome == (True, None) and len(_facts(db)) == 1
    assert _correction_notes(db) == [] and _wakes(db) == []


def test_comment_and_answer_backfill_records_fact_without_note(db):
    from core.corrections_recording import ACTIVE_VERSION_SETTING, version_key
    from infra.settings import set_setting
    from jobs.corrections_scanning import hydrate_candidates, pending_source_candidate_keys

    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    _deliver_batch(db)  # the batch left before the scan found its comment
    _seed_decision(db)
    set_setting(ACTIVE_VERSION_SETTING, {"key": version_key(_version())}, db=db)
    db.commit()

    candidates = hydrate_candidates(pending_source_candidate_keys(_version(), 10))

    assert {(item.source_kind, item.arrived) for item in candidates} == {
        ("doc_comment", False), ("plan_decision_answer", False),
    }
    from jobs.corrections_recording import _record_fact

    assert [_record_fact(item, CORRECTION, _version()) for item in candidates] == [(True, None)] * 2
    assert {subject for subject, _ in _facts(db)} == {f"doc-comment:{comment}", "plan-decision-answer:dec:7"}
    assert _correction_notes(db) == []


def test_decision_other_text_records_fact_and_note_with_question(db):
    from jobs.corrections_recording import candidate_for_source

    _seed_decision(db)
    candidate = candidate_for_source("plan_decision_answer", "dec")

    assert candidate.source_id == "dec:7" and candidate.thread_id == "owner" and candidate.context == ()
    assert candidate.content.startswith("Decision question: Which date?\nContext: Release copy\n")
    assert "B — Ship 2027 — Matches the plan." in candidate.content
    assert candidate.content.endswith("Owner answer: Use 2027, not 2026.")

    assert _record(db, "plan_decision_answer", "dec") == (True, "owner")

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
        "jobs.corrections_dispatch.notify_from_thread", lambda kind, source: seen.append((kind, source)),
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
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from core.corrections_recording import CorrectionCandidate
    from jobs import corrections_dispatch

    source = CorrectionCandidate("doc_comment", "comment", "owner", None, "Wrong.", "2026-10-19T00:00:00Z")
    seen = []
    monkeypatch.setattr(
        corrections_dispatch, "candidate_for_source",
        lambda source_kind, source_id: seen.append((source_kind, source_id)) or source,
    )
    worker = MagicMock()
    worker.clock = lambda: 0.0
    worker.classify_arrival = AsyncMock(return_value=SimpleNamespace(kind="recorded"))

    await corrections_dispatch.dispatch_arrival(worker, "doc_comment", "comment", 0.0)

    assert seen == [("doc_comment", "comment")]
    assert worker.classify_arrival.await_args.args[0].arrived is True
    worker.queue.task_done.assert_called_once()


def test_note_appended_during_running_turn_still_wakes(agent, main_db):
    _seed_decision(main_db, thread=THREAD)
    assert agent.say("m1", "Start the work.") is not None  # a turn is now running

    assert _record(main_db, "plan_decision_answer", "dec") == (True, THREAD)

    wake = main_db.execute(
        "SELECT id,role,delivery_state,content,metadata FROM messages WHERE id LIKE 'correction-wake:%'"
    ).fetchone()
    assert wake["id"] == "correction-wake:decision-answer:dec:7" and wake["delivery_state"] == "queued"
    assert wake["role"] == "system" and not wake["content"].startswith("[EVENT")
    metadata = json.loads(wake["metadata"])
    assert (metadata["type"], metadata["event_disposition"]) == ("context_event", "wake")
    assert [event["chip"] for event in metadata["context_events"]] == [None] * len(metadata["context_events"])
    items = agent.deliver()  # the running turn ends; the hidden wake is its successor
    assert _named(items) == [wake["id"]] and _notes(items) == ["plan-decision-answer:dec:7"]


def test_note_replay_and_batch_close_queue_one_wake(db):
    _seed_decision(db)
    for version in ("v1", "v1", "v2"):
        _record(db, "plan_decision_answer", "dec", version=_version(version))
    assert _wakes(db) == ["correction-wake:decision-answer:dec:7"]

    artifact = seed_document(db, "owner", name="p.md")
    _ask(db, artifact)
    comments = [_user_comment(db, artifact) for _ in range(2)]
    for comment in comments:
        _record(db, "doc_comment", comment)
    _answer(db, artifact, "changes_requested")

    # The answer's own wake already carries the batch note, so it adds no second one.
    assert _wakes(db) == ["correction-wake:decision-answer:dec:7"]
    carrying = [
        row[0] for row in db.execute(
            "SELECT metadata FROM messages WHERE id LIKE 'context-digest:%' AND delivery_state='queued'"
        ) if "review-round:" in row[0]
    ]
    assert len(carrying) == 1


def test_backfill_note_less_fact_queues_no_wake(db):
    _seed_decision(db)
    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))

    assert _record(db, "plan_decision_answer", "dec", arrived=False) == (True, None)
    assert _record(db, "doc_comment", comment, arrived=False) == (True, None)
    assert _record(db, "plan_decision_answer", "dec", arrived=False, payload={**CORRECTION, "label": "not_correction"},
                   version=_version("v2")) == (True, None)

    assert _wakes(db) == [] and _correction_notes(db) == []
