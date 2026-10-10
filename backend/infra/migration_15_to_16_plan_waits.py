"""Schema 15 -> 16: a settled watch is history, and waits are read by thread.

Three changes, all durability of waits:

- ``idx_watches_dedupe`` becomes partial on ``status='armed'``. The old index
  covered every status, so arming a condition whose earlier watch had resolved
  returned that terminal row: the task then waited on a watch nothing would ever
  resolve again. The partial index is strictly weaker than the one it replaces,
  so no existing row can conflict with it and nothing is deleted to make room.
- ``plan_nodes`` gains the two thread indexes a wait reads: a step's delegated
  child, and an executor's own tasks.
- Pending or deferred ``t2_watch_expiry`` continuations whose watch has already
  left ``armed`` are cancelled once. Every write that settles a watch now
  cancels its expiry in the same transaction; this settles the rows that were
  written before it did, instead of leaving a scan to find them later.

The SQL is copied rather than imported so this step keeps producing the shape it
was written for, exactly as the steps before it do.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import sqlite3

log = logging.getLogger(__name__)

_ARMED_ONLY_DEDUPE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_watches_dedupe "
    "ON expected_event_watches"
    "(artifact_external_id, condition_type, condition_value, expected_event) "
    "WHERE status='armed'"
)
_WAIT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_spawned_thread "
    "ON plan_nodes(spawned_thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_plan_nodes_executor_thread "
    "ON plan_nodes(executor_thread_id)",
)
_SETTLE_ORPHANED_EXPIRY = """
    UPDATE continuation_triggers
       SET state='cancelled', reason='watch_settled', settled_at=?, updated_at=?
     WHERE state IN ('pending','deferred') AND kind='t2_watch_expiry'
       AND subject_id IN (
         SELECT id FROM expected_event_watches WHERE status <> 'armed'
       )
"""


def migrate_15_to_16(conn: sqlite3.Connection) -> None:
    """Make watch dedupe armed-only, index the wait reads, settle stale expiries."""
    settled_at = datetime.now(timezone.utc).isoformat()
    conn.execute("DROP INDEX IF EXISTS idx_watches_dedupe")
    conn.execute(_ARMED_ONLY_DEDUPE)
    for statement in _WAIT_INDEXES:
        conn.execute(statement)
    orphaned = conn.execute(
        _SETTLE_ORPHANED_EXPIRY, (settled_at, settled_at),
    ).rowcount
    log.info(
        "Watch dedupe is armed-only; cancelled %d expiry continuation(s) whose "
        "watch had already settled",
        orphaned,
    )
