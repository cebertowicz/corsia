"""A delegated child's model calls are billed to its task while it holds no selection.

A child's calls bind to its own valid selection. Two stretches of its life had
none, so a woken Turn's calls fell unbound and the parent's task cost missed
them: a step the child started and then parked behind an armed watch, and the
time before the child's own first step (or for a child that never plans). The
fallback is usage-only: no Turn snapshot, selection or admission state changes.
Work already returned for review or finished stays unbound on purpose.
"""

from __future__ import annotations

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from plans import admission, public, selection_validity, service
from plans.boundary import PlanActor, mutate
from plans.delegate_attach import Step
from plans.usage_calls import binding_at_call_start, record_call_usage
from tests.external_harness_usage_helpers import persist_turn

MEASURED = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
PARENT, CHILD = "parent-1", "child-1"
PARENT_ROW = {"id": PARENT, "user_id": "u-test", "project_id": "proj-1"}


def _threads(db) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,title,created_at,updated_at) "
        "VALUES (?,'u-test','proj-1','claude-code/opus','Parent','t','t')", (PARENT,),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,spawned_by,model,title,created_at,updated_at) "
        "VALUES (?,'u-test','proj-1',?,'claude-code/sonnet','Child','t','t')", (CHILD, PARENT),
    )
    db.commit()


def _started(db, plan_id: str, owner: str, title: str):
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    revision = service.get_node(node.id, db=db).revision
    admission.start(node.id, expected_revision=revision, actor_id=owner, provenance="test", db=db)
    db.commit()
    return service.get_node(node.id, db=db)


def _handed_step(db):
    """The parent's started step, handed to the child through the real attach route."""
    _threads(db)
    node = _started(db, service.ensure_plan("thread", PARENT, db=db).id, PARENT, "Parent step")
    step = Step(str(node.plan_id), str(node.id), "Parent step")
    public.hand_over_delegated_step(step, CHILD, PARENT_ROW, db=db)
    db.commit()
    return node


def _call(key: str, turn: str = "turn-1") -> None:
    record_call_usage(
        CHILD, "test-model", MEASURED, source="conversation",
        turn_id=turn, usage_key=key, binding=binding_at_call_start(CHILD),
    )


def _rows(db, turn: str = "turn-1") -> list[tuple]:
    return [
        (r["plan_node_id"], r["plan_usage_role"], r["plan_commitment_id"], r["plan_attempt_id"])
        for r in db.execute(
            "SELECT * FROM llm_usage WHERE turn_id=? ORDER BY created_at,rowid", (turn,),
        ).fetchall()
    ]


def _turn_binding(db, turn: str = "turn-1"):
    return db.execute("SELECT plan_node_id FROM active_turns WHERE id=?", (turn,)).fetchone()[0]


def _parent_attempt(db, node_id: str) -> str:
    return db.execute(
        "SELECT id FROM plan_node_attempts WHERE node_id=?", (node_id,),
    ).fetchone()["id"]


def _park_own_step(db, plan_id: str, node_id: str) -> None:
    mutate(
        plan_id, "arm_watch",
        {
            "node_id": node_id, "expected_revision": service.get_node(node_id, db=db).revision,
            "condition": {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
            "expected_event": "proof.ready",
        },
        actor=PlanActor(CHILD, "u-test", "proj-1"), provenance="test",
        mutation_id=f"park-{node_id}", db=db,
    )
    db.commit()


def test_a_child_with_no_plan_bills_its_calls_to_the_step_it_was_handed(deleg_db):
    parent_step = _handed_step(deleg_db)
    persist_turn(CHILD, "turn-1")

    _call("provider-call:1")

    assert _rows(deleg_db) == [(
        parent_step.id, "delegation", parent_step.commitment_id,
        _parent_attempt(deleg_db, parent_step.id),
    )]
    assert _turn_binding(deleg_db) is None  # usage only: the Turn snapshot is untouched


def test_a_parked_own_step_bills_its_calls_woken_by_a_non_plan_input(deleg_db):
    parent_step = _handed_step(deleg_db)
    child_plan = service.ensure_plan("thread", CHILD, db=deleg_db)
    own = _started(deleg_db, child_plan.id, CHILD, "Child step")
    attempt = deleg_db.execute(
        "SELECT id FROM plan_node_attempts WHERE node_id=?", (own.id,),
    ).fetchone()["id"]
    _park_own_step(deleg_db, child_plan.id, own.id)
    persist_turn(CHILD, "turn-1")
    assert selection_validity.attributed_selection(deleg_db, CHILD) is None

    _call("provider-call:1")

    assert _rows(deleg_db) == [(own.id, "delegation", own.commitment_id, attempt)]
    assert own.commitment_id == parent_step.commitment_id
    assert _turn_binding(deleg_db) is None
    assert service.get_node(own.id, db=deleg_db).state == "active"


def test_a_child_that_has_returned_its_step_for_review_stays_unbound(deleg_db):
    _handed_step(deleg_db)
    child_plan = service.ensure_plan("thread", CHILD, db=deleg_db)
    own = _started(deleg_db, child_plan.id, CHILD, "Child step")
    service.transition_node(own.id, "review_requested", db=deleg_db)
    deleg_db.commit()
    persist_turn(CHILD, "turn-1")

    _call("provider-call:1")

    assert _rows(deleg_db) == [(None, None, None, None)]


def test_a_handed_step_already_returned_for_review_stays_unbound(deleg_db):
    parent_step = _handed_step(deleg_db)
    service.transition_node(parent_step.id, "review_requested", db=deleg_db)
    deleg_db.commit()
    persist_turn(CHILD, "turn-1")

    _call("provider-call:1")

    assert _rows(deleg_db) == [(None, None, None, None)]


def test_two_parked_active_steps_are_not_guessed_between(deleg_db):
    _handed_step(deleg_db)
    child_plan = service.ensure_plan("thread", CHILD, db=deleg_db)
    first = _started(deleg_db, child_plan.id, CHILD, "First")
    _park_own_step(deleg_db, child_plan.id, first.id)
    second = _started(deleg_db, child_plan.id, CHILD, "Second")
    _park_own_step(deleg_db, child_plan.id, second.id)
    persist_turn(CHILD, "turn-1")

    _call("provider-call:1")

    assert _rows(deleg_db) == [(None, None, None, None)]


def test_a_selected_own_step_still_takes_the_calls(deleg_db):
    _handed_step(deleg_db)
    child_plan = service.ensure_plan("thread", CHILD, db=deleg_db)
    own = _started(deleg_db, child_plan.id, CHILD, "Child step")
    persist_turn(CHILD, "turn-1")

    _call("provider-call:1")

    assert [row[0] for row in _rows(deleg_db)] == [own.id]


def test_calls_recorded_before_the_hand_over_are_billed_when_it_commits(deleg_db):
    """The worker's first Turn can run before the attach: its unbound calls follow it."""
    _threads(deleg_db)
    parent_step = _started(
        deleg_db, service.ensure_plan("thread", PARENT, db=deleg_db).id, PARENT, "Parent step",
    )
    persist_turn(CHILD, "turn-1")
    _call("provider-call:1")
    assert _rows(deleg_db) == [(None, None, None, None)]

    public.hand_over_delegated_step(
        Step(str(parent_step.plan_id), str(parent_step.id), "Parent step"),
        CHILD, PARENT_ROW, db=deleg_db,
    )
    deleg_db.commit()
    _call("provider-call:2")

    expected = (
        parent_step.id, "delegation", parent_step.commitment_id,
        _parent_attempt(deleg_db, parent_step.id),
    )
    assert _rows(deleg_db) == [expected, expected]
    assert _turn_binding(deleg_db) is None
