"""Validate a review fork's explicit request and document scope."""

from __future__ import annotations

# A document that shows an image is never reviewed by a fork: the fork's read scope is the document
# alone, so it cannot open the image. Any of these (case-insensitive) hands the review back. Each is
# a literal that starts something the editor draws as an image: ``![`` the markdown image, which
# ``cm/markdown-image.ts`` draws, and the HTML a table cell renders through ``renderedFragment`` (an
# ``<img>``, an SVG ``<image>``, ``<picture>``/``<source>``, ``<svg>``, a ``<video>`` poster, an
# ``<input type=image>``, and CSS or a ``background=`` attribute that loads a file). ``@{`` is a
# mermaid image node. The check is a superset, so a wrong match only sends the review back.
IMAGE_MARKERS = (
    "![", "<img", "<image", "<picture", "<source", "<svg", "<video", "<input",
    "url(", "image-set(", "background=", "@{",
)


class ReviewNotForkable(Exception):
    """The review must run on the reviewer's own thread; ``reason`` is the hand-back cause."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def require_forkable(request_id: str) -> None:
    """Return when the request's document is readable text with no image marker, else raise ``ReviewNotForkable``."""
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
    lowered = text.lower()
    if any(marker in lowered for marker in IMAGE_MARKERS):
        raise ReviewNotForkable("document_has_images")


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
        if arguments.get("artifact_id") and arguments["artifact_id"] != row["artifact_id"]:
            raise PermissionError("review fork may only use its requested document")
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
