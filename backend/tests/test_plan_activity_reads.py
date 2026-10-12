"""The compact Activity index reads only the plans whose state it can show.

A plan's reduced state reaches the payload only through a shown top-level row
or through the open step that delegated to it. Every other plan in the tree is
neither read nor reduced, and every owner fact is stepped once per statement
rather than once per row.
"""
from __future__ import annotations

import plans.service as service
from plans import activity_facts, activity_projection, wait_causes
from tests.test_plan_activity_index import _actor, _seed, _task, db  # noqa: F401
from tests.test_plan_activity_subagents import _thread
from tests.plan_writer_authorizer import raw_plan_seed


def _plan_id(db, thread_id: str) -> str:
    return service.ensure_plan("thread", thread_id, db=db).id


def _project_plan_id(db) -> str:
    return service.ensure_plan("project_scope", "p", db=db).id


def _read_plan_ids(db, monkeypatch) -> set[str]:
    batches: list[list[str]] = []
    real = activity_facts.compact_nodes

    def record(plan_ids, *, db):
        batches.append(list(plan_ids))
        return real(plan_ids, db=db)

    monkeypatch.setattr(activity_facts, "compact_nodes", record)
    activity_projection.read_project("p", actor=_actor(), db=db)
    return {plan_id for batch in batches for plan_id in batch}


def test_only_shown_plans_and_their_open_delegates_are_read(db, monkeypatch):
    _seed(db)
    _thread(db, "worker", spawned_by="moving", state="active")

    read = _read_plan_ids(db, monkeypatch)

    assert read == {
        _project_plan_id(db),
        _plan_id(db, "moving"),
        _plan_id(db, "blocked"),
        _plan_id(db, "complete"),
        _plan_id(db, "child"),
    }
    assert _plan_id(db, "worker") not in read


def test_a_closed_delegating_step_does_not_read_its_child_plan(db, monkeypatch):
    _seed(db)
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET state='closed' WHERE spawned_thread_id='child'",
        )
    db.commit()

    read = _read_plan_ids(db, monkeypatch)

    assert read == {
        _project_plan_id(db),
        _plan_id(db, "moving"),
        _plan_id(db, "blocked"),
        _plan_id(db, "complete"),
    }


def _name_thread(db, plan_id: str, title: str, thread_id: str, *, at: str) -> str:
    """A delegating step in `plan_id` that names `thread_id`, attached at `at`."""
    node = _task(plan_id, title, db=db, spawn_kind="claude-code")
    service.transition_node(node.id, "active", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET spawned_thread_id=? WHERE id=?", (thread_id, node.id))
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,actor_id,provenance,event,payload_json,created_at)
           VALUES (?,?,NULL,'test.edge','spawned_thread_attached',
                   json_object('child_plan_id', (SELECT id FROM plans
                     WHERE owner_kind='thread' AND owner_id=?)),?)""",
        (f"edge-{node.id}", node.id, thread_id, at),
    )
    return node.id


def test_every_child_plan_the_delegation_facts_name_is_in_the_delegation_closure(db):
    _seed(db)
    parent = _plan_id(db, "moving")
    for name in ("reused", "archived", "closed", "queued", "reviewing"):
        _thread(db, name, spawned_by="moving", state="active")
    _thread(db, "elsewhere", spawned_by="blocked", state="active")
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at)"
        " VALUES ('ghost','user','p','Ghost','t','t')",
    )
    steps = {
        "older of two attachments": _name_thread(db, parent, "Older", "reused", at="2026-01-01"),
        "newest of two attachments": _name_thread(db, parent, "Newer", "reused", at="2026-02-01"),
        "archived step": _name_thread(db, parent, "Archived", "archived", at="2026-01-01"),
        "closed step": _name_thread(db, parent, "Closed", "closed", at="2026-01-01"),
        "pending step": _name_thread(db, parent, "Queued", "queued", at="2026-01-01"),
        "review step": _name_thread(db, parent, "Reviewing", "reviewing", at="2026-01-01"),
        "other parent plan": _name_thread(db, parent, "Elsewhere", "elsewhere", at="2026-01-01"),
        "thread with no plan": _name_thread(db, parent, "Ghost", "ghost", at="2026-01-01"),
    }
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='2026-03-01' WHERE id=?", (steps["archived step"],))
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (steps["closed step"],))
        db.execute("UPDATE plan_nodes SET state='pending' WHERE id=?", (steps["pending step"],))
        db.execute("UPDATE plan_nodes SET state='review_requested' WHERE id=?", (steps["review step"],))
    db.commit()
    nodes = [dict(row) for row in db.execute("SELECT * FROM plan_nodes").fetchall()]
    node_plans = [(node, {"id": node["plan_id"]}) for node in nodes]
    plans = {str(row["id"]): dict(row) for row in db.execute("SELECT * FROM plans").fetchall()}

    named = {
        str(row["child_plan_id"])
        for row in wait_causes.delegation_facts(node_plans, db=db).values()
        if row["child_plan_id"] is not None
    }
    delegated = set(activity_facts.delegation_closure_ids([parent], plans, db=db))

    assert _plan_id(db, "reused") in named
    assert _plan_id(db, "child") in named
    assert {_plan_id(db, "queued"), _plan_id(db, "reviewing")} <= named
    assert _plan_id(db, "archived") not in named
    assert _plan_id(db, "closed") not in named
    assert _plan_id(db, "elsewhere") not in named
    assert named <= delegated


_UNSTEPPED_PREFIXES = (
    "SELECT * FROM threads",
    "SELECT * FROM projects WHERE id IN",
    "SELECT id FROM threads",
    "SELECT node_id,kind,owner_kind,reason_key",
    "SELECT d.node_id",
    "SELECT w.id",
    "SELECT n.id AS node_id",
    "SELECT t.id,",
    "SELECT i.id AS artifact_id",
)


def test_owner_facts_are_stepped_once_per_statement(db):
    _seed(db)
    _thread(db, "worker", spawned_by="moving", state="active")
    traced: list[str] = []
    db.set_trace_callback(traced.append)

    activity_projection.read_project("p", actor=_actor(), db=db)
    db.set_trace_callback(None)

    statements = [sql.lstrip() for sql in traced]
    assert [sql for sql in statements if sql.startswith(_UNSTEPPED_PREFIXES)] == []
    assert any(
        sql.startswith("WITH q(") and "FROM threads WHERE id IN" in sql
        for sql in statements
    )
