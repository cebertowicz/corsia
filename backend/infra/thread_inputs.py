"""Durable model-bound inputs in one per-thread FIFO."""

from __future__ import annotations

import json
import logging
import sqlite3

from core.checkpoint_types import IdleCheckpointReason
from connections.account_receipts import ReceiptFence, subject_matches_thread
from infra.admission_fence import BEING_DELETED, is_fenced
from infra.database import write_transaction
from infra.held_admission import connection_limit_gate
from infra.idle_checkpoints import cancel_timer_and_record
from infra.queries import thread_unavailable_reason
from infra.queued_input_settlement import still_claimable
from infra.send_types import SendRejected
from infra.thread_input_rows import starts_turn
from infra.vendor_turn_binding import admitted_model_state, turn_account_id
from infra.thread_input_types import (
    Admission,
    ClaimedBatch,
    ClaimedInput,
    FailedInputBatch,
    TurnAdmission,
    TurnIdentity,
    safe_input_prefixes,
)
from infra.write_effects import after_commit
from infra.input_turn_assignment import assign_turn as _assign_turn
from infra.vendor_login import public_metadata

log = logging.getLogger(__name__)


class _RolloverHeld(Exception):
    """Rollback the tentative move without discarding prior admission settlement."""


def _batch(
    thread_id: str,
    turn_id: str,
    turn_order: int,
    epoch: int,
    model: str,
    rows: list[sqlite3.Row],
    stamps: dict | None = None, receipt: ReceiptFence | None = None, recovered: tuple[str, ...] = (), load_for: tuple[str, ...] = (),
) -> ClaimedBatch:
    return ClaimedBatch(
        thread_id=thread_id,
        turn_id=turn_id,
        turn_order=turn_order,
        target_execution_epoch=epoch,
        target_model=model,
        items=tuple(_claimed_item(row, stamps or {}) for row in rows),
        vendor_receipt=receipt,
        recovered_message_ids=recovered, load_for=load_for,
    )


def thread_admission_state(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    allow_unavailable: bool = False,
    admit_while_fenced: bool = False,
) -> sqlite3.Row | None:
    """Return the row a thread is admitted against, or None where the caller tolerates none.

    ``allow_unavailable`` covers an archived, purged, missing or being-deleted
    (``infra.admission_fence``) thread, a state a caller may legitimately skip
    instead of failing on; it has to be asked for by name, so every rejection
    nobody asked to tolerate stays loud. ``admit_while_fenced`` only lets a
    deletion-owned terminal event queue behind that fence; all other unavailable
    states still reject. Model-less threads hold input until a model is chosen.
    """
    row = db.execute(
        "SELECT model,execution_epoch,next_input_order,pending_model,"
        "pending_execution_epoch,switch_after_input_order,switch_status,"
        "lifecycle_state,archived_reason "
        "FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    fenced = is_fenced(thread_id) and not admit_while_fenced
    unavailable = thread_unavailable_reason(row) or (BEING_DELETED if fenced else None)
    if unavailable is not None:
        if allow_unavailable:
            return None
        raise SendRejected(404 if row is None else 409, unavailable)
    return row


def _queued_epoch_prefix(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    expected_epoch: int | None = None,
    expected_model: str | None = None,
) -> list[sqlite3.Row]:
    rows = db.execute(
        "SELECT id,input_order,delivery_revision,role,content,metadata,"
        "delivery_options,created_at,continuation_condition,"
        "delivery_intent,write_outcome,delivery_error,"
        "target_execution_epoch,target_model FROM messages "
        "WHERE thread_id=? AND input_order IS NOT NULL "
        "AND delivery_state='queued' AND active_turn_id IS NULL "
        "ORDER BY input_order",
        (thread_id,),
    ).fetchall()
    if not rows:
        return []
    epoch = expected_epoch
    model = expected_model
    if epoch is None:
        epoch = int(rows[0]["target_execution_epoch"])
    if model is None:
        model = str(rows[0]["target_model"])
    prefix: list[sqlite3.Row] = []
    for row in rows:
        if (
            int(row["target_execution_epoch"]) != epoch
            or str(row["target_model"]) != model
        ):
            break
        prefix.append(row)
    return prefix


def _deliverable_prefix(
    rows: list[sqlite3.Row], lifted: frozenset[str] = frozenset(),
) -> list[sqlite3.Row]:
    """A known unwritten hold stops delivery unless its cause has lifted."""
    for index, row in enumerate(rows):
        if row["write_outcome"] == "proved_no_write" and str(row["id"]) not in lifted:
            return rows[:index]
    return rows


def _claimed_item(row: sqlite3.Row, stamps: dict) -> ClaimedInput:
    raw_metadata = json.loads(row["metadata"] or "{}")
    delivery_options = json.loads(row["delivery_options"] or "{}")
    if not isinstance(raw_metadata, dict):
        raise ValueError(f"input {row['id']} metadata is not an object")
    merged = {**raw_metadata, **stamps}  # the claim's stored writes; None removes a key
    raw_metadata = {k: v for k, v in merged.items() if v is not None or k not in stamps}
    if not isinstance(delivery_options, dict):
        raise ValueError(f"input {row['id']} delivery options are not an object")
    return ClaimedInput(
        message_id=str(row["id"]),
        input_order=int(row["input_order"]),
        revision=int(row["delivery_revision"]),
        role=str(row["role"]),
        content=str(row["content"]),
        intent=str(row["delivery_intent"]),
        metadata=public_metadata(raw_metadata),
        delivery_options=delivery_options,
        created_at=str(row["created_at"]),
    )


def preview_input_batch(
    thread_id: str,
    *,
    execution_epoch: int,
    model: str,
    db: sqlite3.Connection,
) -> ClaimedBatch | None:
    """Read the exact unclaimed FIFO prefix without changing its ownership."""
    rows = _queued_epoch_prefix(
        db,
        thread_id,
        expected_epoch=execution_epoch,
        expected_model=model,
    )
    rows = _deliverable_prefix(rows)
    if not rows:
        return None
    return _batch(
        thread_id,
        f"preview:{thread_id}",
        0,
        execution_epoch,
        model,
        rows,
    )


def invalidate_idle_checkpoint_markers(
    db: sqlite3.Connection, thread_id: str, *, reason: IdleCheckpointReason,
) -> None:
    """Clear superseded idle work and its timer when real work wins.

    Only a caller that has truly taken the thread's idle state over may call
    this: accepting an input has not, since a passive context event starts no
    Turn of its own. The timer and count follow the clear's commit, so a
    rollback leaves the marker armed, timed and uncounted.
    """
    cleared = bool(db.execute(
        "UPDATE active_turns SET idle_checkpoint_pending=0 "
        "WHERE thread_id=? AND idle_checkpoint_pending=1", (thread_id,),
    ).rowcount)
    after_commit(db, lambda: cancel_timer_and_record(thread_id, cleared=cleared, reason=reason))


def invalidate_markers_for_archive(db: sqlite3.Connection, thread_id: str) -> None:
    """Clear an archived thread's markers: nothing will ever fold them."""
    invalidate_idle_checkpoint_markers(db, thread_id, reason=IdleCheckpointReason.ARCHIVED)


def _claimable_prefix(
    connection: sqlite3.Connection, thread_id: str, *, movable: bool = False,
) -> tuple[int, str, list[sqlite3.Row], frozenset[str], bool, tuple[str, ...]] | None:
    """The FIFO prefix an idle claim would take: epoch, model, rows, lifted holds, movable-held."""
    state = thread_admission_state(connection, thread_id, allow_unavailable=True)
    # A model or account hold keeps inputs queued until the relevant choice changes.
    selection = admitted_model_state(thread_id, state, db=connection, movable=movable)
    if selection is None:
        return None
    epoch, model, held = selection
    rows = _queued_epoch_prefix(connection, thread_id, expected_epoch=epoch, expected_model=model)
    from infra.send_target import lifted_hold_ids, load_authors
    from infra.unavailable_model_recovery import liftable_hold_sets, model_admission

    rows = still_claimable(connection, thread_id, rows)
    model_held, account_held = liftable_hold_sets(rows)
    candidate = _deliverable_prefix(rows, model_held | account_held)
    load_for = load_authors((row['id'], str(row['role']), json.loads(row['metadata'] or '{}')) for row in candidate)
    admission = model_admission(model, load_for=load_for, db=connection, thread_id=thread_id)[1] if model_held else Admission("run")
    lifted = lifted_hold_ids(model_held, account_held, admission)
    rows = _deliverable_prefix(rows, lifted)
    if not rows or not any(starts_turn(row) for row in rows):
        return None
    claimed = {row['id'] for row in rows}  # only the people this Turn claims authorize its load
    return epoch, model, rows, lifted, held, tuple(i for i in load_for if i in claimed)


def fail_claimable_prefix(
    connection: sqlite3.Connection, thread_id: str, *, error: str,
) -> FailedInputBatch | None:
    """Fail, in the caller's write, the prefix an idle claim would have taken.

    For admission that cannot prove the account usable and so fails closed:
    the prefix ends visibly failed with Retry and an owner report, never parked.
    """
    prefix = _claimable_prefix(connection, thread_id)
    if prefix is None:
        return None
    from infra.queued_input_settlement import fail_queued

    message_ids = fail_queued(connection, thread_id, prefix[2], error=error)
    return FailedInputBatch(thread_id, message_ids, error)


def _clear_taken_lifted(connection, thread_id, epoch, model, rows, lifted):
    """The sole clearer, inside either canonical claim and only for taken rows."""
    if not lifted:
        return rows
    taken = {str(row['id']) for row in rows}
    connection.executemany(
        "UPDATE messages SET write_outcome=NULL,delivery_error=NULL,"
        "delivery_revision=delivery_revision+1 WHERE id=? "
        "AND write_outcome='proved_no_write' AND delivery_state='queued' AND active_turn_id IS NULL",
        [(message_id,) for message_id in taken & lifted],
    )
    return [row for row in _queued_epoch_prefix(
        connection, thread_id, expected_epoch=epoch, expected_model=model,
    ) if str(row['id']) in taken]


def start_idle_turn(
    thread_id: str, *, turn_id: str, boot_epoch: str, started_at: str,
    db: sqlite3.Connection | None = None, connection_limit_probe=None,
    hold_expired_limit_at_startup: bool = False, connection_limit_subject=None,
) -> TurnAdmission | None:
    """Atomically claim one FIFO prefix as a Turn or a visible Plans failure."""
    with write_transaction(db, scope="start_idle_turn") as connection:
        if connection_limit_subject is not None:
            if not subject_matches_thread(connection_limit_subject, thread_id, db=connection):
                return None
        if connection.execute("SELECT 1 FROM active_turns WHERE thread_id=? AND status='running'", (thread_id,)).fetchone() is not None:
            return None
        from connections.account_moves import apply_pending_accounts
        apply_pending_accounts(thread_id, db=connection)
        prefix = _claimable_prefix(connection, thread_id, movable=True)
        if prefix is None:
            return None
        epoch, model, rows, lifted, movable_held, load_for = prefix
        from connections.account_holds import model_account_hold
        from connections.account_rollover import commit_rollover, plan_rollover
        from plans.public import schedule_thread_after_commit, set_aside_while_paused
        # No destination: the hold stands as with rollover off, before any guard acts on the queue.
        if movable_held and connection_limit_probe is None and plan_rollover(thread_id, eligible_rows=rows, db=connection) is None \
                and model_account_hold(thread_id, model, db=connection) is not None:
            return None
        # Pause contract rule 2 (#1971): only people start a paused agent.
        # Events, worker completions and, until #2286, scheduled wakes land
        # in its digest. Rows that ride along in a person's Turn are not held.
        if set_aside_while_paused(connection, thread_id, rows):
            return None
        from infra.plan_turn_admission import snapshot_or_failure
        plan = snapshot_or_failure(connection, thread_id, rows)
        if isinstance(plan, FailedInputBatch):
            return plan
        try:
            with write_transaction(connection, scope='rollover_fifo_claim'):
                rollover_plan = None if connection_limit_probe is not None else plan_rollover(thread_id, eligible_rows=rows, db=connection)
                moved = commit_rollover(rollover_plan, db=connection)
                if movable_held and model_account_hold(thread_id, model, db=connection) is not None:
                    if moved is not None:
                        raise _RolloverHeld
                    return None
                gated, held = connection_limit_gate(thread_id, model, probe=connection_limit_probe,
                                                    hold_expired_at_startup=hold_expired_limit_at_startup, db=connection)
                if gated:
                    if moved is not None:
                        raise _RolloverHeld
                    return held
                if connection_limit_probe is not None:
                    from infra.connection_limit_release import claim_probe

                    # The window's one probe: its marker commits with this claim or not at all.
                    if not claim_probe(connection_limit_probe, thread_id, db=connection):
                        return None
                invalidate_idle_checkpoint_markers(connection, thread_id, reason=IdleCheckpointReason.SUPERSEDED_BY_TURN)
                rows = _clear_taken_lifted(connection, thread_id, epoch, model, rows, lifted)
                inserted = connection.execute(
                    """INSERT INTO active_turns
                       (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
                        plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
                        plan_usage_role,vendor_account_id)
                       VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?)""",
                    (
                        turn_id, thread_id, epoch, model, started_at, boot_epoch,
                        plan["plan_id"], plan["plan_node_id"], plan["plan_attempt_id"],
                        plan["plan_commitment_id"], plan["plan_usage_role"],
                        turn_account_id(thread_id, model, db=connection),
                    ),
                )
                if inserted.lastrowid is None:
                    raise RuntimeError("Turn insert did not return its order")
                turn_order = int(inserted.lastrowid)
                schedule_thread_after_commit(thread_id, db=connection)  # Turn start is no plan write
                stamps, receipt, recovered = _assign_turn(connection, thread_id, turn_id, rows, initial=True)
        except _RolloverHeld:
            return connection_limit_gate(thread_id, model, probe=connection_limit_probe, hold_expired_at_startup=hold_expired_limit_at_startup, db=connection)[1]
    return _batch(thread_id, turn_id, turn_order, epoch, model, rows, stamps, receipt, recovered, load_for)


def claim_safe_boundary(
    thread_id: str,
    turn_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> ClaimedBatch | None:
    """Claim FIFO inputs through the last Send available at a safe boundary."""
    with write_transaction(db, scope="claim_safe_boundary") as connection:
        turn = connection.execute(
            "SELECT turn_order,execution_epoch,model,vendor_account_id FROM active_turns "
            "WHERE id=? AND thread_id=? AND status='running'",
            (turn_id, thread_id),
        ).fetchone()
        if turn is None:
            # Temporary diagnosis for silent False starvation; delete with its root-cause fix.
            # Never add retry, repair, or suppression here.
            log.debug(
                "safe-boundary claim declined thread_id=%s turn_id=%s reason=no_running_turn",
                thread_id, turn_id,
            )
            return None
        from automation.self_restart import restart_holds_new_work

        if restart_holds_new_work(db=connection):  # draining toward a restart: queued input waits for it
            return None
        epoch = int(turn["execution_epoch"])
        model = str(turn["model"])
        rows = _queued_epoch_prefix(
            connection,
            thread_id,
            expected_epoch=epoch,
            expected_model=model,
        )
        from infra.send_target import boundary_account_lifted_ids

        rows = still_claimable(connection, thread_id, rows)
        lifted = boundary_account_lifted_ids(connection, thread_id, turn, rows)
        rows = _deliverable_prefix(rows, lifted)
        last_send = max((i for i, row in enumerate(rows) if row["delivery_intent"] == "apply_now"), default=None)
        if last_send is None:
            # Temporary diagnosis for silent False starvation; delete with its root-cause fix.
            # Never add retry, repair, or suppression here.
            log.debug(
                "safe-boundary claim declined thread_id=%s turn_id=%s reason=no_apply_now_row",
                thread_id, turn_id,
            )
            return None
        claimed = _clear_taken_lifted(connection, thread_id, epoch, model, rows[:last_send + 1], lifted)
        stamps, receipt, _ = _assign_turn(connection, thread_id, turn_id, claimed)
    return _batch(
        thread_id, turn_id, int(turn["turn_order"]), epoch, model, claimed, stamps, receipt,
    )
