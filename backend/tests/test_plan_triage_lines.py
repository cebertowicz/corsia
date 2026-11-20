"""Plan order is priority: an owner's start that passes ready work above it is refused.

A named start (`activate`, `start`, or a closing operation's `next_node_id`) by
the plan's owner is refused while a top-level item ranked above it is ready:
neither started nor waiting. The person's own start is never refused. The
expected strings restate the approved copy independently of production. What an
add returns is `test_plan_top.py`'s.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plans import service
from tests.plan_close_next_helpers import (
    HUMAN,
    OWNER,
    close,
    next_block,
    open_attempt,
    owner_plan,
    pause,
    plan_update,
    queue_active,
    request_review,
    revision,
    select,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _refusal(title: str, node_id: str, top_id: str, key: int) -> str:
    return (
        f"Not started: '{title}' ({node_id}) is above this in plan order and ready. Start it "
        f"first, or if this outranks it, reorder node_id={top_id} order_key={key}, then start "
        "this with the revision the reorder returns."
    )


def _refused(db, plan_id: str, node_id: str, operation: str = "activate", **kwargs) -> str:
    with pytest.raises(service.PlanError) as refused:
        _activate(db, plan_id, node_id, operation, **kwargs)
    return str(refused.value)


def _activate(
    db, plan_id: str, node_id: str, operation: str = "activate", *, actor=OWNER,
) -> dict:
    return plan_update(
        db, plan_id, operation,
        {"node_id": node_id, "expected_revision": revision(db, node_id)}, actor=actor,
    )


def _phase(db, plan_id: str, title: str):
    node = service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} is verified", db=db,
    )
    db.commit()
    return node


def _watch(db, plan_id: str, node_id: str) -> None:
    """Park one task on a deadline wait three days out."""
    queue_active(db, node_id)
    deadline = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    plan_update(db, plan_id, "arm_watch", {
        "node_id": node_id, "expected_revision": revision(db, node_id),
        "condition": {"type": "deadline", "value": deadline},
        "expected_event": f"proof.{node_id[:8]}",
    })


def _activations(db, node_id: str) -> int:
    return int(db.execute(
        "SELECT COUNT(*) FROM plan_node_events "
        "WHERE node_id=? AND event='state_transition' AND new_state='active'",
        (node_id,),
    ).fetchone()[0])


def _waiting(db, plan_id: str) -> None:
    _watch(db, plan_id, task(db, plan_id, "Waiting work").id)


def _held(db, plan_id: str) -> None:
    held = task(db, plan_id, "Held work")
    queue_active(db, held.id)
    pause(db, plan_id, held.id)


def _behind_a_prerequisite(db, plan_id: str) -> None:
    """A task whose prerequisite is itself unfinished, in review."""
    prerequisite = task(db, plan_id, "Prerequisite")
    blocked = task(db, plan_id, "Blocked work")
    service.add_dependency(blocked.id, prerequisite.id, db=db)
    db.commit()
    select(db, plan_id, prerequisite.id)
    request_review(db, plan_id, prerequisite.id)


def _user_assigned(db, plan_id: str) -> None:
    task(db, plan_id, "Needs the user", assignee="user")


def _in_review(db, plan_id: str) -> None:
    reviewed = task(db, plan_id, "Work in review")
    select(db, plan_id, reviewed.id)
    request_review(db, plan_id, reviewed.id)


def _later(db, plan_id: str) -> None:
    """A task below the cut: not approved for now, so never passed over."""
    task(db, plan_id, "Someday work", later=True)


def _delegated(db, plan_id: str) -> None:
    """A task handed to a worker is started, even before the worker selects it."""
    handed = task(db, plan_id, "Handed to a worker")
    plan_update(db, plan_id, "attach_spawned_thread", {
        "node_id": handed.id, "expected_revision": revision(db, handed.id),
        "spawned_thread_id": "worker",
    })


NOT_READY_SCENES = {
    "waiting": _waiting,
    "later": _later,
    "held": _held,
    "behind_a_prerequisite": _behind_a_prerequisite,
    "user_assigned": _user_assigned,
    "in_review": _in_review,
    "delegated": _delegated,
}


# --- Named start by the plan's owner ----------------------------------------


@pytest.mark.parametrize("operation", ["activate", "start"])
def test_a_named_start_ahead_of_a_ready_task_is_refused(db, operation):
    plan = owner_plan(db)
    passed = task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask from chat")
    if operation == "start":
        queue_active(db, chosen.id)
    state_before = service._node_row(db, chosen.id)["state"]

    message = _refused(db, plan.id, chosen.id, operation)

    assert message == _refusal("Waiting P0 fix", passed.id, chosen.id, 0)
    assert service._node_row(db, chosen.id)["state"] == state_before
    assert open_attempt(db, chosen.id) is None
    assert _activations(db, chosen.id) == (1 if operation == "start" else 0)


def test_a_refusal_names_the_topmost_ready_task_and_counts_only_open_tasks(db):
    plan = owner_plan(db)
    gone = task(db, plan.id, "No longer wanted")
    service.transition_node(gone.id, "closed", db=db)
    db.commit()
    top = task(db, plan.id, "Topmost ready")
    task(db, plan.id, "Second ready")
    chosen = task(db, plan.id, "New ask")

    assert _refused(db, plan.id, chosen.id) == _refusal("Topmost ready", top.id, chosen.id, 0)


def test_a_nested_start_is_judged_by_the_top_level_item_it_belongs_to(db):
    plan = owner_plan(db)
    passed = task(db, plan.id, "Waiting P0 fix")
    phase = _phase(db, plan.id, "New initiative")
    child = task(db, plan.id, "First step", parent_id=phase.id)

    assert _refused(db, plan.id, child.id) == _refusal(
        "Waiting P0 fix", passed.id, phase.id, 0,
    )


def test_order_inside_one_top_level_item_is_the_agents(db):
    plan = owner_plan(db)
    phase = _phase(db, plan.id, "Initiative")
    task(db, plan.id, "First step", parent_id=phase.id)
    second = task(db, plan.id, "Second step", parent_id=phase.id)

    result = _activate(db, plan.id, second.id)

    assert result["admission"]["condition"] == "running"


def test_a_start_with_nothing_ready_above_runs(db):
    plan = owner_plan(db)
    chosen = task(db, plan.id, "Topmost task")
    task(db, plan.id, "Below it")

    result = _activate(db, plan.id, chosen.id)

    assert result["admission"]["condition"] == "running"


@pytest.mark.parametrize("scene", sorted(NOT_READY_SCENES))
def test_a_start_runs_when_everything_above_is_started_or_waiting(db, scene):
    plan = owner_plan(db)
    NOT_READY_SCENES[scene](db, plan.id)
    chosen = task(db, plan.id, "New ask")

    result = _activate(db, plan.id, chosen.id)

    assert result["admission"]["condition"] == "running"


def test_a_start_after_reordering_above_the_ready_task_runs(db):
    plan = owner_plan(db)
    passed = task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask")
    _refused(db, plan.id, chosen.id)
    plan_update(db, plan.id, "reorder", {
        "node_id": chosen.id, "expected_revision": revision(db, chosen.id), "order_key": 0,
    })

    result = _activate(db, plan.id, chosen.id)

    assert result["admission"]["condition"] == "running"
    assert service._node_row(db, passed.id)["state"] == "pending"


def test_the_persons_own_start_is_never_refused(db):
    plan = owner_plan(db)
    task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "The person's pick")

    result = _activate(db, plan.id, chosen.id, actor=HUMAN)

    assert result["admission"]["condition"] == "running"


def test_a_start_behind_a_held_slot_with_nothing_ready_above_reports_the_conflict(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)
    blocked = task(db, plan.id, "New ask")

    result = _activate(db, plan.id, blocked.id)

    assert result["admission"]["conflict"]["selected_node_id"] == current.id
    assert service._node_row(db, blocked.id)["state"] == "pending"


def test_restarting_a_task_that_holds_its_slot_is_not_refused(db):
    plan = owner_plan(db)
    chosen = task(db, plan.id, "Current work")
    _activate(db, plan.id, chosen.id)
    newer = task(db, plan.id, "Newer ready work")
    plan_update(db, plan.id, "reorder", {
        "node_id": newer.id, "expected_revision": revision(db, newer.id), "order_key": 0,
    })

    again = _activate(db, plan.id, chosen.id, "start")

    assert again["admission"]["condition"] == "running"


def test_an_auto_selected_start_is_not_refused(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    queued = task(db, plan.id, "Queued work")
    task(db, plan.id, "Pending ready work")
    select(db, plan.id, current.id)
    queue_active(db, queued.id)

    result = close(db, plan.id, "request_review", current.id)

    block = next_block(result)
    assert block["selected"]["id"] == queued.id
    assert block["selected"]["how"] == "auto_selected"
    assert "named" not in block


def test_a_next_node_id_start_ahead_of_a_ready_task_is_refused(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    passed = task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask")
    select(db, plan.id, current.id)

    block = next_block(close(db, plan.id, "fail", current.id, next_node_id=chosen.id))

    assert passed.id
    assert block["named"] == {
        "reason": "start Waiting P0 fix first (ready, above this task) or reorder this task above it",
        "refusal": "out_of_order",
        "started": False,
    }
    assert service._node_row(db, chosen.id)["state"] == "pending"
    assert _activations(db, chosen.id) == 0


def test_a_next_node_id_start_with_nothing_ready_above_runs(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    chosen = task(db, plan.id, "Next work")
    select(db, plan.id, current.id)

    block = next_block(close(db, plan.id, "fail", current.id, next_node_id=chosen.id))

    assert block["named"]["started"] is True


def test_a_start_of_work_that_could_not_run_anyway_reports_its_own_blocker(db):
    plan = owner_plan(db)
    task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "Paused ask")
    queue_active(db, chosen.id)
    pause(db, plan.id, chosen.id)

    with pytest.raises(service.PlanError) as refused:
        _activate(db, plan.id, chosen.id, "start")

    assert "not ready to start" in str(refused.value)
