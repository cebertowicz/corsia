"""Schema 53 -> 54: index the unbound-human reads the reply-window mark makes.

``history.reply_windows`` answers ``reply_window_opened`` once per assistant
Step in a finished Turn. Its metadata-only child-frame and unreadable-metadata
reads were thread-scoped, so one page read grew with the thread's full history
instead of the selected Turn ids.

Two partial indexes scope those rows. Neither is a table scan of any kind:
each contains only the thread's *unbound* human rows, which is the set the mark
already had to consider.

* ``idx_messages_unbound_human_turn`` keys those rows by their metadata Turn,
  so a selected Turn is found by seek rather than by reading the thread.
  ``json_valid(metadata)`` keeps the expression from raising on a row whose
  metadata is not JSON (the predicate is evaluated first, so such a row simply
  has no entry).
* ``idx_messages_unbound_human_unreadable`` carries that same validity bit, so
  the projection can still ask — by seek, not by scan — whether the thread holds
  a human row whose binding it cannot read, and fail open when it does. Its
  predicate excludes a NULL metadata column, which claims no Turn: a queued
  Send normally has none, and ``json_valid(NULL)`` is NULL on current SQLite and
  0 on older releases, so the guard is stated rather than inferred.

Purely additive: indexes only, no row or column changes.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

# Historical schema 54 carried these indexes. Keep their DDL with the historical
# migration so an old database can still replay every version before later
# migrations remove them again.
_UNBOUND_HUMAN_TURN_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_messages_unbound_human_turn
        ON messages(thread_id, json_extract(metadata, '$.turn_id'))
        WHERE role='user'
          AND active_turn_id IS NULL
          AND json_valid(metadata)
"""

_UNBOUND_HUMAN_UNREADABLE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_messages_unbound_human_unreadable
        ON messages(thread_id, json_valid(metadata))
        WHERE role='user'
          AND active_turn_id IS NULL
          AND metadata IS NOT NULL
"""

_INDEX_53_TO_54_DDL = (
    _UNBOUND_HUMAN_TURN_INDEX_SQL,
    _UNBOUND_HUMAN_UNREADABLE_INDEX_SQL,
)


def migrate_53_to_54(conn: sqlite3.Connection) -> None:
    """Add the unbound-human indexes the reply-window mark reads through."""
    for statement in _INDEX_53_TO_54_DDL:
        conn.execute(statement)
