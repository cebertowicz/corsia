"""Only malformed input, missing authority and invariant breaks refuse the whole close.

These are knowable before any write or are internal defects, so nothing moves:
the closed task keeps its state and selection, the named task is untouched and
no receipt is written. A malformed `next_node_id` is a caller-fixable input
error; `next_node_id` on a non-closing operation is an unexpected parameter; an
actor without authority over the plan is refused; and a named start whose
result diverges from its own classification rolls back as an internal error,
never as a retryable plan conflict.
"""

from __future__ import annotations

import pytest

from plans import service
from plans.boundary import PlanActor
from tests.plan_close_next_helpers import (
    break_named_start,
    close,
    open_attempt,
    owner_plan,
    plan_update,
    receipt_count,
    revision,
    row,
    select,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture


def _snapshot(db, *node_ids: str) -> tuple:
    """Return every durable fact a refused close must leave unchanged."""
    nodes = tuple(
        (row(db, node_id)["state"], row(db, node_id)["revision"],
         None if open_attempt(db, node_id) is None else open_attempt(db, node_id)["id"])
        for node_id in node_ids
    )
    attempts = db.execute(
        "SELECT id,released_at FROM plan_node_attempts ORDER BY id",
    ).fetchall()
    return nodes, tuple(tuple(item) for item in attempts), receipt_count(db)


def _scene(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    task_b = task(db, plan.id, "Task B")
    select(db, plan.id, task_a.id)
    return plan, task_a, task_b


@pytest.mark.parametrize("value", ["", 5, None, ["node"]], ids=["empty", "int", "null", "list"])
def test_a_malformed_next_node_id_refuses_the_whole_close_as_input(db, value):
    plan, task_a, task_b = _scene(db)
    before = _snapshot(db, task_a.id, task_b.id)

    with pytest.raises(service.PlanInputError) as refusal:
        close(db, plan.id, "request_review", task_a.id, next_node_id=value)

    assert "next_node_id" in str(refusal.value)
    assert "unexpected" not in str(refusal.value)
    assert db.in_transaction is False
    assert _snapshot(db, task_a.id, task_b.id) == before


_NON_CLOSING = {
    "reject": {"feedback": "Needs another pass"},
    "start": {},
    "activate": {},
    "resume_node": {"hold_id": "hold-unknown"},
    "resolve_blocker": {"hold_id": "hold-unknown"},
    "resolve_watch": {"watch_id": "watch-unknown"},
    "edit": {"title": "Task A renamed"},
}


@pytest.mark.parametrize("operation", sorted(_NON_CLOSING))
def test_next_node_id_on_a_non_closing_operation_is_an_unexpected_parameter(db, operation):
    plan, task_a, task_b = _scene(db)
    params = {
        "node_id": task_a.id, "expected_revision": revision(db, task_a.id),
        "next_node_id": task_b.id, **_NON_CLOSING[operation],
    }
    before = _snapshot(db, task_a.id, task_b.id)

    with pytest.raises(
        service.PlanInputError, match=f"unexpected parameters for {operation}: next_node_id",
    ):
        plan_update(db, plan.id, operation, params)

    assert _snapshot(db, task_a.id, task_b.id) == before


# Thread-plan review is already owner-only, so for thread actors this refusal
# comes from the primary owner rule; the named start's own authority check is
# defense in depth with no separately reachable thread-actor case.
def test_an_actor_without_authority_over_the_plan_is_refused_before_anything_moves(db):
    plan = owner_plan(db)
    first = task(db, plan.id, "Task one")
    second = task(db, plan.id, "Task two")
    select(db, plan.id, first.id)
    before = _snapshot(db, first.id, second.id)

    with pytest.raises(PermissionError):
        close(
            db, plan.id, "request_review", first.id, next_node_id=second.id,
            actor=PlanActor("worker", "user", None),
        )

    assert _snapshot(db, first.id, second.id) == before


def test_a_named_start_that_diverges_from_its_classification_rolls_back_as_internal(
    db, monkeypatch,
):
    plan, task_a, task_b = _scene(db)
    before = _snapshot(db, task_a.id, task_b.id)
    break_named_start(monkeypatch)

    with pytest.raises(RuntimeError) as failure:
        close(db, plan.id, "request_review", task_a.id, next_node_id=task_b.id)

    assert not isinstance(failure.value, service.PlanError)
    assert db.in_transaction is False
    assert _snapshot(db, task_a.id, task_b.id) == before
