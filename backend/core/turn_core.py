"""Harness-facing API for the one durable Turn owner."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from typing import Callable

from infra.database import get_db, is_lock_refusal, write_transaction
from infra.db_async import run_in_db
from infra.lock_retry import LockRefused, retry_until_committed
from infra.thread_driver import TerminalResult, TerminalSettlement, finish_turn
from infra.thread_inputs import TurnIdentity
from infra.time import utc_now_iso

from core.thread_identity import visible_root_or_none

BOOT_EPOCH = str(uuid.uuid4())
TurnTerminalResult = TerminalResult


class TurnAlreadyActiveError(RuntimeError):
    """Another exact Turn already owns this thread."""


class TurnThreadMissingError(LookupError):
    """The requested Turn has no thread."""


class TurnNotPreadmittedError(RuntimeError):
    """A harness tried to invent a Turn outside input admission."""


class TurnTerminalOwnershipError(RuntimeError):
    """A harness lost the exact Turn terminal compare-and-set."""


def terminal_error_detail(error: BaseException) -> str | BaseException:
    """Keep typed no-write facts until FIFO settlement; format ordinary errors."""
    from connections.account_policy import AccountAdmissionError
    from core.checkpoint_types import CheckpointUnavailable
    from tools.recorded_call import CompletedToolFailure

    return error if isinstance(error, (AccountAdmissionError, CheckpointUnavailable, CompletedToolFailure)) else str(error)


def persist_turn_start(
    turn_id: str,
    thread_id: str,
    model: str,
    content_preview: str,
    *,
    db: sqlite3.Connection | None = None,
) -> TurnIdentity:
    """Bind a harness only to the Turn already admitted with its input batch."""
    del content_preview
    with write_transaction(db, scope="persist_turn_start") as connection:
        if connection.execute(
            "SELECT 1 FROM threads WHERE id=?", (thread_id,),
        ).fetchone() is None:
            raise TurnThreadMissingError(f"turn thread does not exist: {thread_id}")
        row = _open_turn_row(connection, thread_id)
        if row is None:
            raise TurnNotPreadmittedError(
                f"Turn {turn_id} has no atomically admitted input batch"
            )
        if str(row["id"]) != turn_id:
            raise TurnAlreadyActiveError(f"turn already active on session {thread_id}")
        if str(row["model"]) != model:
            raise TurnNotPreadmittedError(
                f"Turn {turn_id} was admitted for {row['model']}, not {model}"
            )
        return TurnIdentity(turn_id, int(row["turn_order"]))


def _open_turn_row(connection: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id,turn_order,model FROM active_turns WHERE thread_id=? "
        "AND status='running'",
        (thread_id,),
    ).fetchone()


def _require_open_turn(thread_id: str, identity: TurnIdentity) -> None:
    connection = get_db()
    try:
        row = _open_turn_row(connection, thread_id)
    finally:
        connection.close()
    if row is None or str(row["id"]) != identity.turn_id:
        raise TurnNotPreadmittedError(
            f"Turn {identity.turn_id} ended before its runtime bound it"
        )


async def bind_preadmitted_turn(
    thread_id: str,
    identity: TurnIdentity,
    bind: Callable[[TurnIdentity], None],
    unbind: Callable[[TurnIdentity], None],
) -> None:
    """Bind a runtime to its admitted Turn, then prove the Turn is still open.

    Binding first means a Stop that ends the Turn either commits before the
    check, which unbinds and raises here, or after it, when its retirement
    pass finds this bound owner.
    """
    bind(identity)
    try:
        await run_in_db(_require_open_turn, thread_id, identity)
    except BaseException:
        unbind(identity)
        raise


async def persist_turn_start_bound(
    turn_id: str,
    thread_id: str,
    model: str,
    content_preview: str,
    bind: Callable[[TurnIdentity], None],
    unbind: Callable[[TurnIdentity], None] | None = None,
    completion: asyncio.Future[bool] | None = None,
) -> TurnIdentity:
    """Bind the preadmitted Turn even when cancellation races the DB call."""
    task = asyncio.create_task(run_in_db(
        persist_turn_start, turn_id, thread_id, model, content_preview,
    ))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    identity = task.result()
    bind(identity)
    if not cancelled:
        return identity
    settlement = await commit_turn_terminal(turn_id, "cancelled")
    if unbind is not None:
        unbind(identity)
    result = await drive_turn_settlement(settlement)
    if result is not TerminalResult.APPLIED:
        error = TurnTerminalOwnershipError(
            f"cancelled start {turn_id} lost terminal ownership: {result.value}"
        )
        if completion is not None and not completion.done():
            completion.set_exception(error)
        raise error
    if completion is not None and not completion.done():
        completion.set_result(True)
    raise asyncio.CancelledError


def persist_turn_terminal(
    turn_id: str,
    outcome: str,
    error: str | BaseException | None = None,
    *,
    ended_at: str | None = None,
    db: sqlite3.Connection | None = None,
) -> TerminalResult:
    """Commit one exact Turn terminal without driving its runnable follow-on work.

    This settlement is discarded down to its bare result and never driven
    through ``drive_terminal_settlement``, so any idle checkpoint marker it
    might otherwise write would never be armed; marking is skipped instead of
    writing a marker nothing will ever act on.
    """
    settlement = persist_turn_settlement(
        turn_id, outcome, error, ended_at=ended_at, db=db, mark_idle_checkpoint=False,
    )
    return settlement.result


def persist_turn_settlement(
    turn_id: str,
    outcome: str,
    error: str | BaseException | None = None,
    *,
    ended_at: str | None = None,
    db: sqlite3.Connection | None = None,
    mark_idle_checkpoint: bool = True,
) -> TerminalSettlement:
    """Commit one terminal and return the exact work made runnable by it."""
    return finish_turn(
        turn_id,
        outcome=outcome,
        ended_at=ended_at or utc_now_iso(),
        successor_turn_id=str(uuid.uuid4()),
        boot_epoch=BOOT_EPOCH,
        error=error,
        db=db,
        mark_idle_checkpoint=mark_idle_checkpoint,
    )


def quarantine_unsettled_writes(turn_id: str, *, error: str) -> None:
    """Record a Turn's fenced provider writes with no settled outcome as ambiguous."""
    from infra.thread_recovery import ambiguous_stale_writes

    with write_transaction(None, scope="quarantine_unsettled_writes") as connection:
        ambiguous_stale_writes(connection, turn_id, error=error)


def _commit_terminal(
    turn_id: str,
    outcome: str,
    error: str | BaseException | None,
    ended_at: str | None,
    quarantine: str | None,
) -> TerminalSettlement | LockRefused:
    """One attempt. A lock refusal means the terminal committed nothing
    (``finish_turn`` contains its post-commit failures); quarantine is idempotent.
    """
    try:
        if quarantine is not None:
            quarantine_unsettled_writes(turn_id, error=quarantine)
        return persist_turn_settlement(turn_id, outcome, error, ended_at=ended_at)
    except Exception as refused:
        if is_lock_refusal(refused):
            return LockRefused(refused)
        raise


async def commit_turn_terminal(
    turn_id: str,
    outcome: str,
    error: str | BaseException | None = None,
    *,
    ended_at: str | None = None,
    quarantine: str | None = None,
) -> TerminalSettlement:
    """Commit terminal state without starting work before its owner releases.

    ``quarantine`` first records the Turn's unsettled provider writes ambiguous
    (``finish_turn`` refuses a Turn that owns one). Lock refusals are retried.
    """
    ended_at = ended_at or utc_now_iso()
    return await retry_until_committed(
        lambda: run_in_db(_commit_terminal, turn_id, outcome, error, ended_at, quarantine),
        subject=f"turn {turn_id}",
    )


async def drive_turn_settlement(
    settlement: TerminalSettlement,
) -> TerminalResult:
    """Drive every action returned by one already-committed terminal."""
    from infra.thread_execution import drive_terminal_settlement

    await drive_terminal_settlement(settlement)
    return settlement.result


async def settle_turn_terminal(
    turn_id: str, outcome: str, error: str | BaseException | None = None,
    *, ended_at: str | None = None, quarantine: str | None = None,
) -> TerminalResult:
    """Commit one terminal and synchronously drive all work it exposes."""
    settlement = await commit_turn_terminal(
        turn_id, outcome, error, ended_at=ended_at, quarantine=quarantine,
    )
    return await drive_turn_settlement(settlement)


async def settle_owned_turn_terminal(
    turn_id: str,
    outcome: str,
    error: str | BaseException | None = None,
    *,
    release: Callable[[], None] | None = None,
) -> None:
    """Settle one preadmitted turn and release its runtime before its successor."""
    settlement = await commit_turn_terminal(turn_id, outcome, error)
    if settlement.result is not TerminalResult.APPLIED:
        raise TurnTerminalOwnershipError(
            f"terminal owner rejected admitted turn {turn_id}: {settlement.result.value}"
        )
    if release is not None:
        release()
    await drive_turn_settlement(settlement)


def _active_rows(db: sqlite3.Connection, clause: str = "", params=()):
    return db.execute(
        "SELECT t.id AS turn_id,t.turn_order,t.thread_id,t.started_at,s.* "
        "FROM active_turns AS t JOIN threads AS s ON s.id=t.thread_id "
        "WHERE t.status='running' "
        f"{clause} ORDER BY t.turn_order",
        params,
    ).fetchall()


def active_turn_count(*, db: sqlite3.Connection | None = None) -> int:
    """Return the exact number of nonterminal Turns."""
    owns = db is None
    connection = db or get_db()
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM active_turns WHERE status='running'"
        ).fetchone()[0])
    finally:
        if owns:
            connection.close()


def active_turn_blockers(
    *, db: sqlite3.Connection | None = None, user_id: str | None = None,
) -> list[dict[str, str | int]]:
    """Return exact durable Turns that block a service drain.

    ``user_id`` scopes the result to that user's own threads. Every blocker
    exposes another user's thread title and id, so callers that fan the
    result out to more than one authenticated user (broadcast, or any HTTP
    caller) must always pass their own ``user_id``; omitting it is only safe
    for internal, single-operator bookkeeping.
    """
    from infra.execution_class import execution_class
    from llm.resolution import ModelUnavailableError

    owns = db is None
    connection = db or get_db()
    clause = "AND s.user_id = ?" if user_id is not None else ""
    params = (user_id,) if user_id is not None else ()
    try:
        blockers = []
        for row in _active_rows(connection, clause, params):
            try:
                klass = execution_class(row)
            except ModelUnavailableError:
                klass = "unresolved"
            blockers.append({
                "threadId": str(row["thread_id"]),
                "turnId": str(row["turn_id"]),
                "turnOrder": int(row["turn_order"]),
                "executionClass": klass,
                "title": str(row["title"] or "Untitled thread"),
                "startedAt": str(row["started_at"]),
            })
        return blockers
    finally:
        if owns:
            connection.close()


def active_turn_thread_ids(
    *, db: sqlite3.Connection | None = None,
) -> set[str]:
    """Return thread ids with a nonterminal Turn."""
    owns = db is None
    connection = db or get_db()
    try:
        return {str(row["thread_id"]) for row in _active_rows(connection)}
    finally:
        if owns:
            connection.close()


def active_turns_for_owner(
    user_id: str,
    *,
    thread_id: str | None = None,
    db: sqlite3.Connection | None = None,
) -> list[dict[str, str | int | None]]:
    """Return the owner's active authoritative Turn identities.

    Each entry also names ``visible_root_thread_id``: the topmost visible
    (``spawned_by IS NULL``) ancestor of the turn's thread, so a client that
    never learns the subagent tree can still attribute a hidden subagent's
    running Turn to the one visible row that owns its lineage. ``None`` means
    the lineage is unresolvable, not that the thread is its own root.
    ``started_at`` is the Turn's durable admission time, the one source a
    client times a running Turn from.
    """
    owns = db is None
    connection = db or get_db()
    try:
        clause = "AND s.user_id=?"
        params: list[str] = [user_id]
        if thread_id is not None:
            clause += " AND t.thread_id=?"
            params.append(thread_id)
        return [
            {
                "thread_id": str(row["thread_id"]),
                "turn_id": str(row["turn_id"]),
                "turn_order": int(row["turn_order"]),
                "visible_root_thread_id": visible_root_or_none(
                    str(row["thread_id"]), db=connection
                ),
                "started_at": str(row["started_at"]),
            }
            for row in _active_rows(connection, clause, params)
        ]
    finally:
        if owns:
            connection.close()
