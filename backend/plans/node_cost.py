"""Per-node token, model-cost, and execution-time roll-up for one plan tree.

``llm_usage`` and ``active_turns`` already carry ``plan_node_id`` per call and
per Turn; this is the one place those per-node facts become a tree total. A
node's *own* figure is its own calls and Turns alone. Its *rolled* figure adds
every descendant in this plan (parent/child, archived included, so retired
work is never silently dropped) plus the rolled total of any plan an attached
child thread owns (``spawned_thread_id`` -> that thread's plan). Rolling is
bottom-up and each plan is folded exactly once, so summing a commitment's
top-level rolled nodes reproduces ``commitment_cost``'s total.

Every batch call is bounded: a fixed handful of ``IN (...)`` queries for the
whole tree, never one query per node or per delegation depth. Width costs
extra queries only: each ``IN`` list is chunked to ``_ID_CHUNK`` so the
host-parameter limit binds the chunk size, not the number of nodes a
workspace may hold.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from infra.packed_rows import fetch_all
from plans import hierarchy, node_time, node_waiting, service, usage_pricing
from plans.status_time import parse as _parse

#: Every ``active_turns.status`` the schema allows; ``_add`` keeps only these.
_EMPTY_TURNS = {"completed": 0, "error": 0, "cancelled": 0, "running": 0, "waiting_retry": 0}
#: Ids per ``IN (...)`` query. SQLite's compiled host-parameter limit is 999 on
#: older builds, and the operator projection batches every plan in a workspace,
#: so the list is chunked rather than trusted to stay small.
_ID_CHUNK = 400


def _rows_in(connection, sql: str, node_ids: list[str], extra: tuple = ()) -> list:
    """Run one ``IN (...)`` query over node ids in bounded chunks.

    ``sql`` carries a single ``{marks}`` placeholder for the id list; ``extra``
    are its leading parameters, repeated for every chunk. Each chunk is read in
    one step (``infra.packed_rows``).
    """
    rows: list = []
    for start in range(0, len(node_ids), _ID_CHUNK):
        chunk = node_ids[start:start + _ID_CHUNK]
        query = sql.format(marks=",".join("?" for _ in chunk))
        rows.extend(fetch_all(connection, query, (*extra, *chunk)))
    return rows


def _empty_own() -> dict[str, Any]:
    return {
        "tokens": {"known": None, "unknown_rows": 0},
        "model_cost_usd": {"known": None, "unknown_rows": 0},
        "usage_recording_failures": 0,
        "execution": {"seconds": 0, "unknown_turns": 0, "turns": dict(_EMPTY_TURNS)},
        "waiting": {"seconds": 0},
    }


def sum_known(values) -> int | float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def settle_known(cost: dict, *, turns: int) -> dict:
    """Zero, not Unknown, only when no call can have begun (decision 235).

    Every Turn makes a model call, so a bound Turn -- running, awaiting its
    receipt, or settled with no row -- leaves an absent amount Unknown.
    """
    if not turns and not cost["usage_recording_failures"]:
        for key in ("tokens", "model_cost_usd"):
            if cost[key]["known"] is None and not cost[key]["unknown_rows"]:
                cost[key] = {**cost[key], "known": 0}
    return cost


def _settled(cost: dict) -> dict:
    return settle_known(dict(cost), turns=sum(cost["execution"]["turns"].values()))


def _add_amount(left: dict, right: dict) -> dict:
    known = None
    if left["known"] is not None or right["known"] is not None:
        known = (left["known"] or 0) + (right["known"] or 0)
    return {"known": known, "unknown_rows": left["unknown_rows"] + right["unknown_rows"]}


def _add(left: dict, right: dict) -> dict:
    """Combine two own-shaped totals; a call is never counted on both sides."""
    left_turns, right_turns = left["execution"]["turns"], right["execution"]["turns"]
    return {
        "tokens": _add_amount(left["tokens"], right["tokens"]),
        "model_cost_usd": _add_amount(left["model_cost_usd"], right["model_cost_usd"]),
        "usage_recording_failures": (
            left["usage_recording_failures"] + right["usage_recording_failures"]
        ),
        "execution": {
            "seconds": left["execution"]["seconds"] + right["execution"]["seconds"],
            "unknown_turns": (
                left["execution"]["unknown_turns"] + right["execution"]["unknown_turns"]
            ),
            "turns": {key: left_turns[key] + right_turns[key] for key in _EMPTY_TURNS},
        },
        # Wall clock beside working time, never folded into tokens or cost.
        "waiting": {"seconds": left["waiting"]["seconds"] + right["waiting"]["seconds"]},
    }


def _own_usage(connection, node_ids: list[str]) -> dict[str, dict]:
    """Each node's tokens and dollars; dollars are Measurement's (``usage_pricing``)."""
    rows = usage_pricing.priced_usage(
        connection, ("plan_node_id",), lambda sql: _rows_in(connection, sql, node_ids),
    )
    return {
        str(row["plan_node_id"]): {
            "tokens": {"known": row["tokens"], "unknown_rows": row["unknown_tokens"]},
            "model_cost_usd": {"known": row["cost"], "unknown_rows": row["unknown_cost"]},
        }
        for row in rows
    }


def _own_failures(connection, node_ids: list[str]) -> dict[str, int]:
    rows = _rows_in(
        connection,
        """SELECT node_id,COUNT(*) AS count FROM plan_node_events
           WHERE event='usage_recording_failed' AND node_id IN ({marks})
           GROUP BY node_id""",
        node_ids,
    )
    return {str(row["node_id"]): int(row["count"]) for row in rows}


def _own_execution(
    connection, node_ids: list[str], now: datetime,
) -> tuple[dict[str, dict], dict[str, list]]:
    """Sum each node's durable Turn intervals; count what cannot be measured.

    Also returns each node's merged intervals (epoch seconds) for the roll-up.

    A Turn still running counts up to the clock. A Turn that ended before it
    started, or that is terminal with no end, is an unknown Turn -- counted,
    never clamped to zero.
    """
    rows = _rows_in(
        connection,
        """SELECT plan_node_id,status,started_at,ended_at FROM active_turns
           WHERE plan_node_id IN ({marks})""",
        node_ids,
    )
    facts: dict[str, dict] = {}
    found: dict[str, list] = defaultdict(list)
    for row in rows:
        node_id = str(row["plan_node_id"])
        fact = facts.setdefault(
            node_id, {"seconds": 0, "unknown_turns": 0, "turns": dict(_EMPTY_TURNS)},
        )
        outcome = str(row["status"])
        fact["turns"][outcome] = fact["turns"].get(outcome, 0) + 1
        start = _parse(row["started_at"])
        end = _parse(row["ended_at"]) if row["ended_at"] else (now if outcome == "running" else None)
        if start is None or end is None or end < start:
            fact["unknown_turns"] += 1
            continue
        fact["seconds"] += int((end - start).total_seconds())
        found[node_id].append((start.timestamp(), end.timestamp()))
    return facts, {node_id: node_waiting.union(items) for node_id, items in found.items()}


def own_many(node_ids, *, db=None, now: datetime | None = None) -> dict[str, dict]:
    """Each node's own usage, execution and waiting facts, in bounded batch queries.

    The count is fixed per call (``node_waiting`` adds its own handful), never
    per node; only chunking a very wide id list adds queries.
    """
    return own_many_timed(node_ids, db=db, now=now)[0]


def own_many_timed(
    node_ids, *, db=None, now: datetime | None = None,
) -> tuple[dict[str, dict], dict[str, node_time.Span]]:
    """``own_many`` plus each node's own time intervals, from the same reads."""
    ids = sorted({str(value) for value in node_ids})
    if not ids:
        return {}, {}
    clock = now or datetime.now(timezone.utc)
    with service._connection(db) as connection:
        usage = _own_usage(connection, ids)
        failures = _own_failures(connection, ids)
        execution, working = _own_execution(connection, ids, clock)
        waiting = node_waiting.own_waiting_intervals(connection, ids, clock, _rows_in)
    result: dict[str, dict] = {}
    spans: dict[str, node_time.Span] = {}
    for node_id in ids:
        own = _empty_own()
        found = usage.get(node_id)
        if found is not None:
            own["tokens"] = found["tokens"]
            own["model_cost_usd"] = found["model_cost_usd"]
        own["usage_recording_failures"] = failures.get(node_id, 0)
        exec_found = execution.get(node_id)
        if exec_found is not None:
            own["execution"] = exec_found
        own["waiting"] = {
            "seconds": int(sum(end - start for start, end in waiting.get(node_id, []))),
        }
        result[node_id] = own
        spans[node_id] = {
            "working": working.get(node_id, []), "waiting": waiting.get(node_id, []),
        }
    return result, spans


#: The node columns the cost projections read.
NODE_COLUMNS = (
    "id,plan_id,parent_id,spawned_thread_id,follow_up_of,commitment_id,state,archived_at"
)


def _plan_nodes(connection, plan_ids: list[str]) -> dict[str, list[dict]]:
    rows = _rows_in(
        connection,
        f"SELECT {NODE_COLUMNS} FROM plan_nodes WHERE plan_id IN ({{marks}})",
        plan_ids,
    )
    by_plan: dict[str, list[dict]] = {plan_id: [] for plan_id in plan_ids}
    for row in rows:
        by_plan[str(row["plan_id"])].append(dict(row))
    return by_plan


def _attached_children(connection, plan_ids: list[str]) -> dict[str, str]:
    """Map each attaching node id to the one child plan whose total it carries.

    A parent never counts the same worker's usage twice: ownership is the one
    reading in ``hierarchy.attaching_nodes``, shared with rollup nesting.
    """
    owners = hierarchy.attaching_nodes(connection, plan_ids)
    return {node_id: child_plan_id for child_plan_id, node_id in owners.items()}


def _rollup_one_plan(
    rows: list[dict], own: dict, attached: dict, child_totals: dict,
    spans: dict, child_spans: dict,
) -> tuple[dict[str, dict], node_time.Span]:
    """Roll one plan's nodes up in a single pass, every child before its parent.

    Tokens, cost and Turn counts sum. Working and Waiting are wall clock, so
    each node's rolled seconds come from the union of its subtree's intervals
    (``node_time``), never from adding its children's seconds. Returns the
    rolled costs and the merged span of the plan's top-level nodes.

    A node's parent is fixed at creation and must already exist in the same
    plan (``node_creation._validate_parent``), so the rows are a forest.
    Reversing a depth-first pre-order visits each child before its parent,
    with no recursion, so no nesting depth can overflow the interpreter stack.
    """
    ids = {row["id"] for row in rows}
    children: dict[str | None, list[str]] = defaultdict(list)
    for row in rows:
        children[row["parent_id"] if row["parent_id"] in ids else None].append(row["id"])
    order: list[str] = []
    stack = list(children[None])
    while stack:
        node_id = stack.pop()
        order.append(node_id)
        stack.extend(children[node_id])
    rolled: dict[str, dict] = {}
    carried: dict[str, node_time.Span] = {}
    for node_id in reversed(order):
        total = dict(own.get(node_id, _empty_own()))
        parts = [spans.get(node_id, node_time.empty())]
        for child_id in children[node_id]:
            total = _add(total, rolled[child_id])
            parts.append(carried[child_id])
        if node_id in attached:
            total = _add(total, child_totals.get(attached[node_id], _empty_own()))
            parts.append(child_spans.get(attached[node_id], node_time.empty()))
        carried[node_id] = node_time.merge(parts)
        working, waiting = node_time.seconds(carried[node_id])
        rolled[node_id] = {
            **total,
            "execution": {**total["execution"], "seconds": working},
            "waiting": {"seconds": waiting},
        }
    return rolled, node_time.merge([carried[node_id] for node_id in children[None]])


def project_many(plan_ids, *, db=None, now: datetime | None = None) -> dict[str, dict[str, dict]]:
    """Every node's own+rolled cost for these plans and every plan below them.

    Returns ``{plan_id: {node_id: {"own": ..., "rolled": ...}}}``. A child
    plan requested beside the step that carries its cost is folded once and
    read in both places.
    """
    return project_with_initiatives(plan_ids, db=db, now=now, initiatives=False)[0]


def project_with_initiatives(
    plan_ids, *, db=None, now: datetime | None = None, initiatives: bool = True,
) -> tuple[dict[str, dict[str, dict]], dict[str, dict | None]]:
    """``project_many`` plus each lineage member's initiative total.

    The second map is ``{node_id: {"follow_ups", "cost", "open"} | None}`` for
    every node of the projected plans; it is empty when ``initiatives`` is
    false. The total reads the rows this call already loaded (see
    ``node_cost_initiative``), so a plan with no follow-ups adds no query.
    """
    ids = sorted({str(value) for value in plan_ids})
    if not ids:
        return {}, {}
    clock = now or datetime.now(timezone.utc)
    with service._connection(db) as connection:
        tree = [str(row["id"]) for row in hierarchy.descendants(connection, ids)]
        loaded = _load_plans(connection, tree, clock)
        found = _lineage_totals(connection, loaded[:3], clock) if initiatives else {}
    nodes_by_plan, own, attached, spans = loaded
    by_plan = _roll_plans(tree, nodes_by_plan, own, attached, spans)
    return by_plan, {
        row["id"]: found.get(row["id"])
        for plan_id in tree for row in nodes_by_plan[plan_id]
    } if initiatives else {}


def _load_plans(connection, tree: list[str], clock) -> tuple[dict, dict, dict, dict]:
    """Nodes, own costs, attachments and own time spans, in batch reads."""
    nodes_by_plan = _plan_nodes(connection, tree)
    own, spans = own_many_timed(
        [row["id"] for rows in nodes_by_plan.values() for row in rows],
        db=connection, now=clock,
    )
    return nodes_by_plan, own, _attached_children(connection, tree), spans


def _roll_plans(
    tree: list[str], nodes_by_plan: dict, own: dict, attached: dict, spans: dict,
) -> dict:
    return _roll_plans_with_totals(tree, nodes_by_plan, own, attached, spans)[0]


def _roll_plans_with_totals(
    tree: list[str], nodes_by_plan: dict, own: dict, attached: dict, spans: dict,
) -> tuple[dict, dict, dict]:
    """``_roll_plans`` plus each plan's total and merged span (its top-level nodes')."""
    by_plan: dict[str, dict[str, dict]] = {}
    totals: dict[str, dict] = {}
    plan_spans: dict[str, node_time.Span] = {}
    # Each plan is rolled only after the child plans its steps carry. An
    # explicit stack keeps delegation depth off the interpreter stack.
    stack = [(plan_id, False) for plan_id in tree]
    while stack:
        plan_id, children_done = stack.pop()
        if plan_id in totals:
            continue
        rows = nodes_by_plan[plan_id]
        carried = [attached[row["id"]] for row in rows if row["id"] in attached]
        if not children_done:
            stack.append((plan_id, True))
            stack.extend((child_id, False) for child_id in carried)
            continue
        rolled, plan_spans[plan_id] = _rollup_one_plan(
            rows, own, attached, {child_id: totals[child_id] for child_id in carried},
            spans, plan_spans,
        )
        by_plan[plan_id] = {
            node_id: {"own": _settled(own.get(node_id, _empty_own())),
                      "rolled": _settled(rolled[node_id])}
            for node_id in rolled
        }
        total = _empty_own()
        for row in rows:
            if row["parent_id"] is None:
                total = _add(total, rolled[row["id"]])
        totals[plan_id] = total
    return by_plan, totals, plan_spans


def _lineage_totals(connection, loaded: tuple[dict, dict, dict], clock) -> dict[str, dict]:
    """Each lineage member's initiative total (``node_cost_initiative`` owns the membership)."""
    from plans import node_cost_initiative

    return node_cost_initiative.totals(connection, loaded, clock)
