"""Triage lines: plan order is the priority, and agents are shown where it disagrees.

`add` returns `rank` when a new top-level task lands below ready top-level work
(T1). A named start (`activate`, `start`, or a closing operation's
`next_node_id`) returns `jumped` when it passes ready top-level work (T2), and
records the passed item on the start's activation event for the KPI (T3).
Both are guidance in the tool result, never a refusal. The expected strings
restate the approved copy independently of production.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plans import service
from plans.boundary import PlanActor
from tests.plan_close_next_helpers import (
    close,
    next_block,
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

WORKSPACE_CONDUCTOR = PlanActor("wk-conductor", "user", None)


def _rank_line(rank: int, total: int, above: list[tuple[str, str]], key: int) -> str:
    names = ", ".join(f"'{title}' ({node_id})" for title, node_id in above)
    return (
        f"{rank} of {total} open. Ready above it: {names}. Work runs in plan "
        f"order; reorder it to order_key={key} only if it outranks them."
    )


def _jumped_line(title: str, node_id: str, rank: int, total: int, top_id: str, key: int) -> str:
    return (
        f"Started ahead of ready '{title}' ({node_id}), ranked {rank} of {total}. "
        f"If this outranks it, reorder node_id={top_id} order_key={key} so the plan says so."
    )


def _add(db, plan_id: str, title: str, **params) -> dict:
    body = {"kind": "task", "title": title, "acceptance_criteria": f"{title} is verified", **params}
    return plan_update(db, plan_id, "add", body)


def _activate(db, plan_id: str, node_id: str, operation: str = "activate") -> dict:
    return plan_update(
        db, plan_id, operation,
        {"node_id": node_id, "expected_revision": revision(db, node_id)},
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


def _activation_payload(db, node_id: str) -> dict | None:
    row = db.execute(
        "SELECT payload_json FROM plan_node_events "
        "WHERE node_id=? AND event='state_transition' AND new_state='active'",
        (node_id,),
    ).fetchone()
    assert row is not None
    return None if row["payload_json"] is None else json.loads(row["payload_json"])


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


NOT_READY_SCENES = {
    "waiting": _waiting,
    "held": _held,
    "behind_a_prerequisite": _behind_a_prerequisite,
    "user_assigned": _user_assigned,
    "in_review": _in_review,
}


# --- T1: add ---------------------------------------------------------------


def test_add_below_a_ready_task_reports_its_rank_and_the_ready_task_above(db):
    plan = owner_plan(db)
    ready = task(db, plan.id, "Fix the P0")

    result = _add(db, plan.id, "New ask from chat")

    assert result["rank"] == _rank_line(2, 2, [("Fix the P0", ready.id)], 0)
    assert result["node"]["title"] == "New ask from chat"
    assert result["node"]["state"] == "pending"


def test_add_names_at_most_two_ready_tasks_topmost_first(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "First ready")
    second = task(db, plan.id, "Second ready")
    task(db, plan.id, "Third ready")

    result = _add(db, plan.id, "New ask")

    assert result["rank"] == _rank_line(
        4, 4, [("First ready", first.id), ("Second ready", second.id)], 0,
    )


def test_add_order_key_is_the_live_position_of_the_topmost_ready_task(db):
    plan = owner_plan(db)
    gone = task(db, plan.id, "No longer wanted")
    service.transition_node(gone.id, "closed", db=db)
    db.commit()
    ready = task(db, plan.id, "Ready work")

    result = _add(db, plan.id, "New ask")

    # The closed task takes no rank, so the new task is 2 of 2 open, but it keeps
    # its position: reorder's order_key names a position among open siblings, so the
    # settled task takes none.
    assert result["rank"] == _rank_line(2, 2, [("Ready work", ready.id)], 0)


def test_add_with_nothing_above_has_no_rank_line(db):
    plan = owner_plan(db)

    result = _add(db, plan.id, "First task")

    assert "rank" not in result
    assert result["node"]["title"] == "First task"


@pytest.mark.parametrize("scene", sorted(NOT_READY_SCENES))
def test_add_has_no_rank_line_when_everything_above_is_not_ready(db, scene):
    plan = owner_plan(db)
    NOT_READY_SCENES[scene](db, plan.id)

    result = _add(db, plan.id, "New ask")

    assert "rank" not in result


def test_add_counts_a_top_level_phase_ready_when_a_task_under_it_is(db):
    plan = owner_plan(db)
    phase = _phase(db, plan.id, "Ship the fix")
    task(db, plan.id, "Child work", parent_id=phase.id)

    result = _add(db, plan.id, "New ask")

    assert result["rank"] == _rank_line(2, 2, [("Ship the fix", phase.id)], 0)


def test_add_does_not_count_a_phase_whose_tasks_are_all_not_ready(db):
    plan = owner_plan(db)
    phase = _phase(db, plan.id, "Parked phase")
    _watch(db, plan.id, task(db, plan.id, "Child work", parent_id=phase.id).id)

    result = _add(db, plan.id, "New ask")

    assert "rank" not in result


def test_a_child_add_has_no_rank_line(db):
    plan = owner_plan(db)
    task(db, plan.id, "Ready work")
    phase = _phase(db, plan.id, "A phase")

    result = _add(db, plan.id, "Child of the phase", parent_id=phase.id)

    assert "rank" not in result
    assert result["node"]["parent_id"] == phase.id


def test_insert_has_no_rank_line(db):
    plan = owner_plan(db)
    ready = task(db, plan.id, "Ready work")

    result = plan_update(db, plan.id, "insert", {
        "kind": "task", "title": "Inserted", "acceptance_criteria": "Inserted is verified",
        "before_node_id": ready.id,
    })

    assert "rank" not in result
    assert result["node"]["title"] == "Inserted"


def test_a_conductors_add_to_another_agents_plan_has_no_rank_line(db):
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('wk-conductor','user','test/model','t','t')",
    )
    db.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        ("corsia_agent_thread_id", json.dumps({"session_id": "wk-conductor"}), "t"),
    )
    db.commit()
    plan = owner_plan(db)
    task(db, plan.id, "Ready work")
    body = {"kind": "task", "title": "From the conductor", "acceptance_criteria": "Done"}

    result = plan_update(db, plan.id, "add", body, actor=WORKSPACE_CONDUCTOR)

    assert "rank" not in result
    assert result["node"]["title"] == "From the conductor"


# --- T2: named start -------------------------------------------------------


@pytest.mark.parametrize("operation", ["activate", "start"])
def test_a_named_start_ahead_of_a_ready_task_reports_what_it_jumped(db, operation):
    plan = owner_plan(db)
    passed = task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask from chat")
    if operation == "start":
        queue_active(db, chosen.id)

    result = _activate(db, plan.id, chosen.id, operation)

    assert result["jumped"] == _jumped_line("Waiting P0 fix", passed.id, 1, 2, chosen.id, 0)
    assert result["admission"]["condition"] == "running"


def test_a_start_names_the_topmost_ready_task_and_ranks_among_open_tasks(db):
    plan = owner_plan(db)
    gone = task(db, plan.id, "No longer wanted")
    service.transition_node(gone.id, "closed", db=db)
    db.commit()
    top = task(db, plan.id, "Topmost ready")
    task(db, plan.id, "Second ready")
    chosen = task(db, plan.id, "New ask")

    result = _activate(db, plan.id, chosen.id)

    assert result["jumped"] == _jumped_line("Topmost ready", top.id, 1, 3, chosen.id, 0)


def test_a_start_names_the_top_level_task_a_nested_start_belongs_to(db):
    plan = owner_plan(db)
    passed = task(db, plan.id, "Waiting P0 fix")
    phase = _phase(db, plan.id, "New initiative")
    child = task(db, plan.id, "First step", parent_id=phase.id)

    result = _activate(db, plan.id, child.id)

    assert result["jumped"] == _jumped_line("Waiting P0 fix", passed.id, 1, 2, phase.id, 0)


def test_a_start_with_nothing_ready_above_has_no_jumped_line(db):
    plan = owner_plan(db)
    chosen = task(db, plan.id, "Topmost task")
    task(db, plan.id, "Below it")

    result = _activate(db, plan.id, chosen.id)

    assert "jumped" not in result
    assert result["admission"]["condition"] == "running"


@pytest.mark.parametrize("scene", sorted(NOT_READY_SCENES))
def test_a_start_has_no_jumped_line_when_everything_above_is_not_ready(db, scene):
    plan = owner_plan(db)
    NOT_READY_SCENES[scene](db, plan.id)
    chosen = task(db, plan.id, "New ask")

    result = _activate(db, plan.id, chosen.id)

    assert "jumped" not in result
    assert result["admission"]["condition"] == "running"


def test_a_start_that_does_not_take_the_slot_has_no_jumped_line(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)
    task(db, plan.id, "Ready work")
    blocked = task(db, plan.id, "New ask")

    result = _activate(db, plan.id, blocked.id)

    assert "jumped" not in result
    assert result["admission"]["conflict"]["selected_node_id"] == current.id
    assert service._node_row(db, blocked.id)["state"] == "pending"


def test_a_task_that_is_running_is_not_counted_as_ready_work_waiting(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)

    result = _add(db, plan.id, "New ask")

    assert "rank" not in result


def test_starting_a_task_that_already_holds_its_slot_has_no_jumped_line(db):
    plan = owner_plan(db)
    task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask")
    first = _activate(db, plan.id, chosen.id)
    assert "jumped" in first

    again = _activate(db, plan.id, chosen.id, "start")

    assert "jumped" not in again


def test_an_auto_selected_start_has_no_jumped_line(db):
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
    assert "jumped" not in result
    assert "named" not in block


def test_a_next_node_id_start_ahead_of_a_ready_task_reports_what_it_jumped(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    passed = task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask")
    select(db, plan.id, current.id)

    block = next_block(close(db, plan.id, "fail", current.id, next_node_id=chosen.id))

    assert block["named"]["started"] is True
    assert block["named"]["jumped"] == _jumped_line(
        "Waiting P0 fix", passed.id, 1, 2, chosen.id, 0,
    )


def test_a_next_node_id_start_with_nothing_ready_above_has_no_jumped_line(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    chosen = task(db, plan.id, "Next work")
    select(db, plan.id, current.id)

    block = next_block(close(db, plan.id, "fail", current.id, next_node_id=chosen.id))

    assert block["named"]["started"] is True
    assert "jumped" not in block["named"]


# --- T3: the start event carries the jump -----------------------------------


def test_a_jumping_activation_records_the_passed_task_on_its_event(db):
    plan = owner_plan(db)
    passed = task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask")

    _activate(db, plan.id, chosen.id)

    assert _activation_payload(db, chosen.id) == {"jumped_ready_node_id": passed.id}


def test_a_jumping_next_node_id_activation_records_the_passed_task_on_its_event(db):
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    passed = task(db, plan.id, "Waiting P0 fix")
    chosen = task(db, plan.id, "New ask")
    select(db, plan.id, current.id)

    close(db, plan.id, "fail", current.id, next_node_id=chosen.id)

    assert _activation_payload(db, chosen.id) == {"jumped_ready_node_id": passed.id}


def test_an_activation_that_jumps_nothing_records_no_payload(db):
    plan = owner_plan(db)
    chosen = task(db, plan.id, "Topmost task")
    task(db, plan.id, "Below it")

    _activate(db, plan.id, chosen.id)

    assert _activation_payload(db, chosen.id) is None
