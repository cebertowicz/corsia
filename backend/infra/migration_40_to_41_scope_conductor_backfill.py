"""Schema 40 -> 41: designate conductors for scopes that predate them (#1940).

Conductor creation moved to creation time (issue #1940): the first account
creates the workspace conductor, and every new project is born with one.
Installs upgrading from v39 carry scopes with no designation — the original
defect. This heals each of them once, inside the runner's transaction, with
the runtime's own writer (``infra.conductor``), so the healing is atomic with
the version bump.

One-time by design: after this migration runs, ``PUT /api/conductor`` with
``thread_id: null`` is a deliberate operator clear that must survive every
later restart — which a recurring startup pass would silently undo while
piling up replacement threads.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import logging
import sqlite3

_logger = logging.getLogger(__name__)


def migrate_40_to_41(conn: sqlite3.Connection) -> None:
    """Designate a conductor for every scope that lacks one."""
    from infra.conductor import backfill_coordination_scopes

    # The conductor writer reads rows by name; the caller's connection is
    # left as found.
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        healed = backfill_coordination_scopes(db=conn)
        for scope, thread_id in healed:
            _logger.info("conductor backfill: scope=%s thread=%s", scope, thread_id)
    finally:
        conn.row_factory = caller_factory
