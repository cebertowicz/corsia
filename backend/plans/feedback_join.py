"""A reply joins the open task its comment thread already has.

A comment thread is one conversation, so a batch of replies whose thread already
has an open task for the same recipient, from the same author, goes into that
task rather than opening a second one. The task must be one its owner will pick
up as it is: a finished task, or one that waits (a hold, an armed watch, an
unmet dependency), would not show the reply until later, so then the reply gets
its own task as before.
"""

from __future__ import annotations

from typing import Any

from plans import intent_words, node_waits, service
from plans.feedback_items import batch_value
from plans.plan_write import transaction
from plans.review_requests import _current, _notify

JOINED_EVENT = "feedback_batch_joined"
# Under review a task has left its owner's hands, so it is not one they pick up.
_WORKABLE = ("pending", "active")

# Earlier delivered batches from this author to this recipient that carried a
# comment of the thread, oldest first.
_THREAD_TASKS = """
    SELECT b.delivered_node_id FROM doc_comments c
      JOIN doc_feedback_batch_items i ON i.comment_id=c.id
      JOIN doc_feedback_batches b ON b.id=i.batch_id
     WHERE c.artifact_id=? AND (c.id=? OR c.parent_id=?)
       AND b.recipient_id=? AND b.author_kind=? AND b.author_id=? AND b.id<>?
       AND b.delivered_node_id IS NOT NULL
     ORDER BY b.created_at,b.id
"""


def _waits(db, node) -> bool:
    node_id = str(node["id"])
    return bool(
        node_waits.open_holds(db, [node_id], compact=True)
        or node_waits.armed_watches(db, [node_id], compact=True)
        or node_waits.waits_on_dependency(db, node_id, str(node["state"]))
    )


def _thread_task(db, recipient_thread_id: str, batch: Any, root_id: str) -> str | None:
    """The thread's first open task for this recipient, if its owner will pick it up as it is."""
    rows = db.execute(_THREAD_TASKS, (
        batch_value(batch, "artifact_id"), root_id, root_id, recipient_thread_id,
        batch_value(batch, "author_kind"), batch_value(batch, "author_id"), batch_value(batch, "id"),
    )).fetchall()
    for node_id in dict.fromkeys(str(row["delivered_node_id"]) for row in rows):
        node = service._node_row(db, _current(db, node_id))
        if node["state"] in service._TERMINAL_STATES:
            continue
        if node["state"] not in _WORKABLE or _waits(db, node):
            return None
        return str(node["id"])
    return None


def open_thread_task(
    db, recipient_thread_id: str, batch: Any, items: list[dict[str, Any]],
) -> str | None:
    """The one open task a batch of replies joins, or None when it needs its own."""
    if not items or any(
        item["kind"] != "comment" or not item.get("parent_id") or item.get("event") == "reopened"
        for item in items
    ):
        return None
    tasks = {
        _thread_task(db, recipient_thread_id, batch, root)
        for root in dict.fromkeys(str(item["parent_id"]) for item in items)
    }
    return tasks.pop() if len(tasks) == 1 else None


def join(db, node_id: str, batch: Any, blocks: str, words, *, provenance: str) -> None:
    """Add the batch's items to the task's description and words, once per batch."""
    batch_id = str(batch_value(batch, "id"))
    with transaction(db) as connection:
        if connection.execute(
            "SELECT 1 FROM plan_node_events WHERE node_id=? AND event=? "
            "AND json_extract(payload_json,'$.batch_id')=? LIMIT 1",
            (node_id, JOINED_EVENT, batch_id),
        ).fetchone():
            return
        node = service._node_row(connection, node_id)
        service.touch_node(connection, node_id, description=f"{node['description'] or ''}\n\n{blocks}".lstrip())
        intent_words.append(connection, node_id, words, revision=node["requirements_revision"])
        service._event(connection, node_id, JOINED_EVENT, actor_id=None,
                       provenance=provenance, payload={"batch_id": batch_id})
        _notify(connection, node["plan_id"])
