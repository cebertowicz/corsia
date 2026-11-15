"""One call moves or closes the whole list, and one refusal costs only its own row.

A cleanup pass over a drifted plan used to cost one call per task, each needing
that task's revision read first: the per-node round trip the Later release exists
to cut (design §5). Bulk `edit` and bulk `close_node` take plain ids and copy the
shape `archive_node` already ships -- one write, the caller's order, and refusals
reported per id instead of ending the call.

Copying that shape is where the two hard parts are, and both are what these tests
aim at. The guards must not be relaxed just because a list came in: a bulk close
still refuses a workflow run's required step, because quietly skipping it would
end the run (``plans.run_end``). And a refusal must take back one row and nothing
else, so a reported refusal keeps the revision it had and can be retried alone --
each node's write is a nested savepoint inside the call's single transaction.
"""

from __future__ import annotations

import json

import pytest

import plans.writer_context as writer_context
from plans import service
from plans.aggregates_recount import stored_mismatches
from plans.boundary import PlanActor
from plans.bulk_fields import MAX_BULK_NODES
from plans.later_fields import LATER_ON_BRANCH
from tests.plan_close_next_helpers import OWNER, owner_plan, plan_update, revision, row
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixtures

#: The ids past the cap, in the shape the boundary refuses before any write.
_PAST_CAP = [f"past-cap-{index}" for index in range(MAX_BULK_NODES + 1)]
TASK_DOD = "Verified by the owner"


def _phase(db, plan_id: str, title: str = "Branch", *, parent_id: str | None = None):
    return service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} is verified",
        parent_id=parent_id, db=db,
    )


def _node(db, plan_id: str, title: str, *, parent_id: str | None = None, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=TASK_DOD,
        parent_id=parent_id, db=db, **kwargs,
    )


def _bulk(db, plan_id: str, operation: str, params: dict, *, actor=OWNER):
    """One bulk call through the real mutation boundary, ids exactly as given."""
    return plan_update(db, plan_id, operation, params, actor=actor)


def _edits(db, plan_id, node_ids, *, actor=OWNER, **change) -> dict:
    return _bulk(db, plan_id, "edit", {"node_ids": node_ids, **change}, actor=actor)


def _closes(db, plan_id, node_ids, reason="Superseded by the new outcome", *, actor=OWNER):
    return _bulk(db, plan_id, "close_node", {"node_ids": node_ids, "reason": reason},
                 actor=actor)


def _later_reason(db, node_id: str):
    return [
        json.loads(payload)["reason"] for (payload,) in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=?",
            (node_id, "node_later"),
        ).fetchall()
    ][-1]


def _reparents(db, node_id: str) -> list[dict]:
    return [
        json.loads(payload) for (payload,) in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=?"
            " ORDER BY created_at",
            (node_id, "node_reparented"),
        ).fetchall()
    ]


# --- bulk edit ---------------------------------------------------------------


def test_one_bulk_edit_moves_every_named_task_below_the_cut(db):
    """The design's own example, in one call: three tasks, one Later decision."""
    plan = owner_plan(db)
    tasks = [_node(db, plan.id, f"Stale {index}") for index in range(3)]
    kept = _node(db, plan.id, "Kept")

    result = _edits(db, plan.id, [task.id for task in tasks], later=True, reason="Rewrite")

    assert [entry["node_id"] for entry in result["changed"]] == [task.id for task in tasks]
    assert result["refused"] == []
    assert all(row(db, task.id)["later"] == 1 for task in tasks)
    assert row(db, kept.id)["later"] == 0
    assert all(_later_reason(db, task.id) == "Rewrite" for task in tasks)


def test_the_bulk_body_needs_no_revision_for_any_node(db):
    """No revision means no read-the-plan-first round trip, which is the point."""
    plan = owner_plan(db)
    task = _node(db, plan.id, "Stale")

    result = _edits(db, plan.id, [task.id], later=True)

    assert result["changed"] == [{"node_id": task.id, "revision": task.revision + 1}]


def test_one_bulk_edit_regroups_every_named_id(db):
    """The other half of the design's example: top-level rows into a new branch."""
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Launch")
    tasks = [_node(db, plan.id, f"Row {index}") for index in range(3)]

    result = _edits(db, plan.id, [task.id for task in tasks], parent_id=branch.id)

    assert [entry["node_id"] for entry in result["changed"]] == [task.id for task in tasks]
    assert all(row(db, task.id)["parent_id"] == branch.id for task in tasks)
    assert _reparents(db, tasks[1].id) == [{"from": None, "to": branch.id}]


def test_a_bulk_edit_applies_the_cut_and_the_branch_in_one_call(db):
    """One call, two changes: a mixed cleanup does not become two calls."""
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Later shape")
    tasks = [_node(db, plan.id, f"Row {index}") for index in range(2)]

    result = _edits(db, plan.id, [task.id for task in tasks],
                    later=True, parent_id=branch.id, reason="Revisit at relaunch")

    assert result["refused"] == []
    for task in tasks:
        assert row(db, task.id)["parent_id"] == branch.id
        assert row(db, task.id)["later"] == 1
    assert stored_mismatches(db, plan.id) == []


def test_a_bulk_edit_rolls_up_the_branch_it_moved_in_once(db):
    """200 moves is 200 writes, not 200 rebuilds: the stored sums stay exact."""
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Launch")
    other = _phase(db, plan.id, "Support")
    tasks = [_node(db, plan.id, f"Row {index}", parent_id=other.id) for index in range(4)]

    _edits(db, plan.id, [task.id for task in tasks], parent_id=branch.id)

    assert row(db, other.id)["sub_open"] == 0
    assert row(db, branch.id)["sub_open"] == 4
    assert stored_mismatches(db, plan.id) == []


def test_one_refusal_costs_only_its_own_row(db):
    """A cycle in the list is one row's fact: the rest of the regroup still lands.

    The refusal is not allowed to take the call's other writes with it, and the
    refused row keeps the revision it had so the caller can retry it alone.
    """
    plan = owner_plan(db)
    first = _phase(db, plan.id, "First")
    second = _phase(db, plan.id, "Second", parent_id=first.id)
    stray = _node(db, plan.id, "Stray")

    result = _edits(db, plan.id, [stray.id, first.id, second.id], parent_id=second.id)

    assert [entry["node_id"] for entry in result["changed"]] == [stray.id]
    # Both container moves are cycles: the first into its own child, the second
    # into itself. Neither reaches the write, and neither took the stray move
    # with it.
    assert [entry["node_id"] for entry in result["refused"]] == [first.id, second.id]
    assert "own branch" in result["refused"][0]["reason"]
    assert row(db, first.id)["revision"] == first.revision
    assert row(db, stray.id)["parent_id"] == second.id
    # The refused move wrote nothing at all: no event, and the sums stayed exact.
    assert _reparents(db, first.id) == []
    assert stored_mismatches(db, plan.id) == []


def test_a_branch_in_a_bulk_later_list_is_refused_and_its_siblings_move(db):
    """The per-node guard is not relaxed for a list, and the rest still cross."""
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Branch")
    task = _node(db, plan.id, "Task")

    result = _edits(db, plan.id, [branch.id, task.id], later=True)

    assert [entry["node_id"] for entry in result["changed"]] == [task.id]
    assert result["refused"][0]["node_id"] == branch.id
    assert LATER_ON_BRANCH.split(".")[0] in result["refused"][0]["reason"]
    assert row(db, branch.id)["later"] == 0


def test_a_lane_owned_id_is_refused_while_the_rest_apply(db):
    """Lane authorization is the boundary's, so it is judged per id here."""
    from tests.test_attention_queue import raise_decision

    # The decision helper's scenes run in a root thread of its own scope, which
    # this module's threads do not include, so the scene adds that project, thread
    # and scope.
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project One','t','t')"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at) "
        "VALUES ('root','user-1','project-1',NULL,'test/model','t','t')"
    )
    db.commit()
    decision = raise_decision(db, "root", "root")
    actor = PlanActor("root", "user-1", "project-1")
    plan_id = decision["plan_id"]
    free = _node(db, plan_id, "Free")

    result = _edits(db, plan_id, [decision["node_id"], free.id], later=True, actor=actor)

    assert [entry["node_id"] for entry in result["changed"]] == [free.id]
    assert result["refused"][0]["node_id"] == decision["node_id"]
    assert "decision operations" in result["refused"][0]["reason"]
    assert row(db, decision["node_id"])["later"] == 0


# --- the bulk body's own refusals -------------------------------------------


def test_a_bulk_edit_refuses_the_fields_that_describe_one_node(db):
    plan = owner_plan(db)
    task = _node(db, plan.id, "Stale")

    for extra in ({"node_id": task.id}, {"expected_revision": task.revision},
                  {"title": "Renamed all of them"}):
        with pytest.raises(service.PlanInputError, match="no single node"):
            _edits(db, plan.id, [task.id], later=True, **extra)
    assert row(db, task.id)["later"] == 0


def test_a_bulk_body_is_chosen_before_any_node_is_read(db):
    """The body is decided first, so a mixed call is never read as a single edit.

    An unknown id would answer "unknown plan node" had the single-node path run
    first; the caller is told about the body instead.
    """
    plan = owner_plan(db)

    with pytest.raises(service.PlanInputError, match="no single node"):
        _bulk(db, plan.id, "edit", {
            "node_id": "no-such-node", "expected_revision": 1,
            "node_ids": ["no-such-node"], "parent_id": None,
        })


def test_a_bulk_edit_that_changes_nothing_is_refused(db):
    """Naming ids is not a change: an empty body would report success and do nothing."""
    plan = owner_plan(db)
    task = _node(db, plan.id, "Stale")

    with pytest.raises(service.PlanInputError, match="at least one of later and parent_id"):
        _bulk(db, plan.id, "edit", {"node_ids": [task.id]})
    assert row(db, task.id)["later"] == 0


def test_the_bulk_list_is_bounded_and_every_id_is_an_id(db):
    plan = owner_plan(db)
    task = _node(db, plan.id, "Stale")

    for ids in ([], _PAST_CAP, task.id, [task.id, ""], [47]):
        with pytest.raises(service.PlanInputError, match=f"1 to {MAX_BULK_NODES}"):
            _edits(db, plan.id, ids, later=True)
    assert row(db, task.id)["later"] == 0


def test_the_bulk_body_is_refused_before_any_other_field(db):
    """The body is decided first, so a mixed call is never read as a single edit."""
    plan = owner_plan(db)
    task = _node(db, plan.id, "Stale")

    with pytest.raises(service.PlanInputError, match="no single node"):
        _bulk(db, plan.id, "edit", {
            "node_id": task.id, "expected_revision": task.revision,
            "node_ids": [task.id], "parent_id": None,
        })


# --- bulk close --------------------------------------------------------------


def test_one_bulk_call_closes_every_named_task(db):
    plan = owner_plan(db)
    tasks = [_node(db, plan.id, f"Stale {index}") for index in range(3)]
    kept = _node(db, plan.id, "Kept")

    result = _closes(db, plan.id, [task.id for task in tasks])

    assert [entry["node_id"] for entry in result["closed"]] == [task.id for task in tasks]
    assert result["refused"] == []
    assert all(row(db, task.id)["state"] == "closed" for task in tasks)
    assert row(db, kept.id)["state"] == "pending"


def test_a_bulk_close_keeps_the_guard_that_protects_the_rest_of_the_plan(db):
    """A branch with an open task under it cannot be closed -- and that is its own
    refusal, not a reason to discard the other ids the caller named."""
    plan = owner_plan(db)
    branch = _phase(db, plan.id, "Branch")
    child = _node(db, plan.id, "Open child", parent_id=branch.id)
    stray = _node(db, plan.id, "Stray")

    result = _closes(db, plan.id, [branch.id, stray.id])

    assert [entry["node_id"] for entry in result["closed"]] == [stray.id]
    assert result["refused"][0]["node_id"] == branch.id
    assert child.id in result["refused"][0]["reason"]
    assert row(db, branch.id)["state"] == "pending"


def test_a_bulk_close_asks_the_required_step_guard_for_every_id(db, monkeypatch):
    """Bulk close never skips a run's required step, and one refusal is one row's.

    The guard's own meaning -- that a required step cannot be skipped, and what
    that does to its run -- is proved in the workflow suite (``plans.workflow_fields``
    is tested there, and the single close calls the same function). What is proved
    here is the bulk part: the guard is consulted for each id, and its refusal is
    collected instead of discarding the rest of the list.
    """
    import plans.workflow_fields as workflow_fields

    plan = owner_plan(db)
    step = _node(db, plan.id, "Required step")
    stray = _node(db, plan.id, "Stray")
    asked: list[str] = []

    def refuse_every_step(db, node_id):
        asked.append(node_id)
        if node_id == step.id:
            raise service.PlanError("required workflow step review can't be skipped")

    monkeypatch.setattr(workflow_fields, "refuse_required_skip", refuse_every_step)

    result = _closes(db, plan.id, [step.id, stray.id], reason="Not doing it")

    assert asked == [step.id, stray.id]
    assert [entry["node_id"] for entry in result["closed"]] == [stray.id]
    assert result["refused"][0]["node_id"] == step.id
    assert row(db, step.id)["state"] == "pending"


def test_a_bulk_close_needs_its_reason(db):
    plan = owner_plan(db)
    task = _node(db, plan.id, "Stale")

    with pytest.raises(service.PlanInputError, match="requires a reason"):
        _bulk(db, plan.id, "close_node", {"node_ids": [task.id]})
    assert row(db, task.id)["state"] == "pending"


# --- the executor's slot -----------------------------------------------------


def test_a_bulk_call_frees_the_slot_and_answers_next_once(db):
    """Two hundred tasks parked is one answer about what to do next, not two hundred."""
    plan = owner_plan(db)
    active = _node(db, plan.id, "Active")
    waiting = _node(db, plan.id, "Waiting")
    plan_update(db, plan.id, "activate",
                {"node_id": active.id, "expected_revision": active.revision})

    result = _edits(db, plan.id, [active.id, waiting.id], later=True)

    # Both tasks are below the cut now, so the answer lists nothing above it; what
    # the block reports is the slot the parked task held, and it reports it once.
    assert result["next"]["open_tasks"] == []
    assert result["next"]["selected"] is None
    assert row(db, active.id)["later"] == 1
    assert row(db, waiting.id)["later"] == 1
    assert db.execute(
        "SELECT 1 FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (active.id,),
    ).fetchone() is None


def test_a_bulk_close_frees_the_slot_once_and_starts_nothing_twice(db):
    from plans import admission_attempts

    plan = owner_plan(db)
    active = _node(db, plan.id, "Active")
    plan_update(db, plan.id, "activate",
                {"node_id": active.id, "expected_revision": active.revision})
    stale = _node(db, plan.id, "Stale")
    waiting = _node(db, plan.id, "Waiting")

    result = _closes(db, plan.id, [active.id, stale.id])

    assert [entry["node_id"] for entry in result["closed"]] == [active.id, stale.id]
    assert admission_attempts.slot_holder(db, plan.id, "owner") is None
    # One release and one reconciliation for the whole call, and the same answer a
    # single close gives here: the slot is free and nothing was named to start.
    assert result["next"]["selected"] is None
    assert row(db, waiting.id)["state"] == "pending"
