"""Post-commit nudges for plan state that changes without a plan write."""

from __future__ import annotations

from collections.abc import Iterable
import logging

from plans.notifications import deliver, prepare_after_commit

logger = logging.getLogger(__name__)


class _Nudge:
    """One after-commit nudge; later writes in the same commit join its ids."""

    def __init__(self, plan_ids: Iterable[str]) -> None:
        self.plan_ids: dict[str, None] = dict.fromkeys(plan_ids)

    def __call__(self) -> None:
        _schedule_delivery(tuple(self.plan_ids))


def schedule_after_commit(plan_ids: Iterable[str | None], *, db) -> None:
    """Nudge the named plans once ``db``'s write commits, from any thread.

    For a plan write nobody else announces: the effect runs on the database
    worker, which cannot reach a socket, so it resolves the audiences there
    and hands delivery to the application loop. A write that rolls back
    nudges nothing, and a missed nudge is recovered by the reload on reconnect.
    Names scheduled during one commit merge into one pending nudge, so a
    commit sends one message per plan. A write inside a transaction some other
    owner commits has no commit to wait for here: it is skipped, never failed,
    and the nudge is lost until the client reconnects.
    """
    from infra.database import TransactionOwnershipError
    from infra.write_effects import after_commit, pending

    ids = tuple(dict.fromkeys(plan_id for plan_id in plan_ids if plan_id))
    if not ids:
        return
    if db.in_transaction:
        for effect in pending(db):
            if isinstance(effect, _Nudge):
                effect.plan_ids.update(dict.fromkeys(ids))
                return
    try:
        after_commit(db, _Nudge(ids))
    except TransactionOwnershipError:
        logger.warning(
            "plans_changed not scheduled: the write's transaction is not owned "
            "here; clients reload on reconnect"
        )


def thread_plan_ids(thread_id: str, *, db) -> tuple[str, ...]:
    """The plan a thread owns, as ids to nudge (empty when it has none)."""
    return tuple(
        str(row["id"])
        for row in db.execute(
            "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=?",
            (thread_id,),
        ).fetchall()
    )


def schedule_thread_after_commit(thread_id: str, *, db) -> None:
    """Nudge a thread's own plan, and so its parents, once ``db`` commits.

    For thread state the plan roll-up and chat list read without a plan write:
    a Turn starting, or a queued input clearing a stall.
    """
    schedule_after_commit(thread_plan_ids(thread_id, db=db), db=db)


def _schedule_delivery(plan_ids: tuple[str, ...]) -> None:
    import infra.app_loop as app_loop

    messages = prepare_after_commit(plan_ids)
    if not messages:
        return

    async def _send() -> None:
        deliver(messages)

    try:
        app_loop.submit(_send())
    except app_loop.AppLoopUnavailable:
        logger.debug("plans_changed not sent: no live event loop; clients reload on reconnect")


def schedule_for_accepted_input(thread_id: str, *, db) -> None:
    """Nudge a thread's plan when a queued input changes what its row reads.

    The parent row reads a child's queued input (``input_queued``) and its
    stalled state ('last Turn failed') from facts, not from a plan write. An
    input queued behind a running Turn changes the former at once; one queued
    on a stalled thread ends the latter.
    """
    from plans.wait_causes import stalled_why

    running = db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND status='running'", (thread_id,),
    ).fetchone()
    if running is not None or stalled_why(db, thread_id) is not None:
        schedule_thread_after_commit(thread_id, db=db)
