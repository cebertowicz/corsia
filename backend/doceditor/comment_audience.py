"""Who a comment box says it goes to, from the same rules delivery uses (design prototype).

One resolver for the label and for delivery: the document's owner when it can receive, else its
supervisor (the scope's conductor until supervisors are stored), else no one. A reply goes to
``feedback_comments.recipients`` for a stand-in reply, so the box cannot say something delivery
will not do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doceditor import feedback_comments
from doceditor.feedback_delivery import recipient_problem
from doceditor.feedback_state import USER_AUTHOR_ID
from infra.auth import is_owner_sentinel
from registry.shared_principals import document_scope


def _name(db, thread_id: str) -> str:
    row = db.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone()
    return ((row["title"] if row else "") or "").strip() or thread_id


def document_audience(db, artifact: dict[str, Any] | None) -> dict[str, Any]:
    """`{state: 'agent', id, name, via}` or `{state: 'none'}` for a new comment on this document."""
    if artifact is None:
        return {"state": "none"}
    owner = str(artifact.get("parent_thread_id") or "")
    candidates = [("owner", owner)] if owner and not is_owner_sentinel(owner) else []
    if not candidates and artifact.get("path"):
        from infra.config import WORKSPACE

        _, conductor = document_scope(Path(str(artifact["path"])), workspace=WORKSPACE, db=db)
        if conductor:
            candidates.append(("supervisor", conductor))
    for via, thread_id in candidates:
        if recipient_problem(db, thread_id) is None:
            return {"state": "agent", "id": thread_id, "name": _name(db, thread_id), "via": via}
    return {"state": "none"}


def person_left(db, root_id: str) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='doc_comment_person_left'").fetchone()
        and db.execute("SELECT 1 FROM doc_comment_person_left WHERE root_id=?", (root_id,)).fetchone())


def set_person_left(db, root_id: str, left: bool) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS doc_comment_person_left(root_id TEXT PRIMARY KEY)")
    if left:
        db.execute("INSERT OR IGNORE INTO doc_comment_person_left(root_id) VALUES (?)", (root_id,))
    else:
        db.execute("DELETE FROM doc_comment_person_left WHERE root_id=?", (root_id,))


def joined_order(db, root_id: str) -> list[str]:
    """Agents in the order they joined this comment chat: by their first comment or first tag, oldest first."""
    seen: list[str] = []
    rows = db.execute(
        "SELECT id, author_kind, author_thread_id FROM doc_comments WHERE id=? OR parent_id=? ORDER BY created_at, rowid",
        (root_id, root_id)).fetchall()
    for row in rows:
        if row["author_kind"] == "agent" and row["author_thread_id"] and row["author_thread_id"] not in seen:
            seen.append(str(row["author_thread_id"]))
        for tag in db.execute("SELECT agent_id FROM doc_comment_tags WHERE comment_id=? ORDER BY rowid", (row["id"],)):
            if tag["agent_id"] not in seen:
                seen.append(str(tag["agent_id"]))
    return seen


def reply_audience(db, artifact: dict[str, Any], root: dict[str, Any]) -> list[dict[str, str]]:
    """Agents that wake for the person's next reply on this thread, named.

    Order: the document's owner (or its supervisor when it falls back) first, then the others in the order they joined the chat.
    """
    stand_in = {"id": "next-reply", "artifact_id": artifact["id"], "parent_id": root["id"],
                "author_kind": "user", "tagged_agent_ids": []}
    ids = [i for i in feedback_comments.recipients(db, stand_in) if i != USER_AUTHOR_ID]
    # The person's comments reach the document's resolved recipient, which is the owner or its supervisor.
    resolved = document_audience(db, artifact)
    if resolved["state"] == "agent" and resolved["id"] not in ids:
        ids.append(resolved["id"])
    joined = joined_order(db, str(root["id"]))
    first = resolved["id"] if resolved["state"] == "agent" else None
    ids.sort(key=lambda i: (i != first, joined.index(i) if i in joined else len(joined), i))
    ready = [i for i in ids if recipient_problem(db, i) is None]
    return [{"id": i, "name": _name(db, i)} for i in ready]


def annotate(db, artifact: dict[str, Any] | None, roots: list[dict[str, Any]]) -> None:
    """Give each open top-level comment `reply_goes_to` and `left_by_you`."""
    if artifact is None:
        return
    for root in roots:
        root["reply_goes_to"] = reply_audience(db, artifact, root)
        root["left_by_you"] = person_left(db, str(root["id"]))
