"""Startup retirement of rows whose task finished without retiring them.

Until only shared-tree documents outlived their task, every durable workspace
file did, a thread's own folder included, so settlement skipped those rows and
fires at most once per node. This pass gives each finished node the settlement
it would get today, through the shared retirement writer: every row is
audited, a pinned row is stamped but stays, and pin or the next write brings a
row back.

It runs once per instance, and again on the start after a task settlement that
could not classify its rows (``startup_backfill.settlement_retirement``): those
rows are the same kind, left behind by a node that has already finished.
"""

from __future__ import annotations

from collections import Counter

from infra.database import write_transaction
from plans.public import TERMINAL_STATES
from registry import node_retirement, startup_backfill
from registry.retirement_predicate import (
    RETIREMENT_NOT_EXEMPT_CLAUSE,
    bind_shared_documents,
)

_MARKER = startup_backfill.FINISHED_TASK_MARKER
_TRIGGER = "finished_task_backfill"

_TERMINAL_SQL = ", ".join(f"'{state}'" for state in sorted(TERMINAL_STATES))

# Unretired, no longer exempt, and bound to a node that has already finished:
# terminal, archived, or gone.
_LEFT_BEHIND = f"""
    SELECT r.origin_plan_node_id AS node_id, r.parent_thread_id
      FROM artifact_registrations AS r
      LEFT JOIN plan_nodes AS n ON n.id=r.origin_plan_node_id
     WHERE r.origin_plan_node_id IS NOT NULL AND r.retired_at IS NULL
       AND {RETIREMENT_NOT_EXEMPT_CLAUSE}
       AND (n.id IS NULL OR n.archived_at IS NOT NULL
            OR n.state IN ({_TERMINAL_SQL}))
     ORDER BY r.origin_plan_node_id, r.parent_thread_id"""


def retire_finished_task_rows(db) -> dict[str, int]:
    """Settle the rows finished nodes left behind; return the count per thread."""
    with write_transaction(db, scope="retire_finished_task_rows") as connection:
        bind_shared_documents(connection)
        rows = connection.execute(_LEFT_BEHIND).fetchall()
        retired = Counter(str(row["parent_thread_id"]) for row in rows)
        for node_id in dict.fromkeys(str(row["node_id"]) for row in rows):
            node_retirement.retire_registrations_for_node(
                connection, node_id=node_id, trigger=_TRIGGER, causal_event_id=None,
            )
    return dict(retired)


def run_startup_backfill() -> None:
    """Run the pass unless its marker says it already ran."""
    startup_backfill.run_once(_MARKER, "finished-task rows", retire_finished_task_rows)
