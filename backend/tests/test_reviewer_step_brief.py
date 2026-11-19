"""A worker handed a reviewer step starts from the step, not the author's framing.

The brief is written from the plan: what was asked (the run's intent), the
step's check and its instruction. The author's message follows as a labelled
note for finding the work. Only a step another agent checks gets it, and only
on its first hand-over.
"""

from __future__ import annotations

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from plans import node_creation, service
from plans.delegate_attach import AUTHOR_NOTE, choose_for_send
from tests.test_delegate_task_plan_step import (  # noqa: F401
    _PARENT, _first_input, _receipt, _seed_parent, spawned,
)

pytestmark = pytest.mark.asyncio

INTENT = "Release notes a customer can follow without help."


def _step(db, plan_id: str, *, by: str, description: str | None = "Read each section as a customer.") -> str:
    phase = service.add_node(
        plan_id, kind="phase", title="Ship the notes", acceptance_criteria="Shipped", intent=INTENT, db=db,
    )
    node = node_creation.create_node(
        db, plan_id, kind="task", title="Review the notes", acceptance_criteria="Every step in the notes works as written",
        parent_id=phase.id, order_key=None, description=description, workflow_ref="ship",
        workflow_version="v1", workflow_step="review", difficulty="routine", size="unknown",
        checkpoint_exception=False, assignee=None, spawn_kind=None, watchdog_budget_seconds=None,
        follow_up_of=None, actor_id=None, provenance="plans.workflow_run", node_id=service._id(),
        intent_node_id=phase.id, workflow_by=by,
    )
    service.transition_node(node.id, "active", db=db)
    db.commit()
    return node.id


async def test_a_reviewer_step_brief_comes_from_the_plan_and_the_author_text_is_a_note(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    node_id = _step(deleg_db, plan_id, by="reviewer")

    worker = (await _receipt(plan_node_id=node_id))["thread_id"]

    text = _first_input(deleg_db, worker)
    head, note = text.split(AUTHOR_NOTE.split("{")[0])
    assert 'reviewer for the workflow step "Review the notes"' in head
    assert f"What was asked: {INTENT}" in head
    assert "The step's check, as of hand-over: Every step in the notes works as written" in head
    assert "How to review: Read each section as a customer." in head
    assert "do the work" not in head
    # The system's pointer to the live read sits outside the author's text.
    assert f"Your plan task: \"Review the notes\" ({node_id})" in head
    assert head.index("How to review") < head.index("Your plan task")
    assert note.startswith("do the work")
    assert "Your plan task" not in note


async def test_a_reviewer_step_without_an_instruction_has_no_how_line(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    node_id = _step(deleg_db, plan_id, by="reviewer", description=None)

    worker = (await _receipt(plan_node_id=node_id))["thread_id"]

    assert "How to review" not in _first_input(deleg_db, worker)


async def test_an_ordinary_step_keeps_the_authors_message_as_is(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    node_id = _step(deleg_db, plan_id, by="agent")

    worker = (await _receipt(plan_node_id=node_id))["thread_id"]

    assert _first_input(deleg_db, worker).startswith("do the work\n\nYour plan task:")


async def test_a_further_send_to_the_same_reviewer_is_not_reframed(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    node_id = _step(deleg_db, plan_id, by="reviewer")
    worker = (await _receipt(plan_node_id=node_id))["thread_id"]

    step = choose_for_send(_PARENT, worker, node_id, db=deleg_db)

    assert step is not None and step.already_attached and step.brief is None
    assert step.lead("one more file", "Your plan task: x") == "one more file\n\nYour plan task: x"
