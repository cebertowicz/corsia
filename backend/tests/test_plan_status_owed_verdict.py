"""The digest says when the plan's owner owes a verdict (#3116).

A returned step put up for review is the owner's to judge. With nothing else
open, "nothing ready: open work is moving or in review" read as an idle plan
while the owner was doing the review. A step waiting on someone else's verdict
-- a delegated worker's last open step, which its parent judges (matrix
section G) -- keeps that wording.
"""

from __future__ import annotations

import sqlite3

from infra.schema import SCHEMA_SQL
from plans import mutations, service, status_context
from plans.boundary import PlanActor
from tests.plan_review_helpers import submit_review_evidence

OWNER = PlanActor("owner", "user", "project")
WAITING_WORDS = "nothing ready: open work is moving or in review"


def _db(*, spawned_by: str | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','P','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
        "VALUES ('owner','user','project','Plan owner','t','t')"
    )
    if spawned_by is not None:
        db.execute(
            "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
            "VALUES (?,'user','project','Parent','t','t')",
            (spawned_by,),
        )
        db.execute("UPDATE threads SET spawned_by=? WHERE id='owner'", (spawned_by,))
    db.commit()
    return db


def _in_review(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Returned step", acceptance_criteria="Step is proven", db=db,
    )
    service.transition_node(node.id, "active", actor_id="owner", db=db)
    submit_review_evidence(node.id, db=db, evidence="Proof", actor_id="owner")
    db.commit()
    return plan, node


def test_the_owner_is_told_it_owes_the_verdict_on_its_review_requested_step():
    db = _db()
    try:
        plan, node = _in_review(db)
        thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)
        assert thin["open_step"]["id"] == node.id
        assert thin["open_step"]["state"] == "review_requested"
        assert thin["note"] == (
            "nothing ready: you owe the verdict on open_step; accept it,"
            " accept_with_follow_up for what is left, or reject it"
        )
    finally:
        db.close()


def _delegate_owner(db) -> None:
    """Attach the owner as the parent's worker on an open delegated step."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    step = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed", spawn_kind="claude-code", db=db,
    )
    service.transition_node(step.id, "active", db=db)
    mutations.attach_spawned_thread(step.id, "owner", db=db)
    db.commit()


def test_a_step_waiting_on_someones_else_verdict_keeps_the_review_wording():
    db = _db(spawned_by="parent")
    try:
        plan, node = _in_review(db)
        _delegate_owner(db)
        thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)
        assert thin["open_step"]["id"] == node.id
        assert thin["note"] == WAITING_WORDS
    finally:
        db.close()


def test_a_spawned_thread_nobody_delegated_to_judges_its_own_step():
    """Spawned alone is not delegated: with no open step naming it, the plan is its own."""
    db = _db(spawned_by="parent")
    try:
        plan, _node = _in_review(db)
        thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)
        assert thin["note"].startswith("nothing ready: you owe the verdict")
    finally:
        db.close()
