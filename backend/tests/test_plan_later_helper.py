"""A delegated helper must be stopped before an agent moves its task to Later."""

import json
import re

import pytest

from core.thread_lifecycle_workers import archive_stopped_worker
from plans import closure, conductor_add, mutations, service
from plans.boundary import PlanActor
from tests.plan_close_next_helpers import HUMAN, owner_plan, plan_update, revision, row, task
from tests.test_plan_reconcile_release import db  # noqa: F401


def delegated(db):
    plan = owner_plan(db)
    node = task(db, plan.id, "Delegated work")
    child_plan = service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(node.id, "worker", actor_id="owner", db=db)
    task(db, child_plan.id, "Worker's work")
    db.commit()
    return plan, node


def edit(db, plan, node, *, actor=None):
    kwargs = {} if actor is None else {"actor": actor}
    return plan_update(db, plan.id, "edit", {
        "node_id": node.id, "later": True, "expected_revision": revision(db, node.id),
    }, **kwargs)


def owner_message():
    return ("Not moved to Later: helper worker is still on 'Delegated work'. "
            "A task in Later is never worked. Stop the helper first "
            "(delegate_stop thread_id=worker), then move it, or keep the task above the line.")


def test_owner_refused_without_writing(db):
    plan, node = delegated(db)
    before = dict(row(db, node.id))
    with pytest.raises(service.PlanInputError, match="^" + re.escape(owner_message()) + "$"):
        edit(db, plan, node)
    assert dict(row(db, node.id)) == before
    assert db.execute("SELECT count(*) FROM plan_node_events WHERE node_id=? AND event='node_later'", (node.id,)).fetchone()[0] == 0


def test_stopped_helper_does_not_block(db):
    plan, node = delegated(db)
    assert archive_stopped_worker(db, "worker")
    assert edit(db, plan, node)["node"]["later"] is True


def test_helper_with_no_open_work_does_not_block(db):
    plan, node = delegated(db)
    child = service.ensure_plan("thread", "worker", db=db)
    child_node = db.execute("SELECT id FROM plan_nodes WHERE plan_id=?", (child.id,)).fetchone()[0]
    closure.close_node(child_node, reason="Work returned", actor_id="worker", db=db)
    assert edit(db, plan, node)["node"]["later"] is True


def test_bulk_refuses_helper_but_moves_other(db):
    plan, node = delegated(db)
    other = task(db, plan.id, "Other work")
    result = plan_update(db, plan.id, "edit", {"node_ids": [node.id, other.id], "later": True})
    assert result["refused"] == [{"node_id": node.id, "reason": owner_message()}]
    assert [item["node_id"] for item in result["changed"]] == [other.id]
    assert row(db, node.id)["later"] == 0
    assert row(db, other.id)["later"] == 1


def test_conductor_edit_is_refused_before_later(db):
    """conductor_add.py:47-69 disallows foreign edits, including Later edits."""
    plan, node = delegated(db)
    db.execute("INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)", (
        "corsia_agent_thread_id", json.dumps({"session_id": "sibling"}), "t"))
    db.commit()
    actor = PlanActor("sibling", "user", None)
    assert conductor_add.coordinator_may_steer(db, service._plan_row(db, plan.id), actor)
    before = dict(row(db, node.id))
    with pytest.raises(service.PlanInputError, match="Only the CORSIA agent"):
        edit(db, plan, node, actor=actor)
    assert dict(row(db, node.id)) == before


def test_person_can_move_task_with_live_helper(db):
    plan, node = delegated(db)
    assert edit(db, plan, node, actor=HUMAN)["node"]["later"] is True
