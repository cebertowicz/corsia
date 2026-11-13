"""The agents a reply box says it goes to: exactly the ones delivery wakes.

Delivery resolves a comment's recipients with the rule in ``feedback_comments``. The box asks that
same rule about the person's next reply on each thread, for every thread in one pass, so it cannot
name an agent that delivery will not reach and costs a fixed number of reads however many threads
a document has.
"""

from __future__ import annotations

from typing import Any

from doceditor import feedback_comments


def _joined_orders(db, root_ids: list[str]) -> dict[str, list[str]]:
    """Per thread, the agents in the order they joined it: by their first comment or first tag, oldest first."""
    orders: dict[str, list[str]] = {root_id: [] for root_id in root_ids}
    if not orders:
        return orders
    marks = ",".join("?" for _ in orders)
    ids = tuple(orders)
    rows = db.execute(
        f"""SELECT COALESCE(c.parent_id, c.id) AS root_id, c.author_kind, c.author_thread_id, t.agent_id FROM doc_comments c
            LEFT JOIN doc_comment_tags t ON t.comment_id=c.id
            WHERE c.id IN ({marks}) OR c.parent_id IN ({marks}) ORDER BY c.created_at, c.rowid, t.rowid""", ids + ids).fetchall()
    for row in rows:
        seen = orders[row["root_id"]]
        if row["author_kind"] == "agent" and row["author_thread_id"] and row["author_thread_id"] not in seen:
            seen.append(str(row["author_thread_id"]))
        if row["agent_id"] and row["agent_id"] not in seen:
            seen.append(str(row["agent_id"]))
    return orders


def _names(db, ids: set[str]) -> dict[str, str]:
    """Thread titles by id; an id that is not a thread has no name to show."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(f"SELECT id, title FROM threads WHERE id IN ({marks})", tuple(ids)).fetchall()
    return {str(row["id"]): (row["title"] or "").strip() or str(row["id"]) for row in rows}


def annotate(db, artifact: dict[str, Any] | None, roots: list[dict[str, Any]]) -> None:
    """Give each top-level comment `reply_goes_to`, the agents a reply on it wakes.

    The document's recipients (its supervisor and owner) lead, then the others in the order they joined the thread.
    """
    if artifact is None or not roots:
        return
    by_root = feedback_comments.recipients_for_replies(db, artifact, roots)
    joined = _joined_orders(db, list(by_root))
    names = _names(db, {agent for ids in by_root.values() for agent in ids})
    document = set(feedback_comments.artifact_recipients(db, artifact))
    for root in roots:
        root_id = str(root["id"])
        order = joined[root_id]
        ids = sorted(by_root[root_id], key=lambda i: (i not in document, order.index(i) if i in order else len(order), i))
        root["reply_goes_to"] = [{"id": i, "name": names[i]} for i in ids if i in names]
