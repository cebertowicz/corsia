"""Validate a review fork's explicit request and document scope."""

from __future__ import annotations

class ReviewNotForkable(Exception):
    """The review must run on the reviewer's own thread; ``reason`` is the hand-back cause."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def require_forkable(request_id: str) -> None:
    """Return when the request's document is readable text, else raise ``ReviewNotForkable``."""
    from doceditor.doc_text import read_doc_text
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT a.path FROM doc_review_requests r JOIN artifact_identities a ON a.id=r.artifact_id WHERE r.id=?",
            (request_id,),
        ).fetchone()
    finally:
        db.close()
    try:
        text = read_doc_text(row["path"] if row else None)
    except (OSError, ValueError) as error:
        raise ReviewNotForkable("document_unreadable") from error
    # The reader decodes with replacement, so a replacement character means it was not UTF-8.
    if "�" in text:
        raise ReviewNotForkable("document_unreadable")


def authorize_tool(request, name: str, arguments: dict) -> str:
    """Bind a fork to its request; restrict document tools to that request's document."""
    from pathlib import Path
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT r.*,a.path FROM doc_review_requests r "
            "JOIN artifact_identities a ON a.id=r.artifact_id WHERE r.fork_id=?",
            (request.fork_id,),
        ).fetchone()
        if (row is None or row["reviewer_thread_id"] != request.thread_id
                or row["state"] != "forked"):
            raise PermissionError("review fork no longer owns this request")
        if not name.startswith("doc_"):
            return str(row["id"])
        if arguments.get("artifact_id") and arguments["artifact_id"] != row["artifact_id"]:
            raise PermissionError("review fork may only use its requested document")
        if parent_id := arguments.get("parent_id"):
            parent = db.execute("SELECT artifact_id FROM doc_comments WHERE id=?", (parent_id,)).fetchone()
            if parent is None or parent["artifact_id"] != row["artifact_id"]:
                raise PermissionError("review fork may only use its requested document")
        if arguments.get("doc_path"):
            selected = Path(arguments["doc_path"])
            if not selected.is_absolute():
                selected = Path(request.working_dir) / selected
            if not row["path"] or selected.resolve() != Path(row["path"]).resolve():
                raise PermissionError("review fork may only use its requested document")
        elif not parent_id or arguments.get("artifact_id"):
            if arguments.get("artifact_id") != row["artifact_id"]:
                raise PermissionError("review fork may only use its requested document")
        return str(row["id"])
    finally:
        db.close()
