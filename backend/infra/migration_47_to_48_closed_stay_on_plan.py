"""Schema 47 -> 48: tasks closed before #1887 come back onto their plans.

Until #1887 ``plans.closure.close_node`` archived the node in the same write
as its close, so a closed task vanished from the plan. Closing no longer
archives. This puts the earlier ones back, so there is one representation of
"closed" rather than two.

The runtime answers "was this archived by its own close?" for every archived
closed node: only ``close_node`` wrote ``closed``, it always archived, and
``archive_node`` refuses a node that is already archived, so no later archive
can have followed. A node goes back only under a parent that is on the plan
(or is coming back with it), never under an archived one, so the pass repeats
until nothing more qualifies. Only ``archived_at`` changes: revisions and
``updated_at`` keep their true history. The dependency edges and watches the
archive dropped stay dropped, which is what a close does today.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3

_RESTORE_SQL = """
    UPDATE plan_nodes SET archived_at=NULL
     WHERE state='closed' AND archived_at IS NOT NULL
       AND (parent_id IS NULL OR EXISTS (
             SELECT 1 FROM plan_nodes parent
              WHERE parent.id=plan_nodes.parent_id AND parent.archived_at IS NULL
           ))
"""


def migrate_47_to_48(conn: sqlite3.Connection) -> None:
    """Restore every closed node its close archived, parents before children."""
    while conn.execute(_RESTORE_SQL).rowcount:
        pass
