"""The one writer of the derived checkpoint entry search index.

Validity is decided only by ``read_continuity_snapshot``: a snapshot the shared
reader rejects indexes nothing, and an entry it accepts is indexed. Nothing in
SQL restates that rule. ``checkpoints.install`` is the single live write seam;
``backfill_checkpoint_entry_search`` is the same writer run over history.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from loguru import logger as log

from core.continuity_render import (
    ContinuitySnapshotUnreadable,
    read_continuity_snapshot,
)

BACKFILL_BATCH = 200
#: One hundred rows use 800 bind parameters, well under SQLite's supported limit.
ENTRY_INDEX_BATCH_ROWS = 100

# ``revision`` is NOT NULL in the index, while the shared reader accepts a
# frozen entry that carries none. Such an entry is stored under this value.
NO_REVISION = 0


@dataclass
class IndexOutcome:
    """Counts from one indexing pass, for the operator log."""

    indexed: int = 0
    rejected: int = 0
    skipped: int = 0

    def add(self, other: "IndexOutcome") -> None:
        self.indexed += other.indexed
        self.rejected += other.rejected
        self.skipped += other.skipped


def index_checkpoint_entries(
    conn: sqlite3.Connection, checkpoint_id: str
) -> IndexOutcome:
    """Index one installed checkpoint's frozen entries, once per revision.

    A repeated entry revision keeps its row and only widens its first/last
    checkpoint pointers, so re-running the pass is idempotent.
    """
    row = conn.execute(
        "SELECT thread_id, history_epoch, generation_order, "
        "       continuity_snapshot_json "
        "FROM checkpoints WHERE id = ? AND status = 'installed'",
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        return IndexOutcome(skipped=1)
    try:
        entries = read_continuity_snapshot(row[3])
    except ContinuitySnapshotUnreadable as error:
        log.warning(
            "checkpoint_entry_index rejected_snapshot checkpoint_id={} error={}",
            checkpoint_id,
            error,
        )
        return IndexOutcome(rejected=1)
    if not entries:
        return IndexOutcome(skipped=1)
    order = row[2]
    index_rows = [
        (
            row[0],
            row[1],
            entry.entry_id,
            NO_REVISION if entry.revision is None else entry.revision,
            entry.field,
            entry.search_text,
            order,
            order,
        )
        for entry in entries
    ]
    _write_index_rows(conn, index_rows)
    return IndexOutcome(indexed=len(entries))


def _write_index_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Upsert frozen entries in bounded statements without changing row semantics."""
    prefix = (
        "INSERT INTO checkpoint_entry_search("
        "thread_id,history_epoch,entry_id,revision,field,search_text,"
        "first_generation_order,last_generation_order) VALUES "
    )
    suffix = (
        " ON CONFLICT(thread_id,history_epoch,entry_id,revision) DO UPDATE SET "
        "first_generation_order=MIN(first_generation_order,excluded.first_generation_order),"
        "last_generation_order=MAX(last_generation_order,excluded.last_generation_order)"
    )
    for start in range(0, len(rows), ENTRY_INDEX_BATCH_ROWS):
        batch = rows[start:start + ENTRY_INDEX_BATCH_ROWS]
        placeholders = ",".join("(?,?,?,?,?,?,?,?)" for _ in batch)
        parameters = [value for index_row in batch for value in index_row]
        conn.execute(prefix + placeholders + suffix, parameters)


def backfill_checkpoint_entry_search(conn: sqlite3.Connection) -> IndexOutcome:
    """Index every installed checkpoint through the same writer, in batches."""
    total = IndexOutcome()
    after = 0
    while True:
        ids = conn.execute(
            "SELECT id, generation_order FROM checkpoints "
            "WHERE status = 'installed' AND generation_order > ? "
            "ORDER BY generation_order LIMIT ?",
            (after, BACKFILL_BATCH),
        ).fetchall()
        if not ids:
            break
        for checkpoint_id, _ in ids:
            total.add(index_checkpoint_entries(conn, checkpoint_id))
        after = ids[-1][1]
    log.info(
        "checkpoint_entry_index backfill indexed={} rejected_snapshots={} skipped={}",
        total.indexed,
        total.rejected,
        total.skipped,
    )
    return total


def delete_thread_entry_index(conn: sqlite3.Connection, thread_id: str) -> None:
    """Drop a thread's index rows when its checkpoints are destroyed.

    Foreign-key cascades are not relied on: purge keeps the thread row and
    connections do not enable them.
    """
    conn.execute(
        "DELETE FROM checkpoint_entry_search WHERE thread_id = ?", (thread_id,)
    )


def delete_thread_checkpoints(conn: sqlite3.Connection, thread_id: str) -> None:
    """Drop a thread's checkpoints, staged edits, and derived index."""
    conn.execute(
        "DELETE FROM checkpoint_staged_edits WHERE checkpoint_id IN "
        "(SELECT id FROM checkpoints WHERE thread_id = ?)",
        (thread_id,),
    )
    conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
    delete_thread_entry_index(conn, thread_id)
