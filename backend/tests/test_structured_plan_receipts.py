"""Replay and concurrency tests for structured-plan mutation receipts."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from infra.schema import SCHEMA_SQL
from plans import boundary, service
from plans.boundary import PlanActor


@pytest.fixture
def db():
    """Create an isolated structured-plan database with a parent and child worker."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for thread_id, user_id, spawned_by in (
        ("parent", "user-1", None),
        ("worker", "user-1", "parent"),
        ("sibling", "user-1", "parent"),
    ):
        connection.execute(
            "INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?)",
            (thread_id, user_id, spawned_by, "t", "t"),
        )
    yield connection
    connection.close()


@pytest.fixture
def plan(db):
    """Create the one thread plan exercised by each receipt test."""
    return service.ensure_plan("thread", "parent", db=db)


def _actor(thread_id="parent"):
    return PlanActor(thread_id, "user-1", None)


def _call(db, plan_id, operation, params, mutation_id, actor=None):
    deferred = boundary.mutate(
        plan_id, operation, params, actor=actor or _actor(), provenance="test.receipts",
        mutation_id=mutation_id, db=db,
    )
    return deferred.result


def _add_params(title="Task"):
    return {
        "kind": "task",
        "title": title,
        "acceptance_criteria": f"{title} DOD",
    }


@pytest.mark.parametrize(
    ("operation", "params"),
    [
        ("edit", {"node_id": "node"}),
        ("split", {"node_id": "node", "title": "Next", "acceptance_criteria": "Next DOD"}),
        ("reorder", {"node_id": "node", "order_key": 0}),
        ("activate", {"node_id": "node"}),
        ("request_review", {"node_id": "node", "evidence": "Proof"}),
        ("reject", {"node_id": "node", "feedback": "Fix"}),
        ("fail", {"node_id": "node"}),
        ("attach_spawned_thread", {"node_id": "node", "spawned_thread_id": "worker"}),
        ("accept", {"node_id": "node", "verification_result": "passed"}),
        (
            "accept_with_follow_up",
            {"node_id": "node", "title": "Follow up", "acceptance_criteria": "Follow DOD",
             "verification_result": "passed"},
        ),
    ],
)
def test_existing_node_operations_require_expected_revision(db, plan, operation, params):
    node = service.add_node(plan.id, kind="task", title="Task", acceptance_criteria="DOD", db=db)
    params["node_id"] = node.id

    with pytest.raises(service.PlanError, match="expected_revision is required"):
        _call(db, plan.id, operation, params, f"missing-{operation}")


def test_add_and_insert_replay_without_duplicate_nodes(db, plan):
    added = _call(db, plan.id, "add", _add_params(), "add-1")
    again = _call(db, plan.id, "add", _add_params(), "add-1")
    inserted = _call(
        db, plan.id, "insert",
        {**_add_params("Inserted"), "before_node_id": added["node"]["id"]},
        "insert-1",
    )
    inserted_again = _call(
        db, plan.id, "insert",
        {**_add_params("Inserted"), "before_node_id": added["node"]["id"]},
        "insert-1",
    )

    assert added == again
    assert inserted == inserted_again
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE plan_id=?", (plan.id,)).fetchone()[0] == 2


def test_public_project_scope_add_is_denied_before_dispatch_or_receipt(db, monkeypatch):
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')"
    )
    db.commit()
    project_plan = service.ensure_plan("project_scope", "project-1", db=db)
    calls = []
    monkeypatch.setattr(
        boundary, "_authorize_and_dispatch",
        lambda *args, **kwargs: calls.append(True),
    )

    with pytest.raises(PermissionError, match="public scope plan mutation"):
        _call(
            db, project_plan.id, "add",
            {"kind": "task", "title": "Project task", "acceptance_criteria": "Project DOD"},
            "project-denied", actor=PlanActor(None, "user-1", "project-1", kind="user"),
        )

    assert calls == []
    assert db.execute(
        "SELECT COUNT(*) FROM plan_nodes WHERE plan_id=?", (project_plan.id,)
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE plan_id=?", (project_plan.id,)
    ).fetchone()[0] == 0


def test_split_replay_returns_same_sibling_and_stale_new_key_conflicts(db, plan):
    source = _call(db, plan.id, "add", _add_params(), "add-source")["node"]
    params = {
        "node_id": source["id"], "title": "Remainder", "acceptance_criteria": "Remainder DOD",
        "expected_revision": source["revision"],
    }
    split = _call(db, plan.id, "split", params, "split-1")
    replay = _call(db, plan.id, "split", params, "split-1")

    assert split == replay
    assert service.get_node(source["id"], db=db).revision == source["revision"] + 1
    with pytest.raises(service.PlanError, match="revision conflict"):
        _call(db, plan.id, "split", params, "split-2")


def test_accept_and_follow_up_replay_from_durable_receipts(db, plan):
    accepted = _reviewed_node(db, plan, "Accept")
    accept_params = {
        "node_id": accepted.id, "verification_result": "passed",
        "expected_revision": accepted.revision,
    }
    first_accept = _call(db, plan.id, "accept", accept_params, "accept-1")
    assert first_accept == _call(db, plan.id, "accept", accept_params, "accept-1")

    original = _reviewed_node(db, plan, "Follow")
    follow_params = {
        "node_id": original.id, "title": "Fix follow-up", "acceptance_criteria": "Fix DOD",
        "verification_result": "passed", "expected_revision": original.revision,
    }
    first_follow = _call(db, plan.id, "accept_with_follow_up", follow_params, "follow-1")
    assert first_follow == _call(db, plan.id, "accept_with_follow_up", follow_params, "follow-1")
    assert db.execute(
        "SELECT COUNT(*) FROM plan_nodes WHERE follow_up_of=?", (original.id,)
    ).fetchone()[0] == 1


def test_mutation_id_collision_and_actor_staleness_are_rejected(db, plan):
    _call(db, plan.id, "add", _add_params(), "collision")
    with pytest.raises(service.PlanError, match="different request"):
        _call(db, plan.id, "add", _add_params("Different"), "collision")
    with pytest.raises(service.PlanError, match="different request"):
        _call(db, plan.id, "add", _add_params(), "collision", actor=_actor("sibling"))
    with pytest.raises(PermissionError, match="stale"):
        _call(
            db, plan.id, "add", _add_params("Stale"), "stale-actor",
            actor=PlanActor("parent", "user-1", "wrong-project"),
        )


def test_public_project_scope_accept_is_denied_before_receipt(db):
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')"
    )
    db.commit()
    project_plan = service.ensure_plan("project_scope", "project-1", db=db)
    node = service.add_node(
        project_plan.id, kind="task", title="Human review", acceptance_criteria="Passes",
        assignee="user", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    waiting = service.transition_node(node.id, "review_requested", db=db)

    with pytest.raises(PermissionError, match="public scope plan mutation"):
        _call(
            db, project_plan.id, "accept",
            {
                "node_id": node.id, "verification_result": "passed",
                "expected_revision": waiting.revision,
            },
            "project-accept", actor=PlanActor(None, "user-1", "project-1", kind="user"),
        )

    assert service.get_node(node.id, db=db).state == "review_requested"
    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE plan_id=?", (project_plan.id,)
    ).fetchone()[0] == 0


def test_mutation_receipts_are_append_only(db, plan):
    """Keep committed receipts immutable."""
    result = _call(db, plan.id, "add", _add_params(), "immutable-receipt")
    original = db.execute(
        "SELECT result_json FROM plan_mutation_receipts WHERE plan_id=? AND mutation_id=?",
        (plan.id, "immutable-receipt"),
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "UPDATE plan_mutation_receipts SET result_json='{}' WHERE plan_id=? AND mutation_id=?",
            (plan.id, "immutable-receipt"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "DELETE FROM plan_mutation_receipts WHERE plan_id=? AND mutation_id=?",
            (plan.id, "immutable-receipt"),
        )
    assert db.execute(
        "SELECT result_json FROM plan_mutation_receipts WHERE plan_id=? AND mutation_id=?",
        (plan.id, "immutable-receipt"),
    ).fetchone()[0] == original
    assert result["node"]["id"] in original

def test_authorization_dispatch_and_receipt_share_the_outer_write(db, plan, monkeypatch):
    observed = []
    original = boundary._authorize_and_dispatch

    def check_transaction(*args, **kwargs):
        observed.append(db.in_transaction)
        return original(*args, **kwargs)

    monkeypatch.setattr(boundary, "_authorize_and_dispatch", check_transaction)
    _call(db, plan.id, "add", _add_params(), "atomic-1")

    assert observed == [True]
    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE plan_id=?",
        (plan.id,),
    ).fetchone()[0] == 1


def test_boundary_first_import_has_no_service_mutation_cycle():
    backend = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import plans.boundary; from plans import service; assert callable(service.reorder_node)"],
        cwd=backend,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _reviewed_node(db, plan, title):
    node = _call(db, plan.id, "add", _add_params(title), f"add-{title}")["node"]
    active = _call(
        db, plan.id, "activate",
        {"node_id": node["id"], "expected_revision": node["revision"]},
        f"active-{title}",
    )["node"]
    return service.get_node(
        _call(
            db, plan.id, "request_review",
            {
                "node_id": node["id"],
                "evidence": "Focused proof passed",
                "expected_revision": active["revision"],
            },
            f"review-{title}",
        )["node"]["id"],
        db=db,
    )
