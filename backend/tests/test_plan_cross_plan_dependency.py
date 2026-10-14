"""A task may wait on another thread's task, and hears once when it settles (#1552).

R8-R12 of ``shared/planning/plans-read-another-plan-2026-09-24.md``: one lane
depends on the fix in another lane's plan. The edge is allowed only where the
caller could read that plan, the wait names whose plan it is, and the
prerequisite's own write tells the dependent's owner the outcome and moves
their plan -- one message, never to a thread that cannot read it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, cross_plan, dependencies, service
from plans.boundary import PlanActor, mutate

LANE = PlanActor("lane", "user", "project")
INFRA = PlanActor("infra", "user", "project")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    for project in ("project", "elsewhere"):
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project, project, "t", "t"),
        )
    for thread_id, title, project in (
        ("lane", "Plans", "project"),
        ("infra", "Infrastructure", "project"),
        ("stranger", "Stranger", "elsewhere"),
    ):
        db.execute(
            """INSERT INTO threads (id,user_id,project_id,title,model,created_at,updated_at)
               VALUES (?,'user',?,?,'test/model','t','t')""",
            (thread_id, project, title),
        )
    db.commit()
    return db


@pytest.fixture
def db():
    connection = _db()
    try:
        yield connection
    finally:
        connection.close()


def _update(db, plan_id: str, operation: str, params: dict, *, actor) -> dict:
    result = mutate(
        plan_id, operation, params, actor=actor, provenance="test.cross_plan",
        mutation_id=uuid.uuid4().hex, db=db,
    ).result
    db.commit()
    return result


def _revision(db, node_id: str) -> int:
    return int(service._node_row(db, node_id)["revision"])


def _task(db, plan_id: str, title: str, *, description: str | None = None, actor_id: str | None = None):
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done",
        description=description, actor_id=actor_id, provenance="test.cross_plan", db=db,
    )
    db.commit()
    return node


def _setup(db):
    """Infrastructure's fix, and the Plans task that waits on it."""
    infra = service.ensure_plan("thread", "infra", db=db)
    fix = _task(db, infra.id, "Fix the dev test")
    lane = service.ensure_plan("thread", "lane", db=db)
    merge = _task(db, lane.id, "Merge my PR")
    _update(db, lane.id, "add_dependency", {
        "node_id": merge.id, "depends_on_node_id": fix.id,
        "expected_revision": _revision(db, merge.id),
    }, actor=LANE)
    return infra, fix, lane, merge


def _submit(db, plan_id: str, node_id: str) -> None:
    admission.activate(
        node_id, expected_revision=_revision(db, node_id), actor_id="infra",
        provenance="test.cross_plan", db=db,
    )
    db.commit()
    _update(db, plan_id, "request_review", {
        "node_id": node_id, "evidence": "Fixed", "expected_revision": _revision(db, node_id),
    }, actor=INFRA)


def _wakes(db, thread_id: str = "lane") -> list[sqlite3.Row]:
    return db.execute(
        "SELECT id,content FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:%' "
        "ORDER BY input_order",
        (thread_id,),
    ).fetchall()


def _ready(db, node_id: str) -> bool:
    """Nothing holds the task back; the Turn the wake starts selects it at admission."""
    return admission.project(node_id, db=db)["condition"] == "ready"


def _cause(db, node_id: str) -> dict:
    return admission.project(node_id, db=db)["causes"][0]


def test_a_dependency_on_another_plan_names_whose_plan_it_waits_on(db):
    infra, fix, _lane, merge = _setup(db)

    cause = _cause(db, merge.id)
    assert cause["reason"] == "Waiting for Fix the dev test (Infrastructure's plan)"
    assert (cause["owner_kind"], cause["owner_id"]) == ("thread", "infra")
    # The edge lives on the dependent: the other plan is not written.
    assert _revision(db, fix.id) == fix.revision
    assert _wakes(db, "infra") == []


@pytest.mark.parametrize("target", ["unreadable", "missing"])
def test_a_plan_the_caller_cannot_read_is_refused_like_a_missing_task(db, target):
    lane = service.ensure_plan("thread", "lane", db=db)
    merge = _task(db, lane.id, "Merge my PR")
    stranger = service.ensure_plan("thread", "stranger", db=db)
    hidden = _task(db, stranger.id, "Private work")
    prerequisite = hidden.id if target == "unreadable" else uuid.uuid4().hex

    with pytest.raises(service.PlanError) as refused:
        _update(db, lane.id, "add_dependency", {
            "node_id": merge.id, "depends_on_node_id": prerequisite,
            "expected_revision": _revision(db, merge.id),
        }, actor=LANE)
    assert str(refused.value) == dependencies._DEPENDENCY_REFUSED


def test_an_existing_edge_can_be_removed_after_the_other_plan_moves_away(db):
    _infra, fix, lane, merge = _setup(db)
    db.execute("UPDATE threads SET project_id='elsewhere' WHERE id='infra'")
    db.commit()

    result = _update(db, lane.id, "remove_dependency", {
        "node_id": merge.id, "depends_on_node_id": fix.id,
        "expected_revision": _revision(db, merge.id),
    }, actor=LANE)

    assert result["removed"] is True
    assert _ready(db, merge.id)


def test_accepting_the_prerequisite_tells_the_dependent_owner_once_and_frees_it(db):
    infra, fix, _lane, merge = _setup(db)
    _submit(db, infra.id, fix.id)

    _update(db, infra.id, "accept", {
        "node_id": fix.id, "verification_result": "passed",
        "expected_revision": _revision(db, fix.id),
    }, actor=INFRA)

    wakes = _wakes(db)
    assert [row["id"] for row in wakes] == [
        f"plan-wake:prerequisite_settled:{merge.id}:{_revision(db, fix.id)}:{fix.id}:done",
    ]
    assert wakes[0]["content"].startswith(
        "[CORSIA plan] Fix the dev test in Infrastructure's plan is done; Merge my PR can go."
    )
    assert _ready(db, merge.id)


@pytest.mark.parametrize(("author", "carried"), [("infra", True), ("lane", False)])
def test_a_prerequisite_wake_carries_a_selected_handoff_description_once_per_plan(db, author, carried):
    infra = service.ensure_plan("thread", "infra", db=db)
    fix = _task(db, infra.id, "Fix the dev test")
    lane = service.ensure_plan("thread", "lane", db=db)
    merge = _task(
        db, lane.id, "Merge my PR", description="Merge after reading the release evidence.", actor_id=author,
    )
    follow_up = _task(db, lane.id, "Publish the release notes")
    for dependent in (merge, follow_up):
        _update(db, lane.id, "add_dependency", {
            "node_id": dependent.id, "depends_on_node_id": fix.id,
            "expected_revision": _revision(db, dependent.id),
        }, actor=LANE)
        admission.activate(
            dependent.id, expected_revision=_revision(db, dependent.id), actor_id="lane",
            provenance="test.cross_plan", db=db,
        )
    db.commit()

    _submit(db, infra.id, fix.id)
    _update(db, infra.id, "accept", {
        "node_id": fix.id, "verification_result": "passed",
        "expected_revision": _revision(db, fix.id),
    }, actor=INFRA)

    facts = [json.loads(row["content"].split("\n", 1)[1]) for row in _wakes(db)]
    assert len(facts) == 2
    expected = {
        "node_id": merge.id, "text": "Merge after reading the release evidence.", "cut": False,
    }
    if carried:
        assert facts[0]["selected_description"] == expected
        assert "selected_description" not in facts[1]
    else:
        assert all("selected_description" not in fact for fact in facts)


def test_a_failed_prerequisite_is_told_and_keeps_the_dependent_waiting_on_it(db):
    infra, fix, _lane, merge = _setup(db)

    _update(db, infra.id, "fail", {
        "node_id": fix.id, "expected_revision": _revision(db, fix.id),
    }, actor=INFRA)

    assert [row["id"].rsplit(":", 1)[1] for row in _wakes(db)] == ["failed"]
    assert "failed; Merge my PR cannot go until you remove that dependency" in _wakes(db)[0]["content"]
    cause = _cause(db, merge.id)
    assert cause["reason_key"] == "dependency_failed"
    assert cause["reason"].startswith("Fix the dev test (Infrastructure's plan) failed")
    assert not _ready(db, merge.id)


@pytest.mark.parametrize("operation", ["close_node", "archive_node"])
def test_a_prerequisite_that_leaves_before_it_was_done_is_told_once(db, operation):
    infra, fix, _lane, merge = _setup(db)
    params = {"node_id": fix.id, "expected_revision": _revision(db, fix.id)}
    if operation == "close_node":
        params["reason"] = "Not needed"

    _update(db, infra.id, operation, params, actor=INFRA)

    how = "closed" if operation == "close_node" else "archived"
    wakes = _wakes(db)
    assert [row["id"].rsplit(":", 1)[1] for row in wakes] == [how]
    assert f"in Infrastructure's plan was {how} before it was done" in wakes[0]["content"]
    assert _ready(db, merge.id)


def test_a_rejection_moves_the_wait_to_the_revision_and_tells_nobody(db):
    infra, fix, _lane, merge = _setup(db)
    _submit(db, infra.id, fix.id)

    _update(db, infra.id, "reject", {
        "node_id": fix.id, "feedback": "Still red", "expected_revision": _revision(db, fix.id),
    }, actor=INFRA)

    assert _wakes(db) == []
    assert _cause(db, merge.id)["reason"] == "Waiting for Fix the dev test (Infrastructure's plan)"
    assert _cause(db, merge.id)["dependency_node_id"] != fix.id


def test_a_same_plan_dependency_still_sends_no_wake(db):
    lane = service.ensure_plan("thread", "lane", db=db)
    first, second = _task(db, lane.id, "First"), _task(db, lane.id, "Second")
    _update(db, lane.id, "add_dependency", {
        "node_id": second.id, "depends_on_node_id": first.id,
        "expected_revision": _revision(db, second.id),
    }, actor=LANE)
    assert _cause(db, second.id)["reason"] == "Waiting for First"
    assert _cause(db, second.id)["owner_kind"] == "system"

    _update(db, lane.id, "fail", {
        "node_id": first.id, "expected_revision": _revision(db, first.id),
    }, actor=LANE)

    assert not [row for row in _wakes(db) if "prerequisite_settled" in row["id"]]


USER = PlanActor(None, "user", None, kind="user")


def _own_plan_pair(db):
    """Two tasks in the lane's own plan, the second waiting on the first."""
    lane = service.ensure_plan("thread", "lane", db=db)
    first, second = _task(db, lane.id, "First"), _task(db, lane.id, "Second")
    _update(db, lane.id, "add_dependency", {
        "node_id": second.id, "depends_on_node_id": first.id,
        "expected_revision": _revision(db, second.id),
    }, actor=LANE)
    return lane, first, second


def _settled(db) -> list[sqlite3.Row]:
    return [row for row in _wakes(db) if "prerequisite_settled" in row["id"]]


def test_a_same_plan_prerequisite_the_user_closes_wakes_its_owner_once(db):
    lane, first, second = _own_plan_pair(db)

    _update(db, lane.id, "close_node", {
        "node_id": first.id, "reason": "Superseded by the new design",
        "expected_revision": _revision(db, first.id),
    }, actor=USER)

    wakes = _settled(db)
    assert [row["id"] for row in wakes] == [
        f"plan-wake:prerequisite_settled:{second.id}:{_revision(db, first.id)}:{first.id}:closed",
    ]
    assert wakes[0]["content"].startswith(
        "[CORSIA plan] First was closed: Superseded by the new design. Second depended"
        " on it; that dependency is gone, so Second is ready. Go ahead with it,"
        " depend on its replacement instead, or close it."
    )
    # The same close told again is the same input: once per dependent,
    # prerequisite and outcome.
    cross_plan.prerequisite_removed(
        db, first.id, [], actor_id="user:user",
        provenance="test.cross_plan", own_dependents=[_dependent_row(db, second.id)],
    )
    assert len(_settled(db)) == 1
    # The dependent freed by the close is not announced a second time as a
    # selection; the closed task's own close wake travels in the same write.
    assert not [row for row in _wakes(db) if "work_selected" in row["id"]]


def test_a_same_plan_prerequisite_another_actor_fails_wakes_its_owner_once(db):
    _lane, first, second = _own_plan_pair(db)

    service.transition_node(first.id, "failed", actor_id="infra", provenance="test.cross_plan", db=db)
    db.commit()
    cross_plan.prerequisite_settled(db, first.id, actor_id="infra", provenance="test.cross_plan")

    wakes = _settled(db)
    assert [row["id"].rsplit(":", 1)[1] for row in wakes] == ["failed"]
    assert wakes[0]["content"].startswith(
        "[CORSIA plan] First failed, and Second depends on it. Second cannot go until"
        " you remove that dependency, depend on its replacement instead, or close it."
    )
    # The edge stays: the row keeps today's reason.
    assert _cause(db, second.id)["reason_key"] == "dependency_failed"


def test_a_same_plan_prerequisite_its_owner_closes_sends_no_wake(db):
    lane, first, _second = _own_plan_pair(db)

    _update(db, lane.id, "close_node", {
        "node_id": first.id, "reason": "Not needed",
        "expected_revision": _revision(db, first.id),
    }, actor=LANE)

    assert _settled(db) == []


def _dependent_row(db, node_id: str):
    return db.execute(
        "SELECT n.id,n.title,n.plan_id,pl.owner_id FROM plan_nodes n "
        "JOIN plans pl ON pl.id=n.plan_id WHERE n.id=?",
        (node_id,),
    ).fetchone()
