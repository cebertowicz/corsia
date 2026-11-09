"""Indexes for the two passes a scoped history render makes over covered rows.

A render or checkpoint install that starts after a checkpoint boundary still
asks two questions about the history that checkpoint already carries: which of
those rows are retention stubs, and whether any Turn it reads has rows there
too. Without a key for those questions SQLite steps over every earlier row of
the thread on its way to the scope. Measured on a synthetic 3,053,475-row
database with an 84,506-row thread: 76,037 rows visited to return 7,262 stub
sequence numbers, and 1.78 M VM steps to prove an empty Turn set — per scoped
render, and the checkpoint install runs them under the writer lock.

The OR across the Turn column and the metadata key is why the second read needs
two indexes: SQLite's MULTI-INDEX OR does not engage at real Turn counts (it
does for an 8-Turn scope and does not for the 934-Turn scope a checkpoint near
the head has), so ``history/scoped_source.py`` asks the two halves separately.

One definition serves both the fresh schema and the v185 -> v186 migration, so
fresh and migrated databases carry identical indexes (the pattern of
``write_hold_indexes.py``). Production databases never run ANALYZE, so each
index is shaped to win the planner's default estimates.
"""

from __future__ import annotations

from infra.tool_retention_marker import TOOL_TRIM_CONTENT_SQL

SCOPED_HISTORY_INDEXES: tuple[tuple[str, str], ...] = (
    # The retention-stub pass: the reader's predicate, verbatim, so the seek is
    # bounded by the stubs a thread has instead of by its whole history.
    ("messages", f"""
    CREATE INDEX IF NOT EXISTS idx_messages_retention_stub
        ON messages(thread_id, seq)
        WHERE role = 'tool' AND {TOOL_TRIM_CONTENT_SQL}"""),
    # The metadata half of the Turn-provenance pass: every row with parseable
    # metadata is in it, so it is the expensive one of the three and the only
    # one whose insert cost is worth measuring before shipping.
    ("messages", """
    CREATE INDEX IF NOT EXISTS idx_messages_meta_turn_seq
        ON messages(thread_id, json_extract(metadata, '$.turn_id'), seq)
        WHERE json_valid(metadata)"""),
    # The column half of the same pass. Only rows claimed by a Turn's column
    # enter it, which is a small share of a mature thread.
    ("messages", """
    CREATE INDEX IF NOT EXISTS idx_messages_column_turn_seq
        ON messages(thread_id, active_turn_id, seq)
        WHERE active_turn_id IS NOT NULL"""),
)

SCOPED_HISTORY_INDEX_SQL = "".join(
    f"{sql};\n" for _table, sql in SCOPED_HISTORY_INDEXES
)
