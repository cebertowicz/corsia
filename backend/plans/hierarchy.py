"""Trusted scope identity and the plan hierarchy derived from owner identity."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import infra.config as config
from infra.packed_rows import fetch_all

#: Plan ids per recursive query, below SQLite's oldest host-parameter limit.
_ID_CHUNK = 400


def validate_owner(owner_kind: str, owner_id: str, *, db) -> None:
    """Reject scope identities not backed by the selected workspace or projects."""
    from plans import service

    if owner_kind == "workspace_scope":
        if owner_id != config.WORKSPACE_SELECTION.workspace_id:
            raise service.PlanError("workspace scope owner is not the selected workspace")
    elif owner_kind == "project_scope":
        row = db.execute("SELECT 1 FROM projects WHERE id=?", (owner_id,)).fetchone()
        if row is None:
            raise service.PlanError(f"unknown project scope owner: {owner_id}")


def _set_parent(plan_id: str, parent_plan_id: str | None, *, db) -> bool:
    """Set one trusted hierarchy edge after rejecting self/cyclic ancestry."""
    from plans import service

    row = service._plan_row(db, plan_id)
    if parent_plan_id == plan_id:
        raise service.PlanError("plan hierarchy cannot parent a plan to itself")
    cursor = parent_plan_id
    seen = set()
    while cursor is not None:
        if cursor == plan_id or cursor in seen:
            raise service.PlanError("plan hierarchy would create a cycle")
        seen.add(cursor)
        cursor = service._plan_row(db, cursor)["parent_plan_id"]
    if row["parent_plan_id"] == parent_plan_id:
        return False
    db.execute(
        "UPDATE plans SET parent_plan_id=?, revision=revision+1, updated_at=? WHERE id=?",
        (parent_plan_id, service._now(), plan_id),
    )
    return True


def derive_parent_plan_id(owner_kind: str, owner_id: str, *, db) -> str | None:
    """Derive one owner's parent plan from its identity alone; never refuses.

    A thread sits under its spawner's plan while both share a project, else
    under its project's plan, else under the workspace plan. A thread whose row
    is gone has no derivable parent.
    """
    from plans import service

    if owner_kind == "workspace_scope":
        return None
    if owner_kind == "thread":
        row = db.execute(
            """SELECT spawner.id AS spawner_id, project.id AS project_id
               FROM threads t
               LEFT JOIN threads spawner ON spawner.id=t.spawned_by
                 AND spawner.project_id IS t.project_id AND spawner.id<>t.id
               LEFT JOIN projects project ON project.id=t.project_id
               WHERE t.id=?""",
            (owner_id,),
        ).fetchone()
        if row is None:
            return None
        if row["spawner_id"]:
            return service.ensure_plan("thread", row["spawner_id"], db=db).id
        if row["project_id"]:
            return service.ensure_plan("project_scope", row["project_id"], db=db).id
    return service.ensure_plan(
        "workspace_scope", config.WORKSPACE_SELECTION.workspace_id, db=db
    ).id


def ensure_plan_for_owner(
    owner_kind: str,
    owner_id: str,
    *,
    db=None,
):
    """Return one trusted owner's plan, creating it under its derived parent."""
    from plans import service

    if owner_kind not in service._OWNER_KINDS:
        raise service.PlanError(f"invalid plan owner kind: {owner_kind}")
    owner_id = service._required_text(owner_id, "owner id")
    with service._connection(db) as connection, service._write(connection):
        validate_owner(owner_kind, owner_id, db=connection)
        existing = connection.execute(
            "SELECT * FROM plans WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchone()
        if existing is not None:
            return service._plan_from_row(existing)
        derived = derive_parent_plan_id(owner_kind, owner_id, db=connection)
        now, plan_id = service._now(), service._id()
        connection.execute(
            """INSERT INTO plans
               (id,owner_kind,owner_id,parent_plan_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?)""",
            (plan_id, owner_kind, owner_id, derived, now, now),
        )
        return service._plan_from_row(service._plan_row(connection, plan_id))


def rehome(thread_id: str, *, db) -> None:
    """Re-derive the parents of a thread's plan and its subagents' plans.

    Every writer of a thread's project or spawner calls this in its own
    transaction, as does thread deletion once the row is gone.
    """
    rows = db.execute(
        """SELECT p.id,p.owner_id FROM plans p JOIN threads t ON t.id=p.owner_id
           WHERE p.owner_kind='thread' AND (t.id=? OR t.spawned_by=?)""",
        (thread_id, thread_id),
    ).fetchall()
    for row in rows:
        _set_parent(
            row["id"], derive_parent_plan_id("thread", row["owner_id"], db=db), db=db,
        )


def delegation_link_sql(step_alias: str, plan_alias: str) -> str:
    """Trusted aliases for the one join from a step to the worker plan it names."""
    return (
        f"{step_alias}.plan_id={plan_alias}.parent_plan_id"
        f" AND {step_alias}.spawned_thread_id={plan_alias}.owner_id"
    )


def delegation_edge_sql(step_alias: str, plan_alias: str) -> str:
    """Trusted aliases for the one delegation edge: a step to its worker plan.

    Every query that joins a step to the worker plan it delegated to (the nesting
    rule, the shown worker plans, the child-cause facts, the delegator lookup, the
    activity closure) builds that join from this, so a change to the edge lands in
    all of them at once. Lookups that already hold the worker thread and parent
    plan ids as parameters are a different shape and do not join through it.
    """
    return f"{plan_alias}.owner_kind='thread' AND {delegation_link_sql(step_alias, plan_alias)}"


def shown_under_step_sql(plan_alias: str) -> str:
    """Trusted plan alias for the one rule that nests a worker plan under a step."""
    return f"""EXISTS (
                  SELECT 1 FROM plan_nodes n
                  WHERE {delegation_edge_sql('n', plan_alias)}
                    AND n.archived_at IS NULL
                )"""


def descendants(db, plan_ids) -> list[Any]:
    """Every plan at or below these plans, once each, in owner order.

    ``shown_under_step`` says an unarchived step in the parent plan names the
    plan's owner, so a bounded tree shows it there rather than at top level.
    """
    ids = list(dict.fromkeys(plan_ids))
    found: dict[str, Any] = {}
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(
            db,
            f"""WITH RECURSIVE tree(id) AS (
                  SELECT id FROM plans WHERE id IN ({marks})
                  UNION
                  SELECT p.id FROM plans p JOIN tree ON p.parent_plan_id=tree.id
                )
                SELECT p.*, {shown_under_step_sql('p')} AS shown_under_step
                FROM plans p JOIN tree ON tree.id=p.id""",
            chunk,
        ):
            found[row["id"]] = row
    return sorted(found.values(), key=lambda row: (row["owner_kind"], row["owner_id"]))


#: The node holding the newest ``spawned_thread_attached`` event for worker
#: plan ``p``: the one node a reused worker's plan belongs to. The one reading
#: of that rule; ``attaching_nodes`` and ``shown_worker_plans`` both apply it.
def newest_attachment_sql(plan_alias: str) -> str:
    """Trusted plan alias for the one newest-attachment rule, without rewriting."""
    return f"""(
    SELECT e.node_id FROM plan_node_events e
      JOIN plan_nodes n ON n.id=e.node_id
     WHERE {delegation_link_sql('n', plan_alias)}
       AND e.event='spawned_thread_attached' AND json_valid(e.payload_json)
       AND json_extract(e.payload_json,'$.child_plan_id')={plan_alias}.id
     ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1)"""


def attaching_nodes(db, parent_plan_ids, *, worker: str | None = None) -> dict[str, str]:
    """Map each worker plan under these parent plans to the one node showing it.

    Several nodes can name the same worker (a follow-up or a revision reuses
    it). The plan belongs to exactly one of them: the node holding the newest
    ``spawned_thread_attached`` event for that child plan. Cost totals read it
    here, so a worker's usage counts once.
    """
    ids = list(dict.fromkeys(parent_plan_ids))
    owner_by_child: dict[str, str] = {}
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(
            db,
            f"""SELECT p.id AS child_plan_id, {newest_attachment_sql('p')} AS node_id
                FROM plans p
                WHERE p.parent_plan_id IN ({marks}) AND p.owner_kind='thread'
                  AND (? IS NULL OR p.owner_id=?)""",
            (*chunk, worker, worker),
        ):
            if row["node_id"] is not None:
                owner_by_child[str(row["child_plan_id"])] = str(row["node_id"])
    return owner_by_child


def shown_worker_plan(db, node) -> Any | None:
    """Return the worker plan this node shows and steers, or None.

    The worker's plan under the node's own plan, unless a newer attachment
    gives it to another node that reuses the same worker. The plan tree nests
    it here and the pane's Pause and Resume act on it, from this one read.
    """
    return shown_worker_plans(db, [node]).get(str(node["id"]))


def shown_worker_plans(db, nodes) -> dict[str, Any]:
    """Map node ids to the worker plans they show, in bounded set reads.

    One worker plan per thread (``UNIQUE(owner_kind, owner_id)``), so each
    node maps to at most one row.
    """
    node_ids = list(dict.fromkeys(str(node["id"]) for node in nodes if node["spawned_thread_id"]))
    shown: dict[str, Any] = {}
    for start in range(0, len(node_ids), _ID_CHUNK):
        chunk = node_ids[start:start + _ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = fetch_all(
            db,
            f"""SELECT n.id AS attached_node_id,p.* FROM plan_nodes n
                JOIN plans p ON {delegation_edge_sql('n', 'p')}
                WHERE n.id IN ({marks})
                  AND COALESCE({newest_attachment_sql('p')}, n.id)=n.id""",
            chunk,
        )
        shown.update({str(row["attached_node_id"]): row for row in rows})
    return shown


#: Attachment levels walked above a touched plan. Chains are shallow (workers
#: are almost always first-level); the bound also ends a malformed cycle.
_MAX_ANCESTOR_DEPTH = 8


def with_attaching_plans(plan_ids: Iterable[str], *, db) -> tuple[str, ...]:
    """Add the thread-owned plans that attach each plan's owner, nearest first.

    A parent's row roll-up and chat-list state embed its children's progress,
    so a child's change also changes the plan above it. This walks the
    ``parent_plan_id`` relation the roll-up reads through
    ``hierarchy.attaching_nodes``.
    """
    found: dict[str, None] = dict.fromkeys(plan_ids)
    for plan_id in tuple(found):
        current = plan_id
        for _ in range(_MAX_ANCESTOR_DEPTH):
            row = db.execute(
                "SELECT parent.id AS id FROM plans child "
                "JOIN plans parent ON parent.id=child.parent_plan_id "
                "WHERE child.id=? AND child.owner_kind='thread' "
                "AND parent.owner_kind='thread'",
                (current,),
            ).fetchone()
            if row is None or row["id"] in found:
                break
            current = str(row["id"])
            found[current] = None
    return tuple(found)
