"""One meaning of "open" for the pane and the chat list, and asks that read the open work (#731 S2-2).

The pane lists the rows ``node_states.listed_open_sql`` names; the chat list asks the same
question to say whether a plan has open work. The decision count and the failed-work ask
read through indexes, so a plan's settled history is never in their way.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import decision_projection, mutations, node_states, operator_chat_state, operator_projection, service
from plans.boundary import PlanActor
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_attention_queue import _seed, raise_decision

HUMAN = PlanActor(None, "user-1", None, kind="user")


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


def _add(db, plan_id: str, title: str, *, kind: str = "task", parent: str | None = None, **kwargs) -> str:
    node = service.add_node(
        plan_id, kind=kind, title=title, acceptance_criteria="done", parent_id=parent, db=db, **kwargs,
    )
    db.commit()
    return node.id


def _accept(db, node_id: str) -> None:
    service.transition_node(node_id, "active", db=db)
    mutations.request_review(node_id, evidence="proven", actor_id="root", db=db)
    service.transition_node(node_id, "accepted", verification_result="passed", verified_by="t", db=db)
    db.commit()


def _fail(db, node_id: str) -> None:
    service.transition_node(node_id, "active", db=db)
    service.transition_node(node_id, "failed", db=db)
    db.commit()


def _pane_has_open(db, thread_id: str) -> bool:
    envelope = operator_projection.read_thread(thread_id, actor=HUMAN, db=db)
    return any(not node["folded"] for plan in envelope["plans"] for node in plan["nodes"])


def _chat_has_open(db, thread_id: str) -> bool:
    return bool(operator_chat_state._plan_rows([thread_id], db=db)[0]["has_open"])


def _container(db, plan_id: str, children: int) -> tuple[str, list[str]]:
    phase = _add(db, plan_id, "Phase", kind="phase")
    return phase, [_add(db, plan_id, f"Child {i}", parent=phase) for i in range(children)]


def _settled_children(db, plan_id: str) -> None:
    _, kids = _container(db, plan_id, 2)
    for kid in kids:
        _accept(db, kid)


def _one_child_still_pending(db, plan_id: str) -> None:
    _, kids = _container(db, plan_id, 2)
    _accept(db, kids[0])


def _pending_child_archived(db, plan_id: str) -> None:
    _, kids = _container(db, plan_id, 2)
    _accept(db, kids[0])
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='2026-01-01T00:00:00+00:00' WHERE id=?", (kids[1],))
    db.commit()


def _a_failed_child(db, plan_id: str) -> None:
    _, kids = _container(db, plan_id, 2)
    _accept(db, kids[0])
    _fail(db, kids[1])


def _only_a_pending_phase(db, plan_id: str) -> None:
    _container(db, plan_id, 0)


@pytest.mark.parametrize(("build", "is_open"), [
    (_settled_children, False),
    (_one_child_still_pending, True),
    (_pending_child_archived, False),
    (_a_failed_child, False),
    (_only_a_pending_phase, True),
])
def test_the_pane_and_the_chat_list_agree_on_open_work(db, build, is_open: bool) -> None:
    """A pending container whose children have all settled is one case among them: neither read has it open."""
    plan_id = service.ensure_plan("thread", "root", db=db).id
    build(db, plan_id)

    assert _pane_has_open(db, "root") is is_open
    assert _chat_has_open(db, "root") is is_open


def test_the_open_rule_is_the_one_the_pane_lists_rows_by(db) -> None:
    plan_id = service.ensure_plan("thread", "root", db=db).id
    _settled_children(db, plan_id)
    _one = _add(db, plan_id, "Still open")

    listed = {row["id"] for row in db.execute(
        f"SELECT id FROM plan_nodes n WHERE n.plan_id=? AND {node_states.listed_open_sql('n')}",
        (plan_id,),
    )}

    assert listed == {_one}
    assert _chat_has_open(db, "root") and _pane_has_open(db, "root")


def _plan_detail(db, sql: str, params: tuple) -> str:
    return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_the_decision_count_reads_the_open_rows_through_the_open_index(db) -> None:
    raise_decision(db, "root", "root")
    plan_id = service.ensure_plan("thread", "root", db=db).id
    _settled_children(db, plan_id)

    detail = _plan_detail(
        db, decision_projection._rows_sql("?", open_nodes=True), (decision_projection.DECISION_TYPE, plan_id),
    )
    assert "idx_plan_nodes_open" in detail, detail
    assert "idx_plan_nodes_tree" not in detail, detail


def test_the_decision_count_is_what_the_full_scan_counts(db) -> None:
    raise_decision(db, "root", "root")
    raise_decision(db, "child", "child")
    plan_id = service.ensure_plan("thread", "root", db=db).id
    _settled_children(db, plan_id)
    plan_ids = [row["id"] for row in db.execute("SELECT id FROM plans")]

    scanned = decision_projection.raised_decisions(plan_ids, db=db)

    assert len(scanned) == 2
    assert decision_projection.awaiting_count([plan_id, service.ensure_plan("thread", "child", db=db).id], db=db) == 2
    assert decision_projection.raised_decisions(plan_ids, db=db, open_nodes=True) == scanned


def _user_failure(db, plan_id: str, title: str) -> str:
    node = _add(db, plan_id, title, assignee="user")
    _fail(db, node)
    return node


def _watched(db, thread_id: str, seen_through: str) -> None:
    db.execute(
        "INSERT INTO plan_pane_views (user_id,thread_id,seen_through,updated_at) VALUES (?,?,?,?)",
        ("user-1", thread_id, seen_through, seen_through),
    )
    db.commit()


def _failed(db, plan_id: str) -> list[str]:
    found = operator_chat_state._failed_nodes([plan_id], actor=HUMAN, db=db)
    return [item["node_id"] for item in found.get(plan_id, [])]


def test_a_failure_the_viewer_has_not_seen_is_asked_and_a_seen_one_is_not(db) -> None:
    plan_id = service.ensure_plan("thread", "root", db=db).id
    old = _user_failure(db, plan_id, "Old failure")
    _watched(db, "root", "9999-01-01T00:00:00+00:00")
    assert _failed(db, plan_id) == []

    db.execute("UPDATE plan_pane_views SET seen_through='2000-01-01T00:00:00+00:00'")
    fresh = _user_failure(db, plan_id, "New failure")
    assert _failed(db, plan_id) == [old, fresh]

    retry = _add(db, plan_id, "Retry")
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (old, retry))
    db.commit()
    assert _failed(db, plan_id) == [fresh]


def test_the_failed_ask_reads_the_users_failures_through_their_index(db) -> None:
    plan_id = service.ensure_plan("thread", "root", db=db).id
    _user_failure(db, plan_id, "Failure")
    _watched(db, "root", "2000-01-01T00:00:00+00:00")
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    _failed(db, plan_id)
    db.set_trace_callback(None)
    asked = next(sql for sql in statements if "plan_pane_views" in sql and "SELECT n.plan_id" in sql)

    detail = _plan_detail(db, asked, ())

    assert "idx_plan_nodes_user_failed" in detail, detail
    assert "idx_plan_nodes_tree" not in detail, detail
