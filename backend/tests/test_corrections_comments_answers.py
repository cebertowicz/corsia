"""Document comments and decision answers feed the corrections loop like chat messages.

A user comment or a free-text decision answer is one classifier input and one
``measurement.correction`` fact. A flagged live arrival journals one triage
note in the fact's transaction, exactly as a chat correction does; a scan's
fact on a comment or decision answer writes no note.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from core.correction_attribution import attributed_facts, workflow_run_of
from infra.database import get_db
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_correction_notes import THREAD, _notes, _version, agent  # noqa: F401 - shared fixture

CORRECTION = {"label": "correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}
NOT_CORRECTION = {**CORRECTION, "label": "not_correction"}
UNREADABLE = {"label": "unreadable", "truncated": False, "original_tokens": 0, "failure_reason": "timeout"}
OPTIONS = [
    {"id": "A", "label": "Ship 2026", "detail": "Matches the draft."},
    {"id": "B", "label": "Ship 2027", "detail": "Matches the plan."},
]
QUESTION = "Which date?\nRelease copy\nAccuracy against schedule\n- Ship 2026: Matches the draft.\n- Ship 2027: Matches the plan."
ANSWER = "Use 2027, not 2026."


@pytest.fixture
def db(isolated_db, monkeypatch):
    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    monkeypatch.setattr("jobs.corrections_realtime.notify", lambda *_args: None)
    connection = get_db()
    seed_thread(connection, "owner", title="Style")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _seed_decision(db, decision_id="dec", *, thread="owner", text=ANSWER, revision=7):
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


def _activate(db) -> None:
    """Make ``_version()`` the active version, the one gate a chat scan fact notes under."""
    from core.corrections_recording import ACTIVE_VERSION_SETTING, version_key
    from infra.settings import set_setting

    set_setting(ACTIVE_VERSION_SETTING, {"key": version_key(_version())}, db=db)
    db.commit()


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


def _subjects(db):
    return [details["correction_subject"] for details, _ in _correction_notes(db)]


def _user_comment(db, artifact):
    row, _ = add_comment(db, artifact, author_kind="user", author="Steve", author_id=None)
    db.commit()
    return str(row["id"])


def test_comment_and_decision_sources_attribute_from_recorded_target_and_arrival_time(db):
    """Non-message sources have both a fact-time target and canonical source arrival time."""
    _activate(db)
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
               "VALUES ('attribution-plan','thread','owner','active',1,'t','t')")
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,"
                   "commitment_id,requirements_revision,revision,created_at,updated_at) "
                   "VALUES ('attributed-node','attribution-plan',1,'task','x','x','active','attributed-node',1,1,'t','t')")
    db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,plan_id,"
               "plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES ('source-turn','owner',0,'m','completed','2026-01-01T00:00:00Z','boot','attribution-plan',"
               "'attributed-node','attempt','attributed-node','execution')")
    comment = _user_comment(db, seed_document(db, "owner", name="attribution.md"))
    _seed_decision(db)
    assert _record(db, "doc_comment", comment) is True
    assert _record(db, "plan_decision_answer", "dec") is True

    facts = {(fact.source_kind, fact.source_id): fact for fact in attributed_facts(db)}
    expected = {("doc_comment", comment), ("plan_decision_answer", "dec:7")}
    assert set(facts) == expected
    assert all(fact.node_id == "attributed-node" and fact.source_at is not None for fact in facts.values())
    assert {key: workflow_run_of(fact.node_id, {"attributed-node": "run"}, ["run"])
            for key, fact in facts.items()} == {key: "run" for key in expected}


def test_each_user_comment_and_free_text_answer_records_one_fact_and_one_note(db):
    artifact = seed_document(db, "owner", name="p.md")
    comments = [_user_comment(db, artifact) for _ in range(2)]
    _seed_decision(db)

    for comment in comments:
        assert _record(db, "doc_comment", comment) is True
    assert _record(db, "plan_decision_answer", "dec") is True

    subjects = [f"doc-comment:{comment}" for comment in comments] + ["plan-decision-answer:dec:7"]
    assert sorted(subject for subject, _ in _facts(db)) == sorted(subjects)
    assert all("body" not in attributes and "content" not in attributes for _, attributes in _facts(db))
    assert sorted(_subjects(db)) == sorted(subjects)  # one note per flagged fact, never one per round


def test_agent_comment_is_never_candidate(db):
    from jobs.corrections_recording import candidate_for_source
    from jobs.corrections_scanning import pending_source_candidate_keys

    artifact = seed_document(db, "owner", name="p.md")
    row, _ = add_comment(db, artifact, author_kind="agent", author_id="owner")
    db.commit()

    assert candidate_for_source("doc_comment", str(row["id"])) is None
    assert pending_source_candidate_keys(_version(), 10) == []


def test_decision_listed_pick_without_text_records_nothing(db):
    from jobs.corrections_recording import candidate_for_source
    from jobs.corrections_scanning import pending_source_candidate_keys

    _seed_decision(db, text=None)

    assert candidate_for_source("plan_decision_answer", "dec") is None
    assert pending_source_candidate_keys(_version(), 10) == []
    assert _facts(db) == [] and _correction_notes(db) == []


def test_classifier_input_is_the_persons_words_after_what_the_agent_put_to_them(db):
    from core.corrections_input import build_text
    from jobs.corrections_recording import candidate_for_source

    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    _seed_decision(db)

    on_comment = candidate_for_source("doc_comment", comment)
    on_decision = candidate_for_source("plan_decision_answer", "dec")

    assert (on_comment.content, on_comment.context) == ("Please check this point.", (("Agent", "quoted source"),))
    assert (on_decision.content, on_decision.context) == (ANSWER, (("Agent", QUESTION),))
    assert build_text(list(on_decision.context), on_decision.content) == f"[Agent] {QUESTION}\n[OWNER] {ANSWER}"
    assert on_decision.source_id == "dec:7" and on_decision.thread_id == "owner"


def test_a_flagged_arrival_journals_its_note_in_the_facts_transaction(db, monkeypatch):
    import infra.passive_changes as passive_changes

    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    real_append = passive_changes.append_thread_context_note

    def append_then_fail(*args, **kwargs):
        real_append(*args, **kwargs)
        raise RuntimeError("journal failed")

    monkeypatch.setattr(passive_changes, "append_thread_context_note", append_then_fail)
    with pytest.raises(RuntimeError, match="journal failed"):
        _record(db, "doc_comment", comment)
    assert _facts(db) == [] and _correction_notes(db) == []  # rolled back together: the scan retries it

    monkeypatch.setattr(passive_changes, "append_thread_context_note", real_append)
    assert _record(db, "doc_comment", comment) is True
    (details, _summary), = _correction_notes(db)
    assert details == {
        "chip": None, "kind": "correction_note", "correction_subject": f"doc-comment:{comment}",
        "reoffer_unwritten": True,
    }


def test_a_flagged_source_that_is_not_a_correction_writes_no_note(db):
    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    _seed_decision(db)

    assert _record(db, "doc_comment", comment, payload=NOT_CORRECTION) is True
    assert _record(db, "plan_decision_answer", "dec", payload=UNREADABLE) is True

    assert len(_facts(db)) == 2 and _correction_notes(db) == []


def test_each_note_names_its_source_quotes_the_person_and_asks_for_one_triage(db):
    from core.correction_notes import TRIAGE_TOGETHER

    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    _seed_decision(db)
    _record(db, "doc_comment", comment)
    _record(db, "plan_decision_answer", "dec")

    (_, on_comment), (_, on_decision) = _correction_notes(db)
    assert on_comment.startswith(f"Correction recorded on your artifact p.md (comment {comment}, sent ")
    assert '"Please check this point."' in on_comment and "when the comment arrived" in on_comment
    assert on_decision.startswith('Correction recorded on your plan decision "Which date?" (answered ')
    assert f'"{ANSWER}"' in on_decision and "when the answer arrived" in on_decision
    key_line = "\nSubject key for triage and outcome: "
    assert all(key_line in note and note.split(key_line)[0].endswith("\n" + TRIAGE_TOGETHER)
               for note in (on_comment, on_decision))


def test_a_chat_note_does_not_ask_to_triage_comments_together():
    from core.correction_notes import TRIAGE_TOGETHER, note_text

    note = note_text("m1", "That is wrong.", "2026-10-19T00:00:00Z")
    assert TRIAGE_TOGETHER not in note and note.endswith("when the message arrived, do nothing more.")


def test_a_failing_comment_notify_never_fails_the_committed_comment(monkeypatch):
    from jobs.corrections_recording import notify_doc_comment

    def broken(*_args):
        raise RuntimeError("worker gone")

    monkeypatch.setattr("jobs.corrections_realtime.notify", broken)
    notify_doc_comment("c1")


@pytest.mark.parametrize("source", ["doc_comment", "plan_decision_answer"])
def test_a_scan_fact_for_a_comment_or_answer_writes_no_note(db, source):
    from jobs.corrections_recording import _record_fact
    from jobs.corrections_scanning import hydrate_candidates, pending_source_candidate_keys

    _activate(db)  # even under the active version, where a chat scan fact may note
    if source == "doc_comment":
        _user_comment(db, seed_document(db, "owner", name="p.md"))
    else:
        _seed_decision(db)

    (candidate,) = hydrate_candidates(pending_source_candidate_keys(_version(), 10))

    assert (candidate.source_kind, candidate.arrived) == (source, False)
    assert _record_fact(candidate, CORRECTION, _version()) is True
    assert len(_facts(db)) == 1 and _correction_notes(db) == []


def test_a_duplicate_fact_under_another_version_writes_no_second_note(db):
    comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
    _seed_decision(db)

    for kind, source in (("doc_comment", comment), ("plan_decision_answer", "dec")):
        assert _record(db, kind, source) is True
        assert _record(db, kind, source) is False  # a replay under the same version
        assert _record(db, kind, source, version=_version("v2")) is True  # its own fact, no second note

    assert len(_facts(db)) == 4
    assert _subjects(db) == [f"doc-comment:{comment}", "plan-decision-answer:dec:7"]


@pytest.mark.parametrize("source", ["doc_comment", "plan_decision_answer"])
@pytest.mark.parametrize("target", ["missing", "archived", "ui"])
def test_missing_archived_or_ui_target_keeps_fact_without_note(db, source, target):
    thread = {"missing": "gone", "archived": "owner", "ui": "ui"}[target]
    if target == "archived":
        db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id='owner'")
    if source == "doc_comment":
        comment = _user_comment(db, seed_document(db, "owner", name="p.md"))
        if target != "archived":  # handed to another recipient than the document's owner
            db.execute("UPDATE doc_feedback_batches SET recipient_id=? WHERE id=?", (thread, batches(db)[0]["id"]))
        outcome = _record(db, "doc_comment", comment)
    else:
        _seed_decision(db, thread=thread)
        outcome = _record(db, "plan_decision_answer", "dec")

    assert outcome is True and len(_facts(db)) == 1
    assert _correction_notes(db) == []


@pytest.mark.asyncio
async def test_typed_source_arrival_is_classified_as_arrived_through_the_shared_queue(monkeypatch):
    from dataclasses import replace as with_fields
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.corrections_recording import CorrectionCandidate
    from jobs import corrections_realtime

    source = CorrectionCandidate("doc_comment", "comment", "owner", None, "Wrong.", "2026-10-19T00:00:00Z")
    seen = []
    monkeypatch.setattr(
        corrections_realtime, "candidate_for_source",
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

    assert agent.deliver() is None  # the running turn ends with no successor of its own
    assert _notes(agent.say("m2")) == ["plan-decision-answer:dec:7"]


def test_notes_queue_no_input_of_their_own(db):
    def queued() -> list[str]:
        return [row[0] for row in db.execute("SELECT id FROM messages WHERE delivery_state='queued' ORDER BY seq")]

    artifact = seed_document(db, "owner", name="p.md")
    comments = [_user_comment(db, artifact) for _ in range(2)]
    _seed_decision(db)
    before = queued()

    for comment in comments:
        _record(db, "doc_comment", comment)
    _record(db, "plan_decision_answer", "dec")

    assert len(_correction_notes(db)) == 3 and queued() == before


def test_the_scan_reads_only_its_windows_comments(db, monkeypatch):
    """The comment scan is a range on ``idx_doc_comments_user_created``, not a read of every comment."""
    import infra.database as database
    from jobs.corrections_scanning import pending_source_candidate_keys

    plan: list[str] = []
    real_get_db = database.get_db

    class Explaining:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            if "FROM doc_comments c" in sql:
                plan.extend(row[3] for row in self._connection.execute("EXPLAIN QUERY PLAN " + sql, params))
            return self._connection.execute(sql, params)

        def close(self):
            self._connection.close()

    monkeypatch.setattr(database, "get_db", lambda: Explaining(real_get_db()))
    pending_source_candidate_keys(_version(), 20, since="2000-01-01")

    assert any(
        step.startswith("SEARCH c") and "idx_doc_comments_user_created" in step and "created_at>" in step for step in plan
    ), plan
