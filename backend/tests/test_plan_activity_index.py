"""Compact Activity scope-index contract and parity tests (#3142)."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import json
import re
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.schema import SCHEMA_SQL
import plans.admission as admission
import plans.holds as holds
import plans.mutations as mutations
import plans.service as service
from plans.boundary import PlanActor


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','Project','t','t')"
    )
    for thread_id, title, spawned_by in (
        ("moving", "Moving", None),
        ("blocked", "Blocked", None),
        ("complete", "Complete", None),
        ("child", "Attached child", "moving"),
        ("foreign", "Foreign", None),
    ):
        user_id = "other" if thread_id == "foreign" else "user"
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,title,created_at,updated_at)
               VALUES (?,?,?,?,?,'t','t')""",
            (thread_id, user_id, "p", spawned_by, title),
        )
    connection.commit()
    yield connection
    connection.close()


def _actor() -> PlanActor:
    return PlanActor(None, "user", None, kind="user")


def _task(plan_id: str, title: str, *, db: sqlite3.Connection, **kwargs):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} done",
        db=db,
        **kwargs,
    )


def _seed(db: sqlite3.Connection) -> None:
    moving = service.ensure_plan("thread", "moving", db=db)
    running = _task(moving.id, "Run", db=db)
    service.transition_node(running.id, "active", db=db)
    admission.select(running.id, actor_id="moving", provenance="test", db=db)

    blocked = service.ensure_plan("thread", "blocked", db=db)
    failed_prerequisite = _task(blocked.id, "Failed prerequisite", db=db)
    service.transition_node(failed_prerequisite.id, "failed", db=db)
    held = _task(blocked.id, "Held", db=db)
    service.add_dependency(held.id, failed_prerequisite.id, db=db)

    complete = service.ensure_plan("thread", "complete", db=db)
    done = _task(complete.id, "Done", db=db)
    service.transition_node(done.id, "active", db=db)
    service.transition_node(done.id, "failed", db=db)

    child = service.ensure_plan("thread", "child", db=db)
    child_task = _task(child.id, "Child task", db=db)
    service.transition_node(child_task.id, "active", db=db)
    parent_task = _task(moving.id, "Delegated", db=db, spawn_kind="claude-code")
    service.transition_node(parent_task.id, "active", db=db)
    mutations.attach_spawned_thread(parent_task.id, "child", db=db)
    db.commit()


def _priority_at(full: dict) -> str:
    matches = (
        [node["updated_at"] for node in full["nodes"] if node["requires_user_action"]]
        if full["display"]["label_key"] == "needs_you"
        else []
    )
    return max(matches, default=full["plan"]["updated_at"])


def test_compact_project_index_matches_full_activity_semantics(db):
    from plans import activity_projection, operator_projection

    _seed(db)
    full = operator_projection.read_project("p", actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full_by_id = {entry["plan"]["id"]: entry for entry in full["plans"]}

    assert compact["contract_version"] == 1
    assert compact["representation"] == "activity_scope_index"
    assert compact["target"] == full["target"]
    assert compact["attention"] == full["attention"]
    assert {row["plan"]["id"] for row in compact["plans"]} == set(full_by_id)
    for row in compact["plans"]:
        detailed = full_by_id[row["plan"]["id"]]
        assert row == {
            "plan": {
                key: detailed["plan"][key]
                for key in ("id", "owner_kind", "owner_id", "state", "revision", "updated_at")
            },
            "owner": detailed["owner"],
            "progress": {
                "open": detailed["progress"]["open"],
                "liveness": detailed["progress"]["liveness"],
            },
            "has_nodes": bool(detailed["nodes"]),
            "display": {
                "label_key": detailed["display"]["label_key"],
                "tone_key": detailed["display"]["tone_key"],
            },
            "priority_at": _priority_at(detailed),
        }
        assert not ({"nodes", "current_node_ids", "next_node_ids", "oversize"} & row.keys())


class _RecordingDb:
    def __init__(self, connection):
        self.connection = connection
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, statement, parameters=()):
        self.calls.append((statement, tuple(parameters)))
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_compact_index_823_plans_4673_nodes_is_chunked_and_fact_bounded(db, monkeypatch):
    from plans import activity_projection, admission_projection, operator_node_state

    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('scale','Scale','t','t')"
    )
    root = service.ensure_plan("project_scope", "scale", db=db)
    thread_rows = []
    plan_rows = []
    node_rows = []
    for index in range(823):
        thread_id = f"scale-thread-{index:04d}"
        plan_id = f"scale-plan-{index:04d}"
        thread_rows.append((thread_id, "user", "scale", f"Scale {index}", "t", "t"))
        plan_rows.append((plan_id, thread_id, root.id, "t", "t"))
        node_count = 6 if index < 558 else 5
        for node_index in range(node_count):
            node_id = f"scale-node-{index:04d}-{node_index}"
            node_rows.append((
                node_id, plan_id, node_index, f"Task {node_index}",
                "Task complete", node_id, "t", "t",
            ))
    db.executemany(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES (?,?,?,?,?,?)""",
        thread_rows,
    )
    db.executemany(
        """INSERT INTO plans
           (id,owner_kind,owner_id,parent_plan_id,created_at,updated_at)
           VALUES (?,'thread',?,?,?,?)""",
        plan_rows,
    )
    db.executemany(
        """INSERT INTO plan_nodes
           (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,
            commitment_id,created_at,updated_at)
           VALUES (?,?,NULL,?,'task',?,?,?,?,?)""",
        node_rows,
    )
    db.commit()
    assert len(node_rows) == 4673

    def forbidden(*_args, **_kwargs):
        raise AssertionError("compact projection crossed the full-detail boundary")

    monkeypatch.setattr(admission_projection, "causes_many", forbidden)
    monkeypatch.setattr(admission_projection, "project_many", forbidden)
    monkeypatch.setattr(operator_node_state, "project", forbidden)
    recorded = _RecordingDb(db)
    payload = activity_projection.read_project("scale", actor=_actor(), db=recorded)

    # A packed read (infra.packed_rows) first prepares its column names with
    # LIMIT 0, which reads nothing; the read itself is counted.
    selects = [
        statement for statement, _parameters in recorded.calls
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        and not statement.rstrip().endswith(") LIMIT 0")
    ]
    dynamic_in_groups = [
        group
        for statement in selects
        for group in re.findall(r"\bIN\s*\(([^()]*)\)", statement, flags=re.IGNORECASE)
        if "?" in group
    ]
    assert len(payload["plans"]) == 823
    assert len(selects) <= 32
    assert dynamic_in_groups
    assert max(group.count("?") for group in dynamic_in_groups) <= 400
    assert len(json.dumps(payload).encode()) < 1_000_000
    lowered = "\n".join(selects).lower()
    assert "plan_node_cost" not in lowered
    assert "plan_node_evidence" not in lowered
    assert "select * from plan_node_attempts" not in lowered
    assert "select n.*" not in lowered


def test_compact_nodes_uses_one_packed_statement_per_chunk(db):
    from plans import activity_facts

    plan = service.ensure_plan("thread", "moving", db=db)
    node = _task(plan.id, "Packed", db=db)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        rows = activity_facts.compact_nodes([plan.id], db=db)
    finally:
        db.set_trace_callback(None)

    assert rows[plan.id][0]["id"] == node.id
    assert any(
        "json_group_array" in statement and "FROM plan_nodes n" in statement
        for statement in statements
    )


def test_compact_wait_ranges_keep_earliest_display_and_latest_overdue_clock(
    db, monkeypatch,
):
    from plans import activity_facts, not_moving, operator_node_state

    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, "Wait twice", db=db)
    db.executemany(
        """INSERT INTO expected_event_watches
           (id,owner_thread_id,plan_id,plan_node_id,condition_type,condition_value,
            status,armed_at,created_at,updated_at)
           VALUES (?, 'moving', ?, ?, 'deadline', ?, 'armed', 't', 't', 't')""",
        [
            ("early", plan.id, task.id, "2026-09-30T08:00:00+00:00"),
            ("late", plan.id, task.id, "2026-09-30T09:55:00+00:00"),
        ],
    )
    db.commit()

    row = activity_facts.compact_nodes([plan.id], db=db)[plan.id][0]
    assert row["wait_deadline"] == "2026-09-30T08:00:00+00:00"
    assert row["wait_deadline_latest"] == "2026-09-30T09:55:00+00:00"

    monkeypatch.setattr(
        not_moving, "_utcnow",
        lambda: datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
    )
    counts = dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0)
    counts["waiting"] = 1
    facts = not_moving.read_from_facts(
        counts,
        user_ready=0,
        user_blocked=0,
        paused_by_user=0,
        wait_deadlines=[(row["wait_deadline"], row["wait_deadline_latest"])],
    )
    assert facts == {
        "need_you": 0,
        "next_wait_ends_at": "2026-09-30T08:00:00+00:00",
        "next_overdue_at": "2026-09-30T10:06:00+00:00",
        "overdue_waits": 0,
        "paused_by_you": 0,
    }
    monkeypatch.setattr(
        not_moving, "_utcnow",
        lambda: datetime(2026, 9, 30, 10, 7, tzinfo=timezone.utc),
    )
    overdue = not_moving.read_from_facts(
        counts,
        user_ready=0,
        user_blocked=0,
        paused_by_user=0,
        wait_deadlines=[(row["wait_deadline"], row["wait_deadline_latest"])],
    )
    assert overdue is not None
    assert overdue["overdue_waits"] == 1
    assert overdue["next_overdue_at"] is None


def test_compact_semantic_matrix_matches_full_projection(db):
    from plans import activity_projection, artifact_links, decisions, operator_projection
    from registry import attention, models

    def add_thread(thread_id: str, title: str, *, user_id: str = "user"):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,title,created_at,updated_at)
               VALUES (?,?,?,?,?,?)""",
            (thread_id, user_id, "p", title, "t", "t"),
        )
        return service.ensure_plan("thread", thread_id, db=db)

    ready = add_thread("ready", "Ready")
    _task(ready.id, "Ready task", db=db)

    waiting = add_thread("waiting", "Waiting")
    waiting_task = _task(waiting.id, "Waiting task", db=db)
    db.execute(
        """INSERT INTO expected_event_watches
           (id,owner_thread_id,plan_id,plan_node_id,condition_type,condition_value,
            status,armed_at,created_at,updated_at)
           VALUES ('watch','waiting',?,?,'deadline','2030-01-01T00:00:00+00:00',
                   'armed','t','t','t')""",
        (waiting.id, waiting_task.id),
    )

    paused = add_thread("paused", "Paused")
    _task(paused.id, "Paused task", db=db)
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (paused.id,))

    human = add_thread("human-review", "Human review")
    human_task = _task(human.id, "Human review task", db=db, assignee="user")
    service.transition_node(human_task.id, "review_requested", db=db)

    parent = add_thread("parent-review", "Parent review")
    parent_task = _task(parent.id, "Parent review task", db=db)
    service.transition_node(parent_task.id, "review_requested", db=db)

    accepted = add_thread("accepted", "Accepted")
    accepted_task = _task(accepted.id, "Accepted task", db=db)
    db.execute(
        "UPDATE plan_nodes SET state='accepted' WHERE id=?", (accepted_task.id,),
    )

    attention_plan = add_thread("raised-attention", "Raised attention")
    attention_task = _task(attention_plan.id, "Read the note", db=db)
    artifact = models.register_artifact(
        integration="workspace", type="document", external_id="activity-note.md",
        parent_thread_id="raised-attention", title="Activity note", db=db,
    )
    artifact_links.link(
        attention_task.id, artifact["id"], "attention", actor_id="raised-attention",
        provenance="test.activity_index", db=db,
    )
    attention.raise_attention(artifact["id"], db=db)

    empty = add_thread("empty", "Empty")
    assert empty.id
    archived = add_thread("archived-owner", "Archived owner")
    _task(archived.id, "Hidden", db=db)
    db.execute(
        "UPDATE threads SET lifecycle_state='archived',archived_at='t' WHERE id='archived-owner'"
    )
    inaccessible = add_thread("inaccessible", "Inaccessible", user_id="other")
    _task(inaccessible.id, "Hidden", db=db)

    decision_plan = add_thread("decision", "Decision")
    source = _task(decision_plan.id, "Choose", db=db)
    decision_actor = PlanActor("decision", "user", "p")
    drafted = decisions.draft(
        decision_plan.id,
        source_node_id=source.id,
        title="Choose a path",
        context="The plan needs one path.",
        tradeoff="Speed versus certainty.",
        recommendation="A",
        rationale="A is reversible.",
        options=[
            {"id": "A", "label": "A", "detail": "Use A."},
            {"id": "B", "label": "B", "detail": "Use B."},
        ],
        other_enabled=True,
        mutation_id="activity-decision-draft",
        actor=decision_actor,
        provenance="test",
        db=db,
    ).result["decision"]
    reviewed = decisions.parent_review(
        drafted["id"],
        expected_revision=drafted["revision"],
        mutation_id="activity-decision-review",
        actor=decision_actor,
        provenance="test",
        db=db,
    ).result["decision"]
    decisions.raise_to_user(
        reviewed["id"],
        expected_revision=reviewed["revision"],
        mutation_id="activity-decision-raise",
        actor=decision_actor,
        provenance="test",
        db=db,
    )
    db.commit()

    full = operator_projection.read_project("p", actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full_by_id = {entry["plan"]["id"]: entry for entry in full["plans"]}
    compact_by_id = {entry["plan"]["id"]: entry for entry in compact["plans"]}
    assert compact["attention"] == full["attention"] == {"awaiting_count": 1}
    assert set(compact_by_id) == set(full_by_id) | {empty.id}
    assert compact_by_id[empty.id]["has_nodes"] is False
    assert compact_by_id[empty.id]["progress"] == {"open": 0, "liveness": "idle"}
    assert archived.id not in compact_by_id
    assert inaccessible.id not in compact_by_id

    labels = {row["owner"]["thread_id"]: row["display"]["label_key"] for row in compact["plans"]}
    expected_labels = {
        "ready": "waiting",  # Current dev: unselected work waits its turn.
        "waiting": "waiting",
        "paused": "waiting",
        "human-review": "needs_you",
        "parent-review": "working",
        "accepted": "finished",
        "raised-attention": "needs_you",
        "decision": "needs_you",
    }
    assert {key: labels[key] for key in expected_labels} == expected_labels
    for plan_id, row in compact_by_id.items():
        if plan_id == empty.id:
            continue
        detailed = full_by_id[plan_id]
        assert row["owner"] == detailed["owner"]
        assert row["display"] == {
            "label_key": detailed["display"]["label_key"],
            "tone_key": detailed["display"]["tone_key"],
        }
        assert row["progress"] == {
            "open": detailed["progress"]["open"],
            "liveness": detailed["progress"]["liveness"],
        }
        assert row["has_nodes"] == bool(detailed["nodes"])
        assert row["priority_at"] == _priority_at(detailed)


@pytest.mark.parametrize("scope", ["project", "workspace"])
def test_compact_scope_includes_empty_thread_but_not_structural_roots(db, scope):
    from plans import activity_projection, operator_projection

    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('empty','user','p','Empty','t','t')"""
    )
    empty = service.ensure_plan("thread", "empty", db=db)
    db.commit()

    if scope == "project":
        compact = activity_projection.read_project("p", actor=_actor(), db=db)
        full = operator_projection.read_project("p", actor=_actor(), db=db)
    else:
        compact = activity_projection.read_workspace(actor=_actor(), db=db)
        full = operator_projection.read_workspace(actor=_actor(), db=db)

    compact_by_id = {row["plan"]["id"]: row for row in compact["plans"]}
    full_ids = {row["plan"]["id"] for row in full["plans"]}
    assert empty.id not in full_ids
    assert compact_by_id[empty.id]["owner"] == {
        "label": "Empty",
        "project_id": "p",
        "thread_id": "empty",
    }
    assert compact_by_id[empty.id]["has_nodes"] is False
    assert compact_by_id[empty.id]["progress"] == {"open": 0, "liveness": "idle"}
    assert all(
        row["plan"]["owner_kind"] not in {"workspace_scope", "project_scope"}
        for row in compact["plans"]
    )


def test_compact_nested_sibling_local_order_matches_full_projection(db):
    from plans import activity_projection, operator_projection

    plan = service.ensure_plan("thread", "moving", db=db)
    created_at = "2026-01-01T00:00:00+00:00"
    db.executemany(
        """INSERT INTO plan_nodes
           (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,
            commitment_id,state,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("zz-phase", plan.id, None, 0, "phase", "Phase", "Phase done",
             "zz-phase", "pending", created_at, created_at),
            ("yy-track", plan.id, "zz-phase", 0, "track", "Track", "Track done",
             "zz-phase", "pending", created_at, created_at),
            ("aa-open", plan.id, "yy-track", 0, "task", "Open", "Open done",
             "zz-phase", "pending", created_at, created_at),
            ("bb-done", plan.id, "yy-track", 1, "task", "Done", "Done",
             "zz-phase", "accepted", created_at, created_at),
        ],
    )
    db.commit()

    full = operator_projection.read_thread("moving", actor=_actor(), db=db)["plans"][0]
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    compact_row = next(
        row for row in compact["plans"] if row["owner"]["thread_id"] == "moving"
    )

    assert compact_row["progress"] == {
        "open": full["progress"]["open"],
        "liveness": full["progress"]["liveness"],
    }
    assert compact_row["display"] == {
        "label_key": full["display"]["label_key"],
        "tone_key": full["display"]["tone_key"],
    }
    assert compact_row["priority_at"] == _priority_at(full)


@pytest.mark.parametrize("prerequisite_state", ["pending", "failed"])
def test_compact_dependency_quietness_tracks_scope_membership(
    db, prerequisite_state,
):
    from plans import activity_projection, hierarchy, operator_projection

    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p2','Other','t','t')"
    )
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('peer','user','p','Peer','t','t')"""
    )
    plan = service.ensure_plan("thread", "moving", db=db)
    peer = service.ensure_plan("thread", "peer", db=db)
    prerequisite = _task(peer.id, "Peer prerequisite", db=db)
    if prerequisite_state == "failed":
        service.transition_node(prerequisite.id, "failed", db=db)
    dependent = _task(plan.id, "Dependent", db=db)
    service.add_dependency(dependent.id, prerequisite.id, db=db)
    db.commit()

    expected_in_scope = "waiting"
    full = operator_projection.read_project("p", actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full_row = next(row for row in full["plans"] if row["owner"]["thread_id"] == "moving")
    compact_row = next(
        row for row in compact["plans"] if row["owner"]["thread_id"] == "moving"
    )
    assert compact_row["display"]["label_key"] == full_row["display"]["label_key"]
    assert compact_row["display"]["label_key"] == expected_in_scope
    assert compact_row["progress"] == {
        key: full_row["progress"][key] for key in ("open", "liveness")
    }
    assert compact_row["progress"]["liveness"] == "blocked"

    db.execute("UPDATE threads SET project_id='p2' WHERE id='peer'")
    hierarchy.rehome("peer", db=db)
    db.commit()

    full = operator_projection.read_project("p", actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full_row = next(row for row in full["plans"] if row["owner"]["thread_id"] == "moving")
    compact_row = next(
        row for row in compact["plans"] if row["owner"]["thread_id"] == "moving"
    )
    dependent_row = next(node for node in full_row["nodes"] if node["id"] == dependent.id)
    assert dependent_row["display"]["label_key"] == "waiting"
    assert compact_row["display"] == {
        "label_key": full_row["display"]["label_key"],
        "tone_key": full_row["display"]["tone_key"],
    } == {"label_key": "waiting", "tone_key": "waiting"}

    full = operator_projection.read_workspace(actor=_actor(), db=db)
    compact = activity_projection.read_workspace(actor=_actor(), db=db)
    full_row = next(row for row in full["plans"] if row["owner"]["thread_id"] == "moving")
    compact_row = next(
        row for row in compact["plans"] if row["owner"]["thread_id"] == "moving"
    )
    assert compact_row["display"]["label_key"] == full_row["display"]["label_key"]
    assert compact_row["display"]["label_key"] == expected_in_scope
    assert compact_row["progress"] == {
        key: full_row["progress"][key] for key in ("open", "liveness")
    }
    assert compact_row["progress"]["liveness"] == "blocked"


@pytest.mark.parametrize("kind", ["paused", "blocked"])
def test_compact_held_parent_with_active_child_matches_full(db, kind):
    from plans import activity_projection, operator_projection

    _seed(db)
    ordinary = db.execute(
        "SELECT id FROM plan_nodes WHERE title='Run'"
    ).fetchone()
    service.transition_node(str(ordinary["id"]), "closed", db=db)
    child_task = db.execute(
        "SELECT id FROM plan_nodes WHERE title='Child task'"
    ).fetchone()
    admission.select(str(child_task["id"]), actor_id="child", provenance="test", db=db)
    parent = db.execute(
        "SELECT * FROM plan_nodes WHERE spawned_thread_id='child'"
    ).fetchone()
    holds.place_hold(
        str(parent["id"]),
        kind=kind,
        reason_key=f"user_{kind}",
        reason=f"User {kind} the delegated task",
        owner_kind="user",
        owner_id="user",
        expected_revision=int(parent["revision"]),
        actor_id="user",
        provenance="test.activity_index",
        db=db,
    )
    db.commit()

    full = operator_projection.read_project("p", actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full_row = next(row for row in full["plans"] if row["owner"]["thread_id"] == "moving")
    compact_row = next(
        row for row in compact["plans"] if row["owner"]["thread_id"] == "moving"
    )

    assert compact_row["display"] == {
        "label_key": full_row["display"]["label_key"],
        "tone_key": full_row["display"]["tone_key"],
    } == {"label_key": "waiting", "tone_key": "waiting"}


def test_compact_child_reattachment_churn_matches_full_newest_owner(db):
    from plans import activity_projection, operator_projection

    _seed(db)
    parent_plan = db.execute(
        "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id='moving'"
    ).fetchone()
    db.execute(
        "UPDATE plan_nodes SET state='closed' WHERE plan_id=? AND spawned_thread_id='child'",
        (str(parent_plan["id"]),),
    )
    replacement = _task(
        str(parent_plan["id"]), "Delegated again", db=db, spawn_kind="claude-code",
    )
    service.transition_node(replacement.id, "active", db=db)
    mutations.attach_spawned_thread(replacement.id, "child", db=db)
    db.commit()

    full = operator_projection.read_project("p", actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full_row = next(row for row in full["plans"] if row["owner"]["thread_id"] == "moving")
    compact_row = next(row for row in compact["plans"] if row["owner"]["thread_id"] == "moving")
    attached = [node for node in full_row["nodes"] if node["child"] is not None]

    assert [node["id"] for node in attached] == [replacement.id]
    assert compact_row["display"] == {
        "label_key": full_row["display"]["label_key"],
        "tone_key": full_row["display"]["tone_key"],
    }
    assert compact_row["progress"] == {
        "open": full_row["progress"]["open"],
        "liveness": full_row["progress"]["liveness"],
    }


@pytest.mark.parametrize(
    ("kind", "label", "bucket", "needs_user"),
    [
        ("human_decision", "needs_you", "review_human", True),
        ("human_review", "needs_you", "review_human", True),
        ("parent_agent", "working", "review_parent_agent", False),
        ("external", "waiting", "review_parent_agent", False),
    ],
)
def test_compact_pure_review_reducers_cover_every_review_kind(
    kind, label, bucket, needs_user,
):
    from plans import operator_node_state

    display = operator_node_state.display_from_facts(
        "review_requested", "review", kind, needs_user=needs_user,
    )
    assert display["label_key"] == label
    assert operator_node_state.leaf_bucket_from_facts(
        "review_requested", "review", kind, display_label=display["label_key"],
    ) == bucket


def test_compact_and_full_representations_have_independent_tokens(db):
    from plans import activity_projection, operator_projection

    _seed(db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full = operator_projection.read_project("p", actor=_actor(), db=db)

    assert compact["revision_token"] != full["revision_token"]
    assert full["contract_version"] == 2
    assert "representation" not in full


class _DbProxy:
    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None


async def _immediate(callable_):
    return callable_()


def test_compact_http_routes_are_authenticated_etag_aware_and_separate(db, monkeypatch):
    from api import plans as api_plans

    _seed(db)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('empty-project','Empty','t','t')"
    )
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('empty-member','user','empty-project','Empty member','t','t')"""
    )
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('secret','Secret','t','t')"
    )
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('secret-member','other','secret','Secret member','t','t')"""
    )
    service.ensure_plan("project_scope", "secret", db=db)
    db.commit()
    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)

    with TestClient(app) as anonymous:
        assert anonymous.get("/api/plans/activity/scopes/projects/p").status_code == 401

    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user"}
    with TestClient(app) as client:
        first = client.get("/api/plans/activity/scopes/projects/p")
        assert first.status_code == 200
        assert first.json()["representation"] == "activity_scope_index"
        unchanged = client.get(
            "/api/plans/activity/scopes/projects/p",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        moving_plan = db.execute(
            "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id='moving'"
        ).fetchone()
        _task(str(moving_plan["id"]), "New compact fact", db=db)
        db.commit()
        changed = client.get(
            "/api/plans/activity/scopes/projects/p",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert changed.status_code == 200
        assert changed.headers["etag"] != first.headers["etag"]
        full = client.get("/api/plans/operator/scopes/projects/p")
        assert full.status_code == 200
        assert full.headers["etag"] != first.headers["etag"]
        workspace = client.get("/api/plans/activity/scopes/workspace")
        assert workspace.status_code == 200
        empty = client.get("/api/plans/activity/scopes/projects/empty-project")
        assert empty.status_code == 200
        assert empty.json()["plans"] == []
        assert empty.json()["attention"] == {"awaiting_count": 0}
        hidden = client.get("/api/plans/activity/scopes/projects/secret")
        assert hidden.status_code == 404
        assert "secret" not in hidden.text.lower()

    routes = {route.path for route in api_plans.router.routes}
    assert routes >= {
        "/api/plans/activity/scopes/workspace",
        "/api/plans/activity/scopes/projects/{project_id}",
    }
