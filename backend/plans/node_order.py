"""The one canonical SQL depth-first order of one plan's live nodes."""

from __future__ import annotations

import json
import logging
from typing import Any

import plans.admission_projection as admission_projection
from infra.packed_rows import fetch_all
from plans import aggregates, node_states

logger = logging.getLogger(__name__)

#: Task states in which the work still belongs to its executor.
OPEN_TASK_STATES = frozenset({"pending", "active"})

#: Fixed Python result bound for one thread-admission eligibility batch.
ACTIVE_TASK_PAGE_SIZE = 32

#: One traversal powers every canonical plan-order reader. ``live`` projects
#: only fields needed by ordered admission and compact next-work consumers;
#: prose joins only through ``_FULL_ORDERED_SQL`` for explicit full-row reads.
_LIVE_COLUMNS = """id,parent_id,order_key,kind,state,archived_at,assignee,
               executor_thread_id,owner_thread_id,requirements_revision,
               revision,title"""

_LIVE_ALL_SQL = f"""    live AS (
        SELECT {_LIVE_COLUMNS}
        FROM plan_nodes
        WHERE plan_id=? AND archived_at IS NULL
"""

#: Only the open nodes and their unarchived ancestors. ``scope`` climbs by
#: parent id, so the read grows with the open work and its depth, never with
#: the settled history the plan keeps. ``_SEED`` and ``_STALE`` are where the open
#: rows and the stale containers are chosen (``_open_cte``).
_SEED = "__open_rows__"
_STALE = "__stale_containers__"
_LIVE_OPEN_SQL = f"""    scope(id,parent_id) AS (
        SELECT id,parent_id FROM plan_nodes
        WHERE plan_id=? AND {_SEED}
        UNION
        SELECT id,parent_id FROM plan_nodes
        WHERE plan_id=? AND {_STALE}
        UNION
        SELECT parent.id,parent.parent_id FROM plan_nodes AS parent
        JOIN scope ON parent.id=scope.parent_id
        WHERE {node_states.unarchived_sql("parent")}
    ), live AS (
        SELECT {_LIVE_COLUMNS}
        FROM plan_nodes
        WHERE id IN (SELECT id FROM scope)
"""

_ORDERED_TAIL_SQL = """    ), ranked AS (
        SELECT live.*,
               ROW_NUMBER() OVER (
                   PARTITION BY parent_id ORDER BY order_key,id
               ) AS sibling_rank
        FROM live
    ), reachable AS (
        SELECT ranked.*, json_array() AS ancestor_path,
               printf('%020d', sibling_rank) AS tree_position
        FROM ranked WHERE parent_id IS NULL
        UNION ALL
        SELECT child.*, json_insert(reachable.ancestor_path, '$[#]', reachable.id),
               reachable.tree_position || '.' || printf('%020d', child.sibling_rank)
        FROM ranked AS child JOIN reachable ON child.parent_id=reachable.id
    ), ordered AS (
        SELECT reachable.*, 0 AS stranded FROM reachable
        UNION ALL
        SELECT ranked.*, json_array() AS ancestor_path, NULL AS tree_position, 1 AS stranded
        FROM ranked
        WHERE NOT EXISTS (
            SELECT 1 FROM reachable WHERE reachable.id=ranked.id
        )
    )
"""

_ORDERED_CTE_SQL = "WITH RECURSIVE\n" + _LIVE_ALL_SQL + _ORDERED_TAIL_SQL


def _open_cte(db) -> str:
    """The ordered CTE over the open rows and their ancestors; it binds the plan id twice.

    The open rows are read through ``idx_plan_nodes_open``, the stored fold answer the pane
    lists, and a pending parent whose children have all settled (a stale container, folded
    but not settled) through ``idx_plan_nodes_stale``, so the read still lists every
    unsettled node. A database below the aggregates schema (an older rung of the migration
    ladder) has no such columns and reads the non-terminal states.
    """
    if aggregates.ready(db):
        seed = node_states.listed_open_sql("plan_nodes")
        stale = "archived_at IS NULL AND folded=1 AND state='pending'"
    else:
        seed, stale = node_states.open_sql("plan_nodes"), "0"
    sql = _LIVE_OPEN_SQL.replace(_SEED, seed).replace(_STALE, stale)
    return "WITH RECURSIVE\n" + sql + _ORDERED_TAIL_SQL


_ORDER_BY_SQL = """ORDER BY ordered.stranded,
             CASE WHEN ordered.stranded=0 THEN ordered.tree_position END,
             CASE WHEN ordered.stranded=1 THEN ordered.order_key END,
             CASE WHEN ordered.stranded=1 THEN ordered.id END"""

_ORDERED_CURSOR_TAIL_SQL = """SELECT ordered.* FROM ordered
    WHERE ?=0 OR (ordered.kind='task' AND ordered.state='active')
    """ + _ORDER_BY_SQL

#: CROSS JOIN pins the plan's own rows as the outer loop. Left to choose, SQLite
#: scanned every plan's nodes and probed this plan's rows for each one.
_FULL_ORDERED_SQL = _ORDERED_CTE_SQL + """SELECT node.*, ordered.ancestor_path,
           ordered.tree_position, ordered.stranded
    FROM ordered CROSS JOIN plan_nodes AS node ON node.id=ordered.id
    """ + _ORDER_BY_SQL


def ordered_cursor(db, plan_id: str, *, active_tasks_only: bool = False):
    """Return the canonical ordered cursor for a plan's open nodes and their ancestors.

    An active task is open work, so a read of the active tasks costs the open work
    and never the settled history. Callers that need only the first eligible task
    retain this single SQL execution and use ``fetchmany(ACTIVE_TASK_PAGE_SIZE)``.
    """
    return db.execute(_open_cte(db) + _ORDERED_CURSOR_TAIL_SQL, (plan_id, plan_id, int(active_tasks_only)))


def _warn_stranded(rows) -> None:
    """Name corrupt rows that the canonical cursor had to append after the tree."""
    stranded = sorted(str(row["id"]) for row in rows if row["stranded"])
    if stranded:
        logger.warning(
            "plan hierarchy has live nodes unreachable from a root; ordered after the tree: %s",
            ", ".join(stranded),
        )


def ordered_nodes_with_paths(db, plan_id: str) -> list[tuple[object, tuple[str, ...]]]:
    """Return full live node rows and paths for consumers that need node bodies."""
    rows = fetch_all(db, _FULL_ORDERED_SQL, (plan_id,))
    _warn_stranded(rows)
    return [
        (row, tuple(json.loads(str(row["ancestor_path"]))))
        for row in rows
    ]


#: The open nodes and their ancestors in plan order, each with the two facts
#: a reader cannot get from the open rows alone: whether the node is itself
#: open, and whether any child of it is in work (settled children count).
_OPEN_ORDERED_TAIL_SQL = f"""SELECT node.*, ordered.ancestor_path,
           ordered.stranded,
           ({node_states.open_sql("node")}) AS is_open,
           ({node_states.has_child_in_work_sql("node")}) AS has_child_in_work
    FROM ordered CROSS JOIN plan_nodes AS node ON node.id=ordered.id
    """ + _ORDER_BY_SQL


def open_nodes_with_paths(db, plan_id: str) -> list[tuple[Any, tuple[str, ...]]]:
    """Return the plan's open nodes in plan order, with their ancestor paths.

    The read costs the open work, never the settled history: an open node is
    never under a retired one (``node_states.retired``), so climbing from the
    open nodes reaches every ancestor they need. Each row carries
    ``has_child_in_work`` from ``node_states.has_child_in_work_sql``, so a
    parent whose children all settled is still a parent. A node under a
    retired ancestor is out of the work and is not returned.
    """
    rows = fetch_all(db, _open_cte(db) + _OPEN_ORDERED_TAIL_SQL, (plan_id, plan_id))
    _warn_stranded(rows)
    paths = {str(row["id"]): tuple(json.loads(str(row["ancestor_path"]))) for row in rows}
    by_id = {str(row["id"]): row for row in rows}
    return [
        (row, paths[str(row["id"])])
        for row in rows
        if row["is_open"]
        and not any(
            ancestor in by_id and node_states.retired(by_id[ancestor])
            for ancestor in paths[str(row["id"])]
        )
    ]


def ordered_nodes(db, plan_id: str) -> list:
    """Return the plan's unarchived node rows in canonical plan order."""
    return [row for row, _path in ordered_nodes_with_paths(db, plan_id)]


def iter_active_task_pages(db, plan_id: str):
    """Yield fixed batches, warning before eligibility can select stranded work."""
    cursor = ordered_cursor(db, plan_id, active_tasks_only=True)
    while rows := cursor.fetchmany(ACTIVE_TASK_PAGE_SIZE):
        _warn_stranded(rows)
        yield rows


def active_task_ids(db, plan_id: str) -> list[str]:
    """Return active task ids in canonical plan order for scope-plan full scans."""
    rows = ordered_cursor(db, plan_id, active_tasks_only=True).fetchall()
    _warn_stranded(rows)
    return [str(row["id"]) for row in rows]


def open_tasks(db, plan, executor: str) -> list:
    """Return one executor's open task rows in canonical plan order.

    An open task is an unarchived pending or active task with no child in
    work (``node_states.parent_ids``), run by ``executor`` and not assigned
    to the user. Work awaiting review is the reviewer's and terminal work is
    settled, so neither is open; held, blocked and waiting tasks are, because
    they are still this executor's work.
    """
    rows = [row for row, _path in open_nodes_with_paths(db, plan["id"])]
    parents = node_states.parent_ids(rows)
    return [row for row in rows if _is_open(row, plan, executor, parents)]


def _is_open(row, plan, executor: str, parents: set) -> bool:
    """Return whether one ordered node row is an open task of ``executor``."""
    return (
        row["kind"] == "task"
        and row["state"] in OPEN_TASK_STATES
        and row["assignee"] != "user"
        and row["id"] not in parents
        and admission_projection.executor_of(row, plan) == executor
    )
