"""Schema 57 -> 58: non-user blockers become waits; gone owners' plans retire.

Until stuck plans option C, ``block_node`` placed a hold whoever cleared it,
and a hold has no end: a task blocked on another thread, an outside party or
the system sat unchanged until someone happened to look (#2310's stuck
plans). ``block_node`` now arms a deadline watch for those owners and keeps a
hold only for a blocker the user clears. This moves the earlier ones the same
way, so there is one representation of a wait on anyone but the user.

First, a plan whose owner thread was deleted or purged before delete meant
delete (#2647) is archived, as ``plans.lifecycle.archive_ended_owner`` now
does when an owner goes. The rest of that path already ran when the thread
went: its open work was closed and its parent told, so only the archive is
owed, and its blockers are left as they are, since a wait would wake no one.

A hold qualifies when it is a blocker, uncleared, owned by anyone but the
user, on a task still open (pending, active or in review), on an unarchived
plan a thread owns. A plan no thread owns keeps its holds: a wait there would
wake no one, and the runtime refuses to arm one (``blocked_wait.authorize``).
Each is cleared and replaced by a wait due 24 hours after this step runs, the
default wait, with the hold's own reason, key and owner, through the runtime's
own writers (``plans.blocked_wait.convert_hold``). Every event this step
writes carries its provenance, so no owner or parent is woken or told
(``plans.plan_events.from_schema_step``): the worker raised nothing new. The wait wakes the plan's owner when it ends, which is the first
time anything looks at these again. A settled or archived task's hold is left
alone: nothing shows or selects it.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

_PROVENANCE = "migration.57_to_58"
#: ``plans.blocked_wait.DEFAULT_WAIT``, copied so this step keeps its meaning.
_WAIT = timedelta(hours=24)

_GONE_OWNERS_SQL = """
    SELECT p.owner_id, CASE WHEN t.id IS NULL THEN 'deleted' ELSE 'purged' END AS reason
      FROM plans p LEFT JOIN threads t ON t.id=p.owner_id
     WHERE p.owner_kind='thread' AND p.state<>'archived'
       AND (t.id IS NULL OR t.lifecycle_state='purged')
     ORDER BY p.owner_id
"""

_CANDIDATES_SQL = """
    SELECT h.* FROM plan_node_holds h
      JOIN plan_nodes n ON n.id=h.node_id
      JOIN plans p ON p.id=n.plan_id AND p.owner_kind='thread' AND p.state<>'archived'
     WHERE h.kind='blocked' AND h.cleared_at IS NULL AND h.owner_kind<>'user'
       AND n.state IN ('pending','active','review_requested') AND n.archived_at IS NULL
     ORDER BY h.created_at, h.id
"""


def migrate_57_to_58(conn: sqlite3.Connection) -> None:
    """Archive gone owners' plans, then replace every open non-user blocker hold with a wait."""
    from plans import public

    deadline = (datetime.now(timezone.utc) + _WAIT).isoformat()
    # The plan writers read rows by name; the caller's connection is left as found.
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        for owner in conn.execute(_GONE_OWNERS_SQL).fetchall():
            public.archive_ended_owner(
                str(owner["owner_id"]), reason=str(owner["reason"]),
                provenance=_PROVENANCE, db=conn,
            )
        for hold in conn.execute(_CANDIDATES_SQL).fetchall():
            public.convert_blocked_hold(
                conn, hold, deadline=deadline, provenance=_PROVENANCE,
            )
    finally:
        conn.row_factory = caller_factory
