"""Truthful review settlement and linked correction contract."""

from __future__ import annotations

import json
import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for thread_id, spawned_by in (
        ("owner", None),
        ("worker", "owner"),
        ("child", "owner"),
    ):
        connection.execute(
            "INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
            "VALUES (?,'user',?,'t','t')",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


def _actor() -> PlanActor:
    return PlanActor("owner", "user", None)


def _mutate(db, plan_id, operation, params, mutation_id):
    return mutate(
        plan_id,
        operation,
        params,
        actor=_actor(),
        provenance="test.plan_settlement",
        mutation_id=mutation_id,
        db=db,
    ).result


def _task(db, plan_id, title, **kwargs):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} is verified",
        db=db,
        **kwargs,
    )


def _activate(db, plan_id, node, key):
    return _mutate(
        db,
        plan_id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        key,
    )


def _review(db, plan_id, node, key, evidence="Focused proof passed"):
    return _mutate(
        db,
        plan_id,
        "request_review",
        {
            "node_id": node["id"] if isinstance(node, dict) else node.id,
            "evidence": evidence,
            "expected_revision": (
                node["revision"] if isinstance(node, dict) else node.revision
            ),
        },
        key,
    )


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
    )


def test_review_requires_evidence_and_acceptance_refuses_unfinished_children(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id,
        kind="phase",
        title="Required phase",
        acceptance_criteria="Every required child is accepted",
        db=db,
    )
    child = _task(db, plan.id, "Required child", parent_id=phase.id)

    active = _activate(db, plan.id, phase, "activate-phase")
    assert active["node"]["state"] == "active"
    assert active["admission"]["selection"] is None

    with pytest.raises(service.PlanInputError, match="evidence"):
        _mutate(
            db,
            plan.id,
            "request_review",
            {
                "node_id": phase.id,
                "evidence": "   ",
                "expected_revision": active["node"]["revision"],
            },
            "review-without-evidence",
        )

    reviewed = _review(
        db, plan.id, active["node"], "review-phase", evidence="Phase proof",
    )
    evidence_event = db.execute(
        """SELECT payload_json FROM plan_node_events
           WHERE node_id=? AND event='review_evidence_submitted'
           ORDER BY rowid DESC LIMIT 1""",
        (phase.id,),
    ).fetchone()
    assert json.loads(evidence_event["payload_json"]) == {
        "artifact_ids": [],
        "evidence": "Phase proof",
        "requirements_revision": 1,
    }

    with pytest.raises(service.PlanError, match="child work"):
        _accept(db, plan.id, reviewed["node"], "accept-unfinished-phase")

    assert service.get_node(phase.id, db=db).state == "review_requested"
    assert service.get_node(child.id, db=db).state == "pending"


def test_acceptance_refuses_a_dependency_added_after_review(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    target = _task(db, plan.id, "Reviewed target")
    prerequisite = _task(db, plan.id, "Required proof")
    active = _activate(db, plan.id, target, "activate-target")
    reviewed = _review(db, plan.id, active["node"], "review-target")

    blocked = _mutate(
        db,
        plan.id,
        "add_dependency",
        {
            "node_id": target.id,
            "depends_on_node_id": prerequisite.id,
            "expected_revision": reviewed["node"]["revision"],
        },
        "add-required-proof",
    )
    assert blocked["admission"]["condition"] == "review"
    assert blocked["admission"]["causes"][0]["reason_key"] == "dependency"

    with pytest.raises(service.PlanError, match="required work"):
        _accept(db, plan.id, blocked["node"], "accept-blocked-target")


def test_attached_child_work_inherits_commitment_and_blocks_parent_acceptance(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _task(
        db, plan.id, "Delegated obligation", spawn_kind="delegated",
    )
    active = _activate(db, plan.id, parent, "activate-parent")
    child_plan = service.ensure_plan("thread", "child", db=db)
    existing = _task(db, child_plan.id, "Existing child work")

    attached = _mutate(
        db,
        plan.id,
        "attach_spawned_thread",
        {
            "node_id": parent.id,
            "spawned_thread_id": "child",
            "expected_revision": active["node"]["revision"],
        },
        "attach-child",
    )
    existing = service.get_node(existing.id, db=db)
    future = _task(db, child_plan.id, "Future child work")
    assert existing.commitment_id == parent.commitment_id
    assert future.commitment_id == parent.commitment_id

    reviewed = _review(db, plan.id, attached["node"], "review-parent")
    # Only a worker still working holds the verdict back; an idle one is
    # stood down by it (#1866).
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-child','child',0,'native/test','running','t0','boot')"""
    )
    with pytest.raises(service.PlanError, match="delegated child work"):
        _accept(db, plan.id, reviewed["node"], "accept-open-child")


def test_add_correction_preserves_terminal_source_and_blocks_affected_work(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(db, plan.id, "Original obligation")
    active_source = _activate(db, plan.id, source, "activate-source")
    reviewed_source = _review(
        db, plan.id, active_source["node"], "review-source",
    )
    accepted_source = _accept(
        db, plan.id, reviewed_source["node"], "accept-source",
    )["node"]

    affected = _task(db, plan.id, "Affected recommendation")
    active_affected = _activate(db, plan.id, affected, "activate-affected")
    corrected = _mutate(
        db,
        plan.id,
        "add_correction",
        {
            "node_id": source.id,
            "title": "Correct the obligation",
            "acceptance_criteria": "Corrected proof passes",
            "affected_nodes": [
                {
                    "node_id": affected.id,
                    "expected_revision": active_affected["node"]["revision"],
                }
            ],
            "expected_revision": accepted_source["revision"],
        },
        "add-correction",
    )

    correction = corrected["correction"]
    assert service.get_node(source.id, db=db).state == "accepted"
    assert correction["state"] == "pending"
    assert correction["follow_up_of"] == source.id
    assert correction["commitment_id"] == source.commitment_id
    assert service.get_node(affected.id, db=db).unmet_dependencies
    assert db.execute(
        """SELECT 1 FROM plan_node_dependencies
           WHERE node_id=? AND depends_on_node_id=?""",
        (affected.id, correction["id"]),
    ).fetchone() is not None

    before = db.execute(
        "SELECT COUNT(*) FROM plan_nodes WHERE follow_up_of=?", (source.id,),
    ).fetchone()[0]
    with pytest.raises(service.PlanError, match="unfinished"):
        _mutate(
            db,
            plan.id,
            "add_correction",
            {
                "node_id": source.id,
                "title": "Invalid correction",
                "acceptance_criteria": "Never created",
                "affected_nodes": [
                    {
                        "node_id": source.id,
                        "expected_revision": accepted_source["revision"],
                    }
                ],
                "expected_revision": accepted_source["revision"],
            },
            "invalid-terminal-affected",
        )
    assert db.execute(
        "SELECT COUNT(*) FROM plan_nodes WHERE follow_up_of=?", (source.id,),
    ).fetchone()[0] == before
