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


def _walk(attempts: str, chain: str, attempt_roots: str, chain_roots: str, stops: str) -> str:
    """The ``attempts`` and ``chain`` CTEs named so, from the given root rows, never entering a node in ``stops``."""
    return f"""
        , {attempts}(task_id, node_id) AS (
          {attempt_roots}
          UNION
          SELECT a.task_id, n.follow_up_of FROM {attempts} a JOIN plan_nodes n ON n.id=a.node_id
          WHERE n.follow_up_of IS NOT NULL
            AND EXISTS (SELECT 1 FROM plan_node_events e WHERE e.node_id=n.id AND e.event='revision_created')
        ), {chain}(task_id, node_id, plan_id) AS (
          {chain_roots}
          UNION
          SELECT c.task_id, n.id, n.plan_id FROM {chain} c
          JOIN plan_nodes n ON n.plan_id=c.plan_id AND n.parent_id=c.node_id
          WHERE n.id NOT IN ({stops})
          UNION
          SELECT c.task_id, r.id, r.plan_id FROM {chain} c
          CROSS JOIN plan_nodes d ON d.id=c.node_id AND d.spawned_thread_id IS NOT NULL
          CROSS JOIN plans p ON {delegation_edge_sql('d', 'p')}
          CROSS JOIN plan_nodes r ON r.plan_id=p.id AND r.parent_id IS NULL
          WHERE c.node_id={newest_attachment_sql('p')}
            AND r.id NOT IN ({stops})
        )
    """


def chain_ctes(*, only_tasks: bool = False, seeded: bool = False) -> str:
    """CTEs ``chain(task_id, node_id, plan_id)`` for the accepted tasks in the grain's range.

    Append to ``scope_cte() + outcome_ctes(grain, limited_to_range=False)``. Stopping
    at accepted tasks of every period, not only the queried ones, keeps a
    task's figure the same whichever periods are read. With ``only_tasks`` the walk starts
    only from the task ids in the ``:task_ids`` JSON array; every accepted task still
    stops it, so a task's chain is the same either way.

    With ``seeded`` (which needs ``only_tasks``) the ``:task_ids`` tasks that are terminal but not accepted
    outcomes (failed, closed, corrected, or accepted outside the scope), that no accepted task's chain reaches
    and that were not sent back for a revision, are also walked as ``seed_chain(task_id, node_id, plan_id)``
    by the same recursion, each as ``seeds(task_id, settled_at)`` (its first terminal time, the cut-off a
    caller reads its usage to). A seed's walk stops at accepted tasks and at other seeds' attempts; the accepted
    walk is unchanged, since seeds are only nodes it does not reach.
    """
    seed = "AND a.task_id IN (SELECT value FROM json_each(:task_ids))" if only_tasks else ""
    accepted = _walk(
        "attempts", "chain", "SELECT id, id FROM outcome_rows",
        f"""SELECT a.task_id, a.node_id, m.plan_id FROM attempts a JOIN outcome_rows o ON o.id=a.task_id
          JOIN plan_nodes m ON m.id=a.node_id
          WHERE o.period IS NOT NULL {seed}""",
        "SELECT node_id FROM attempts")
    if not seeded:
        return accepted
    return accepted + """
        , seeds(task_id, settled_at) AS (
          SELECT n.id, COALESCE((SELECT MIN(e.created_at) FROM plan_node_events e WHERE e.node_id=n.id AND e.new_state=n.state), n.updated_at)
          FROM plan_nodes n WHERE n.id IN (SELECT value FROM json_each(:task_ids))
            AND n.state IN ('accepted','accepted_with_followup','failed','closed')
            AND n.id NOT IN (SELECT id FROM outcome_rows) AND n.id NOT IN (SELECT node_id FROM chain)
            AND NOT EXISTS (SELECT 1 FROM plan_nodes r WHERE r.follow_up_of=n.id
              AND EXISTS (SELECT 1 FROM plan_node_events e WHERE e.node_id=r.id AND e.event='revision_created'))
        )""" + _walk(
        "seed_attempts", "seed_chain", "SELECT task_id, task_id FROM seeds",
        "SELECT a.task_id, a.node_id, m.plan_id FROM seed_attempts a JOIN plan_nodes m ON m.id=a.node_id",
        "SELECT node_id FROM attempts UNION SELECT node_id FROM seed_attempts")
