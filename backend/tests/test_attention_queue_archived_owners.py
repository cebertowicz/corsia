"""The attention queue lists a decision only where Activity would count it.

The dock reads `/api/attention/queue` and Activity reads its own awaiting
count. Both must apply the same owner predicate, so a decision whose owner sits
in an archived project is in neither.
"""

from __future__ import annotations

import plans.mutations as mutations
import plans.service as service
from plans import activity_projection, hierarchy
from plans.boundary import PlanActor
from tests.test_attention_queue import _seed, db, raise_decision  # noqa: F401
from tests.test_attention_queue_api import _attention_client


def _archive_project_of(db, thread_id: str) -> None:
    """Move a thread into a project that is archived after the decision was raised."""
    db.execute(
        "INSERT INTO projects (id,name,status,created_at,updated_at)"
        " VALUES ('gone','Gone','archived','t','t')"
    )
    db.execute("UPDATE threads SET project_id='gone' WHERE id=?", (thread_id,))
    db.commit()


def _awaiting(db) -> int:
    actor = PlanActor(None, "user-1", None, kind="user")
    return activity_projection.read_workspace(actor=actor, db=db)["attention"]["awaiting_count"]


def test_queue_omits_a_decision_owned_in_an_archived_project(db, monkeypatch):
    live = raise_decision(db, "child", "child", title="Live question")
    raise_decision(db, "other-root", "other-root", title="Archived question")
    _archive_project_of(db, "other-root")
    client = _attention_client(db, monkeypatch)

    body = client.get("/api/attention/queue").json()

    assert [item["detail"]["title"] for item in body["items"]] == ["Live question"]
    assert body["count"] == 1
    assert body["current"]["external_id"] == live["id"]


def test_queue_count_and_activity_awaiting_count_agree(db, monkeypatch):
    raise_decision(db, "child", "child", title="Live question")
    raise_decision(db, "other-root", "other-root", title="Archived question")
    client = _attention_client(db, monkeypatch)
    assert client.get("/api/attention/queue").json()["count"] == _awaiting(db) == 2

    _archive_project_of(db, "other-root")

    assert client.get("/api/attention/queue").json()["count"] == _awaiting(db) == 1


def test_queue_shows_the_decision_again_once_its_project_is_active(db, monkeypatch):
    raise_decision(db, "other-root", "other-root", title="Back again")
    _archive_project_of(db, "other-root")
    client = _attention_client(db, monkeypatch)
    assert client.get("/api/attention/queue").json()["count"] == 0

    db.execute("UPDATE projects SET status='active' WHERE id='gone'")
    db.commit()

    assert client.get("/api/attention/queue").json()["count"] == 1


def test_a_workers_decision_follows_the_top_level_plan_that_shows_it(db, monkeypatch):
    """A worker plan nests under its parent's step; its parent's project decides."""
    parent = service.ensure_plan("thread", "root", db=db)
    step = service.add_node(
        parent.id, kind="task", title="Delegated",
        acceptance_criteria="Done", spawn_kind="claude-code", db=db,
    )
    service.transition_node(step.id, "active", db=db)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    db.commit()
    raise_decision(db, "child", "child", title="Worker question")
    client = _attention_client(db, monkeypatch)
    assert client.get("/api/attention/queue").json()["count"] == _awaiting(db) == 1

    _archive_project_of(db, "root")

    assert client.get("/api/attention/queue").json()["count"] == _awaiting(db) == 0


def test_owner_filter_reads_a_fixed_number_of_statements(db):
    """The filter's cost does not grow with the number of queued decisions."""
    actor = PlanActor(None, "user-1", None, kind="user")

    def statements(plan_ids) -> int:
        seen: list[str] = []
        # A packed read (infra.packed_rows) first prepares its column names with
        # LIMIT 0, which reads nothing; the read itself is counted.
        db.set_trace_callback(
            lambda sql: None if sql.rstrip().endswith(") LIMIT 0") else seen.append(sql)
        )
        try:
            activity_projection.counted_plan_ids(plan_ids, actor=actor, db=db)
        finally:
            db.set_trace_callback(None)
        return len(seen)

    one = raise_decision(db, "child", "child")["plan_id"]
    few = [one] + [
        raise_decision(db, thread, thread)["plan_id"]
        for thread in ("grandchild", "other-root")
    ]

    assert statements([one]) == statements(few)
    assert statements([one]) <= 5


def _add_thread(db, thread_id: str, spawned_by: str) -> None:
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,lifecycle_state,archived_at,
            title,model,created_at,updated_at)
           VALUES (?, 'user-1', 'project-1', ?, 'active', NULL, ?,
                   'claude-code/sonnet','t','t')""",
        (thread_id, spawned_by, thread_id),
    )


def _delegate(db, parent_thread: str, child_thread: str) -> None:
    """Nest the child's plan under a step of the parent's plan."""
    parent = service.ensure_plan("thread", parent_thread, db=db)
    step = service.add_node(
        parent.id, kind="task", title=f"Delegate {child_thread}",
        acceptance_criteria="Done", spawn_kind="claude-code", db=db,
    )
    service.transition_node(step.id, "active", db=db)
    mutations.attach_spawned_thread(step.id, child_thread, db=db)
    db.commit()


def test_a_decision_below_a_deep_worker_chain_is_not_dropped(db, monkeypatch):
    """No depth cap: Activity walks down without one, so the queue walks up the same."""
    names = [f"worker-{level}" for level in range(1, 11)]
    parent = "root"
    for name in names:
        _add_thread(db, name, parent)
        _delegate(db, parent, name)
        parent = name
    db.commit()
    raise_decision(db, parent, parent, title="Ten levels down")
    client = _attention_client(db, monkeypatch)

    body = client.get("/api/attention/queue").json()

    assert [item["detail"]["title"] for item in body["items"]] == ["Ten levels down"]
    assert body["count"] == _awaiting(db) == 1

    _archive_project_of(db, "root")

    assert client.get("/api/attention/queue").json()["count"] == _awaiting(db) == 0


def test_a_parent_cycle_ends_and_has_no_top_level_plan(db):
    _add_thread(db, "worker", "root")
    _delegate(db, "root", "worker")
    ids = [
        service.ensure_plan("thread", thread, db=db).id for thread in ("root", "worker")
    ]
    # The service refuses a cycle, so close the loop on the rows: the worker's
    # plan gains a step that names the root thread, and the root plan a parent.
    loop = service.add_node(
        ids[1], kind="task", title="Back to root", acceptance_criteria="Done", db=db,
    )
    db.execute("UPDATE plan_nodes SET spawned_thread_id='root' WHERE id=?", (loop.id,))
    db.execute("UPDATE plans SET parent_plan_id=? WHERE id=?", (ids[1], ids[0]))
    db.commit()
    shown = db.execute(
        f"SELECT COUNT(*) FROM plans p WHERE p.id IN (?,?) AND "
        f"({hierarchy.shown_under_step_sql('p')})", ids,
    ).fetchone()[0]
    assert shown == 2
    actor = PlanActor(None, "user-1", None, kind="user")

    assert activity_projection.counted_plan_ids(ids, actor=actor, db=db) == set()


def test_an_archived_top_level_plan_is_not_counted(db):
    top = raise_decision(db, "other-root", "other-root")["plan_id"]
    actor = PlanActor(None, "user-1", None, kind="user")
    assert activity_projection.counted_plan_ids([top], actor=actor, db=db) == {top}

    db.execute("UPDATE plans SET state='archived' WHERE id=?", (top,))
    db.commit()

    assert activity_projection.counted_plan_ids([top], actor=actor, db=db) == set()
