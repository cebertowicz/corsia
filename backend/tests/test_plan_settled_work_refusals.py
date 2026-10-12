"""Finished work holds nothing open beneath it, and no write path can make it (#731).

The stored aggregates read a settled node as a finished fold row only if nothing
under it is open. Three writers could still build "a finished parent over open
work"; each now refuses, with a message that says what to do instead:

* ``add_node`` under a parent that is accepted, failed or closed;
* a ``failed`` transition (the typed op and the generic ``_transition``) over
  open descendants;
* a follow-up, which lands beside its source, or at the source's nearest
  unsettled ancestor when the source's parent has settled since.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import closure, mutations, rejection, service

PARENT_REFUSAL = "node parent is"
FAIL_REFUSAL = "cannot be failed over them"


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    connection.execute(
        "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at)"
        " VALUES ('owner','user','project','Plan owner','t','t')"
    )
    connection.commit()
    yield connection
    connection.close()


def _task(db, plan_id: str, title: str, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} is proven",
        db=db, **kwargs,
    )


def _phase(db, plan_id: str, title: str, **kwargs):
    return service.add_node(
        plan_id, kind="phase", title=title, acceptance_criteria=f"{title} is proven",
        db=db, **kwargs,
    )


def _count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0]


def _settled_over_open_work(db) -> list[str]:
    """Settled nodes that have an open descendant: the shape no writer may leave."""
    return [
        row["id"] for row in db.execute(
            """WITH RECURSIVE below(root, id) AS (
                   SELECT id, id FROM plan_nodes
                   UNION ALL
                   SELECT b.root, c.id FROM below b JOIN plan_nodes c ON c.parent_id=b.id)
               SELECT DISTINCT n.id FROM plan_nodes n
                 JOIN below b ON b.root=n.id AND b.id<>n.id
                 JOIN plan_nodes d ON d.id=b.id
                WHERE n.state IN ('accepted','accepted_with_followup','failed','closed')
                  AND d.state IN ('pending','active','review_requested')
                  AND d.archived_at IS NULL"""
        )
    ]


def _review(db, node_id: str):
    service.transition_node(node_id, "active", db=db)
    return mutations.request_review(node_id, evidence="proven", actor_id="owner", db=db)


@pytest.mark.parametrize("settle", ["failed", "closed", "accepted"])
def test_a_settled_parent_takes_no_new_child(db, settle):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    if settle == "closed":
        closure.close_node(parent.id, reason="not wanted", provenance="test", db=db)
    elif settle == "failed":
        service.transition_node(parent.id, "active", db=db)
        service.transition_node(parent.id, "failed", db=db)
    else:
        _review(db, parent.id)
        service.transition_node(
            parent.id, "accepted", verification_result="passed", verified_by="test", db=db,
        )
    before = _count(db)

    with pytest.raises(service.PlanError, match=PARENT_REFUSAL):
        _task(db, plan.id, "Late child", parent_id=parent.id)

    assert _count(db) == before
    assert _settled_over_open_work(db) == []


def test_an_open_parent_still_takes_a_child(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    service.transition_node(parent.id, "active", db=db)

    child = _task(db, plan.id, "Child", parent_id=parent.id)

    assert child.parent_id == parent.id


def test_a_node_cannot_fail_over_an_open_descendant(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    service.transition_node(parent.id, "active", db=db)
    _task(db, plan.id, "Open child", parent_id=parent.id)

    with pytest.raises(service.PlanError, match=FAIL_REFUSAL):
        service.transition_node(parent.id, "failed", db=db)

    assert service._node_row(db, parent.id)["state"] == "active"
    assert _settled_over_open_work(db) == []


def test_the_generic_transition_refuses_the_same_failure(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    service.transition_node(parent.id, "active", db=db)
    deep_parent = _phase(db, plan.id, "Middle", parent_id=parent.id)
    _task(db, plan.id, "Deep", parent_id=deep_parent.id)

    with pytest.raises(service.PlanError, match=FAIL_REFUSAL):
        service._transition(
            db, parent.id, "failed", actor_id=None, provenance="test",
            verification_result=None, verified_by=None,
        )

    assert service._node_row(db, parent.id)["state"] == "active"


def test_a_node_fails_once_everything_beneath_it_is_settled(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    service.transition_node(parent.id, "active", db=db)
    child = _task(db, plan.id, "Child", parent_id=parent.id)
    closure.close_node(child.id, reason="not wanted", provenance="test", db=db)

    service.transition_node(parent.id, "failed", db=db)

    assert service._node_row(db, parent.id)["state"] == "failed"
    assert _settled_over_open_work(db) == []


def test_a_follow_up_of_finished_work_skips_a_parent_that_settled_since(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    grand = _phase(db, plan.id, "Grandparent")
    service.transition_node(grand.id, "active", db=db)
    parent = _phase(db, plan.id, "Parent", parent_id=grand.id)
    source = _task(db, plan.id, "Source", parent_id=parent.id)
    service.transition_node(source.id, "active", db=db)
    service.transition_node(source.id, "failed", db=db)
    closure.close_node(parent.id, reason="done elsewhere", provenance="test", db=db)

    follow_up = service.add_follow_up(
        source.id, title="Residual", acceptance_criteria="Residual proven", db=db,
    )

    assert follow_up.parent_id == grand.id
    assert _settled_over_open_work(db) == []


def test_a_follow_up_with_no_unsettled_ancestor_goes_to_the_top_level(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    source = _task(db, plan.id, "Source", parent_id=parent.id)
    service.transition_node(source.id, "active", db=db)
    service.transition_node(source.id, "failed", db=db)
    closure.close_node(parent.id, reason="done elsewhere", provenance="test", db=db)

    follow_up = service.add_follow_up(
        source.id, title="Residual", acceptance_criteria="Residual proven", db=db,
    )

    assert follow_up.parent_id is None
    assert _settled_over_open_work(db) == []


def test_a_follow_up_of_live_work_lands_beside_its_source(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    service.transition_node(parent.id, "active", db=db)
    source = _task(db, plan.id, "Source", parent_id=parent.id)

    follow_up = service.add_follow_up(
        source.id, title="Residual", acceptance_criteria="Residual proven", db=db,
    )

    assert follow_up.parent_id == parent.id


def test_accepting_with_a_follow_up_keeps_it_beside_the_source(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    service.transition_node(parent.id, "active", db=db)
    source = _task(db, plan.id, "Source", parent_id=parent.id)
    _review(db, source.id)

    accepted, follow_up = service.accept_with_follow_up(
        source.id, title="Residual", acceptance_criteria="Residual proven",
        verification_result="passed", verified_by="test", db=db,
    )

    assert accepted.state == "accepted_with_followup"
    assert follow_up.parent_id == parent.id
    assert _settled_over_open_work(db) == []


def test_sending_a_submission_back_puts_its_revision_beside_it(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _phase(db, plan.id, "Parent")
    service.transition_node(parent.id, "active", db=db)
    source = _task(db, plan.id, "Source", parent_id=parent.id)
    _review(db, source.id)

    rejected, revision, replay = rejection.reject_submission(
        source.id, feedback="needs the edge case", db=db,
    )

    assert replay is False
    assert rejected.state == "closed"
    assert revision.parent_id == parent.id
    assert _settled_over_open_work(db) == []
