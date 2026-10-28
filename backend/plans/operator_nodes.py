"""Node and plan presentation for the canonical operator contract.

This module adapts one rollup plus precomputed canonical node state. It never
queries or mutates plan state.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

import plans.operator_display_state as display_state
import plans.operator_node_state as node_state
import plans.operator_plan_display as plan_header
import plans.operator_scheduled as scheduled_work
import plans.rollup as rollup
import plans.service as service
import plans.user_steering as user_steering
import plans.wait_summary as wait_summary
from plans import open_tree
from plans.node_states import folded_ids, is_container, parent_ids, work_ids


def _artifact_fields(link: dict) -> dict:
    return {
        "id": link["artifact_id"],
        "role": link["role"],
        "integration": link["integration"],
        "type": link["type"],
        "external_id": link["external_id"],
        "title": link.get("title"),
        "path": link.get("path"),
        "state": link["state"],
        "attention_state": link.get("attention_state"),
    }


def _child_summary(
    projected: rollup.PlanRollup | None,
    thread_id: str | None,
) -> dict | None:
    if projected is None or thread_id is None:
        return None
    return {
        "available": True,
        "plan_id": projected.plan["id"],
        "thread_id": thread_id,
        "progress": dict(projected.progress),
    }


def _present_node(
    node: dict,
    review_targets: dict[str, dict],
    state: dict,
    plan_state: str,
) -> dict:
    links = node["artifact_links"]
    discussion = next(
        (link["artifact_id"] for link in links if link["role"] == "discussion"),
        None,
    )
    completion = None
    if node["completed_event_id"]:
        completion = {
            "event_id": node["completed_event_id"],
            "at": node["completed_at"],
            "witnessed": False,
        }
    execution = state["execution"]
    # Precedence decides disclosure: only a primarily blocked node opens its
    # ancestor path. A paused, review, or terminal node keeps its blocker as a
    # detail cause without widening what the tree opens by default.
    attention_path = (
        [*node["path"], node["id"]] if execution["condition"] == "blocked" else []
    )
    return {
        "id": node["id"],
        "parent_id": node["parent_id"],
        "depth": node["depth"],
        "kind": node["kind"],
        "title": node["title"],
        "description": node["description"],
        # A person step names only this plan's open dependents; cross-plan
        # dependents remain private to their plan owner.
        "waiting_on": node["dependent_titles"] if node["assignee"] == "user" else [],
        "acceptance_criteria": node["acceptance_criteria"],
        "state": node["state"],
        "unmet_dependencies": list(node["unmet_dependencies"]),
        "held_by": node["held_by"],
        "assignee": node["assignee"],
        "revision": node["revision"],
        "requirements_revision": node["requirements_revision"],
        "commitment_id": node["commitment_id"],
        "updated_at": node["updated_at"],
        "last_transition_at": state["last_transition_at"],
        "requires_user_action": state["requires_user_action"],
        "executor": state["executor"],
        "execution": execution,
        "display": state["display"],
        "status_reason": state["status_reason"],
        "evidence": state["evidence"],
        "commitment_cost": state["commitment_cost"],
        "node_cost": state["node_cost"],
        "initiative": state["initiative"],
        "branch_progress": None,
        "scheduled_until": None,
        "attention_path": attention_path,
        "completion": completion,
        "child": _child_summary(node["child_plan"], node["spawned_thread_id"]),
        "review": review_targets.get(node["id"]),
        "user_actions": user_steering.offered(
            node, review_targets.get(node["id"]), execution["causes"],
            plan_state=plan_state,
            worker_plan_state=(
                node["child_plan"].plan["state"] if node["child_plan"] else None
            ),
        ),
        "discussion_artifact_id": discussion,
        "artifacts": [
            _artifact_fields(link) for link in links if link["role"] != "discussion"
        ],
    }


def distribution_from_buckets(
    rows: list[dict], buckets: dict[str, str],
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Count compact raw-row leaves bottom-up, independent of input row order.

    A row saying ``has_child_in_work`` (a read of the open work) is a parent though its
    children are not among ``rows``: it adds no leaf of its own.
    """
    ids = {str(row["id"]) for row in rows}
    work = work_ids(rows)
    holders = parent_ids(rows)
    children: dict[str, list[str]] = {}
    parent_of: dict[str, str] = {}
    for row in rows:
        node_id = str(row["id"])
        parent_id = None if row["parent_id"] is None else str(row["parent_id"])
        if parent_id in ids and node_id in work:
            children.setdefault(parent_id, []).append(node_id)
            parent_of[node_id] = parent_id

    branches: dict[str, dict[str, int]] = {}
    remaining = {node_id: len(children.get(node_id, [])) for node_id in ids}
    ready = deque(node_id for node_id, count in remaining.items() if count == 0)
    while ready:
        node_id = ready.popleft()
        own = dict.fromkeys(node_state.DISTRIBUTION_KEYS, 0)
        child_ids = children.get(node_id, [])
        if not child_ids and node_id in work and node_id not in holders:
            own[buckets[node_id]] = 1
        for child_id in child_ids:
            for key, value in branches[child_id].items():
                own[key] += value
        branches[node_id] = own
        parent_id = parent_of.get(node_id)
        if parent_id is not None:
            remaining[parent_id] -= 1
            if remaining[parent_id] == 0:
                ready.append(parent_id)
    if len(branches) != len(ids):
        raise ValueError("plan node hierarchy contains a cycle")

    plan = dict.fromkeys(node_state.DISTRIBUTION_KEYS, 0)
    for row in rows:
        if row["parent_id"] not in ids:
            for key, value in branches[str(row["id"])].items():
                plan[key] += value
    return branches, plan


def _branch_summary(
    nodes: list[dict], tree: open_tree.OpenTree | None = None,
) -> tuple[dict[str, dict[str, int]], dict[str, bool], dict[str, bool], dict[str, bool], dict[str, str | None]]:
    """Count every presented node's leaves once, bottom-up in canonical order.

    Given the open ``tree`` (a read that loaded only the open work), a node is a leaf
    unless the stored aggregates say it has children, and the settled leaves of the
    children left out count from the node's stored sums less what its listed children
    passed up.

    The same pass rolls up what is quietly scheduled: ``scheduled`` maps each
    node to the soonest end among the agent-scheduled waits beneath it (its own
    included), but only when ALL of its open work is such waiting and nothing
    on it or beneath it needs a person; otherwise ``None``.
    """
    ids = {node["id"] for node in nodes}
    rows = {str(row["id"]): row for row in tree.rows} if tree is not None else {}
    work = work_ids(nodes)
    children: dict[str, list[str]] = {}
    for node in nodes:
        if node["parent_id"] in ids and node["id"] in work:
            children.setdefault(node["parent_id"], []).append(node["id"])
    counts: dict[str, dict[str, int]] = {}
    needs_user: dict[str, bool] = {}
    asks_user: dict[str, bool] = {}
    working: dict[str, bool] = {}
    # Per node: some open work beneath is not scheduled, and the soonest scheduled end.
    unscheduled: dict[str, bool] = {}
    soonest: dict[str, datetime | None] = {}
    for node in reversed(nodes):
        own = dict.fromkeys(node_state.DISTRIBUTION_KEYS, 0)
        child_ids = children.get(node["id"], [])
        loose = False
        ends: list[datetime] = []
        if not child_ids and node["id"] in work:
            bucket = node_state.leaf_bucket(node)
            if tree is None or node["id"] not in tree.shape.parents:
                own[bucket] = 1
            if bucket in node_state.OPEN_KEYS:
                end = scheduled_work.scheduled_leaf_end(node)
                loose = end is None
                ends = [] if end is None else [end]
        elif child_ids:
            has_wait, end = scheduled_work.own_wait(node)
            loose = not scheduled_work.needs_no_one(node) or (has_wait and end is None)
            ends = [] if end is None else [end]
        for child_id in child_ids:
            for key, value in counts[child_id].items():
                own[key] += value
            loose = loose or unscheduled[child_id]
            if (child_end := soonest[child_id]) is not None:
                ends.append(child_end)
        if tree is not None and node["id"] in work:
            row = rows[node["id"]]
            own["done"] += row["sub_done"] - sum(rows[child]["prop_done"] for child in child_ids)
            own["failed"] += row["sub_failed"] - sum(rows[child]["prop_failed"] for child in child_ids)
        counts[node["id"]] = own
        unscheduled[node["id"]] = loose
        soonest[node["id"]] = min(ends) if ends else None
        needs_user[node["id"]] = node["requires_user_action"] or any(
            needs_user[child_id] for child_id in child_ids
        )
        asks_user[node["id"]] = node["display"]["label_key"] == "needs_you" or any(
            asks_user[child_id] for child_id in child_ids
        )
        working[node["id"]] = (
            not child_ids and node["display"]["label_key"] == "working"
        ) or any(working[child_id] for child_id in child_ids)
    scheduled = {
        node_id: None if unscheduled[node_id] or end is None else end.isoformat()
        for node_id, end in soonest.items()
    }
    return counts, needs_user, asks_user, working, scheduled


_BRANCH_FINISHED = {
    "failed": {"label_key": "finished", "tone_key": "failed"},
    "done": node_state.TERMINAL_DISPLAY["accepted"],
    "empty": node_state.TERMINAL_DISPLAY["closed"],
}


def branch_display(
    counts: dict[str, int], *, asks_user: bool, working: bool,
) -> dict[str, str]:
    """Read a container through the same four states as each leaf."""
    if working:
        return {"label_key": "working", "tone_key": "active"}
    if asks_user:
        return {"label_key": "needs_you", "tone_key": "human-review"}
    lead = node_state.leading(counts)
    if lead in _BRANCH_FINISHED:
        return dict(_BRANCH_FINISHED[lead])
    return {"label_key": "waiting", "tone_key": "waiting"}


def _apply_branches(
    nodes: list[dict], raw: list[dict], tree: open_tree.OpenTree | None = None,
) -> dict[str, int]:
    """Give parents their branch progress and fold, and return the plan's counts.

    Only a container (``node_states.is_container``) reads as its leaves. Raw
    lifecycle ``state`` is never changed, so actions still read the node itself.
    Given the open ``tree``, the fold and the parents are the stored aggregates' and the
    plan's counts add the settled leaves no listed row stands for.
    """
    counts, needs_user, asks_user, working, scheduled = _branch_summary(nodes, tree)
    fold = tree.shape.folded if tree is not None else folded_ids(raw)
    parents = tree.shape.parents if tree is not None else parent_ids(nodes)
    ids = {node["id"] for node in nodes}
    for node, row in zip(nodes, raw):
        node["folded"] = node["id"] in fold
        node["scheduled_until"] = scheduled[node["id"]]
        # A parent with its own ask on the user has an obligation of its own, so it
        # reads as itself and its ask is counted once, here; its leaves count theirs.
        node["container"] = (
            node["id"] in parents and is_container(row, True)
            and node["display"]["label_key"] != "needs_you"
        )
        if node["id"] not in parents:
            node["status_reason"] = node["status_reason"] or display_state.queued_reason(node)
            continue
        own = counts[node["id"]]
        node["branch_progress"] = {"done": own["done"], "total": sum(own.values())}
        if node["container"]:
            node["requires_user_action"] = needs_user[node["id"]]
            node["display"] = branch_display(
                own, asks_user=asks_user[node["id"]], working=working[node["id"]],
            )
            if node["display"]["label_key"] == "needs_you" and display_state.plan_paused(node["execution"]):
                # Under the user's plan pause a branch keeps its asks' look and names the pause.
                node["status_reason"] = node["status_reason"] or user_steering.USER_PAUSE_REASON
    plan = dict.fromkeys(node_state.DISTRIBUTION_KEYS, 0)
    roots = [node for node in nodes if node["parent_id"] not in ids]
    for node in roots:
        for key, value in counts[node["id"]].items():
            plan[key] += value
    if tree is not None:
        rows = {str(row["id"]): row for row in tree.rows}
        for key, column in (("done", "top_done"), ("failed", "top_failed")):
            plan[key] += tree.plan[column] - sum(rows[node["id"]]["prop_" + key] for node in roots)
    return plan


#: Open nodes past which a plan has stopped being a plan and become a list. A
#: soft signal, never a refusal: the pane header and the digest say so once the
#: count is above it, and the owner decides what to close, accept or archive.
#: Nothing sweeps.
OPEN_NODE_BUDGET = 40


def oversize(nodes: list[dict]) -> dict[str, int] | None:
    """Name a plan whose open nodes exceed the budget, or ``None`` within it."""
    open_nodes = sum(
        1 for node in nodes if node["state"] not in service._TERMINAL_STATES
    )
    if open_nodes <= OPEN_NODE_BUDGET:
        return None
    return {"open": open_nodes, "budget": OPEN_NODE_BUDGET}


def _leaf_ids(nodes: list[dict], tree: open_tree.OpenTree | None = None) -> set[str]:
    parents = tree.shape.parents if tree is not None else parent_ids(nodes)
    return {node["id"] for node in nodes if node["id"] not in parents}


def present_plan(
    projected: rollup.PlanRollup,
    owner: dict | None,
    review_targets: dict[str, dict],
    states: dict[str, dict],
    tree: open_tree.OpenTree | None = None,
) -> dict | None:
    """Present one plan: its nodes, progress, current and next work, and folds.

    ``tree`` is the open work a read loaded instead of the whole plan
    (``plans.open_tree``); structure and settled counts then come from its stored
    aggregates.

    A folded node (``plans.node_states.folded``) stays in ``nodes`` and its
    parent's branch count; a folded root branch leaves plan progress
    (``node_states.in_play``). The pane collapses it into its level's count.
    """
    if owner is None or projected.plan["state"] == "archived":
        return None
    nodes = [
        _present_node(
            node, review_targets, states[node["id"]], projected.plan["state"],
        )
        for node in projected.nodes
    ]
    counts = _apply_branches(nodes, projected.nodes, tree)
    leaf_ids = _leaf_ids(nodes, tree)
    # A delegated step whose child plan is moving reads Working without running
    # itself; it is current work all the same, so its path stays open.
    current = [
        node["id"]
        for node in nodes
        if node["id"] in leaf_ids
        and node_state.shown_condition(node) in node_state.CURRENT_CONDITIONS
    ]
    # The user's own task is never the agent's next step (#2181): admission,
    # liveness and ordering already refuse it to the agent, so the pick must too.
    users = frozenset(node["id"] for node in projected.nodes if node["assignee"] == "user")
    next_ids = node_state.next_node_ids([node for node in nodes if node["id"] not in users])
    progress = {**projected.progress, "distribution": counts}
    display = plan_header.plan_display(projected.plan, counts, nodes, users)
    # Each outside-wait row reads the same value as the sidebar and chip (EW11).
    for node in nodes:
        if node_wait := wait_summary.from_nodes([node]):
            node["wait"] = node_wait
    if wait := wait_summary.from_nodes(nodes):
        display["wait"] = wait
    elif until := wait_summary.set_aside_until(nodes):
        display["set_aside_until"] = until
    return {
        "plan": dict(projected.plan),
        "owner": owner,
        "progress": progress,
        "display": display,
        "oversize": oversize(nodes),
        "current_node_ids": current,
        "next_node_ids": next_ids,
        "nodes": nodes,
    }
