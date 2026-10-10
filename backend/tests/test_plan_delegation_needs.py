"""RL1-RL5: a worker's missing input is durable parent-facing state."""

from __future__ import annotations

import json
import uuid

import pytest

from plans import admission, delegation, projections, public, service, status_context
from plans.boundary import PlanActor, mutate
from tests.child_parent_notice_helpers import delegated, db, wakes, worker_actor


PARENT_BLOCK_GUIDANCE = (
    "If a criterion needs something you can't open or reach, block_node with "
    "owner_kind 'parent' and say what; don't substitute your own version."
)


def _block_parent(connection, plan, node, mutation_id=None):
    return mutate(
        plan.id, "block_node", {
            "node_id": node.id,
            "expected_revision": service.get_node(node.id, db=connection).revision,
            "reason_key": "missing_input", "reason": "Need the approved mockup path",
            "owner_kind": "parent", "owner_id": None,
        }, actor=worker_actor(), provenance="test.delegation_needs",
        mutation_id=mutation_id or uuid.uuid4().hex, db=connection,
    )


def _armed_payload(connection, node_id):
    row = connection.execute(
        """SELECT e.payload_json FROM expected_event_watches w
           JOIN plan_node_events e ON e.node_id=w.plan_node_id AND e.event='watch_armed'
            AND json_extract(e.payload_json,'$.watch_id')=w.id
           WHERE w.plan_node_id=? AND w.status='armed'""", (node_id,),
    ).fetchone()
    return json.loads(row["payload_json"])


def test_rl1_parent_resolves_to_delegating_thread_and_refuses_elsewhere(db):
    _step, plan, node = delegated(db)
    _block_parent(db, plan, node)

    assert _armed_payload(db, node.id)["blocked"] == {
        "reason_key": "missing_input", "reason": "Need the approved mockup path",
        "owner_kind": "thread", "owner_id": "parent", "to_parent": True,
    }
    root = service.ensure_plan("thread", "grandparent", db=db)
    own = service.add_node(root.id, kind="task", title="Own", acceptance_criteria="Done", db=db)
    with pytest.raises(service.PlanInputError, match="owner_kind user or raise a decision"):
        mutate(root.id, "block_node", {
            "node_id": own.id, "expected_revision": own.revision, "reason_key": "input",
            "reason": "Need input", "owner_kind": "parent", "owner_id": None,
        }, actor=PlanActor("grandparent", "user-1", "project-1"),
        provenance="test.delegation_needs", mutation_id=uuid.uuid4().hex, db=db)


def test_n2_the_b4_wake_names_the_need_and_its_answer_route(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)

    [wake] = wakes(db)
    line = str(wake["content"]).splitlines()[0]
    assert "blocked on you: Need the approved mockup path" in line
    assert f"delegate_send(thread_id=worker, plan_node_id={step.id})" in line
    assert wake["id"].startswith("plan-wake:child_needs_input:")


def test_delegated_worker_status_names_the_parent_block_path(db):
    step, plan, _node = delegated(db)

    projection = status_context.projection(
        db, service._plan_row(db, plan.id), actor=worker_actor(), clock="2026-08-01T00:00:00+00:00",
    )
    root = service.ensure_plan("thread", "grandparent", db=db)
    parent = status_context.projection(
        db, service._plan_row(db, root.id),
        actor=PlanActor("grandparent", "user-1", "project-1"), clock="2026-08-01T00:00:00+00:00",
    )

    assert projection["delegated_plan_guidance"] == PARENT_BLOCK_GUIDANCE
    assert "delegated_plan_guidance" not in parent


def test_rl2_parent_step_reads_worker_need_as_actionable(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)

    projection = admission.project(step.id, db=db)
    assert projection["causes"][0]["reason_key"] == "worker_needs_input"
    assert projection["causes"][0]["owner_id"] == "parent"
    assert projections.liveness_signal("parent", db=db).node_ids == (step.id,)


def test_rl3_real_harvest_holds_an_armed_or_expired_need_until_answered(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)
    service.transition_node(node.id, "review_requested", actor_id="worker", db=db)

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []
    assert service.get_node(step.id, db=db).state == "active"
    db.execute(
        "UPDATE expected_event_watches SET status='expired' "
        "WHERE plan_node_id=? AND status='armed'", (node.id,),
    )
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []
    assert service.get_node(step.id, db=db).state == "active"

    assert public.answer_worker_needs("parent", "worker", step.id, mutation_id="answer-1", db=db) == 1
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    assert service.get_node(step.id, db=db).state == "review_requested"


def test_rl4_answer_is_idempotent_and_only_resolves_attached_worker_need(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)

    assert public.answer_worker_needs("parent", "worker", step.id, mutation_id="send-1", db=db) == 1
    assert public.answer_worker_needs("parent", "worker", step.id, mutation_id="send-1", db=db) == 1
    assert public.answer_worker_needs("parent", "worker", "not-a-step", mutation_id="send-2", db=db) == 0


def test_rl4_a_send_key_the_worker_already_used_still_answers(db):
    """The parent's send key lives in its own namespace inside the worker's plan."""
    step, plan, node = delegated(db)
    _block_parent(db, plan, node, mutation_id="shared-key")

    assert public.answer_worker_needs("parent", "worker", step.id, mutation_id="shared-key", db=db) == 1


def test_rl5_closing_parent_step_settles_worker_blocker_watch(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)
    from plans import closure

    assert db.execute(
        "SELECT COUNT(*) FROM expected_event_watches WHERE plan_node_id=? "
        "AND status IN ('armed','expired')", (node.id,),
    ).fetchone()[0] == 1
    closure.close_node(step.id, reason="Parent no longer needs this work", actor_id="parent", db=db)
    assert db.execute(
        "SELECT COUNT(*) FROM expected_event_watches WHERE plan_node_id=? "
        "AND status IN ('armed','expired')", (node.id,),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["close_node", "mark_done", "archive_node"])
def test_rl5_a_worker_settling_its_step_after_the_deadline_ends_the_need(db, operation):
    """An expired need on a step its worker settled no longer holds the parent."""
    from plans import delegation_needs

    step, plan, node = delegated(db)
    _block_parent(db, plan, node)
    db.execute(
        "UPDATE expected_event_watches SET status='expired' "
        "WHERE plan_node_id=? AND status='armed'", (node.id,),
    )
    params = {"node_id": node.id, "expected_revision": service.get_node(node.id, db=db).revision}
    if operation == "close_node":
        params["reason"] = "Worked around the missing input"
    if operation == "mark_done":
        params["evidence"] = "Finished without the missing input"
    if operation == "archive_node":
        closed = dict(params, reason="Not needed after all")
        mutate(
            plan.id, "close_node", closed, actor=worker_actor(),
            provenance="test.delegation_needs", mutation_id=uuid.uuid4().hex, db=db,
        )
        params["expected_revision"] = service.get_node(node.id, db=db).revision
    mutate(
        plan.id, operation, params, actor=worker_actor(),
        provenance="test.delegation_needs", mutation_id=uuid.uuid4().hex, db=db,
    )

    assert not delegation_needs.open_for(service._plan_row(db, plan.id), db=db)
    accept = {
        "node_id": step.id, "expected_revision": service.get_node(step.id, db=db).revision,
        "evidence": "The worker settled its step", "verification_result": "passed",
    }
    mutate(
        service.get_node(step.id, db=db).plan_id, "accept", accept,
        actor=PlanActor("parent", "user-1", "project-1"),
        provenance="test.delegation_needs", mutation_id=uuid.uuid4().hex, db=db,
    )
    assert service.get_node(step.id, db=db).state == "accepted"


@pytest.mark.parametrize("operation", ["mark_done", "accept"])
def test_rl3_the_parent_cannot_accept_around_an_open_need(db, operation):
    """An idle worker blocked on its parent is not stood down by the parent's verdict (RL10)."""
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)
    params = {"node_id": step.id, "expected_revision": service.get_node(step.id, db=db).revision}
    params["evidence"] = "Attempted while the worker's need is open"
    if operation == "accept":
        params["verification_result"] = "passed"

    with pytest.raises(service.PlanError, match="blocked on you: Need the approved mockup path"):
        mutate(
            service.get_node(step.id, db=db).plan_id, operation, params,
            actor=PlanActor("parent", "user-1", "project-1"),
            provenance="test.delegation_needs", mutation_id=uuid.uuid4().hex, db=db,
        )
    assert service.get_node(step.id, db=db).state == "active"
    assert service.get_node(node.id, db=db).state not in ("closed", "accepted")


def test_rl3_the_users_mark_done_still_settles_around_an_open_need(db):
    """The user's word on the row is the verdict: the need closes with the worker's step."""
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)

    mutate(
        service.get_node(step.id, db=db).plan_id, "mark_done",
        {"node_id": step.id, "expected_revision": service.get_node(step.id, db=db).revision},
        actor=PlanActor(None, "user-1", None, kind="user"),
        provenance="test.delegation_needs", mutation_id=uuid.uuid4().hex, db=db,
    )

    assert service.get_node(step.id, db=db).state == "accepted"
    assert service.get_node(node.id, db=db).state == "closed"


def _facts(db, *steps):
    """Read the delegated-child facts of these parent steps, keyed by step id."""
    from plans import wait_causes

    parent = service._plan_row(db, steps[0].plan_id)
    return wait_causes.delegation_facts(
        [(service._node_row(db, step.id), parent) for step in steps], db=db,
    )


def _second_need(db, plan, reason):
    """Block a second worker task on the parent, as the first one is."""
    node = service.add_node(
        plan.id, kind="task", title=f"Second {reason}", acceptance_criteria="Tests pass", db=db,
    )
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "expected_revision": service.get_node(node.id, db=db).revision,
            "reason_key": "missing_input", "reason": reason,
            "owner_kind": "parent", "owner_id": None,
        }, actor=worker_actor(), provenance="test.delegation_needs",
        mutation_id=uuid.uuid4().hex, db=db,
    )
    return node


def _arm_at(db, node_id, armed_at):
    """Set when a node's watch was armed, and return the watch's id."""
    db.execute(
        "UPDATE expected_event_watches SET armed_at=? WHERE plan_node_id=?", (armed_at, node_id),
    )
    return db.execute(
        "SELECT id FROM expected_event_watches WHERE plan_node_id=?", (node_id,),
    ).fetchone()["id"]


def test_delegation_facts_report_no_need_for_a_worker_that_asks_nothing(db):
    step, _plan, _node = delegated(db)

    row = _facts(db, step)[step.id]

    assert (row["parent_need"], row["parent_need_reason"]) == (0, None)


def test_delegation_facts_report_an_open_need_and_its_reason(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)

    row = _facts(db, step)[step.id]

    assert (row["parent_need"], row["parent_need_reason"]) == (1, "Need the approved mockup path")


def test_delegation_facts_name_the_oldest_of_two_open_needs(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)
    second = _second_need(db, plan, "Need the signed contract")
    _arm_at(db, node.id, "2026-08-01T00:00:02+00:00")
    _arm_at(db, second.id, "2026-08-01T00:00:01+00:00")

    row = _facts(db, step)[step.id]

    assert (row["parent_need"], row["parent_need_reason"]) == (1, "Need the signed contract")


def test_delegation_facts_break_an_arming_tie_by_the_lowest_watch_id(db):
    """Eight needs armed at one instant: the lowest watch id's reason wins, whatever the order."""
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)
    nodes = {node.id: "Need the approved mockup path"} | {
        _second_need(db, plan, f"Need number {n}").id: f"Need number {n}" for n in range(7)
    }
    reasons = {
        _arm_at(db, node_id, "2026-08-01T00:00:01+00:00"): reason
        for node_id, reason in nodes.items()
    }

    row = _facts(db, step)[step.id]

    assert row["parent_need_reason"] == reasons[min(reasons)]


def test_delegation_facts_hold_on_an_expired_need_but_not_a_resolved_one(db):
    step, plan, node = delegated(db)
    _block_parent(db, plan, node)

    db.execute("UPDATE expected_event_watches SET status='expired' WHERE plan_node_id=?", (node.id,))
    assert _facts(db, step)[step.id]["parent_need"] == 1

    db.execute("UPDATE expected_event_watches SET status='resolved' WHERE plan_node_id=?", (node.id,))
    row = _facts(db, step)[step.id]
    assert (row["parent_need"], row["parent_need_reason"]) == (0, None)


def test_delegation_facts_do_not_read_a_need_of_a_plan_outside_the_batch(db):
    step, plan, node = delegated(db)
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at) "
        "VALUES ('worker2','user-1','project-1','parent','native/model','t','t')"
    )
    other_step = service.add_node(
        service._plan_row(db, step.plan_id)["id"], kind="task", title="Delegate more",
        acceptance_criteria="Worker result is reviewed", spawn_kind="claude-code", db=db,
    )
    service.transition_node(other_step.id, "active", db=db)
    other_plan = service.ensure_plan("thread", "worker2", db=db)
    other_node = service.add_node(
        other_plan.id, kind="task", title="Other", acceptance_criteria="Done", db=db,
    )
    from plans import mutations

    mutations.attach_spawned_thread(other_step.id, "worker2", db=db)
    mutate(
        other_plan.id, "block_node", {
            "node_id": other_node.id,
            "expected_revision": service.get_node(other_node.id, db=db).revision,
            "reason_key": "missing_input", "reason": "Other worker is stuck",
            "owner_kind": "parent", "owner_id": None,
        }, actor=PlanActor("worker2", "user-1", "project-1"),
        provenance="test.delegation_needs", mutation_id=uuid.uuid4().hex, db=db,
    )

    only_first = _facts(db, step)
    both = _facts(db, step, other_step)

    assert set(only_first) == {step.id}
    assert (only_first[step.id]["parent_need"], only_first[step.id]["parent_need_reason"]) == (0, None)
    assert (both[step.id]["parent_need"], both[step.id]["parent_need_reason"]) == (0, None)
    assert (both[other_step.id]["parent_need"], both[other_step.id]["parent_need_reason"]) == (
        1, "Other worker is stuck",
    )
