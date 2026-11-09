"""Read a registration's filesystem facts before its SQLite write transaction.

Callers that already hold a write transaction (durable publication) prepare
first and pass the result, so no disk access happens under the writer lock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.database import get_db
from registry import service
from registry.file_identity import FileIdentity, PreparedFileWrite
from registry.identity_bridge import written_row
from registry.identity_metadata import parse_metadata, sha256_file


def prepare_file_write(
    abs_path: str | FileIdentity | PreparedFileWrite, thread_id: str, *, db=None,
) -> PreparedFileWrite | None:
    """Resolve identity, hash the local file, and plan any moved-file recovery."""
    if isinstance(abs_path, PreparedFileWrite):
        return abs_path
    identity = service.prepare_file_identity(abs_path, db=db)
    if identity is None:
        return None
    path = Path(identity.canonical)
    if not path.is_file() or path.is_symlink():
        return PreparedFileWrite(identity, None, None)
    _db = db or get_db()
    try:
        moved_from = None
        existing = service.find_file_identity(identity, db=_db)
        if existing is None:
            from registry.moved_files import plan_shared_moved_file_recovery

            moved_from = plan_shared_moved_file_recovery(path, thread_id, _db)
    finally:
        if not db:
            _db.close()
    return PreparedFileWrite(identity, sha256_file(path), moved_from)


def registered_unchanged(thread_id: str, abs_path: str, artifact_id: str, *, db) -> dict[str, Any] | None:
    """This thread's row for a file whose registration would change nothing, read without a writer.

    A document open registers its read twice in a row: the HTTP snapshot, then the live open. When
    the first left ``artifact_id`` registered to this thread exactly as ``register_write_strict``
    would leave it again, the second would only restamp it. Returns the row
    ``register_write_strict`` would return, or None to register as usual.
    """
    from registry.registration_writer import _owner_registration, _ownership_is_released

    prepared = prepare_file_write(abs_path, thread_id, db=db)
    if prepared is None:
        return None
    identity = prepared.identity
    existing = service.find_file_identity(identity, db=db)
    if (existing is None or existing["id"] != artifact_id or existing["state"] != "active"
            or existing["external_id"] != identity.external_id or existing["path"] != identity.canonical
            or existing["title"] != Path(identity.canonical).name):
        return None
    current = parse_metadata(existing["metadata"])
    if current.get("zone") != identity.zone or prepared.metadata({}, current):
        return None
    row = db.execute(
        "SELECT relationship_state, retired_at, surfacing, origin_plan_node_id FROM artifact_registrations "
        "WHERE artifact_id=? AND parent_thread_id=?",
        (artifact_id, thread_id),
    ).fetchone()
    if row is None or row["retired_at"] is not None or row["surfacing"] == "settled" or row["origin_plan_node_id"]:
        return None
    # Registration keeps the current owner and makes every other reader a subscriber; an unowned,
    # never-released file would pass to this thread, which is a change.
    owner = _owner_registration(db, artifact_id)
    if owner is not None and owner["parent_thread_id"] == thread_id:
        return written_row(db, artifact_id, thread_id)
    if row["relationship_state"] != "subscriber-recent":
        return None
    if owner is None and not _ownership_is_released(db, artifact_id):
        return None
    return written_row(db, artifact_id, thread_id)


def apply_prepared_recovery(prepared: PreparedFileWrite, thread_id: str, db) -> dict | None:
    """Adopt the prepared moved identity inside the caller's transaction."""
    if prepared.moved_from is None:
        return None
    from registry.moved_files import apply_moved_file_recovery

    return apply_moved_file_recovery(prepared.moved_from, actor_thread_id=thread_id, db=db)
