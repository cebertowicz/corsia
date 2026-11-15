"""An agent's check-back watch never hides the person's own task (#2631).

A task assigned to the person still reads Needs you, counts as theirs and stays
listed while all that holds it is the agent's own watch (a set-aside date or a
plain watch). A blocker armed as a wait, a dependency or a pause still defers it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plans import operator_projection
from tests.plan_close_next_helpers import HUMAN, owner_plan, plan_update, revision, task
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture


def _later(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _user_task(db, plan_id: str, title: str = "Sign in to the account") -> str:
    result = plan_update(db, plan_id, "add", {
        "title": title, "acceptance_criteria": "Signed in", "assignee": "user",
        "size": "S", "intent": "The person signs in",
        "description": "Open the link and sign in. Already tried: the API",
    })
    return result["node"]["id"]


def _watch(db, plan_id: str, node_id: str, kind: str) -> None:
    params = {
        "node_id": node_id, "expected_revision": revision(db, node_id),
        "condition": {"type": "deadline", "value": _later(2)},
    }
    if kind == "set_aside":
        params["reason"] = "Check back on the person's step"
    else:
        params["expected_event"] = "external.done"
    plan_update(db, plan_id, "arm_watch", params)


def _shown(db, node_id: str) -> tuple[dict, dict]:
    envelope = operator_projection.read_thread("owner", actor=HUMAN, db=db)
    entry = envelope["plans"][0]
    return envelope, next(node for node in entry["nodes"] if node["id"] == node_id)


@pytest.mark.parametrize("kind", ["set_aside", "plain"])
def test_agent_check_back_keeps_the_persons_task_theirs(db, kind):
    plan = owner_plan(db)
    task(db, plan.id, "Agent work")
    node_id = _user_task(db, plan.id)
    _watch(db, plan.id, node_id, kind)

    envelope, node = _shown(db, node_id)

    assert not node["folded"]  # shown in the pane, not tucked away
    assert node["execution"]["condition"] == "waiting"
    assert node["display"]["label_key"] == "needs_you"
    assert node["requires_user_action"] is True
    assert node["scheduled_until"] is None  # never folded away as the agent's own schedule
    assert envelope["plans"][0]["display"]["label_key"] == "needs_you"


def test_blocker_wait_on_the_persons_task_still_defers_it(db):
    plan = owner_plan(db)
    node_id = _user_task(db, plan.id)
    plan_update(db, plan.id, "block_node", {
        "node_id": node_id, "expected_revision": revision(db, node_id),
        "reason_key": "awaiting_vendor", "reason": "The vendor has to enable the account",
        "owner_kind": "external", "owner_id": None,
    })

    _envelope, node = _shown(db, node_id)

    assert node["display"]["label_key"] == "waiting"
    assert node["requires_user_action"] is False


def test_dependency_on_the_persons_task_still_defers_it(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "Prepare the account")
    node_id = _user_task(db, plan.id)
    plan_update(db, plan.id, "add_dependency", {
        "node_id": node_id, "expected_revision": revision(db, node_id), "depends_on_node_id": first.id,
    })
    _watch(db, plan.id, node_id, "set_aside")

    _envelope, node = _shown(db, node_id)

    assert node["display"]["label_key"] == "waiting"
    assert node["requires_user_action"] is False


def test_paused_plan_keeps_the_look_but_defers_the_ask(db):
    plan = owner_plan(db)
    node_id = _user_task(db, plan.id)
    _watch(db, plan.id, node_id, "set_aside")
    plan_update(db, plan.id, "pause", {"expected_revision": db.execute(
        "SELECT revision FROM plans WHERE id=?", (plan.id,)).fetchone()[0]}, actor=HUMAN)

    _envelope, node = _shown(db, node_id)

    assert node["display"]["label_key"] == "needs_you"
    assert node["requires_user_action"] is False
