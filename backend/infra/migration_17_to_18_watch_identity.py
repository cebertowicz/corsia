"""Schema 17 -> 18: a watch is one thread's wait on one artifact's event.

``idx_watches_dedupe`` keyed an armed watch on the external id, the condition
and the event alone. Two things that key could not tell apart:

- two integrations sharing an external id -- one integration's pull request
  deduplicated against another's, so arming reused or refused the wrong watch
  before the resolver, which matches the artifact's full identity, ever ran;
- two threads waiting on the same event of the same artifact -- the second arm
  was refused as "bound to a different plan actor", though each thread's wait
  is its own and resolves only when the event reaches that thread.

The key now carries the artifact's full identity and the owner, the same shape
resolution matches on. It is strictly weaker than the key it replaces -- every
row unique under the old key is unique under this one -- so no existing row
conflicts with it and nothing is deleted to make room.

The SQL is copied rather than imported so this step keeps producing the shape it
was written for, exactly as the steps before it do.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_IDENTITY_DEDUPE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_watches_dedupe "
    "ON expected_event_watches"
    "(artifact_integration, artifact_type, artifact_external_id, owner_thread_id, "
    "condition_type, condition_value, expected_event) "
    "WHERE status='armed'"
)


def migrate_17_to_18(conn: sqlite3.Connection) -> None:
    """Key an armed watch on the artifact's full identity and its owner."""
    conn.execute("DROP INDEX IF EXISTS idx_watches_dedupe")
    conn.execute(_IDENTITY_DEDUPE)
    log.info("Watch dedupe now keys on the artifact's full identity and its owner")
