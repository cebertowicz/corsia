"""Node and plan presentation for the canonical operator contract.

This module adapts one rollup plus precomputed canonical node state. It never
queries or mutates plan state.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

import plans.operator_display_state as display_state
import plans.operator_node_presentation as node_presentation
import plans.operator_node_state as node_state
import plans.operator_plan_display as plan_header
import plans.operator_scheduled as scheduled_work
import plans.rollup as rollup
import plans.service as service
import plans.user_steering as user_steering
import plans.wait_summary as wait_summary
from plans import open_tree
from plans.node_states import folded_ids, is_container, parent_ids, work_ids


def distribution_from_buckets(
    rows: list[dict], buckets: dict[str, str], *,
    work: set[str] | None = None, parents: set[str] | None = None,
    include_branches: bool = True,
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Count compact raw-row leaves bottom-up, independent of input row order.

    A row saying ``is_parent`` (a read of the open work) is a parent though its
    children are not among ``rows``: it adds no leaf of its own.
    Passed ``work`` and ``parents`` sets must be computed from these same rows.
    With ``include_branches=False`` the first result is empty; totals are unchanged.
    """
    work = work_ids(rows) if work is None else work
    parents = parent_ids(rows, work=work) if parents is None else parents
    ids = {str(row["id"]) for row in rows}
    children: dict[str, list[str]] = {}
    parent_of: dict[str, str] = {}
    for row in rows:
        node_id = str(row["id"])
        parent_id = None if row["parent_id"] is None else str(row["parent_id"])
        if parent_id in ids and node_id in work:
            children.setdefault(parent_id, []).append(node_id)
            parent_of[node_id] = parent_id

    branches: dict[str, dict[str, int]] = {}
    plan = dict.fromkeys(node_state.DISTRIBUTION_KEYS, 0)
    processed = 0
    remaining = {node_id: len(children.get(node_id, [])) for node_id in ids}
    ready = deque(node_id for node_id, count in remaining.items() if count == 0)
    while ready:
        node_id = ready.popleft()
        child_ids = children.get(node_id, [])
        leaf = not child_ids and node_id in work and node_id not in parents
        if include_branches:
            own = dict.fromkeys(node_state.DISTRIBUTION_KEYS, 0)
            if leaf:
                own[buckets[node_id]] = 1
            for child_id in child_ids:
                for key, value in branches[child_id].items():
                    own[key] += value
            branches[node_id] = own
        elif leaf:
            plan[buckets[node_id]] += 1
        processed += 1
        parent_id = parent_of.get(node_id)
        if parent_id is not None:
            remaining[parent_id] -= 1
            if remaining[parent_id] == 0:
                ready.append(parent_id)
    if processed != len(ids):
        raise ValueError("plan node hierarchy contains a cycle")
    if not include_branches:
        return branches, plan

    for row in rows:
        if row["parent_id"] not in ids:
            for key, value in branches[str(row["id"])].items():
                plan[key] += value
    return branches, plan


def _branch_summary(
    nodes: list[dict], tree: open_tree.OpenTree | None = None, *, later: frozenset[str] = frozenset(),
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
            bucket = node_state.leaf_bucket(node, later=node["id"] in later)
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


def later_branches(nodes: list[dict], parents: set[str] | frozenset[str], later: frozenset[str]) -> set[str]:
    """Return the parents whose every open task beneath is in Later, at least one.

    Only a task carries the stored flag (``plans.later_fields``); a branch is Later
    by this roll-up, so the sheet folds a phase whose open work is all below the cut
    into its level's Later row. Children come after their parent in ``nodes``.
    """
    ids = {node["id"] for node in nodes}
    # Per node: (some open task beneath, every open task beneath is Later).
    seen: dict[str, tuple[bool, bool]] = {}
    for node in reversed(nodes):
        if node["id"] not in parents:
            open_task = node["state"] not in service._TERMINAL_STATES
            seen[node["id"]] = (open_task, not open_task or node["id"] in later)
        has_open, all_later = seen.get(node["id"], (False, True))
        if node["parent_id"] in ids:
            above = seen.get(node["parent_id"], (False, True))
            seen[node["parent_id"]] = (above[0] or has_open, above[1] and all_later)
    return {node_id for node_id in parents if seen.get(node_id, (False, True)) == (True, True)}


def _apply_branches(
    nodes: list[dict], raw: list[dict], tree: open_tree.OpenTree | None = None,
) -> dict[str, int]:
    """Give parents their branch progress and fold, and return the plan's counts.

    Only a container (``node_states.is_container``) reads as its leaves. Raw
    lifecycle ``state`` is never changed, so actions still read the node itself.
    Given the open ``tree``, the fold and the parents are the stored aggregates' and the
    plan's counts add the settled leaves no listed row stands for.
    """
    later = frozenset(str(row["id"]) for row in raw if row.get("later"))
    counts, needs_user, asks_user, working, scheduled = _branch_summary(nodes, tree, later=later)
    fold = tree.shape.folded if tree is not None else folded_ids(raw)
    parents = tree.shape.parents if tree is not None else parent_ids(nodes)
    ids = {node["id"] for node in nodes}
    later_parents = later_branches(nodes, parents, later)
    for node, row in zip(nodes, raw):
        node["folded"] = node["id"] in fold
        node["scheduled_until"] = scheduled[node["id"]]
        if node["id"] in parents:
            node["later"] = node["id"] in later_parents
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


#: Open top-level nodes past which a plan has become a list: a phase or run is one
#: piece of work however many steps it holds. A soft signal, never a refusal: the
#: pane header and the digest say so above it; the owner decides what to close,
#: accept or archive. Nothing sweeps.
OPEN_NODE_BUDGET = 40


def oversize(nodes: list[dict]) -> dict[str, int] | None:
    """Name a plan whose open top-level nodes exceed the budget, or ``None`` within it."""
    open_nodes = sum(1 for node in nodes if node["parent_id"] is None
                     and node["state"] not in service._TERMINAL_STATES)
    if open_nodes <= OPEN_NODE_BUDGET:
        return None
    return {"open": open_nodes, "budget": OPEN_NODE_BUDGET}


def _roll_waits_up(nodes: list[dict]) -> None:
    """Give a waiting branch its steps' earliest outside wait, so its own row says when.

    A branch with nothing running and nothing asking the person stays closed; the
    row that stands for it names the check-back its steps wait on.
    """
    below: dict[str, list[dict]] = {}
    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        parent = by_id.get(node["parent_id"])
        while parent is not None:
            below.setdefault(parent["id"], []).append(node)
            parent = by_id.get(parent["parent_id"])
    for node in nodes:
        if node.get("container") and node["display"]["label_key"] == "waiting" and not node.get("wait"):
            if wait := wait_summary.from_nodes(below.get(node["id"], [])):
                node["wait"] = wait


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
        node_presentation.present_node(
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
    # The user's own task and Later work are never the agent's next step (#2181, Later
    # design §2): admission, liveness and ordering already refuse them, so the pick must too.
    users = frozenset(node["id"] for node in projected.nodes if node["assignee"] == "user")
    below_cut = frozenset(str(node["id"]) for node in projected.nodes if node.get("later"))
    next_ids = node_state.next_node_ids([n for n in nodes if n["id"] not in users | below_cut])
    progress = {**projected.progress, "distribution": counts}
    display = plan_header.plan_display(projected.plan, counts, nodes, users, below_cut)
    # Each outside-wait row reads the same value as the sidebar and chip (EW11).
    for node in nodes:
        if node_wait := wait_summary.from_nodes([node]):
            node["wait"] = node_wait
    _roll_waits_up(nodes)
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
