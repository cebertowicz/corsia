"""Internal read-only workspace, project, thread, and child-plan rollups."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from infra.packed_rows import fetch_all
from plans import artifact_links, hierarchy, node_order, node_waits, projections, service, tree_rows
from plans.models import PlanState


@dataclass(frozen=True)
class PlanRollup:
    """One plan with aggregate progress and ordered node hierarchy.

    ``moving`` says whether a step in this plan, or in a plan delegated below
    it, is running; a delegated step reads it to show its child's work.
    """

    plan: dict
    progress: dict
    nodes: list[dict]
    moving: bool = False
    #: The loaded open tree when ``nodes`` are the open work and a window, not the whole plan
    #: (``plans.open_rollup``); None for the full projection.
    tree: Any = None


@dataclass(frozen=True)
class ScopeRollup:
    """One concrete scope and all visible descendant plan projections."""

    scope: dict
    plans: list[PlanRollup]


def _plan_data(row) -> dict:
    """Add freshness metadata to the canonical internal plan projection."""
    projected = asdict(service._plan_from_row(row))
    projected["updated_at"] = row["updated_at"]
    return projected


def round_feedback_sql(node_alias: str, event_alias: str) -> str:
    """The feedback event of a sent-back round.

    ``plan_nodes.sent_back`` alone says a row is a round; its ``review_rejected``
    event is history, read only for the feedback text it carries.
    """
    return f"{node_alias}.sent_back=1 AND {event_alias}.event='review_rejected'"


def round_feedback(rows) -> dict[str, str]:
    """Each round's feedback from its event rows (``node_id``, ``payload_json``), last wins."""
    feedback: dict[str, str] = {}
    for row in rows:
        try:
            text = json.loads(row["payload_json"] or "{}").get("feedback")
        except (TypeError, json.JSONDecodeError):
            text = None
        feedback[str(row["node_id"])] = str(text or "")
    return feedback


def _settlements(connection, plan_id: str) -> tuple[dict, dict[str, str]]:
    """Return each node's completion and the feedback each sent-back round got.

    A round a rejection sent back closes beside the revision carrying the work
    (``plans.rejection``). It is history of that task, never a row of its own:
    shown, it reads as a second, closed copy of one task. Its revision says
    why it is being redone instead.
    """
    completed = {
        row["node_id"]: row for row in fetch_all(
            connection,
            """SELECT e.id,e.node_id,e.created_at FROM plan_node_events e
               JOIN plan_nodes n ON n.id=e.node_id
               WHERE n.plan_id=? AND e.event='state_transition'
                 AND e.new_state IN ('accepted','accepted_with_followup')
               ORDER BY e.created_at,e.rowid""",
            (plan_id,),
        )
    }
    sent_back = round_feedback(fetch_all(
        connection,
        f"""SELECT e.node_id,e.payload_json FROM plan_node_events e
           JOIN plan_nodes n ON n.id=e.node_id
           WHERE n.plan_id=? AND {round_feedback_sql('n', 'e')}
           ORDER BY e.created_at,e.rowid""",
        (plan_id,),
    ))
    return completed, sent_back


def _links_by_node(connection, plan_id: str) -> dict:
    rows = fetch_all(
        connection,
        f"""SELECT l.*,{artifact_links.LINK_IDENTITY_COLUMNS}
            FROM plan_node_artifact_links l
            JOIN plan_nodes n ON n.id=l.node_id
            JOIN artifact_identities i ON i.id=l.artifact_id
            WHERE n.plan_id=? ORDER BY l.role,l.artifact_id""",
        (plan_id,),
    )
    indexed: dict[str, list[dict]] = {}
    for row in rows:
        indexed.setdefault(row["node_id"], []).append(dict(row))
    return indexed


def _child_plan(
    connection, row, child_depth: int | None, memo: dict[str, PlanState],
    child_plans: dict, child_nodes: dict,
):
    """Load an attached child summary or bounded detail below one node."""
    # A reused worker's plan shows under the node holding its newest attachment.
    child = child_plans.get(str(row["id"]))
    if child is None:
        return None
    if child_depth == 0:
        state = projections._plan_state(
            connection, str(child["id"]), memo,
            plan=child, nodes=child_nodes.get(str(child["id"])),
        )
        return PlanRollup(
            plan=_plan_data(child),
            progress=asdict(state.progress),
            nodes=[],
            moving=state.moving,
        )
    next_depth = None if child_depth is None else child_depth - 1
    return _rollup_plans(connection, [child], next_depth, memo)[0]


def _node_rollup(
    connection, row, path, completed, links, waits, child_depth, memo,
    child_plans, child_nodes,
) -> dict:
    """Project one node at its tree position, with completion, links and child plan."""
    node = service._node_from_row(connection, row, waits=waits[str(row["id"])]).as_dict()
    completion = completed.get(row["id"])
    node.update({
        "sent_back_because": None,
        "depth": len(path),
        "updated_at": row["updated_at"],
        "path": list(path),
        "completed_event_id": completion["id"] if completion else None,
        "completed_at": completion["created_at"] if completion else None,
        "artifact_links": links.get(row["id"], []),
        "child_plan": _child_plan(
            connection, row, child_depth, memo, child_plans, child_nodes,
        ),
    })
    return node


def _rollup_plan(
    connection, plan_row, child_depth: int | None, memo: dict[str, PlanState],
    listed: list[tuple[Any, tuple[str, ...]]], child_plans: dict, child_nodes: dict,
) -> PlanRollup:
    """Project one plan's nodes in canonical plan order, recursing into child plans."""
    completed, sent_back = _settlements(connection, plan_row["id"])
    links = _links_by_node(connection, plan_row["id"])
    rounds = {str(row["id"]) for row, _path in listed if row["sent_back"]}
    rows = [
        (row, path) for row, path in listed
        if str(row["id"]) not in rounds and rounds.isdisjoint(path)
    ]
    waits = node_waits.project(connection, [row for row, _path in rows])
    ordered: list[dict] = []
    for row, path in rows:
        node = _node_rollup(
            connection, row, path, completed, links, waits, child_depth, memo,
            child_plans, child_nodes,
        )
        if node["follow_up_of"] in sent_back and node["state"] in ("pending", "active"):
            node["sent_back_because"] = sent_back[node["follow_up_of"]] or None
        ordered.append(node)
    # Child plans were projected above, so this reuses their memoized state.
    # Reuse this rollup's durable plan row and full node snapshot for the root
    # state rather than querying the identical facts again under the writer lock.
    state = projections._plan_state(
        connection,
        str(plan_row["id"]),
        memo,
        plan=plan_row,
        nodes=[row for row, _path in listed],
    )
    return PlanRollup(
        plan=_plan_data(plan_row),
        progress=asdict(state.progress),
        nodes=ordered,
        moving=state.moving,
    )


def rollup_thread(
    plan_id: str, *, db, child_depth: int | None = None,
    memo: dict[str, PlanState] | None = None,
) -> PlanRollup:
    """Project one plan with optionally bounded attached-child detail.

    A reader that goes on to project these plans' admission passes the same
    ``memo`` to both, so each plan's state is walked once per request.
    """
    with service._connection(db) as connection:
        return _rollup_plans(
            connection, [service._plan_row(connection, plan_id)], child_depth,
            {} if memo is None else memo,
        )[0]


def rollup_threads(
    plan_rows, *, db, child_depth: int | None = None,
    memo: dict[str, PlanState] | None = None,
) -> list[PlanRollup]:
    """Project several root plans, as ``rollup_thread`` does each."""
    with service._connection(db) as connection:
        return _rollup_plans(
            connection, list(plan_rows), child_depth, {} if memo is None else memo,
        )


def _rollup_plans(
    connection, plan_rows: list, child_depth: int | None, memo: dict[str, PlanState],
) -> list[PlanRollup]:
    """Project sibling plans with their shown child plans read as one set."""
    listed = {
        str(plan["id"]): node_order.ordered_nodes_with_paths(connection, plan["id"])
        for plan in plan_rows
    }
    child_plans, child_nodes = _shown_children(
        connection,
        [node for plan_nodes in listed.values() for node, _path in plan_nodes],
        child_depth,
    )
    return [
        _rollup_plan(
            connection, plan, child_depth, memo, listed[str(plan["id"])],
            child_plans, child_nodes,
        )
        for plan in plan_rows
    ]


def _shown_children(connection, nodes, child_depth: int | None) -> tuple[dict, dict]:
    """Read the worker plans these nodes show, as one set."""
    child_plans = hierarchy.shown_worker_plans(connection, nodes)
    # Only a summary-depth child is projected from its raw tree rows here; a
    # detailed child lists its own nodes when it recurses.
    child_nodes = tree_rows.many(
        connection, (plan["id"] for plan in child_plans.values()),
    ) if child_depth == 0 else {}
    return child_plans, child_nodes


def _visible(projected: PlanRollup) -> bool:
    if projected.plan["state"] == "archived":
        return False
    return bool(projected.nodes) or projected.progress["liveness"] != "idle"


def rollup_scope(
    owner_kind: str, owner_id: str, *, db, child_depth: int | None = None,
    memo: dict[str, PlanState] | None = None,
) -> ScopeRollup:
    """Read one concrete scope tree with optionally bounded child detail."""
    if owner_kind not in {"workspace_scope", "project_scope"}:
        raise service.PlanError("scope rollup requires workspace or project identity")
    hierarchy.validate_owner(owner_kind, owner_id, db=db)
    root = db.execute(
        "SELECT * FROM plans WHERE owner_kind=? AND owner_id=?",
        (owner_kind, owner_id),
    ).fetchone()
    plans = []
    if memo is None:
        memo = {}
    for row in hierarchy.descendants(db, [] if root is None else [root["id"]]):
        if child_depth is not None and row["shown_under_step"]:
            continue
        projected = _rollup_plans(db, [row], child_depth, memo)[0]
        if _visible(projected):
            plans.append(projected)
    return ScopeRollup(scope={"kind": owner_kind, "id": owner_id}, plans=plans)


def child_plan_for_spawnable(
    node_id: str, *, db, child_depth: int | None = None,
    memo: dict[str, PlanState] | None = None,
) -> PlanRollup | None:
    """Return attached child detail with an optional descendant depth bound."""
    node = service._node_row(db, node_id)
    return _child_plan(
        db, node, child_depth, {} if memo is None else memo,
        *_shown_children(db, [node], child_depth),
    )
