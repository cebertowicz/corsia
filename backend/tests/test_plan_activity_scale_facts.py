"""Fact-rich scale proof against actual compact modules, without timing assertions."""
from __future__ import annotations

import json
import re
import time

from plans import activity_projection, admission_projection, operator_node_state, service
from tests.test_plan_activity_index import db, _actor
from tests.test_plan_activity_decision_facts import _body
from tests.plan_writer_authorizer import raw_plan_seed


def seed_scale(db, *, rich: bool) -> None:
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('scale','Scale','t','t')")
    root = service.ensure_plan("project_scope", "scale", db=db)
    threads, plans, nodes, identities, links = [], [], [], [], []
    for index in range(823):
        thread_id, plan_id = f"scale-thread-{index:04d}", f"scale-plan-{index:04d}"
        threads.append((thread_id, "user", "scale", f"Scale {index}", "t", "t"))
        plans.append((plan_id, thread_id, root.id, "t", "t"))
        count = 6 if index < 558 else 5
        for offset in range(count):
            node_id = f"scale-node-{index:04d}-{offset}"
            nodes.append((node_id, plan_id, offset, f"Task {offset}", node_id,
                          "review_requested" if rich and offset == 0 else "pending"))
        if rich:
            review_id, source_id = f"scale-node-{index:04d}-0", f"scale-node-{index:04d}-1"
            body = _body() | {
                "plan_id": plan_id, "node_id": review_id, "source_node_id": source_id,
                "thread_id": thread_id, "order_key": 0, "revision": 1,
                "context": "FULL_CONTEXT_SENTINEL_" + "x" * 32768,
                "options": [{"id": "yes", "label": "FULL_OPTION_SENTINEL_" + "y" * 32768}],
            }
            identities.append((f"decision-{index}", json.dumps({"attention_state": "raised", "plan_decision": body})))
            links.append((review_id, f"decision-{index}"))
    db.executemany("""INSERT INTO threads(id,user_id,project_id,title,created_at,updated_at)
        VALUES (?,?,?,?,?,?)""", threads)
    db.executemany("""INSERT INTO plans(id,owner_kind,owner_id,parent_plan_id,created_at,updated_at)
        VALUES (?,'thread',?,?,?,?)""", plans)
    with raw_plan_seed(db):
        db.executemany("""INSERT INTO plan_nodes
            (id,plan_id,order_key,kind,title,commitment_id,state,acceptance_criteria,created_at,updated_at)
            VALUES (?,?,?,'task',?,?,?,'done','t','t')""", nodes)
    db.executemany("""INSERT INTO artifact_identities
        (id,integration,type,external_id,state,metadata,created_at,updated_at)
        VALUES (?,'local','plan-decision',?,'active',?,'t','t')""",
        [(identity, identity, metadata) for identity, metadata in identities])
    db.executemany("""INSERT INTO plan_node_artifact_links(node_id,artifact_id,role,provenance,created_at)
        VALUES (?,?,'attention','test','t')""", links)
    db.commit()
    assert len(nodes) == 4673


class RecordingCursor:
    def __init__(self, cursor, metric):
        self.cursor, self.metric = cursor, metric

    def _record(self, row):
        if row is not None:
            self.metric["rows"] += 1
            self.metric["selected_bytes"] += sum(len(str(value).encode("utf-8")) for value in row if value is not None)
            if "metadata" in self.metric["columns"]:
                self.metric["metadata_bytes"] += len((row["metadata"] or "").encode())
        return row

    def __iter__(self):
        for row in self.cursor:
            yield self._record(row)

    def fetchall(self):
        return [self._record(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        return self._record(self.cursor.fetchone())


class RecordingDb:
    def __init__(self, db):
        self.db, self.queries = db, []

    def execute(self, sql, parameters=()):
        cursor = self.db.execute(sql, parameters)
        metric = {"sql": sql, "columns": [item[0] for item in cursor.description or []],
                  "rows": 0, "selected_bytes": 0, "metadata_bytes": 0}
        self.queries.append(metric)
        return RecordingCursor(cursor, metric)


def measure(db, *, rich: bool):
    recorded = RecordingDb(db)
    started = time.perf_counter()
    result = activity_projection.read_project("scale", actor=_actor(), db=recorded)
    elapsed = time.perf_counter() - started
    # A packed read (infra.packed_rows) first prepares its column names with
    # LIMIT 0, which reads nothing; the read itself is counted.
    selects = [
        query for query in recorded.queries
        if query["sql"].lstrip().upper().startswith(("SELECT", "WITH"))
        and not query["sql"].rstrip().endswith(") LIMIT 0")
    ]
    groups = [group for query in selects for group in re.findall(r"\bIN\s*\(([^()]*)\)", query["sql"], re.I) if "?" in group]
    wire = json.dumps(result)
    assert len(result["plans"]) == 823
    assert result["attention"]["awaiting_count"] == (823 if rich else 0)
    assert len(selects) <= 32
    assert max((group.count("?") for group in groups), default=0) <= 400
    assert len(wire.encode()) < 1_000_000
    assert "FULL_CONTEXT_SENTINEL" not in wire and "FULL_OPTION_SENTINEL" not in wire
    assert all("nodes" not in row for row in result["plans"])
    return {"rich": rich, "seconds": elapsed, "selects": len(selects),
            "max_in_placeholders": max((group.count("?") for group in groups), default=0),
            "wire_bytes": len(wire.encode()), "awaiting_count": result["attention"]["awaiting_count"],
            "selected_bytes": sum(query["selected_bytes"] for query in selects),
            "metadata_bytes": sum(query["metadata_bytes"] for query in selects),
            "queries": selects}


def test_rich_decision_scale_preserves_fact_budget_and_no_detail_boundary(db, monkeypatch):
    seed_scale(db, rich=True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("compact crossed the full-detail boundary")

    monkeypatch.setattr(admission_projection, "causes_many", forbidden)
    monkeypatch.setattr(admission_projection, "project_many", forbidden)
    monkeypatch.setattr(operator_node_state, "project", forbidden)
    measure(db, rich=True)
