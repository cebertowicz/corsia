"""Operation-specific protections decide; Plans refuses no tool call.

With no Plans refusal at the tool boundary, a child still cannot change its
parent's plan and a thread still cannot forge a delegation completion. Each denial comes from its own owner - the plan
mutation boundary and the delegation tool's identity check - not from Plans.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import admission, holds, service
from plans.boundary import mutate
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ERROR_HINT,
    add_task,
    dispatch_rows,
    execute_as,
    open_turn,
    registry_of,
    thread_actor,
)
from tools.delegate_send import DelegateSendTool
from tools.plans import PlanUpdateTool

_FORGED_COMPLETION = (
    "Error: completion intent is valid only from a delegated child to "
    "its registered parent during an active turn"
)


def _parent_blocked_task(db, parent_id: str):
    """Give the parent a task in its own plan held by a blocker the user clears.

    Only the user's own blocker is a hold; one anyone else clears is a wait.
    """
    plan = service.ensure_plan("thread", parent_id, db=db)
    task = add_task(db, plan.id, "Parent-owned")
    selected = admission.activate(
        task.id, expected_revision=task.revision, actor_id=parent_id,
        provenance="test", db=db,
    )
    blocked = holds.block(
        task.id, reason_key="missing_instructions",
        reason="Waiting for the user's brief", owner_kind="user",
        owner_id=None, expected_revision=selected["node"]["revision"],
        actor_id=parent_id, provenance="test", db=db,
    )
    db.commit()
    return plan, task, blocked["admission"]["causes"][0]["id"]


def _resolve_params(db, node_id: str, hold_id: str) -> dict:
    return {
        "node_id": node_id, "hold_id": hold_id,
        "expected_revision": service._node_row(db, node_id)["revision"],
    }


def _uncleared(db, hold_id: str) -> bool:
    return db.execute(
        "SELECT cleared_at FROM plan_node_holds WHERE id=?", (hold_id,),
    ).fetchone()["cleared_at"] is None


def test_child_cannot_resolve_a_parent_owned_blocker_at_the_boundary(isolated_db):
    parent_id = mk_session("protect-boundary-parent")
    child_id = mk_session("protect-boundary-child", spawned_by=parent_id)
    db = get_db()
    plan, task, hold_id = _parent_blocked_task(db, parent_id)

    with pytest.raises(PermissionError, match="require the owning thread"):
        mutate(
            plan.id, "resolve_blocker", _resolve_params(db, task.id, hold_id),
            actor=thread_actor(child_id), provenance="test.protections",
            mutation_id="child-resolves-parent-blocker", db=db,
        )
    db.rollback()

    assert _uncleared(db, hold_id)
    db.close()


@pytest.mark.asyncio
async def test_child_plan_update_on_the_parent_plan_is_denied_by_plans_boundary(
    isolated_db,
):
    parent_id = mk_session("protect-tool-parent")
    child_id = mk_session("protect-tool-child", spawned_by=parent_id)
    db = get_db()
    plan, task, hold_id = _parent_blocked_task(db, parent_id)
    identity = open_turn(db, child_id)

    result = await execute_as(
        registry_of(PlanUpdateTool()), identity, child_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "child-tool-resolve",
            "operation": "resolve_blocker",
            "params": _resolve_params(db, task.id, hold_id),
        },
    )

    # plan_update states its own remedy, so the registry appends no hint.
    assert result == "Error: plan is outside the current actor scope"
    assert _uncleared(db, hold_id)
    assert dispatch_rows(db) == []
    db.close()


@pytest.mark.asyncio
async def test_forged_completion_is_rejected_by_delegation_identity(isolated_db):
    """The child has live plan work, so only delegation identity may refuse."""
    parent_id = mk_session("protect-completion-parent")
    child_id = mk_session("protect-completion-child", spawned_by=parent_id)
    wrong_parent_id = mk_session("protect-completion-stranger")
    db = get_db()
    plan = service.ensure_plan("thread", child_id, db=db)
    add_task(db, plan.id, "Child obligation")
    db.commit()
    identity = open_turn(db, child_id)

    result = await execute_as(
        registry_of(DelegateSendTool()), identity, child_id, "delegate_send",
        {"thread_id": wrong_parent_id, "message": "finished", "intent": "completion"},
    )

    assert result == _FORGED_COMPLETION + ERROR_HINT
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=?", (wrong_parent_id,),
    ).fetchone()[0] == 0
    assert dispatch_rows(db) == []
    db.close()
