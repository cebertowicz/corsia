"""Bounded thread-plan task pages retain canonical SQL order semantics."""

from __future__ import annotations

import sqlite3

from infra.schema import SCHEMA_SQL
from plans import node_order, selection_validity, service
from tests.plan_writer_authorizer import raw_plan_seed


def _db() -> sqlite3.Connection:
    """Return one schema-complete in-memory database for page-order tests."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('owner','user','t','t')",
    )
    return db


def _node(
    db,
    plan_id: str,
    kind: str,
    title: str,
    order_key: int,
    *,
    parent_id: str | None = None,
    assignee: str | None = None,
):
    """Add a node and pin the requested canonical sibling position."""
    node = service.add_node(
        plan_id,
        kind=kind,
        title=title,
        acceptance_criteria=f"{title} is verified",
        parent_id=parent_id,
        assignee=assignee,
        db=db,
    )
    db.execute("UPDATE plan_nodes SET order_key=? WHERE id=?", (order_key, node.id))
    return node


def _active_task(db, plan_id: str, title: str, order_key: int, **kwargs):
    """Add one active task at an explicit canonical sibling position."""
    task = _node(db, plan_id, "task", title, order_key, **kwargs)
    service.transition_node(task.id, "active", db=db)
    return task


def _page_ids(db, plan_id: str) -> list[str]:
    """Read all bounded pages without deciding any task's eligibility."""
    return [
        str(row["id"])
        for page in node_order.iter_active_task_pages(db, plan_id)
        for row in page
    ]


def test_active_task_pages_match_depth_first_through_hierarchy_ties_and_stranding():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        phase = _node(db, plan.id, "phase", "First phase", 0)
        first = _active_task(db, plan.id, "First child", 0, parent_id=phase.id)
        tied_left = _active_task(db, plan.id, "Tied left", 1, parent_id=phase.id)
        tied_right = _active_task(db, plan.id, "Tied right", 1, parent_id=phase.id)
        second = _active_task(db, plan.id, "Second root", 1)
        retired = _node(db, plan.id, "phase", "Retired phase", 2)
        stranded = _active_task(db, plan.id, "Stranded child", 0, parent_id=retired.id)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (retired.id,))
        db.commit()

        expected = [
            first.id,
            *(sorted((tied_left.id, tied_right.id))),
            second.id,
            stranded.id,
        ]
        assert _page_ids(db, plan.id) == expected
    finally:
        db.close()


def test_active_task_pages_have_fixed_parameters_and_bounded_result_rows():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        for index in range(node_order.ACTIVE_TASK_PAGE_SIZE + 1):
            _active_task(db, plan.id, f"Task {index}", index)
        db.commit()
        calls: list[tuple[str, tuple]] = []

        class RecordingDB:
            """Expose the page query's exact parameter shape."""

            def execute(self, sql, parameters=()):
                calls.append((sql, tuple(parameters)))
                return db.execute(sql, parameters)

        pages = list(node_order.iter_active_task_pages(RecordingDB(), plan.id))

        page_calls = [(sql, params) for sql, params in calls if "live AS (" in sql]
        assert [len(rows) for rows in pages] == [node_order.ACTIVE_TASK_PAGE_SIZE, 1]
        assert [len(params) for _sql, params in page_calls] == [3]
        assert page_calls[0][1] == (plan.id, plan.id, 1)
    finally:
        db.close()


def test_first_eligible_active_id_scans_over_64_ineligible_tasks_in_bounded_batches(monkeypatch):
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        prerequisite = _node(db, plan.id, "task", "Pending prerequisite", 0)
        for index in range(node_order.ACTIVE_TASK_PAGE_SIZE * 2):
            held = _active_task(db, plan.id, f"Held {index}", index + 1)
            service.add_dependency(held.id, prerequisite.id, db=db)
        _active_task(
            db,
            plan.id,
            "User-owned",
            node_order.ACTIVE_TASK_PAGE_SIZE * 2 + 1,
            assignee="user",
        )
        parent = _active_task(
            db,
            plan.id,
            "Parent task",
            node_order.ACTIVE_TASK_PAGE_SIZE * 2 + 2,
        )
        child = _node(db, plan.id, "task", "Pending child", 0)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET parent_id=? WHERE id=?", (parent.id, child.id))
        eligible = _active_task(
            db,
            plan.id,
            "First eligible",
            node_order.ACTIVE_TASK_PAGE_SIZE * 2 + 3,
        )
        db.commit()
        batches: list[int] = []
        real_eligible_ids = selection_validity.eligible_ids

        def capture_eligibility(connection, node_plans):
            batches.append(len(node_plans))
            return real_eligible_ids(connection, node_plans)

        monkeypatch.setattr(selection_validity, "eligible_ids", capture_eligibility)
        calls: list[tuple[str, tuple]] = []

        class RecordingDB:
            def execute(self, sql, parameters=()):
                calls.append((sql, tuple(parameters)))
                return db.execute(sql, parameters)

        plan_row = service._plan_row(db, plan.id)
        assert selection_validity.first_eligible_active_id(RecordingDB(), plan_row) == eligible.id
        assert batches == [node_order.ACTIVE_TASK_PAGE_SIZE] * 2 + [3]
        page_queries = [(sql, params) for sql, params in calls if "live AS (" in sql]
        assert len(page_queries) == 1
        assert page_queries[0][1] == (plan.id, plan.id, 1)
    finally:
        db.close()
