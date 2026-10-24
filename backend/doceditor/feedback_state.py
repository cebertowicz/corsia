"""Read-only feedback batch projections for document consumers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from registry import models as registry_models

USER_AUTHOR_ID = "ui"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def undelivered_for_document(db, *, author_kind: str, author_id: str, artifact_id: str) -> list[dict]:
    """Return retry state for one author's document, preserving each batch's owning lane."""
    rows = db.execute(
        """SELECT id,item_count,undelivered_reason,attempts,delivery_policy FROM doc_feedback_batches
           WHERE state='undelivered' AND author_kind=? AND author_id=? AND artifact_id=?
           ORDER BY updated_at,id""",
        (author_kind, author_id, artifact_id),
    ).fetchall()
    return [dict(row) for row in rows]


def take_unreported_undelivered(db, author_id: str, now: str) -> list[dict[str, Any]]:
    """Mark and return agent failures not yet surfaced to their author."""
    rows = db.execute(
        """SELECT id,artifact_id,item_count,undelivered_reason FROM doc_feedback_batches
           WHERE author_kind='agent' AND author_id=? AND state='undelivered'
             AND author_result_notified_at IS NULL ORDER BY updated_at,id""",
        (author_id,),
    ).fetchall()
    if rows:
        db.executemany(
            "UPDATE doc_feedback_batches SET author_result_notified_at=?,updated_at=? WHERE id=?",
            [(now, now, row["id"]) for row in rows],
        )
    return [dict(row) for row in rows]


def has_pending_for_document(
    db, *, author_kind: str, author_id: str, artifact_id: str,
) -> bool:
    """Whether this author has an open or retryable batch for one document."""
    return db.execute(
        """SELECT 1 FROM doc_feedback_batches
           WHERE state IN ('open','undelivered') AND author_kind=? AND author_id=? AND artifact_id=?
           LIMIT 1""",
        (author_kind, author_id, artifact_id),
    ).fetchone() is not None


def open_counts_for_document(db, artifact_id: str) -> dict[str, int]:
    """The user's unsent comments on one document: the total and each held lane."""
    rows = db.execute(
        """SELECT batch.delivery_policy AS policy, COUNT(DISTINCT item.comment_id) AS count
           FROM doc_feedback_batches batch
           JOIN doc_feedback_batch_items item ON item.batch_id=batch.id
           WHERE batch.state='open' AND batch.author_kind='user'
             AND batch.author_id=? AND batch.artifact_id=?
           GROUP BY batch.delivery_policy""",
        (USER_AUTHOR_ID, artifact_id),
    ).fetchall()
    by_policy = {row["policy"]: int(row["count"]) for row in rows}
    return {
        "open_count": sum(by_policy.values()),
        "document_held_count": by_policy.get("document-held", 0),
        "image_held_count": by_policy.get("image-held", 0),
    }


def user_feedback_for_document(db, artifact_id: str) -> dict[str, Any]:
    """Project the user's durable feedback state for one document canvas."""
    artifact = registry_models.get_artifact(artifact_id, db=db)
    owner = db.execute(
        "SELECT title FROM threads WHERE id=?",
        (artifact["parent_thread_id"],),
    ).fetchone() if artifact else None
    return {
        **open_counts_for_document(db, artifact_id),
        "owner_label": owner["title"] if owner else None,
        "undelivered_batches": undelivered_for_document(
            db,
            author_kind="user",
            author_id=USER_AUTHOR_ID,
            artifact_id=artifact_id,
        ),
    }


def _new_reply_rows(user_id: str, artifact_id: str | None) -> tuple[str, tuple]:
    """The one definition of a new reply: the FROM/JOIN/WHERE every read of it shares.

    A reply is new for a user when someone else wrote it, in an open root the
    user touched (wrote it or replied in it), and it is newer than the user's
    seen stamp on that root: by time, then by reply id.
    """
    # A document read binds both sides to its artifact so idx_doc_comments_artifact bounds the scan to that document.
    scoped = artifact_id is not None
    sql = f"""FROM doc_comments reply
           JOIN doc_comments root ON root.id=reply.parent_id
           LEFT JOIN doc_comment_thread_views thread_view
             ON thread_view.user_id=? AND thread_view.root_id=root.id
           WHERE reply.author_kind<>'user' AND root.resolved=0
             {"AND reply.artifact_id=? AND root.artifact_id=?" if scoped else ""}
             AND (root.author_kind='user' OR EXISTS (
                 SELECT 1 FROM doc_comments touched
                 WHERE touched.parent_id=root.id AND touched.author_kind='user'
                   {"AND touched.artifact_id=?" if scoped else ""}
             ))
             AND (
                 reply.created_at>COALESCE(thread_view.seen_at, '')
                 OR (
                     thread_view.seen_at IS NOT NULL
                     AND reply.created_at=thread_view.seen_at
                     AND reply.id>thread_view.seen_reply_id
                 )
             )"""
    return sql, ((user_id, artifact_id, artifact_id, artifact_id) if scoped else (user_id,))


def _new_replies(db, user_id: str, artifact_id: str) -> list[tuple[str, str]]:
    """One document's new replies as (root id, reply id) in thread order, one row per reply."""
    sql, params = _new_reply_rows(user_id, artifact_id)
    rows = db.execute(
        f"SELECT root.id AS root_id, reply.id AS reply_id {sql} ORDER BY reply.created_at, reply.id",
        params,
    ).fetchall()
    return [(str(row["root_id"]), str(row["reply_id"])) for row in rows]


def reply_waiting_ids(db, user_id: str) -> set[str]:
    """Return documents containing a thread with a new reply: one row per document, not per reply."""
    sql, params = _new_reply_rows(user_id, None)
    rows = db.execute(f"SELECT DISTINCT root.artifact_id AS artifact_id {sql}", params).fetchall()
    return {str(row["artifact_id"]) for row in rows}


def annotate_thread_attention(db, user_id: str, artifact_id: str, threads: list[dict]) -> None:
    """Attach each thread's new reply ids to the comment-list projection."""
    new_by_root: dict[str, list[str]] = {}
    for root_id, reply_id in _new_replies(db, user_id, artifact_id):
        new_by_root.setdefault(root_id, []).append(reply_id)
    for thread in threads:
        thread["new_reply_ids"] = new_by_root.get(thread["id"], [])


def mark_thread_viewed(
    db, user_id: str, artifact_id: str, root_id: str, seen_reply_id: str,
) -> list[str]:
    """Persist the newest rendered reply for one root, never moving its stamp back.

    Returns the root's new reply ids that remain after the stamp.
    """
    root = db.execute(
        "SELECT id FROM doc_comments WHERE id=? AND artifact_id=? AND parent_id IS NULL",
        (root_id, artifact_id),
    ).fetchone()
    if root is None:
        raise ValueError("root_not_found")
    reply = db.execute(
        "SELECT id,created_at FROM doc_comments WHERE id=? AND parent_id=?",
        (seen_reply_id, root_id),
    ).fetchone()
    if reply is None:
        raise ValueError("reply_not_found")
    db.execute(
        """INSERT INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id)
           VALUES (?,?,?,?)
           ON CONFLICT(user_id,root_id) DO UPDATE SET
             seen_at=excluded.seen_at, seen_reply_id=excluded.seen_reply_id
           WHERE excluded.seen_at>doc_comment_thread_views.seen_at
              OR (excluded.seen_at=doc_comment_thread_views.seen_at
                  AND excluded.seen_reply_id>doc_comment_thread_views.seen_reply_id)""",
        (user_id, root_id, reply["created_at"], reply["id"]),
    )
    return [reply_id for waiting_root, reply_id in _new_replies(db, user_id, artifact_id) if waiting_root == root_id]


def annotate_pane(pane: dict, user_id: str, *, db) -> dict:
    """Add the passive reply marker to every artifact row in a pane payload."""
    waiting = reply_waiting_ids(db, user_id)

    def visit(nodes: list[dict]) -> None:
        for node in nodes:
            if node.get("kind") == "artifact":
                node["reply_waiting"] = node.get("id") in waiting
            else:
                visit(node.get("children") or [])

    visit(pane.get("nodes") or [])
    return pane


def seconds_until_next_due(now: datetime, *, db) -> float | None:
    """Seconds until the earliest open batch still ahead of its deadline, or None when there is none.

    An already-overdue batch is left to the ordinary sweep, so one that keeps failing cannot hide the others.
    """
    row = db.execute(
        "SELECT MIN(closes_at) FROM doc_feedback_batches WHERE state='open' AND closes_at>?", (now.isoformat(),),
    ).fetchone()
    return None if row[0] is None else (datetime.fromisoformat(row[0]) - now).total_seconds()
