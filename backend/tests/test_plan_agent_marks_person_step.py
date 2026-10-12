"""The plan owner marks a step assigned to the user Done once it is really done.

A person's step can finish outside its row (the user said so in chat, or the
agent verified it happened). The owner agent settles it with ``mark_done`` and
evidence, the row reads Done with who marked it, and it never takes the
executor slot: a user step is never selectable. A decision's review row, a
step with open children and a step the user paused stay out of the agent's
reach, and the user's own Mark done is unchanged.
"""

from __future__ import annotations

import json

import pytest

from plans import closure, node_settlement, operator_projection, service
from plans.boundary import PlanActor
from tests.plan_close_next_helpers import (
    HUMAN,
    OWNER,
    close_params,
    open_attempt,
    owner_plan,
    pause,
    plan_update,
    revision,
    row,
    select,
    task,
)
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture

EVIDENCE = "You said the key is set; a test call returned 200"


def _mark(db, plan_id: str, node_id: str, *, actor=OWNER) -> dict:
    return plan_update(
        db, plan_id, "mark_done", close_params(db, "mark_done", node_id), actor=actor,
    )


def _events(db, node_id: str) -> list[tuple[str, str, str | None]]:
    return [
        (event["prior_state"], event["new_state"], json.loads(event["payload_json"] or "{}").get("verified_by"))
        for event in db.execute(
            """SELECT prior_state, new_state, payload_json FROM plan_node_events
               WHERE node_id=? AND event='state_transition' ORDER BY rowid""",
            (node_id,),
        ).fetchall()
    ]


def _evidence(db, node_id: str) -> list[str]:
    return [
        json.loads(event["payload_json"])["evidence"]
        for event in db.execute(
            """SELECT payload_json FROM plan_node_events
               WHERE node_id=? AND event='review_evidence_submitted' ORDER BY rowid""",
            (node_id,),
        ).fetchall()
    ]


def _pane(db) -> dict[str, dict]:
    entry = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    return {node["id"]: node for node in entry["nodes"]}


def test_the_owner_marks_a_pending_user_step_done_without_the_slot(db):
    plan = owner_plan(db)
    mine = task(db, plan.id, "My work")
    keys = task(db, plan.id, "Set the key", assignee="user")
    attempt = select(db, plan.id, mine.id)

    params = close_params(db, "mark_done", keys.id)
    params["evidence"] = EVIDENCE
    result = plan_update(db, plan.id, "mark_done", params)

    assert result["node"]["state"] == "accepted"
    assert _events(db, keys.id)[-1] == ("review_requested", "accepted", "owner")
    assert _evidence(db, keys.id) == [EVIDENCE]
    assert row(db, mine.id)["state"] == "active"
    assert open_attempt(db, mine.id)["id"] == attempt
    assert open_attempt(db, keys.id) is None


def test_the_owner_marks_an_active_user_step_done(db):
    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")
    service.transition_node(keys.id, "active", db=db)
    db.commit()

    _mark(db, plan.id, keys.id)

    assert row(db, keys.id)["state"] == "accepted"
    assert _events(db, keys.id)[-1] == ("review_requested", "accepted", "owner")


def test_marking_needs_evidence(db):
    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")
    params = close_params(db, "mark_done", keys.id)
    del params["evidence"]

    with pytest.raises(service.PlanInputError):
        plan_update(db, plan.id, "mark_done", params)
    assert row(db, keys.id)["state"] == "pending"


def test_a_decisions_review_row_stays_with_the_decision(db):
    from plans import decisions

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

    with pytest.raises(node_settlement.LaneOwnedNodeError, match="decision operations.*approval"):
        _mark(db, plan.id, raised["node_id"])
    assert row(db, raised["node_id"])["state"] == "review_requested"


def test_marking_withdraws_the_questions_drafted_from_the_step(db):
    from plans import decision_lifecycle, decision_projection, decisions

    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")
    common = {"actor": OWNER, "provenance": "test.mark_done", "db": db}
    drafted = decisions.draft(
        plan.id, source_node_id=keys.id, mutation_id="decision-draft",
        title="Which key do we use?", context="Two keys are in play.",
        tradeoff="One is scoped, one is broad.", recommendation="A",
        rationale="Scoped is safer.", other_enabled=True,
        options=[
            {"id": "A", "label": "Scoped key", "detail": "Read only."},
            {"id": "B", "label": "Broad key", "detail": "Read and write."},
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

    _mark(db, plan.id, keys.id)

    assert row(db, keys.id)["state"] == "accepted"
    assert decision_projection.read(raised["id"], db=db)["state"] == "invalidated"
    reasons = [
        json.loads(event["payload_json"])["reason"]
        for event in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='decision_invalidated'",
            (raised["node_id"],),
        ).fetchall()
    ]
    assert reasons == [decision_lifecycle.SOURCE_MARKED_DONE]


def test_a_step_with_open_children_is_refused(db):
    plan = owner_plan(db)
    parent = service.add_node(
        plan.id, kind="phase", title="Set up the account", assignee="user",
        acceptance_criteria="The account exists", db=db,
    )
    task(db, plan.id, "Pick a plan", parent_id=parent.id)

    with pytest.raises(service.PlanError, match="open"):
        _mark(db, plan.id, parent.id)
    assert row(db, parent.id)["state"] == "pending"


def test_a_step_the_user_paused_is_refused(db):
    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")
    service.transition_node(keys.id, "active", db=db)
    db.commit()
    pause(db, plan.id, keys.id, actor=HUMAN)

    with pytest.raises(service.PlanError, match="paused"):
        _mark(db, plan.id, keys.id)
    assert row(db, keys.id)["state"] == "active"


def test_a_settled_step_is_refused(db):
    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")
    _mark(db, plan.id, keys.id)

    with pytest.raises(service.PlanError):
        _mark(db, plan.id, keys.id)


def test_a_thread_that_does_not_own_the_plan_is_refused(db):
    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")

    with pytest.raises(PermissionError):
        _mark(db, plan.id, keys.id, actor=PlanActor("worker", "user", None))
    assert row(db, keys.id)["state"] == "pending"


def test_the_row_says_which_agent_marked_it(db):
    plan = owner_plan(db)
    db.execute("UPDATE threads SET title='Infrastructure' WHERE id='owner'")
    db.commit()
    keys = task(db, plan.id, "Set the key", assignee="user")
    mine = task(db, plan.id, "My work")

    _mark(db, plan.id, keys.id)
    plan_update(db, plan.id, "mark_done", close_params(db, "mark_done", mine.id))

    nodes = _pane(db)
    assert nodes[keys.id]["status_reason"] == "Marked by Infrastructure"
    assert nodes[mine.id]["status_reason"] != "Marked by Infrastructure"


def test_a_row_the_user_marked_carries_no_marker(db):
    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")

    plan_update(db, plan.id, "mark_done", {
        "node_id": keys.id, "expected_revision": revision(db, keys.id),
    }, actor=HUMAN)

    node = _pane(db)[keys.id]
    assert node["state"] == "accepted"
    assert not (node["status_reason"] or "").startswith("Marked by")


def test_a_closed_row_keeps_its_close_reason(db):
    plan = owner_plan(db)
    keys = task(db, plan.id, "Set the key", assignee="user")
    closure.close_node(keys.id, reason="Not needed", provenance="test", db=db)
    db.commit()

    assert _pane(db)[keys.id]["status_reason"] == "Not needed"


def test_marking_ends_the_steps_other_stops(db):
    from plans import dependencies, holds, node_waits

    plan = owner_plan(db)
    first = task(db, plan.id, "Create the account")
    keys = task(db, plan.id, "Set the key", assignee="user")
    dependencies.add_dependency(keys.id, first.id, actor_id="owner", db=db)
    holds.block_hold(
        keys.id, reason_key="awaiting_user", reason="Waiting on the key",
        owner_kind="user", owner_id=None, expected_revision=revision(db, keys.id),
        actor_id="owner", provenance="test", db=db,
    )
    db.commit()

    _mark(db, plan.id, keys.id)

    assert row(db, keys.id)["state"] == "accepted"
    assert holds.active_holds(keys.id, db=db) == []
    assert node_waits.unmet_prerequisites(db, [keys.id]).get(keys.id, []) == []
