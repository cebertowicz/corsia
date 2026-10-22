"""Whether a document-feedback task can still take more comments before its owner starts it."""

from __future__ import annotations


def feedback_node_has_started(db, node_id: str) -> bool:
    """Whether the task was ever selected or billed; the rule `inherit_open_child_nodes` uses for "has run"."""
    return db.execute(
        """SELECT 1 FROM plan_node_attempts WHERE node_id=?
           UNION SELECT 1 FROM llm_usage WHERE plan_node_id=? LIMIT 1""",
        (node_id, node_id),
    ).fetchone() is not None


def feedback_node_takes_more(db, node_id: str, recipient_thread_id: str) -> bool:
    """Whether the owner's task is still active and unstarted, so a new comment can join it."""
    row = db.execute(
        """SELECT 1 FROM plan_nodes node JOIN plans plan ON plan.id=node.plan_id
           WHERE node.id=? AND node.state='active' AND plan.owner_kind='thread' AND plan.owner_id=?""",
        (node_id, recipient_thread_id),
    ).fetchone()
    return row is not None and not feedback_node_has_started(db, node_id)
