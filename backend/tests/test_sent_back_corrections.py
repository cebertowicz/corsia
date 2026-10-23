"""Sent-back plan rejections become correction evidence without agent triage."""

from __future__ import annotations

import json
import logging
import sqlite3

from infra.schema import SCHEMA_SQL
from plans import service

FEEDBACK = "The implementation misses the empty input path."


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,created_at,updated_at) VALUES "
        "('parent','user','project','t','t')"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,created_at,updated_at) VALUES "
        "('worker','user','project','parent','t','t')"
    )
    plan = service.ensure_plan("thread", "parent", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Worker task", acceptance_criteria="Works", db=db,
    )
    db.execute("UPDATE plan_nodes SET executor_thread_id='worker' WHERE id=?", (node.id,))
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,actor_id,provenance,event,payload_json,created_at)
           VALUES ('reject-event',?,'parent','test','review_rejected',?,?)""",
        (node.id, json.dumps({"feedback": FEEDBACK}), "2026-10-01T12:00:00+00:00"),
    )
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,actor_id,provenance,event,payload_json,created_at)
           VALUES ('revision-event',?,'parent','test','revision_created',?,?)""",
        (node.id, json.dumps({"source_node_id": node.id, "feedback": FEEDBACK}), "2026-10-01T12:00:00+00:00"),
    )
    db.commit()
    return db


def test_sent_back_fact_uses_the_rejection_event_and_nearest_persistent_owner():
    from core.sent_back_corrections import (
        backfill_sent_back_corrections,
        prepare_sent_back_correction,
        record_sent_back_correction,
        sent_back_rejection_event_ids,
    )

    db = _db()
    try:
        notes_before = db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0]
        assert sent_back_rejection_event_ids(db) == ["reject-event"]
        fact = prepare_sent_back_correction("reject-event", db=db)
        assert fact is not None
        assert fact.thread_id == "parent"
        assert fact.project_id == "project"
        assert backfill_sent_back_corrections(db) == 1
        assert not record_sent_back_correction(fact, db=db)
        assert backfill_sent_back_corrections(db) == 0

        row = db.execute(
            "SELECT dedupe_key,subject_key,agent_thread_id,attributes FROM outcome_events"
        ).fetchone()
        assert row["dedupe_key"] == "correction:sent_back:reject-event"
        assert row["subject_key"] == "plan-review-rejected:reject-event"
        assert row["agent_thread_id"] == "parent"
        assert json.loads(row["attributes"]) == {
            "source_kind": "sent_back", "source_id": "reject-event", "thread_id": "parent",
            "label": "correction",
        }
        assert FEEDBACK not in " ".join(str(value) for value in row)
        assert db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == notes_before
    finally:
        db.close()


def test_rejection_write_records_only_its_review_rejected_event():
    from plans import rejection
    from tests.plan_review_helpers import submit_review_evidence

    db = _db()
    try:
        plan = service.ensure_plan("thread", "parent", db=db)
        node = service.add_node(plan.id, kind="task", title="Review me", acceptance_criteria="Done", db=db)
        service.transition_node(node.id, "active", actor_id="parent", db=db)
        db.execute("UPDATE plan_nodes SET executor_thread_id='parent' WHERE id=?", (node.id,))
        submit_review_evidence(node.id, db=db, actor_id="parent")
        rejected, _revision, replayed = rejection.reject_submission(node.id, feedback=FEEDBACK, actor_id="parent", db=db)

        assert not replayed
        assert rejected.state == "closed"
        rejection_event = db.execute(
            "SELECT id FROM plan_node_events WHERE node_id=? AND event='review_rejected'", (node.id,),
        ).fetchone()[0]
        closed_events = db.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND new_state='closed'", (node.id,),
        ).fetchone()[0]
        facts = db.execute(
            "SELECT subject_key FROM outcome_events WHERE event_name='measurement.correction'"
        ).fetchall()
        assert closed_events == 1
        assert [row["subject_key"] for row in facts] == [f"plan-review-rejected:{rejection_event}"]
    finally:
        db.close()


def test_delegated_rejection_records_one_fact_for_the_reviewed_node_only():
    from core.sent_back_corrections import backfill_sent_back_corrections, sent_back_rejection_event_ids
    from tests.test_plan_rejection_revision import _db as rejection_db, _reject, _returned

    db = rejection_db(None)
    try:
        plan, delegated, _worker_plan, leaves = _returned(db, "Worker step", "Second worker step")
        _reject(db, plan.id, delegated.id)

        reviewed_event = db.execute(
            "SELECT id FROM plan_node_events WHERE node_id=? AND event='review_rejected'", (delegated.id,),
        ).fetchone()["id"]
        leaf_events = db.execute(
            "SELECT id FROM plan_node_events WHERE event='review_rejected' AND node_id IN (?, ?)",
            tuple(leaf.id for leaf in leaves),
        ).fetchall()
        assert len(leaf_events) == 2
        assert sent_back_rejection_event_ids(db) == [reviewed_event]

        facts = db.execute(
            "SELECT subject_key,agent_thread_id FROM outcome_events WHERE event_name='measurement.correction'"
        ).fetchall()
        assert [(row["subject_key"], row["agent_thread_id"]) for row in facts] == [
            (f"plan-review-rejected:{reviewed_event}", "parent")
        ]

        db.execute("DELETE FROM outcome_events")
        db.commit()
        assert backfill_sent_back_corrections(db) == 1
    finally:
        db.close()


def test_worker_rejection_of_a_subworker_remains_a_verdict():
    from core.sent_back_corrections import sent_back_rejection_event_ids
    from plans import mutations, rejection
    from tests.plan_review_helpers import submit_review_evidence
    from tests.test_plan_rejection_revision import _db as rejection_db

    db = rejection_db(None)
    try:
        db.execute(
            """INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES ('subworker','user-1','project-1','worker','test/model','t','t')"""
        )
        worker_plan = service.ensure_plan("thread", "worker", db=db)
        delegated = service.add_node(
            worker_plan.id, kind="task", title="Sub-worker task", acceptance_criteria="Done",
            spawn_kind="test", db=db,
        )
        service.transition_node(delegated.id, "active", actor_id="worker", db=db)
        mutations.attach_spawned_thread(delegated.id, "subworker", db=db)
        submit_review_evidence(delegated.id, db=db, actor_id="worker")

        rejected, _revision, replayed = rejection.reject_submission(
            delegated.id, feedback=FEEDBACK, actor_id="worker", db=db,
        )
        event_id = db.execute(
            "SELECT id FROM plan_node_events WHERE node_id=? AND event='review_rejected'", (delegated.id,),
        ).fetchone()["id"]
        fact = db.execute(
            "SELECT subject_key,agent_thread_id FROM outcome_events WHERE event_name='measurement.correction'"
        ).fetchone()

        assert not replayed
        assert rejected.state == "closed"
        assert sent_back_rejection_event_ids(db) == [event_id]
        assert tuple(fact) == (f"plan-review-rejected:{event_id}", "parent")
    finally:
        db.close()


def test_recorder_failure_does_not_fail_a_committed_rejection(monkeypatch, caplog):
    from core import sent_back_corrections
    from plans import rejection
    from tests.plan_review_helpers import submit_review_evidence

    db = _db()
    try:
        plan = service.ensure_plan("thread", "parent", db=db)
        node = service.add_node(plan.id, kind="task", title="Review me", acceptance_criteria="Done", db=db)
        service.transition_node(node.id, "active", actor_id="parent", db=db)
        db.execute("UPDATE plan_nodes SET executor_thread_id='parent' WHERE id=?", (node.id,))
        submit_review_evidence(node.id, db=db, actor_id="parent")

        def fail_recording(*_args, **_kwargs):
            raise RuntimeError("measurement unavailable")

        monkeypatch.setattr(sent_back_corrections, "record_rejection_correction", fail_recording)
        with caplog.at_level(logging.WARNING, logger="core.sent_back_corrections"):
            rejected, _revision, replayed = rejection.reject_submission(
                node.id, feedback=FEEDBACK, actor_id="parent", db=db,
            )

        assert not replayed
        assert rejected.state == "closed"
        assert "sent-back correction recording failed for rejection event" in caplog.text
    finally:
        db.close()


def test_sent_back_facts_are_split_by_source_and_do_not_enter_trial_verdicts():
    from core.sent_back_corrections import prepare_sent_back_correction, record_sent_back_correction
    from core.correction_attribution import attributed_facts

    db = _db()
    try:
        fact = prepare_sent_back_correction("reject-event", db=db)
        assert fact is not None
        assert record_sent_back_correction(fact, db=db)

        rows = db.execute(
            """SELECT json_extract(attributes, '$.source_kind') AS source_kind, COUNT(*) AS n
               FROM outcome_events WHERE event_name='measurement.correction'
               GROUP BY source_kind"""
        ).fetchall()
        assert [(row["source_kind"], row["n"]) for row in rows] == [("sent_back", 1)]
        assert attributed_facts(db) == []
    finally:
        db.close()
