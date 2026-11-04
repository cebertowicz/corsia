"""What the person has to decide about held replies, read once for every surface.

The attention queue, the chats list and Activity's awaiting count all read ``pending``, so they
never disagree about which held replies wait. A reply is shown on its sender's nearest visible
chat (a worker's parent), and only while that chat is live and outside an archived project.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core.thread_identity import root_visible_thread_ids
import infra.held_replies as held_replies
from plans.attention_detail import HELD_REPLY_PRIORITY

HELD_REPLY_RENDERER = "held-reply"


def pending(db: sqlite3.Connection, user_id: str, *, with_text: bool = True) -> list[dict[str, Any]]:
    """Every held reply waiting on this person, oldest first, with the names a person reads."""
    # Import lazily: api initialization imports plans.public via measurement.
    from api.provider_meta import PROVIDER_META

    rows = db.execute(
        "SELECT h.id, h.sender_thread_id, h.recipient_thread_id, h.error, h.created_at"
        + (", h.text" if with_text else "")
        + " FROM held_replies h JOIN threads s ON s.id=h.sender_thread_id "
        "WHERE h.user_id=? AND h.state=? AND s.purged_at IS NULL ORDER BY h.created_at, h.id",
        (user_id, held_replies.HELD),
    ).fetchall()
    if not rows:
        return []
    roots = root_visible_thread_ids([str(row["sender_thread_id"]) for row in rows], db=db)
    visible_ids = sorted({
        roots.get(str(row["sender_thread_id"])) or str(row["sender_thread_id"]) for row in rows
    })
    visible = {
        str(row["id"]): row for row in db.execute(
            "SELECT t.id, t.title, t.project_id, p.name AS project_name FROM threads t "
            "LEFT JOIN projects p ON p.id=t.project_id "
            f"WHERE t.id IN ({','.join('?' for _ in visible_ids)}) AND t.user_id=? "
            "AND t.purged_at IS NULL AND t.archived_at IS NULL "
            "AND COALESCE(t.lifecycle_state,'active')!='archived' "
            "AND COALESCE(p.status,'active')!='archived'",
            (*visible_ids, user_id),
        )
    }
    recipient_ids = sorted({str(row["recipient_thread_id"]) for row in rows})
    recipients = {
        str(row["id"]): row for row in db.execute(
            "SELECT t.id, t.title, (SELECT m.provider FROM models m "
            "WHERE m.id=COALESCE(t.pending_model,t.model) OR m.litellm_model=COALESCE(t.pending_model,t.model) "
            "LIMIT 1) AS provider, (SELECT m.harness FROM models m "
            "WHERE m.id=COALESCE(t.pending_model,t.model) OR m.litellm_model=COALESCE(t.pending_model,t.model) "
            "LIMIT 1) AS harness FROM threads t "
            f"WHERE t.id IN ({','.join('?' for _ in recipient_ids)})",
            recipient_ids,
        )
    }
    out = []
    for row in rows:
        sender = str(row["sender_thread_id"])
        shown = visible.get(roots.get(sender) or sender)
        if shown is None:
            continue
        recipient = recipients.get(str(row["recipient_thread_id"]))
        provider = str(recipient["provider"] or "") if recipient else ""
        harness = str(recipient["harness"] or "") if recipient else ""
        vendor = {"claude-code": "anthropic", "codex": "openai"}.get(harness, provider)
        out.append({
            "id": str(row["id"]),
            "sender_thread_id": sender,
            "visible_thread_id": str(shown["id"]),
            "visible_title": shown["title"] or "Untitled thread",
            "project_id": shown["project_id"],
            "project_label": shown["project_name"],
            "recipient_label": held_replies.recipient_label(recipient["title"] if recipient else None),
            "provider_label": PROVIDER_META.get(vendor, {}).get("label", provider) if provider else "a cloud provider",
            **({"text": str(row["text"])} if with_text else {}),
            "error": row["error"],
            "created_at": str(row["created_at"]),
        })
    return out


def queue_rows(db: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """The held replies as attention rows, for the queue's own ordering to place."""
    return [
        {
            "external_id": item["id"],
            "renderer": HELD_REPLY_RENDERER,
            "deferred": False,
            "priority": HELD_REPLY_PRIORITY,
            "raised_at": item["created_at"],
            "detail": {
                "id": item["id"],
                "thread_id": item["visible_thread_id"],
                "title": (
                    f"Send {item['visible_title']}'s reply to {item['recipient_label']} "
                    f"on {item['provider_label']}?"
                ),
                "text": item["text"],
                "recipient": item["recipient_label"],
                "provider": item["provider_label"],
                "error": item["error"],
                "raised_at": item["created_at"],
                "source": {
                    "thread_id": item["visible_thread_id"],
                    "thread_label": item["visible_title"],
                    "project_id": item["project_id"],
                    "project_label": item["project_label"],
                    "parent_reviewed": False,
                },
            },
        }
        for item in pending(db, user_id)
    ]


def count(db: sqlite3.Connection, user_id: str, *, project_id: str | None = None) -> int:
    """How many held replies wait on this person: Activity's share of the awaiting count.

    With ``project_id``, only those shown on a chat in that project, as a project's Activity counts.
    """
    items = pending(db, user_id, with_text=False)
    if project_id is None:
        return len(items)
    return sum(1 for item in items if item["project_id"] == project_id)


def items_by_thread(db: sqlite3.Connection, user_id: str) -> dict[str, list[dict[str, Any]]]:
    """The chats list's ``hold`` items, grouped on each reply's visible chat."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in pending(db, user_id, with_text=False):
        grouped.setdefault(item["visible_thread_id"], []).append({
            "kind": "hold",
            "title": f"Approve reply to {item['recipient_label']}",
            "node_id": None,
        })
    return grouped
