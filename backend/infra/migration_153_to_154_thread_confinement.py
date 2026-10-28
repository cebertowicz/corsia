"""Schema 153 -> 154: a thread's launch confinement, read by every harness."""

from __future__ import annotations

import sqlite3

# A Claude Code review child made before this kept its confinement on its session
# records only; its newest review record is copied onto its row, so no other harness
# can launch it unconfined. Empty read dirs copy as ``[]``, which reads as damaged.
_REVIEW_RECORD = """
    FROM artifact_identities i JOIN artifact_registrations r ON r.artifact_id=i.id
    WHERE r.parent_thread_id=threads.id AND i.integration='claude-code'
      AND i.type='session' AND json_valid(i.metadata)
      AND json_extract(i.metadata, '$.claude_invocation.confinement')='review'
"""

# A record the old launch reader refused: not JSON, a confinement that is not a known
# mode, or an unconfined launch that names add-dirs (``stored_confinement``).
_DAMAGED_RECORD = """
    NOT json_valid(i.metadata)
    OR (json_extract(i.metadata, '$.claude_invocation.confinement') IS NOT NULL
        AND (json_type(i.metadata, '$.claude_invocation.confinement') <> 'text'
             OR json_extract(i.metadata, '$.claude_invocation.confinement') NOT IN ('none', 'review')
             OR (json_extract(i.metadata, '$.claude_invocation.confinement')='none'
                 AND json_type(i.metadata, '$.claude_invocation.add_dirs') IS NOT NULL
                 AND (json_type(i.metadata, '$.claude_invocation.add_dirs') <> 'array'
                      OR json_array_length(i.metadata, '$.claude_invocation.add_dirs') > 0))))
"""


def migrate_153_to_154(conn: sqlite3.Connection) -> None:
    """Add each thread's write-once confinement and read dirs, and carry over the
    confinement of every Claude Code review child made before them."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if not columns:
        return
    if "confinement" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN confinement TEXT")
    if "confined_read_dirs" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN confined_read_dirs TEXT")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"artifact_identities", "artifact_registrations"} <= tables:
        return
    # A damaged session could have been the review record. Preserve the old
    # fail-closed behavior even when another valid record exists for the thread.
    conn.execute(
        "UPDATE threads SET confinement='review', confined_read_dirs='[]' "
        "WHERE confinement IS NULL AND spawned_by IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM artifact_identities i JOIN artifact_registrations r ON r.artifact_id=i.id "
        "WHERE r.parent_thread_id=threads.id AND i.integration='claude-code' "
        f"AND i.type='session' AND ({_DAMAGED_RECORD}))"
    )
    conn.execute(
        "UPDATE threads SET confinement='review', confined_read_dirs=("
        "SELECT COALESCE(json_extract(i.metadata, '$.claude_invocation.add_dirs'), '[]') "
        f"{_REVIEW_RECORD} ORDER BY i.updated_at DESC, i.id ASC LIMIT 1) "
        f"WHERE confinement IS NULL AND EXISTS (SELECT 1 {_REVIEW_RECORD})"
    )
