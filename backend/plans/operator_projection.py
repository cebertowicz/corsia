"""Authenticated, read-only operator projection over canonical plan rollups."""

from __future__ import annotations

import hashlib
import json

import infra.config as config
import plans.decision_projection as decision_projection
import plans.delegation_harvest as delegation_harvest
import plans.hierarchy as hierarchy
import plans.operator_display_state as operator_display_state
import plans.open_folds as open_folds
import plans.open_rollup as open_rollup
import plans.open_tree as open_tree
import plans.operator_node_state as operator_node_state
import plans.operator_nodes as operator_nodes
import plans.operator_owners as operator_owners
import plans.pane_views as pane_views
import plans.rollup as rollup
import plans.service as service
from plans.boundary import PlanActor, _authorize_plan_scope

CONTRACT_VERSION = 2
_UNAVAILABLE = "operator target is unavailable"


class OperatorNotFound(LookupError):
    """An absent or inaccessible operator target with non-disclosing semantics."""


def _require_human(actor: PlanActor) -> None:
    if actor.kind != "user" or not actor.user_id:
        raise OperatorNotFound(_UNAVAILABLE)


def _session(thread_id: str, actor: PlanActor, *, db):
    row = db.execute(
        "SELECT * FROM threads WHERE id=? AND user_id=? AND purged_at IS NULL",
        (thread_id, actor.user_id),
    ).fetchone()
    if row is None:
        raise OperatorNotFound(_UNAVAILABLE)
    return row


def _project(project_id: str, actor: PlanActor, *, db):
    # Plan content stays behind membership; a plan-less project has none to disclose.
    access = db.execute(
        """SELECT 1 FROM threads WHERE user_id=? AND project_id=?
           UNION ALL SELECT 1 WHERE NOT EXISTS (
             SELECT 1 FROM plans WHERE owner_kind='project_scope' AND owner_id=?)
           LIMIT 1""",
        (actor.user_id, project_id, project_id),
    ).fetchone()
    project = db.execute(
        "SELECT * FROM projects WHERE id=? AND COALESCE(status,'active') != 'archived'",
        (project_id,),
    ).fetchone()
    if access is None or project is None:
        raise OperatorNotFound(_UNAVAILABLE)
    return project


def _plan_for_thread(thread_id: str, actor: PlanActor, *, db):
    """The actor's thread session and its readable plan, None when it has none yet."""
    session = _session(thread_id, actor, db=db)
    plan = db.execute(
        """SELECT * FROM plans
           WHERE owner_kind='thread' AND owner_id=? AND state != 'archived'""",
        (thread_id,),
    ).fetchone()
    if plan is None:
        return session, None
    try:
        _authorize_plan_scope(db, plan["id"], actor)
    except (PermissionError, service.PlanError) as exc:
        raise OperatorNotFound(_UNAVAILABLE) from exc
    return session, plan


def _review_targets(
    projected: list[rollup.PlanRollup],
    actor: PlanActor,
    *,
    db, open_nodes: bool = False,
) -> dict[str, dict]:
    """Resolve each review-requested node to the actor who can move it.

    ``open_nodes``: read only decisions held at a node with open work (a read of the open work).
    The caller already chose which plans are visible; these lookups only name
    who reviews, so an archived owner or parent still resolves.
    """
    plan_ids = [item.plan["id"] for item in projected]
    raised = decision_projection.raised_decisions(plan_ids, db=db, open_nodes=open_nodes)
    decision_nodes = {item["node_id"] for item in raised}
    targets: dict[str, dict] = {}
    for item in projected:
        reviewers: dict[str, dict] = {}
        parent_reviews: frozenset[str] | None = None
        for node in item.nodes:
            kind = operator_display_state.review_kind_from_facts(
                node["state"], node["assignee"], item.plan["owner_kind"],
                human_decision=node["id"] in decision_nodes,
            )
            if kind is None:
                continue
            if kind == "human_decision":
                targets[node["id"]] = {
                    "kind": "human_decision",
                    "label": "Needs your decision",
                    "actor": {"kind": "user", "id": None, "label": "You"},
                }
                continue
            if kind == "human_review":
                targets[node["id"]] = {
                    "kind": "human_review",
                    "label": "Needs your review",
                    "actor": {"kind": "user", "id": None, "label": "You"},
                }
                continue
            # A step is its plan owner's to judge -- including one the harvest
            # put up for review, whose worker is the owner's -- except the ones
            # the owner's delegating parent judges (matrix section G).
            if parent_reviews is None:
                parent_reviews = delegation_harvest.parent_reviews(db, item.plan)
            if node["id"] in parent_reviews:
                owner = _session(item.plan["owner_id"], actor, db=db)
                reviewer_id = owner["spawned_by"] or item.plan["owner_id"]
            else:
                reviewer_id = item.plan["owner_id"]
            if reviewer_id not in reviewers:
                reviewers[reviewer_id] = {
                    "kind": "thread",
                    "id": reviewer_id,
                    "label": _session(reviewer_id, actor, db=db)["title"] or "Parent agent",
                }
            targets[node["id"]] = {
                "kind": "parent_agent",
                "label": (
                    "Awaiting the agent's review"
                    if reviewer_id == item.plan["owner_id"] else "Awaiting parent agent"
                ),
                "actor": reviewers[reviewer_id],
            }
    return targets


def _child_plan_ids(projected: list[rollup.PlanRollup]) -> set[str]:
    child_ids: set[str] = set()
    for item in projected:
        for node in item.nodes:
            child = node["child_plan"]
            if child is not None:
                child_ids.add(child.plan["id"])
    return child_ids


def presented_plans(
    projected: list[rollup.PlanRollup], actor: PlanActor, *, db, include_archived: bool = False,
    costs: bool = True, memo: dict | None = None, lean: bool = False,
) -> list[dict]:
    """Present each visible top-level plan; ``lean`` is ``operator_node_state.project``'s.

    A rollup over open trees (``plans.open_rollup``) is presented from the rows it
    lists: each node's facts are read for those rows alone, and each presented plan
    says what it folded, per level, and how many roots it has (``plans.open_folds``).
    """
    child_ids = _child_plan_ids(projected)
    owners = operator_owners.owner_index(projected, actor, db=db, include_archived=include_archived)
    opened = bool(projected) and all(item.tree is not None for item in projected)
    review_targets = _review_targets(projected, actor, db=db, open_nodes=opened)
    node_states = operator_node_state.project(
        projected, review_targets, db=db, costs=costs, memo=memo, lean=lean,
        detail_ids=frozenset(node["id"] for item in projected for node in item.nodes) if opened else None,
        titles=open_tree.PlansTitles(db, [item.tree for item in projected]) if opened else None,
    )
    plans = []
    for item in projected:
        if item.plan["id"] in child_ids:
            continue
        presented = operator_nodes.present_plan(
            item,
            owners.get(item.plan["id"]),
            review_targets,
            node_states,
            item.tree,
        )
        if presented is not None:
            if item.tree is not None:
                presented["folds"] = open_folds.levels(item.tree)
                presented["root_count"] = item.tree.plan["top_roots"]
            plans.append(presented)
    return plans


def _envelope(
    target: dict,
    projected: list[rollup.PlanRollup],
    actor: PlanActor,
    *,
    db,
    include_archived: bool = False,
    seen: str | None = None,
    costs: bool = True,
    memo: dict | None = None,
):
    plans = presented_plans(
        projected, actor, db=db, include_archived=include_archived, costs=costs, memo=memo,
    )
    attention = {
        # The plan's whole relationship to a decision: it can be waiting on one
        # and says so. The decision itself belongs to the user's queue, not to
        # plan payload, so no body is published here.
        "awaiting_count": decision_projection.awaiting_count(
            [plan["plan"]["id"] for plan in plans], db=db,
        ),
    }
    revision_source = {
        "contract_version": CONTRACT_VERSION,
        "target": target,
        "plans": plans,
        "attention": attention,
        "seen_through": seen,
    }
    token = hashlib.sha256(
        json.dumps(
            revision_source, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    return {
        "contract_version": CONTRACT_VERSION,
        "as_of": service._now(),
        "target": target,
        "revision_token": token,
        "plans": plans,
        "attention": attention,
        "seen_through": seen,
    }


def record_thread_view(
    thread_id: str, *, seen_through: object, actor: PlanActor, db,
) -> dict:
    """Record that the pane showed this thread's plan to its reader (``plans.pane_views``)."""
    _require_human(actor)
    _session(thread_id, actor, db=db)
    return pane_views.record(
        db, user_id=actor.user_id, thread_id=thread_id,
        seen_through=seen_through,
    )


def _scope(kind: str, owner_id: str, label: str, actor: PlanActor, *, db):
    memo: dict = {}
    projected = open_rollup.rollup_scope(
        kind, owner_id, db=db, memo=memo, window=open_tree.FoldWindow(since=None),
    )
    root = db.execute(
        "SELECT state FROM plans WHERE owner_kind=? AND owner_id=?",
        (kind, owner_id),
    ).fetchone()
    if root is not None and root["state"] == "archived":
        raise OperatorNotFound(_UNAVAILABLE)
    target = {"kind": kind, "id": owner_id, "label": label}
    return _envelope(target, projected.plans, actor, db=db, memo=memo)


def read_workspace(*, actor: PlanActor, db) -> dict:
    """Read the selected workspace rollup without ensuring or reconciling state."""
    _require_human(actor)
    selected = config.WORKSPACE_SELECTION
    return _scope(
        "workspace_scope", selected.workspace_id, selected.name, actor, db=db,
    )


def read_project(project_id: str, *, actor: PlanActor, db) -> dict:
    """Read one accessible project rollup without disclosing foreign identity."""
    _require_human(actor)
    project = _project(project_id, actor, db=db)
    return _scope("project_scope", project_id, project["name"], actor, db=db)


def read_thread(thread_id: str, *, actor: PlanActor, db) -> dict:
    """Read one accessible thread through the canonical operator plan contract."""
    _require_human(actor)
    session, plan = _plan_for_thread(thread_id, actor, db=db)
    memo: dict = {}
    seen = pane_views.read(db, user_id=actor.user_id, thread_id=thread_id)
    projected = [] if plan is None else [
        open_rollup.rollup_thread(plan["id"], db=db, memo=memo, window=open_tree.FoldWindow(since=seen)),
    ]
    target = {"kind": "thread", "id": thread_id, "label": session["title"] or "Untitled thread"}
    return _envelope(
        target, projected, actor, db=db, include_archived=True, seen=seen, memo=memo,
    )


def read_child(node_id: str, *, actor: PlanActor, db) -> dict:
    """Read one attached child plan with exactly one bounded detail level."""
    _require_human(actor)
    node = db.execute(
        """SELECT n.id,n.plan_id,n.archived_at,p.state AS plan_state
           FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE n.id=?""",
        (node_id,),
    ).fetchone()
    if node is None or node["archived_at"] is not None or node["plan_state"] == "archived":
        raise OperatorNotFound(_UNAVAILABLE)
    try:
        _authorize_plan_scope(db, node["plan_id"], actor)
    except (PermissionError, service.PlanError) as exc:
        raise OperatorNotFound(_UNAVAILABLE) from exc
    memo: dict = {}
    child = open_rollup.child_plan_for_spawnable(
        node_id, db=db, child_depth=1, memo=memo, window=open_tree.FoldWindow(since=None),
    )
    if child is None or child.plan["state"] == "archived":
        raise OperatorNotFound(_UNAVAILABLE)
    session = _session(child.plan["owner_id"], actor, db=db)
    target = {
        "kind": "thread",
        "id": child.plan["owner_id"],
        "label": session["title"] or "Untitled thread",
    }
    return _envelope(target, [child], actor, db=db, include_archived=True, memo=memo)
