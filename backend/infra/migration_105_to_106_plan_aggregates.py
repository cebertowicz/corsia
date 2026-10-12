"""Schema 105 -> 106: stored subtree aggregates on plan nodes and plans (#731).

Adds the aggregate columns and their indexes, narrows the plan revision trigger so
aggregate writes cannot bump a plan, backfills ``sent_back`` from the
``review_rejected`` events, then computes every plan's aggregates bottom-up and
asserts them equal to an independent recount before the runner commits. A plan whose
aggregates differ from the recount fails the whole migration; nothing is repaired
silently.

``plans.revision`` and ``plans.updated_at`` must not move: the trigger is narrowed
before the backfill, and the migration compares both before and after. Runs inside
the runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3

from infra.plan_aggregates_schema import (
    NODE_AGGREGATE_COLUMNS, NODE_AGGREGATE_INDEXES, NODE_UPDATE_TRIGGER_SQL, PLAN_AGGREGATE_COLUMNS,
    TOUCH_INDEXES,
)


class PlanAggregatesMigrationError(RuntimeError):
    """The backfilled aggregates disagree with the recount, or a plan's revision moved."""


def _add_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def prepare(conn: sqlite3.Connection) -> None:
    """Add the columns and indexes, narrow the trigger, and backfill the row facts.

    The trigger is narrowed before any row is written, so the backfill cannot
    inflate a plan's revision.
    """
    _add_columns(conn, "plans", PLAN_AGGREGATE_COLUMNS)
    _add_columns(conn, "plan_nodes", NODE_AGGREGATE_COLUMNS)
    conn.execute("DROP TRIGGER IF EXISTS bump_plan_revision_node_update")
    conn.execute(NODE_UPDATE_TRIGGER_SQL)
    conn.execute(
        "UPDATE plan_nodes SET sent_back=1 WHERE state='closed' AND EXISTS "
        "(SELECT 1 FROM plan_node_events e WHERE e.node_id=plan_nodes.id AND e.event='review_rejected')"
    )
    conn.execute(
        "UPDATE plan_nodes SET own_last_changed=COALESCE("
        "(SELECT MAX(created_at) FROM plan_node_events e WHERE e.node_id=plan_nodes.id), updated_at)"
    )
    for statement in (*NODE_AGGREGATE_INDEXES.values(), *TOUCH_INDEXES.values()):
        conn.execute(statement)


def backfill(conn: sqlite3.Connection) -> list[str]:
    """Compute every plan's aggregates and return the plans whose recount differs (ids)."""
    from plans import public

    differing: list[str] = []
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        for (plan_id,) in conn.execute("SELECT id FROM plans").fetchall():
            public.rebuild_plan_aggregates(conn, plan_id)
            if public.plan_aggregate_mismatches(conn, plan_id):
                differing.append(str(plan_id))
    finally:
        conn.row_factory = caller_factory
    return differing


def _revisions(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT id, revision, updated_at FROM plans")}


def migrate_105_to_106(conn: sqlite3.Connection) -> None:
    """Create the aggregates a fresh schema has, then prove them against a recount."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"plans", "plan_nodes"} <= tables:
        return
    before = _revisions(conn)
    prepare(conn)
    differing = backfill(conn)
    if differing:
        raise PlanAggregatesMigrationError(
            f"{len(differing)} plan(s) have stored aggregates that differ from the recount"
        )
    if before != _revisions(conn):
        raise PlanAggregatesMigrationError("the backfill moved a plan's revision or updated_at")
