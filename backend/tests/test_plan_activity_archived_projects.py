"""Activity rollups skip archived projects and reduce only the plans they can show."""
from __future__ import annotations

import plans.hierarchy as hierarchy
import plans.service as service
from plans import activity_facts, activity_projection, operator_projection, wait_causes
from tests.test_plan_activity_index import _actor, _seed, _task, db  # noqa: F401


def _archived_project_thread(db) -> str:
    db.execute(
        "INSERT INTO projects (id,name,status,created_at,updated_at)"
        " VALUES ('gone','Gone','archived','t','t')"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at)"
        " VALUES ('stale','user','gone','Stale','t','t')"
    )
    hierarchy.rehome("stale", db=db)
    plan = service.ensure_plan("thread", "stale", db=db)
    _task(plan.id, "Old work", db=db)
    db.commit()
    return plan.id


def _owner_threads(payload: dict) -> set[str]:
    return {row["owner"]["thread_id"] for row in payload["plans"]}


def test_workspace_activity_omits_thread_in_archived_project(db):
    _seed(db)
    stale_plan = _archived_project_thread(db)

    compact = activity_projection.read_workspace(actor=_actor(), db=db)

    assert "stale" not in _owner_threads(compact)
    assert stale_plan not in {row["plan"]["id"] for row in compact["plans"]}
    assert {"moving", "blocked"} <= _owner_threads(compact)


def test_workspace_activity_shows_thread_once_its_project_is_active(db):
    _seed(db)
    stale_plan = _archived_project_thread(db)

    db.execute("UPDATE projects SET status='active' WHERE id='gone'")
    db.commit()
    shown = activity_projection.read_workspace(actor=_actor(), db=db)

    assert stale_plan in {row["plan"]["id"] for row in shown["plans"]}


def test_archived_project_plans_are_not_reduced(db, monkeypatch):
    _seed(db)
    stale_plan = _archived_project_thread(db)
    seen: list[list[str]] = []
    real = activity_facts.compact_nodes
    monkeypatch.setattr(
        activity_facts, "compact_nodes",
        lambda plan_ids, *, db: seen.append(list(plan_ids)) or real(plan_ids, db=db),
    )

    activity_projection.read_workspace(actor=_actor(), db=db)

    assert seen and stale_plan not in seen[0]
    assert set(seen[0]) >= {
        row["id"] for row in db.execute(
            "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id IN ('moving','blocked','child')"
        )
    }


def test_delegation_facts_read_only_nodes_with_a_spawned_thread(db, monkeypatch):
    _seed(db)
    seen: list[list[tuple]] = []
    real = wait_causes.delegation_facts
    monkeypatch.setattr(
        wait_causes, "delegation_facts",
        lambda pairs, *, db: seen.append(list(pairs)) or real(pairs, db=db),
    )

    activity_projection.read_workspace(actor=_actor(), db=db)

    assert len(seen) == 1 and seen[0]
    assert all(node["spawned_thread_id"] is not None for node, _plan in seen[0])


def test_operator_workspace_projection_keeps_archived_project_thread(db):
    _seed(db)
    _archived_project_thread(db)

    full = operator_projection.read_workspace(actor=_actor(), db=db)

    assert "stale" in _owner_threads(full)
