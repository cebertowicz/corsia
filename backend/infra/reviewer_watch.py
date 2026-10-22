"""Which documents a reviewer is still watching, and since when (DCOL-FR10.2, FR10.6).

A review request keeps its reviewer hearing about a document until the review
ends or seven days pass without the reviewer commenting on or editing it.
Request lifecycle state selects pending reviews; handed-back work follows
its public Plans task after the cheap SQL window.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


WATCH_DAYS = 7


def watched_reviews(db: sqlite3.Connection, thread_id: str, now: datetime | None = None) -> dict[str, str]:
    """Map artifact id to the time from which its changes are owed to this reviewer."""
    from infra.queries import thread_is_available
    from doceditor.review_lifecycle import is_open

    reviewer = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not thread_is_available(reviewer):  # A retired reviewer can take no input, so owe it none.
        return {}
    horizon = ((now or datetime.now(timezone.utc)) - timedelta(days=WATCH_DAYS)).isoformat()
    rows = db.execute(
        "SELECT request.* FROM doc_review_requests AS request "
        "WHERE request.reviewer_thread_id=:thread_id "
        "AND request.state IN ('forked','handed_back','failed') "
        # Only unsettled requests are read; each stops at its first activity inside the window.
        "AND (julianday(request.created_at)>=julianday(:horizon) OR EXISTS ("
        " SELECT 1 FROM doc_comments AS comment "
        " WHERE comment.artifact_id=request.artifact_id AND comment.author_thread_id=request.reviewer_thread_id"
        " AND julianday(comment.created_at)>=julianday(:horizon)"
        ") OR EXISTS ("
        " SELECT 1 FROM context_change_journal AS edit "
        " WHERE edit.object_kind='artifact' AND edit.object_id=request.artifact_id "
        " AND edit.change_kind='content_edited' AND edit.actor_thread_id=request.reviewer_thread_id"
        " AND julianday(edit.created_at)>=julianday(:horizon)"
        ")) ORDER BY request.created_at",
        {"thread_id": thread_id, "horizon": horizon},
    ).fetchall()
    since: dict[str, str] = {}
    for row in rows:
        if is_open(db, row):
            since.setdefault(str(row["artifact_id"]), str(row["created_at"]))
    return since


def watch_json(watched: dict[str, str]) -> str:
    """The watch map as JSON rows ``{id, since}`` for one bound ``json_each`` parameter."""
    return json.dumps([{"id": key, "since": value} for key, value in watched.items()])
