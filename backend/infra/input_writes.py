"""Provider-write fencing and settlement for claimed input batches."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Awaitable, Callable, TypeVar
from infra.provider_write_evidence import ProviderWriteEvidence

from core.checkpoint_types import CheckpointUnavailable
from core.stop_causes import PlanLimitStop
from infra.checkpoint_refusal_settlement import settle_checkpoint_refusal
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.input_sequencing import failed_delivery_error, sequence_attempt
from infra.queries import thread_unavailable_reason
from infra.send_types import SendRejected
from infra.thread_inputs import ClaimedBatch
from infra.vendor_login import without_login_sql

T = TypeVar("T")


def settle_account_refusal(db, rows, error) -> tuple[str, ...]:
    """Retain queued input/sequence and release only the proved-unwritten claim."""
    for row in rows:
        if row["delivery_state"] != "queued":
            raise RuntimeError("account refusal cannot hold an already settled input")
        db.execute(f"UPDATE messages SET metadata={without_login_sql()},active_turn_id=NULL,delivery_attempt_id=NULL,write_started_at=NULL,"
                   "write_outcome='proved_no_write',delivery_error=?,next_attempt_at=NULL,"
                   "delivery_revision=delivery_revision+1 WHERE id=?", (error.reason, row["id"]))
    return ()


def begin_batch_write(
    batch: ClaimedBatch,
    *,
    attempt_id: str,
    started_at: str,
    db: sqlite3.Connection | None = None,
) -> None:
    """Fence the exact claimed batch before provider dispatch."""
    if not attempt_id or not started_at:
        raise ValueError("a provider write requires attempt identity and time")
    with write_transaction(db, scope="begin_batch_write") as connection:
        thread_row = connection.execute(
            "SELECT lifecycle_state,archived_reason FROM threads WHERE id=?",
            (batch.thread_id,),
        ).fetchone()
        unavailable = thread_unavailable_reason(thread_row)
        if unavailable is not None:
            raise SendRejected(404 if thread_row is None else 409, unavailable)
        from connections.account_policy import require_claim
        from infra.vendor_turn_binding import model_vendor

        provider = model_vendor(batch.target_model, db=connection)
        if provider:
            require_claim(provider, thread_id=batch.thread_id, model=batch.target_model, turn_id=batch.turn_id, db=connection)
        for item in batch.items:
            updated = connection.execute(
                "UPDATE messages SET delivery_attempt_id=?,write_started_at=?,"
                "write_outcome=NULL,next_attempt_at=NULL,delivery_error=NULL "
                "WHERE id=? AND thread_id=? AND active_turn_id=? "
                "AND delivery_revision=? AND delivery_state='queued' "
                "AND (delivery_attempt_id IS NULL OR write_outcome='proved_no_write')",
                (
                    attempt_id,
                    started_at,
                    item.message_id,
                    batch.thread_id,
                    batch.turn_id,
                    item.revision,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"input write claim changed before dispatch: {item.message_id}"
                )


def lift_batch_fence(
    batch: ClaimedBatch,
    *,
    attempt_id: str,
    db: sqlite3.Connection | None = None,
) -> None:
    """Undo a committed begin that never reached dispatch, keeping the Turn's claim."""
    with write_transaction(db, scope="lift_batch_fence") as connection:
        lifted = connection.execute(
            "UPDATE messages SET delivery_attempt_id=NULL,write_started_at=NULL "
            "WHERE thread_id=? AND active_turn_id=? AND delivery_attempt_id=? "
            "AND delivery_state='queued' AND write_outcome IS NULL",
            (batch.thread_id, batch.turn_id, attempt_id),
        ).rowcount
        if lifted not in (0, len(batch.items)):
            raise RuntimeError(f"input write fence changed before lift: {attempt_id}")


async def fence_batch_write(
    batch: ClaimedBatch,
    *,
    attempt_id: str,
    started_at: str,
    db: sqlite3.Connection | None = None,
) -> None:
    """Begin a provider write; a cancelled begin leaves the pre-begin state."""
    try:
        await _write(begin_batch_write, batch, attempt_id=attempt_id, started_at=started_at, db=db)
    except asyncio.CancelledError:
        # run_in_db joins the begin before reporting cancellation, so it may
        # have committed. No provider bytes left: lift the fence so the Turn's
        # terminal settles these inputs exactly as if the cancel came first.
        await _write(lift_batch_fence, batch, attempt_id=attempt_id, db=db)
        raise


def release_batch_before_write(
    batch: ClaimedBatch,
    *,
    hold_error: str | None = None,
    db: sqlite3.Connection | None = None,
) -> None:
    """Release or hold an exact safe-boundary claim that never reached provider I/O."""
    updates, params = (
        (
            f"metadata={without_login_sql()},active_turn_id=NULL,write_outcome='proved_no_write',"
            "delivery_error=?,delivery_revision=delivery_revision+1",
            (hold_error,),
        )
        if hold_error is not None else (f"metadata={without_login_sql()},active_turn_id=NULL", ())
    )
    with write_transaction(db, scope="release_batch_before_write") as connection:
        for item in batch.items:
            updated = connection.execute(
                f"UPDATE messages SET {updates} WHERE id=? "
                "AND thread_id=? AND active_turn_id=? AND delivery_revision=? "
                "AND delivery_state='queued' AND write_started_at IS NULL",
                (*params, item.message_id, batch.thread_id, batch.turn_id, item.revision),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"input boundary claim changed before release: {item.message_id}"
                )


def release_batch_after_proved_no_write(
    batch: ClaimedBatch,
    *,
    attempt_id: str,
    error: str | None,
    db: sqlite3.Connection | None = None,
) -> None:
    """Return a fenced batch after the adapter proved zero provider bytes."""
    with write_transaction(
        db, scope="release_batch_after_proved_no_write",
    ) as connection:
        for item in batch.items:
            updated = connection.execute(
                f"UPDATE messages SET metadata={without_login_sql()},active_turn_id=NULL,delivery_attempt_id=NULL,"
                "write_started_at=NULL,write_outcome=NULL,delivery_error=? "
                "WHERE id=? AND thread_id=? AND active_turn_id=? "
                "AND delivery_revision=? AND delivery_state='queued' "
                "AND delivery_attempt_id=? AND write_outcome IS NULL",
                (
                    error,
                    item.message_id,
                    batch.thread_id,
                    batch.turn_id,
                    item.revision,
                    attempt_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"input write proof changed before release: {item.message_id}"
                )


def settle_batch_write(
    attempt_id: str,
    *,
    outcome: str,
    error: str | BaseException | None = None,
    no_write_state: str = "failed",
    db: sqlite3.Connection | None = None,
) -> tuple[str, ...]:
    """Settle one fenced batch without inferring whether bytes were written.

    Returns the ids it ended ``failed``, for the caller to drive the report owner after commit.
    """
    if outcome not in {"accepted", "proved_no_write", "ambiguous"}:
        raise ValueError(f"invalid provider write outcome: {outcome}")
    if no_write_state not in {"failed", "cancelled"}:
        raise ValueError(f"invalid proved no-write state: {no_write_state}")
    if outcome != "proved_no_write" and no_write_state != "failed":
        raise ValueError("only a proved no-write may select its delivery state")
    with write_transaction(db, scope="settle_batch_write") as connection:
        rows = connection.execute(
            "SELECT id,thread_id,active_turn_id,delivery_state,write_outcome,"
            "next_attempt_at,seq,target_model FROM messages "
            "WHERE delivery_attempt_id=? "
            "ORDER BY input_order",
            (attempt_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"provider write attempt does not exist: {attempt_id}")
        settled = [row for row in rows if row["write_outcome"] is not None]
        if settled:
            if len(settled) == len(rows) and all(
                str(row["write_outcome"]) == outcome for row in rows
            ):
                return ()
            raise RuntimeError(f"provider write attempt settled inconsistently: {attempt_id}")
        if outcome == "accepted":
            return sequence_attempt(
                connection, rows, state="delivered", outcome=outcome, error=None,
            )
        if outcome == "ambiguous":
            return sequence_attempt(
                connection, rows, state="ambiguous", outcome=outcome,
                error=str(error) if error is not None else None,
            )
        from connections.account_policy import AccountAdmissionError

        if no_write_state == "failed" and isinstance(error, AccountAdmissionError):
            return settle_account_refusal(connection, rows, error)
        if no_write_state == "failed" and isinstance(error, CheckpointUnavailable):
            return settle_checkpoint_refusal(connection, rows, error)
        from infra.live_writer_hold import hold_for_live_writer
        from subagents.codex_runtime_errors import is_live_writer_refusal

        if no_write_state == "failed" and is_live_writer_refusal(error):
            held = hold_for_live_writer(connection, rows)
            if held is not None:
                return held
        return sequence_attempt(
            connection, rows, state=no_write_state, outcome=outcome,
            error=failed_delivery_error(connection, rows, error),
        )


def release_checkpoint_inputs(
    db: sqlite3.Connection, model_id: str, *, thread_id: str | None = None,
) -> tuple[str, ...]:
    """Release current-source holds inside the actual cap/install transaction."""
    from llm.resolution import _target_row

    if not db.in_transaction:
        raise RuntimeError("checkpoint input release requires its change transaction")
    scope = (
        "delivery_state='queued' "
        "AND write_outcome='proved_no_write' AND active_turn_id IS NULL "
        "AND write_started_at IS NULL "
        + ("" if thread_id is None else "AND thread_id=? ")
        + "AND EXISTS ("
        "SELECT 1 FROM threads t WHERE t.id=messages.thread_id "
        "AND t.model=messages.target_model "
        "AND t.execution_epoch=messages.target_execution_epoch)"
    )
    params: tuple[str, ...] = () if thread_id is None else (thread_id,)
    targets = db.execute(
        "SELECT DISTINCT target_model FROM messages WHERE " + scope,
        params,
    ).fetchall()
    released: set[str] = set()
    for row in targets:
        target_model = str(row["target_model"])
        if target_model != model_id:
            target = _target_row(target_model, db=db)
            if target is None or target["id"] != model_id:
                continue
        released.update(str(item[0]) for item in db.execute(
            "UPDATE messages SET write_outcome=NULL,delivery_attempt_id=NULL,"
            "delivery_revision=delivery_revision+1,delivery_error=NULL "
            "WHERE target_model=? AND " + scope + " RETURNING thread_id",
            (target_model, *params),
        ))
    return tuple(sorted(released))


def settle_unwritten_turn_inputs(
    db: sqlite3.Connection,
    turn_id: str,
    *,
    outcome: str,
    error: str | BaseException | None,
) -> tuple[str, ...]:
    """Release or visibly end inputs whose Turn terminalized before dispatch.

    Returns the ids it ended ``failed``, for the caller to drive the report owner after commit.
    """
    rows = db.execute(
        "SELECT id,thread_id,delivery_state,write_started_at,write_outcome,target_model FROM messages "
        "WHERE active_turn_id=? AND delivery_state='queued' ORDER BY input_order",
        (turn_id,),
    ).fetchall()
    unresolved = [
        row for row in rows
        if row["write_started_at"] is not None and row["write_outcome"] is None
    ]
    if unresolved:
        raise RuntimeError(f"turn {turn_id} still owns an unresolved provider write")
    if not rows:
        return ()
    from connections.account_policy import AccountAdmissionError

    if outcome == "error" and isinstance(error, AccountAdmissionError):
        return settle_account_refusal(db, rows, error)
    if outcome == "error" and isinstance(error, CheckpointUnavailable):
        return settle_checkpoint_refusal(db, rows, error)
    # A plan-limit Stop returns its inputs to the queue at their original seq.
    if outcome == "completed" or (outcome == "cancelled" and isinstance(error, PlanLimitStop)):
        db.execute(
            f"UPDATE messages SET metadata={without_login_sql()},active_turn_id=NULL WHERE active_turn_id=? "
            "AND delivery_state='queued'",
            (turn_id,),
        )
        return ()
    state = "cancelled" if outcome == "cancelled" else "failed"
    return sequence_attempt(
        db,
        rows,
        state=state,
        outcome="proved_no_write",
        error=failed_delivery_error(db, rows, error),
    )


async def _write(action, *args, db: sqlite3.Connection | None, **kwargs):
    if db is not None:
        return action(*args, db=db, **kwargs)
    return await run_in_db(action, *args, **kwargs)


async def _settle_evidence(
    attempt_id: str,
    evidence: ProviderWriteEvidence,
    error: BaseException | None,
    *,
    db: sqlite3.Connection | None,
) -> None:
    if evidence.accepted:
        outcome = "accepted"
    elif evidence.ambiguous:
        outcome = "ambiguous"
    else:
        outcome = "proved_no_write"
    failed = await _write(
        settle_batch_write,
        attempt_id,
        outcome=outcome,
        error=error,
        db=db,
    )
    if outcome == "proved_no_write" and not evidence.proved_no_write:
        evidence.mark_proved_no_write()
    if db is None:  # a caller-owned transaction drives after its own commit
        from infra.failed_wake_surface import drive_failed_wake_owners

        await drive_failed_wake_owners(failed)


async def dispatch_fenced_batch(
    batch: ClaimedBatch,
    dispatcher: Callable[[ProviderWriteEvidence], Awaitable[T]],
    *,
    attempt_id: str,
    started_at: str,
    evidence: ProviderWriteEvidence | None = None,
    db: sqlite3.Connection | None = None,
) -> T:
    """Fence, dispatch outside storage, and settle only adapter evidence."""
    await fence_batch_write(batch, attempt_id=attempt_id, started_at=started_at, db=db)
    evidence = evidence or ProviderWriteEvidence()
    try:
        result = await dispatcher(evidence)
    except BaseException as error:
        if evidence.turn_owned:
            raise
        await _settle_evidence(
            attempt_id, evidence, error, db=db,
        )
        raise
    if evidence.turn_owned:
        return result
    if not evidence.accepted and not evidence.ambiguous:
        error = RuntimeError("provider adapter returned with no provider-write evidence")
        await _settle_evidence(
            attempt_id, evidence, error, db=db,
        )
        raise error
    await _settle_evidence(
        attempt_id, evidence, None, db=db,
    )
    return result
