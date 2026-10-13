"""A user-step creation guard applies only to new agent-authored steps."""

from __future__ import annotations

import pytest

from plans import service
from plans.boundary import PlanActor, mutate
from plans.boundary_fields import USER_STEP_DESCRIPTION_REFUSAL
from tests.plan_close_next_helpers import OWNER, owner_plan, plan_update, revision, row, task
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401


_DESCRIPTION = "Run the command below.\n\nAlready tried: I ran it through the supported agent route."


def _add(plan_id: str, *, db, assignee: str | None = "user") -> dict:
    return plan_update(
        db,
        plan_id,
        "add",
        {
            "kind": "task",
            "title": "Run the command",
            "acceptance_criteria": "The command reports success.",
            "assignee": assignee,
            "description": _DESCRIPTION,
        },
    )


def test_add_and_insert_require_a_nonempty_tried_marker_for_new_user_steps(db):
    plan = owner_plan(db)
    anchor = task(db, plan.id, "Anchor")
    for operation, params in (
        ("add", {"kind": "task", "title": "Do it", "acceptance_criteria": "Done", "assignee": "user"}),
        ("insert", {"kind": "task", "title": "Do it", "acceptance_criteria": "Done", "assignee": "user", "before_node_id": anchor.id, "description": "How to do it."}),
    ):
        with pytest.raises(service.PlanInputError, match="Already tried"):
            plan_update(db, plan.id, operation, params)

    assert _add(plan.id, db=db)["node"]["assignee"] == "user"


def test_creation_gate_leaves_agent_adds_and_existing_user_step_paths_usable(db):
    plan = owner_plan(db)
    agent = _add(plan.id, db=db, assignee=None)
    user = _add(plan.id, db=db)["node"]

    assert agent["node"]["assignee"] is None
    plan_update(
        db, plan.id, "edit",
        {"node_id": user["id"], "expected_revision": user["revision"], "description": "Updated instructions."},
    )
    assert row(db, user["id"])["description"] == "Updated instructions."

    settled = plan_update(
        db, plan.id, "mark_done",
        {"node_id": user["id"], "expected_revision": revision(db, user["id"]), "evidence": "The person confirmed it."},
    )
    assert settled["node"]["state"] == "accepted"

    closed = _add(plan.id, db=db)["node"]
    plan_update(
        db, plan.id, "close_node",
        {"node_id": closed["id"], "expected_revision": closed["revision"], "reason": "The person will not do this."},
    )
    assert row(db, closed["id"])["state"] == "closed"


def test_decision_writer_and_worker_refusal_stay_outside_the_creation_gate(db):
    plan = owner_plan(db)
    source = task(db, plan.id, "Choose a name")
    from plans import decisions

    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id="draft-decision",
        title="Which name?", context="Two names fit.", tradeoff="Clarity versus brevity.",
        recommendation="A", rationale="Clarity wins.", other_enabled=True,
        options=[{"id": "A", "label": "Clear"}, {"id": "B", "label": "Short"}],
        actor=OWNER, provenance="test.user-step-gate", db=db,
    ).result["decision"]
    assert row(db, decision["node_id"])["assignee"] == "user"

    from plans.delegation import attach_spawned_thread

    delegated = task(db, plan.id, "Worker task")
    worker = service.ensure_plan("thread", "worker", db=db)
    attach_spawned_thread(delegated.id, "worker", actor_id="owner", db=db)
    worker_actor = PlanActor("worker", "user", None)
    with pytest.raises(service.PlanInputError, match="delegated worker"):
        mutate(
            worker.id, "add",
            {"kind": "task", "title": "Ask the person", "acceptance_criteria": "Done", "assignee": "user"},
            actor=worker_actor, provenance="test.user-step-gate", mutation_id="worker-user-step", db=db,
        )


def test_refusal_gives_the_one_line_fix():
    assert USER_STEP_DESCRIPTION_REFUSAL == (
        "user steps need instructions with 'Already tried: …'; add what you tried"
    )
