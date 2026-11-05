"""A reply joins the open task its comment thread already has.

A comment thread is one conversation, so a batch of replies whose thread already
has an open task for the same recipient, from the same author, goes into that
task rather than opening a second one. A task its owner has not been handed yet
is picked up as it is, so the reply is in its description when the owner first
reads it. A person's reply also joins a task its owner is working on now; the
owner may have read that task already, so the caller wakes it with the reply
(``plans.person_input.joined_reply``); working on it now is the same test
that picks which tasks that wake reaches (``plans.person_input.running``). A
finished task, a task that waits (a hold, an armed watch, an unmet
dependency), or a handed task its owner is not running would not show the
reply until later; the reply then joins a later task of the thread that
qualifies, or gets its own task as before. The joined task's title is
recounted over every item it now holds.

A task can therefore hold several batches. Each batch's ``delivered_node_id``
is the one record of the task that holds it: settlement reads it to settle a
task only once every batch in it is settled, and a batch already delivered
resolves to that task instead of joining twice.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from infra.write_effects import after_commit
from plans import mutations, node_waits, notifications, service
from plans.feedback_items import batch_value, load_feedback_items
from plans.plan_write import transaction

# A feedback task is made active at once; until its owner selects it, it is still queued.
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


class Join(NamedTuple):
    """The task a batch of replies joins; ``working`` when its owner is already on it."""

    node_id: str
    working: bool


def _handed(db, node_id: str) -> bool:
    return db.execute("SELECT 1 FROM plan_node_attempts WHERE node_id=? LIMIT 1", (node_id,)).fetchone() is not None


def _waits(db, node) -> bool:
    node_id = str(node["id"])
    return bool(
        node_waits.open_holds(db, [node_id], compact=True)
        or node_waits.armed_watches(db, [node_id], compact=True)
        or node_waits.waits_on_dependency(db, node_id, str(node["state"]))
    )


def _being_worked(db, node, batch: Any) -> bool:
    """A person's reply can reach the owner of this handed task now: the wake's own test finds it running."""
    from plans import person_input

    return batch_value(batch, "author_kind") == "user" and bool(person_input.running(db, [node]))


def _thread_task(db, recipient_thread_id: str, batch: Any, root_id: str) -> Join | None:
    """The thread's first open task not handed yet, else (for a person's reply) the first one being worked."""
    rows = db.execute(_THREAD_TASKS, (
        batch_value(batch, "artifact_id"), root_id, root_id, recipient_thread_id,
        batch_value(batch, "author_kind"), batch_value(batch, "author_id"), batch_value(batch, "id"),
    )).fetchall()
    working = None
    for node_id in dict.fromkeys(str(row["delivered_node_id"]) for row in rows):
        node = service._node_row(db, node_id)
        if node["state"] not in _WORKABLE or _waits(db, node):
            continue
        if not _handed(db, node_id):
            return Join(node_id, False)
        if working is None and _being_worked(db, node, batch):
            working = Join(node_id, True)
    return working


def open_thread_task(
    db, recipient_thread_id: str, batch: Any, items: list[dict[str, Any]],
) -> Join | None:
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


def delivered_task(db, batch_id: str) -> str | None:
    """The task that already holds a delivered batch, or None."""
    row = db.execute("SELECT delivered_node_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()
    return str(row["delivered_node_id"]) if row and row["delivered_node_id"] else None


def earlier_items(db, node_id: str, batch: Any) -> list[dict[str, Any]]:
    """The items of the batches already delivered into the task, oldest first.

    Delivery clears a batch's edit item, so a batch whose count exceeds its
    comments held one, and it counts as one edit.
    """
    rows = db.execute(
        "SELECT * FROM doc_feedback_batches WHERE delivered_node_id=? AND id<>? ORDER BY created_at,id",
        (node_id, batch_value(batch, "id")),
    ).fetchall()
    held: list[dict[str, Any]] = []
    for row in rows:
        items = load_feedback_items(db, row)
        held += items + ([{"kind": "edit"}] if int(row["item_count"]) > len(items) else [])
    return held


def join(db, node_id: str, *, title: str, blocks: str, intent: str | None, provenance: str) -> None:
    """Add a batch's items to the task through the node's one amend path.

    ``intent`` restates the task's intent when the person's new comments change
    what it must answer; None leaves it as it is.
    """
    with transaction(db) as connection:
        node = service._node_row(connection, node_id)
        mutations.edit_node(
            node_id, title=title, description=f"{node['description'] or ''}\n\n{blocks}".lstrip(),
            **({} if intent is None else {"intent": intent}), provenance=provenance, db=connection,
        )
        plan_id = str(node["plan_id"])

        def notify() -> None:
            """Refresh the plan view once the amend commits."""
            notifications.notify_after_commit((plan_id,), db=connection)

        after_commit(connection, notify)
