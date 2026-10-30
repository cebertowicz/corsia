"""Rejecting a reviewed submission creates its one linked revision task.

The submission stays where it was reviewed, terminal, with its evidence and the
feedback preserved on its events. Exactly one revision task is created beside
it -- linked, carrying the current requirements, the feedback, the assigned
worker and the original commitment -- and reaches its executor through normal
eligibility. A delegated step's verdict also lands on what the worker returned;
the revision is attached to the same worker, which is woken once with it in
hand. Replaying the rejection returns the same revision; rejecting the revised
submission creates its next revision.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from unittest.mock import AsyncMock

import pytest

from automation.continuation import runner
from automation.continuation.sources.harvest import HarvestSource
from infra.schema import SCHEMA_SQL
from plans import closure, mutations, service
from plans.boundary import PlanActor, mutate
from plans.operator_node_state import retired_display
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed

NOW = "2026-08-01T00:00:00+00:00"
PARENT = PlanActor("parent", "user-1", "project-1")
WORKER = PlanActor("worker", "user-1", "project-1")
FEEDBACK = "The endpoint returns 500 on an empty body; cover it and retry."


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


def _sweep(db) -> None:
    asyncio.run(runner.sweep(
        NOW, db=db, sources=[HarvestSource()], deliver=AsyncMock(return_value=True),
    ))


def _returned(db, *titles: str):
    """A harvested delegation: the worker returned ``titles``; the parent was woken."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(delegated.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    leaves = []
    for title in titles:
        leaf = service.add_node(
            plan.id, kind="task", title=title, acceptance_criteria="Done", db=db,
        )
        service.transition_node(leaf.id, "active", actor_id="worker", db=db)
        submit_review_evidence(leaf.id, db=db, evidence=f"{title} done", actor_id="worker")
        leaves.append(leaf)
    db.commit()
    _sweep(db)
    return parent_plan, delegated, plan, leaves


def _reject(
    db, plan_id: str, node_id: str, feedback: str = FEEDBACK, mutation_id: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    return mutate(
        plan_id, "reject",
        {
            "node_id": node_id, "feedback": feedback,
            "expected_revision": (
                service._node_row(db, node_id)["revision"]
                if expected_revision is None else expected_revision
            ),
        },
        actor=PARENT, provenance="test.rejection", mutation_id=mutation_id or uuid.uuid4().hex, db=db,
    ).result


def _events(db, node_id: str, event: str) -> list[dict]:
    return [
        json.loads(row["payload_json"] or "{}")
        for row in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=? "
            "ORDER BY created_at,rowid",
            (node_id, event),
        ).fetchall()
    ]


def _wakes(db, thread_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT id,content FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:%' "
        "ORDER BY seq,rowid",
        (thread_id,),
    ).fetchall()


def _attempt_rows(db, node_id: str) -> list[tuple]:
    """Every attempt row for the node, whole, so a replay can be shown to add none."""
    return [tuple(row) for row in db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? ORDER BY rowid", (node_id,),
    ).fetchall()]


def _state(db, node_id: str) -> str:
    return service.get_node(node_id, db=db).state



def _node_row(db, node_id: str) -> sqlite3.Row:
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _revision_of(db, node_id: str) -> sqlite3.Row:
    rows = db.execute(
        "SELECT * FROM plan_nodes WHERE follow_up_of=? ORDER BY rowid", (node_id,),
    ).fetchall()
    assert len(rows) == 1, [r["id"] for r in rows]
    return rows[0]


def _start(db, plan_id: str, node_id: str) -> dict:
    """The executor takes the revision up, as any task: selection is explicit."""
    return mutate(
        plan_id, "activate",
        {"node_id": node_id, "expected_revision": service._node_row(db, node_id)["revision"]},
        actor=PARENT, provenance="test.activate", mutation_id=uuid.uuid4().hex, db=db,
    ).result


def _plain_step(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(
        plan.id, kind="task", title="Draft the brief", description="Two pages, no fluff.",
        acceptance_criteria="The brief is written", db=db,
    )
    service.transition_node(step.id, "active", actor_id="parent", db=db)
    submit_review_evidence(step.id, db=db, actor_id="parent")
    db.commit()
    return plan, step


def test_rejection_revision_uses_the_source_canonical_intent_without_an_attachment(monkeypatch):
    db = _db(monkeypatch)
    try:
        plan = service.ensure_plan("thread", "parent", db=db)
        canonical = service.add_node(
            plan.id, kind="task", title="Canonical task", intent="Keep the agreed outcome",
            acceptance_criteria="The outcome is verified", db=db,
        )
        step = service.add_node(
            plan.id, kind="task", title="Inherited task", intent_node_id=canonical.id,
            acceptance_criteria="The inherited task is verified", db=db,
        )
        service.transition_node(step.id, "active", actor_id="parent", db=db)
        submit_review_evidence(step.id, db=db, actor_id="parent")
        db.commit()

        _reject(db, plan.id, step.id)

        revision = _revision_of(db, step.id)
        assert revision["intent_node_id"] == canonical.id
    finally:
        db.close()


def test_rejecting_a_step_creates_its_one_linked_revision(monkeypatch):
    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)

        result = _reject(db, plan.id, step.id)
        db.commit()

        # The submission stays where it was reviewed: settled, neutral, evidence and
        # feedback preserved. Sent back is not failed: it reads as a closed row
        # whose reason says it was sent back, never red.
        assert _state(db, step.id) == "closed"
        assert closure.settled_reasons(db, [_node_row(db, step.id)]) == {
            step.id: f"Sent back: {FEEDBACK}",
        }
        assert retired_display(_state(db, step.id))["tone_key"] != "failed"
        assert len(_events(db, step.id, "review_evidence_submitted")) == 1
        assert _events(db, step.id, "review_rejected") == [{"feedback": FEEDBACK}]
        # Exactly one revision, linked, with the same requirements and commitment.
        revision = _revision_of(db, step.id)
        source = _node_row(db, step.id)
        assert result["revision"] == {
            "node_id": revision["id"], "revision": revision["revision"], "source_node_id": step.id,
            "handoff": "actor",
        }
        assert revision["state"] == "pending"
        assert (revision["title"], revision["acceptance_criteria"], revision["description"]) == (
            source["title"], source["acceptance_criteria"], source["description"],
        )
        assert (revision["commitment_id"], revision["assignee"], revision["parent_id"]) == (
            source["commitment_id"], source["assignee"], source["parent_id"],
        )
        assert _events(db, revision["id"], "revision_created") == [
            {"source_node_id": step.id, "feedback": FEEDBACK},
        ]
        # The executor takes the revision up through normal eligibility, feedback in hand.
        from plans import close_next

        assert revision["id"] in {row["id"] for row in close_next.executor_next(db, plan.id, "parent")["open_tasks"]}
        _start(db, plan.id, revision["id"])
        block = close_next.executor_next(db, plan.id, "parent")
        assert block["selected"]["id"] == revision["id"]
        assert block["selected"]["feedback"] == FEEDBACK
    finally:
        db.close()


def test_replaying_the_rejection_returns_the_same_revision_and_changes_nothing(monkeypatch):
    """A retry carries the revision the caller saw before the rejection, and is honored."""
    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)
        seen = service._node_row(db, step.id)["revision"]
        first = _reject(db, plan.id, step.id)
        db.commit()
        revision = _revision_of(db, step.id)
        nodes_before = db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0]
        attempts_before = _attempt_rows(db, revision["id"])
        wakes_before = len(_wakes(db, "parent"))

        again = mutate(
            plan.id, "reject",
            {"node_id": step.id, "feedback": FEEDBACK, "expected_revision": seen},
            actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
        ).result
        db.commit()

        assert {k: v for k, v in again["revision"].items() if k != "handoff"} == {
            k: v for k, v in first["revision"].items() if k != "handoff"
        }
        assert again["revision"]["handoff"] == "replayed"
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == nodes_before
        assert _attempt_rows(db, revision["id"]) == attempts_before
        assert len(_wakes(db, "parent")) == wakes_before
        assert _events(db, step.id, "review_rejected") == [{"feedback": FEEDBACK}]
    finally:
        db.close()


def test_replaying_a_rejection_the_upgrade_left_failed_returns_its_revision(monkeypatch):
    """Schema 57 keeps a sent-back submission with open work below it Failed."""
    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)
        seen = service._node_row(db, step.id)["revision"]
        first = _reject(db, plan.id, step.id)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (step.id,))
        db.commit()

        again = mutate(
            plan.id, "reject",
            {"node_id": step.id, "feedback": FEEDBACK, "expected_revision": seen},
            actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
        ).result

        assert again["revision"]["node_id"] == first["revision"]["node_id"]
        assert again["revision"]["handoff"] == "replayed"
    finally:
        db.close()


def test_rejecting_the_revised_submission_creates_its_next_revision(monkeypatch):
    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)
        _reject(db, plan.id, step.id)
        db.commit()
        revision = _revision_of(db, step.id)
        service.transition_node(revision["id"], "active", actor_id="parent", db=db)
        submit_review_evidence(revision["id"], db=db, actor_id="parent")
        db.commit()

        _reject(db, plan.id, revision["id"], feedback="Still 500 on null body.")
        db.commit()

        assert _state(db, revision["id"]) == "closed"
        second = _revision_of(db, revision["id"])
        assert second["title"] == "Draft the brief"
        assert _events(db, second["id"], "revision_created") == [
            {"source_node_id": revision["id"], "feedback": "Still 500 on null body."},
        ]
    finally:
        db.close()


def test_rejecting_the_delegated_step_hands_the_worker_its_revision(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (first, second) = _returned(
            db, "Write the tests", "Wire the endpoint",
        )
        assert _state(db, delegated.id) == "review_requested"

        result = _reject(db, parent_plan.id, delegated.id)
        db.commit()

        # The verdict lands on the step and on what the worker returned.
        assert _state(db, delegated.id) == "closed"
        for leaf in (first, second):
            assert _state(db, leaf.id) == "closed"
            assert _events(db, leaf.id, "review_rejected") == [{"feedback": FEEDBACK}]
            assert len(_events(db, leaf.id, "review_evidence_submitted")) == 1
        # One revision, attached to the same worker with the same kind and commitment.
        revision = _revision_of(db, delegated.id)
        assert result["revision"]["node_id"] == revision["id"]
        assert result["revision"]["handoff"] == "delivered"
        assert (revision["spawned_thread_id"], revision["spawn_kind"]) == ("worker", "claude-code")
        assert revision["commitment_id"] == delegated.commitment_id
        assert len(_events(db, revision["id"], "spawned_thread_attached")) == 1
        worker_step = db.execute(
            "SELECT * FROM plan_nodes WHERE plan_id=? AND title=?",
            (plan.id, "Revise: Delegate work"),
        ).fetchone()
        assert worker_step is not None
        assert worker_step["intent_node_id"] == delegated.id
        # The worker is woken once, with the revision and its feedback in hand.
        wakes = _wakes(db, "worker")
        assert len(wakes) == 1
        assert wakes[0]["id"].startswith("plan-wake:revision_requested:")
        facts = json.loads(wakes[0]["content"].split("\n", 1)[1])
        assert facts["revision"]["node_id"] == revision["id"]
        assert facts["revision"]["feedback"] == FEEDBACK
        assert facts["revision"]["acceptance_criteria"] == "Worker result is reviewed"
        # Nothing has returned for the revision yet: a sweep harvests nothing and wakes nobody.
        _sweep(db)
        assert _state(db, revision["id"]) == "pending"
        assert len(_wakes(db, "worker")) == 1
        assert _wakes(db, "parent") == [w for w in _wakes(db, "parent") if not w["id"].startswith("plan-wake:child_returned:" + revision["id"])]
    finally:
        db.close()


def test_a_parent_busy_with_other_work_still_hands_the_worker_its_revision(monkeypatch):
    """The worker revises in its own plan, whatever holds the parent's slot (#1731, #2052)."""
    from plans import admission, projections, wake_currency

    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, _leaves = _returned(db, "Write the tests")
        other = service.add_node(
            parent_plan.id, kind="task", title="Other work", acceptance_criteria="Done", db=db,
        )
        db.commit()
        _start(db, parent_plan.id, other.id)
        db.commit()

        result = _reject(db, parent_plan.id, delegated.id)
        db.commit()

        revision = _revision_of(db, delegated.id)
        assert result["revision"]["handoff"] == "delivered"
        # Its worker's step, pending like any delegated step (#2199).
        assert revision["state"] == "pending"
        (wake,) = _wakes(db, "worker")
        stored = db.execute(
            "SELECT continuation_condition FROM messages WHERE id=?", (wake["id"],),
        ).fetchone()[0]
        # Still worth the worker's Turn when claimed, though the parent's slot is taken.
        assert wake_currency.is_current("worker", json.loads(stored), db=db)
        # The worker's queued wake is it working: the parent is not told the
        # revision is its own ready work (#2052).
        plan_row = db.execute("SELECT * FROM plans WHERE id=?", (parent_plan.id,)).fetchone()
        actionable, _projected = projections.actionable_leaves(db, plan_row)
        assert revision["id"] not in {row["id"] for row in actionable}
        (cause,) = admission.project(revision["id"], db=db)["causes"]
        assert (cause["reason_key"], cause["child_active"]) == ("delegated_child", True)
    finally:
        db.close()


def test_what_waited_on_the_submission_waits_on_its_revision(monkeypatch):
    """Rejection moves dependents onto the revision; accepting it releases them (#1730)."""
    from plans import admission

    db = _db(monkeypatch)
    try:
        plan = service.ensure_plan("thread", "parent", db=db)
        after = service.add_node(
            plan.id, kind="task", title="Ship it", acceptance_criteria="Shipped", db=db,
        )
        _, step = _plain_step(db)
        service.add_dependency(after.id, step.id, db=db)
        db.commit()

        _reject(db, plan.id, step.id)
        db.commit()

        revision = _revision_of(db, step.id)
        edges = db.execute(
            "SELECT depends_on_node_id FROM plan_node_dependencies WHERE node_id=?", (after.id,),
        ).fetchall()
        assert [row[0] for row in edges] == [revision["id"]]
        assert [c["reason_key"] for c in admission.project(after.id, db=db)["causes"]] == [
            "dependency",
        ]
        assert _events(db, after.id, "dependency_removed") == [{"depends_on_node_id": step.id}]
        assert _events(db, after.id, "dependency_added")[-1] == {"depends_on_node_id": revision["id"]}

        _start(db, plan.id, revision["id"])
        submit_review_evidence(revision["id"], db=db, actor_id="parent")
        mutate(
            plan.id, "accept",
            {
                "node_id": revision["id"], "verification_result": "passed",
                "expected_revision": _node_row(db, revision["id"])["revision"],
            },
            actor=PlanActor(None, "user-1", None, kind="user"), provenance="test.accept",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()
        assert admission.project(after.id, db=db)["causes"] == []
    finally:
        db.close()


def test_a_return_whose_steps_all_failed_still_gets_its_revision(monkeypatch):
    """A worker that gave up is handed the revision like any other; nothing is stranded."""
    db = _db(monkeypatch)
    try:
        parent_plan = service.ensure_plan("thread", "parent", db=db)
        delegated = service.add_node(
            parent_plan.id, kind="task", title="Delegate work",
            acceptance_criteria="Worker result is reviewed",
            spawn_kind="claude-code", db=db,
        )
        service.transition_node(delegated.id, "active", db=db)
        plan = service.ensure_plan("thread", "worker", db=db)
        mutations.attach_spawned_thread(delegated.id, "worker", db=db)
        leaf = service.add_node(
            plan.id, kind="task", title="Write the tests", acceptance_criteria="Done", db=db,
        )
        service.transition_node(leaf.id, "active", actor_id="worker", db=db)
        mutations.fail_node(leaf.id, actor_id="worker", db=db)
        db.commit()
        _sweep(db)
        assert _state(db, delegated.id) == "review_requested"

        _reject(db, parent_plan.id, delegated.id)
        db.commit()

        revision = _revision_of(db, delegated.id)
        assert revision["spawned_thread_id"] == "worker"
        assert len(_wakes(db, "worker")) == 1
    finally:
        db.close()


def test_an_unreadable_revision_reason_is_said_not_dropped(monkeypatch):
    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)
        _reject(db, plan.id, step.id)
        db.commit()
        revision = _revision_of(db, step.id)
        _start(db, plan.id, revision["id"])
        # Events are append-only: a later creation record for the revision is corrupt.
        db.execute(
            """INSERT INTO plan_node_events
               (id,node_id,actor_id,provenance,event,prior_state,new_state,payload_json,created_at)
               VALUES ('corrupt-1',?,'parent','test','revision_created',NULL,NULL,'{not json','2999-01-01T00:00:00+00:00')""",
            (revision["id"],),
        )
        db.commit()
        from plans import close_next

        block = close_next.executor_next(db, plan.id, "parent")
        assert block["selected"]["id"] == revision["id"]
        assert "feedback" not in block["selected"]
        assert block["selected"]["feedback_unreadable"] is True
    finally:
        db.close()


def test_cut_feedback_says_where_the_whole_reason_is_read(monkeypatch):
    """The handoff and the worker's wake bound the feedback; the task's detail read carries it whole."""
    from plans import close_next, status
    from plans.close_next_bounds import FEEDBACK_MAX_BYTES
    from prompts.plan_tools import FEEDBACK_CUT

    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)
        long_feedback = ("Reason " * 200).strip()
        _reject(db, plan.id, step.id, feedback=long_feedback)
        db.commit()
        revision = _revision_of(db, step.id)
        _start(db, plan.id, revision["id"])

        handed = close_next.executor_next(db, plan.id, "parent")["selected"]["feedback"]
        assert handed.endswith(FEEDBACK_CUT) and len(handed.encode()) <= FEEDBACK_MAX_BYTES + 16
        plan_row = db.execute("SELECT * FROM plans WHERE id=?", (plan.id,)).fetchone()
        detail = status.node_detail(db, plan_row, revision["id"], actor=PlanActor("parent", "user", None))
        created = [e for e in detail["events"] if e["event"] == "revision_created"]
        assert created and created[0]["payload"]["feedback"] == long_feedback
    finally:
        db.close()


def test_a_holder_that_cannot_take_the_revision_leaves_it_pending_and_on_record(monkeypatch):
    """No wake is invented for a worker without a model; the missed handoff is durable."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, leaves = _returned(db, "Write the tests")
        db.execute("UPDATE threads SET model=NULL WHERE id='worker'")
        db.commit()

        result = _reject(db, parent_plan.id, delegated.id)
        db.commit()

        revision = _revision_of(db, delegated.id)
        assert result["revision"]["handoff"] == "undelivered"
        assert (revision["state"], revision["spawned_thread_id"]) == ("pending", "worker")
        assert _wakes(db, "worker") == []
        assert _events(db, revision["id"], "revision_handoff_undelivered") == [{"holder": "worker"}]
    finally:
        db.close()


def test_other_feedback_for_a_settled_submission_is_refused_not_replayed(monkeypatch):
    import pytest

    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)
        seen = service._node_row(db, step.id)["revision"]
        _reject(db, plan.id, step.id)
        db.commit()
        revision = _revision_of(db, step.id)

        with pytest.raises(service.PlanError, match="already rejected with different feedback"):
            mutate(
                plan.id, "reject",
                {"node_id": step.id, "feedback": "Different reason.", "expected_revision": seen},
                actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
            )

        assert _events(db, step.id, "review_rejected") == [{"feedback": FEEDBACK}]
        assert _revision_of(db, step.id)["id"] == revision["id"]
    finally:
        db.close()


def test_the_verdict_lands_only_on_the_steps_the_worker_returned(monkeypatch):
    """Work the worker put up for review after the harvest is not part of this verdict."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (returned,) = _returned(db, "Write the tests")
        later = service.add_node(
            plan.id, kind="task", title="Wire the endpoint", acceptance_criteria="Done", db=db,
        )
        service.transition_node(later.id, "active", actor_id="worker", db=db)
        submit_review_evidence(later.id, db=db, evidence="Wired", actor_id="worker")
        db.commit()

        _reject(db, parent_plan.id, delegated.id)
        db.commit()

        assert _state(db, returned.id) == "closed"
        assert _state(db, later.id) == "review_requested"
        assert _events(db, later.id, "review_rejected") == []
    finally:
        db.close()


def test_the_owner_takes_back_its_own_submission_without_rejecting_it(monkeypatch):
    """Starting work you put up for review reopens it: nothing is sent back or red."""
    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)

        from plans import close_next

        _start(db, plan.id, step.id)
        db.commit()

        assert _state(db, step.id) == "active"
        assert close_next.executor_next(db, plan.id, "parent")["selected"]["id"] == step.id
        assert _events(db, step.id, "review_rejected") == []
        assert db.execute(
            "SELECT COUNT(*) FROM plan_nodes WHERE follow_up_of=?", (step.id,),
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_starting_settled_or_delegated_work_names_the_route_that_applies(monkeypatch):
    import pytest

    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, _leaves = _returned(db, "Write the tests")
        with pytest.raises(service.PlanError, match="accept it, or send it back with reject"):
            _start(db, parent_plan.id, delegated.id)

        plan, step = _plain_step(db)
        mutate(
            plan.id, "accept",
            {"node_id": step.id, "verification_result": "passed",
             "expected_revision": service._node_row(db, step.id)["revision"]},
            actor=PARENT, provenance="test.accept", mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()
        with pytest.raises(service.PlanError, match="add_follow_up"):
            _start(db, plan.id, step.id)
    finally:
        db.close()


def _worker_revision(db) -> dict:
    return json.loads(_wakes(db, "worker")[-1]["content"].split("\n", 1)[1])["revision"]


def test_a_worker_is_handed_the_whole_feedback_and_criteria(monkeypatch):
    """#2835: the revision lives in the parent's plan, so the wake is the worker's whole brief."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, _leaves = _returned(db, "Write the tests")
        criteria = "\n".join(f"{n}. Criterion {n} " + "x" * 90 for n in range(1, 9))
        service.touch_node(db, delegated.id, acceptance_criteria=criteria)
        feedback = "\n".join(f"{n}. Finding {n}: " + "y" * 280 for n in range(1, 9))
        _reject(db, parent_plan.id, delegated.id, feedback=feedback)
        db.commit()

        handed = _worker_revision(db)
        assert handed["feedback"] == feedback
        assert handed["acceptance_criteria"] == criteria
    finally:
        db.close()


def test_feedback_past_the_ceiling_names_the_read_the_worker_can_make(monkeypatch):
    """A cut handed to a worker names the parent's plan, the only place the node is readable."""
    from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES

    db = _db(monkeypatch)
    try:
        parent_plan, delegated, worker_plan, _leaves = _returned(db, "Write the tests")
        _reject(db, parent_plan.id, delegated.id, feedback="z" * (REVISION_BRIEF_MAX_BYTES * 2))
        db.commit()

        revision = _revision_of(db, delegated.id)
        seeded = db.execute(
            "SELECT description FROM plan_nodes WHERE plan_id=? AND state='pending'",
            (worker_plan.id,),
        ).fetchone()
        assert seeded is not None
        assert len(seeded["description"].encode()) <= REVISION_BRIEF_MAX_BYTES
        handed = _worker_revision(db)["feedback"]
        assert handed.endswith(
            f"plan_read(thread_id='parent', node_id='{revision['id']}') returns its full feedback]"
        )
        assert len(handed.encode()) <= REVISION_BRIEF_MAX_BYTES
        from plans.current import read_current_status

        # The read the marker names works from the worker's own scope.
        detail = read_current_status(
            actor=WORKER, node_id=revision["id"], thread_id="parent", db=db,
        )
        created = [e for e in detail["events"] if e["event"] == "revision_created"]
        assert created[0]["payload"]["feedback"] == "z" * (REVISION_BRIEF_MAX_BYTES * 2)
    finally:
        db.close()


def test_send_backs_are_uncapped_and_advise_from_the_second_round(monkeypatch):
    """A send-back round is not an accept-with-follow-up chain depth."""
    db = _db(monkeypatch)
    try:
        parent_plan, current, worker_plan, _leaves = _returned(db, "Write the tests")
        for round_number in range(1, 6):
            result = _reject(db, parent_plan.id, current.id)
            db.commit()

            revision = _revision_of(db, current.id)
            assert result["revision"]["node_id"] == revision["id"]
            if round_number == 1:
                assert "guidance" not in result
            else:
                assert result["guidance"].startswith(
                    f"Round {round_number}; worker model: test/model."
                )
                assert "delegate_set_model" in result["guidance"]
                assert "replace_spawned_thread" in result["guidance"]

            worker_step = db.execute(
                "SELECT id FROM plan_nodes WHERE plan_id=? AND state='pending' ORDER BY rowid DESC",
                (worker_plan.id,),
            ).fetchone()
            assert worker_step is not None
            service.transition_node(worker_step["id"], "active", actor_id="worker", db=db)
            submit_review_evidence(worker_step["id"], db=db, actor_id="worker")
            db.commit()
            _sweep(db)
            current = service.get_node(revision["id"], db=db)
            assert current.state == "review_requested"
    finally:
        db.close()


def test_rejecting_delegated_work_seeds_one_worker_step_and_its_return(monkeypatch):
    """maestro#3440: a worker receives actionable revision work in its own plan."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, worker_plan, _leaves = _returned(db, "Write the tests")

        mutation_id = uuid.uuid4().hex
        seen = service._node_row(db, delegated.id)["revision"]
        result = _reject(
            db, parent_plan.id, delegated.id, mutation_id=mutation_id, expected_revision=seen,
        )
        db.commit()

        revision = _revision_of(db, delegated.id)
        seeded = db.execute(
            "SELECT * FROM plan_nodes WHERE plan_id=? AND state='pending' ORDER BY rowid",
            (worker_plan.id,),
        ).fetchall()
        assert len(seeded) == 1
        worker_step = seeded[0]
        assert (worker_step["title"], worker_step["acceptance_criteria"], worker_step["description"]) == (
            "Revise: Delegate work", revision["acceptance_criteria"], FEEDBACK,
        )
        assert _worker_revision(db)["worker_step_id"] == worker_step["id"]

        service.transition_node(worker_step["id"], "active", actor_id="worker", db=db)
        submit_review_evidence(worker_step["id"], db=db, actor_id="worker")
        db.commit()
        _sweep(db)

        assert _state(db, revision["id"]) == "review_requested"
        replay = _reject(
            db, parent_plan.id, delegated.id, mutation_id=mutation_id, expected_revision=seen,
        )
        assert replay == result
        assert db.execute(
            "SELECT COUNT(*) FROM plan_nodes WHERE plan_id=? AND title=?",
            (worker_plan.id, "Revise: Delegate work"),
        ).fetchone()[0] == 1
        assert result["revision"]["node_id"] == revision["id"]
    finally:
        db.close()


def _seeded(db, worker_plan_id: str) -> sqlite3.Row:
    (step,) = db.execute(
        "SELECT * FROM plan_nodes WHERE plan_id=? AND title LIKE 'Revise: %' ORDER BY rowid",
        (worker_plan_id,),
    ).fetchall()
    return step


def test_the_worker_is_told_to_work_its_own_step_with_the_revision_as_reference(monkeypatch):
    """The send-back wake names the seeded step, never the revision as its own step."""
    from prompts import plan_wakes

    db = _db(monkeypatch)
    try:
        parent_plan, delegated, worker_plan, _leaves = _returned(db, "Write the tests")
        _reject(db, parent_plan.id, delegated.id)
        db.commit()

        revision = _revision_of(db, delegated.id)
        step = _seeded(db, worker_plan.id)
        assert step["id"] != revision["id"]
        assert _wakes(db, "worker")[-1]["content"].split("\n", 1)[0] == (
            plan_wakes.REVISION_WORKER_STEP.format(
                title="Delegate work", worker_step_id=step["id"], revision_id=revision["id"],
            )
        )
    finally:
        db.close()


def test_a_send_back_with_no_seeded_step_keeps_the_plain_wording(monkeypatch):
    """A plain step's executor is told the revision itself is its work."""
    from plans import plan_wake
    from prompts import plan_wakes

    db = _db(monkeypatch)
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        step = service.add_node(
            plan.id, kind="task", title="Draft the brief", acceptance_criteria="Done", db=db,
        )
        db.commit()

        wake_id = plan_wake.notify_revision(db, step.id, recipient="worker", feedback=FEEDBACK)
        content = db.execute("SELECT content FROM messages WHERE id=?", (wake_id,)).fetchone()[0]
        assert content.split("\n", 1)[0] == plan_wakes.WAKE_REVISION_REQUESTED.format(
            title="Draft the brief",
        )
    finally:
        db.close()


def test_replacing_the_worker_closes_its_seeded_step(monkeypatch):
    """A fresh worker takes the revision; the old one's seeded step does not linger open."""
    db = _db(monkeypatch)
    try:
        db.execute(
            """INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES ('fresh','user-1','project-1','parent','test/model','t','t')"""
        )
        parent_plan, delegated, worker_plan, _leaves = _returned(db, "Write the tests")
        _reject(db, parent_plan.id, delegated.id)
        db.commit()
        revision = _revision_of(db, delegated.id)

        mutate(
            parent_plan.id, "replace_spawned_thread",
            {
                "node_id": revision["id"], "spawned_thread_id": "fresh",
                "expected_revision": revision["revision"],
            },
            actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()

        assert _seeded(db, worker_plan.id)["state"] == "closed"
    finally:
        db.close()


def test_the_users_mark_done_closes_the_seeded_step(monkeypatch):
    """The user's word on the revision stands the worker down, seeded step included."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, worker_plan, _leaves = _returned(db, "Write the tests")
        _reject(db, parent_plan.id, delegated.id)
        db.commit()
        revision = _revision_of(db, delegated.id)

        mutate(
            parent_plan.id, "mark_done",
            {"node_id": revision["id"], "expected_revision": revision["revision"]},
            actor=PlanActor(None, "user-1", None, kind="user"), provenance="test.rejection",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()

        assert _state(db, revision["id"]) == "accepted"
        assert _seeded(db, worker_plan.id)["state"] != "pending"
    finally:
        db.close()


def test_the_parent_taking_the_revision_over_settles_the_seeded_step(monkeypatch):
    """Done or not done, the parent's own settle leaves no seeded step open (G8).

    Once the worker is idle with the send-back delivered, the verdict stands it down.
    """
    for operation, extra, settled in (
        ("mark_done", {"evidence": "Fixed it myself"}, "accepted"),
        ("close_node", {"reason": "Not needed after all"}, "closed"),
    ):
        db = _db(monkeypatch)
        try:
            parent_plan, delegated, worker_plan, _leaves = _returned(db, "Write the tests")
            _reject(db, parent_plan.id, delegated.id)
            db.commit()
            revision = _revision_of(db, delegated.id)
            db.execute("UPDATE messages SET delivery_state='delivered' WHERE thread_id='worker'")

            mutate(
                parent_plan.id, operation,
                {"node_id": revision["id"], "expected_revision": revision["revision"], **extra},
                actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
            )
            db.commit()

            assert _state(db, revision["id"]) == settled
            assert _seeded(db, worker_plan.id)["state"] == "closed"
        finally:
            db.close()


def test_mark_done_waits_while_the_worker_still_has_the_send_back(monkeypatch):
    """A queued send-back is work in hand: the parent's Mark done does not cut it off."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, worker_plan, _leaves = _returned(db, "Write the tests")
        _reject(db, parent_plan.id, delegated.id)
        db.commit()
        revision = _revision_of(db, delegated.id)

        with pytest.raises(service.PlanError, match="worker is still working on it"):
            mutate(
                parent_plan.id, "mark_done",
                {
                    "node_id": revision["id"], "expected_revision": revision["revision"],
                    "evidence": "Fixed it myself",
                },
                actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
            )
        db.rollback()

        assert _seeded(db, worker_plan.id)["state"] == "pending"
    finally:
        db.close()
