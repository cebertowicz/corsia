"""The user marks any open row done; agents mark only their active work done.

The operator projection offers the user Mark done and Won't do on every open
row, whoever it is assigned to (``plans.user_settle``). Mark done accepts it in
one write whose lifecycle events all read as the user's, and Won't do closes
it with a reason; the user's archive stays the separate operation that removes
a task. The agent tool shares the Mark done name but never gains this override:
a user-assigned row stays the user's, and every ordinary stop still binds.

Answering is what finishes a question, so the decision lane still owns the
review node a raised decision created: Mark done is refused there, and Won't do
withdraws the question.
"""

from __future__ import annotations

import pytest

from plans import boundary_fields, user_steering
from tests.plan_close_next_helpers import (
    HUMAN,
    OWNER,
    owner_plan,
    plan_update,
    revision,
    row,
    task,
)
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture


def _mark_done(db, plan_id: str, node_id: str, *, actor=HUMAN) -> dict:
    return plan_update(
        db, plan_id, "mark_done",
        {"node_id": node_id, "expected_revision": revision(db, node_id)}, actor=actor,
    )


def _transitions(db, node_id: str) -> list[tuple[str, str, str]]:
    return [
        (event["prior_state"], event["new_state"], event["actor_id"])
        for event in db.execute(
            """SELECT prior_state, new_state, actor_id FROM plan_node_events
               WHERE node_id=? AND event='state_transition' ORDER BY rowid""",
            (node_id,),
        ).fetchall()
    ]


def test_the_user_marks_their_pending_task_done_in_one_write(db):
    plan = owner_plan(db)
    user_task = task(db, plan.id, "User sign-off", assignee="user")

    result = _mark_done(db, plan.id, user_task.id)

    assert result["node"]["state"] == "accepted"
    assert row(db, user_task.id)["state"] == "accepted"
    assert _transitions(db, user_task.id) == [
        ("pending", "active", HUMAN.audit_id),
        ("active", "review_requested", HUMAN.audit_id),
        ("review_requested", "accepted", HUMAN.audit_id),
    ]


def test_a_task_awaiting_the_users_review_is_accepted_directly(db):
    plan = owner_plan(db)
    user_task = task(db, plan.id, "User sign-off", assignee="user")
    plan_update(db, plan.id, "activate", {
        "node_id": user_task.id, "expected_revision": revision(db, user_task.id),
    }, actor=HUMAN)
    plan_update(db, plan.id, "request_review", {
        "node_id": user_task.id, "evidence": "Signed", "expected_revision": revision(db, user_task.id),
    }, actor=HUMAN)

    _mark_done(db, plan.id, user_task.id)

    assert _transitions(db, user_task.id)[-1] == ("review_requested", "accepted", HUMAN.audit_id)


def test_an_agent_cannot_mark_the_users_task_done(db):
    plan = owner_plan(db)
    user_task = task(db, plan.id, "User sign-off", assignee="user")

    with pytest.raises(PermissionError):
        _mark_done(db, plan.id, user_task.id, actor=OWNER)
    assert row(db, user_task.id)["state"] == "pending"


def test_the_user_marks_done_a_task_an_agent_owns(db):
    plan = owner_plan(db)
    agent_task = task(db, plan.id, "Agent work")

    _mark_done(db, plan.id, agent_task.id)

    assert row(db, agent_task.id)["state"] == "accepted"


def test_archive_removes_the_users_task(db):
    plan = owner_plan(db)
    user_task = task(db, plan.id, "User sign-off", assignee="user")

    plan_update(db, plan.id, "archive_node", {
        "node_id": user_task.id, "expected_revision": revision(db, user_task.id),
    }, actor=HUMAN)

    assert row(db, user_task.id)["archived_at"] is not None


@pytest.mark.parametrize(
    ("node", "review", "actions"),
    [
        ({"kind": "task", "assignee": "user", "state": "pending"}, None,
         ["mark_done", "pause_node", "close_node"]),
        ({"kind": "task", "assignee": "user", "state": "review_requested"}, {"kind": "human_review"},
         ["mark_done", "pause_node", "close_node"]),
        ({"kind": "task", "assignee": "user", "state": "review_requested"}, {"kind": "human_decision"}, ["close_node"]),
        ({"kind": "task", "assignee": "user", "state": "accepted"}, None, []),
        ({"kind": "task", "assignee": None, "state": "pending"}, None,
         ["mark_done", "pause_node", "close_node"]),
        ({"kind": "phase", "assignee": "user", "state": "pending"}, None, ["mark_done", "close_node"]),
    ],
)
def test_the_plan_pane_offers_mark_done_and_won_t_do_on_any_open_row(node, review, actions):
    assert user_steering.offered(
        node, review, (), plan_state="active", worker_plan_state=None,
    ) == actions


def test_mark_done_never_settles_a_decision_the_user_has_not_answered(db):
    from plans import decision_projection, decisions, node_settlement

    plan = owner_plan(db)
    source = task(db, plan.id, "Name the feature")
    common = {"actor": OWNER, "provenance": "test.mark_done", "db": db}
    drafted = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id="decision-draft",
        title="What do we call it?", context="Two names are in play.",
        tradeoff="One is shorter, one is clearer.", recommendation="A",
        rationale="Clarity wins.", other_enabled=True,
        options=[
            {"id": "A", "label": "Plans", "detail": "The shorter name."},
            {"id": "B", "label": "Structured plans", "detail": "The clearer name."},
        ],
        **common,
    ).result["decision"]
    reviewed = decisions.parent_review(
        drafted["id"], expected_revision=drafted["revision"],
        mutation_id="decision-review", **common,
    ).result["decision"]
    raised = decisions.raise_to_user(
        reviewed["id"], expected_revision=reviewed["revision"],
        mutation_id="decision-raise", **common,
    ).result["decision"]

    with pytest.raises(node_settlement.LaneOwnedNodeError):
        _mark_done(db, plan.id, raised["node_id"])

    assert row(db, raised["node_id"])["state"] == "review_requested"
    assert decision_projection.read(raised["id"], db=db)["state"] == "raised"


def test_mark_done_is_shared_vocabulary_but_agent_evidence_is_required():
    from tools.plans import PlanUpdateTool
    from tests.test_plan_tool_schema_contract import _rules

    tool = PlanUpdateTool()
    operations = tool.parameters["properties"]["operation"]["enum"]
    required = _rules(tool)["mark_done"]["properties"]["params"]["required"]

    assert "mark_done" not in boundary_fields.HUMAN_OPERATIONS
    assert "mark_done" in operations
    assert set(required) == {"node_id", "expected_revision", "evidence"}
