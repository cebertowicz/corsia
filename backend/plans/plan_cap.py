"""The cap on an agent's next work: past it, the lowest unstarted work moves to Later.

An agent works one thing at a time, so a plan needs only enough work waiting
to start above the line for the person to see what comes next and move
something up without opening Later. When more than ``CAP`` top-level items
above the line are unstarted, the lowest of them in plan order move to Later,
through the same flip an agent's own move makes (``later_fields.flip``), so the
row shows why and pulling it back is the ordinary pull back.

Only top-level items count, and an item counts once however many steps it has:
a phase or a workflow run is one item. Started work never moves and is not
counted, whatever it waits on (a reviewer, a check, a reply, a person). An item
is unstarted when every open task under it is pending, holds no slot, has no
worker, is not the person's own task, and waits on nothing but the agent's own
check-back. Only a thread's own plan is capped.

It runs when work is added or inserted, when an item is pulled back from Later
(that item stays where it was put), and once on the first start
(``run_startup_spill``), which first brings back any item only partly in Later.
"""

from __future__ import annotations

import sqlite3

import plans.admission_attempts as attempts
import plans.node_order as node_order
import plans.node_waits as node_waits
import plans.service as service

#: Top-level unstarted items kept above the line in an agent's plan: the item the
#: agent starts next, plus about four the person can hold at once to compare and
#: move up (Cowan 2001, "The magical number 4 in short-term memory").
CAP = 5

#: The reason a spilled task shows on its Later row.
REASON = f"Moved to Later: more than {CAP} items were waiting to start"
PROVENANCE = "plans.plan_cap"
STARTUP_MARKER = "plans:plan_cap:first_spill"


def _open_holds(db, node_ids: list[str]) -> set[str]:
    if not node_ids:
        return set()
    marks = ",".join("?" for _ in node_ids)
    rows = db.execute(
        f"SELECT DISTINCT node_id FROM plan_node_holds WHERE cleared_at IS NULL AND node_id IN ({marks})",
        node_ids,
    ).fetchall()
    return {str(row["node_id"]) for row in rows}


def _waiting(db, node_ids: list[str]) -> set[str]:
    """Tasks with an armed watch other than the agent's own check-back."""
    watches = node_waits.armed_watches(db, node_ids, compact=True)
    return {
        node_id for node_id, rows in watches.items()
        if any(not node_waits.is_check_back(node_waits.watch_cause(row)) for row in rows)
    }


def unstarted_above(db, plan_id: str) -> list[tuple[str, list[str]]]:
    """The plan's unstarted top-level items above the line, in plan order, each with its open tasks above the line."""
    ordered = node_order.open_nodes_with_paths(db, plan_id)
    roots = [str(row["id"]) for row, _path in ordered if row["parent_id"] is None]
    tasks: dict[str, list] = {root: [] for root in roots}
    for row, path in ordered:
        if not row["is_parent"] and row["kind"] == "task":
            tasks[path[0] if path else str(row["id"])].append(row)
    ids = [str(row["id"]) for rows in tasks.values() for row in rows]
    started = set(attempts.open_attempts(ids, db=db, compact=True)) | _open_holds(db, ids) | _waiting(db, ids)
    items = []
    for root in roots:
        rows = tasks[root]
        if not rows or any(
            row["state"] != "pending" or row["spawned_thread_id"] or row["assignee"] == "user"
            or str(row["id"]) in started
            for row in rows
        ):
            continue
        above = [str(row["id"]) for row in rows if not row["later"]]
        if above:
            items.append((root, above))
    return items


def spill(db: sqlite3.Connection, plan_id: str, *, keep: frozenset[str] = frozenset(),
          actor_id: str | None = None) -> list[str]:
    """Move the lowest unstarted items past the cap to Later; return those items, top-level ids.

    ``keep`` holds the top-level ids of items just pulled back from Later: they are not the ones moved.
    """
    plan = service._plan_row(db, plan_id)
    if plan["owner_kind"] != "thread":
        return []
    items = unstarted_above(db, plan_id)
    over = len(items) - CAP
    if over <= 0:
        return []
    moved: list[str] = []
    for root, tasks in reversed(items):  # lowest first
        if over <= 0:
            break
        if root in keep:
            continue
        from plans import later_fields

        for task_id in tasks:
            later_fields.flip(db, task_id, later=True, reason=REASON, actor_id=actor_id, provenance=PROVENANCE)
        moved.append(root)
        over -= 1
    return moved


def pulled_back(db, node_ids: list[str], *, actor_id: str | None) -> list[str]:
    """Apply the cap after items were pulled back from Later: they stay where they were put,
    and the lowest other unstarted items make room."""
    from plans import later_fields

    if not node_ids:
        return []
    plan_id = str(service._node_row(db, node_ids[0])["plan_id"])
    keep = frozenset(later_fields.item_root_id(db, node_id) for node_id in node_ids)
    return spill(db, plan_id, keep=keep, actor_id=actor_id)


def whole_items(db, plan_id: str) -> int:
    """Bring back every item only partly in Later, so each item is in Later whole or not at all.

    Returns the number of items brought back. Only the first start runs it: every
    write since keeps an item on one side (``later_fields``).
    """
    from plans import later_fields

    count = 0
    for row, _path in node_order.open_nodes_with_paths(db, plan_id):
        if row["parent_id"] is not None:
            continue
        values = {bool(task["later"]) for task in later_fields.item_tasks(db, str(row["id"]))}
        if values == {True, False}:
            later_fields.set_item(db, str(row["id"]), later=False, reason=None, actor_id=None, provenance=PROVENANCE)
            count += 1
    return count


def spill_all(db) -> dict[str, int]:
    """Apply the cap to every agent's plan once; return the items moved per thread."""
    from infra.database import write_transaction

    counts: dict[str, int] = {}
    # Every plan's items are made whole; only an agent's own plan is capped (``spill``).
    plans = db.execute("SELECT id, owner_id FROM plans ORDER BY id").fetchall()
    for plan in plans:
        with write_transaction(db, scope="plan_cap_first_spill") as connection:
            whole_items(connection, str(plan["id"]))
            moved = spill(connection, str(plan["id"]))
        if moved:
            counts[str(plan["owner_id"])] = len(moved)
    return counts


def run_startup_spill() -> None:
    """Apply the cap to every existing plan once per instance."""
    from registry import startup_backfill

    startup_backfill.run_once(STARTUP_MARKER, "plan cap first spill", spill_all)
