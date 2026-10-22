"""System code opens a step for the person in an agent's plan and settles it when done.

The shared browser's password guard is the first caller: a password page opens
one step per page visit, the agent's current task waits on it, and the step
settles when the guard observes the sign-in, with the system (not the agent,
not the person) on the record.
"""

from __future__ import annotations

import json

import pytest

from plans import lifecycle, public, service
from tests.plan_close_next_helpers import (
    HUMAN,
    OWNER,
    close_params,
    open_attempt,
    owner_plan,
    pause,
    plan_update,
    row,
    select,
    task,
)
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture

PROVENANCE = "browser.password_guard"
TITLE = "Enter your password in the browser (example.com)"
DESCRIPTION = "Enter your password in the browser pane. The agent can't see this page and carries on once you're past it."


def _open(db, key: str = "visit-1") -> str:
    node_id = public.open_user_step(
        db, "owner", key=key, title=TITLE, description=DESCRIPTION, provenance=PROVENANCE,
    )
    db.commit()
    return node_id


def _settle(db, node_id: str) -> bool:
    settled = public.settle_user_step(
        db, node_id, evidence="Password field no longer on the page", provenance=PROVENANCE,
    )
    db.commit()
    return settled


def _prerequisites(db, node_id: str) -> list[str]:
    return [r["depends_on_node_id"] for r in db.execute(
        "SELECT depends_on_node_id FROM plan_node_dependencies WHERE node_id=?", (node_id,),
    ).fetchall()]


def _pane_reason(db, node_id: str) -> str | None:
    from plans import operator_projection

    entry = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    return {node["id"]: node for node in entry["nodes"]}[node_id]["status_reason"]


def test_open_makes_one_system_user_step_and_the_selected_task_waits_on_it(db):
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page", intent="Finish the browser sign-in")
    select(db, plan.id, mine.id)

    step = _open(db)

    node = row(db, step)
    assert (node["assignee"], node["title"], node["description"]) == ("user", TITLE, DESCRIPTION)
    assert node["intent_node_id"] == mine.id
    assert node["intent"] is None
    created = db.execute(
        "SELECT actor_id, provenance FROM plan_node_events WHERE node_id=? AND event='node_created'",
        (step,),
    ).fetchone()
    assert (created["actor_id"], created["provenance"]) == (None, PROVENANCE)
    assert _prerequisites(db, mine.id) == [step]
    assert open_attempt(db, mine.id) is None
    assert open_attempt(db, step) is None


def test_the_same_key_returns_the_same_step_and_a_new_key_a_new_one(db):
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page")
    select(db, plan.id, mine.id)

    first = _open(db)
    assert _open(db) == first
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE assignee='user'").fetchone()[0] == 1
    assert _settle(db, first) is True
    assert _open(db) == first  # the same visit stays answered, settled or not
    assert _open(db, "visit-2") != first


def test_no_selected_task_means_no_dependency_or_intent_reference(db):
    owner_plan(db)
    step = _open(db)
    assert row(db, step)["state"] == "pending"
    assert row(db, step)["intent_node_id"] is None
    assert db.execute("SELECT COUNT(*) FROM plan_node_dependencies").fetchone()[0] == 0


def test_multiple_selected_tasks_do_not_supply_a_user_step_intent(db, monkeypatch):
    plan = owner_plan(db)
    first = task(db, plan.id, "First selected task")
    second = task(db, plan.id, "Second selected task")
    monkeypatch.setattr(
        "plans.admission_attempts.current_for_executor",
        lambda *_args: [{"node_id": first.id}, {"node_id": second.id}],
    )

    step = _open(db)

    assert row(db, step)["intent_node_id"] is None


def test_settle_marks_it_done_by_the_system_and_wakes_the_waiting_task(db):
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page")
    select(db, plan.id, mine.id)
    step = _open(db)

    assert _settle(db, step) is True

    assert row(db, step)["state"] == "accepted"
    transition = db.execute(
        """SELECT actor_id, provenance, payload_json FROM plan_node_events
           WHERE node_id=? AND event='state_transition' AND new_state='accepted'""",
        (step,),
    ).fetchone()
    assert (transition["actor_id"], transition["provenance"]) == (None, PROVENANCE)
    assert json.loads(transition["payload_json"])["verified_by"] == f"system:{PROVENANCE}"
    assert _pane_reason(db, step) == "Marked by CORSIA"
    assert open_attempt(db, mine.id) is not None
    wake = db.execute(
        "SELECT id, content FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:work_selected:%'",
        ("owner",),
    ).fetchone()
    assert wake is not None
    assert wake["id"] == f"plan-wake:work_selected:{mine.id}:{row(db, mine.id)['revision']}"
    assert "Read the page is now your current task" in wake["content"]


def test_settle_on_a_paused_plan_settles_and_the_plan_stays_paused(db):
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page")
    select(db, plan.id, mine.id)
    step = _open(db)
    lifecycle.transition(
        plan.id, "paused", actor_id="user:test", provenance="test",
        expected_revision=service._plan_row(db, plan.id)["revision"], db=db,
    )
    db.commit()

    assert _settle(db, step) is True

    assert row(db, step)["state"] == "accepted"
    assert service._plan_row(db, plan.id)["state"] == "paused"
    assert open_attempt(db, mine.id) is None
    assert _prerequisites(db, mine.id) == [step]  # met, so the wake waits for resume

    lifecycle.transition(
        plan.id, "active", actor_id="user:test", provenance="test",
        expected_revision=service._plan_row(db, plan.id)["revision"], db=db,
    )
    db.commit()
    assert open_attempt(db, mine.id) is not None


@pytest.mark.parametrize("by", [OWNER, HUMAN], ids=["agent", "person"])
def test_settle_ends_a_pause_on_the_step_even_the_persons_own(db, by):
    """The person did the step, so their own earlier pause on it has nothing left to stop."""
    plan = owner_plan(db)
    step = _open(db)
    service.transition_node(step, "active", db=db)
    db.commit()
    pause(db, plan.id, step, actor=by)

    assert _settle(db, step) is True
    assert row(db, step)["state"] == "accepted"


def test_settle_withdraws_the_questions_drafted_from_the_step(db):
    from plans import decision_lifecycle, decision_projection, decisions

    plan = owner_plan(db)
    step = _open(db)
    common = {"actor": OWNER, "provenance": "test.system_user_step", "db": db}
    drafted = decisions.draft(
        plan.id, source_node_id=step, mutation_id="decision-draft",
        title="Which account do we sign in with?", context="Two accounts are in play.",
        tradeoff="One is personal, one is shared.", recommendation="A",
        rationale="The shared one is meant for this.", other_enabled=True,
        options=[
            {"id": "A", "label": "Shared account", "detail": "Used by the team."},
            {"id": "B", "label": "Personal account", "detail": "Yours only."},
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

    assert _settle(db, step) is True

    assert decision_projection.read(raised["id"], db=db)["state"] == "invalidated"
    reasons = [
        json.loads(event["payload_json"])["reason"]
        for event in db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='decision_invalidated'",
            (raised["node_id"],),
        ).fetchall()
    ]
    assert reasons == [decision_lifecycle.SOURCE_DONE]


@pytest.mark.parametrize("ending", ["closed", "settled", "archived"])
def test_settle_after_the_step_ended_does_nothing(db, ending):
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page")
    select(db, plan.id, mine.id)
    step = _open(db)
    if ending == "closed":
        plan_update(db, plan.id, "close_node", {**close_params(db, "close_node", step), "reason": "Not now"}, actor=HUMAN)
    elif ending == "settled":
        _settle(db, step)
    else:
        plan_update(db, plan.id, "close_node", {**close_params(db, "close_node", step), "reason": "Not now"}, actor=HUMAN)
        plan_update(db, plan.id, "archive_node", {"node_id": step, "expected_revision": row(db, step)["revision"]}, actor=HUMAN)
    before = row(db, step)["state"], row(db, step)["revision"]

    assert _settle(db, step) is False
    assert (row(db, step)["state"], row(db, step)["revision"]) == before


def test_declining_the_step_releases_the_waiting_task(db):
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page")
    select(db, plan.id, mine.id)
    step = _open(db)

    plan_update(db, plan.id, "close_node", {**close_params(db, "close_node", step), "reason": "Not now"}, actor=HUMAN)

    assert row(db, step)["state"] == "closed"
    assert open_attempt(db, mine.id) is not None


def test_an_empty_key_is_refused(db):
    owner_plan(db)
    with pytest.raises(service.PlanInputError):
        public.open_user_step(db, "owner", key=" ", title=TITLE, description=DESCRIPTION, provenance=PROVENANCE)


def test_settle_refuses_a_step_the_system_did_not_open(db):
    """An agent's own task, or a person step an agent asked for, is never settled by the system."""
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page")
    asked = service.add_node(
        plan.id, kind="task", title="Sign the form", acceptance_criteria="Signed",
        assignee="user", actor_id=OWNER.audit_id, provenance="test", db=db,
    )
    db.commit()
    for node_id in (mine.id, asked.id):
        with pytest.raises(service.PlanInputError):
            _settle(db, node_id)
        assert row(db, node_id)["state"] not in {"done", "accepted"}


def test_every_event_on_the_step_is_system_attributed(db):
    """The step reads as the system's, never the agent's: no agent actor anywhere on it."""
    plan = owner_plan(db)
    mine = task(db, plan.id, "Read the page")
    select(db, plan.id, mine.id, actor=OWNER)
    step = _open(db)
    _settle(db, step)
    actors = {r["actor_id"] for r in db.execute(
        "SELECT actor_id FROM plan_node_events WHERE node_id=?", (step,),
    ).fetchall()}
    assert actors == {None}


def test_the_key_and_opened_reads_are_primary_key_seeks_on_the_plans_receipts(db):
    """Each open and settle reads a bounded key range, however long the plan's history."""
    from plans import system_user_steps

    plan = owner_plan(db)
    step = _open(db)
    open_plan = " ".join(r["detail"] for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT result_json FROM plan_mutation_receipts WHERE plan_id=? AND mutation_id=?",
        (plan.id, "k"),
    ).fetchall())
    settle_plan = " ".join(r["detail"] for r in db.execute(
        "EXPLAIN QUERY PLAN " + system_user_steps._OPENED_SQL,
        system_user_steps._opened_params(plan.id, step),
    ).fetchall())
    for detail in (open_plan, settle_plan):
        assert "USING PRIMARY KEY" in detail and "mutation_id" in detail, detail
