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


# The person's unsent items: each comment once, plus one for a batch's coalesced document edit. Every count of
# what is waiting reads this one projection, so an edit-only batch has a Send too.
_USER_ITEMS = """
    SELECT batch.id AS batch_id, batch.artifact_id AS artifact_id, batch.recipient_id AS recipient_id,
           batch.delivery_policy AS policy, batch.opened_at AS opened_at, item.comment_id AS item_key
      FROM doc_feedback_batches batch JOIN doc_feedback_batch_items item ON item.batch_id=batch.id
     WHERE batch.state IN ({states}) AND batch.author_kind='user' AND batch.author_id=?
    UNION ALL
    SELECT batch.id, batch.artifact_id, batch.recipient_id, batch.delivery_policy, batch.opened_at,
           'edit:' || batch.id
      FROM doc_feedback_batches batch JOIN doc_feedback_edit_items edit ON edit.batch_id=batch.id
     WHERE batch.state IN ({states}) AND batch.author_kind='user' AND batch.author_id=?"""
_OPEN_USER_ITEMS = _USER_ITEMS.format(states="'open'")
_PENDING_USER_ITEMS = _USER_ITEMS.format(states="'open','undelivered'")


def open_counts_for_document(db, artifact_id: str) -> dict[str, int]:
    """The user's unsent items on one document: the total and each held lane."""
    rows = db.execute(
        f"SELECT policy, COUNT(DISTINCT item_key) AS count FROM ({_OPEN_USER_ITEMS}) WHERE artifact_id=? GROUP BY policy",
        (USER_AUTHOR_ID, USER_AUTHOR_ID, artifact_id),
    ).fetchall()
    by_policy = {row["policy"]: int(row["count"]) for row in rows}
    return {
        "open_count": sum(by_policy.values()),
        "document_held_count": by_policy.get("document-held", 0),
        "image_held_count": by_policy.get("image-held", 0),
    }


def held_counts_for_thread(db, thread_id: str, policy: str) -> list[dict[str, Any]]:
    """One row per document the thread holds in one lane: the artifact and its unsent items."""
    rows = db.execute(
        f"""SELECT artifact_id, COUNT(DISTINCT item_key) AS count FROM ({_OPEN_USER_ITEMS})
            WHERE recipient_id=? AND policy=? GROUP BY artifact_id ORDER BY MIN(opened_at), artifact_id""",
        (USER_AUTHOR_ID, USER_AUTHOR_ID, thread_id, policy),
    ).fetchall()
    return [{"artifact_id": row["artifact_id"], "open_count": int(row["count"])} for row in rows]


# A retry from one lane's control also retries the immediate batches that failed beside it, since a
# document header is where those show; an image lane's retry stays image-only.
RETRY_LANES = {
    "immediate": ("immediate",),
    "document-held": ("document-held", "immediate"),
    "image-held": ("image-held",),
}


def user_batch_ids(
    db, state: str, *, artifact_id: str | None = None, recipient_id: str | None = None,
    policies: tuple[str, ...] | None = None,
) -> list[str]:
    """The user's batches in one state, optionally narrowed to a document, a recipient thread and lanes."""
    where, params = ["state=?", "author_kind='user'", "author_id=?"], [state, USER_AUTHOR_ID]
    for column, value in (("artifact_id", artifact_id), ("recipient_id", recipient_id)):
        if value is not None:
            where.append(f"{column}=?")
            params.append(value)
    if policies is not None:
        where.append(f"delivery_policy IN ({','.join('?' * len(policies))})")
        params.extend(policies)
    rows = db.execute(
        f"SELECT id FROM doc_feedback_batches WHERE {' AND '.join(where)} ORDER BY opened_at,id", params,
    ).fetchall()
    return [row["id"] for row in rows]


def pending_item_keys(db, batch_ids: list[str]) -> dict[str, set[str]]:
    """The items each named user batch holds now, read before a close or retry consumes them."""
    keys: dict[str, set[str]] = {batch_id: set() for batch_id in batch_ids}
    marks = ",".join("?" * len(batch_ids))
    rows = db.execute(
        f"SELECT batch_id, item_key FROM ({_PENDING_USER_ITEMS}) WHERE batch_id IN ({marks})",
        (USER_AUTHOR_ID, USER_AUTHOR_ID, *batch_ids),
    ).fetchall() if batch_ids else []
    for row in rows:
        keys[row["batch_id"]].add(row["item_key"])
    return keys


def outcome_counts(db, keys: dict[str, set[str]]) -> dict[str, int]:
    """What the batches read by `pending_item_keys` ended in: items delivered, and items left undelivered."""
    rows = db.execute(
        f"SELECT id,state FROM doc_feedback_batches WHERE id IN ({','.join('?' * len(keys))})", tuple(keys),
    ).fetchall() if keys else []
    ended: dict[str, set[str]] = {"delivered": set(), "undelivered": set()}
    for row in rows:
        if row["state"] in ended:
            ended[row["state"]] |= keys[row["id"]]
    return {"sent_count": len(ended["delivered"]), "undelivered_count": len(ended["undelivered"])}


def undelivered_documents(db, keys: dict[str, set[str]]) -> list[dict[str, Any]]:
    """The documents whose batches read by `pending_item_keys` ended undelivered, each with its title."""
    rows = db.execute(
        f"SELECT DISTINCT artifact_id FROM doc_feedback_batches WHERE state='undelivered' "
        f"AND id IN ({','.join('?' * len(keys))}) ORDER BY artifact_id", tuple(keys),
    ).fetchall() if keys else []
    documents = []
    for row in rows:
        artifact = registry_models.get_artifact(row["artifact_id"], db=db)
        documents.append({"artifact_id": row["artifact_id"], "title": artifact["title"] if artifact else None})
    return documents


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


def _person_tagged(alias: str) -> str:
    """SQL predicate for a comment whose author tagged the person; alias is an internal SQL identifier."""
    return f"{alias}.tags_person=1"


def _eligible_top_level(alias: str) -> str:
    """SQL predicate for an eligible unread top-level comment; alias is an internal SQL identifier.

    A tag of the person makes the comment eligible on its own: the noise rules that keep an agent's
    suggestions and review notes out of the unread set decide what needs the person *without* an ask.
    """
    return f"""{alias}.parent_id IS NULL AND {alias}.author_kind='agent'
               AND ({_person_tagged(alias)}
                    OR ({alias}.replacement IS NULL AND {alias}.review=0
                        AND EXISTS (SELECT 1 FROM doc_comments person
                                    WHERE person.artifact_id={alias}.artifact_id AND person.author_kind='user'
                                      AND person.created_at<{alias}.created_at)))"""


def may_change_unread_attention(
    author_kind: str, parent_id: str | None, replacement: str | None, review: bool, *, tags_person: bool = False,
) -> bool:
    """Coarse reload hint: deliberately omits the earlier person-comment condition.

    A tag of the person is the one exception to the noise rules it mirrors: it makes even a
    suggestion or a review note need them, so the surface reloads for it (``_eligible_top_level``).
    """
    return author_kind == 'agent' and (tags_person or bool(parent_id or (replacement is None and not review)))


def unread_comment_rows(user_id: str, artifact_id: str | None, *, include_author_thread: bool = False) -> tuple[str, tuple]:
    """The one definition of unread rows for replies and eligible top-level comments.

    Both use the same per-thread watermark, ordered by creation time and id. A tag of the
    person makes either kind eligible even where the ordinary rules would not.
    """
    # A document read binds both sides to its artifact so idx_doc_comments_artifact bounds the scan to that document.
    scoped = artifact_id is not None
    sql = f"""FROM doc_comments item
           {"JOIN threads thread ON thread.id=item.author_thread_id" if include_author_thread else ""}
           JOIN doc_comments root ON root.id=COALESCE(item.parent_id, item.id)
           LEFT JOIN (
               SELECT DISTINCT parent_id FROM doc_comments
               WHERE author_kind='user' AND parent_id IS NOT NULL
                 {"AND artifact_id=?" if scoped else ""}
           ) touched ON touched.parent_id=root.id
           LEFT JOIN doc_comment_thread_views thread_view
             ON thread_view.user_id=? AND thread_view.root_id=root.id
           WHERE item.author_kind<>'user'
             {"AND item.artifact_id=? AND root.artifact_id=?" if scoped else ""}
             AND ((item.parent_id IS NOT NULL
                   AND (root.author_kind='user' OR touched.parent_id IS NOT NULL OR {_person_tagged('item')}))
                  OR ({_eligible_top_level('item')}))
             AND (
                 item.created_at>COALESCE(thread_view.seen_at, '')
                 OR (
                     thread_view.seen_at IS NOT NULL
                     AND item.created_at=thread_view.seen_at
                     AND item.id>thread_view.seen_reply_id
                 )
             )"""
    return sql, ((artifact_id, user_id, artifact_id, artifact_id) if scoped else (user_id,))


def _unread_items(db, user_id: str, artifact_id: str) -> list[tuple[str, str]]:
    """One document's unread items as (root id, item id), ordered by time and id."""
    sql, params = unread_comment_rows(user_id, artifact_id)
    rows = db.execute(
        f"SELECT root.id AS root_id, item.id AS item_id {sql} ORDER BY item.created_at, item.id",
        params,
    ).fetchall()
    return [(str(row["root_id"]), str(row["item_id"])) for row in rows]


def unread_item_ids(db, user_id: str) -> set[str]:
    """Return document IDs with unread replies or eligible top-level comments."""
    sql, params = unread_comment_rows(user_id, None)
    rows = db.execute(f"SELECT DISTINCT root.artifact_id AS artifact_id {sql}", params).fetchall()
    return {str(row["artifact_id"]) for row in rows}


def annotate_thread_attention(db, user_id: str, artifact_id: str, threads: list[dict]) -> None:
    """Attach unread reply IDs and the separate top-level comment flag to each thread."""
    new_by_root: dict[str, list[str]] = {}
    for root_id, item_id in _unread_items(db, user_id, artifact_id):
        new_by_root.setdefault(root_id, []).append(item_id)
    for thread in threads:
        unread = new_by_root.get(thread["id"], [])
        thread["new_comment"] = thread["id"] in unread
        thread["new_reply_ids"] = [item for item in unread if item != thread["id"]]


def mark_thread_viewed(
    db, user_id: str, artifact_id: str, root_id: str, seen_item_id: str,
) -> list[str]:
    """Persist the newest rendered item for one root, never moving its stamp back.

    Returns the root's new reply ids that remain after the stamp.
    """
    root = db.execute(
        "SELECT id FROM doc_comments WHERE id=? AND artifact_id=? AND parent_id IS NULL",
        (root_id, artifact_id),
    ).fetchone()
    if root is None:
        raise ValueError("root_not_found")
    item = db.execute(
        f"""SELECT item.id,item.created_at FROM doc_comments item
           WHERE item.id=? AND (item.parent_id=? OR (item.id=? AND {_eligible_top_level('item')}))""",
        (seen_item_id, root_id, root_id),
    ).fetchone()
    if item is None:
        raise ValueError("reply_not_found")
    db.execute(
        """INSERT INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id)
           VALUES (?,?,?,?)
           ON CONFLICT(user_id,root_id) DO UPDATE SET
             seen_at=excluded.seen_at, seen_reply_id=excluded.seen_reply_id
           WHERE excluded.seen_at>doc_comment_thread_views.seen_at
              OR (excluded.seen_at=doc_comment_thread_views.seen_at
                  AND excluded.seen_reply_id>doc_comment_thread_views.seen_reply_id)""",
        (user_id, root_id, item["created_at"], item["id"]),
    )
    return [item_id for waiting_root, item_id in _unread_items(db, user_id, artifact_id)
            if waiting_root == root_id and item_id != root_id]


def annotate_pane(pane: dict, user_id: str, *, db) -> dict:
    """Add the passive reply marker to every artifact row in a pane payload."""
    waiting = unread_item_ids(db, user_id)

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
