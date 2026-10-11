"""Read one plan node as a pane row, for a row the pane lists without its details (#149).

A row that comes from the folded-history route (``plans.folded_reads``) carries only
what the level list shows. Opening it reads the node here: the same projection a
listed row gets (``operator_projection.presented_plans``), over the node and its
ancestors alone, so the cost is the node and its cost rows, not the plan's history.
"""

from __future__ import annotations

import plans.open_rollup as open_rollup
import plans.open_tree as open_tree
import plans.service as service
from plans.boundary import PlanActor
from plans.folded_reads import _authorized_plan
from plans.operator_projection import OperatorNotFound, _UNAVAILABLE, presented_plans


def read_node(plan_id: str, node_id: str, *, actor: PlanActor, db) -> dict:
    """The node as the pane presents a listed row; ``OperatorNotFound`` when it, the plan or access is not there.

    Authorization is the plan read's (``folded_reads._authorized_plan``): an unknown,
    archived or out-of-scope plan, and an absent or archived node, read alike.
    """
    _authorized_plan(plan_id, actor, db=db)
    memo: dict = {}
    with service._connection(db) as connection:
        try:
            tree = open_tree.load_node(connection, plan_id, node_id)
        except KeyError as exc:
            raise OperatorNotFound(_UNAVAILABLE) from exc
        projected = open_rollup.rollup_open(connection, tree, memo)
    for plan in presented_plans(
        [projected], actor, db=db, include_archived=True, memo=memo,
        detail_ids=frozenset({node_id}),
    ):
        for presented in plan["nodes"]:
            if presented["id"] == node_id:
                return presented
    # A sent-back round is history of its task and never a row.
    raise OperatorNotFound(_UNAVAILABLE)
