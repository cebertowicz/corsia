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
    revision = service.add_node(plan.id, kind="task", title="Revision", acceptance_criteria="Works", db=db)
    db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (node.id, revision.id))
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
        (revision.id, json.dumps({"source_node_id": node.id, "feedback": FEEDBACK}), "2026-10-01T12:00:00+00:00"),
    )
    db.commit()
    return db


def _reviewable(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    node = service.add_node(plan.id, kind="task", title="Review me", acceptance_criteria="Done", db=db)
    service.transition_node(node.id, "active", actor_id="parent", db=db)
    db.execute("UPDATE plan_nodes SET executor_thread_id='parent' WHERE id=?", (node.id,))
    from tests.plan_review_helpers import submit_review_evidence

    submit_review_evidence(node.id, db=db, actor_id="parent")
    db.commit()
    return node


def test_sent_back_fact_uses_the_rejection_event_and_nearest_persistent_owner():
    from core.sent_back_corrections import (
        prepare_sent_back_correction,
        reconcile_sent_back_corrections,
        record_sent_back_correction,
        unrecorded_rejection_event_ids,
    )

    db = _db()
    try:
        notes_before = db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0]
        assert unrecorded_rejection_event_ids(db, since=None, limit=10) == ["reject-event"]
        fact = prepare_sent_back_correction("reject-event", db=db)
        assert fact is not None
        assert fact.thread_id == "parent"
        assert fact.project_id == "project"
        assert reconcile_sent_back_corrections(db) == 1
        assert not record_sent_back_correction(fact, db=db)
        assert reconcile_sent_back_corrections(db) == 0
        assert unrecorded_rejection_event_ids(db, since=None, limit=10) == []

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


def test_rejection_records_its_fact_only_after_the_write_commits():
    from core import sent_back_corrections
    from plans import rejection

    db = _db()
    try:
        node = _reviewable(db)
        seen: list[bool] = []
        record = sent_back_corrections.record_rejection_correction

        def spy(rejection_event_id, *, db):
            seen.append(db.in_transaction)
            return record(rejection_event_id, db=db)

        sent_back_corrections.record_rejection_correction = spy
        try:
            rejected, _revision, replayed = rejection.reject_submission(
                node.id, feedback=FEEDBACK, actor_id="parent", db=db,
            )
        finally:
            sent_back_corrections.record_rejection_correction = record

        assert not replayed
        assert rejected.state == "closed"
        assert seen == [False]
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
    from core.sent_back_corrections import reconcile_sent_back_corrections, unrecorded_rejection_event_ids
    from tests.test_plan_rejection_revision import _db as rejection_db, _reject, _returned

    db = rejection_db(None)
    try:
        plan, delegated, _worker_plan, leaves = _returned(db, "Worker step", "Second worker step")
        # The production path: plans.boundary.mutate, a nested write on the caller's connection.
        _reject(db, plan.id, delegated.id)

        reviewed_event = db.execute(
            "SELECT id FROM plan_node_events WHERE node_id=? AND event='review_rejected'", (delegated.id,),
        ).fetchone()["id"]
        leaf_events = db.execute(
            "SELECT id FROM plan_node_events WHERE event='review_rejected' AND node_id IN (?, ?)",
            tuple(leaf.id for leaf in leaves),
        ).fetchall()
        assert len(leaf_events) == 2
        assert unrecorded_rejection_event_ids(db, since=None, limit=10) == []

        facts = db.execute(
            "SELECT subject_key,agent_thread_id FROM outcome_events WHERE event_name='measurement.correction'"
        ).fetchall()
        assert [(row["subject_key"], row["agent_thread_id"]) for row in facts] == [
            (f"plan-review-rejected:{reviewed_event}", "parent")
        ]

        db.execute("DELETE FROM outcome_events")
        db.commit()
        assert unrecorded_rejection_event_ids(db, since=None, limit=10) == [reviewed_event]
        assert reconcile_sent_back_corrections(db) == 1
    finally:
        db.close()


def test_worker_rejection_of_a_subworker_remains_a_verdict():
    from core.sent_back_corrections import unrecorded_rejection_event_ids
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
        assert unrecorded_rejection_event_ids(db, since=None, limit=10) == []
        assert tuple(fact) == (f"plan-review-rejected:{event_id}", "parent")
    finally:
        db.close()


def test_failed_live_recording_does_not_fail_the_rejection_and_the_nightly_recovery_records_it(monkeypatch, caplog):
    from core import sent_back_corrections
    from plans import rejection

    db = _db()
    try:
        node = _reviewable(db)
        real = sent_back_corrections.record_sent_back_correction

        def fail_recording(*_args, **_kwargs):
            raise RuntimeError("measurement unavailable")

        monkeypatch.setattr(sent_back_corrections, "record_sent_back_correction", fail_recording)
        with caplog.at_level(logging.WARNING, logger="core.sent_back_corrections"):
            rejected, _revision, replayed = rejection.reject_submission(
                node.id, feedback=FEEDBACK, actor_id="parent", db=db,
            )
        event_id = db.execute(
            "SELECT id FROM plan_node_events WHERE node_id=? AND event='review_rejected'", (node.id,),
        ).fetchone()[0]

        assert not replayed
        assert rejected.state == "closed"
        assert f"sent-back correction recording failed for rejection event {event_id}" in caplog.text
        assert db.execute("SELECT COUNT(*) FROM outcome_events").fetchone()[0] == 0

        monkeypatch.setattr(sent_back_corrections, "record_sent_back_correction", real)
        # The seeded historical rejection is recovered with it.
        assert sent_back_corrections.reconcile_sent_back_corrections(db) == 2
        assert db.execute(
            "SELECT COUNT(*) FROM outcome_events WHERE subject_key=?", (f"plan-review-rejected:{event_id}",),
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_recovery_is_bounded_by_the_newest_fact_and_the_batch_limit():
    from datetime import timedelta

    from core.sent_back_corrections import reconcile_sent_back_corrections, unrecorded_rejection_event_ids

    db = _db()
    try:
        node_id = db.execute("SELECT node_id FROM plan_node_events WHERE id='reject-event'").fetchone()[0]
        for index, day in enumerate(("2026-10-02", "2026-10-20", "2026-10-21"), start=1):
            db.execute(
                """INSERT INTO plan_node_events
                   (id,node_id,actor_id,provenance,event,payload_json,created_at)
                   VALUES (?,?,'parent','test','review_rejected',?,?)""",
                (f"reject-{index}", node_id, json.dumps({"feedback": FEEDBACK}), f"{day}T12:00:00+00:00"),
            )
        db.commit()

        # No fact yet: the whole history is eligible, a batch at a time, oldest first.
        assert unrecorded_rejection_event_ids(db, since=None, limit=2) == ["reject-event", "reject-1"]
        assert reconcile_sent_back_corrections(db, limit=2) == 2
        assert reconcile_sent_back_corrections(db, limit=2) == 2
        assert unrecorded_rejection_event_ids(db, since=None, limit=10) == []

        # A later fact bounds the rescan: an event older than the window is no longer read.
        db.execute("DELETE FROM outcome_events WHERE subject_key='plan-review-rejected:reject-event'")
        db.commit()
        assert reconcile_sent_back_corrections(db, window=timedelta(days=7)) == 0
        assert unrecorded_rejection_event_ids(db, since=None, limit=10) == ["reject-event"]
    finally:
        db.close()


def test_an_unresolvable_executor_is_logged_with_its_event_and_reason(caplog):
    from core.sent_back_corrections import prepare_sent_back_correction

    db = _db()
    try:
        # A delegation cycle never reaches a persistent owner.
        db.execute("UPDATE threads SET spawned_by='worker' WHERE id='parent'")
        db.commit()
        with caplog.at_level(logging.WARNING, logger="core.sent_back_corrections"):
            assert prepare_sent_back_correction("reject-event", db=db) is None
            assert prepare_sent_back_correction("no-such-event", db=db) is None

        assert "rejection event reject-event: executor parent has no persistent owner" in caplog.text
        assert "rejection event no-such-event: no such rejection" in caplog.text
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
