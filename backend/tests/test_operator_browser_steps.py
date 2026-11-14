"""The person's plan read marks each open browser step with the thread whose shared browser it is in.

The chat chip and the plan row read ``browser_thread_id`` to open that browser in one press; any
other person step keeps opening its instructions or details.
"""

from __future__ import annotations

from api.operator_annotations import mark_browser_steps
from host_access.browser_steps import PasswordStep, PersonStep
from plans import operator_projection, public
from tests.plan_close_next_helpers import HUMAN, owner_plan, task
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture


def _open(db, thread_id: str, key: str, provenance: str) -> str:
    node_id = public.open_user_step(
        db, thread_id, key=key, title=f"Step {key}", description="On the shared browser.", provenance=provenance,
    )
    db.commit()
    return node_id


def _nodes(db, thread_id: str) -> dict[str, dict]:
    envelope = operator_projection.read_thread(thread_id, actor=HUMAN, db=db)
    mark_browser_steps(db, envelope)
    return {node["id"]: node for entry in envelope["plans"] for node in entry["nodes"]}


def test_password_and_person_steps_carry_their_own_thread(db):
    owner_plan(db)
    password = _open(db, "owner", "password_step:visit-1", PasswordStep.provenance)
    person = _open(db, "owner", "person_step:visit-2", PersonStep.provenance)

    nodes = _nodes(db, "owner")

    assert nodes[password]["browser_thread_id"] == "owner"
    assert nodes[person]["browser_thread_id"] == "owner"


def test_a_subagents_step_names_the_subagent_not_the_reader(db):
    step = _open(db, "worker", "password_step:visit-1", PasswordStep.provenance)

    assert _nodes(db, "worker")[step]["browser_thread_id"] == "worker"


def test_other_person_steps_and_agent_tasks_are_not_marked(db):
    plan = owner_plan(db)
    other = _open(db, "owner", "review:1", "plans.person_step_instructions")
    mine = task(db, plan.id, "Read the page")

    nodes = _nodes(db, "owner")

    assert "browser_thread_id" not in nodes[other]
    assert "browser_thread_id" not in nodes[mine.id]


def test_a_settled_step_is_no_longer_marked(db):
    owner_plan(db)
    step = _open(db, "owner", "password_step:visit-1", PasswordStep.provenance)
    assert public.settle_user_step(
        db, step, evidence="Password field no longer on the page", provenance=PasswordStep.provenance,
    )
    db.commit()

    node = _nodes(db, "owner").get(step)

    assert node is None or "browser_thread_id" not in node
