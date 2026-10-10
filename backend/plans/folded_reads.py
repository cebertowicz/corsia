"""The folded history of a plan, on demand (#731 S2-2).

A pane read lists the open work and a window of folded rows (``plans.open_tree``); the
rest of a plan's finished work is paged here, one level at a time, newest first, from
``idx_plan_nodes_folded``. A folded row carries its own aggregates, so opening one pages
the level beneath it the same way. ``read_path`` places one node the client has not loaded:
its ancestors, and the page of its parent's folded rows it sits on.
"""

from __future__ import annotations

from plans import node_states, service
from plans.boundary import PlanActor, _authorize_plan_scope
from plans.operator_display_state import TERMINAL_DISPLAY
from plans.operator_projection import OperatorNotFound, _require_human

PAGE_SIZE = 50
ROOT = "root"
_UNAVAILABLE = "operator target is unavailable"
_BUCKET_STATE = {"done": "accepted", "failed": "failed", "closed": "closed"}
_COLUMNS = (
    "id,parent_id,kind,title,state,fold_bucket,folded_at,"
    "child_folded_done,child_folded_failed,child_folded_closed"
)
_FOLDED = f"plan_id=? AND {node_states.unarchived_sql('plan_nodes')} AND folded=1 AND sent_back=0"


def _authorized_plan(plan_id: str, actor: PlanActor, *, db):
    """The plan, or ``OperatorNotFound``: unknown, archived and out of scope read alike."""
    _require_human(actor)
    plan = db.execute("SELECT id,state FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None or plan["state"] == "archived":
        raise OperatorNotFound(_UNAVAILABLE)
    try:
        _authorize_plan_scope(db, plan_id, actor)
    except (PermissionError, service.PlanError) as exc:
        raise OperatorNotFound(_UNAVAILABLE) from exc
    return plan


def _row_of(plan_id: str, node_id: str, *, db):
    row = db.execute(
        f"SELECT {_COLUMNS},folded,archived_at FROM plan_nodes WHERE id=? AND plan_id=?",
        (node_id, plan_id),
    ).fetchone()
    if row is None or row["archived_at"] is not None:
        raise OperatorNotFound(_UNAVAILABLE)
    return row


def _item(row) -> dict:
    """One folded row as the pane shows it: its tone, and the folded rows beneath it by tone."""
    state = str(row["state"])
    shown = state if state in TERMINAL_DISPLAY else _BUCKET_STATE.get(str(row["fold_bucket"]), "closed")
    folds = {tone: row[f"child_folded_{tone}"] for tone in ("done", "failed", "closed")}
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "kind": row["kind"],
        "title": row["title"],
        "state": state,
        "folded_at": row["folded_at"],
        "fold_bucket": row["fold_bucket"],
        "display": dict(TERMINAL_DISPLAY[shown]),
        "folds": folds,
        "has_folded_children": any(folds.values()),
    }


def _cursor(row) -> str:
    return f"{row['folded_at']},{row['id']}"


def _parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    stamp, _comma, node_id = cursor.rpartition(",")
    if not stamp or not node_id:
        raise ValueError("cursor must be <folded_at>,<id>")
    return stamp, node_id


def _level(parent: str) -> tuple[str, tuple]:
    return (" AND parent_id IS NULL", ()) if parent == ROOT else (" AND parent_id=?", (parent,))


def read_folded(
    plan_id: str, *, parent: str = ROOT, cursor: str | None = None, actor: PlanActor, db,
) -> dict:
    """One page of the folded rows directly beneath ``parent`` (a node id or ``root``), newest first.

    ``next_cursor`` is None on the last page. Raises ``ValueError`` for a malformed cursor
    and ``OperatorNotFound`` when the plan, the parent or the caller's access is not there.
    """
    plan = _authorized_plan(plan_id, actor, db=db)
    if parent != ROOT:
        _row_of(plan_id, parent, db=db)
    after = _parse_cursor(cursor)
    level, level_params = _level(parent)
    sql = f"SELECT {_COLUMNS} FROM plan_nodes WHERE {_FOLDED}{level}"
    params: tuple = (plan_id, *level_params)
    if after is not None:
        sql += " AND (folded_at<? OR (folded_at=? AND id>?))"
        params += (after[0], after[0], after[1])
    rows = db.execute(sql + " ORDER BY folded_at DESC, id LIMIT ?", (*params, PAGE_SIZE + 1)).fetchall()
    page = rows[:PAGE_SIZE]
    return {
        "plan_id": plan["id"],
        "parent": parent,
        "items": [_item(row) for row in page],
        "next_cursor": _cursor(page[-1]) if len(rows) > PAGE_SIZE else None,
    }


def _ancestors(plan_id: str, row, *, db) -> list[dict]:
    """The node's ancestors, root first, each with whether it is folded."""
    chain: list[dict] = []
    seen = {str(row["id"])}
    parent = row["parent_id"]
    while parent is not None and str(parent) not in seen:
        seen.add(str(parent))
        found = db.execute(
            "SELECT id,parent_id,kind,title,state,folded,archived_at FROM plan_nodes "
            "WHERE id=? AND plan_id=?", (parent, plan_id),
        ).fetchone()
        if found is None or found["archived_at"] is not None:
            break
        chain.append({
            "id": found["id"], "kind": found["kind"], "title": found["title"],
            "state": found["state"], "folded": bool(found["folded"]),
        })
        parent = found["parent_id"]
    return chain[::-1]


def read_path(plan_id: str, node_id: str, *, actor: PlanActor, db) -> dict:
    """A node's ancestor chain and, when it is folded, where it sits in its parent's folded pages.

    ``page`` is None for a node that is not folded (it is in the open work). Otherwise
    ``index`` is the node's place among its level's folded rows, ``page_index`` its page,
    and ``cursor`` the cursor that fetches that page from ``read_folded`` (None for the first).
    """
    _authorized_plan(plan_id, actor, db=db)
    row = _row_of(plan_id, node_id, db=db)
    answer = {
        "plan_id": plan_id,
        "node": _item(row) if row["folded"] else None,
        "ancestors": _ancestors(plan_id, row, db=db),
        "page": None,
    }
    if not row["folded"]:
        return answer
    level, level_params = _level(ROOT if row["parent_id"] is None else str(row["parent_id"]))
    where = f"{_FOLDED}{level}"
    params: tuple = (plan_id, *level_params)
    index = db.execute(
        f"SELECT COUNT(*) FROM plan_nodes WHERE {where} AND (folded_at>? OR (folded_at=? AND id<?))",
        (*params, row["folded_at"], row["folded_at"], row["id"]),
    ).fetchone()[0]
    page_index = index // PAGE_SIZE
    before = None
    if page_index:
        last_before = db.execute(
            f"SELECT folded_at,id FROM plan_nodes WHERE {where} ORDER BY folded_at DESC, id "
            "LIMIT 1 OFFSET ?", (*params, page_index * PAGE_SIZE - 1),
        ).fetchone()
        before = _cursor(last_before)
    answer["page"] = {
        "parent": ROOT if row["parent_id"] is None else row["parent_id"],
        "index": index, "page_index": page_index, "page_size": PAGE_SIZE, "cursor": before,
    }
    return answer
