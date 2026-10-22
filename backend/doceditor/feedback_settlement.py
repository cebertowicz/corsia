"""Close a delivered feedback task when the person has settled every item of its batch.

The owner's task for another agent's batch asks for judgment on each item. A
suggestion the person accepted or rejected, or a comment resolved, has had it,
so the last such item settles the task with the outcomes as its evidence.
Acknowledged or replied-to comments are still owed judgment and never count,
and a batch that carried an edit item is never settled here.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _outcome(item) -> str | None:
    """How one comment item was settled, or None while its judgment is still owed."""
    if item["decision"]:
        return f"suggestion {item['decision']} by {item['decided_by']}"
    return "resolved" if item["resolved"] else None


def settle_batches_of(db, comment_id: str) -> None:
    """Settle each delivered agent batch holding ``comment_id`` whose items are now all settled."""
    from plans import public

    batches = db.execute(
        """SELECT b.id,b.recipient_id,b.item_count FROM doc_feedback_batches b
           JOIN doc_feedback_batch_items i ON i.batch_id=b.id
           WHERE i.comment_id=? AND b.author_kind='agent'""",
        (comment_id,),
    ).fetchall()
    for batch in batches:
        items = db.execute(
            """SELECT c.id,c.resolved,c.decision,c.decided_by FROM doc_feedback_batch_items i
               JOIN doc_comments c ON c.id=i.comment_id WHERE i.batch_id=? ORDER BY c.created_at,c.id""",
            (batch["id"],),
        ).fetchall()
        # The batch's count includes its edit item, which delivery has already cleared.
        outcomes = [_outcome(item) for item in items]
        if batch["item_count"] != len(items) or None in outcomes:
            continue
        evidence = "\n".join(f"Comment {item['id']}: {outcome}" for item, outcome in zip(items, outcomes))
        try:
            public.settle_feedback_node(db, batch["recipient_id"], batch["id"], evidence=evidence)
        except public.PlanError as exc:
            # The person's decision stands; the owner closes a task that cannot settle itself.
            log.warning("feedback batch %s task not settled: %s", batch["id"], exc)
