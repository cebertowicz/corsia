"""Read a registration's filesystem facts before its SQLite write transaction.

Callers that already hold a write transaction (durable publication) prepare
first and pass the result, so no disk access happens under the writer lock.
"""

from __future__ import annotations

from pathlib import Path

from infra.database import get_db
from registry.file_resolution import find_file_identity, prepare_file_identity
from registry.file_identity import FileIdentity, PreparedFileWrite
from registry.identity_metadata import sha256_file


def prepare_file_write(
    abs_path: str | FileIdentity | PreparedFileWrite, thread_id: str, *, db=None,
) -> PreparedFileWrite | None:
    """Resolve identity, hash the local file, and plan any moved-file recovery."""
    if isinstance(abs_path, PreparedFileWrite):
        return abs_path
    identity = prepare_file_identity(abs_path, db=db)
    if identity is None:
        return None
    path = Path(identity.canonical)
    if not path.is_file() or path.is_symlink():
        return PreparedFileWrite(identity, None, None)
    _db = db or get_db()
    try:
        moved_from = None
        existing = find_file_identity(identity, db=_db)
        if existing is None:
            from registry.moved_files import plan_shared_moved_file_recovery

            moved_from = plan_shared_moved_file_recovery(path, thread_id, _db)
    finally:
        if not db:
            _db.close()
    return PreparedFileWrite(identity, sha256_file(path), moved_from)


def apply_prepared_recovery(prepared: PreparedFileWrite, thread_id: str, db) -> dict | None:
    """Adopt the prepared moved identity inside the caller's transaction."""
    if prepared.moved_from is None:
        return None
    from registry.moved_files import apply_moved_file_recovery

    return apply_moved_file_recovery(prepared.moved_from, actor_thread_id=thread_id, db=db)
