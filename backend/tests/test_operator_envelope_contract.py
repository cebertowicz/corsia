"""The frontend decodes the envelope this projection builds, not one it imagines (#1768).

``frontend/src/lib/api/plansDecode.ts`` is the pane's trust boundary for the
operator envelope. Its own fixtures were written by hand, so a field the server
never sent (``attention.external_reviews``) could be required and ship. This
test builds a real envelope through ``operator_projection`` and holds it to the
committed fixture the frontend's decoder test reads: when the projection's
shape changes, this fails until the fixture is regenerated with
``CORSIA_WRITE_CONTRACT_FIXTURES=1``, and the decoder test then runs on it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from infra.schema import SCHEMA_SQL
from plans import admission, mutations, operator_projection, review_evidence, service
from plans import watch_operations
from plans.boundary import PlanActor
from tests.plan_scene import _artifact

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend/src/lib/__tests__/fixtures/operator-envelope.contract.json"
)
_PROVENANCE = "test.operator.contract"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    for thread_id, spawned_by, title in (
        ("owner", None, "Plan owner"), ("worker", "owner", "Worker"),
    ):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,title,created_at,updated_at)
               VALUES (?,'user','project',?,?,'t','t')""",
            (thread_id, spawned_by, title),
        )
    db.commit()
    return db


def _task(plan_id: str, title: str, *, db, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", db=db, **kwargs,
    )


def _envelope(db) -> dict:
    """One thread read whose rows carry every optional fact the pane renders."""
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Delivery",
        acceptance_criteria="Delivered", db=db,
    )
    running = _task(plan.id, "Running", db=db, parent_id=phase.id)
    waiting = _task(plan.id, "Waiting", db=db, parent_id=phase.id)
    review = _task(plan.id, "Review", db=db)
    delegated = _task(plan.id, "Delegated", db=db, spawn_kind="claude-code")
    _task(service.ensure_plan("thread", "worker", db=db).id, "Child step", db=db)
    pull_request = _artifact(db, external_id="owner/repo#1", owner="owner")
    with service._write(db):
        for node in (waiting, running):
            admission.activate(
                node.id, expected_revision=node.revision, actor_id="owner",
                provenance=_PROVENANCE, db=db,
            )
            if node is waiting:
                watch_operations.arm(
                    plan.id,
                    {
                        "node_id": waiting.id,
                        "condition": {"type": "event", "value": "ci.complete"},
                        "artifact_id": pull_request,
                        "expected_event": "ci.complete",
                    },
                    PlanActor("owner", "user", "project"),
                    db,
                )
        service._transition(
            db, review.id, "active", actor_id="owner", provenance=_PROVENANCE,
            verification_result=None, verified_by=None,
        )
        review_evidence.submit(
            review.id, evidence="Focused checks passed.",
            expected_revision=service._node_row(db, review.id)["revision"],
            actor_id="owner", provenance=_PROVENANCE, db=db,
        )
    service.transition_node(delegated.id, "active", db=db)
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    db.commit()
    payload = operator_projection.read_thread(
        "owner", actor=PlanActor(None, "user", None, kind="user"), db=db,
    )
    return json.loads(json.dumps(payload))


def _shape(value):
    """The envelope's structure with its values erased: keys, types, nulls."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return sorted({json.dumps(_shape(item), sort_keys=True) for item in value})
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "null" if value is None else type(value).__name__


def test_the_fixture_the_frontend_decodes_has_the_projections_shape():
    db = _db()
    try:
        envelope = _envelope(db)
    finally:
        db.close()
    nodes = envelope["plans"][0]["nodes"]
    # The fixture is only a contract if it carries the facts the pane branches on.
    assert any(node["execution"]["attempts"] for node in nodes)
    assert any(node["execution"]["causes"] for node in nodes)
    assert any(node["evidence"] for node in nodes)
    assert any(node["commitment_cost"] for node in nodes)
    assert any(node["child"] for node in nodes)
    assert any(node["branch_progress"] for node in nodes)
    if os.environ.get("CORSIA_WRITE_CONTRACT_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(envelope, indent=1, sort_keys=True) + "\n")
    committed = json.loads(FIXTURE.read_text())
    assert _shape(envelope) == _shape(committed), (
        "The operator envelope changed shape. Regenerate the frontend fixture with "
        "CORSIA_WRITE_CONTRACT_FIXTURES=1 and make plansDecode.ts accept it."
    )
