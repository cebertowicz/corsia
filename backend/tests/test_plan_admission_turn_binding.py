"""Turn accounting across requirements edits and commitments.

`plan_node_attempts` alone selects work, and a Turn's plan columns are
write-once attribution. A requirements edit reselects in the same transaction
with no refusal and no manual `start`, and settling one commitment lets the
same Turn select the next. The requirements edit under an already-started call
lives in `test_plan_tool_dispatch_concurrent_calls.py`; which selection a call
is recorded under, whatever the Turn names, lives in
`test_plan_tool_dispatch_attribution.py`.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import (
    admission,
    followups,
    review_evidence,
    service,
)
from plans.boundary import mutate
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ProbeTool,
    add_task,
    execute_as,
    open_turn,
    registry_of,
    thread_actor,
)


def _turn_row(db, turn_id: str):
    return db.execute(
        "SELECT * FROM active_turns WHERE id=?", (turn_id,),
    ).fetchone()


def _attempt_row(db, attempt_id: str):
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()


def _dispatched(db, turn_id: str) -> list[tuple[str, str]]:
    """Return (tool name, attempt id) for every call recorded in one Turn."""
    rows = db.execute(
        """SELECT tool_name,attempt_id FROM plan_tool_dispatches
           WHERE turn_id=? ORDER BY started_at,id""",
        (turn_id,),
    ).fetchall()
    return [(row["tool_name"], row["attempt_id"]) for row in rows]


def _edit_requirements(db, plan_id: str, node, actor_thread: str):
    """Bump one task's requirements revision through the public boundary."""
    return mutate(
        plan_id,
        "edit",
        {
            "node_id": node["id"],
            "acceptance_criteria": "The revised requirement is verified",
            "expected_revision": node["revision"],
        },
        actor=thread_actor(actor_thread),
        provenance="test.turn_binding",
        mutation_id="edit-requirements",
        db=db,
    ).result


@pytest.mark.asyncio
async def test_requirements_edit_reselects_in_the_same_transaction(isolated_db):
    thread_id = mk_session("binding-stale")
    db = get_db()
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = add_task(db, plan.id, "Revisioned work")
    activated = admission.activate(
        node.id, expected_revision=node.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    stale_attempt = activated["admission"]["selection"]["attempt_id"]
    db.commit()

    edited = _edit_requirements(db, plan.id, activated["node"], thread_id)
    db.commit()

    # The stale attempt closes and a valid one opens without `start`.
    reselected = edited["admission"]["selection"]
    assert edited["node"]["requirements_revision"] == 2
    assert _attempt_row(db, stale_attempt)["released_at"] is not None
    assert edited["admission"]["condition"] == "running"
    assert reselected["requirements_revision"] == 2
    assert reselected["attempt_id"] != stale_attempt

    tool = ProbeTool()
    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == "ok"
    assert tool.calls == 1
    assert _dispatched(db, identity.turn_id) == [(tool.name, reselected["attempt_id"])]
    # Turn accounting keeps the first attempt it carried.
    assert _turn_row(db, identity.turn_id)["plan_attempt_id"] == stale_attempt
    db.close()


@pytest.mark.asyncio
async def test_a_reviewer_can_still_use_tools_while_its_work_awaits_review(
    isolated_db,
):
    """Review must not disarm the tools review depends on.

    `review_requested` used to engage the gate, but selection requires an
    active node, so no candidate could ever exist and every work-mode tool was
    refused. The reviewer could not read a file, search, or run a check to
    judge the evidence — its only reachable exit was accepting unverified.
    """
    thread_id = mk_session("binding-remedy")
    db = get_db()
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = add_task(db, plan.id, "Awaiting review")
    activated = admission.activate(
        node.id, expected_revision=node.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    review_evidence.submit(
        node.id, evidence="Focused checks passed.",
        expected_revision=activated["node"]["revision"], actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()

    projected = admission.project(node.id, db=db)
    assert projected["condition"] == "review"

    tool = ProbeTool()
    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == "ok"
    assert tool.calls == 1
    db.close()


@pytest.mark.asyncio
async def test_same_commitment_successor_runs_while_the_turn_keeps_its_first_attempt(
    isolated_db,
):
    thread_id = mk_session("binding-commitment")
    db = get_db()
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    first = add_task(db, plan.id, "First in commitment")
    activated = admission.activate(
        first.id, expected_revision=first.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    first_attempt = activated["admission"]["selection"]["attempt_id"]
    successor = followups.add_follow_up(
        first.id, title="Second in commitment",
        acceptance_criteria="The same commitment continues", actor_id=thread_id,
        provenance="test", inherit_commitment=True, db=db,
    )
    db.commit()
    commitment_id = activated["node"]["commitment_id"]
    assert successor.commitment_id == commitment_id
    assert _turn_row(db, identity.turn_id)["plan_attempt_id"] == first_attempt

    service.transition_node(first.id, "failed", db=db)
    assert admission.release(first.id, "failed", db=db)
    db.commit()

    continued = admission.activate(
        successor.id, expected_revision=successor.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()
    tool = ProbeTool()
    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    successor_attempt = continued["admission"]["selection"]["attempt_id"]
    assert continued["admission"]["condition"] == "running"
    turn = _turn_row(db, identity.turn_id)
    # Write-once accounting keeps the first attempt; the call follows selection.
    assert (turn["plan_attempt_id"], turn["plan_commitment_id"]) == (
        first_attempt, commitment_id,
    )
    assert result == "ok"
    assert _dispatched(db, identity.turn_id) == [(tool.name, successor_attempt)]
    db.close()


def _plan_display(db, plan_id: str) -> tuple[dict, dict]:
    """Project one plan's leaf distribution and its overview display."""
    from plans import operator_display_state, operator_node_state, operator_plan_display

    rows = db.execute("SELECT * FROM plan_nodes WHERE plan_id=?", (plan_id,)).fetchall()
    plan_row = service._plan_row(db, plan_id)
    projection = admission.project_many([(row, plan_row) for row in rows], db=db)
    nodes = [
        {"id": row["id"], "kind": row["kind"], "state": row["state"],
         "parent_id": row["parent_id"], "execution": projection[str(row["id"])],
         "review": None,
         "display": operator_node_state._display(
             {**dict(row), "child_plan": None}, projection[str(row["id"])], None,
             asks_user=operator_display_state.row_asks_user(dict(row), None, projection[str(row["id"])]),
         )}
        for row in rows
    ]
    parents = {node["parent_id"] for node in nodes}
    counts = dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0)
    for node in nodes:
        if node["id"] not in parents:
            counts[operator_node_state.leaf_bucket(node)] += 1
    return counts, operator_plan_display.plan_display(plan_row, counts, nodes)


def test_the_next_commitment_is_selected_in_the_same_turn(isolated_db):
    """Settling one commitment hands the same Turn to the next.

    Accounting stays on the first commitment, the second is selected now with
    no further Turn, liveness still counts it, and the plan reads as advancing
    instead of queued.
    """
    from automation import plan_liveness
    from automation.continuation.sources.liveness import LivenessSource
    from plans import projections

    thread_id = mk_session("same-turn-selection")
    db = get_db()
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    first = add_task(db, plan.id, "First commitment")
    second = add_task(db, plan.id, "Second commitment")
    admission.activate(first.id, expected_revision=first.revision,
                       actor_id=thread_id, provenance="test", db=db)
    current = service._node_row(db, first.id)
    review_evidence.submit(first.id, evidence="Focused proof passed",
                           expected_revision=current["revision"],
                           actor_id=thread_id, provenance="test", db=db)
    service._transition(db, first.id, "accepted", actor_id=thread_id,
                        provenance="test", verification_result="passed",
                        verified_by=thread_id)
    admission.release(first.id, "accepted", db=db)
    db.commit()

    continued = admission.activate(second.id, expected_revision=second.revision,
                                   actor_id=thread_id, provenance="test", db=db)
    db.commit()

    assert continued["node"]["state"] == "active"
    assert continued["admission"]["condition"] == "running"
    assert continued["admission"]["conflict"] is None
    assert _turn_row(db, identity.turn_id)["plan_commitment_id"] == first.commitment_id
    assert projections.liveness_signal(thread_id, db=db).node_ids == (second.id,)

    plan_liveness.on_turn_complete(thread_id, db=db)
    db.commit()
    armed = db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,),
    ).fetchone()
    assert armed["status"] == "armed"
    assert [req.thread_id for req in LivenessSource().due(
        "2099-01-01T00:00:00+00:00", db=db,
    )] == [thread_id]

    counts, display = _plan_display(db, plan.id)
    assert (counts["done"], counts["active"], counts["pending_ready"]) == (1, 1, 0)
    assert display == {"label_key": "working", "tone_key": "active"}
    db.close()


def test_queued_work_without_a_selection_reads_waiting_not_finished(isolated_db):
    """Queued work remains visibly open, never falsely finished, and never
    reads Working while nothing moves it."""
    thread_id = mk_session("pending-only-plan")
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    add_task(db, plan.id, "Not yet started")
    db.commit()

    counts, display = _plan_display(db, plan.id)

    assert counts["pending_ready"] == 1
    assert display == {"label_key": "waiting", "tone_key": "waiting"}
    db.close()
