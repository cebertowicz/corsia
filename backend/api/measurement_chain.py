"""The nodes whose usage belongs to one accepted task: its whole chain.

A task's chain is its own attempts (the task and the sent-back attempts it
revises), everything under them in the same plan, and the plan of every worker
thread attached to any of those nodes, recursively. Worker membership is the
plan hierarchy's own rule (``hierarchy.newest_attachment_sql``): a worker plan
belongs to the one node holding its newest attachment, so a reused worker
counts once.

Each node belongs to one task, the nearest accepted task above it. The walk
stops at a node that is itself an accepted task or an attempt of one, so a
nested task keeps its subtree and revision chain and no call is counted twice.
Only a revision with a ``revision_created`` event links to its predecessor: an
accept-with-follow-up successor is new work and does not pull in its source.
"""

from __future__ import annotations

from plans.public import delegation_edge_sql, newest_attachment_sql


def chain_ctes(*, only_tasks: bool = False) -> str:
    """CTEs ``chain(task_id, node_id, plan_id)`` for the accepted tasks in the grain's range.

    Append to ``scope_cte() + outcome_ctes(grain, limited_to_range=False)``. Stopping
    at accepted tasks of every period, not only the queried ones, keeps a
    task's figure the same whichever periods are read. With ``only_tasks`` the walk starts
    only from the task ids in the ``:task_ids`` JSON array; every accepted task still
    stops it, so a task's chain is the same either way.
    """
    seed = "AND a.task_id IN (SELECT value FROM json_each(:task_ids))" if only_tasks else ""
    return f"""
        , attempts(task_id, node_id) AS (
          SELECT id, id FROM outcome_rows
          UNION
          SELECT a.task_id, n.follow_up_of FROM attempts a JOIN plan_nodes n ON n.id=a.node_id
          WHERE n.follow_up_of IS NOT NULL
            AND EXISTS (SELECT 1 FROM plan_node_events e WHERE e.node_id=n.id AND e.event='revision_created')
        ), chain(task_id, node_id, plan_id) AS (
          SELECT a.task_id, a.node_id, m.plan_id FROM attempts a JOIN outcome_rows o ON o.id=a.task_id
          JOIN plan_nodes m ON m.id=a.node_id
          WHERE o.period IS NOT NULL {seed}
          UNION
          SELECT c.task_id, n.id, n.plan_id FROM chain c
          JOIN plan_nodes n ON n.plan_id=c.plan_id AND n.parent_id=c.node_id
          WHERE n.id NOT IN (SELECT node_id FROM attempts)
          UNION
          SELECT c.task_id, r.id, r.plan_id FROM chain c
          CROSS JOIN plan_nodes d ON d.id=c.node_id AND d.spawned_thread_id IS NOT NULL
          CROSS JOIN plans p ON {delegation_edge_sql('d', 'p')}
          CROSS JOIN plan_nodes r ON r.plan_id=p.id AND r.parent_id IS NULL
          WHERE c.node_id={newest_attachment_sql('p')}
            AND r.id NOT IN (SELECT node_id FROM attempts)
        )
    """
