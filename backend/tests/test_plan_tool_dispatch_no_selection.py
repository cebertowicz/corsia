"""Tool calls with no valid plan selection reach their native boundaries.

Plans is work discipline, not universal authorization. When the executor has
no valid selection - pending work, a held task, a
paused plan, work awaiting review, an empty or finished plan, or only work
selected in another thread's plan, such as its parent's - every tool call runs
through the registry's own boundaries. Nothing is refused by Plans, nothing is
recorded, nothing is selected, and no hold is cleared as a side effect.

This invariance holds for every state here because none leaves the executor's
own active (unpaused) plan with reconcilable work: a genuinely empty or
already-settled plan, a held or blocked task, a paused plan, or a selection
that belongs to a different thread's plan. Turn admission owns the initial
selection. Ready work a call finds unselected is also left alone, and
counted as unattributed (the last two tests).
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import admission, holds, service, tool_dispatches
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ProbeTool,
    add_task,
    bash_probe,
    delegate_send_probe,
    dispatch_rows,
    execute_as,
    open_attempt_ids,
    open_turn,
    read_file_probe,
    registry_of,
    selected_plan,
    thread_actor,
)


#: The ``mk_session`` user, who alone pauses without a relay (#1969).
USER = PlanActor(None, "user-1", None, kind="user")


def _mutate(db, thread_id: str, plan_id: str, operation: str, params: dict, *, actor=None):
    """Apply one plan mutation through the real actor boundary and commit it."""
    result = mutate(
        plan_id, operation, params, actor=actor or thread_actor(thread_id),
        provenance="test.plan_tool_dispatch", mutation_id=f"{operation}-{thread_id}",
        db=db,
    ).result
    db.commit()
    return result


def _pending_task(db) -> str:
    thread_id = mk_session("no-selection-pending")
    plan = service.ensure_plan("thread", thread_id, db=db)
    add_task(db, plan.id, "Not started yet")
    db.commit()
    return thread_id


def _paused_task(db) -> str:
    thread_id = mk_session("no-selection-pause-node")
    plan, selected = selected_plan(db, thread_id)
    _mutate(db, thread_id, plan.id, "pause_node", {
        "node_id": selected["node"]["id"], "reason": "Wait for the operator",
        "expected_revision": selected["node"]["revision"],
    }, actor=USER)
    return thread_id


def _blocked_task(db) -> str:
    thread_id = mk_session("no-selection-block-node")
    plan, selected = selected_plan(db, thread_id)
    _mutate(db, thread_id, plan.id, "block_node", {
        "node_id": selected["node"]["id"], "reason_key": "missing_instructions",
        "reason": "Waiting for the brief", "owner_kind": "thread",
        "owner_id": thread_id, "expected_revision": selected["node"]["revision"],
    })
    return thread_id


def _paused_plan(db) -> str:
    thread_id = mk_session("no-selection-paused-plan")
    plan, _selected = selected_plan(db, thread_id)
    _mutate(db, thread_id, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=USER)
    return thread_id


def _review_in_paused_plan(db) -> str:
    thread_id = mk_session("no-selection-review-paused")
    plan, selected = selected_plan(db, thread_id)
    service.transition_node(selected["node"]["id"], "review_requested", db=db)
    db.commit()
    _mutate(db, thread_id, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=USER)
    return thread_id


def _uncertain_effect_hold_in_paused_plan(db) -> str:
    """A hold carrying the `uncertain_effect` reason key, in a paused plan.

    The hold is placed by the `holds.block` writer (which also releases the
    attempt) and the plan is then paused, so the absent selection comes from the
    hold and the pause. Such a hold never gates a call and is not cleared by
    one; `test_plan_turn_terminal_recovery` shows existing hold rows stay
    unchanged byte for byte.
    """
    thread_id = mk_session("no-selection-uncertain-hold")
    plan, selected = selected_plan(db, thread_id)
    holds.block(
        selected["node"]["id"], reason_key="uncertain_effect",
        reason="A tool effect ended without a settled result",
        owner_kind="user", owner_id=None,
        expected_revision=selected["node"]["revision"], actor_id=thread_id,
        provenance="test", db=db,
    )
    db.commit()
    _mutate(db, thread_id, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=USER)
    return thread_id


def _empty_plan(db) -> str:
    thread_id = mk_session("no-selection-empty")
    service.ensure_plan("thread", thread_id, db=db)
    db.commit()
    return thread_id


def _settled_plan(db) -> str:
    thread_id = mk_session("no-selection-settled")
    plan, selected = selected_plan(db, thread_id)
    _mutate(db, thread_id, plan.id, "fail", {
        "node_id": selected["node"]["id"],
        "expected_revision": selected["node"]["revision"],
    })
    return thread_id


def _child_beside_its_parents_selection(db) -> str:
    """The parent runs selected work in its plan; the child has none of its own.

    Only a plan's owner runs its tasks, so the parent's valid selection never
    serves the child's calls.
    """
    parent_id = mk_session("no-selection-parent")
    child_id = mk_session("no-selection-child", spawned_by=parent_id)
    selected_plan(db, parent_id)
    return child_id


_NO_SELECTION_STATES = {
    "pending_task": _pending_task,
    "pause_node_hold": _paused_task,
    "block_node_hold": _blocked_task,
    "paused_plan": _paused_plan,
    "review_requested_in_paused_plan": _review_in_paused_plan,
    "uncertain_effect_hold_in_paused_plan": _uncertain_effect_hold_in_paused_plan,
    "empty_plan": _empty_plan,
    "fully_settled_plan": _settled_plan,
    "child_beside_its_parents_selection": _child_beside_its_parents_selection,
}


def _plan_truth(db) -> dict:
    """Snapshot every task's lifecycle and effective condition."""
    rows = db.execute(
        "SELECT id,state,revision FROM plan_nodes WHERE kind='task'",
    ).fetchall()
    return {
        row["id"]: (
            row["state"], row["revision"],
            admission.project(row["id"], db=db)["condition"],
        )
        for row in rows
    }


def _recorded(db) -> list[tuple[str, str]]:
    """Return (tool name, attempt id) for every dispatch row."""
    return [(row["tool_name"], row["attempt_id"]) for row in dispatch_rows(db)]


def _uncleared_hold_ids(db) -> set[str]:
    rows = db.execute(
        "SELECT id FROM plan_node_holds WHERE cleared_at IS NULL",
    ).fetchall()
    return {str(row["id"]) for row in rows}


def _probes() -> tuple[list[ProbeTool], list[tuple[str, dict]]]:
    """Inspection, control, stop, communication, and mixed read/write calls."""
    tools = [
        ProbeTool("uncataloged_probe", result="uncatalogued ran"),
        read_file_probe(result="file read ran"),
        ProbeTool("plan_read", result="plan read ran"),
        ProbeTool("managed_process_stop", result="stop ran"),
        ProbeTool("restart_resume", result="restart resume ran"),
        delegate_send_probe(result="message queued"),
        bash_probe(result="shell ran"),
    ]
    calls = [
        ("uncataloged_probe", {}),
        ("read_file", {"path": "notes.txt"}),
        ("plan_read", {}),
        ("managed_process_stop", {}),
        ("restart_resume", {}),
        ("delegate_send", {
            "thread_id": "someone-else", "message": "status update",
            "intent": "progress",
        }),
        ("bash", {"command": "cat notes.txt"}),
        ("bash", {"command": "printf done > notes.txt"}),
    ]
    return tools, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("state_name", sorted(_NO_SELECTION_STATES))
async def test_every_call_runs_unrecorded_without_a_valid_selection(
    isolated_db, state_name,
):
    """No valid selection is not a reason to refuse any kind of tool call.

    Each call must reach the tool itself and return its exact result. Holds,
    task states, and executor slots are unchanged afterwards: running an
    unrelated call during a real hold does not resume the held task.
    """
    db = get_db()
    executor = _NO_SELECTION_STATES[state_name](db)
    identity = open_turn(db, executor)
    tools, calls = _probes()
    by_name = {tool.name: tool for tool in tools}
    registry = registry_of(*tools)
    attempts_before = open_attempt_ids(db)
    holds_before = _uncleared_hold_ids(db)
    truth_before = _plan_truth(db)

    for name, params in calls:
        result = await execute_as(registry, identity, executor, name, params)
        assert result == by_name[name].result

    assert {name: tool.calls for name, tool in by_name.items()} == {
        "uncataloged_probe": 1, "read_file": 1, "plan_read": 1,
        "managed_process_stop": 1, "restart_resume": 1, "delegate_send": 1, "bash": 2,
    }
    assert _recorded(db) == []
    assert open_attempt_ids(db) == attempts_before
    assert _uncleared_hold_ids(db) == holds_before
    assert _plan_truth(db) == truth_before
    db.close()


@pytest.mark.asyncio
async def test_a_call_without_turn_identity_runs_unrecorded_under_a_selection(
    isolated_db,
):
    """A checkpoint-shaped call has no Turn to account to, so it runs unlinked."""
    thread_id = mk_session("no-turn-identity")
    db = get_db()
    open_turn(db, thread_id)
    _plan, selected = selected_plan(db, thread_id)
    tool = ProbeTool(result="checkpoint read ran")

    result = await execute_as(registry_of(tool), None, thread_id, tool.name)

    assert result == "checkpoint read ran"
    assert tool.calls == 1
    assert _recorded(db) == []
    assert open_attempt_ids(db) == {selected["admission"]["selection"]["attempt_id"]}
    db.close()


TOOL = "bash"


def _open_attempts(db) -> list:
    return db.execute(
        "SELECT node_id FROM plan_node_attempts WHERE released_at IS NULL",
    ).fetchall()


def _record_locks(db, thread_id: str, turn_id: str, dispatch_id: str) -> tuple[bool, int]:
    """Record one call and return (recorded, write locks taken)."""
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        recorded = tool_dispatches.record(thread_id, turn_id, TOOL, dispatch_id=dispatch_id)
    finally:
        db.set_trace_callback(None)
    locks = sum(
        1 for statement in statements
        if statement.lstrip().upper().startswith("BEGIN IMMEDIATE")
    )
    return recorded, locks


def test_ready_unselected_work_stays_unselected_by_a_tool_call(isolated_db):
    """A directly opened Turn's call records nothing and selects nothing."""
    thread_id = mk_session("dispatch-never-selects-ready")
    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        task = add_task(db, plan.id, "Ready work")
        service.transition_node(task.id, "active", db=db)
        db.commit()
        identity = open_turn(db, thread_id)

        recorded, locks = _record_locks(db, thread_id, identity.turn_id, "dispatch-ready")

        assert recorded is False
        assert locks == 0
        assert _open_attempts(db) == []
        assert dispatch_rows(db) == []
        assert tool_dispatches._UNATTRIBUTED_COUNTS[identity.turn_id] == (thread_id, 1)
    finally:
        tool_dispatches._UNATTRIBUTED_COUNTS.clear()
        db.close()


def test_an_unreconciled_release_is_not_repaired_by_a_tool_call(isolated_db):
    """A release that skipped reconciliation leaves the next task for admission."""
    thread_id = mk_session("dispatch-never-selects-release")
    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        first = add_task(db, plan.id, "First ready", order_key=0)
        second = add_task(db, plan.id, "Second ready", order_key=1)
        service.transition_node(first.id, "active", db=db)
        service.transition_node(second.id, "active", db=db)
        admission.reconcile_plan(plan.id, actor_id=None, provenance="test", db=db)
        db.commit()
        service.transition_node(first.id, "failed", db=db)
        assert admission.release(first.id, "test_setup_empty_slot", db=db)
        db.commit()
        identity = open_turn(db, thread_id)

        recorded, locks = _record_locks(db, thread_id, identity.turn_id, "dispatch-release")

        assert recorded is False
        assert locks == 0
        assert _open_attempts(db) == []
        assert dispatch_rows(db) == []
    finally:
        tool_dispatches._UNATTRIBUTED_COUNTS.clear()
        db.close()
