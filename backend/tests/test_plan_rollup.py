"""P4a red-first tests for scope hierarchy, revisions, and internal rollup."""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

import infra.config as config
from infra.schema import SCHEMA_SQL
import plans.artifact_links as artifact_links
import plans.hierarchy as hierarchy
import plans.rollup as rollup
import plans.service as service
from plans.boundary import PlanActor, mutate
from tests.plan_review_helpers import submit_review_evidence


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for project_id in ("project-1", "project-2"):
        connection.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project_id, project_id, "t", "t"),
        )
    for thread_id, project_id, spawned_by in (
        ("conductor-a", "project-1", None),
        ("conductor-b", "project-1", None),
        ("parent", "project-1", None),
        ("child", "project-1", "parent"),
        ("other-project", "project-2", None),
        ("cross-project-child", "project-2", "parent"),
        ("workspace-thread", None, None),
    ):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,created_at,updated_at)
               VALUES (?,'user-1',?,?,'t','t')""",
            (thread_id, project_id, spawned_by),
        )
    connection.execute(
        "UPDATE projects SET conductor_thread_id='conductor-a' WHERE id='project-1'"
    )
    connection.commit()
    yield connection
    connection.close()


def _task(plan_id: str, title: str, *, db, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )


def _accept(node_id: str, *, db) -> None:
    service.transition_node(node_id, "active", db=db)
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id, "accepted", verification_result="passed",
        verified_by="reviewer", db=db,
    )


def _plan_data(projected):
    return asdict(projected)


def test_scope_owner_validation_and_conductor_replacement_invariance(db):
    with pytest.raises(service.PlanError, match="unknown project"):
        service.ensure_plan("project_scope", "missing", db=db)
    with pytest.raises(service.PlanError, match="selected workspace"):
        service.ensure_plan("workspace_scope", "wrong-workspace", db=db)

    parent_plan = service.ensure_plan("thread", "parent", db=db)
    _task(parent_plan.id, "Visible", db=db)
    before = _plan_data(rollup.rollup_scope("project_scope", "project-1", db=db))

    db.execute(
        "UPDATE projects SET conductor_thread_id='conductor-b' WHERE id='project-1'"
    )
    db.commit()
    after = _plan_data(rollup.rollup_scope("project_scope", "project-1", db=db))

    assert after == before
    assert after["scope"] == {"kind": "project_scope", "id": "project-1"}
    assert "conductor_session_id" not in repr(after)


def test_public_scope_mutation_is_denied_before_receipt_or_event(db):
    project_plan = service.ensure_plan("project_scope", "project-1", db=db)
    actor = PlanActor(None, "user-1", "project-1", kind="user")

    with pytest.raises(PermissionError, match="scope plan mutation"):
        mutate(
            project_plan.id, "add",
            {"kind": "task", "title": "Denied", "acceptance_criteria": "Never"},
            actor=actor, provenance="test", mutation_id="scope-public", db=db,
        )

    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE plan_id=?",
        (project_plan.id,),
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM plan_nodes WHERE plan_id=?", (project_plan.id,),
    ).fetchone()[0] == 0


def test_aggregate_revision_covers_nodes_dependencies_and_links(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    initial = service._plan_row(db, plan.id)["revision"]
    prerequisite = _task(plan.id, "Prerequisite", db=db)
    after_nodes = service._plan_row(db, plan.id)["revision"]
    dependent = _task(plan.id, "Dependent", db=db)
    service.add_dependency(dependent.id, prerequisite.id, db=db)
    after_dependency = service._plan_row(db, plan.id)["revision"]
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,created_at,updated_at)
           VALUES ('artifact-1','test','document','doc-1','active','t','t')"""
    )
    artifact_links.link(
        dependent.id, "artifact-1", "input", actor_id="parent",
        provenance="test", source_event_id="external-1", db=db,
    )
    after_link = service._plan_row(db, plan.id)["revision"]

    assert initial < after_nodes < after_dependency < after_link


def test_rollup_hides_empty_archived_and_preserves_paused_material_liveness(db):
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    review = _task(parent_plan.id, "Review", db=db)
    service.transition_node(review.id, "active", db=db)
    service.transition_node(review.id, "review_requested", db=db)
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (parent_plan.id,))

    child_plan = service.ensure_plan("thread", "child", db=db)
    prerequisite = _task(child_plan.id, "Prerequisite", db=db)
    dependent = _task(child_plan.id, "Dependent", db=db)
    service.add_dependency(dependent.id, prerequisite.id, db=db)
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (child_plan.id,))
    archived = service.ensure_plan("thread", "other-project", db=db)
    _task(archived.id, "Archived", db=db)
    db.execute("UPDATE plans SET state='archived', archived_at='t' WHERE id=?", (archived.id,))
    db.commit()

    projection = rollup.rollup_scope("project_scope", "project-1", db=db)
    by_owner = {item.plan["owner_id"]: item for item in projection.plans}

    assert by_owner["parent"].plan["state"] == "paused"
    assert by_owner["parent"].progress["liveness"] == "needs-review"
    assert by_owner["child"].progress["liveness"] == "paused"
    assert "conductor-a" not in by_owner
    assert "conductor-b" not in by_owner
    assert "other-project" not in by_owner


def test_rollup_reports_stale_active_completion_cursor_and_descendants(db, monkeypatch):
    plan = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Phase", acceptance_criteria="Phase DOD", db=db,
    )
    track = service.add_node(
        plan.id, kind="track", title="Track", acceptance_criteria="Track DOD",
        parent_id=phase.id, db=db,
    )
    active = _task(plan.id, "Active", parent_id=track.id, db=db)
    prerequisite = _task(plan.id, "Prerequisite", db=db)
    blocked = _task(plan.id, "Blocked", parent_id=track.id, db=db)
    service.add_dependency(blocked.id, prerequisite.id, db=db)
    service.transition_node(active.id, "active", db=db)
    monkeypatch.setattr(
        "plans.projections._now_utc",
        lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
        raising=False,
    )
    _accept(prerequisite.id, db=db)
    service.add_dependency(blocked.id, active.id, db=db)
    db.commit()

    projected = rollup.rollup_thread(plan.id, db=db)
    nodes = {node["id"]: node for node in projected.nodes}

    # Adding a dependency is an eligibility change, so it reconciles the plan
    # in the same transaction: the eligible, unselected Active task takes the
    # empty executor slot and the plan is advancing, not blocked.
    selected = db.execute(
        "SELECT node_id FROM plan_node_attempts WHERE plan_id=? AND released_at IS NULL",
        (plan.id,),
    ).fetchall()
    assert [row["node_id"] for row in selected] == [active.id]
    # The accepted top-level prerequisite folds out of the counter.
    assert projected.progress == {"done": 0, "total": 2, "open": 2, "liveness": "advancing"}
    assert nodes[prerequisite.id]["completed_event_id"]
    assert nodes[prerequisite.id]["completed_at"]
    assert nodes[active.id]["state"] == "active"


def test_phase_only_tree_is_not_projected_as_empty(db):
    plan = service.ensure_plan("thread", "workspace-thread", db=db)
    service.add_node(
        plan.id, kind="phase", title="Planning",
        acceptance_criteria="Deliver resulting plan expansion",
        checkpoint_exception=True, db=db,
    )

    projected = rollup.rollup_thread(plan.id, db=db)

    assert projected.progress["total"] == 1
    assert len(projected.nodes) == 1
    assert projected.nodes[0]["depth"] == 0
    assert projected.nodes[0]["path"] == []


def test_workspace_scope_uses_selected_workspace_identity(db):
    projected = rollup.rollup_scope(
        "workspace_scope", config.WORKSPACE_SELECTION.workspace_id, db=db,
    )

    assert projected.scope == {
        "kind": "workspace_scope",
        "id": config.WORKSPACE_SELECTION.workspace_id,
    }
    assert projected.plans == []


def _parent_of(owner_kind: str, owner_id: str, *, db) -> tuple[str, str] | None:
    plan = service.ensure_plan(owner_kind, owner_id, db=db)
    row = db.execute(
        "SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan.parent_plan_id,),
    ).fetchone()
    return None if row is None else (row["owner_kind"], row["owner_id"])


def test_plan_parent_is_derived_from_owner_identity_and_never_refused(db):
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("UPDATE threads SET spawned_by='deleted-spawner' WHERE id='conductor-b'")
    workspace = ("workspace_scope", config.WORKSPACE_SELECTION.workspace_id)

    assert _parent_of(*workspace, db=db) is None
    assert _parent_of("project_scope", "project-1", db=db) == workspace
    assert _parent_of("thread", "child", db=db) == ("thread", "parent")
    assert _parent_of("thread", "cross-project-child", db=db) == (
        "project_scope", "project-2",
    )
    assert _parent_of("thread", "conductor-b", db=db) == ("project_scope", "project-1")
    assert _parent_of("thread", "workspace-thread", db=db) == workspace


def test_rehome_follows_project_moves_and_a_deleted_spawner(db):
    parent = service.ensure_plan("thread", "parent", db=db)
    child = service.ensure_plan("thread", "child", db=db)
    project_1 = service.ensure_plan("project_scope", "project-1", db=db)
    project_2 = service.ensure_plan("project_scope", "project-2", db=db)

    def parents():
        return tuple(
            service._plan_row(db, plan.id)["parent_plan_id"] for plan in (parent, child)
        )

    db.execute("UPDATE threads SET project_id='project-2' WHERE id='parent'")
    hierarchy.rehome("parent", db=db)
    assert parents() == (project_2.id, project_1.id)

    db.execute("UPDATE threads SET project_id='project-2' WHERE id='child'")
    hierarchy.rehome("child", db=db)
    assert parents() == (project_2.id, parent.id)

    revision = service._plan_row(db, child.id)["revision"]
    hierarchy.rehome("child", db=db)
    assert service._plan_row(db, child.id)["revision"] == revision

    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM threads WHERE id='parent'")
    hierarchy.rehome("parent", db=db)
    assert parents() == (project_2.id, project_2.id)


def test_set_parent_refuses_a_cycle_and_a_self_edge(db):
    parent = service.ensure_plan("thread", "parent", db=db)
    child = service.ensure_plan("thread", "child", db=db)

    with pytest.raises(service.PlanError, match="cycle"):
        hierarchy._set_parent(parent.id, child.id, db=db)
    with pytest.raises(service.PlanError, match="itself"):
        hierarchy._set_parent(child.id, child.id, db=db)
    assert service._plan_row(db, child.id)["parent_plan_id"] == parent.id
