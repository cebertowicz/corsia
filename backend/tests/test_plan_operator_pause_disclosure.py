"""Condition precedence, not every stored cause, decides default disclosure.

PLANS-CANONICAL-STATE-MATRIX display matrix: "Paused | Collapsed by default"
and "Blocked | Blocked ancestor path open". Only review and human action
outrank pause, so they stay current. A paused node that also carries a blocker
displays Paused, keeps "Blocked by X" as a detail cause, and reopens its
blocked path when the pause clears. Terminal lifecycle outranks everything, so
a failed node that still carries a blocker never opens its path either.
"""

from __future__ import annotations

import sqlite3

from infra.schema import SCHEMA_SQL
import plans.boundary as boundary
from plans import (
    admission, holds, mutations, operator_projection, review_evidence, service,
)
from plans.boundary import PlanActor

HUMAN = PlanActor(None, "user", None, kind="user")
PROVENANCE = "test.operator.pause_disclosure"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('owner','user','project','Plan owner','t','t')"""
    )
    db.commit()
    return db


def _phase(plan_id: str, title: str, *, db):
    return service.add_node(
        plan_id, kind="phase", title=title,
        acceptance_criteria=f"{title} is proven", db=db,
    )


def _task(plan_id: str, title: str, *, parent, db):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", parent_id=parent.id, db=db,
    )


def _activate(node, *, db) -> None:
    with service._write(db):
        admission.activate(
            node.id, expected_revision=node.revision, actor_id="owner",
            provenance=PROVENANCE, db=db,
        )


def _request_review(node, *, db) -> None:
    with service._write(db):
        service._transition(
            db, node.id, "active", actor_id="owner", provenance=PROVENANCE,
            verification_result=None, verified_by=None,
        )
        review_evidence.submit(
            node.id, evidence="Focused checks passed.",
            expected_revision=service._node_row(db, node.id)["revision"],
            actor_id="owner", provenance=PROVENANCE, db=db,
        )


def _plan_lifecycle(plan_id: str, operation: str, *, db) -> None:
    revision = service._plan_row(db, plan_id)["revision"]
    boundary.mutate(
        plan_id, operation, {"expected_revision": revision}, actor=HUMAN,
        provenance=PROVENANCE, mutation_id=f"{operation}-{revision}", db=db,
    )


def _node_hold(node_id: str, change, *, db, **params) -> None:
    revision = service._node_row(db, node_id)["revision"]
    with service._write(db):
        change(
            node_id, expected_revision=revision, actor_id="owner",
            provenance=PROVENANCE, db=db, **params,
        )


def _read(db) -> tuple[dict, dict[str, dict]]:
    projected = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    return projected, {node["id"]: node for node in projected["nodes"]}


def test_whole_plan_pause_keeps_only_review_obligations_current():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        build = _phase(plan.id, "Build", db=db)
        implement = _task(plan.id, "Implement", parent=build, db=db)
        polish = _task(plan.id, "Polish", parent=build, db=db)
        review = _phase(plan.id, "Review", db=db)
        harvest = _task(plan.id, "Harvest", parent=review, db=db)
        later = _phase(plan.id, "Later", db=db)
        ship = _task(plan.id, "Ship", parent=later, db=db)
        service.add_dependency(ship.id, polish.id, db=db)
        _activate(implement, db=db)
        _request_review(harvest, db=db)

        active, nodes = _read(db)
        assert {implement.id, harvest.id, ship.id} <= set(active["current_node_ids"])
        assert nodes[ship.id]["attention_path"] == [later.id, ship.id]

        _plan_lifecycle(plan.id, "pause", db=db)
        paused, nodes = _read(db)
        shipped = nodes[ship.id]
        assert paused["current_node_ids"] == [harvest.id]
        assert paused["next_node_ids"] == []
        assert shipped["execution"]["condition"] == "paused"
        assert "dependency" in {
            cause["reason_key"] for cause in shipped["execution"]["causes"]
        }
        assert list(shipped["unmet_dependencies"]) == [polish.id]
        assert shipped["attention_path"] == []
        assert paused["progress"]["distribution"]["paused"] == 3

        _plan_lifecycle(plan.id, "resume", db=db)
        resumed, nodes = _read(db)
        assert ship.id in resumed["current_node_ids"]
        assert nodes[ship.id]["attention_path"] == [later.id, ship.id]
    finally:
        db.close()


def test_node_pause_is_not_current_and_keeps_its_blocker_as_detail():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        build = _phase(plan.id, "Build", db=db)
        implement = _task(plan.id, "Implement", parent=build, db=db)
        verify = _task(plan.id, "Verify", parent=build, db=db)
        _activate(implement, db=db)
        _node_hold(implement.id, holds.pause, reason="Waiting on a product call", db=db)
        # The pause freed the owner's one slot in this plan, so Verify runs next.
        _activate(verify, db=db)

        _node_hold(
            verify.id, holds.block, reason_key="security_signoff",
            reason="Security sign-off", owner_kind="user", owner_id=None, db=db,
        )
        _node_hold(verify.id, holds.pause, reason="Hold for the release window", db=db)

        projected, nodes = _read(db)
        verifying = nodes[verify.id]
        assert nodes[implement.id]["execution"]["condition"] == "paused"
        assert verifying["execution"]["condition"] == "paused"
        assert implement.id not in projected["current_node_ids"]
        assert verify.id not in projected["current_node_ids"]
        assert "blocked" in {cause["kind"] for cause in verifying["execution"]["causes"]}
        assert verifying["held_by"] == "user"
        assert verifying["attention_path"] == []

        pause_hold = next(
            cause for cause in verifying["execution"]["causes"]
            if cause["kind"] == "paused"
        )
        _node_hold(
            verify.id, holds.clear, hold_id=pause_hold["id"],
            expected_kind="paused", db=db,
        )
        resumed, nodes = _read(db)
        assert nodes[verify.id]["execution"]["condition"] == "blocked"
        assert verify.id in resumed["current_node_ids"]
        assert nodes[verify.id]["attention_path"] == [build.id, verify.id]
    finally:
        db.close()


def test_failed_node_keeps_its_blocker_as_detail_without_opening_its_path():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        build = _phase(plan.id, "Build", db=db)
        prepare = _task(plan.id, "Prepare", parent=build, db=db)
        ship = _task(plan.id, "Ship", parent=build, db=db)
        service.add_dependency(ship.id, prepare.id, db=db)
        mutations.fail_node(prepare.id, actor_id="owner", provenance=PROVENANCE, db=db)

        blocked, nodes = _read(db)
        assert nodes[ship.id]["execution"]["condition"] == "blocked"
        assert ship.id in blocked["current_node_ids"]
        assert nodes[ship.id]["attention_path"] == [build.id, ship.id]

        # Failing does not clear dependency or hold causes, so the blocker
        # survives on the terminal row as detail only.
        mutations.fail_node(ship.id, actor_id="owner", provenance=PROVENANCE, db=db)
        failed, nodes = _read(db)
        shipped = nodes[ship.id]
        assert shipped["execution"]["condition"] == "terminal"
        assert shipped["display"] == {"label_key": "finished", "tone_key": "failed"}
        assert "dependency_failed" in {
            cause["reason_key"] for cause in shipped["execution"]["causes"]
        }
        # A terminal node waits on nothing; the failed prerequisite stays a cause.
        assert list(shipped["unmet_dependencies"]) == []
        assert shipped["attention_path"] == []
        assert failed["current_node_ids"] == []
    finally:
        db.close()
