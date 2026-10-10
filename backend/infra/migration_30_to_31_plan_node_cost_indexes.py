"""Schema 30 -> 31: index ``plan_node_id`` on ``llm_usage`` and ``active_turns`` (#2090).

``plans.node_cost.project_many`` filters both tables on ``plan_node_id IN
(...)`` at every attached-plan depth, and neither table had an index leading
with that column, so each call was a full table scan. Purely additive: two
partial indexes, scoped to the plan-attributed rows since most usage and
Turn rows carry no plan attribution at all.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_INDEX_30_TO_31_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_plan_node"
    " ON llm_usage(plan_node_id) WHERE plan_node_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_active_turns_plan_node"
    " ON active_turns(plan_node_id) WHERE plan_node_id IS NOT NULL",
)


def migrate_30_to_31(conn: sqlite3.Connection) -> None:
    """Add the ``plan_node_id`` indexes the plan cost roll-up reads through."""
    for statement in _INDEX_30_TO_31_DDL:
        conn.execute(statement)
