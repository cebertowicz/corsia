"""Close a delivered feedback task when the person has settled every item it holds.

The owner's task for another agent's batch asks for judgment on each item. A
suggestion the person accepted or rejected, or a comment resolved, has had it,
so the last such item settles the task with the outcomes as its evidence.
Acknowledged or replied-to comments are still owed judgment and never count,
and a batch that carried an edit item is never settled here. A batch can join
an earlier batch's task (``plans.feedback_join``), so a task settles only once
every batch delivered into it is settled. Only a task whose batches are all an
agent's settles here: the person resolves their own comments, so a task holding
any person's batch stays with its owner to close, the same as a task of only the
person's comments, whichever kind of batch joined it first.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _outcome(item) -> str | None:
    """How one comment item was settled, or None while its judgment is still owed."""
    if item["decision"]:
        return f"suggestion {item['decision']} by {item['decided_by']}"
    return "resolved" if item["resolved"] else None


def _batch_evidence(db, batch) -> list[str] | None:
    """One evidence line per item of a settled batch, or None while any is still owed."""
    items = db.execute(
        """SELECT c.id,c.resolved,c.decision,c.decided_by FROM doc_feedback_batch_items i
           JOIN doc_comments c ON c.id=i.comment_id WHERE i.batch_id=? ORDER BY c.created_at,c.id""",
        (batch["id"],),
    ).fetchall()
    # The batch's count includes its edit item, which delivery has already cleared.
    outcomes = [_outcome(item) for item in items]
    if batch["item_count"] != len(items) or None in outcomes:
        return None
    return [f"Comment {item['id']}: {outcome}" for item, outcome in zip(items, outcomes)]


def settle_batches_of(db, comment_id: str) -> None:
    """Settle each feedback task holding ``comment_id`` whose batches are all an agent's and now all settled."""
    from plans import public

    tasks = db.execute(
        """SELECT DISTINCT b.recipient_id,b.delivered_node_id FROM doc_feedback_batches b
           JOIN doc_feedback_batch_items i ON i.batch_id=b.id
           WHERE i.comment_id=? AND b.author_kind='agent' AND b.delivered_node_id IS NOT NULL""",
        (comment_id,),
    ).fetchall()
    for task in tasks:
        batches = db.execute(
            """SELECT id,item_count,author_kind FROM doc_feedback_batches
               WHERE delivered_node_id=? AND recipient_id=? ORDER BY created_at,id""",
            (task["delivered_node_id"], task["recipient_id"]),
        ).fetchall()
        if not all(batch["author_kind"] == "agent" for batch in batches):
            continue
        evidence = [_batch_evidence(db, batch) for batch in batches]
        if None in evidence:
            continue
        lines = "\n".join(line for batch_lines in evidence if batch_lines for line in batch_lines)
        try:
            public.settle_feedback_node(db, task["recipient_id"], batches[0]["id"], evidence=lines)
        except public.PlanError as exc:
            # The person's decision stands; the owner closes a task that cannot settle itself.
            log.warning("feedback task %s not settled: %s", task["delivered_node_id"], exc)
