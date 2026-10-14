"""The plan rollup over the open tree: ``rollup`` for a read that costs open work (#731 S2-1).

``plans.rollup`` projects every unarchived node of a plan. This projects the rows
``plans.open_tree`` loaded (the open work, a recent window of settled leaves and the
ancestors they climb through) into the same ``PlanRollup`` and node dicts, reading
each per-node fact (completion, links, waits, a rejected round's feedback) for those
rows alone. A node carries ``has_child_in_work``, the stored fact ``node_states``
reads to know a parent whose children are not loaded.

The full projection stays the reference for the pane and for the tests that hold the
two equal (``tests/test_plan_open_tree_equivalence.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict

from infra.packed_rows import fetch_all
from plans import artifact_links, hierarchy, node_waits, open_tree, projections, rollup, service
from plans.models import PlanState

_ID_CHUNK = 400


def _in_chunks(connection, sql: str, ids: Iterable[str]) -> list:
    """Run ``sql`` (one ``{marks}`` placeholder) over ids in bounded ``IN`` lists."""
    unique = list(dict.fromkeys(ids))
    rows: list = []
    for start in range(0, len(unique), _ID_CHUNK):
        chunk = unique[start:start + _ID_CHUNK]
        rows.extend(fetch_all(connection, sql.format(marks=",".join("?" for _ in chunk)), chunk))
    return rows


def completions(connection, node_ids: Iterable[str]) -> dict:
    """Each node's latest acceptance event (``rollup._settlements``' ``completed``)."""
    rows = _in_chunks(
        connection,
        """SELECT e.id,e.node_id,e.created_at FROM plan_node_events e
           WHERE e.node_id IN ({marks}) AND e.event='state_transition'
             AND e.new_state IN ('accepted','accepted_with_followup')
           ORDER BY e.created_at,e.rowid""",
        node_ids,
    )
    return {row["node_id"]: row for row in sorted(rows, key=lambda row: row["created_at"])}


def sent_back_feedback(connection, node_ids: Iterable[str]) -> dict[str, str]:
    """The feedback each of these sent-back rounds got (``rollup._settlements``' ``sent_back``)."""
    rows = _in_chunks(
        connection,
        f"""SELECT e.node_id,e.created_at,e.payload_json FROM plan_node_events e
            JOIN plan_nodes n ON n.id=e.node_id
            WHERE e.node_id IN ({{marks}}) AND {rollup.round_feedback_sql('n', 'e')}
            ORDER BY e.created_at,e.rowid""",
        node_ids,
    )
    return rollup.round_feedback(sorted(rows, key=lambda row: row["created_at"]))


def links(connection, node_ids: Iterable[str]) -> dict:
    """The artifact links of these nodes (``rollup._links_by_node``)."""
    rows = _in_chunks(
        connection,
        f"""SELECT l.*,{artifact_links.LINK_IDENTITY_COLUMNS}
            FROM plan_node_artifact_links l
            JOIN artifact_identities i ON i.id=l.artifact_id
            WHERE l.node_id IN ({{marks}}) ORDER BY l.role,l.artifact_id""",
        node_ids,
    )
    indexed: dict[str, list[dict]] = {}
    for row in rows:
        indexed.setdefault(row["node_id"], []).append(dict(row))
    return indexed


def rollup_open(
    connection, tree: open_tree.OpenTree, memo: dict[str, PlanState], *,
    child_depth: int | None = 0, window: open_tree.FoldWindow | None = None,
) -> rollup.PlanRollup:
    """Project the loaded rows as ``rollup.rollup_thread`` does the whole plan.

    Children are summaries at ``child_depth`` 0; deeper, each shown worker plan is read as
    an open tree of its own (with ``window``) and projected one level less deep.
    """
    plan = tree.plan
    # A sent-back round is history of its task and never a row; nothing beneath it is one either.
    rounds = {str(row["id"]) for row in tree.rows if row["sent_back"]}
    listed = [
        (row, path) for row, path in tree.listed
        if str(row["id"]) not in rounds and rounds.isdisjoint(path)
    ]
    rows = [row for row, _path in listed]
    ids = [str(row["id"]) for row in rows]
    feedback = sent_back_feedback(connection, [
        str(row["follow_up_of"]) for row in rows if row["follow_up_of"] is not None
    ])
    child_plans = hierarchy.shown_worker_plans(connection, rows)
    waits = node_waits.project(connection, rows)
    completed, linked = completions(connection, ids), links(connection, ids)
    ordered: list[dict] = []
    for row, path in listed:
        node = rollup._node_rollup(
            connection, row, path, completed, linked, waits, 0, memo, child_plans, {},
        )
        node["has_child_in_work"] = row["child_in_work"] > 0
        if child_depth != 0 and str(row["id"]) in child_plans:
            child = child_plans[str(row["id"])]
            node["child_plan"] = rollup_open(
                connection, open_tree.load(connection, str(child["id"]), fold_window=window), memo,
                child_depth=None if child_depth is None else child_depth - 1, window=window,
            )
        if node["follow_up_of"] in feedback and node["state"] in ("pending", "active"):
            node["sent_back_because"] = feedback[node["follow_up_of"]] or None
        ordered.append(node)
    state = projections._plan_state(connection, str(plan["id"]), memo, plan=plan, tree=tree)
    return rollup.PlanRollup(
        plan=rollup._plan_data(plan), progress=asdict(state.progress), nodes=ordered,
        moving=state.moving, tree=tree,
    )


def rollup_plans(
    connection, plan_rows, memo: dict[str, PlanState], *, child_depth: int | None = 0,
    window: open_tree.FoldWindow | None = None,
) -> list[rollup.PlanRollup]:
    """Project sibling plans over their open trees, each with the folded rows ``window`` lists."""
    return [
        rollup_open(
            connection, open_tree.load(connection, str(plan["id"]), fold_window=window), memo,
            child_depth=child_depth, window=window,
        )
        for plan in plan_rows
    ]


def rollup_thread(
    plan_id: str, *, db, memo: dict[str, PlanState] | None = None,
    window: open_tree.FoldWindow | None = None,
) -> rollup.PlanRollup:
    """``rollup.rollup_thread`` over the open work, summary-depth children."""
    with service._connection(db) as connection:
        return rollup_plans(
            connection, [service._plan_row(connection, plan_id)], {} if memo is None else memo,
            window=window,
        )[0]


def rollup_scope(
    owner_kind: str, owner_id: str, *, db, memo: dict[str, PlanState] | None = None,
    window: open_tree.FoldWindow | None = None,
) -> rollup.ScopeRollup:
    """``rollup.rollup_scope`` over the open work of each plan, summary-depth children."""
    if owner_kind not in {"workspace_scope", "project_scope"}:
        raise service.PlanError("scope rollup requires workspace or project identity")
    hierarchy.validate_owner(owner_kind, owner_id, db=db)
    root = db.execute(
        "SELECT * FROM plans WHERE owner_kind=? AND owner_id=?", (owner_kind, owner_id),
    ).fetchone()
    memo = {} if memo is None else memo
    plans = []
    for row in hierarchy.descendants(db, [] if root is None else [root["id"]]):
        if row["shown_under_step"]:
            continue
        projected = rollup_plans(db, [row], memo, window=window)[0]
        if _visible(projected):
            plans.append(projected)
    return rollup.ScopeRollup(scope={"kind": owner_kind, "id": owner_id}, plans=plans)


def _visible(projected: rollup.PlanRollup) -> bool:
    if projected.plan["state"] == "archived":
        return False
    return bool(projected.nodes) or projected.tree.plan["top_roots"] > 0 or projected.progress["liveness"] != "idle"


def child_plan_for_spawnable(
    node_id: str, *, db, child_depth: int = 1, memo: dict[str, PlanState] | None = None,
    window: open_tree.FoldWindow | None = None,
) -> rollup.PlanRollup | None:
    """The plan attached to one node, listed over its open work with bounded child detail."""
    node = service._node_row(db, node_id)
    child = hierarchy.shown_worker_plans(db, [node]).get(str(node["id"]))
    if child is None:
        return None
    return rollup_plans(db, [child], {} if memo is None else memo, child_depth=child_depth - 1, window=window)[0]

