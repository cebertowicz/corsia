"""Which reason a named task that did not start reports, and how that reason reads.

When several reasons apply, `next.named` reports the first in contract order:
unknown, not open, owned by an open user decision, assigned to the user, not
ready, then the executor slot held. The user's work is reported as the user's
even when it is also waiting, because no later change lets the agent start it. Every reason the eligibility
owner gives for a task that cannot take its slot is reported and the close
commits. A slot-held reason is an action, and a long holder title is cut so
that action is never lost.
"""

from __future__ import annotations

import pytest

from plans import selection_validity
from tests.plan_close_next_helpers import (
    MARKER,
    QUOTES,
    REASON_CAP,
    assert_block_shape,
    close,
    next_block,
    open_attempt,
    owner_plan,
    pause,
    queue_active,
    request_review,
    row,
    select,
    size,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture

#: A holder title at the authoring maximum of 60 characters.
LONG_TITLE = "Running work with a sixty character title that fills the cap"
SLOT_HELD_PREFIX = "executor slot held by "


def _named(block: dict) -> tuple:
    assert_block_shape(block, named=True)
    named = block["named"]
    assert named["started"] is False
    return named["refusal"], named["reason"]


def _durable(db, node_id: str) -> tuple:
    stored = row(db, node_id)
    attempt = open_attempt(db, node_id)
    return stored["state"], stored["revision"], None if attempt is None else tuple(attempt)


def test_a_held_active_user_assigned_task_is_reported_as_the_users_and_stays_held(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    user_task = task(db, plan.id, "User sign-off", assignee="user")
    select(db, plan.id, task_a.id)
    queue_active(db, user_task.id)
    pause(db, plan.id, user_task.id)
    before = _durable(db, user_task.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=user_task.id))

    assert _named(block) == ("user_assigned", "assigned to the user")
    assert _durable(db, user_task.id) == before
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (user_task.id,),
    ).fetchone()[0] == 1


def test_the_eligibility_owner_names_its_reasons_in_report_order():
    """Each reason has a scene in the close-next suite that reports it.

    `not_task` and `archived` are judged before the named start writes and
    read as `not_open`; `user_assigned` reads as the user's; the rest read as
    `not_ready`. A reason added to the eligibility owner joins this tuple only
    with a scene proving the named start reports it and the close commits.
    """
    assert selection_validity.SLOT_BLOCKERS == (
        "not_task", "archived", "user_assigned", "plan_inactive", "has_children", "held",
    )


def test_a_legacy_task_with_child_nodes_is_reported_not_ready_as_activate_leaves_it(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    parent = task(db, plan.id, "Legacy parent task")
    child = task(db, plan.id, "Legacy child task")
    # Tasks are leaves today; a legacy row can still sit under one.
    db.execute("UPDATE plan_nodes SET parent_id=? WHERE id=?", (parent.id, child.id))
    db.commit()
    select(db, plan.id, task_a.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=parent.id))

    assert _named(block) == ("not_ready", "task has child nodes")
    assert (row(db, parent.id)["state"], open_attempt(db, parent.id)) == ("active", None)
    # The parent is not an open task; the child under it is.
    assert [item["id"] for item in block["open_tasks"]] == [child.id]


def test_a_task_in_a_plan_that_is_not_active_names_the_plan_state(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    # A legacy plan state no plan writer produces today.
    db.execute("UPDATE plans SET state='complete' WHERE id=?", (plan.id,))
    db.commit()

    block = next_block(close(db, plan.id, "fail", task_a.id, next_node_id=task_b.id))

    assert _named(block) == ("not_ready", "plan is complete")
    assert row(db, task_a.id)["state"] == "failed"
    assert open_attempt(db, task_b.id) is None


def _slot_held_scene(db, variant: str) -> tuple[str, str, str]:
    """Return (plan id, Task A in review, named task id) while a long-titled task runs.

    The named task is pending, or active and queued when ``variant`` is
    ``active``.
    """
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    holder = task(db, plan.id, LONG_TITLE)
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    queue_active(db, holder.id)
    if variant == "active":
        queue_active(db, task_b.id)
    request_review(db, plan.id, task_a.id)
    return plan.id, task_a.id, task_b.id


def _assert_action_whole(reason: str, action: str) -> None:
    """Assert the reason keeps its action whole and cuts only the holder's title."""
    assert reason.startswith(SLOT_HELD_PREFIX)
    assert reason.endswith(action)
    title = reason[len(SLOT_HELD_PREFIX):-len(action)]
    assert title.endswith(MARKER)
    kept = title[:-len(MARKER)]
    assert kept and LONG_TITLE.startswith(kept)
    assert size(reason) <= REASON_CAP + QUOTES
    # Longest cut: one more title character would not have fit.
    fuller = SLOT_HELD_PREFIX + LONG_TITLE[: len(kept) + 1] + MARKER + action
    assert size(fuller) > REASON_CAP


@pytest.mark.parametrize(
    ("variant", "action"),
    [
        ("pending", "; activate this task after that task closes"),
        ("active", "; start this task after that task closes"),
    ],
)
def test_a_long_holder_title_is_cut_so_the_slot_held_action_stays_whole(db, variant, action):
    plan_id, task_a_id, task_b_id = _slot_held_scene(db, variant)
    before = _durable(db, task_b_id)

    block = next_block(close(db, plan_id, "accept", task_a_id, next_node_id=task_b_id))

    refusal, reason = _named(block)
    assert refusal == "slot_held"
    _assert_action_whole(reason, action)
    assert _durable(db, task_b_id) == before
