"""The agents a comment box says it goes to: exactly the ones delivery wakes.

Delivery resolves a comment's recipients in ``feedback_comments.recipients``. The box asks the same
function about a stand-in for the person's next comment or reply, so it cannot name an agent that
delivery will not reach, and it names no one when delivery has no recipient.
"""

from __future__ import annotations

from typing import Any

from doceditor import feedback_comments


def _names(db, ids: list[str]) -> list[dict[str, str]]:
    """The thread titles of recipient ids; an id that is not a thread has no name to show."""
    named: list[dict[str, str]] = []
    for thread_id in ids:
        row = db.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is not None:
            named.append({"id": thread_id, "name": (row["title"] or "").strip() or thread_id})
    return named


def joined_order(db, root_id: str) -> list[str]:
    """Agents in the order they joined this comment chat: by their first comment or first tag, oldest first."""
    seen: list[str] = []
    rows = db.execute(
        """SELECT c.author_kind, c.author_thread_id, t.agent_id FROM doc_comments c
           LEFT JOIN doc_comment_tags t ON t.comment_id=c.id
           WHERE c.id=? OR c.parent_id=? ORDER BY c.created_at, c.rowid, t.rowid""",
        (root_id, root_id)).fetchall()
    for row in rows:
        if row["author_kind"] == "agent" and row["author_thread_id"] and row["author_thread_id"] not in seen:
            seen.append(str(row["author_thread_id"]))
        if row["agent_id"] and row["agent_id"] not in seen:
            seen.append(str(row["agent_id"]))
    return seen


def goes_to(db, artifact: dict[str, Any], root: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Agents the person's next comment (no `root`) or next reply on `root` wakes, named.

    The document's owner leads, then the others in the order they joined the chat.
    """
    stand_in = {"id": "next-comment", "artifact_id": artifact["id"], "parent_id": root["id"] if root else None,
                "author_kind": "user", "tagged_agent_ids": []}
    ids = list(feedback_comments.recipients(db, stand_in))
    joined = joined_order(db, str(root["id"])) if root else []
    owner = str(artifact.get("parent_thread_id") or "")
    ids.sort(key=lambda i: (i != owner, joined.index(i) if i in joined else len(joined), i))
    return _names(db, ids)


def for_new_comment(db, artifact: dict[str, Any] | None) -> dict[str, str]:
    """`{state: 'agent', id, name}` for the agent a new comment on this document goes to, else `{state: 'none'}`."""
    named = goes_to(db, artifact) if artifact is not None else []
    return {"state": "agent", **named[0]} if named else {"state": "none"}


def annotate(db, artifact: dict[str, Any] | None, roots: list[dict[str, Any]]) -> None:
    """Give each top-level comment `reply_goes_to`, the agents a reply on it wakes."""
    if artifact is None:
        return
    for root in roots:
        root["reply_goes_to"] = goes_to(db, artifact, root)
