"""The parent's verdict on a delegated step settles the steps the worker returned.

A worker returns by putting its last step up for review; the harvest hands the
parent one wake with the result. The parent's acceptance of its delegated step
is its verdict on that result -- so it accepts the worker's returned steps in
the same write, with the same verification, and the worker is asked for
nothing: no second verdict on each step first, no model call to acknowledge.
Work the worker has not returned still holds the parent's acceptance back. The
parent's own evidence on the verdict is enough when the harvest put the step up
for review with none (#1977).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from automation import plan_liveness_terminal
from automation.continuation import runner
from automation.continuation.sources.harvest import HarvestSource
from automation.turn_end_effects import PendingTurnEndEffects
from infra.schema import SCHEMA_SQL
from plans import archival, mutations, service
from plans.boundary import PlanActor, mutate
from plans.projections import open_delegated_node_ids, open_delegated_work
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed

NOW = "2026-08-01T00:00:00+00:00"
PARENT = PlanActor("parent", "user-1", "project-1")


def _db(monkeypatch) -> sqlite3.Connection:
    monkeypatch.setenv("CORSIA_STRUCTURED_PLAN_P4_ENABLED", "1")
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


def _returned(db, *titles: str, still_running: bool = False):
    """A harvested delegation: the worker returned ``titles`` and the parent was woken."""
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
    if still_running:
        running = service.add_node(
            plan.id, kind="task", title="Still running", acceptance_criteria="Done", db=db,
        )
        service.transition_node(running.id, "active", actor_id="worker", db=db)
    db.commit()
    asyncio.run(runner.sweep(
        NOW, db=db, sources=[HarvestSource()],
        is_idle=lambda _thread: True, deliver=AsyncMock(return_value=True),
    ))
    return parent_plan, delegated, plan, leaves


def _verdict(db, plan_id: str, operation: str, node_id: str, **extra) -> dict:
    return mutate(
        plan_id, operation,
        {
            "node_id": node_id, "verification_result": "passed",
            "expected_revision": service._node_row(db, node_id)["revision"],
            **extra,
        },
        actor=PARENT, provenance="test.verdict", mutation_id=uuid.uuid4().hex, db=db,
    ).result


def _worker_wakes(db) -> list[str]:
    return [
        row["id"] for row in db.execute(
            "SELECT id,metadata FROM messages WHERE thread_id='worker'"
        ).fetchall()
        if json.loads(row["metadata"] or "{}").get("event_disposition") != "passive"
    ]


def test_accepting_the_delegated_step_accepts_the_returned_steps(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (first, second) = _returned(
            db, "Write the tests", "Wire the endpoint",
        )
        assert service.get_node(delegated.id, db=db).state == "review_requested"
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        _verdict(db, parent_plan.id, "accept", delegated.id)
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        for leaf in (first, second):
            row = db.execute(
                """SELECT n.state, e.verification_result, e.payload_json
                   FROM plan_nodes n JOIN plan_node_events e ON e.node_id=n.id
                   WHERE n.id=? AND e.event='state_transition' AND e.new_state='accepted'""",
                (leaf.id,),
            ).fetchone()
            assert row["state"] == "accepted"
            assert row["verification_result"] == "passed"
            assert json.loads(row["payload_json"])["verified_by"] == PARENT.audit_id
        assert not open_delegated_work(db, plan.id)
        assert _worker_wakes(db) == []
    finally:
        db.close()


def _second_round(db, plan):
    """The worker puts one more step up for review in a new, completed Turn."""
    later = service.add_node(
        plan.id, kind="task", title="Address review", acceptance_criteria="Done", db=db,
    )
    service.transition_node(later.id, "active", actor_id="worker", db=db)
    submit_review_evidence(later.id, db=db, evidence="Review addressed", actor_id="worker")
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-second-return','worker',0,'native/test','completed',?,'boot')""",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),),
    )
    return later


def _boundaries(db, node_id) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='child_plan_completed'",
        (node_id,),
    ).fetchone()[0]


def test_a_round_the_parent_cannot_be_told_about_records_no_boundary(monkeypatch):
    """The boundary moves only with the wake that reports it, as in the harvest."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, _ = _returned(db, "Write the tests")
        before = service.get_node(delegated.id, db=db).revision
        _second_round(db, plan)
        monkeypatch.setattr("plans.plan_wake.append", lambda *a, **k: None)

        effects = PendingTurnEndEffects()
        plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)

        assert service.get_node(delegated.id, db=db).revision == before
        assert _boundaries(db, delegated.id) == 1
        assert effects.changed_plan_ids == []
    finally:
        db.close()


def test_a_step_the_owner_already_returned_keeps_its_revision(monkeypatch):
    """A later worker round must not move a revision under the grandparent's review."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, _ = _returned(db, "Write the tests")
        submit_review_evidence(delegated.id, db=db, evidence="Owner verified", actor_id="parent")
        before = service.get_node(delegated.id, db=db).revision
        _second_round(db, plan)

        effects = PendingTurnEndEffects()
        plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)

        assert service.get_node(delegated.id, db=db).revision == before
        assert _boundaries(db, delegated.id) == 1
    finally:
        db.close()


def test_accepting_after_a_send_back_round_settles_that_round(monkeypatch):
    """#2923: each later worker Turn is a new verdict boundary, not only a wake."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (_first,) = _returned(db, "Write the tests")
        seen_revision = service.get_node(delegated.id, db=db).revision

        later = _second_round(db, plan)

        effects = PendingTurnEndEffects()
        plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)
        plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)
        current = service.get_node(delegated.id, db=db)

        assert current.revision == seen_revision + 1
        assert db.execute(
            """SELECT COUNT(*) FROM plan_node_events
               WHERE node_id=? AND event='child_plan_completed'""",
            (delegated.id,),
        ).fetchone()[0] == 2
        assert effects.changed_plan_ids == [parent_plan.id]

        with pytest.raises(service.PlanError, match="conflict"):
            mutate(
                parent_plan.id, "accept",
                {
                    "node_id": delegated.id,
                    "verification_result": "passed",
                    "evidence": "Verified the second return",
                    "expected_revision": seen_revision,
                },
                actor=PARENT, provenance="test.verdict", mutation_id=uuid.uuid4().hex,
                db=db,
            )
        assert service.get_node(later.id, db=db).state == "review_requested"

        _verdict(
            db, parent_plan.id, "accept", delegated.id,
            evidence="Verified the second return",
        )
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        assert service.get_node(later.id, db=db).state == "accepted"
        assert _worker_wakes(db) == []
    finally:
        db.close()


def test_accepting_with_a_follow_up_settles_the_return_too(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (leaf,) = _returned(db, "Write the tests")
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        result = _verdict(
            db, parent_plan.id, "accept_with_follow_up", delegated.id,
            title="Polish", acceptance_criteria="Polished",
        )
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted_with_followup"
        assert service.get_node(leaf.id, db=db).state == "accepted"
        assert result["follow_up"]["title"] == "Polish"
        assert not open_delegated_work(db, plan.id)
        assert _worker_wakes(db) == []
    finally:
        db.close()


def test_a_parent_takes_over_the_rest_without_failing_anything(monkeypatch):
    """#2232 handover: 'worker done, I own the tail' is accept_with_follow_up, not reject.

    The worker's return settles, nothing reads failed, the rest is the
    parent's own task with no worker on it, and what depended on the
    delegated step is free to move.
    """
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (leaf,) = _returned(db, "Open the PR")
        dependent = service.add_node(
            parent_plan.id, kind="task", title="Ship it", acceptance_criteria="Shipped", db=db,
        )
        service.add_dependency(dependent.id, delegated.id, db=db)
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        result = _verdict(
            db, parent_plan.id, "accept_with_follow_up", delegated.id,
            title="Land the PR", acceptance_criteria="Merged",
        )
        db.commit()

        follow_up = service.get_node(result["follow_up"]["id"], db=db)
        assert follow_up.spawned_thread_id is None and follow_up.state == "pending"
        states = {
            row["state"] for row in db.execute(
                "SELECT state FROM plan_nodes WHERE plan_id IN (?,?)", (parent_plan.id, plan.id),
            ).fetchall()
        }
        assert "failed" not in states
        assert service.get_node(leaf.id, db=db).state == "accepted"
        assert not service.get_node(dependent.id, db=db).unmet_dependencies
        assert _worker_wakes(db) == []
    finally:
        db.close()


def _worker_turn_running(db) -> None:
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-worker','worker',0,'native/test','running','t0','boot')"""
    )


def test_a_worker_still_working_holds_the_verdict_back(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (leaf,) = _returned(
            db, "Write the tests", still_running=True,
        )
        # Not harvested: the worker still runs. Put the step up for review by
        # hand to reach the verdict's own guard.
        assert service.get_node(delegated.id, db=db).state == "active"
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        _worker_turn_running(db)

        with pytest.raises(service.PlanError, match="delegated child work"):
            _verdict(db, parent_plan.id, "accept", delegated.id, evidence="Too soon")

        # The refused verdict wrote nothing, its note included.
        assert _evidence(db, delegated.id) == "Focused test proof passed"

        assert service.get_node(leaf.id, db=db).state == "review_requested"
        assert open_delegated_work(db, plan.id)
    finally:
        db.close()


def _leftover(db, plan_id: str, title: str = "Still running") -> sqlite3.Row:
    return db.execute(
        """SELECT n.id, n.state, e.payload_json FROM plan_nodes n
           JOIN plan_node_events e ON e.node_id=n.id AND e.new_state='closed'
           WHERE n.plan_id=? AND n.title=?""",
        (plan_id, title),
    ).fetchone()


def test_accepting_stands_down_a_worker_that_is_not_working(monkeypatch):
    """#1866: a worker between Turns cannot hold its parent's verdict hostage."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (leaf,) = _returned(
            db, "Write the tests", still_running=True,
        )
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        _verdict(
            db, parent_plan.id, "accept", delegated.id,
            evidence="Verified the returned commit myself",
        )
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        # No harvest returned anything, so nothing the worker holds was part
        # of this verdict: its unfinished and unreturned steps both close.
        for title in ("Still running", "Write the tests"):
            leftover = _leftover(db, plan.id, title)
            assert leftover["state"] == "closed"
            assert "accepted the step that delegated" in leftover["payload_json"]
        assert leaf.id == _leftover(db, plan.id, "Write the tests")["id"]
        assert not open_delegated_work(db, plan.id)
        assert _worker_wakes(db) == []
    finally:
        db.close()


def test_accepting_stands_down_an_unreachable_worker(monkeypatch):
    """#1866: an archived worker can never settle its steps, so the verdict does."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(
            db, "Write the tests", still_running=True,
        )
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        db.execute(
            "UPDATE threads SET lifecycle_state='archived', archived_reason='user' "
            "WHERE id='worker'"
        )

        _verdict(db, parent_plan.id, "accept", delegated.id, evidence="Verified")
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        assert _leftover(db, plan.id)["state"] == "closed"
        assert not open_delegated_work(db, plan.id)
    finally:
        db.close()


def _evidence(db, node_id: str) -> str | None:
    from plans import review_evidence

    node = service._node_row(db, node_id)
    found = review_evidence.latest(
        node_id, requirements_revision=node["requirements_revision"], db=db,
    )
    return None if found is None else found["evidence"]


def test_the_parents_evidence_accepts_a_step_the_harvest_left_bare(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, _leaves = _returned(db, "Write the tests")
        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert _evidence(db, delegated.id) is None
        with pytest.raises(service.PlanError, match="review evidence"):
            _verdict(db, parent_plan.id, "accept", delegated.id)

        _verdict(
            db, parent_plan.id, "accept", delegated.id,
            evidence="Ran the tests on the returned commit",
        )
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        assert _evidence(db, delegated.id) == "Ran the tests on the returned commit"
    finally:
        db.close()


def test_the_parents_evidence_accepts_a_bare_step_after_an_edit(monkeypatch):
    """#2329: an edit re-revises the requirements, and accept still needs no detour."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(db, "Write the tests")
        mutate(
            parent_plan.id, "edit",
            {
                "node_id": delegated.id, "acceptance_criteria": "Merged to dev",
                "expected_revision": service._node_row(db, delegated.id)["revision"],
            },
            actor=PARENT, provenance="test.edit", mutation_id=uuid.uuid4().hex, db=db,
        )

        _verdict(db, parent_plan.id, "accept", delegated.id, evidence="Merged as a60555a1")
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        failed = db.execute(
            "SELECT 1 FROM plan_nodes WHERE plan_id IN (?,?) AND state='failed'",
            (parent_plan.id, plan.id),
        ).fetchone()
        assert failed is None
        assert _worker_wakes(db) == []
    finally:
        db.close()


def _grandchild(db, plan, *, running: bool):
    """The worker's unfinished step, itself delegated to a grandchild."""
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES ('grandchild','user-1','project-1','worker','test/model','t','t')"""
    )
    step = db.execute(
        "SELECT id FROM plan_nodes WHERE plan_id=? AND title='Still running'", (plan.id,),
    ).fetchone()["id"]
    below = service.ensure_plan("thread", "grandchild", db=db)
    mutations.attach_spawned_thread(step, "grandchild", db=db)
    leaf = service.add_node(
        below.id, kind="task", title="Grandchild work", acceptance_criteria="Done", db=db,
    )
    service.transition_node(leaf.id, "active", actor_id="grandchild", db=db)
    if running:
        db.execute(
            """INSERT INTO active_turns
               (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
               VALUES ('turn-grandchild','grandchild',0,'native/test','running','t0','boot')"""
        )
    return below, leaf


def test_a_running_grandchild_holds_the_verdict_back(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(
            db, "Write the tests", still_running=True,
        )
        _below, leaf = _grandchild(db, plan, running=True)
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        with pytest.raises(service.PlanError, match="still working"):
            _verdict(db, parent_plan.id, "accept", delegated.id, evidence="Too soon")

        assert service.get_node(leaf.id, db=db).state == "active"
    finally:
        db.close()


def test_standing_down_an_idle_worker_retires_its_idle_grandchild_too(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(
            db, "Write the tests", still_running=True,
        )
        below, _leaf = _grandchild(db, plan, running=False)
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        _verdict(db, parent_plan.id, "accept", delegated.id, evidence="Verified")
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        assert not open_delegated_work(db, plan.id)
        assert not open_delegated_work(db, below.id)
    finally:
        db.close()


def test_a_verdict_note_follows_evidence_already_submitted(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, _leaves = _returned(db, "Write the tests")
        submit_review_evidence(delegated.id, db=db, evidence="Proof", actor_id="parent")

        _verdict(db, parent_plan.id, "accept", delegated.id, evidence="Looks right")
        db.commit()

        assert _evidence(db, delegated.id) == "Proof\n\nAccepted: Looks right"
    finally:
        db.close()


def test_a_failed_node_carries_its_evidence(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan = service.ensure_plan("thread", "parent", db=db)
        node = service.add_node(
            parent_plan.id, kind="task", title="Try it", acceptance_criteria="Works",
            db=db,
        )
        service.transition_node(node.id, "active", db=db)
        mutate(
            parent_plan.id, "fail",
            {
                "node_id": node.id, "evidence": "The API refuses the call",
                "expected_revision": service._node_row(db, node.id)["revision"],
            },
            actor=PARENT, provenance="test.verdict", mutation_id=uuid.uuid4().hex,
            db=db,
        )
        db.commit()

        assert service.get_node(node.id, db=db).state == "failed"
        assert _evidence(db, node.id) == "The API refuses the call"
    finally:
        db.close()


def _started_branch(db, plan_id: str) -> str:
    """A step the worker started whose own step is done: no open child, still open."""
    branch = service.add_node(
        plan_id, kind="phase", title="Branch", acceptance_criteria="Done", db=db,
    )
    service.transition_node(branch.id, "active", actor_id="worker", db=db)
    step = service.add_node(
        plan_id, kind="task", title="Branch step", acceptance_criteria="Done",
        parent_id=branch.id, db=db,
    )
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (step.id,))
    db.commit()
    return branch.id


def test_accepting_stands_down_a_started_step_whose_steps_are_done(monkeypatch):
    """Nothing the worker started stays in progress under an accepted step."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(db, "Write the tests")
        branch = _started_branch(db, plan.id)

        _verdict(db, parent_plan.id, "accept", delegated.id, evidence="Verified")
        db.commit()

        assert service.get_node(delegated.id, db=db).state == "accepted"
        leftover = _leftover(db, plan.id, "Branch")
        assert leftover["id"] == branch and leftover["state"] == "closed"
        assert "accepted the step that delegated" in leftover["payload_json"]
    finally:
        db.close()


def test_closing_the_delegated_step_closes_a_started_step_whose_steps_are_done(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, _leaves = _returned(db, "Write the tests")
        branch = _started_branch(db, plan.id)

        mutate(
            parent_plan.id, "close_node",
            {"node_id": delegated.id, "reason": "Not needed",
             "expected_revision": service._node_row(db, delegated.id)["revision"]},
            actor=PARENT, provenance="test.verdict", mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()

        assert service.get_node(branch, db=db).state == "closed"
        assert _worker_wakes(db) == []
    finally:
        db.close()


def test_a_started_step_whose_steps_are_done_is_open_work(monkeypatch):
    """One answer to "does the worker have work open" (Plans debt 112): a step
    the worker started stays open work after its own steps settle."""
    db = _db(monkeypatch)
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        branch = _started_branch(db, plan.id)

        assert open_delegated_node_ids(db, plan.id) == (branch,)
        assert open_delegated_work(db, plan.id)
    finally:
        db.close()


def test_a_never_started_parent_reads_as_its_steps(monkeypatch):
    db = _db(monkeypatch)
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        branch = service.add_node(
            plan.id, kind="phase", title="Branch", acceptance_criteria="Done", db=db,
        )
        step = service.add_node(
            plan.id, kind="task", title="Branch step", acceptance_criteria="Done",
            parent_id=branch.id, db=db,
        )
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (step.id,))
        db.commit()

        assert open_delegated_node_ids(db, plan.id) == ()
    finally:
        db.close()


def test_archive_refuses_while_a_started_worker_step_is_open(monkeypatch):
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
        branch = _started_branch(db, plan.id)

        with pytest.raises(service.PlanError, match="spawned thread's own plan work") as refused:
            archival.archive_node(delegated.id, db=db)
        assert branch in str(refused.value)
    finally:
        db.close()
