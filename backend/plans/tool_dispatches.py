"""Durable ownership of tool calls that started under selected plan work.

``plan_node_attempts`` alone selects work. This module records a call that
actually started under the executor's canonical valid selection, settles it
exactly once, and at Turn terminal closes whatever that Turn left open. It
never binds Turn accounting or refuses a call: without a live Turn a call
simply runs unrecorded. The tool boundary never records Plans' own tools (tool
catalog category ``plans``): whatever they change commits inside the Plans
write itself, so no effect of theirs can still land after it.

Selection belongs to Turn admission (``infra.thread_inputs.start_idle_turn``
via ``turn_admission.snapshot_after_admission``), which reconciles the
executor's plan before a Turn starts, and to each production release path,
which reconciles in its own write. Recording a call never selects work: a call
with no valid selection runs unrecorded and takes no write lock (#2168).

An open recorded call never holds its attempt's slot: parking a task
releases it at once (#2705), and the call's row keeps its attempt id for
attribution. Settling the call still releases an attempt that is no longer
valid but was never released, offers its task the slot back when the task is
still eligible, and reconciles its plan in the same write.

Every call ``record`` returns False for on a live Turn is counted in an
in-process dict keyed by Turn -- no write lock and no database write per call,
the same no-cost contract as the branch itself. A call with no Turn, or for a
Turn that is not live, is never counted: no Turn terminal would ever flush it.
``settle_turn`` writes the Turn's total once, at Turn terminal, inside the
terminal write it already runs in. A process restart mid-Turn loses whatever
the dict held, and a failed flush is logged and swallowed like every other
plan-state failure here, so written totals are a lower bound on how often a
thread ran with nothing selected, never an exact one.

This count is temporary diagnosis (``infra.unattributed_dispatch_schema``
states the question it answers and when to delete it).
"""

from __future__ import annotations

import logging
import threading

from infra.database import get_db
from plans import admission, selection_validity, service

log = logging.getLogger(__name__)

#: Every outcome a started call can settle with.
OUTCOMES = frozenset({"succeeded", "refused", "failed", "unknown"})

#: In-process, per-Turn count of work calls ``record`` returned False for.
#: Guarded by ``_UNATTRIBUTED_LOCK`` because calls for different threads run
#: concurrently on the async DB worker pool's threads.
_UNATTRIBUTED_LOCK = threading.Lock()
_UNATTRIBUTED_COUNTS: dict[str, tuple[str, int]] = {}


def _count_unattributed(thread_id: str, turn_id: str) -> None:
    """Count one unattributed work call by a live Turn, in memory only."""
    with _UNATTRIBUTED_LOCK:
        _thread, count = _UNATTRIBUTED_COUNTS.get(turn_id, (thread_id, 0))
        _UNATTRIBUTED_COUNTS[turn_id] = (thread_id, count + 1)


def _turn_is_live(db, thread_id: str, turn_id: str) -> bool:
    """Return whether this Turn is running or waiting to retry for this thread."""
    return db.execute(
        """SELECT 1 FROM active_turns
           WHERE id=? AND thread_id=? AND status='running'""",
        (turn_id, thread_id),
    ).fetchone() is not None


def _selection_for_call(db, thread_id: str, turn_id: str):
    """Return the attempt a call by this live Turn is recorded under, or None."""
    if not _turn_is_live(db, thread_id, turn_id):
        return None
    return selection_validity.attributed_selection(db, thread_id)


def record(
    thread_id: str,
    turn_id: str | None,
    tool_name: str,
    *,
    dispatch_id: str,
) -> bool:
    """Record one started tool call under the executor's canonical valid selection.

    Returns False, writing nothing and taking no write lock, when ``turn_id``
    is None, the Turn is not running or waiting to retry for this thread, or
    the thread has no valid selection. Otherwise re-validates inside one
    ``BEGIN IMMEDIATE`` write, inserts the dispatch row under ``dispatch_id``
    and returns True. Plan state never makes it raise. Every False returned
    here for a live Turn is counted for later measurement (module docstring).
    """
    if turn_id is None:
        return False
    db = get_db()
    try:
        if not _turn_is_live(db, thread_id, turn_id):
            return False
        if selection_validity.attributed_selection(db, thread_id) is None:
            _count_unattributed(thread_id, turn_id)
            return False
        with service._write(db):
            attempt = _selection_for_call(db, thread_id, turn_id)
            if attempt is None:
                # Under the write lock a live Turn has not reached its
                # terminal write yet, so its flush is still to come.
                if _turn_is_live(db, thread_id, turn_id):
                    _count_unattributed(thread_id, turn_id)
                return False
            db.execute(
                """INSERT INTO plan_tool_dispatches
                   (id,attempt_id,turn_id,tool_name,started_at)
                   VALUES (?,?,?,?,?)""",
                (
                    dispatch_id, attempt["id"], turn_id,
                    service._required_text(tool_name, "tool name"), service._now(),
                ),
            )
            return True
    finally:
        db.close()


def settle(dispatch_id: str, outcome: str) -> bool:
    """Settle one recorded call exactly once and release its attempt if now invalid.

    Reads first: an absent or already-settled row is a no-op that takes no
    write lock. Otherwise, in one write, closes the row, and when its attempt
    is still open but no longer valid, releases it as ``dispatch_settled``,
    offers its task the slot back and reconciles the attempt's plan.

    Returns:
        Whether this call closed the row.

    Raises:
        ValueError: ``outcome`` is not one of ``OUTCOMES``.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"invalid plan tool dispatch outcome: {outcome!r}")
    db = get_db()
    try:
        row = db.execute(
            """SELECT attempt_id FROM plan_tool_dispatches
               WHERE id=? AND settled_at IS NULL""",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            return False
        with service._write(db):
            changed = db.execute(
                """UPDATE plan_tool_dispatches SET settled_at=?,outcome=?
                   WHERE id=? AND settled_at IS NULL""",
                (service._now(), outcome, dispatch_id),
            )
            if changed.rowcount != 1:
                return False
            _release_if_invalid(
                db, row["attempt_id"], "dispatch_settled",
                provenance="plan_tool_dispatch_settlement",
            )
            return True
    finally:
        db.close()


def settle_turn(turn_id: str, *, db) -> int:
    """Close every call a terminal Turn left open, inside its terminal write.

    Each open call settles as ``unknown``. An affected attempt that is still
    valid stays selected; one that is open but no longer valid is released as
    ``turn_terminal``, its task is offered the slot back, and its plan is
    reconciled. No hold is created: a missing tool result proves nothing about
    an effect, and operation-specific owners reconcile their own.

    The Turn's count of calls made with nothing selected is written here too
    (``_flush_unattributed``), so Turn terminal has one Plans entry point.

    Returns:
        How many open calls were settled as ``unknown``.
    """
    _flush_unattributed(turn_id, db=db)
    rows = db.execute(
        """SELECT attempt_id FROM plan_tool_dispatches
           WHERE turn_id=? AND settled_at IS NULL
           ORDER BY started_at,id""",
        (turn_id,),
    ).fetchall()
    if not rows:
        return 0
    db.execute(
        """UPDATE plan_tool_dispatches SET settled_at=?,outcome='unknown'
           WHERE turn_id=? AND settled_at IS NULL""",
        (service._now(), turn_id),
    )
    for attempt_id in dict.fromkeys(str(row["attempt_id"]) for row in rows):
        _release_if_invalid(db, attempt_id, "turn_terminal", provenance="turn_terminal")
    return len(rows)


def _thread_plan_state(db, thread_id: str) -> str:
    """Return the thread's own active or paused plan state, or 'none'.

    Any other lifecycle (complete, archived, no plan at all) reports 'none':
    only active and paused can still hold agent-selectable work.
    """
    row = db.execute(
        "SELECT state FROM plans WHERE owner_kind='thread' AND owner_id=?"
        " AND state IN ('active','paused')",
        (thread_id,),
    ).fetchone()
    return str(row["state"]) if row is not None else "none"


def _flush_unattributed(turn_id: str, *, db) -> None:
    """Persist this Turn's unattributed-call count once, inside its terminal write.

    Pops the in-process counter for the Turn; a Turn with none writes nothing.
    Never raises: a failure to flush is logged and swallowed,
    the same way an uncounted call already is (module docstring) -- the number
    this measures is always a lower bound, never a correctness guarantee.
    """
    try:
        with _UNATTRIBUTED_LOCK:
            thread_id, count = _UNATTRIBUTED_COUNTS.pop(turn_id, ("", 0))
        if count <= 0:
            return
        plan_state = _thread_plan_state(db, thread_id)
        db.execute(
            """INSERT INTO plan_unattributed_dispatches
               (turn_id,thread_id,call_count,plan_state,created_at)
               VALUES (?,?,?,?,?)""",
            (turn_id, thread_id, count, plan_state, service._now()),
        )
    except Exception:
        log.exception(
            "unattributed dispatch flush failed: turn=%s", turn_id,
        )


def _release_if_invalid(db, attempt_id: str, reason: str, *, provenance: str) -> None:
    """Release an open attempt that is no longer valid, then reselect and reconcile.

    The released attempt's own task is offered its executor's slot before the
    plan is reconciled, so a task whose requirements changed while this call
    was open continues instead of losing its slot to earlier queued work. A
    legacy attempt that names a thread other than the plan owner is released
    the same way, and the task is offered to the plan owner.
    """
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()
    if attempt is None or attempt["released_at"] is not None:
        return
    if selection_validity.selection_valid(db, attempt):
        return
    admission.release(attempt["node_id"], reason, db=db)
    admission.reselect_and_reconcile(
        attempt["node_id"], actor_id=None, provenance=provenance, db=db,
    )
