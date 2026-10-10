"""Transactional model-switch boundary on the canonical input FIFO."""

from __future__ import annotations

import sqlite3

from infra.database import write_transaction
from connections.account_holds import refresh_thread_holds
from connections.account_policy import AccountAdmissionError
from connections.account_selection import bind_model_account
from infra.model_switch_inputs import retarget_unwritten_suffix as _retarget_unwritten_suffix
from infra.model_switch_inputs import (
    carry_passive_source_inputs,
    log_switch_choice,
    return_unstarted_suffix_to_source,
    rides_switch,
)
from infra.model_switch_types import SwitchRequest, SwitchTransition


def _switch_row(db: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM threads WHERE switch_operation_id=?", (operation_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"model switch does not exist: {operation_id}")
    return row


def _log_requested_choice(thread_id: str, row: sqlite3.Row, requested: str) -> None:
    """Log the requested writer unless the transfer just forced the target."""
    if row["switch_checkpoint_choice"] == requested:
        log_switch_choice(thread_id, "target" if requested == "target" else "source")


def _validated_request(
    destination_model: str, operation_id: str, requested_at: str,
    actor_kind: str, reason: str, checkpoint_choice: str,
) -> str:
    destination = destination_model.strip()
    if not destination or not operation_id or not requested_at or not reason.strip():
        raise ValueError("model switch requires destination, operation, and time")
    if actor_kind not in {"user", "agent", "system"}:
        raise ValueError("model switch actor kind is invalid")
    if checkpoint_choice not in {"current", "target"}:
        raise ValueError("model switch checkpoint choice is invalid")
    return destination


def _source_and_defer_receipt(row: sqlite3.Row, destination: str, *, db) -> tuple[str, bool]:
    """Defer a destination-vendor move receipt while a cross-vendor switch prepares."""
    from infra.vendor_turn_binding import model_vendor

    source = str(row['model'] or '').strip()
    return source, model_vendor(source, db=db) != model_vendor(destination, db=db)


def request_switch(
    thread_id: str,
    destination_model: str,
    *,
    operation_id: str,
    requested_at: str,
    actor_kind: str = "user",
    actor_id: str | None = None,
    reason: str = "model_switch",
    checkpoint_choice: str = "current",
    user_id: str | None = None,
    account_id: str | None = None,
    db: sqlite3.Connection | None = None,
) -> SwitchRequest | None:
    """Freeze the FIFO boundary chosen by this request's database commit."""
    destination = _validated_request(destination_model, operation_id, requested_at, actor_kind, reason, checkpoint_choice)
    with write_transaction(db, scope="request_switch") as connection:
        row = connection.execute(
            "SELECT id,user_id,model,execution_epoch,next_input_order,pending_model,"
            "pending_execution_epoch,switch_after_input_order,"
            "switch_operation_id,switch_status,switch_actor_kind,switch_actor_id,switch_reason,"
            "switch_checkpoint_choice,switch_requested_at FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise ValueError("task does not exist")
        if user_id is not None and str(row["user_id"]) != user_id:
            raise ValueError("task is not owned by the requesting user")
        source, defer_receipt = _source_and_defer_receipt(row, destination, db=connection)
        bind_model_account(thread_id, destination, account_id=account_id,
                           actor_thread_id=actor_id if actor_kind == "agent" else None,
                           defer_receipt=defer_receipt, db=connection)
        if not source:
            raise ValueError("task has no selected model")
        existing = row["switch_operation_id"]
        if existing is not None:
            same_destination = str(row["pending_model"]) == destination
            if same_destination and row["switch_status"] != "failed":
                row, changed = _retarget_unwritten_suffix(connection, row)
                return _request_value(
                    connection, thread_id, row, source, destination, duplicate=True, updated_input_ids=changed,
                )
            return _replace_pending_switch(
                connection,
                row,
                destination=destination,
                operation_id=operation_id,
                requested_at=requested_at,
                actor_kind=actor_kind,
                actor_id=actor_id,
                reason=reason,
                checkpoint_choice=checkpoint_choice,
            )
        if source == destination:
            return None
        boundary = int(row["next_input_order"]) - 1
        destination_epoch = int(row["execution_epoch"]) + 1
        connection.execute(
            "UPDATE threads SET pending_model=?,pending_execution_epoch=?,"
            "switch_after_input_order=?,switch_operation_id=?,"
            "switch_status='preparing',switch_error=NULL,switch_requested_at=?,"
            "switch_actor_kind=?,switch_actor_id=?,switch_reason=?,"
            "switch_checkpoint_choice=? "
            "WHERE id=?",
            (
                destination, destination_epoch, boundary, operation_id,
                requested_at, actor_kind, actor_id, reason, checkpoint_choice,
                thread_id,
            ),
        )
        current = connection.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        current, changed = _retarget_unwritten_suffix(connection, current)
        _log_requested_choice(thread_id, current, checkpoint_choice)
        return _request_value(
            connection, thread_id, current, source, destination, duplicate=False, updated_input_ids=changed,
        )


def _replace_pending_switch(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    destination: str,
    operation_id: str,
    requested_at: str,
    actor_kind: str,
    actor_id: str | None,
    reason: str,
    checkpoint_choice: str,
) -> SwitchRequest:
    """Retarget the one uncommitted destination without moving its FIFO cut."""
    if row["switch_status"] not in {"preparing", "failed"}:
        raise RuntimeError("model switch is already committing")
    thread_id = str(row["id"])
    destination_epoch = int(row["pending_execution_epoch"])
    db.execute(
        "UPDATE threads SET pending_model=?,switch_operation_id=?,"
        "switch_status='preparing',switch_error=NULL,switch_requested_at=?,"
        "switch_actor_kind=?,switch_actor_id=?,switch_reason=?,"
        "switch_checkpoint_choice=? WHERE id=?",
        (
            destination, operation_id, requested_at, actor_kind,
            actor_id, reason, checkpoint_choice, thread_id,
        ),
    )
    changed_destination = db.execute(
        "UPDATE messages SET target_model=?,delivery_revision=delivery_revision+1 WHERE thread_id=? "
        "AND target_execution_epoch=? AND active_turn_id IS NULL RETURNING id",
        (destination, thread_id, destination_epoch),
    ).fetchall()
    current = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    current, changed = _retarget_unwritten_suffix(db, current)
    _log_requested_choice(thread_id, current, checkpoint_choice)
    return _request_value(
        db, thread_id, current, str(row["model"]), destination, duplicate=False,
        updated_input_ids=tuple(str(item[0]) for item in changed_destination) + changed,
    )


def _request_value(
    db: sqlite3.Connection,
    thread_id: str,
    row: sqlite3.Row,
    source: str,
    destination: str,
    *,
    duplicate: bool,
    updated_input_ids: tuple[str, ...] = (),
) -> SwitchRequest:
    return SwitchRequest(
        str(row["switch_operation_id"]), thread_id, source, destination,
        int(row["execution_epoch"]), int(row["pending_execution_epoch"]),
        int(row["switch_after_input_order"]),
        str(row["switch_actor_kind"] or "system"),
        str(row["switch_actor_id"]) if row["switch_actor_id"] is not None else None,
        str(row["switch_reason"] or "model_switch"),
        str(row["switch_checkpoint_choice"] or "current"),
        str(row["switch_requested_at"]),
        int(row["next_input_order"]) - 1,
        _source_work_remains(db, row),
        duplicate,
        updated_input_ids,
    )


def resolvable_switch(
    db: sqlite3.Connection,
    thread_id: str,
) -> SwitchRequest | None:
    """Resolve a drained source or transfer its proved-unwritten held suffix."""
    row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None or row["switch_status"] != "preparing":
        return None
    row, changed = _retarget_unwritten_suffix(db, row)
    if _source_work_remains(db, row):
        return None
    return _request_value(
        db, thread_id, row, str(row["model"]), str(row["pending_model"]),
        duplicate=True, updated_input_ids=changed,
    )


def advance_pending_switch(thread_id: str) -> SwitchRequest | None:
    """Reconcile an existing switch on its natural wake, including startup."""
    with write_transaction(None, scope="advance_pending_switch") as db:
        row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None or row["switch_status"] not in {"preparing", "ready"}:
            return None
        row, changed = _retarget_unwritten_suffix(db, row)
        return _request_value(
            db, thread_id, row, str(row["model"]), str(row["pending_model"]), duplicate=True, updated_input_ids=changed,
        )


def read_switch(
    thread_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> SwitchRequest | None:
    """Read the one durable pending switch and its current source wait."""
    connection = db
    owns_connection = connection is None
    if connection is None:
        from infra.database import get_db

        connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        if row is None or row["switch_status"] not in {"preparing", "ready"}:
            return None
        return _request_value(
            connection,
            thread_id,
            row,
            str(row["model"]),
            str(row["pending_model"]),
            duplicate=True,
        )
    finally:
        if owns_connection:
            connection.close()


def _source_work_remains(db: sqlite3.Connection, row: sqlite3.Row) -> bool:
    thread_id = str(row["id"])
    epoch = int(row["execution_epoch"])
    if db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND execution_epoch=? "
        "AND status='running' LIMIT 1",
        (thread_id, epoch),
    ).fetchone() is not None:
        return True
    queued = db.execute(
        "SELECT role,metadata,seq,active_turn_id,delivery_state,write_started_at,"
        "write_outcome,delivery_attempt_id FROM messages WHERE thread_id=? "
        "AND delivery_state='queued' AND active_turn_id IS NULL "
        "AND target_execution_epoch=?",
        (thread_id, epoch),
    ).fetchall()
    return any(not rides_switch(item) for item in queued)


def activate_ready_switch(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    committed_at: str,
) -> SwitchTransition | None:
    """Commit a ready destination only after every source-epoch input drains."""
    row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None or row["switch_status"] != "ready":
        return None
    if _source_work_remains(db, row):
        return None
    operation_id = str(row["switch_operation_id"])
    source_model = str(row["model"])
    destination_model = str(row["pending_model"])
    try:
        bind_model_account(thread_id, destination_model,
                           actor_thread_id=row["switch_actor_id"] if row["switch_actor_kind"] == "agent" else None,
                           db=db)
    except AccountAdmissionError:
        # Activation runs inside Turn settlement; a raced refusal becomes the
        # destination's durable hold and launch admission refuses spending.
        refresh_thread_holds(thread_id, db=db)
    updated = db.execute(
        "UPDATE threads SET model=pending_model,"
        "execution_epoch=pending_execution_epoch,pending_model=NULL,"
        "pending_execution_epoch=NULL,switch_after_input_order=NULL,"
        "switch_operation_id=NULL,switch_status=NULL,switch_error=NULL,"
        "switch_requested_at=NULL,switch_actor_kind=NULL,switch_actor_id=NULL,"
        "switch_reason=NULL,switch_checkpoint_choice=NULL,"
        "runtime_context_epoch=runtime_context_epoch+1,"
        "updated_at=? WHERE id=? AND switch_status='ready'",
        (committed_at, thread_id),
    )
    if updated.rowcount != 1:
        return None
    from connections.account_moves import release_deferred_receipt
    from infra.vendor_turn_binding import model_vendor

    release_deferred_receipt(thread_id, model_vendor(destination_model, db=db), db=db)
    # Passive context never held the switch back, so it rides to the destination.
    carry_passive_source_inputs(db, row)
    return SwitchTransition(
        thread_id,
        operation_id,
        True,
        source_model,
        destination_model,
    )



def fail_switch(
    operation_id: str,
    *,
    error: str,
    db: sqlite3.Connection | None = None,
) -> SwitchTransition:
    """Fail only the destination while preserving source and accepted inputs.

    A destination that never wrote anything keeps no claim on the FIFO: any
    suffix an earlier preparing transfer moved to it returns to the source in
    this same transaction, so the source can answer it again.

    The first cause wins. Concurrent drivers of one operation can each reach
    this exit, and the later ones only ever carry the consequence of the first
    failure, so overwriting would destroy the only diagnosable cause.
    """
    if not error.strip():
        raise ValueError("failed model switch requires a reason")
    with write_transaction(db, scope="fail_switch") as connection:
        row = _switch_row(connection, operation_id)
        thread_id = str(row["id"])
        recorded = connection.execute(
            "UPDATE threads SET switch_status='failed',"
            "switch_error=COALESCE(NULLIF(TRIM(switch_error),''),?) "
            "WHERE id=? AND switch_operation_id=? RETURNING switch_error",
            (error, thread_id, operation_id),
        ).fetchone()
        # The destination will never run, so its unstarted inputs return to the
        # source in this same transaction rather than waiting for an owner.
        return_unstarted_suffix_to_source(connection, row)
    return SwitchTransition(
        thread_id,
        operation_id,
        False,
        str(row["model"]),
        str(row["pending_model"]),
        error=str(recorded["switch_error"]) if recorded is not None else error,
    )
