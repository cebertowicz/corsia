"""Where a promoted file went, for a caller that still names the path it left."""

from __future__ import annotations

from dataclasses import replace
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
    if where is None:
        return None
    then = f"{verb} it there" + (", or use a new name for a different file" if verb == "write" else "")
    return f"This file was promoted to {where}; {then}."


def write_authority_unless_promoted(
    target: Path, *, workspace: Path, thread_id: str, direct_user: bool, db,
) -> WriteAuthority | None:
    """``document_write_authority`` for a write the shared publisher does not govern, after refusing
    an agent's write that would recreate a file a promote moved, and pointing at where the document went.

    The refusal guards an agent replaying a call on a path it no longer owns. A person's own save
    (``direct_user``) is never refused: it creates the file they named, as it did before.
    """
    message = None if direct_user else moved_message(target, "write", db=db)
    if message is not None:
        raise PromotedPathRefused(message)
    return document_write_authority(
        target, workspace=workspace, thread_id=thread_id, direct_user=direct_user, db=db,
    )


def mark_carried(prepared, source_identity, *, db):
    """``(prepared, left)`` for a promote registering ``source_identity``'s file at its new path.

    A recorded hash that matches makes registration move the source's own identity there; ``left`` is
    that identity's row as it stood before, and the recovery is marked as the promote's so its change
    note says so. The source is the row ``find_file_identity`` resolves, legacy path ids included, the
    same lookup ``settle_promoted_source`` uses. Any other registration returns ``(prepared, None)``.
    """
    moved = prepared.moved_from
    source = service.find_file_identity(source_identity, db=db) if moved and source_identity is not None else None
    if source is None or moved.identity["id"] != source["id"]:
        return prepared, None
    return replace(prepared, moved_from=replace(moved, by_promote=True)), moved.identity


def settle_promoted_source(source_identity, left, artifact: dict, *, db) -> None:
    """Close the source's old path after a promote registered ``artifact``, in the registration's transaction.

    A new identity (the recorded hash did not match) tombstones the old row and takes its comments. A
    carried one (``artifact`` is ``left``'s own id) left no row at the old path, so one is inserted: a
    promoted tombstone naming it, with no registration, so the pane and ``artifact_list`` never show it,
    and no comments, so nothing moves. A source never registered leaves nothing.
    """
    old = service.find_file_identity(source_identity, db=db) if source_identity is not None else None
    if old is not None:
        models.retire(old["id"], tombstone=True, cause="promoted", successor=artifact["id"], db=db)
    elif left is not None and artifact["id"] == left["id"]:
        models.tombstone_promoted_path(left, artifact["id"], db=db)
