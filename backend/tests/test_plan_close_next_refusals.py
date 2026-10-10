"""A named task that cannot start is reported in `next.named`; the close still commits.

Every reason the named task does not start - unknown or in another plan, not
open, owned by an open user decision, assigned to the user, run by a different
executor, not ready, or its executor slot held - is a successful close whose
`next.named` carries `started: false`, a refusal code and a bounded reason. The
named task is changed only as far as the agent's own activate/start would have
changed it, holds are never cleared, and reconcile still runs.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.database import get_db
from infra.schema import SCHEMA_SQL
from plans import close_next, decisions, service
from tests.plan_close_next_helpers import (
    HUMAN,
    OWNER,
    QUOTES,
    REASON_CAP,
    assert_block_shape,
    close,
    close_params,
    next_block,
    open_attempt,
    owner_plan,
    pause,
    plan_update,
    queue_active,
    receipt_count,
    request_review,
    revision,
    row,
    select,
    size,
    task,
)
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture


def _assert_refused(block: dict, code: str) -> str:
    """Assert one reported refusal and return its reason."""
    assert_block_shape(block, named=True)
    named = block["named"]
    assert (named["started"], named["refusal"]) == (False, code)
    assert isinstance(named["reason"], str) and named["reason"]
    assert size(named["reason"]) <= REASON_CAP + QUOTES
    return named["reason"]


def _durable(db, node_id: str) -> tuple:
    """Return the node facts a reported refusal must leave untouched."""
    stored = row(db, node_id)
    attempt = open_attempt(db, node_id)
    return (
        stored["state"], stored["revision"], stored["archived_at"],
        None if attempt is None else tuple(attempt),
    )


def _selected_a(db, plan):
    task_a = task(db, plan.id, "Task A")
    select(db, plan.id, task_a.id)
    return task_a


def test_an_unknown_task_and_another_plans_task_get_the_same_report(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    earlier = task(db, plan.id, "Earlier queued work")
    select(db, plan.id, task_a.id)
    queue_active(db, earlier.id)
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    foreign = task(db, worker_plan.id, "Worker own plan task")
    foreign_before = _durable(db, foreign.id)
    receipts = receipt_count(db)

    unknown = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id="no-such-node"))
    other_plan = next_block(close(db, plan.id, "request_review", earlier.id, next_node_id=foreign.id))

    reason = _assert_refused(unknown, "unknown")
    assert _assert_refused(other_plan, "unknown") == reason
    assert foreign.id not in reason
    assert receipt_count(db) == receipts + 2
    assert row(db, task_a.id)["state"] == "review_requested"
    # Reconcile still ran after the refused start.
    assert (unknown["selected"]["id"], unknown["selected"]["how"]) == (earlier.id, "auto_selected")
    assert _durable(db, foreign.id) == foreign_before


def _not_open_target(db, plan, target: str) -> tuple[str | None, str]:
    """Build a node that is not an open task; return (node id, word its reason names)."""
    if target == "phase":
        phase = service.add_node(
            plan.id, kind="phase", title="Phase", acceptance_criteria="Phase is verified", db=db,
        )
        db.commit()
        return phase.id, "phase"
    if target == "closed_itself":
        return None, "review_requested"
    node = task(db, plan.id, f"Target {target}")
    if target == "archived":
        plan_update(db, plan.id, "archive_node", close_params(db, "archive_node", node.id))
    elif target == "failed":
        plan_update(db, plan.id, "fail", close_params(db, "fail", node.id))
    else:
        select(db, plan.id, node.id)
        request_review(db, plan.id, node.id)
        if target == "accepted":
            plan_update(db, plan.id, "accept", close_params(db, "accept", node.id))
    return node.id, target


@pytest.mark.parametrize(
    "target", ["archived", "accepted", "review_requested", "failed", "phase", "closed_itself"],
)
def test_a_node_that_is_not_an_open_task_is_reported_with_its_state_or_kind(db, target):
    plan = owner_plan(db)
    node_id, word = _not_open_target(db, plan, target)
    task_a = _selected_a(db, plan)
    node_id = node_id or task_a.id
    before = None if target == "closed_itself" else _durable(db, node_id)

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=node_id))

    assert word in _assert_refused(block, "not_open")
    if before is not None:
        assert _durable(db, node_id) == before


_DECISION = {
    "title": "Which venue should we book?",
    "context": "The launch needs one venue confirmed this week.",
    "tradeoff": "The larger hall costs more; the studio limits attendance.",
    "recommendation": "A",
    "rationale": "Attendance matters more than the price difference.",
    "options": [
        {"id": "A", "label": "Book the hall", "detail": "Seats 200."},
        {"id": "B", "label": "Book the studio", "detail": "Seats 60."},
    ],
    "other_enabled": True,
}


def test_a_task_owned_by_an_open_user_decision_is_reported_and_untouched(db):
    plan = owner_plan(db)
    source = task(db, plan.id, "Decision source")
    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id="draft-venue", actor=OWNER,
        provenance="test.plan_close_next", db=db, **_DECISION,
    ).result["decision"]
    db.commit()
    task_a = _selected_a(db, plan)
    before = _durable(db, decision["node_id"])

    block = next_block(
        close(db, plan.id, "request_review", task_a.id, next_node_id=decision["node_id"]),
    )

    _assert_refused(block, "lane_owned")
    assert _durable(db, decision["node_id"]) == before


def test_a_named_active_task_that_is_held_is_reported_and_its_hold_stays(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    held = task(db, plan.id, "Held work")
    select(db, plan.id, task_a.id)
    queue_active(db, held.id)
    pause(db, plan.id, held.id)
    before = _durable(db, held.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=held.id))

    assert _assert_refused(block, "not_ready").startswith("paused:")
    assert _durable(db, held.id) == before
    assert block["selected"] is None
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (held.id,),
    ).fetchone()[0] == 1


def _fresh_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    seed_threads(connection)
    return connection


def _pending_that_cannot_run(connection, case: str):
    """Return (plan, Task A, pending target, closing operation) for one scene.

    The target cannot take its executor's slot after the close. A case named
    ``user_assigned_*`` is also the user's work. Review refuses while the plan
    is paused, so those scenes close Task A by failing it.
    """
    plan = owner_plan(connection)
    task_a = task(connection, plan.id, "Task A")
    prerequisite = task(connection, plan.id, "Unaccepted prerequisite")
    assignment = {"assignee": "user"} if case.startswith("user_assigned") else {}
    target = task(connection, plan.id, "Target task", **assignment)
    if case.endswith("blocked"):
        service.add_dependency(target.id, prerequisite.id, db=connection)
        connection.commit()
    select(connection, plan.id, task_a.id)
    if not case.endswith("plan_paused"):
        return plan, task_a, target, "request_review"
    plan_update(connection, plan.id, "pause", {
        "expected_revision": service._plan_row(connection, plan.id)["revision"],
    }, actor=HUMAN)
    return plan, task_a, target, "fail"


@pytest.mark.parametrize(
    ("case", "code", "reason_start"),
    [
        ("blocked", "not_ready", "blocked:"),
        ("plan_paused", "not_ready", "paused:"),
        ("user_assigned", "user_assigned", None),
        # The user's work is reported as the user's even while it is also not
        # ready: waiting for its prerequisite would never let the agent start it.
        ("user_assigned_blocked", "user_assigned", None),
        ("user_assigned_plan_paused", "user_assigned", None),
    ],
)
def test_a_named_pending_task_that_cannot_run_is_left_exactly_as_activate_leaves_it(
    case, code, reason_start,
):
    named_db, twin_db = _fresh_db(), _fresh_db()
    try:
        plan, task_a, target, operation = _pending_that_cannot_run(named_db, case)
        twin_plan, twin_a, twin_target, _operation = _pending_that_cannot_run(twin_db, case)

        block = next_block(
            close(named_db, plan.id, operation, task_a.id, next_node_id=target.id),
        )
        close(twin_db, twin_plan.id, operation, twin_a.id)
        plan_update(twin_db, twin_plan.id, "activate", {
            "node_id": twin_target.id, "expected_revision": revision(twin_db, twin_target.id),
        })

        reason = _assert_refused(block, code)
        if reason_start is not None:
            assert reason.startswith(reason_start)
        assert open_attempt(named_db, target.id) is None
        assert _durable(named_db, target.id)[:3] == _durable(twin_db, twin_target.id)[:3]
    finally:
        named_db.close()
        twin_db.close()


def test_a_user_assigned_task_is_never_selected_by_a_named_start(db):
    plan = owner_plan(db)
    user_task = task(db, plan.id, "User task", assignee="user")
    task_a = _selected_a(db, plan)

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=user_task.id))

    _assert_refused(block, "user_assigned")
    assert open_attempt(db, user_task.id) is None
    assert block["selected"] is None
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=?", (user_task.id,),
    ).fetchone()[0] == 0


def test_a_slot_held_by_running_work_is_reported_and_the_named_task_stays_pending(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    running = task(db, plan.id, "Running work")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    queue_active(db, running.id)
    request_review(db, plan.id, task_a.id)
    before = _durable(db, task_b.id)

    block = next_block(close(db, plan.id, "accept", task_a.id, next_node_id=task_b.id))

    assert _assert_refused(block, "slot_held").startswith("executor slot held by Running work")
    assert _durable(db, task_b.id) == before
    assert (block["selected"]["id"], block["selected"]["how"]) == (running.id, "running")


def test_activate_and_a_named_start_leave_a_user_assigned_task_untouched(db):
    """The user's task is theirs to complete: neither call writes it."""
    plan = owner_plan(db)
    user_task = task(db, plan.id, "User sign-off", assignee="user")
    task_a = _selected_a(db, plan)
    before = _durable(db, user_task.id)

    activated = plan_update(db, plan.id, "activate", {
        "node_id": user_task.id, "expected_revision": revision(db, user_task.id),
    })

    assert activated["admission"]["conflict"] == {"condition": "user_assigned"}
    assert _durable(db, user_task.id) == before

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=user_task.id))

    assert _assert_refused(block, "user_assigned") == "assigned to the user"
    assert _durable(db, user_task.id) == before
    assert row(db, user_task.id)["state"] == "pending"


def test_a_task_run_by_a_different_executor_is_refused_before_any_write(db):
    """A scope plan's legacy rows can name another executor; the named start never touches them."""
    plan = owner_plan(db)
    other = task(db, plan.id, "Another executor's task")
    before = _durable(db, other.id)
    slot = close_next.SlotSnapshot(plan.id, "worker", None)

    named = close_next.start_named(db, other.id, slot, actor_id="worker", provenance="test")

    assert (named.refusal, named.reason) == ("other_executor", "run by a different executor")
    assert _durable(db, other.id) == before
