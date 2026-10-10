"""Hidden subagents never appear in the compact Activity index."""
from __future__ import annotations

import json
import math

import plans.hierarchy as hierarchy
import plans.service as service
from plans import activity_projection, decision_projection
from tests.test_plan_activity_decision_facts import _body
from tests.test_plan_activity_index import _actor, _seed, _task, db  # noqa: F401
from tests.plan_writer_authorizer import raw_plan_seed


def _thread(
    db,
    thread_id: str,
    *,
    spawned_by: str | None = None,
    parent_thread_id: str | None = None,
    project: str = "p",
    assignee: str | None = None,
    state: str = "pending",
):
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,parent_thread_id,title,created_at,updated_at)
           VALUES (?,'user',?,?,?,?,'t','t')""",
        (thread_id, project, spawned_by, parent_thread_id, thread_id.title()),
    )
    hierarchy.rehome(thread_id, db=db)
    plan = service.ensure_plan("thread", thread_id, db=db)
    task = _task(plan.id, f"{thread_id} work", db=db, assignee=assignee)
    if state in {"active", "failed"}:
        service.transition_node(task.id, "active", db=db)
    if state == "failed":
        service.transition_node(task.id, "failed", db=db)
    db.commit()
    return plan


def _read(db) -> dict:
    return activity_projection.read_project("p", actor=_actor(), db=db)


def _rows(payload: dict) -> dict[str, dict]:
    return {row["owner"]["thread_id"]: row for row in payload["plans"]}


def test_hidden_subagents_are_absent_in_every_state_and_lineage(db):
    _seed(db)
    _thread(db, "working", spawned_by="moving", state="active")
    _thread(db, "failed", spawned_by="moving", state="failed")
    _thread(db, "asker", spawned_by="moving", assignee="user")
    _thread(db, "grandchild", spawned_by="working", state="active")
    _thread(db, "archived-spawner")
    db.execute("UPDATE threads SET archived_at='t' WHERE id='archived-spawner'")
    db.commit()
    _thread(db, "orphan", spawned_by="archived-spawner", state="active")

    rows = _rows(_read(db))

    assert {"child", "working", "failed", "asker", "grandchild", "orphan"}.isdisjoint(rows)
    assert {"moving", "blocked", "complete"} <= rows.keys()
    assert all("subagents" not in row for row in rows.values())


def test_visible_chat_with_lineage_keeps_its_own_row(db):
    _seed(db)
    _thread(db, "chat", parent_thread_id="moving")

    rows = _rows(_read(db))

    assert "chat" in rows
    assert "subagents" not in rows["chat"]


def _answerable_decision(db, plan_id: str, thread_id: str) -> None:
    source = _task(plan_id, "Source", db=db)
    review = _task(plan_id, "Review", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='review_requested' WHERE id=?", (review.id,))
    body = _body() | {
        "plan_id": plan_id, "node_id": review.id, "source_node_id": source.id,
        "thread_id": thread_id, "order_key": 0, "revision": 1,
    }
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,metadata,created_at,updated_at)
           VALUES ('artifact','local','plan-decision','decision','active',?,'t','t')""",
        (json.dumps({"attention_state": "raised", "plan_decision": body}),),
    )
    db.execute(
        """INSERT INTO plan_node_artifact_links
           (node_id,artifact_id,role,provenance,created_at)
           VALUES (?,'artifact','attention','test','t')""",
        (review.id,),
    )
    db.commit()


def test_hidden_subagent_decision_still_counts_toward_attention(db):
    _seed(db)
    worker = _thread(db, "worker", spawned_by="moving")
    _answerable_decision(db, worker.id, "worker")

    payload = _read(db)

    assert "worker" not in _rows(payload)
    assert payload["attention"]["awaiting_count"] == 1


class _Recording:
    def __init__(self, connection):
        self.connection, self.sql = connection, []

    def execute(self, statement, parameters=()):
        self.sql.append(statement)
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_hidden_subagent_lookup_is_chunked_and_rows_stay_absent(db):
    _seed(db)
    count = 850
    db.executemany(
        """INSERT INTO threads (id,user_id,project_id,spawned_by,title,created_at,updated_at)
           VALUES (?,'user','p','moving',?,'t','t')""",
        [(f"w{index:04d}", f"W{index}") for index in range(count)],
    )
    db.executemany(
        """INSERT INTO plans (id,owner_kind,owner_id,parent_plan_id,created_at,updated_at)
           VALUES (?,'thread',?,(SELECT id FROM plans WHERE owner_id='moving'),'t','t')""",
        [(f"wp{index:04d}", f"w{index:04d}") for index in range(count)],
    )
    db.commit()
    recording = _Recording(db)

    payload = activity_projection.read_project("p", actor=_actor(), db=recording)

    # A packed read (infra.packed_rows) first prepares its column names with
    # LIMIT 0, which reads nothing; the read itself is counted.
    hidden_queries = [
        sql for sql in recording.sql
        if "spawned_by IS NOT NULL" in sql and not sql.rstrip().endswith(") LIMIT 0")
    ]
    assert len(hidden_queries) == math.ceil((count + 5) / hierarchy._ID_CHUNK)
    rows = _rows(payload)
    assert not any(f"w{index:04d}" in rows for index in (0, count - 1))
    assert all("subagents" not in row for row in rows.values())
