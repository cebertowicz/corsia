"""Journal the plan status a plan write left owed, off the writer lock where it can.

A plan write marks each thread plan it touched in ``plan_status_dirty``, in
its own transaction (``status_context.mark_touched``). The thin projection is
then journaled here, in one of three places:

* **After commit** (``schedule``): the projection is computed in a read
  snapshot, off the writer lock, and a short write appends it -- but only if
  the marker still carries the ``dirty_seq`` the snapshot read. A write that
  landed meanwhile replaced the marker with a newer seq, so this drain
  appends nothing and leaves the plan dirty for that write's own drain.
* **Before a digest** (``drain_for_digest``): the owner's next digest must
  carry the current status, so every status its plans still owe is
  journaled first, under the caller's lock. Inside an open plan write that
  includes the plans the write touched so far, which carry no marker yet.
  The write still marks them at its end, so the after-commit drain
  recomputes what the rest of the write changed.
* **At startup** (``sweep``): a marker a process exit left behind.

A drain that fails logs and leaves the marker; the next drain journals it.
"""

from __future__ import annotations

import logging
from typing import Iterable

from infra import write_effects
from infra.database import TransactionOwnershipError, get_db, write_transaction

log = logging.getLogger(__name__)

_EFFECT_KEY = "plan_status_drain"


def mark(db, plan_ids: Iterable[str]) -> None:
    """Mark each plan as owing its status, with a ``dirty_seq`` never used before."""
    for plan_id in plan_ids:
        db.execute("INSERT OR REPLACE INTO plan_status_dirty (plan_id) VALUES (?)", (plan_id,))


def schedule(db, plan_ids: Iterable[str]) -> None:
    """Drain the plans once ``db``'s write commits, merged into one effect per transaction.

    A write inside a transaction nobody here owns has no commit to wait for,
    so it drains in place, under that transaction's lock, as before.
    """
    ids = list(plan_ids)
    try:
        write_effects.after_commit_merged(db, _EFFECT_KEY, ids, lambda owed: drain(db, owed))
    except TransactionOwnershipError:
        _drain_in_place(db, ids)


def drain(db, plan_ids: Iterable[str]) -> None:
    """Journal each plan's owed status: computed off the lock, appended in a short write."""
    if db.in_transaction:
        # Effects of a frame nested in another owner's transaction come due
        # before that transaction commits; there is no lock to step out of.
        _drain_in_place(db, plan_ids)
        return
    for plan_id in plan_ids:
        try:
            _drain_committed(db, plan_id)
        except Exception:
            log.exception("plan status drain failed for plan %s; its marker stays", plan_id)


def drain_for_digest(db, thread_id: str) -> None:
    """Journal every status ``thread_id``'s own plans owe, before its digest reads the journal."""
    from plans import plan_write, status_context

    owed = [
        str(row[0])
        for row in db.execute(
            "SELECT dirty.plan_id FROM plan_status_dirty dirty JOIN plans p ON p.id=dirty.plan_id "
            "WHERE p.owner_kind='thread' AND p.owner_id=? ORDER BY dirty.dirty_seq",
            (thread_id,),
        )
    ]
    started = plan_write.write_started(db)
    if started is not None:
        owed += [
            str(row["id"])
            for row in status_context.touched_plans(db, since=started, owner_id=thread_id)
        ]
    if owed:
        _drain_in_place(db, dict.fromkeys(owed))


def sweep(db=None) -> None:
    """Drain every marker left behind, such as by a process exit between commit and drain."""
    connection = db if db is not None else get_db()
    owed = [
        str(row[0])
        for row in connection.execute("SELECT plan_id FROM plan_status_dirty ORDER BY dirty_seq")
    ]
    if owed:
        log.info("plan status sweep: draining %d owed status(es)", len(owed))
        drain(connection, owed)


def _read_owed(db, plan_id: str) -> tuple[int, tuple[str, int] | None] | None:
    """The plan's marker seq and its status, read in one snapshot off the writer lock.

    A deferred transaction that only reads takes no write lock, and every
    statement in it sees the same committed state. Returns None when nothing
    is owed.
    """
    from plans import status_context

    db.execute("BEGIN")
    try:
        # One read for the marker, the plan and its owner thread: this runs
        # after every plan write, at turn start too.
        row = db.execute(
            """SELECT p.*, dirty.dirty_seq AS owed_seq, t.id AS owed_owner,
                       t.user_id AS owed_owner_user, t.project_id AS owed_owner_project
                FROM plan_status_dirty dirty
                LEFT JOIN plans p ON p.id=dirty.plan_id
                LEFT JOIN threads t ON p.owner_kind='thread' AND t.id=p.owner_id
                WHERE dirty.plan_id=?""",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        seq = int(row["owed_seq"])
        if row["id"] is None or row["owed_owner"] is None:
            return seq, None
        plan = {key: row[key] for key in row.keys() if not key.startswith("owed_")}
        owner = (str(row["owed_owner_user"]), row["owed_owner_project"])
        return seq, status_context.compute_status(db, plan, owner=owner)
    finally:
        db.execute("ROLLBACK")


def _drain_committed(db, plan_id: str) -> None:
    from plans import status_context

    owed = _read_owed(db, plan_id)
    if owed is None:
        return
    seq, computed = owed
    with write_transaction(db, scope="plan_status_drain") as connection:
        cleared = connection.execute(
            "DELETE FROM plan_status_dirty WHERE plan_id=? AND dirty_seq=?", (plan_id, seq),
        ).rowcount
        # Gone: another drain journaled it. Replaced: a later write owes a newer
        # status, which its own drain (or the next digest) journals.
        if cleared and computed is not None:
            status_context.append_status(connection, plan_id, *computed)


def _drain_in_place(db, plan_ids: Iterable[str]) -> None:
    """Journal each plan's status inside the transaction ``db`` holds, one savepoint each."""
    from plans import status_context

    for plan_id in plan_ids:
        try:
            with write_transaction(db, scope="plan_status_drain") as connection:
                row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
                if row is not None:
                    status_context.journal_status(connection, row)
                connection.execute("DELETE FROM plan_status_dirty WHERE plan_id=?", (plan_id,))
        except Exception:
            log.exception("plan status drain failed for plan %s; its marker stays", plan_id)
