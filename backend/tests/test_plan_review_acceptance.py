"""Structural review evidence and acceptance-gate contract."""

from __future__ import annotations

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import artifact_links, service
from plans.boundary import PlanActor, mutate
from tests.plan_ask_helpers import with_actionable_user_ask


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('owner','user','t','t')"
    )
    connection.commit()
    yield connection
    connection.close()


def _mutate(db, plan_id, operation, params, key):
    actor = PlanActor("owner", "user", None)
    params = with_actionable_user_ask(db, plan_id, operation, params, actor)
    return mutate(
        plan_id,
        operation,
        params,
        actor=actor,
        provenance="test.plan_review_acceptance",
        mutation_id=key,
        db=db,
    ).result


def _task(db, plan_id, title):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} is verified",
        db=db,
    )


def _activate(db, plan_id, node, key):
    return _mutate(
        db,
        plan_id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        key,
    )["node"]


def _review(db, plan_id, node, key, *, artifact_ids=None):
    params = {
        "node_id": node["id"],
        "evidence": "Focused proof passed",
        "expected_revision": node["revision"],
    }
    if artifact_ids is not None:
        params["artifact_ids"] = artifact_ids
    return _mutate(db, plan_id, "request_review", params, key)["node"]


def _accept(db, plan_id, node, key):
    return _mutate(
        db,
        plan_id,
        "accept",
        {
            "node_id": node["id"],
            "verification_result": "passed",
            "expected_revision": node["revision"],
        },
        key,
    )["node"]


def test_acceptance_requires_evidence_for_the_current_requirements(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Current requirements")
    active = _activate(db, plan.id, node, "activate-current")
    reviewed = _review(db, plan.id, active, "review-current")

    edited = _mutate(
        db,
        plan.id,
        "edit",
        {
            "node_id": node.id,
            "acceptance_criteria": "Updated requirements are verified",
            "expected_revision": reviewed["revision"],
        },
        "edit-requirements",
    )["node"]
    assert edited["requirements_revision"] == 2
    with pytest.raises(service.PlanError, match="current requirements"):
        _accept(db, plan.id, edited, "accept-stale-evidence")

    rereviewed = _review(db, plan.id, edited, "review-updated")
    assert _accept(db, plan.id, rereviewed, "accept-updated")["state"] == "accepted"


def test_a_blocking_hold_stops_review_before_acceptance_has_to(db):
    """A live stop is refused at the review transition, not just at acceptance.

    Requesting review used to be reachable while a hold was in force, which
    let an agent move its own node out of the gated states and regain its
    tools while the user's stop was still recorded. Holds require an active
    task, so refusing here makes "in review and held" unreachable through the
    boundary; `review_verification` keeps its own hold check as defence in
    depth for any future path that does not come through this one.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    blocked_node = _task(db, plan.id, "Blocked review")
    active = _activate(db, plan.id, blocked_node, "activate-blocked")
    blocked = _mutate(
        db,
        plan.id,
        "block_node",
        {
            "node_id": blocked_node.id,
            "reason_key": "blocking_finding",
            "reason": "A blocking finding remains",
            # Only the user's own blocker is a hold; any other is a wait.
            "owner_kind": "user",
            "owner_id": None,
            "expected_revision": active["revision"],
        },
        "block-review",
    )["node"]

    with pytest.raises(service.PlanError, match="blocking findings"):
        _review(db, plan.id, blocked, "review-blocked")

    # The stop held: the node never left the state the gate can see.
    assert service.get_node(blocked_node.id, db=db).state == "active"

    watched_node = _task(db, plan.id, "Watched review")
    active = _activate(db, plan.id, watched_node, "activate-watched")
    watched = _mutate(
        db,
        plan.id,
        "arm_watch",
        {
            "node_id": watched_node.id,
            "condition": {
                "type": "deadline",
                "value": "2099-01-01T00:00:00+00:00",
            },
            "expected_event": "External proof arrives",
            "expected_revision": active["revision"],
        },
        "arm-review-watch",
    )["node"]
    # A watch is a wait, not a stop: review is reachable, and acceptance
    # settles the node's own armed wait through the canonical teardown path.
    reviewed = _review(db, plan.id, watched, "review-watched")
    _accept(db, plan.id, reviewed, "accept-watched")
    watch = db.execute(
        "SELECT status FROM expected_event_watches WHERE plan_node_id=?",
        (watched_node.id,),
    ).fetchone()
    assert watch["status"] == "resolved"


def test_review_artifacts_are_this_threads_own_active_results(db):
    """An artifact nobody owns is not this thread's to attach, and is named."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Artifact proof")
    active = _activate(db, plan.id, node, "activate-artifact")
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,created_at,updated_at)
           VALUES ('artifact-1','test','document','proof','active','t','t')"""
    )
    db.commit()

    with pytest.raises(service.PlanInputError, match="not owned: artifact-1"):
        _review(db, plan.id, active, "review-unlinked", artifact_ids=["artifact-1"])
    artifact_links.link(
        node.id,
        "artifact-1",
        "result",
        actor_id="owner",
        provenance="test.plan_review_acceptance",
        db=db,
    )
    active = service.get_node(node.id, db=db)
    reviewed = _review(
        db,
        plan.id,
        {"id": active.id, "revision": active.revision},
        "review-linked",
        artifact_ids=["artifact-1"],
    )

    db.execute("UPDATE artifact_identities SET state='archived' WHERE id='artifact-1'")
    db.commit()
    with pytest.raises(service.PlanError, match="unavailable result artifact"):
        _accept(db, plan.id, reviewed, "accept-archived-artifact")
    db.execute("UPDATE artifact_identities SET state='active' WHERE id='artifact-1'")
    db.commit()
    assert _accept(db, plan.id, reviewed, "accept-active-artifact")["state"] == "accepted"


def test_review_cannot_be_requested_while_the_plan_is_paused(db):
    """A pause is the user's stop; one self-service mutation must not lift it.

    Requesting review moves the node out of the states the tool gate can see,
    so without this an agent could regain every work tool while the pause was
    still recorded — making "pause stops further dispatch" unenforceable.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Work")
    active = _activate(db, plan.id, node, "activate-paused-review")
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (plan.id,))

    with pytest.raises(service.PlanError, match="active plan"):
        _review(db, plan.id, active, "review-paused")

    assert service.get_node(node.id, db=db).state == "active"
