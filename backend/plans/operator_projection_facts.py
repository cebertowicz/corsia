"""Batched facts used to project operator node state."""

from datetime import datetime, timezone

from plans import admission, operator_node_facts as facts, operator_stopped
from plans.admission_projection import executor_of
from plans.operator_display_state import coverage_matches


def read(projected, *, db, now, costs, memo, lean, detail_ids, attempt_limit):
    """Read the shared facts once for every node in this operator projection."""
    node_plans = [(node, item.plan) for item in projected for node in item.nodes]
    executors = {node['id']: executor_of(node, plan) for node, plan in node_plans}
    details = facts.NO_DETAIL if lean else facts.detail(
        projected, node_plans, executors, limit=attempt_limit, db=db,
        scoped=detail_ids is not None,
    )
    executions = admission.project_many(node_plans, db=db, memo=memo)
    stops = operator_stopped.nodes(node_plans, executors, executions, db=db)
    cost_facts = facts.project_costs(
        node_plans, db=db, now=now or datetime.now(timezone.utc), wanted=costs,
        detail_ids=detail_ids,
    )
    coverage: dict[str, list[dict]] = {}
    for row in coverage_matches((node['id'] for node, _ in node_plans), db=db):
        coverage.setdefault(row['person_node_id'], []).append(row)
    return node_plans, executors, details, executions, stops, cost_facts, coverage


def split_covered(nodes, states) -> tuple[list[dict], list[dict]]:
    """Drop covered person-comment tasks and name the feedback row carrying each one's counts.

    A covered task is always a root leaf, so removing it changes no branch count.
    An open ask carries the counts on its own; only feedback-only coverage needs a carrier.
    """
    visible, carriers = [], []
    for node in nodes:
        state = states[node['id']]
        if not state['comment_task_covered']:
            visible.append(node)
        matches = state['comment_coverage']
        if matches and not any(row['carrier_kind'] == 'ask' for row in matches):
            carrier = min(matches, key=lambda row: row['carrier_id'])
            carriers.append({**carrier, 'title': node['title'], 'owed': state['comment_attention_owed']})
    return visible, carriers
