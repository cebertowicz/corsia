"""Doc-comment storage keyed by artifact id."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from doceditor import comment_notifications, feedback_reopens, feedback_settlement, live_events
from infra.database import get_db, write_transaction

_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def validate_client_id(v: str | None) -> str | None:
    """Reject oversized or unsafe client_ids."""
    if v is None:
        return v
    if len(v) > 64:
        raise ValueError("client_id must be at most 64 characters")
    if not _CLIENT_ID_RE.fullmatch(v):
        raise ValueError("client_id must contain only A-Z, a-z, 0-9, '-', '_'")
    return v


class CrossArtifactIdError(ValueError):
    """Raised when a client_id collides with a comment on a different artifact
    or created by a different author. Maps to HTTP 409 at the API layer so the
    client can re-mint a fresh id and retry."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_to_threads(rows: list[dict]) -> list[dict]:
    """Group flat rows into top-level threads with nested replies."""
    tops: dict[str, dict] = {}
    replies: list[dict] = []
    for r in rows:
        if r["parent_id"]:
            replies.append(r)
        else:
            r["replies"] = []
            tops[r["id"]] = r
    for r in replies:
        parent = tops.get(r["parent_id"])
        if parent is not None:
            parent["replies"].append(r)
    return list(tops.values())


def get_comment(comment_id: str, *, db=None) -> dict | None:
    """Return one comment row by id."""
    _db = db or get_db()
    try:
        row = _db.execute(
            "SELECT * FROM doc_comments WHERE id=?", (comment_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if not db:
            _db.close()


def get_artifact_id_for_comment(comment_id: str, *, db=None) -> str | None:
    """Return the artifact id for a comment."""
    _db = db or get_db()
    try:
        row = _db.execute(
            "SELECT artifact_id FROM doc_comments WHERE id=?", (comment_id,)
        ).fetchone()
        return row["artifact_id"] if row else None
    finally:
        if not db:
            _db.close()


def list_for_artifact(artifact_id: str, *, db=None) -> list[dict]:
    """All comment threads on an artifact, oldest-first within each level."""
    _db = db or get_db()
    try:
        rows = [
            dict(r) for r in _db.execute(
                "SELECT * FROM doc_comments WHERE artifact_id=? ORDER BY created_at",
                (artifact_id,),
            ).fetchall()
        ]
        return _rows_to_threads(rows)
    finally:
        if not db:
            _db.close()


def create_with_status(
    artifact_id: str,
    *,
    body: str,
    author: str = "you",
    author_kind: str = "user",
    author_thread_id: str | None = None,
    parent_id: str | None = None,
    anchor_text: str | None = None,
    from_off: int | None = None,
    to_off: int | None = None,
    anchor_prefix: str | None = None,
    anchor_suffix: str | None = None,
    review: bool = False,
    replacement: str | None = None,
    client_id: str | None = None,
    region: tuple[float, float, float, float] | None = None,
    image_rev: str | None = None,
    image_src: str | None = None,
    db=None,
) -> tuple[dict[str, Any], bool]:
    """Insert a comment and return its row plus whether this call created it.

    A ``region`` (x, y, w, h in fractions of the image) takes the next number
    on its image, counting resolved comments too, so a number is never reused.
    """
    cid = client_id or str(uuid.uuid4())[:12]
    now = _now()
    x, y, w, h = region if region is not None else (None, None, None, None)
    with write_transaction(db, scope="create_doc_comment") as connection:
        number = None
        if region is not None and connection.execute(
            "SELECT 1 FROM doc_comments WHERE id=?", (cid,)
        ).fetchone() is None:
            number = connection.execute(
                """SELECT COALESCE(MAX(region_number), 0) + 1 FROM doc_comments
                   WHERE artifact_id=? AND COALESCE(image_src, '')=COALESCE(?, '')""",
                (artifact_id, image_src),
            ).fetchone()[0]
        inserted = connection.execute(
            """INSERT INTO doc_comments
               (id, artifact_id, parent_id, author, author_kind, author_thread_id,
                body, anchor_text, from_off, to_off, anchor_prefix, anchor_suffix,
                review, resolved, acked, replacement, created_at, updated_at,
                region_x, region_y, region_w, region_h, region_number, image_rev, image_src)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (
                cid, artifact_id, parent_id, author, author_kind, author_thread_id,
                body, anchor_text, from_off, to_off, anchor_prefix, anchor_suffix,
                1 if review else 0, replacement, now, now,
                x, y, w, h, number, image_rev, image_src,
            ),
        )
        row = connection.execute(
            "SELECT * FROM doc_comments WHERE id=?", (cid,)
        ).fetchone()
        r = dict(row)
        if r["artifact_id"] != artifact_id:
            raise CrossArtifactIdError(
                f"client_id '{cid}' belongs to artifact '{r['artifact_id']}', "
                f"not '{artifact_id}'"
            )
        if r["author"] != author:
            raise CrossArtifactIdError(
                f"client_id '{cid}' was created by author '{r['author']}', "
                f"not '{author}'"
            )
        created = inserted.rowcount == 1
        if created:
            # Only a new top-level agent comment names itself; replies, suggestions and user comments do not.
            named = author_kind == "agent" and author_thread_id and not parent_id and replacement is None
            comment_notifications.announce_after_commit(
                connection, artifact_id,
                comment_id=cid if named else None, author_thread_id=author_thread_id if named else None,
            )
            if region is not None:
                live_events.region_after_commit(connection, "placed", artifact_id, w, h, author_kind=author_kind)
        return r, created


def create(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Insert a comment while preserving the established row-only API."""
    return create_with_status(*args, **kwargs)[0]


def update(comment_id: str, fields: dict[str, Any], *, db=None) -> dict | None:
    """Patch a comment (resolved / acked / body / offsets). Returns the row.

    Resolving drops any unsent re-opened item for the comment, whichever route resolved it.
    """
    allowed = {"resolved", "acked", "body", "from_off", "to_off", "anchor_text", "decision", "decided_by", "decided_by_thread_id", "decided_at", "decision_event_id",
               "region_x", "region_y", "region_w", "region_h", "image_rev"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        params.append(int(v) if k in ("resolved", "acked") and isinstance(v, bool) else v)
    if not sets:
        return None
    now = _now()
    sets.append("updated_at=?")
    params.extend([now, comment_id])
    with write_transaction(db, scope="update_doc_comment") as connection:
        cur = connection.execute(f"UPDATE doc_comments SET {', '.join(sets)} WHERE id=?", params)
        if cur.rowcount == 0:
            return None
        if fields.get("resolved"):
            feedback_reopens.discard_open_item(connection, comment_id, now)
            feedback_settlement.settle_batches_of(connection, comment_id)
        row = dict(connection.execute("SELECT * FROM doc_comments WHERE id=?", (comment_id,)).fetchone())
        if "region_x" in fields:
            live_events.region_after_commit(connection, "moved", row["artifact_id"], row["region_w"], row["region_h"])
        comment_notifications.announce_after_commit(connection, row["artifact_id"])
        return row


def set_stranded_notice(comment_id: str, at: str | None, *, db=None) -> None:
    """Record when a suggestion's author was told its text was changed, or clear that once it can be applied again.

    Not an edit of the comment: nothing is announced and its update time stays.
    """
    with write_transaction(db, scope="set_doc_comment_stranded_notice") as connection:
        connection.execute("UPDATE doc_comments SET stranded_notified_at=? WHERE id=?", (at, comment_id))


def update_positions(artifact_id: str, updates: list[dict], *, db=None) -> int:
    """Batch-persist remapped anchor offsets and optionally refreshed context."""
    now = _now()
    with write_transaction(db, scope="update_doc_comment_positions") as connection:
        n = 0
        for u in updates:
            sets = ["from_off=?", "to_off=?"]
            params: list[Any] = [u.get("from_off"), u.get("to_off")]
            if "anchor_prefix" in u:
                sets.extend(["anchor_prefix=?", "anchor_suffix=?"])
                params.extend([u["anchor_prefix"], u["anchor_suffix"]])
            sets.append("updated_at=?")
            params.extend([now, u.get("id"), artifact_id])
            cur = connection.execute(
                f"UPDATE doc_comments SET {', '.join(sets)} WHERE id=? AND artifact_id=?",
                params,
            )
            n += cur.rowcount
        return n
