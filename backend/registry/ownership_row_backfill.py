"""One-time retirement of the pane rows the shared ownership assignment added.

Before ownership stopped adding pane rows, the startup assignment left a
visible row for the conductor it registered each unregistered file under and
another for the owner it resolved. This pass retires exactly those rows, and
only where nothing has touched them since, through the shared retirement
writer, so each one is audited and comes back on pin or the next write.
"""

from __future__ import annotations

from collections import Counter

from infra.database import write_transaction
from registry import node_retirement, startup_backfill

_MARKER = "artifact_pane:ownership_assignment_rows_retired"
_TRIGGER = "ownership_assignment_backfill"

# A row qualifies only while it is still exactly what the assignment wrote:
# taskless, visible, unpinned, unretired, and last updated by the assignment's
# own event. The resolved owner's row was created by that event. The initial
# owner's row was created by the registration the assignment made immediately
# before it; a row its thread registered earlier, through real work, is not.
_ASSIGNMENT_ROWS = """
    SELECT DISTINCT r.artifact_id, r.parent_thread_id
      FROM artifact_registration_events AS a
      JOIN artifact_registrations AS r ON r.artifact_id=a.artifact_id
     WHERE a.event_type='ownership_rehomed' AND a.authority='system'
       AND a.reason='ownership_assignment'
       AND r.updated_at=a.created_at
       AND r.origin_plan_node_id IS NULL AND r.retired_at IS NULL
       AND r.surfacing='visible' AND r.relationship_state<>'owner-pinned'
       AND (
           (r.parent_thread_id=a.new_owner_thread_id AND r.created_at=a.created_at)
           OR (
               r.parent_thread_id=a.old_owner_thread_id
               AND EXISTS (
                   SELECT 1 FROM artifact_registration_events AS g
                    WHERE g.rowid=a.rowid-1 AND g.artifact_id=a.artifact_id
                      AND g.event_type='registered'
                      AND g.affected_thread_id=r.parent_thread_id
                      AND g.created_at=r.created_at
               )
           )
       )
     ORDER BY r.parent_thread_id, r.artifact_id"""


def retire_assignment_rows(db) -> dict[str, int]:
    """Retire the untouched rows the assignment added; return the count per thread."""
    retired: Counter[str] = Counter()
    with write_transaction(db, scope="retire_assignment_rows") as connection:
        for row in connection.execute(_ASSIGNMENT_ROWS).fetchall():
            retired[str(row["parent_thread_id"])] += node_retirement.retire_ownership_row(
                connection,
                artifact_id=str(row["artifact_id"]),
                thread_id=str(row["parent_thread_id"]),
                trigger=_TRIGGER,
            )
    return dict(retired)


def run_startup_backfill() -> None:
    """Run the pass once per instance."""
    startup_backfill.run_once(_MARKER, "ownership assignment rows", retire_assignment_rows)
