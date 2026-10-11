"""Whether an image inside a document may be read: the one scope check, at placing and at every read after.

A region comment stores the path of the image it is on. That path is a name,
not a grant: the file behind it can change after the comment is placed (a
retargeted link), so everything that moves a pin on it or shows it to anyone
checks it again here, as the file route checks every read of the bytes.
"""

from __future__ import annotations

from pathlib import Path

from doceditor.comment_action_errors import CommentActionError
from doceditor.document_scope import ProjectNotFound, scope_bases
from registry.path_access import path_allowed


def owner_bases(db, owner_thread_id: str | None) -> list[Path]:
    """The workspace's roots plus those of the owner's project, where a document's images may live."""
    bases = scope_bases(None, db)
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (owner_thread_id,)).fetchone() if owner_thread_id else None
    if row and row["project_id"]:
        try:
            bases += scope_bases(row["project_id"], db)
        except ProjectNotFound:
            pass
    return bases


def checked_image(db, path: str, owner_thread_id: str | None) -> Path:
    """The file at ``path`` as it resolves now: existing, inside a root its owner can read, and a file."""
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise CommentActionError("region_target", f"the image {path} could not be found.") from exc
    if not path_allowed(resolved, owner_bases(db, owner_thread_id), db=db):
        raise CommentActionError("region_target", f"the image {path} is outside this document's scope.")
    if not resolved.is_file():
        raise CommentActionError("region_target", f"{path} is not a file.")
    return resolved
