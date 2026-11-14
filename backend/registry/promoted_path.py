"""Where a promoted file went, for a caller that still names the path it left."""

from __future__ import annotations

from pathlib import Path

from infra import config
from infra.database import get_db
from infra.workspace_paths import resolve_registered_path
from registry import models, service
from registry.document_ownership import document_write_authority
from registry.shared_principals import WriteAuthority
from registry.shared_write_errors import PromotedPathRefused


def promoted_to(path: str | Path, *, db=None) -> str | None:
    """The workspace-relative path the document that left ``path`` now lives at, else None.

    Answers only for a path with no file on disk whose registry row is a promote's tombstone
    with a live successor. A path deleted for any other reason, a path never registered and a
    path that holds a file all answer None, so ordinary writes are untouched.
    """
    target = Path(path).expanduser().resolve(strict=False)
    if target.exists():
        return None
    _db = db or get_db()
    try:
        identity = service.prepare_file_identity(str(target), db=_db)
        if identity is None or identity.zone != "workspace":
            return None
        row = service.find_file_identity(identity, db=_db)
        if row is None or models.retired_cause(row["id"], db=_db) != "promoted":
            return None
        successor = models.successor_id(row["id"], db=_db)
        if not successor or models.identity_state(successor, db=_db) != "active":
            return None
        live = _db.execute(
            "SELECT external_id, path FROM artifact_identities WHERE id=?", (successor,)
        ).fetchone()
        if live is None:
            return None
        resolved = resolve_registered_path(live["external_id"], live["path"])
        if not resolved:
            return None
        root = config.WORKSPACE.resolve()
        return str(Path(resolved).relative_to(root)) if Path(resolved).is_relative_to(root) else resolved
    finally:
        if not db:
            _db.close()


def moved_message(path: str | Path, verb: str, *, db=None) -> str | None:
    """The one sentence plus next step for a caller that named a promoted-away path, else None."""
    where = promoted_to(path, db=db)
    return None if where is None else f"This file was promoted to {where}; {verb} it there."


def write_authority_unless_promoted(
    target: Path, *, workspace: Path, thread_id: str, direct_user: bool, db,
) -> WriteAuthority | None:
    """``document_write_authority`` for a write the shared publisher does not govern, after refusing
    one that would recreate a file a promote moved, and pointing at where the document went."""
    message = moved_message(target, "write", db=db)
    if message is not None:
        raise PromotedPathRefused(message)
    return document_write_authority(
        target, workspace=workspace, thread_id=thread_id, direct_user=direct_user, db=db,
    )
