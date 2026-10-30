"""Compact, set-wise Activity index over canonical plan state.

This representation reads only minimal lifecycle and aggregate facts. Full node
presentation remains exclusively owned by ``operator_projection.read_thread``.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json

import infra.config as config
from plans import (
    activity_facts,
    activity_reducer,
    activity_subagents,
    hierarchy,
    node_states,
    not_moving,
    operator_display_state,
    operator_nodes,
    operator_owners,
    operator_plan_display,
    operator_projection,
    service,
    wait_causes,
)
from plans.boundary import PlanActor
from plans.models import PlanState

CONTRACT_VERSION = 1
REPRESENTATION = "activity_scope_index"


def _plan_meta(plan) -> dict:
    return {
        "id": str(plan["id"]),
        "owner_kind": str(plan["owner_kind"]),
        "owner_id": str(plan["owner_id"]),
        "state": str(plan["state"]),
        "revision": int(plan["revision"]),
        "updated_at": str(plan["updated_at"]),
    }


def _reachable_plan_ids(plans: dict[str, dict], roots: list[str]) -> set[str]:
    children: dict[str, list[str]] = defaultdict(list)
    for plan_id, plan in plans.items():
        parent = plan["parent_plan_id"]
        if parent is not None:
            children[str(parent)].append(plan_id)
    found: set[str] = set()
    pending = list(roots)
    while pending:
        plan_id = pending.pop()
        if plan_id in found:
            continue
        found.add(plan_id)
        pending.extend(children.get(plan_id, []))
    return found

def _reduce_plan(
    plan: dict,
    rows: list[dict],
    *,
    states: dict[str, PlanState],
    child_facts: dict[str, dict],
    human_decisions: set[str],
    decision_waits: set[str],
    executor_stop_facts: dict[str, dict],
) -> tuple[dict[str, activity_reducer.ReducedNode], PlanState, dict]:

    reduced: dict[str, activity_reducer.ReducedNode] = {}
    for row in rows:
        node_id = str(row["id"])
        child_cause = None
        child_fact = child_facts.get(node_id)
        if child_fact is not None and child_fact["child_plan_id"] is not None:
            child_state = states.get(str(child_fact["child_plan_id"]))
            if child_state is not None:
                child_cause = wait_causes.delegation_cause(
                    child_fact, plan=plan, child=child_state,
                )
        reduced[node_id] = activity_reducer.reduce_node(
            row,
            plan,
            review_kind=activity_facts.review_kind(row, plan, human_decisions),
            decision_wait=node_id in decision_waits,
            child_cause=child_cause,
            executor_stop_fact=executor_stop_facts.get(node_id),
        )
    buckets = {node_id: item.bucket for node_id, item in reduced.items()}
    branches, counts = operator_nodes.distribution_from_buckets(rows, buckets)
    # The rows are the open work; the settled leaves are the plan's stored counters (#731 S2-2).
    counts = {**counts, "done": counts["done"] + plan["top_done"], "failed": counts["failed"] + plan["top_failed"]}
    leaves = node_states.leaves(rows)
    user_ready = sum(
        reduced[str(row["id"])].bucket == "pending_ready" and row["assignee"] == "user"
        for row in leaves
    )
    user_blocked = sum(
        reduced[str(row["id"])].bucket == "blocked"
        and reduced[str(row["id"])].user_blocker
        for row in leaves
    )
    paused_by_user = sum(
        reduced[str(row["id"])].bucket == "paused"
        and (plan["state"] == "paused" or reduced[str(row["id"])].user_pause)
        for row in leaves
    )
    deadlines = [
        (item.wait_deadline, item.wait_deadline_latest, None)
        for row in leaves
        if (item := reduced[str(row["id"])]).bucket == "waiting"
        and item.wait_deadline is not None
        and item.wait_deadline_latest is not None
    ]
    still = not_moving.read_from_facts(
        counts,
        user_ready=user_ready,
        user_blocked=user_blocked,
        paused_by_user=paused_by_user,
        wait_deadlines=deadlines,
        child_running=any(reduced[str(row["id"])].child_active for row in leaves),
    )
    display = operator_plan_display.display_from_facts(
        plan,
        counts,
        still=still,
        working=any(
            reduced[str(row["id"])].label_key == "working"
            and row["assignee"] != "user"
            for row in leaves
        ),
        needs_user=any(
            reduced[str(row["id"])].requires_user_action for row in leaves
        ),
    )
    return reduced, activity_reducer.plan_state(plan, rows, reduced), display


def _top_level(plan_rows: list) -> list:
    return [
        plan for plan in plan_rows
        if not plan["shown_under_step"] and plan["state"] != "archived"
    ]


def _shown_owners(top_level: list, actor: PlanActor, *, db) -> dict[str, dict]:
    """The one owner predicate: who owns a top-level plan Activity may show."""
    return operator_owners.owner_index(
        top_level, actor, db=db, include_archived=False,
        exclude_archived_projects=True,
    )


def counted_plan_ids(plan_ids, *, actor: PlanActor, db) -> set[str]:
    """Which of these plans Activity's awaiting count would include.

    A plan counts when the top-level plan above it (itself, or the plan a
    worker's step shows it under) is unarchived and has an owner Activity shows,
    so the user's queue and Activity's count agree on an archived project. The
    walk has no depth cap, like ``hierarchy.descendants``; UNION ends a malformed
    cycle, and a cycle of shown plans has no top, so it stays excluded. A paused
    plan counts nothing (``plan_defers_asks``); the walk's start rows already
    carry each plan's state, so that costs no extra read. Two
    set-wise reads for the walk plus the owner index, whatever the plan count.
    """
    wanted = list(dict.fromkeys(str(plan_id) for plan_id in plan_ids))
    top_of: dict[str, str] = {}
    paused: set[str] = set()
    for start in range(0, len(wanted), hierarchy._ID_CHUNK):
        chunk = wanted[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(
            f"""WITH RECURSIVE up(start_id, id, parent_id, shown, start_state) AS (
                  SELECT p.id, p.id, p.parent_plan_id,
                         {hierarchy.shown_under_step_sql('p')}, p.state
                    FROM plans p WHERE p.id IN ({marks})
                  UNION
                  SELECT up.start_id, p.id, p.parent_plan_id,
                         {hierarchy.shown_under_step_sql('p')}, up.start_state
                    FROM plans p JOIN up ON p.id = up.parent_id
                   WHERE up.shown
                )
                SELECT start_id, id, start_state FROM up WHERE NOT shown""",
            chunk,
        ).fetchall():
            top_of[str(row["start_id"])] = str(row["id"])
            if operator_display_state.plan_defers_asks(str(row["start_state"])):
                paused.add(str(row["start_id"]))
    tops: list = []
    top_ids = sorted(set(top_of.values()))
    for start in range(0, len(top_ids), hierarchy._ID_CHUNK):
        chunk = top_ids[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        tops.extend(db.execute(
            f"SELECT * FROM plans WHERE id IN ({marks}) AND state != 'archived'",
            chunk,
        ).fetchall())
    owners = _shown_owners(tops, actor, db=db)
    return {
        plan_id for plan_id, top_id in top_of.items()
        if top_id in owners and plan_id not in paused
    }


def _owned_rows(plan_rows: list, owners: dict[str, dict]) -> list:
    """The plans worth reducing: each shown top-level plan and the worker
    plans shown under its steps.

    Only a worker plan under a step feeds a child state into its parent, so a
    plan outside those trees (an archived or foreign owner, an archived
    project) can neither appear nor change a shown row.
    """
    index = {
        str(plan["id"]): plan for plan in plan_rows
        if plan["shown_under_step"] or str(plan["id"]) in owners
    }
    keep = _reachable_plan_ids(index, [str(plan_id) for plan_id in owners])
    return [plan for plan in plan_rows if str(plan["id"]) in keep]


def _is_hidden(owner: dict, hidden: set[str]) -> bool:
    """Whether a shown owner is a hidden subagent thread."""
    return owner["thread_id"] is not None and str(owner["thread_id"]) in hidden


def _top_level_rows(
    plan_rows: list,
    plans: dict[str, dict],
    nodes_by_plan: dict[str, list[dict]],
    states: dict[str, PlanState],
    reduced_by_plan: dict[str, dict[str, activity_reducer.ReducedNode]],
    displays: dict[str, dict],
    owners: dict[str, dict],
    hidden: set[str],
) -> tuple[list[dict], list[str]]:
    """Activity rows for shown plans; hidden subagents stay only in ``eligible_ids``.

    Their raised decisions still count toward the scope's attention, so their
    plans are eligible but get no reduced state and no row.
    """
    result: list[dict] = []
    eligible_ids: list[str] = []
    for raw_plan in _top_level(plan_rows):
        plan_id = str(raw_plan["id"])
        owner = owners.get(plan_id)
        if owner is None:
            continue
        if _is_hidden(owner, hidden):
            eligible_ids.append(plan_id)
            continue
        plan = plans[plan_id]
        node_rows = nodes_by_plan.get(plan_id, [])
        state = states[plan_id]
        if not node_rows and not plan["top_roots"] and plan["owner_kind"] != "thread":
            continue
        display = displays[plan_id]
        result.append({
            "plan": _plan_meta(plan),
            "owner": owner,
            "progress": {
                "open": state.progress.open,
                "liveness": state.progress.liveness,
            },
            "has_nodes": bool(node_rows) or plan["top_roots"] > 0,
            "display": {
                "label_key": display["label_key"],
                "tone_key": display["tone_key"],
            },
            "priority_at": activity_reducer.priority_at(
                plan, display, node_rows, reduced_by_plan[plan_id],
            ),
        })
        eligible_ids.append(plan_id)
    return result, eligible_ids


def _project_rows(
    plan_rows: list, owners: dict[str, dict], *, db,
) -> tuple[list[dict], list[str], list[dict]]:
    plan_ids = [str(plan["id"]) for plan in plan_rows]
    plans = {str(plan["id"]): dict(plan) for plan in plan_rows}
    hidden = activity_subagents.spawners(plan_rows, db=db)
    shown = [
        str(plan["id"]) for plan in _top_level(plan_rows)
        if str(plan["id"]) in owners and not _is_hidden(owners[str(plan["id"])], hidden)
    ]
    reached = activity_facts.delegation_closure_ids(shown, plans, db=db)
    nodes_by_plan = activity_facts.compact_nodes(reached, db=db)
    decision_facts, human_decisions, decision_waits = activity_facts.decision_indexes(
        plan_ids, db=db,
    )
    executor_stop_facts = activity_facts.executor_stop_facts(nodes_by_plan, plans, db=db)
    pairs = [
        (row, plans[plan_id])
        for plan_id, rows in nodes_by_plan.items()
        for row in rows
        if row["spawned_thread_id"] is not None
    ]
    child_facts = wait_causes.delegation_facts(pairs, db=db)
    states: dict[str, PlanState] = {}
    reduced_by_plan: dict[str, dict[str, activity_reducer.ReducedNode]] = {}
    displays: dict[str, dict] = {}
    depths = activity_facts.depths(plan_rows)
    for plan_id in sorted(reached, key=lambda value: depths[value], reverse=True):
        reduced, state, display = _reduce_plan(
            plans[plan_id],
            nodes_by_plan.get(plan_id, []),
            states=states,
            child_facts=child_facts,
            human_decisions=human_decisions,
            decision_waits=decision_waits,
            executor_stop_facts=executor_stop_facts,
        )
        reduced_by_plan[plan_id] = reduced
        states[plan_id] = state
        displays[plan_id] = display
    rows, eligible_ids = _top_level_rows(
        plan_rows, plans, nodes_by_plan, states, reduced_by_plan, displays, owners, hidden,
    )
    return rows, eligible_ids, decision_facts


def _scope(kind: str, owner_id: str, label: str, actor: PlanActor, *, db) -> dict:
    hierarchy.validate_owner(kind, owner_id, db=db)
    root = db.execute(
        "SELECT * FROM plans WHERE owner_kind=? AND owner_id=?",
        (kind, owner_id),
    ).fetchone()
    if root is not None and root["state"] == "archived":
        raise operator_projection.OperatorNotFound("operator target is unavailable")
    plan_rows = hierarchy.descendants(db, [] if root is None else [root["id"]])
    owners = _shown_owners(_top_level(plan_rows), actor, db=db)
    plan_rows = _owned_rows(plan_rows, owners)
    rows, eligible_ids, decision_facts = _project_rows(plan_rows, owners, db=db)
    plan_index = {str(plan["id"]): dict(plan) for plan in plan_rows}
    eligible_tree = _reachable_plan_ids(plan_index, eligible_ids)
    awaiting = len({
        str(item["artifact_id"])
        for item in decision_facts
        if item["answerable"] and str(item["plan_id"]) in eligible_tree
        and not operator_display_state.ask_deferred(
            plan_paused=operator_display_state.plan_defers_asks(plan_index[str(item["plan_id"])]["state"]),
            node_paused=item["node_paused"],
        )
    })
    target = {"kind": kind, "id": owner_id, "label": label}
    revision_source = {
        "contract_version": CONTRACT_VERSION,
        "representation": REPRESENTATION,
        "target": target,
        "plans": rows,
        "attention": {"awaiting_count": awaiting},
    }
    token = hashlib.sha256(
        json.dumps(
            revision_source, sort_keys=True, separators=(",", ":"), default=str,
        ).encode()
    ).hexdigest()
    return {
        **revision_source,
        "as_of": service._now(),
        "revision_token": token,
    }


def read_workspace(*, actor: PlanActor, db) -> dict:
    """Read the selected workspace's compact Activity index."""
    operator_projection._require_human(actor)
    selected = config.WORKSPACE_SELECTION
    return _scope(
        "workspace_scope", selected.workspace_id, selected.name, actor, db=db,
    )


def read_project(project_id: str, *, actor: PlanActor, db) -> dict:
    """Read one accessible project's compact Activity index."""
    operator_projection._require_human(actor)
    project = operator_projection._project(project_id, actor, db=db)
    return _scope("project_scope", project_id, project["name"], actor, db=db)
