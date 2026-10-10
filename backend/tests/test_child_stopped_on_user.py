"""A worker stopped on the user is heard once; a renewed blocker is not news.

W5 (``shared/planning/plans-work-never-stops-2026-09-28.md``): a delegated
worker whose open work is held only by the user has nothing that will ever
wake it, and no one looks at a subagent's plan. Its parent hears once, when the
worker's Turn ends that way, so it can take the question to the user.
A plan stopped only because the user paused it is the user's own choice: the
parent already has the digest, and nothing wakes a user's pause.

Debt 100 (``shared/planning/PLANS-DEBT-LEDGER.md``): a worker that re-arms the
same blocker is renewing it. The parent reads "renewed until <date>" and is not
woken again; a blocker that cleared and came back is new and wakes it.
"""

from __future__ import annotations

from plans import blocked_wait, holds, service
from automation import watches
from tests.child_parent_notice_helpers import (
    db,
    delegated,
    end_worker_turn,
    notices,
    wakes,
    worker_actor,
)

__all__ = ["db"]

_STOPPED = "is held only by the user"


def _user_blocker(db, node, reason="Needs the user's API key"):
    holds.block(
        node.id, reason_key="needs_user", reason=reason, owner_kind="user",
        owner_id=None, expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="worker", provenance="test", db=db,
    )


def _stopped_wakes(db) -> list:
    return [row for row in wakes(db) if _STOPPED in str(row["content"])]


def test_a_worker_held_only_by_the_user_tells_its_parent_once(db):
    _step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    _user_blocker(db, node)

    end_worker_turn(db)
    end_worker_turn(db)

    [wake] = _stopped_wakes(db)
    content = str(wake["content"])
    assert "Work" in content and "Needs the user's API key" in content
    assert "decision" in content.lower()


def test_a_worker_with_work_it_can_move_is_not_stopped(db):
    _step, plan, node = delegated(db)
    _user_blocker(db, node)
    service.add_node(plan.id, kind="task", title="Other", acceptance_criteria="Done", db=db)

    end_worker_turn(db)

    assert _stopped_wakes(db) == []


def test_a_plan_stopped_only_by_the_users_pause_wakes_no_one(db):
    _step, _plan, node = delegated(db)
    holds.pause_hold(
        node.id, "Hold on", expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="user:user-1", provenance="test", db=db,
    )

    end_worker_turn(db)

    assert _stopped_wakes(db) == []


# --- Debt 100: a renewed blocker ----------------------------------------------


def _wait_on(db, plan, node, reason):
    from plans import watch_operations

    params = blocked_wait.watch_params({
        "node_id": node.id, "reason_key": "missing_input", "reason": reason,
        "owner_kind": "external",
        "expected_revision": service.get_node(node.id, db=db).revision,
    })
    return watch_operations.arm_write(plan.id, params, worker_actor(), db)


def test_re_arming_an_expired_blocker_renews_it_without_a_wake(db):
    _step, plan, node = delegated(db)
    first = _wait_on(db, plan, node, "Needs the upstream fixture")
    watches.expire_watch(first.id, db=db)

    renewed = _wait_on(db, plan, node, "Needs the upstream fixture")

    assert len(wakes(db)) == 1
    last = notices(db)[-1]
    assert last["change_kind"] == "plan.child_blocker_renewed"
    assert f"renewed until {renewed.condition['value']}" in str(last["summary"])


def test_re_arming_a_blocker_still_armed_renews_it_without_a_wake(db):
    _step, plan, node = delegated(db)
    _wait_on(db, plan, node, "Needs the upstream fixture")

    _wait_on(db, plan, node, "Needs the upstream fixture")

    assert len(wakes(db)) == 1
    assert notices(db)[-1]["change_kind"] == "plan.child_blocker_renewed"


def test_a_blocker_that_cleared_and_came_back_wakes_again(db):
    from plans import watch_operations

    _step, plan, node = delegated(db)
    first = _wait_on(db, plan, node, "Needs the upstream fixture")
    watch_operations.resolve(
        plan.id,
        {"node_id": node.id, "watch_id": first.id,
         "expected_revision": service.get_node(node.id, db=db).revision},
        worker_actor(), db,
    )

    _wait_on(db, plan, node, "Needs the upstream fixture")

    assert len(wakes(db)) == 2


def test_the_blocker_reply_names_the_call_that_renews_it():
    watch = type("Watch", (), {"id": "w1", "condition": {"value": "2026-10-01T00:00:00+00:00"}})

    text = blocked_wait.explain(watch)

    assert "block_node" in text and "same reason" in text
