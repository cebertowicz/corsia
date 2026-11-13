"""The span half of the plan-node roll: each node's Working and Waiting union, with no totals.

``node_cost`` rolls totals and spans together; a caller that reads only Working
(``node_cost_scoped.working_seconds``) rolls just these spans, so the tree walk and the
merge live here once for both.
"""

from __future__ import annotations

from collections import defaultdict
from typing import NamedTuple

from plans import node_time


def forest(rows: list[dict]) -> tuple[dict[str | None, list[str]], list[str]]:
    """The plan's children by parent (``None`` for the top level) and its nodes with every child before its parent."""
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
    order.reverse()
    return children, order


def carried_spans(
    order: list[str], children: dict, attached: dict, spans: dict, child_spans: dict,
) -> dict[str, node_time.Span]:
    """Each node's merged span: its own, its children's and the attached child plan's."""
    carried: dict[str, node_time.Span] = {}
    for node_id in order:
        parts = [spans.get(node_id, node_time.empty())]
        parts.extend(carried[child_id] for child_id in children[node_id])
        if node_id in attached:
            parts.append(child_spans.get(attached[node_id], node_time.empty()))
        carried[node_id] = node_time.merge(parts)
    return carried


def plans_bottom_up(tree: list[str], nodes_by_plan: dict, attached: dict):
    """Yield ``(plan_id, rows, carried_plan_ids)`` for each plan, only after the child plans its steps carry.

    An explicit stack keeps delegation depth off the interpreter stack.
    """
    done: set[str] = set()
    stack = [(plan_id, False) for plan_id in tree]
    while stack:
        plan_id, children_done = stack.pop()
        if plan_id in done:
            continue
        rows = nodes_by_plan[plan_id]
        carried = [attached[row["id"]] for row in rows if row["id"] in attached]
        if not children_done:
            stack.append((plan_id, True))
            stack.extend((child_id, False) for child_id in carried)
            continue
        done.add(plan_id)
        yield plan_id, rows, carried


class SpanRoll(NamedTuple):
    """One plan's nodes rolled to spans: its forest and every node's carried span."""

    children: dict[str | None, list[str]]
    order: list[str]
    carried: dict[str, node_time.Span]

    @property
    def plan(self) -> node_time.Span:
        """The plan's merged span: its top-level nodes' carried spans, merged."""
        return node_time.merge([self.carried[node_id] for node_id in self.children[None]])


def roll_spans(rows: list[dict], attached: dict, spans: dict, child_spans: dict) -> SpanRoll:
    """Roll one plan's rows to spans, given the spans of the child plans its steps carry."""
    children, order = forest(rows)
    return SpanRoll(children, order, carried_spans(order, children, attached, spans, child_spans))


def rolled_plan_spans(tree: list[str], nodes_by_plan: dict, attached: dict, spans: dict) -> dict[str, node_time.Span]:
    """Each plan's merged span, with the totals ``node_cost._roll_plans_with_totals`` also builds left out."""
    merged: dict[str, node_time.Span] = {}
    for plan_id, rows, _carried in plans_bottom_up(tree, nodes_by_plan, attached):
        merged[plan_id] = roll_spans(rows, attached, spans, merged).plan
    return merged


def shape_spans(shape: dict, spans: dict, child_spans: dict | None = None) -> SpanRoll:
    """The span roll of a loaded scoped-read shape (``node_cost_scoped._roll_shape``).

    ``child_spans`` is the carried plans' merged spans when the caller already rolled them with
    their totals; None rolls them here, spans alone.
    """
    if child_spans is None:
        child_spans = rolled_plan_spans(shape["tree"], shape["child_nodes"], shape["child_attached"], spans)
    return roll_spans(shape["rows"], shape["carried"], spans, child_spans)
