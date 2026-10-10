"""Best-effort post-commit invalidation for canonical operator plan reads."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from typing import Any

import infra.config as config
import infra.state as state
from infra.database import get_db
from infra.send_target import NO_MODEL_SELECTED
from plans import hierarchy

CONTRACT_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeferredMutation:
    """A durable mutation result plus content-free delivery facts."""

    result: dict[str, Any]
    messages: tuple[tuple[str, dict], ...]
    transcript_rows: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    artifact_messages: tuple[tuple[str, dict], ...] = ()
    #: Threads this committed write gave work to, driven once after commit.
    drives: tuple[str, ...] = ()


def capture_message_floor(db) -> int:
    """Mark the start of one transaction's possible transcript appends."""
    return int(db.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0])


def _transcript_rows_since(floor: int | None, *, db):
    """Read exact plan-origin rows appended by the just-committed mutation."""
    if floor is None:
        return ()
    from infra.send_broadcast import read_input_broadcast_row

    rows = db.execute(
        "SELECT thread_id,id FROM messages WHERE rowid>? "
        "AND json_valid(metadata) AND json_extract(metadata,'$.origin')='plan' "
        "ORDER BY rowid",
        (floor,),
    ).fetchall()
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        projected = read_input_broadcast_row(str(row["id"]), db=db)
        if projected is not None:
            grouped[str(row["thread_id"])].append(projected)
    return tuple((thread_id, tuple(items)) for thread_id, items in grouped.items())


def _thread_scope(owner_id: str, *, db):
    return db.execute(
        "SELECT user_id,project_id FROM threads WHERE id=?", (owner_id,)
    ).fetchone()


def _audiences(plan, *, db) -> tuple[str, ...]:
    kind, owner_id = plan["owner_kind"], plan["owner_id"]
    if kind == "thread":
        thread_row = _thread_scope(owner_id, db=db)
        return (thread_row["user_id"],) if thread_row is not None else ()
    if kind == "project_scope":
        rows = db.execute(
            "SELECT DISTINCT user_id FROM threads WHERE project_id=? ORDER BY user_id",
            (owner_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT DISTINCT user_id FROM threads ORDER BY user_id"
        ).fetchall()
    return tuple(row["user_id"] for row in rows)


def _change(plan, *, db) -> dict:
    project_id = None
    if plan["owner_kind"] == "thread":
        thread_row = _thread_scope(plan["owner_id"], db=db)
        project_id = thread_row["project_id"] if thread_row is not None else None
    elif plan["owner_kind"] == "project_scope":
        project_id = plan["owner_id"]
    return {
        "plan_id": plan["id"],
        "revision": plan["revision"],
        "owner": {"kind": plan["owner_kind"], "id": plan["owner_id"]},
        "scope": {
            "workspace_id": config.WORKSPACE_SELECTION.workspace_id,
            "project_id": project_id,
        },
    }


def _changes_by_user(plan_ids: Iterable[str], *, db) -> dict[str, list[dict]]:
    """Resolve authorized content-free changes from committed plan rows."""
    by_user: dict[str, list[dict]] = defaultdict(list)
    for plan_id in hierarchy.with_attaching_plans(plan_ids, db=db):
        plan = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            continue
        change = _change(plan, db=db)
        for user_id in _audiences(plan, db=db):
            by_user[user_id].append(change)
    return by_user


def prepare_after_commit(
    plan_ids: Iterable[str],
    *,
    db=None,
) -> tuple[tuple[str, dict], ...]:
    """Resolve delivery facts, containing the full recoverable preparation."""
    connection = db
    owned = connection is None
    messages: tuple[tuple[str, dict], ...] = ()
    try:
        if connection is None:
            connection = get_db()
        if connection.in_transaction:
            logger.error(
                "plans_changed caller contract violation: preparation requires "
                "a committed transaction"
            )
        else:
            by_user = _changes_by_user(plan_ids, db=connection)
            messages = tuple(
                (
                    user_id,
                    {
                        "type": "plans_changed",
                        "contract_version": CONTRACT_VERSION,
                        "plans": changes,
                    },
                )
                for user_id, changes in by_user.items()
            )
    except Exception as exc:  # noqa: BLE001 - committed state remains authoritative
        logger.warning(
            "Post-commit plans_changed preparation failed: %s",
            type(exc).__name__,
        )
    if owned and connection is not None:
        try:
            connection.close()
        except Exception as exc:  # noqa: BLE001 - committed state remains authoritative
            logger.warning(
                "Post-commit plans_changed preparation failed: %s",
                type(exc).__name__,
            )
            messages = ()
    return messages


def deliver(messages: Iterable[tuple[str, dict]]) -> bool:
    """Best-effort delivery on the caller's event loop; GET remains recovery."""
    delivered = False
    for user_id, message in messages:
        delivered = True
        try:
            state.broadcast_to_user(user_id, message)
        except Exception as exc:  # noqa: BLE001 - invalidation is recoverable
            logger.warning(
                "Post-commit plans_changed delivery failed for user %s: %s",
                user_id,
                type(exc).__name__,
            )
    return delivered


def deferred_mutation(
    result: dict[str, Any],
    plan_ids: Iterable[str],
    *,
    db,
    message_floor: int | None = None,
    artifact_event_floor: int | None = None,
    drives: tuple[str, ...] = (),
):
    """Carry committed plan and artifact invalidations across a sync boundary.

    ``drives`` is read by the caller *inside* its write and handed here, not
    derived after commit: a rowid floor says when a row was written, never by
    whom, so a post-commit read can sweep up a concurrent mutation's wake and
    claim a recipient this write never gave work to.
    """
    from registry.node_retirement import after_commit as artifact_after_commit

    return DeferredMutation(
        result,
        prepare_after_commit(plan_ids, db=db),
        _transcript_rows_since(message_floor, db=db),
        artifact_after_commit(artifact_event_floor, db=db),
        drives,
    )


#: The inputs this domain appends for another thread to act on. A plan-origin
#: row that is not one of these is a record, not an errand, and driving its
#: thread would start Turns nobody asked for.
_WAKE_IDS = ("plan-wake:%", "plan-decision-answer:%")


def wake_recipients(floor: int | None, *, db) -> tuple[str, ...]:
    """Return the threads this write gave a result-bearing plan input to.

    Call this **inside** the write that appended them. SQLite serialises
    writers, so while this transaction holds the write lock no other can
    commit, and every row above the floor is necessarily this write's own.
    Asking after commit cannot tell the two apart.

    It runs under the writer lock, so it reads only the rows above the floor:
    deduplicating and ordering in SQL makes SQLite walk the whole
    ``(thread_id, created_at)`` index instead (#2489).

    An input held for a model the user has yet to choose wakes nobody: there is
    nothing for the driver to run until that choice releases it.
    """
    if floor is None:
        return ()
    rows = db.execute(
        "SELECT thread_id FROM messages WHERE rowid>? AND (id LIKE ? OR id LIKE ?) "
        "AND COALESCE(delivery_error,'')!=?",
        (floor, *_WAKE_IDS, NO_MODEL_SELECTED),
    ).fetchall()
    return tuple(sorted({str(row["thread_id"]) for row in rows}))


@contextmanager
def collect_wake_recipients(db, into: list[str] | None) -> Iterator[None]:
    """Add to ``into`` the threads the write inside this block gave a plan wake.

    The rule ``boundary.mutate`` applies to its own write, for the writers that
    have no mutation to carry it (the child-return harvest, a worker's Turn
    end, a thread's archive): a floor captured as the block opens and read as
    it closes, both inside the write and so under its lock, where every row
    above the floor is necessarily this write's own. Whoever awaits after the
    commit hands ``into`` to :func:`drive_threads`; nothing here starts a Turn.
    A block that raises collects nothing, and ``into=None`` collects nothing
    for a caller that has no way to drive.
    """
    if into is None:
        yield
        return
    floor = capture_message_floor(db)
    yield
    for thread_id in wake_recipients(floor, db=db):
        if thread_id not in into:
            into.append(thread_id)


async def drive_deferred(value) -> None:
    """Drive each thread a committed Plans mutation gave work to, once."""
    if isinstance(value, DeferredMutation):
        await drive_threads(value.drives)


def drive_after_response(value) -> None:
    """Drive the threads a committed write woke, once the response is on its way.

    The write is durable and its wake is in the target thread's FIFO, which owns
    recovery, so a request does not wait on that thread's Turn preparation. The
    task is retained and its failure logged by the shared tracker; a drive that
    cannot take input is already logged by :func:`drive_threads`. Call it from a
    running event loop only after the write has committed.
    """
    if not getattr(value, "drives", None):
        return
    from ws.ingress import track_background_task

    track_background_task(
        asyncio.get_running_loop().create_task(drive_deferred(value)),
    )


async def drive_threads(threads: Iterable[str]) -> None:
    """Drive each thread a committed Plans write gave work to, once.

    The write is already durable, so a recipient that cannot take input right
    now is logged and left to the durable FIFO, exactly as the decision-answer
    drive this generalizes has always done. Call it only after the write's
    owner has committed: a drive inside that write cannot see the input it is
    for, and waits on the write lock its own caller holds.
    """
    pending = tuple(dict.fromkeys(threads))
    if not pending:
        return
    from infra.send_types import SendRejected
    from infra.thread_execution import drive_thread

    for thread_id in pending:
        try:
            await drive_thread(thread_id)
        except SendRejected as exc:
            logger.warning(
                "Post-commit plan wake drive rejected for thread %s: %s",
                thread_id,
                exc.detail,
            )
        except Exception as exc:  # noqa: BLE001 - durable FIFO owns recovery
            logger.warning(
                "Post-commit plan wake drive failed for thread %s: %s",
                thread_id,
                type(exc).__name__,
            )


async def drive_after(release, threads: Iterable[str]) -> None:
    """Await a Turn's own ``release``, then drive the parents it woke.

    The parents are durable work, so they are driven even when the release
    raises; the drive logs its own failures and never masks the release's.
    """
    try:
        await release
    finally:
        await drive_threads(threads)


def deliver_deferred(value):
    """Deliver a deferred mutation nudge and restore its public result shape."""
    if not isinstance(value, DeferredMutation):
        return value
    deliver(value.messages)
    deliver(value.artifact_messages)
    from infra.send_broadcast import broadcast_input_rows

    for thread_id, rows in value.transcript_rows:
        try:
            broadcast_input_rows(thread_id, rows)
        except Exception as exc:  # noqa: BLE001 - committed input remains recoverable
            logger.warning(
                "Post-commit plan transcript delivery failed for thread %s: %s",
                thread_id,
                type(exc).__name__,
            )
    return value.result


#: Where the outermost plan write on this call stack reports the plans it touched,
#: when its caller delivers its own nudge (``collect_touched``).
_TOUCHED_SINK: ContextVar[list[str] | None] = ContextVar("plan_touched_sink", default=None)


@contextmanager
def collect_touched() -> Iterator[list[str]]:
    """Collect the plans the writes inside touched, for the caller's own nudge.

    A writer that already delivers ``plans_changed`` after its commit folds the
    collected ids into that one message, so a write is never nudged twice and
    a plan it touched but did not name (a dependent in another plan) is still
    named.
    """
    touched: list[str] = []
    token = _TOUCHED_SINK.set(touched)
    try:
        yield touched
    finally:
        _TOUCHED_SINK.reset(token)


def report_touched(plan_ids: Iterable[str], *, db) -> None:
    """Hand the plans a write touched to the caller's nudge, or schedule their own."""
    sink = _TOUCHED_SINK.get()
    if sink is not None:
        sink.extend(plan_ids)
    else:
        from plans.nudge_scheduling import schedule_after_commit

        schedule_after_commit(plan_ids, db=db)


def notify_after_commit(plan_ids: Iterable[str], *, db=None) -> bool:
    """Prepare and deliver a recoverable nudge after its writer has committed."""
    return deliver(prepare_after_commit(plan_ids, db=db))
