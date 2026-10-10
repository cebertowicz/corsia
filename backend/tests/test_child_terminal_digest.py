"""A delegated step's terminal is a passive digest fact for the plan that delegated it.

The worker's plan hangs under the parent's plan. When a step of the worker's
settles -- accepted, accepted with follow-up, failed -- the parent learns of it
the way it learns of any change it did not make: as one line in the passive
digest its next Turn reads, never as a message that starts a Turn. A completed
step is not a decision the parent has to make, a block it has to clear, or a
review that keeps the worker from continuing; the group-ready wake at harvest
is where the result reaches the parent as something to act on.

Only the parent's own verdict on a delegated step is the parent's receipt.
"""

from __future__ import annotations

import json
import sqlite3

from infra.schema import SCHEMA_SQL
from plans import mutations, service
from tests.plan_review_helpers import submit_review_evidence

NOW = "2026-08-01T00:00:00+00:00"


def _db(monkeypatch) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    db.commit()
    return db


def _delegated(db):
    """The parent's delegated step and the worker's own active step under it."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(delegated.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Write the tests",
        acceptance_criteria="Tests pass", db=db,
    )
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    db.commit()
    return parent_plan, plan, node


def _facts(db, plan_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT change_kind,summary,details_json,actor_thread_id "
        "FROM context_change_journal WHERE object_kind='plan' AND object_id=? "
        "AND change_kind<>'plan.status' ORDER BY seq",
        (plan_id,),
    ).fetchall()


def _parent_messages(db) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT content,metadata FROM messages WHERE thread_id='parent' ORDER BY seq"
    ).fetchall()


def test_a_delegated_steps_completion_is_a_digest_fact_not_a_parent_message(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, plan, node = _delegated(db)
        submit_review_evidence(node.id, db=db, actor_id="worker")
        service.transition_node(
            node.id, "accepted", actor_id="worker",
            verification_result="passed", verified_by="worker", db=db,
        )
        db.commit()

        facts = _facts(db, parent_plan.id)
        assert [(f["change_kind"], f["summary"], f["actor_thread_id"]) for f in facts] == [
            (
                "plan.child_node_review_requested",
                "Delegated step put up for review: Write the tests", "worker",
            ),
            ("plan.child_node_completed", "Delegated step accepted: Write the tests", "worker"),
        ]
        details = json.loads(facts[-1]["details_json"])
        assert details["node_id"] == node.id
        assert details["child_plan_id"] == plan.id
        assert details["worker_thread_id"] == "worker"
        # Neither wakes: the worker reviewing its own step is a digest fact,
        # and its final return is the harvest's to carry.
        assert _parent_messages(db) == []
    finally:
        db.close()


def test_the_parents_next_input_reads_the_fact_in_its_digest(monkeypatch):
    from infra.thread_driver import admit_input

    db = _db(monkeypatch)
    try:
        _parent_plan, _plan, node = _delegated(db)
        submit_review_evidence(node.id, db=db, actor_id="worker")
        service.transition_node(
            node.id, "accepted", actor_id="worker",
            verification_result="passed", verified_by="worker", db=db,
        )
        db.commit()

        admit_input(
            "parent", role="user", content="carry on", intent="apply_now",
            message_id="user-input", turn_id="turn-1", boot_epoch="boot",
            admitted_at=NOW, db=db,
        )
        db.commit()

        digest = next(
            row for row in _parent_messages(db)
            if json.loads(row["metadata"] or "{}").get("event_type") == "context.digest"
        )
        assert "Delegated step accepted: Write the tests" in digest["content"]
        assert json.loads(digest["metadata"])["event_disposition"] == "passive"
    finally:
        db.close()


def test_every_step_that_settled_before_the_next_input_is_in_the_digest(monkeypatch):
    """Two steps, one parent plan, one digest: both facts, not the latest one.

    The digest collapses repeat changes to one object. A step's terminal names
    the step, so a second step settling is a second notice, not a repeat of
    the first.
    """
    from infra.thread_driver import admit_input

    db = _db(monkeypatch)
    try:
        parent_plan, plan, first = _delegated(db)
        second = service.add_node(
            plan.id, kind="task", title="Wire the endpoint", acceptance_criteria="Done", db=db,
        )
        service.transition_node(second.id, "active", actor_id="worker", db=db)
        for node in (first, second):
            submit_review_evidence(node.id, db=db, actor_id="worker")
            service.transition_node(
                node.id, "accepted", actor_id="worker",
                verification_result="passed", verified_by="worker", db=db,
            )
        db.commit()

        admit_input(
            "parent", role="user", content="carry on", intent="apply_now",
            message_id="user-input", turn_id="turn-1", boot_epoch="boot",
            admitted_at=NOW, db=db,
        )
        db.commit()

        digest = next(
            row for row in _parent_messages(db)
            if json.loads(row["metadata"] or "{}").get("event_type") == "context.digest"
        )
        assert "Delegated step accepted: Write the tests" in digest["content"]
        assert "Delegated step accepted: Wire the endpoint" in digest["content"]
        kinds = [f["change_kind"] for f in _facts(db, parent_plan.id)]
        assert kinds.count("plan.child_node_completed") == 2
    finally:
        db.close()


def test_a_delegated_steps_failure_is_a_digest_fact_too(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, _plan, node = _delegated(db)
        service.transition_node(node.id, "failed", actor_id="worker", db=db)
        db.commit()

        assert [(f["change_kind"], f["summary"]) for f in _facts(db, parent_plan.id)] == [
            ("plan.child_node_failed", "Delegated step failed: Write the tests"),
        ]
        assert _parent_messages(db) == []
    finally:
        db.close()


def test_the_parents_own_verdict_is_its_receipt_not_a_digest_fact(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, _plan, node = _delegated(db)
        submit_review_evidence(node.id, db=db, actor_id="worker")
        service.transition_node(
            node.id, "accepted", actor_id="parent",
            verification_result="passed", verified_by="parent", db=db,
        )
        db.commit()

        # The worker putting its own step up for review is its own doing, so it
        # is a fact the parent reads. The parent's verdict on it is the parent's
        # own act, so that is a receipt in its transcript and never a fact.
        assert [f["change_kind"] for f in _facts(db, parent_plan.id)] == [
            "plan.child_node_review_requested",
        ]
        receipts = [
            row for row in _parent_messages(db)
            if json.loads(row["metadata"] or "{}").get("type") == "activity_receipt"
        ]
        assert [r["content"] for r in receipts] == [
            "Delegated step accepted: Write the tests",
        ]
    finally:
        db.close()


def test_a_result_after_the_delegated_step_is_gone_is_not_the_parents(monkeypatch):
    """parent_plan_id outlives the delegation; the fact must not.

    Once the delegated step is archived, or the worker is no longer the
    parent's, a later result of the worker's is not this parent's to read.
    """
    db = _db(monkeypatch)
    try:
        parent_plan, plan, node = _delegated(db)
        delegated = db.execute(
            "SELECT id FROM plan_nodes WHERE plan_id=? AND spawned_thread_id='worker'",
            (parent_plan.id,),
        ).fetchone()["id"]
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (delegated,))
        db.commit()
        service.transition_node(node.id, "failed", actor_id="worker", db=db)

        later = service.add_node(
            plan.id, kind="task", title="Later work", acceptance_criteria="Done", db=db,
        )
        db.execute("UPDATE plan_nodes SET archived_at=NULL WHERE id=?", (delegated,))
        db.execute("UPDATE threads SET spawned_by=NULL WHERE id='worker'")
        db.commit()
        service.transition_node(later.id, "active", actor_id="worker", db=db)
        service.transition_node(later.id, "failed", actor_id="worker", db=db)
        db.commit()

        assert _facts(db, parent_plan.id) == []
    finally:
        db.close()


def test_a_step_of_an_undelegated_plan_writes_no_child_fact(monkeypatch):
    db = _db(monkeypatch)
    try:
        plan = service.ensure_plan("thread", "parent", db=db)
        node = service.add_node(
            plan.id, kind="task", title="Own work", acceptance_criteria="Done", db=db,
        )
        service.transition_node(node.id, "active", actor_id="parent", db=db)
        submit_review_evidence(node.id, db=db, actor_id="parent")
        service.transition_node(
            node.id, "accepted", actor_id="parent",
            verification_result="passed", verified_by="parent", db=db,
        )
        db.commit()

        assert db.execute(
            "SELECT COUNT(*) FROM context_change_journal "
            "WHERE change_kind LIKE 'plan.child_node_%'"
        ).fetchone()[0] == 0
        assert [r["content"] for r in _parent_messages(db)] == [
            "Top-level milestone accepted: Own work",
        ]
    finally:
        db.close()
