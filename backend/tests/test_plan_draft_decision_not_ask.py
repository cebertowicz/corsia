"""A decision still in draft is not an ask (#3401).

Drafting creates the decision's user-assigned review node at once, but the
person can answer only a raised decision. Until the raise, Activity must not
list that node as a task waiting on the person, and a raise attempted too
early must name the step that is missing and who takes it.
"""

from __future__ import annotations

import pytest

import plans.decisions as decisions
import plans.service as service
from doc_feedback_helpers import seed_thread
from infra.database import get_db, write_transaction
from plans import activity_rows
from plans.boundary import PlanActor
from plans.current import read_current_status

_FIELDS = {
    "title": "Which port should the dev server use?",
    "context": "Two services want port 3001.",
    "tradeoff": "Moving one breaks bookmarks; sharing breaks both.",
    "recommendation": "A",
    "rationale": "A keeps the person's bookmarks working.",
    "options": [
        {"id": "A", "label": "Move the test server", "detail": "Port 3002."},
        {"id": "B", "label": "Move the dev server", "detail": "Port 3003."},
    ],
    "other_enabled": True,
}


def _actor(thread_id: str) -> PlanActor:
    return PlanActor(thread_id, "owner-token", None)


def _task_node_ids(db) -> set[str]:
    return {
        row["detail"]["node_id"]
        for row in activity_rows.rows(db, "owner-token", [])
        if row["kind"] == "task"
    }


def _seed_child(db, child: str, parent: str) -> None:
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (parent, child))


def _draft_result(db, owner: str, mutation: str) -> dict:
    with write_transaction(db, scope="draft") as conn:
        plan = service.ensure_plan("thread", owner, db=conn)
        source = service.add_node(
            plan.id, kind="task", title="Pick the port", acceptance_criteria="Port picked",
            actor_id=owner, provenance="test", db=conn,
        )
    return decisions.draft(
        plan.id, source_node_id=source.id, mutation_id=mutation,
        actor=_actor(owner), provenance="tool.plans.decision", db=db, **_FIELDS,
    ).result


def _draft(db, owner: str, mutation: str) -> dict:
    return _draft_result(db, owner, mutation)["decision"]


def _condition(db, owner: str, node_id: str) -> dict:
    detail = read_current_status(actor=_actor(owner), node_id=node_id, db=db)
    assert detail is not None
    return detail["execution"]


def _step(db, operation: str, decision: dict, actor: str, mutation: str) -> dict:
    return getattr(decisions, operation)(
        decision["id"], expected_revision=decision["revision"], mutation_id=mutation,
        actor=_actor(actor), provenance="tool.plans.decision", db=db,
    ).result["decision"]


def test_a_draft_decision_is_not_a_task_in_activity_until_it_is_raised(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        decision = _draft(db, "owner", "draft-1")
        assert decision["node_id"] not in _task_node_ids(db)

        reviewed = _step(db, "parent_review", decision, "owner", "review-1")
        assert decision["node_id"] not in _task_node_ids(db)

        _step(db, "raise_to_user", reviewed, "owner", "raise-1")
        # Raised, the person answers it as a decision card, never as a bare task.
        assert decision["node_id"] not in _task_node_ids(db)
    finally:
        db.close()


def test_a_draft_review_task_waits_on_the_agent_in_the_plan_not_on_the_person(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        decision = _draft(db, "owner", "draft-4")
        execution = _condition(db, "owner", decision["node_id"])
        assert execution["condition"] == "waiting"
        cause = execution["causes"][0]
        assert cause["reason_key"] == "decision_not_raised"
        assert cause["reason"] == "Not raised yet: the agent reviews it first"
        assert (cause["owner_kind"], cause["owner_id"]) == ("thread", "owner")

        reviewed = _step(db, "parent_review", decision, "owner", "review-4")
        assert _condition(db, "owner", decision["node_id"])["condition"] == "waiting"

        _step(db, "raise_to_user", reviewed, "owner", "raise-4")
        raised = _condition(db, "owner", decision["node_id"])
        assert all(c["reason_key"] != "decision_not_raised" for c in raised["causes"])
    finally:
        db.close()


def test_a_delegated_draft_review_task_waits_on_the_parent(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "parent")
        seed_thread(db, "child")
        _seed_child(db, "child", "parent")
        decision = _draft(db, "child", "draft-5")
        cause = _condition(db, "child", decision["node_id"])["causes"][0]
        assert (cause["reason_key"], cause["owner_id"]) == ("decision_not_raised", "parent")
        assert decision["node_id"] not in _task_node_ids(db)
    finally:
        db.close()


def test_the_draft_result_says_the_person_cannot_see_it_yet_and_what_comes_next(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "parent")
        seed_thread(db, "child")
        _seed_child(db, "child", "parent")
        own = _draft_result(db, "owner", "draft-6")["next"]
        assert own == decisions.DRAFT_NEXT_OWN
        assert "can't see" in own and "parent_review" in own
        delegated = _draft_result(db, "child", "draft-7")["next"]
        assert delegated == decisions.DRAFT_NEXT_PARENT
    finally:
        db.close()


def test_a_delegated_author_is_told_its_parent_reviews_and_raises(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "parent")
        seed_thread(db, "child")
        _seed_child(db, "child", "parent")
        decision = _draft(db, "child", "draft-8")
        for operation in ("raise_to_user", "parent_review"):
            with pytest.raises(service.PlanError) as refused:
                _step(db, operation, decision, "child", f"{operation}-8")
            assert "delegated this work to you" in str(refused.value)
    finally:
        db.close()


def test_an_ordinary_person_task_still_shows(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        with write_transaction(db, scope="person_task") as conn:
            plan = service.ensure_plan("thread", "owner", db=conn)
            task = service.add_node(
                plan.id, kind="task", title="Sign in", acceptance_criteria="Signed in",
                assignee="user", actor_id=None, provenance="test", db=conn,
            )
        assert task.id in _task_node_ids(db)
    finally:
        db.close()


def test_raising_a_draft_names_the_missing_review_step_for_root_work(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        decision = _draft(db, "owner", "draft-2")
        with pytest.raises(service.PlanError) as refused:
            _step(db, "raise_to_user", decision, "owner", "raise-2")
        message = str(refused.value)
        assert "parent_review" in message
        assert "You are its reviewer" in message
    finally:
        db.close()


def test_raising_a_draft_names_the_parent_thread_that_reviews_it(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "parent")
        seed_thread(db, "child")
        _seed_child(db, "child", "parent")
        decision = _draft(db, "child", "draft-3")
        with pytest.raises(service.PlanError) as refused:
            _step(db, "raise_to_user", decision, "parent", "raise-3")
        message = str(refused.value)
        assert "parent_review" in message
        assert "You are its reviewer" in message
    finally:
        db.close()
