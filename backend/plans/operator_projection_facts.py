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
    coverage = coverage_matches((node['id'] for node, _ in node_plans), db=db)
    return node_plans, executors, details, executions, stops, cost_facts, coverage
