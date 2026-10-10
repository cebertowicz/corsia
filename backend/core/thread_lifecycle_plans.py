"""Plan-side callbacks for thread lifecycle transitions."""

from __future__ import annotations


def withdraw_owned_decisions(db, thread_id: str, *, reason: str) -> None:
    """Withdraw decisions owned by a thread that is becoming unavailable."""
    from plans import public

    public.withdraw_for_owner_thread_in_transaction(
        db,
        thread_id,
        reason=reason,
        provenance="lifecycle.thread_archive",
    )
    announce_lifecycle(db, thread_id)


def wake_parent_of_ended_child(db, thread_id: str, *, reason: str) -> None:
    """Tell a delegating parent that its child ended."""
    from plans import public

    public.on_child_thread_ended(db, thread_id, reason=reason)


def announce_lifecycle(db, thread_id: str) -> None:
    """Nudge the thread's plan and its parents after a lifecycle change.

    A parent's row reads the child's ``lifecycle_state`` as a fact, so a state
    change that settles no node (restore, retention archive, purge) is announced
    here rather than by a plan write.
    """
    from plans import public

    public.schedule_thread_after_commit(thread_id, db=db)
