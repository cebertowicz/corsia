"""Task-local ownership for feedback produced by an artifact-review fork."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from collections.abc import Iterator

_review_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "review_fork_request_id", default=None,
)


def current_request_id() -> str | None:
    """Return the request whose isolated fork is currently writing feedback."""
    return _review_request_id.get()


@contextmanager
def review_fork_scope(request_id: str) -> Iterator[None]:
    """Bind one request so comments and submit stay in its own batch."""
    token = _review_request_id.set(request_id)
    try:
        yield
    finally:
        _review_request_id.reset(token)


def authorize_tool(request, name: str, arguments: dict) -> str:
    """Bind a fork id to its durable request and exact document before any read or write."""
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
        if parent_id := arguments.get("parent_id"):
            parent = db.execute("SELECT artifact_id FROM doc_comments WHERE id=?", (parent_id,)).fetchone()
            if parent is None or parent["artifact_id"] != row["artifact_id"]:
                raise PermissionError("review fork may only use its requested document")
        if name == "read_file" or arguments.get("doc_path"):
            path = arguments.get("path") if name == "read_file" else arguments.get("doc_path")
            selected = Path(path or "")
            if not selected.is_absolute():
                selected = Path(request.working_dir) / selected
            if not row["path"] or selected.resolve() != Path(row["path"]).resolve():
                raise PermissionError("review fork may only read its requested document")
        elif not parent_id or arguments.get("artifact_id"):
            if arguments.get("artifact_id") != row["artifact_id"]:
                raise PermissionError("review fork may only use its requested document")
        return str(row["id"])
    finally:
        db.close()
