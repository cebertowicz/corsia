"""Batched historical review facts stay scoped, complete, and read-only."""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import closure, lesson_reviews, public, service
from tests.plan_writer_authorizer import raw_plan_seed


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    connection.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES('project','Project','t','t')")
    for owner in ("owner", "other"):
        connection.execute(
            "INSERT INTO threads(id,user_id,project_id,model,created_at,updated_at) VALUES(?,'user','project','test/model','t','t')",
            (owner,),
        )
    connection.execute(
        """INSERT INTO artifact_identities(id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES('artifact','workspace','skill','workspace://artifact','Lesson','active','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def review(db, *, owner="owner", kind="lesson", subject="subject", sha="a" * 64):
    shared = dict(artifact_id="artifact", title="Review", acceptance="Record a disposition", review_after="2030-01-01T00:00:00Z")
    if kind == "lesson":
        return lesson_reviews.ensure_lesson_review(db, owner, lesson_id=subject, behavioral_sha256=sha, **shared)[0]
    return lesson_reviews.ensure_capture_review(db, owner, capture_id=subject, capture_hash=sha, **shared)[0]


def history(db, *owners):
    return public.lesson_review_history(db, owner_thread_ids=owners or ("owner",))


def test_settled_and_rereviewed_version_is_two_nodes_under_one_key(db):
    first = review(db)
    closure.close_node(first, reason="Evidence insufficient; retire", db=db)
    second = review(db)
    db.commit()
    db.execute("PRAGMA query_only=ON")
    rows = history(db)
    assert [row["node_id"] for row in rows] == [first, second]
    assert len({row["key"] for row in rows}) == 1
    assert [row["open"] for row in rows] == [False, True]
    assert rows[0]["close_reason"] == "Evidence insufficient; retire"
    assert rows[0]["settled_at"]
    assert rows[0]["scheduled_for"] is None
    assert rows[1]["scheduled_for"] == "2030-01-01T00:00:00+00:00"
    assert rows[1]["subject_id"] == "subject"
    assert rows[1]["subject_hash"] == "a" * 64


@pytest.mark.parametrize("reason,typed", [
    (lesson_reviews.SUPERSEDED_BY_VERSION, "version"),
    (lesson_reviews.SUPERSEDED_BY_TRIAL, "trial"),
    (lesson_reviews.SUPERSEDED_BY_CAPTURE, "capture"),
    (lesson_reviews.SUPERSEDED_BY_OWNER, "owner"),
])
def test_superseded_reason_is_typed_by_plans(db, reason, typed):
    node = review(db)
    closure.close_node(node, reason=reason, db=db)
    assert history(db)[0]["superseded"] == typed


def test_closed_archived_nodes_archived_plan_and_missing_owner_remain_visible(db):
    node = review(db, kind="capture")
    closure.close_node(node, reason="Retired", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='2026-10-01T00:00:00Z' WHERE id=?", (node,))
    db.execute("UPDATE plans SET state='archived' WHERE owner_id='owner'")
    db.execute("DELETE FROM threads WHERE id='owner'")
    [row] = history(db)
    assert row["kind"] == "capture"
    assert row["archived_at"] == "2026-10-01T00:00:00Z"
    assert row["plan_state"] == "archived"
    assert row["owner_missing"] is True
    assert row["open"] is False


def test_resolved_owner_tuple_is_required_and_deduplicated(db):
    for scopes in ((), ("",), ("all",), ["owner"]):
        with pytest.raises(ValueError, match="resolved owner"):
            public.lesson_review_history(db, owner_thread_ids=scopes)
    assert history(db) == []
    node = review(db)
    assert [row["node_id"] for row in history(db, "owner", "owner")] == [node]


def test_report_reads_over_two_hundred_reviews_without_losing_the_tail(db):
    nodes = [review(db, subject=f"lesson-{index}") for index in range(201)]
    db.commit()
    db.execute("PRAGMA query_only=ON")
    rows = history(db)
    assert {row["node_id"] for row in rows} == set(nodes)
    assert len(rows) == 201


def test_subject_id_with_colons_and_malformed_key_preserve_raw_identity(db):
    first = review(db, subject="subject:with:colons")
    second = review(db, subject="malformed")
    db.execute("UPDATE plan_node_artifact_links SET source_event_id='lesson:broken' WHERE node_id=?", (second,))
    rows = {row["node_id"]: row for row in history(db)}
    assert rows[first]["subject_id"] == "subject:with:colons"
    assert rows[first]["subject_hash"] == "a" * 64
    assert rows[second]["key"] == "lesson:broken"
    assert rows[second]["subject_id"] is None
    assert rows[second]["subject_hash"] is None


def test_batch_query_plan_never_scans_the_links_table(db):
    from plans import lesson_review_history as reader

    review(db)
    plan = service.ensure_plan("thread", "other", db=db)
    for number in range(100):
        node = service.add_node(plan.id, kind="task", title="Feedback", acceptance_criteria="Resolve", db=db)
        db.execute(
            """INSERT INTO plan_node_artifact_links(node_id,artifact_id,role,provenance,source_event_id,created_at)
               VALUES(?,'artifact','input','feedback',?,'t')""", (node.id, str(number)),
        )
    parameters = (lesson_reviews.PROVENANCE, "owner", 7, "lesson:", 8, "capture:")
    details = [row["detail"] for row in db.execute(
        "EXPLAIN QUERY PLAN " + reader._batch_history_query(1), parameters,
    )]
    assert not any("SCAN l" in line or "SCAN plan_node_artifact_links" in line for line in details), details
    assert any("SEARCH l USING PRIMARY KEY (node_id=?)" in line for line in details), details
    assert any("SEARCH n USING INDEX idx_plan_nodes_tree (plan_id=?)" in line for line in details), details


def test_batch_reader_keeps_terminal_states_tied_links_and_latest_armed_watch(db):
    states = ("accepted", "accepted_with_followup", "failed", "closed")
    nodes = [review(db, subject=state) for state in states]
    settled_at = {
        node: f"2026-09-2{index}T00:00:00Z"
        for index, node in enumerate(nodes)
    }
    for node, state in zip(nodes, states):
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (state, node))
        db.execute(
            "INSERT INTO plan_node_events(id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES(?,?, 'test.batch', 'state_transition', 'active', ?, ?)",
            (uuid.uuid4().hex, node, state, settled_at[node]),
        )
    # Same timestamp, later rowid: the selected settled timestamp remains exact.
    db.execute(
        "INSERT INTO plan_node_events(id,node_id,provenance,event,prior_state,new_state,created_at) "
        "VALUES(?,?, 'test.batch', 'state_transition', 'active', 'accepted', ?)",
        (uuid.uuid4().hex, nodes[0], settled_at[nodes[0]]),
    )
    open_node = review(db, subject="open")
    linked_at = {
        nodes[0]: "2026-09-10T00:00:00Z", nodes[1]: "2026-09-10T00:00:00Z",
        nodes[2]: "2026-09-10T00:00:01Z", nodes[3]: "2026-09-10T00:00:02Z",
        open_node: "2026-09-10T00:00:03Z",
    }
    for node, value in linked_at.items():
        db.execute("UPDATE plan_node_artifact_links SET created_at=? WHERE node_id=?", (value, node))
    db.execute(
        """INSERT INTO artifact_identities(id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES('artifact-a','workspace','skill','workspace://artifact-a','A','active','t','t'),
                 ('artifact-b','workspace','skill','workspace://artifact-b','B','active','t','t')"""
    )
    for artifact, role in (("artifact-a", "input"), ("artifact-b", "result")):
        db.execute(
            "INSERT INTO plan_node_artifact_links(node_id,artifact_id,role,provenance,source_event_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (open_node, artifact, role, lesson_reviews.PROVENANCE,
             f"lesson:alternate:{'b' * 64}", linked_at[open_node]),
        )
    plan_id = db.execute("SELECT plan_id FROM plan_nodes WHERE id=?", (open_node,)).fetchone()["plan_id"]
    watches = (
        ("resolved", "2031-09-30T00:00:00Z", "2032-01-01T00:00:00Z", "2031-09-30T00:01:00Z"),
        ("armed", "2031-10-01T00:00:00Z", "2032-01-02T00:00:00Z", None),
        ("armed", "2031-10-01T00:00:00Z", "2032-01-03T00:00:00Z", None),
    )
    for status, armed, condition, resolved in watches:
        db.execute(
            "INSERT INTO expected_event_watches(id,plan_id,plan_node_id,condition_type,condition_value,expected_event,status,armed_at,resolved_at,created_at,updated_at) "
            "VALUES(?,?,?,'deadline',?,'scheduled',?,?, ?,?,?)",
            (uuid.uuid4().hex, plan_id, open_node, condition, status, armed, resolved, armed, armed),
        )

    rows = history(db)
    by_node = {row["node_id"]: row for row in rows}
    assert len(rows) == len(nodes) + 1
    assert set(by_node) == {*nodes, open_node}
    assert [row["node_id"] for row in rows] == [
        *sorted(nodes[:2]), nodes[2], nodes[3], open_node,
    ]
    for node, state in zip(nodes, states):
        assert by_node[node]["state"] == state
        assert by_node[node]["open"] is False
        assert by_node[node]["settled_at"] == settled_at[node]
        assert by_node[node]["scheduled_for"] is None
    # The original artifact wins the created_at/artifact_id/role tie deterministically.
    assert by_node[open_node]["key"] == lesson_reviews.lesson_key("open", "a" * 64)
    assert by_node[open_node]["artifact_id"] == "artifact"
    assert by_node[open_node]["linked_at"] == linked_at[open_node]
    assert by_node[open_node]["open"] is True
    assert by_node[open_node]["settled_at"] is None
    assert by_node[open_node]["scheduled_for"] == "2032-01-03T00:00:00Z"


def test_batch_reader_preserves_explicit_owner_order_missing_owner_and_history_facts(db):
    owner_nodes = [review(db, subject=f"owner-{number}") for number in range(201)]
    for number, node in enumerate(owner_nodes):
        db.execute(
            "UPDATE plan_node_artifact_links SET created_at=? WHERE node_id=?",
            (f"2026-09-01T00:00:00.{number:03d}Z", node),
        )
    other_node = review(db, owner="other", subject="other")
    closure.close_node(owner_nodes[0], reason="Retired", db=db)
    db.execute("DELETE FROM threads WHERE id='other'")
    rows = history(db, "other", "owner", "other")
    assert [row["node_id"] for row in rows] == [other_node, *owner_nodes]
    assert [row["owner_id"] for row in rows] == ["other", *(["owner"] * len(owner_nodes))]
    assert rows[0]["owner_missing"] is True
    first = next(row for row in rows if row["node_id"] == owner_nodes[0])
    assert first["close_reason"] == "Retired" and first["settled_at"]
