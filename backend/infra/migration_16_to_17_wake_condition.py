"""Schema 16 -> 17: a queued plan wake carries the condition it was collected for.

A plan wake is admitted when its source state still describes work that can
move. Between that append and the moment a Turn claims it, the work can be
closed, released, or made to wait -- and the Turn would then start for nothing.
The condition was checked once and discarded, so the claim had nothing to judge.

``messages.continuation_condition`` stores it. The column sits outside the
payload FIFO admission compares for duplicate identity (``metadata`` and
``delivery_options``), so a redelivery of the same input is still recognized as
the same input, and a claim-time judgement never changes what was delivered.

Rows queued before this step carry no condition and are claimed as they are
today: the wake that wrote them was current when it was appended.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)


def migrate_16_to_17(conn: sqlite3.Connection) -> None:
    """Add the queued input's collected continuation condition."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
    if "continuation_condition" in columns:
        return
    conn.execute(
        "ALTER TABLE messages ADD COLUMN continuation_condition TEXT "
        "CHECK (continuation_condition IS NULL OR ("
        "json_valid(continuation_condition) "
        "AND json_type(continuation_condition)='object'))"
    )
    log.info("Queued plan wakes now carry the condition they were collected for")
