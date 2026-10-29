"""The sole durable transition owner for one thread's Turn and input FIFO."""

from __future__ import annotations

import logging
import sqlite3

from core.stop_causes import PlanLimitStop
from infra.database import write_transaction
from infra.plan_limit_continue import queue_limit_refusal_continue, queue_plan_limit_continue
from infra.completed_tool_results import (
    persist_completed_tool_results,
    persist_completed_tool_results_in_transaction as _persist_completed_tool_results,
)
from infra.idle_checkpoints import IdleCheckpointDue, idle_marker_due
from infra.document_viewing import PreparedViewingDocument
from infra.input_admission import accept_thread_input, stamped_human_metadata
from infra.input_writes import settle_unwritten_turn_inputs
from infra.retained_usage import UsageSettlement, usage_settlement
from infra.thread_driver_types import (
    InputAdmission,
    StartupRecovery,
    TerminalResult,
    TerminalSettlement,
)
from infra.thread_input_types import ClaimedBatch, HeldAdmission, TurnAdmission
from infra.terminal_usage_limits import Recorded, TerminalLimits, Unrecorded, settle_terminal_limits
from infra.thread_inputs import fail_claimable_prefix, is_fenced, start_idle_turn
from infra.turn_usage_receipts import project_thread_usage_before_turn_deletion

log = logging.getLogger(__name__)


def delete_thread_turns(db: sqlite3.Connection, thread_id: str) -> None:
    """Write held usage, then delete the Turn audit of a thread being deleted."""
    UsageSettlement(owns_commit=False).settle(db, thread_id)
    project_thread_usage_before_turn_deletion(db, thread_id)
    db.execute("DELETE FROM active_turns WHERE thread_id=?", (thread_id,))


def admit_input(
    thread_id: str,
    *,
    role: str,
    content: str,
    intent: str,
    message_id: str,
    turn_id: str,
    boot_epoch: str,
    admitted_at: str,
    metadata: dict | None = None,
    viewing_document: PreparedViewingDocument | None = None,
    delivery_options: dict | None = None,
    continuation: dict | None = None,
    materialize_passive_digest: bool = True,
    db: sqlite3.Connection | None = None,
) -> InputAdmission:
    """Accept one FIFO input and atomically choose the idle Turn winner."""
    with write_transaction(db, scope="admit_input") as connection:
        from automation.self_restart import restart_holds_new_work
        from infra.passive_changes import materialize_pending_digest
        passive = materialize_pending_digest(connection, thread_id) if materialize_passive_digest else None
        accepted = accept_thread_input(
            thread_id,
            role=role,
            content=content,
            intent=intent,
            message_id=message_id,
            metadata=stamped_human_metadata(
                connection, role, message_id, metadata,
                viewing_document=viewing_document, thread_id=thread_id,
            ),
            delivery_options=delivery_options,
            continuation=continuation,
            created_at=admitted_at,
            db=connection,
        )
        held_for_restart = restart_holds_new_work(db=connection)
        batch = (
            None
            if accepted.duplicate or held_for_restart
            else start_idle_turn(
                thread_id,
                turn_id=turn_id,
                boot_epoch=boot_epoch,
                started_at=admitted_at,
                db=connection,
            )
        )
    return InputAdmission(accepted, batch, held_for_restart, passive)


#: The line `_parent_completion` puts above a worker's answer.
PARENT_COMPLETION_HEADING = "Subagent finished:"


def _parent_completion(
    db: sqlite3.Connection,
    turn_id: str,
    child_id: str,
) -> str | None:
    parent = db.execute(
        "SELECT spawned_by FROM threads WHERE id=?",
        (child_id,),
    ).fetchone()[0]
    if not parent or is_fenced(str(parent)):
        return None
    from events.worker_starts import started_by

    if started_by(db, child_id) is not None:
        return None  # a rule-started worker answers its owner only as a worker.* event
    from plans.public import has_open_structured_attachment

    if has_open_structured_attachment(child_id, db=db):
        return None
    existing = db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND json_valid(metadata) "
        "AND json_extract(metadata,'$.completion')=1 "
        "AND json_extract(metadata,'$.child_turn_id')=? LIMIT 1",
        (str(parent), turn_id),
    ).fetchone()
    if existing is not None:
        return None
    from infra.thread_confinement import ChildOutput

    answer = ChildOutput.read(db, child_id).final_reply(db, turn_id=turn_id)
    # The wrapper carries the child's result. A Turn that produced none has
    # nothing to deliver, and waking the parent for it is a model call per
    # blank Turn — a legitimately unchanged wait would never be quiet.
    if answer is None:
        return None
    content = f"{PARENT_COMPLETION_HEADING}\n\n{answer['content']}"
    from infra.passive_changes import materialize_pending_digest

    materialize_pending_digest(db, str(parent))
    accept_thread_input(
        str(parent),
        role="user",
        content=content,
        intent="apply_now",
        message_id=f"parent-completion:{turn_id}",
        metadata={
            "sender_kind": "agent",
            "from_thread": child_id,
            "completion": True,
            "child_turn_id": turn_id,
            "terminal_status": "completed",
        },
        db=db,
    )
    return str(parent)


def _terminal_duplicate(
    row: sqlite3.Row | None,
    outcome: str,
) -> TerminalSettlement | None:
    if row is None:
        return TerminalSettlement(TerminalResult.CONFLICTING_TERMINAL, None)
    current = str(row["status"])
    if current == "running":
        return None
    result = (
        TerminalResult.ALREADY_SAME_OUTCOME
        if current == outcome
        else TerminalResult.CONFLICTING_TERMINAL
    )
    return TerminalSettlement(result, str(row["thread_id"]))


def _end_turn_previews(turn_id: str, outcome: str) -> None:
    """The backstop for every harness: no streamed edit preview outlives its Turn."""
    from doceditor.live_preview import turn_ended

    turn_ended(turn_id, {"completed": "turn_ended", "cancelled": "stopped"}.get(outcome, "failed"))


def finish_turn(
    turn_id: str,
    *,
    outcome: str,
    ended_at: str,
    successor_turn_id: str,
    boot_epoch: str,
    error: str | BaseException | None = None,
    db: sqlite3.Connection | None = None,
    mark_idle_checkpoint: bool = True,
    hold_expired_limit_at_startup: bool = False,
) -> TerminalSettlement:
    """Settle one exact Turn and claim its successor in the same transaction.

    ``mark_idle_checkpoint=False`` is for a caller that discards this
    settlement without driving it: no timer would ever arm the marker, so it
    is never written rather than written and later found dead.
    """
    if outcome not in {"completed", "cancelled", "error"}:
        raise ValueError(f"invalid Turn terminal outcome: {outcome}")
    with usage_settlement(db) as usage, write_transaction(db, scope="finish_turn") as connection:
        row = connection.execute(
            "SELECT thread_id,status FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()
        duplicate = _terminal_duplicate(row, outcome)
        if duplicate is not None:
            return duplicate
        thread_id = str(row["thread_id"])
        limits = _persist_before_terminal(
            connection, usage, thread_id, turn_id, outcome, ended_at, error,
        )
        error = limits.error
        if isinstance(limits.limit, Unrecorded):
            outcome = "error"
        updated = connection.execute(
            "UPDATE active_turns SET status=?,ended_at=?,error=?,retry_at=NULL "
            "WHERE id=? AND status='running'",
            (outcome, ended_at, str(error) if error is not None else None, turn_id),
        )
        if updated.rowcount != 1:
            return TerminalSettlement(
                TerminalResult.CONFLICTING_TERMINAL, thread_id,
            )
        from plans.public import settle_turn_dispatches as settle_plan_dispatches

        unknown_dispatches = settle_plan_dispatches(turn_id, db=connection)
        failed_message_ids = settle_unwritten_turn_inputs(
            connection, turn_id, outcome=outcome, error=error,
        )
        parent = _parent_completion(connection, turn_id, thread_id) if outcome == "completed" else None
        if outcome == "cancelled" and isinstance(error, PlanLimitStop):
            queue_plan_limit_continue(connection, turn_id, thread_id)
        elif outcome == "error" and isinstance(limits.limit, Recorded):
            queue_limit_refusal_continue(connection, turn_id, thread_id)
        successor, switch_transition, switch_request, idle = _claim_after_terminal(
            connection,
            thread_id=thread_id,
            turn_id=turn_id,
            outcome=outcome,
            ended_at=ended_at,
            successor_turn_id=successor_turn_id,
            boot_epoch=boot_epoch,
            mark_idle_checkpoint=mark_idle_checkpoint,
            hold_expired_limit_at_startup=hold_expired_limit_at_startup,
            fail_closed=limits.fail_closed,
        )
    _end_turn_previews(turn_id, outcome)
    wakes = () if parent is None else (parent,)
    return TerminalSettlement(
        TerminalResult.APPLIED,
        thread_id,
        successor,
        wakes,
        switch_transition,
        switch_request,
        unknown_dispatches,
        idle,
        failed_message_ids,
        "limited" if isinstance(limits.limit, Recorded) else outcome,
    )


def _claim_after_terminal(
    connection: sqlite3.Connection,
    *,
    thread_id: str,
    turn_id: str,
    outcome: str,
    ended_at: str,
    successor_turn_id: str,
    boot_epoch: str,
    mark_idle_checkpoint: bool,
    hold_expired_limit_at_startup: bool,
    fail_closed: str | None = None,
):
    """Claim switch/successor work and derive its idle-marker consequence.

    ``fail_closed`` means admission cannot prove the account usable: the prefix
    a successor would claim is failed visibly instead (Amendment 16 C1, C2).
    """

    from automation.restart_resumption import idle_threads_with_held_input
    from automation.self_restart import restart_holds_new_work
    from infra.model_switches import activate_ready_switch, resolvable_switch

    switch_transition = activate_ready_switch(
        connection, thread_id, committed_at=ended_at,
    )
    switch_request = (
        None if switch_transition is not None else resolvable_switch(connection, thread_id)
    )
    held_input = False
    if fail_closed is not None:
        successor = fail_claimable_prefix(connection, thread_id, error=fail_closed)
    elif restart_holds_new_work(db=connection):
        # A restart hold admits no new work: inputs stay queued until the hold
        # releases, then startup or the abort wake claims them in FIFO order.
        successor = None
        held_input = bool(idle_threads_with_held_input(connection, thread_id))
    else:
        successor = start_idle_turn(
            thread_id,
            turn_id=successor_turn_id,
            boot_epoch=boot_epoch,
            started_at=ended_at,
            db=connection,
            hold_expired_limit_at_startup=hold_expired_limit_at_startup,
        )
    idle = _write_idle_marker(connection, idle_marker_due(
        connection, turn_id, outcome=outcome,
        successor_claimed=isinstance(successor, ClaimedBatch),
        switch_pending=switch_transition is not None or switch_request is not None,
        mark_idle_checkpoint=mark_idle_checkpoint,
        successor_held=held_input or isinstance(successor, HeldAdmission),
    ))
    return successor, switch_transition, switch_request, idle


def _persist_before_terminal(
    connection: sqlite3.Connection,
    usage: UsageSettlement,
    thread_id: str,
    turn_id: str,
    outcome: str,
    ended_at: str,
    error,
) -> TerminalLimits:
    """Persist terminal usage and settle account limits before successor admission."""

    _persist_completed_tool_results(connection, thread_id, turn_id, error)
    usage.settle(connection, thread_id)
    return settle_terminal_limits(
        connection, thread_id, turn_id, outcome=outcome, ended_at=ended_at, error=error,
    )


def _write_idle_marker(
    connection: sqlite3.Connection, due: IdleCheckpointDue | None,
) -> IdleCheckpointDue | None:
    """Set the due Turn's idle marker in the settlement transaction."""
    if due is not None:
        connection.execute("UPDATE active_turns SET idle_checkpoint_pending=1 WHERE id=?", (due.turn_id,))
    return due


def consume_idle_checkpoint_marker(connection: sqlite3.Connection, turn_id: str) -> bool:
    """Atomically claim and clear one pending marker; False means lost the race."""
    updated = connection.execute(
        "UPDATE active_turns SET idle_checkpoint_pending=0 "
        "WHERE id=? AND status='completed' AND idle_checkpoint_pending=1",
        (turn_id,),
    )
    return updated.rowcount == 1


def retire_legacy_waiting_retry_turns(conn: sqlite3.Connection) -> None:
    """Migration-only: return legacy waiting-retry rows to dead-boot recovery."""

    legacy_turn_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT id FROM active_turns WHERE status='waiting_retry'"
        ).fetchall()
    ]
    for turn_id in legacy_turn_ids:
        conn.execute(
            "UPDATE messages SET delivery_attempt_id=NULL,write_started_at=NULL,"
            "write_outcome=NULL,next_attempt_at=NULL WHERE active_turn_id=? "
            "AND delivery_state='queued' AND write_outcome='proved_no_write'",
            (turn_id,),
        )
    conn.execute(
        "UPDATE active_turns SET status='running',retry_at=NULL "
        "WHERE status='waiting_retry'"
    )
