"""Schema 58 -> 59: pauses an agent placed on its own plan become scheduled tasks.

Pause is the user's now (``plans.scheduled_wait``, W1 of
``shared/planning/plans-work-never-stops-2026-09-28.md``). Until then an agent
could pause its own task or its whole plan, and a pause has no end: nothing
ever came back to it. This moves every such pause to what an agent does
instead, a task scheduled until a date with the pause's reason.

First, a pause the user placed before holds recorded the user as
``owner_kind`` (``thread`` with the user's ``user:`` audit id) is recorded as
the user's, so ``owner_kind='user'`` is the one reading of a user pause from
here on (``user_steering.is_user_pause``, and the user's time in
``plans.node_waiting``).

What moves, on an unarchived plan a thread owns (schema 58 archived those
whose owner is gone):

- an open pause hold on an open, unarchived task that the plan's own owner
  thread placed (``scheduled_wait.is_agent_pause``, the runtime's predicate);
  the user's pauses stay;
- a whole-plan pause whose latest pausing transition is the owner thread's
  own act, not a cascade from above and not the user's, placed before an
  agent's plan pause needed the user's message in its Turn: the plan is
  resumed through the runtime writer and each of its open tasks that nothing
  else holds or watches is scheduled with "Set aside before agents could
  pause". A plan pause placed after that passed on the user's own request,
  so it stays paused, the user's to resume. Task pauses never needed the
  user's message, so every one the owner placed moves.

Deadlines are spread evenly over the 7 days after this step runs, oldest pause
first, the first more than an hour out, so the moved pauses do not all wake at
once. Every event is marked as a move, so no parent is told. A lifted plan's
tasks are scheduled before its pause is lifted, so lifting it finds nothing
ready to select and tells its agent nothing. Rerunning finds nothing left to
move.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from infra.plan_schema import HOLD_UPDATE_GUARD_SQL

_PROVENANCE = "migration.58_to_59"
#: When an agent's whole-plan pause started to need the user's message in its
#: Turn (#2237 merged 2026-09-24 04:04:25Z; ``pause_authority.require``). A
#: plan pause placed from then on relayed the user. The merge, not the later
#: deploy, so a pause placed in between also stays: nothing restarts that the
#: user may have asked to stop.
_PLAN_PAUSE_RELAYED_USER_FROM = "2026-09-24T04:04:25"
_FIRST = timedelta(hours=1)
_SPREAD = timedelta(days=7)

_LEGACY_USER_PAUSES_SQL = """
    UPDATE plan_node_holds SET owner_kind='user'
     WHERE kind='paused' AND owner_kind='thread' AND owner_id LIKE 'user:%'
"""


def _record_legacy_user_pauses(conn: sqlite3.Connection) -> None:
    """Name the user as owner on older user pauses, cleared ones included.

    Holds are otherwise immutable; the guard is lifted for this one update and
    re-created from its single definition in the same transaction.
    """
    conn.execute("DROP TRIGGER IF EXISTS restrict_plan_hold_update")
    conn.execute(_LEGACY_USER_PAUSES_SQL)
    conn.execute(HOLD_UPDATE_GUARD_SQL)


_HOLDS_SQL = """
    SELECT h.*, p.owner_id AS plan_owner_id FROM plan_node_holds h
      JOIN plan_nodes n ON n.id=h.node_id
      JOIN plans p ON p.id=n.plan_id AND p.owner_kind='thread' AND p.state<>'archived'
     WHERE h.kind='paused' AND h.cleared_at IS NULL
       AND n.state IN ('pending','active','review_requested') AND n.archived_at IS NULL
     ORDER BY h.created_at, h.id
"""

_PAUSED_PLANS_SQL = """
    SELECT p.id, p.owner_id, e.actor_id, e.payload_json, e.created_at FROM plans p
      JOIN plan_events e ON e.id=(
        SELECT id FROM plan_events WHERE plan_id=p.id AND event='state_transition'
           AND new_state='paused' ORDER BY created_at DESC, id DESC LIMIT 1)
     WHERE p.owner_kind='thread' AND p.state='paused'
     ORDER BY e.created_at, p.id
"""

#: Open tasks a lifted plan pause leaves with nothing that ends their wait.
_UNHELD_TASKS_SQL = """
    SELECT n.id FROM plan_nodes n
     WHERE n.plan_id=? AND n.kind='task' AND n.archived_at IS NULL
       AND n.state IN ('pending','active')
       AND NOT EXISTS (SELECT 1 FROM plan_node_holds h
                        WHERE h.node_id=n.id AND h.cleared_at IS NULL)
       AND NOT EXISTS (SELECT 1 FROM expected_event_watches w
                        WHERE w.plan_node_id=n.id AND w.status='armed')
     ORDER BY n.order_key, n.id
"""


def _moves(conn) -> list[tuple[str, str, sqlite3.Row]]:
    """Every pause to move, as (placed at, kind, subject), oldest first."""
    from plans import public

    moves: list[tuple[str, str, sqlite3.Row]] = [
        (hold["created_at"], "hold", hold)
        for hold in conn.execute(_HOLDS_SQL).fetchall()
        if public.is_agent_pause(hold, hold["plan_owner_id"])
    ]
    for plan in conn.execute(_PAUSED_PLANS_SQL).fetchall():
        carried = plan["payload_json"] and "cascade_root" in plan["payload_json"]
        relayed = plan["created_at"] >= _PLAN_PAUSE_RELAYED_USER_FROM
        if plan["actor_id"] == plan["owner_id"] and not carried and not relayed:
            moves.append((plan["created_at"], "plan", plan))
    return sorted(moves, key=lambda move: (move[0], move[1]))


def migrate_58_to_59(conn: sqlite3.Connection) -> None:
    """Schedule every open agent pause, spread over the next 7 days."""
    from plans import public

    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        _record_legacy_user_pauses(conn)
        moves = _moves(conn)
        # A lifted plan's tasks are read while every pause hold is still open,
        # so a task the agent had also paused is scheduled once, from its own
        # hold. The plans are lifted last, once nothing on them is ready.
        slots = [
            task for _placed, kind, subject in moves
            for task in ([subject] if kind == "hold" else conn.execute(
                _UNHELD_TASKS_SQL, (subject["id"],),
            ).fetchall())
        ]
        _schedule(conn, slots, now=datetime.now(timezone.utc))
        for _placed, kind, subject in moves:
            if kind == "plan":
                public.lift_agent_plan_pause(
                    conn, str(subject["id"]), provenance=_PROVENANCE,
                )
    finally:
        conn.row_factory = caller_factory


def _schedule(conn, slots: list, *, now: datetime) -> None:
    """Give each slot, in order, its evenly spaced deadline."""
    from plans import public

    count = len(slots)
    for index, slot in enumerate(slots):
        deadline = (now + _FIRST + (_SPREAD - _FIRST) * (index + 1) / count).isoformat()
        if "kind" in slot.keys():
            public.schedule_agent_pause(
                conn, slot, deadline=deadline, provenance=_PROVENANCE,
            )
        else:
            public.schedule_moved_task(
                conn, str(slot["id"]), reason=public.MOVED_PAUSE_REASON,
                deadline=deadline, provenance=_PROVENANCE,
            )
