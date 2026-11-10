"""Thread labels and visible plan targets for workflow runs."""
import json
import sqlite3
from typing import Any, Iterable


def agents(db: sqlite3.Connection, run_ids: Iterable[str]) -> dict[str, dict[str, Any] | None]:
    """The thread that ran each run: the owner of its plan, as ``/agents`` names a thread."""
    found: dict[str, dict[str, Any] | None] = {run_id: None for run_id in run_ids}
    rows = db.execute("""
        SELECT n.id AS run_id,p.owner_id AS id,t.title AS label FROM plan_nodes n
        JOIN plans p ON p.id=n.plan_id AND p.owner_kind='thread'
        LEFT JOIN threads t ON t.id=p.owner_id
        WHERE n.id IN (SELECT value FROM json_each(:ids))
    """, {"ids": json.dumps(sorted(run_ids))}).fetchall()
    found.update({str(row["run_id"]): {"id": str(row["id"]), "label": row["label"]} for row in rows})
    return found


def plan_targets(db: sqlite3.Connection, run_ids: Iterable[str]) -> dict[str, dict[str, str | None] | None]:
    """Find the visible plan and its nesting step using the plan attachment owner."""
    from plans.public import attached_parent_node

    rows = db.execute("""
        SELECT n.id AS run_id, p.id AS plan_id, p.owner_id, t.spawned_by
        FROM plan_nodes n JOIN plans p ON p.id=n.plan_id AND p.owner_kind='thread'
        JOIN threads t ON t.id=p.owner_id
        WHERE n.id IN (SELECT value FROM json_each(:ids))
    """, {"ids": json.dumps(sorted(run_ids))}).fetchall()
    result: dict[str, dict[str, str | None] | None] = {}
    by_plan: dict[str, dict[str, str | None] | None] = {}
    for row in rows:
        original_plan = str(row["plan_id"])
        if original_plan in by_plan:
            result[str(row["run_id"])] = by_plan[original_plan]
            continue
        plan_id, thread_id, spawned_by = row["plan_id"], row["owner_id"], row["spawned_by"]
        step_id = None
        while spawned_by:
            attachment = attached_parent_node(plan_id, db=db)
            if attachment is None:
                break
            step_id = str(attachment["id"])
            parent = db.execute("SELECT p.id, p.owner_id, t.spawned_by FROM plans p "
                                "JOIN threads t ON t.id=p.owner_id WHERE p.id=? AND p.owner_kind='thread'",
                                (attachment["plan_id"],)).fetchone()
            if parent is None:
                break
            plan_id, thread_id, spawned_by = parent["id"], parent["owner_id"], parent["spawned_by"]
        target = None if spawned_by else {"thread_id": str(thread_id), "step_id": step_id}
        by_plan[original_plan] = target
        result[str(row["run_id"])] = target
    return result


def labels(db: sqlite3.Connection, thread_ids: Iterable[str]) -> dict[str, str | None]:
    """Each thread's own title, which is what the page shows for an agent."""
    ids = sorted(thread_ids)
    if not ids:
        return {}
    return {str(row["id"]): row["title"] for row in db.execute(
        "SELECT id,title FROM threads WHERE id IN (SELECT value FROM json_each(:ids))",
        {"ids": json.dumps(ids)},
    ).fetchall()}
