"""User-visible unread reply projection, using the canonical comment and view stores."""

from __future__ import annotations

from pathlib import Path

from doceditor.feedback_state import _new_reply_rows
from infra.auth import may_access_artifact
from registry import models as registry_models


def _removed(path: str) -> bool:
    try:
        Path(path).stat()
    except FileNotFoundError:
        return True
    except OSError:
        # A transient file error keeps the hand-off available for its retry surface.
        return False
    return False


def unread_reply_items(db, user_id: str) -> list[dict]:
    """Group unread agent replies by the replying thread and document, after authorization."""
    source, params = _new_reply_rows(user_id, None)
    source = source.replace(
        "FROM doc_comments reply", "FROM doc_comments reply JOIN threads thread ON thread.id=reply.author_thread_id"
    )
    rows = db.execute(
        f"""SELECT root.artifact_id, reply.author_thread_id, thread.title AS thread_label,
                   COUNT(*) AS unread_count, MAX(reply.created_at) AS newest_at
              {source}
               AND reply.author_kind='agent' AND thread.user_id=?
              GROUP BY root.artifact_id, reply.author_thread_id
              ORDER BY newest_at DESC, root.artifact_id, reply.author_thread_id""",
        (*params, user_id),
    ).fetchall()
    result = []
    for row in rows:
        artifact = registry_models.get_artifact(str(row["artifact_id"]), db=db)
        if not artifact or artifact.get("state") != "active" or artifact.get("type") != "document":
            continue
        owner, path = artifact.get("parent_thread_id"), artifact.get("path")
        if not owner or not may_access_artifact(owner, user_id, db=db) or not path or _removed(path):
            continue
        title = artifact.get("title") or Path(path).name
        key = f"{row['author_thread_id']}:{row['artifact_id']}"
        result.append({
            "external_id": key, "renderer": "comment-reply",
            "detail": {
                "id": key, "title": title, "excerpt": "", "position": 1, "total_count": 1,
                "unread_count": row["unread_count"], "newest_at": row["newest_at"],
                "destination": {"pane": "document-replies", "id": row["artifact_id"], "path": path, "title": title},
                "source": {"thread_id": row["author_thread_id"], "thread_label": row["thread_label"] or "Agent",
                           "project_id": None, "project_label": None, "parent_reviewed": False},
            },
        })
    return result
