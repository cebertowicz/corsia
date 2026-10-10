"""Indexes for the reads writers make while they hold the write lock.

SQLite has one write lock, so a read inside a write transaction delays every
other writer for as long as it runs. Each index here bounds one such read by
what it asks for rather than by a thread's whole history.

One definition serves both the fresh schema and the v83 -> v84 migration, so
fresh and migrated databases carry identical indexes. Production databases
never run ANALYZE, so each index is shaped to win the planner's default
estimates; the v84 tests pin that.
"""

from __future__ import annotations


WRITE_HOLD_INDEXES: tuple[tuple[str, str], ...] = (
    # Queued input: account holds, FIFO heads, stall causes and the vendor
    # policy refresh ask whether a thread has any. Without this each probe reads
    # every input row the thread ever had. Only queued rows enter it, so it
    # stays small; delivery_state as a key column gives it a second equality,
    # so the planner prefers it to idx_messages_input_order in any creation
    # order instead of by a tie.
    ("messages", """
    CREATE INDEX IF NOT EXISTS idx_messages_queued_inputs
        ON messages(thread_id, delivery_state, input_order)
        WHERE input_order IS NOT NULL AND delivery_state='queued'"""),
    # A thread's running Turn, asked at every claim, Turn end and stall check;
    # without the status key the probe reads every Turn the thread ever ran.
    ("active_turns", """
    CREATE INDEX IF NOT EXISTS idx_active_turns_thread_status
        ON active_turns(thread_id, status)"""),
)

WRITE_HOLD_INDEX_SQL = "".join(f"{sql};\n" for _table, sql in WRITE_HOLD_INDEXES)
