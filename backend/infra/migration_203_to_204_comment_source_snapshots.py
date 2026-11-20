"""Schema 203 -> 204: keep the document text each comment's anchor was measured against.

A comment's anchor is a pair of offsets into the document as it was when the comment was
made. Until now only the offsets and the quoted text were stored, so after a whole-file
rewrite the offsets had to be re-found in the new text: a rewrite that deleted the
commented line could land them on a neighbouring line, moving the comment onto text
nobody commented on. From here the exact source text is stored with the anchor, and
unchanged spans are re-measured against it instead of being searched for in the new text
(``doceditor.comment_snapshots``, ``doceditor.snapshot_reanchor``).

Purely additive. The table is empty and every existing row's ``snapshot_id`` stays NULL,
which means "written before snapshots were kept": those comments are placed exactly as
before, and a row gains its first snapshot the next time its document is saved with the
anchor still valid (``doceditor.comment_state_updates``) or the next time a comment is
created on that document. Nothing is backfilled: the old text of a past version is not
recoverable from the current file, so a backfilled snapshot would be a guess.

Snapshots are shared by identical bytes and are collected when no anchor references one
any more (``doceditor.comment_snapshots.collect_candidates``), so a document that is
edited and saved gains rows only while comments still point at the versions they were
made on.

Creating the step twice changes nothing. A database that has no ``doc_comments`` table
never held a document comment, so the step does nothing there, as the other comment steps
do. Runs inside the migration runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3

from infra.doc_comments_schema import (
    ANCHORS_BY_SNAPSHOT_INDEX_SQL,
    DOC_COMMENT_SNAPSHOTS_SCHEMA_SQL,
)

#: The one new column, added here rather than by rebuilding ``doc_comments`` so an
#: upgrade never rewrites the table. Same definition as the fresh-install DDL.
SNAPSHOT_ID_COLUMN_DDL = (
    "ALTER TABLE doc_comments ADD COLUMN snapshot_id INTEGER REFERENCES doc_comment_snapshots(id)"
)


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)).fetchone() is not None


def _has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))


def migrate_203_to_204(db: sqlite3.Connection) -> None:
    """Add the snapshot store and the reference from each anchored comment to it."""
    if not _has_table(db, 'doc_comments'):
        return  # a database that never held a document comment has no anchors to keep
    for statement in DOC_COMMENT_SNAPSHOTS_SCHEMA_SQL.split(';'):
        if statement.strip():
            db.execute(statement)
    if not _has_column(db, 'doc_comments', 'snapshot_id'):
        db.execute(SNAPSHOT_ID_COLUMN_DDL)
    db.execute(ANCHORS_BY_SNAPSHOT_INDEX_SQL)
