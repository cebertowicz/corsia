"""Plans' own tools record no dispatch; a recorded dispatch never holds a slot.

The tool catalog is the one source of which tools are Plans' own (category
``plans``). The registry runs them without a dispatch row, because whatever
they change commits inside the Plans write itself, so no effect of theirs can
still land after that write. Every other started call under a valid selection
is recorded under its attempt. An unsettled dispatch counts toward the
projection's in-flight count but never defers the attempt's release (#2705).
"""

from __future__ import annotations

import json

import pytest

from infra.database import get_db
from plans import admission, service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ProbeTool,
    add_task,
    dispatch_rows,
    execute_as,
    open_attempt_ids,
    open_turn,
    registry_of,
    selected_plan,
)
from tests.test_plan_reconcile_release import (  # noqa: F401 - shared scaffolding
    HUMAN,
    attempt,
    db,
    open_attempt,
    plan_update,
    queued_pair,
    revision,
)
from tools.catalog import TOOL_CATALOG
from tools.dispatch_ownership import is_plans_catalog_tool
from tools.plans import PlanUpdateTool

#: Plans' own tools, exactly as the tool catalog classifies them.
PLANS_CATALOG_TOOLS = tuple(
    sorted(name for name, entry in TOOL_CATALOG.items() if entry.category == "plans")
)


def _open_dispatch(db, attempt_id: str, tool_name: str, dispatch_id: str) -> None:
    """Seed one started, unsettled call of ``tool_name`` on an attempt."""
    db.execute(
        "INSERT INTO plan_tool_dispatches (id,attempt_id,turn_id,tool_name,started_at) "
        "VALUES (?,?,?,?,?)",
        (dispatch_id, attempt_id, "turn-in-flight", tool_name, service._now()),
    )
    db.commit()


def _pause_node(db, plan_id: str, node_id: str) -> dict:
    return plan_update(
        db, plan_id, "pause_node",
        {"node_id": node_id, "reason": "Operator paused the work",
         "expected_revision": revision(db, node_id)},
        actor=HUMAN,
    )


def _in_flight_count(db, node_id: str) -> int:
    return admission.project(node_id, db=db)["selection"]["in_flight_dispatch_count"]


def test_the_catalog_classifies_plans_own_tools():
    """The parametrized cases below are never silently empty."""
    assert "plan_update" in PLANS_CATALOG_TOOLS


@pytest.mark.parametrize("name", PLANS_CATALOG_TOOLS)
def test_a_harness_prefixed_plans_tool_is_still_plans_own(name):
    """The dispatch-row decision drops the harness prefix, so ``mcp__corsia__plan_update``
    records no dispatch, the same as ``plan_update``."""
    assert is_plans_catalog_tool(name)
    assert is_plans_catalog_tool(f"mcp__corsia__{name}")


@pytest.mark.parametrize(
    "name", ["bash", "mcp__corsia__bash", "Bash", "delegate_task", "mcp__other__plan_update", ""],
)
def test_a_non_plans_tool_keeps_its_dispatch_row_with_or_without_a_prefix(name):
    """Only the corsia prefix is dropped; every other tool is still recorded."""
    assert not is_plans_catalog_tool(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", PLANS_CATALOG_TOOLS)
async def test_a_catalog_plans_tool_runs_under_a_selection_and_records_no_dispatch(
    isolated_db, name,
):
    """Plans' own tool runs with its exact result and leaves no dispatch row.

    The selection is valid - an ordinary call right after it is recorded under
    it - so the missing row comes from the catalog classification, not from a
    missing selection or Turn.
    """
    thread_id = mk_session(f"plans-tool-{name}")
    connection = get_db()
    identity = open_turn(connection, thread_id)
    _plan, selected = selected_plan(connection, thread_id)
    attempt_id = selected["admission"]["selection"]["attempt_id"]
    plans_tool, ordinary = ProbeTool(name, result=f"{name} ran"), ProbeTool()
    registry = registry_of(plans_tool, ordinary)

    assert await execute_as(registry, identity, thread_id, name) == f"{name} ran"

    assert plans_tool.calls == 1
    assert dispatch_rows(connection) == []
    assert open_attempt_ids(connection) == {attempt_id}

    assert await execute_as(registry, identity, thread_id, ordinary.name) == "ok"

    assert [
        (row["tool_name"], row["attempt_id"], row["outcome"])
        for row in dispatch_rows(connection)
    ] == [(ordinary.name, attempt_id, "succeeded")]
    connection.close()


def test_an_open_non_plan_dispatch_keeps_its_attempt_but_not_the_slot(db):
    """A started call stays attributed to its attempt; parking frees the slot (#2705)."""
    plan, task_a, a_attempt, task_b = queued_pair(db)
    _open_dispatch(db, a_attempt, "bash", "shell-call")

    _pause_node(db, plan.id, task_a)

    assert attempt(db, a_attempt)["released_at"] is not None
    assert open_attempt(db, task_b) is not None
    assert [row["attempt_id"] for row in dispatch_rows(db)] == [a_attempt]


def test_in_flight_dispatch_count_is_the_attempts_unsettled_calls(db):
    """The displayed in-flight count is the attempt's unsettled calls.

    Each open call adds one. The count is display only: a pause still releases
    the attempt while calls are open.
    """
    plan, task_a, a_attempt, task_b = queued_pair(db)
    assert _in_flight_count(db, task_a) == 0

    _open_dispatch(db, a_attempt, "bash", "first-call")

    assert _in_flight_count(db, task_a) == 1

    _open_dispatch(db, a_attempt, "write_file", "second-call")

    assert _in_flight_count(db, task_a) == 2
    _pause_node(db, plan.id, task_a)
    assert attempt(db, a_attempt)["released_at"] is not None
    assert open_attempt(db, task_b) is not None


def _edit_requirements(db, plan_id: str, node_id: str) -> dict:
    return plan_update(
        db, plan_id, "edit",
        {"node_id": node_id, "acceptance_criteria": "The revised requirement is verified",
         "expected_revision": revision(db, node_id)},
    )


def test_requirements_edit_with_an_open_non_plan_dispatch_reselects_at_once(db):
    """An effect-carrying call neither keeps the stale attempt nor adds a wait."""
    plan, task_a, stale_attempt, _task_b = queued_pair(db)
    _open_dispatch(db, stale_attempt, "bash", "shell-call")

    edited = _edit_requirements(db, plan.id, task_a)

    assert attempt(db, stale_attempt)["release_reason"] == "requirements_changed"
    assert edited["admission"]["condition"] == "running"
    assert edited["admission"]["selection"]["attempt_id"] != stale_attempt


@pytest.mark.asyncio
async def test_requirements_edit_through_the_real_plan_update_tool_runs_at_revision_two(
    isolated_db,
):
    """The edit call leaves no dispatch, so nothing holds the stale attempt.

    Every other edit test calls the boundary directly. Here the edit is a real
    `plan_update` call by the executor under the very attempt it invalidates,
    and its result is truthful: running at requirements revision 2.
    """
    thread_id = mk_session("plan-tools-edit")
    connection = get_db()
    identity = open_turn(connection, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=connection)
    task = add_task(connection, plan.id, "Revisioned work")
    activated = admission.activate(
        task.id, expected_revision=task.revision, actor_id=thread_id,
        provenance="test", db=connection,
    )
    connection.commit()
    stale_attempt = activated["admission"]["selection"]["attempt_id"]

    raw = await execute_as(
        registry_of(PlanUpdateTool()), identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "edit-through-tool", "operation": "edit",
            "params": {
                "node_id": task.id,
                "acceptance_criteria": "The revised requirement is verified",
                "expected_revision": activated["node"]["revision"],
            },
        },
    )

    assert not raw.startswith("Error"), raw
    edited = json.loads(raw)
    assert edited["node"]["requirements_revision"] == 2
    assert edited["admission"]["condition"] == "running"
    assert edited["admission"]["selection"]["requirements_revision"] == 2
    assert edited["admission"]["selection"]["attempt_id"] != stale_attempt
    assert attempt(connection, stale_attempt)["release_reason"] == "requirements_changed"
    assert dispatch_rows(connection) == []
    connection.close()
