"""The explicit legal transitions of one durable checkpoint generation."""

from __future__ import annotations

import sqlite3
from enum import Enum
from typing import cast

from core.checkpoint_types import (
    CheckpointMode,
    CheckpointRecord,
    CheckpointRequest,
    CheckpointStatus,
    CheckpointTrigger,
)
from infra.checkpoint_admission import (
    advances_or_switch_refreshes,
    installed_winner,
)
from infra.checkpoint_entry_index import index_checkpoint_entries
from infra.checkpoint_install_validation import cold_install_refusal
from infra.checkpoint_receipts import completion_receipt_id
from infra.database import get_db, write_transaction
from infra.continuity_staging import freeze_continuity, merge_staged_edits


class CheckpointConflictKind(str, Enum):
    """Why `_create_running` lost the race, for callers that need to know."""

    STALE_EPOCH = "stale_epoch"
    ACTIVE_CHECKPOINT = "active_checkpoint"
    BOUNDARY_NOT_DURABLE = "boundary_not_durable"
    STALE_PREDECESSOR = "stale_predecessor"
    BOUNDARY_NOT_ADVANCING = "boundary_not_advancing"


class CheckpointConflict(RuntimeError):
    """Checkpoint creation no longer matches the thread's durable head."""

    def __init__(self, message: str, kind: CheckpointConflictKind) -> None:
        super().__init__(message)
        self.kind = kind


class CheckpointTransitionError(RuntimeError):
    """A requested exact checkpoint transition lost its compare-and-swap."""


def checkpoint_record(row: sqlite3.Row) -> CheckpointRecord:
    """Build the typed record for one ``checkpoints`` row."""
    return CheckpointRecord(
        generation_order=int(row["generation_order"]),
        id=str(row["id"]),
        thread_id=str(row["thread_id"]),
        history_epoch=int(row["history_epoch"]),
        previous_generation_id=row["previous_generation_id"],
        through_seq=int(row["through_seq"]),
        through_message_id=str(row["through_message_id"]),
        trigger=cast(CheckpointTrigger, row["trigger"]),
        mode=cast(CheckpointMode, row["mode"]),
        status=cast(CheckpointStatus, row["status"]),
        source_harness=str(row["source_harness"]),
        source_model=str(row["source_model"]),
        writer_harness=str(row["writer_harness"]),
        writer_model=str(row["writer_model"]),
        originating_turn_id=row["originating_turn_id"],
        first_included_seq=row["first_included_seq"],
        continuity_base_revision=int(row["continuity_base_revision"]),
        continuity_base_snapshot_json=str(row["continuity_base_snapshot_json"]),
        raw_result=row["raw_result"],
        continuity_snapshot_json=row["continuity_snapshot_json"],
        artifact_snapshot_json=row["artifact_snapshot_json"],
        plan_status_snapshot_json=row["plan_status_snapshot_json"],
        through_at=row["through_at"],
        usage_json=row["usage_json"],
        error=row["error"],
        started_at=str(row["started_at"]),
        captured_at=row["captured_at"],
        installed_at=row["installed_at"],
        ended_at=row["ended_at"],
        adopted_input_tokens=row["adopted_input_tokens"],
    )


def _head(db: sqlite3.Connection, thread_id: str, epoch: int) -> CheckpointRecord | None:
    row = db.execute(
        "SELECT * FROM checkpoints WHERE thread_id=? AND history_epoch=? "
        "AND status='installed' ORDER BY generation_order DESC LIMIT 1",
        (thread_id, epoch),
    ).fetchone()
    return checkpoint_record(row) if row is not None else None


def _required_record(db: sqlite3.Connection, checkpoint_id: str) -> CheckpointRecord:
    row = db.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
    if row is None:
        raise CheckpointTransitionError(f"checkpoint {checkpoint_id!r} does not exist")
    return checkpoint_record(row)


def _create_running(connection: sqlite3.Connection, request: CheckpointRequest) -> CheckpointRecord:
    thread_row = connection.execute(
        "SELECT history_epoch FROM threads WHERE id=?", (request.thread_id,)
    ).fetchone()
    if thread_row is None or int(thread_row["history_epoch"]) != request.history_epoch:
        raise CheckpointConflict(
            "checkpoint history epoch is stale", CheckpointConflictKind.STALE_EPOCH,
        )
    active = connection.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id=? "
        "AND status IN ('running','captured')",
        (request.thread_id,),
    ).fetchone()
    if active is not None:
        raise CheckpointConflict(
            "thread already has an active checkpoint", CheckpointConflictKind.ACTIVE_CHECKPOINT,
        )
    boundary = connection.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND id=? AND seq=?",
        (request.thread_id, request.through_message_id, request.through_seq),
    ).fetchone()
    if boundary is None:
        raise CheckpointConflict(
            "checkpoint boundary is no longer durable", CheckpointConflictKind.BOUNDARY_NOT_DURABLE,
        )
    head = _head(connection, request.thread_id, request.history_epoch)
    head_id = head.id if head is not None else None
    if request.previous_generation_id != head_id:
        winner = installed_winner(head, request)
        if winner is not None:
            return winner
        raise CheckpointConflict(
            "checkpoint predecessor is stale", CheckpointConflictKind.STALE_PREDECESSOR,
        )
    if not advances_or_switch_refreshes(head, request):
        raise CheckpointConflict(
            "checkpoint boundary does not advance", CheckpointConflictKind.BOUNDARY_NOT_ADVANCING,
        )
    base_revision, base_snapshot = freeze_continuity(
        connection, request.thread_id, request.history_epoch
    )
    connection.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,previous_generation_id,through_seq,"
        "through_message_id,trigger,mode,status,source_harness,source_model,"
        "writer_harness,writer_model,originating_turn_id,first_included_seq,"
        "continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES (?,?,?,?,?,?,?,?, 'running',?,?,?,?,?,?,?,?,?)",
        (
            request.id,
            request.thread_id,
            request.history_epoch,
            request.previous_generation_id,
            request.through_seq,
            request.through_message_id,
            request.trigger,
            request.mode,
            request.source_harness,
            request.source_model,
            request.writer_harness,
            request.writer_model,
            request.originating_turn_id,
            request.first_included_seq,
            base_revision,
            base_snapshot,
            request.started_at,
        ),
    )
    return _required_record(connection, request.id)


def create_running(request: CheckpointRequest, *, db: sqlite3.Connection | None = None) -> CheckpointRecord:
    """Admit the request or return the installed sibling that won its race."""
    with write_transaction(db, scope="checkpoint_create") as connection:
        return _create_running(connection, request)


def admit_cold_input(
    checkpoint_id: str, *, first_included_seq: int | None,
    db: sqlite3.Connection | None = None,
) -> None:
    """Record the writer's fitted range once, before its first provider write."""
    if first_included_seq is not None and (
        isinstance(first_included_seq, bool)
        or not isinstance(first_included_seq, int)
        or first_included_seq < 0
    ):
        raise ValueError("cold input sequence must be a non-negative integer or None")
    with write_transaction(db, scope="checkpoint_cold_input") as connection:
        changed = connection.execute(
            "UPDATE checkpoints SET first_included_seq=? WHERE id=? "
            "AND mode='cold' AND status='running' AND first_included_seq IS NULL "
            "AND (? IS NULL OR ? <= through_seq) "
            "AND EXISTS (SELECT 1 FROM threads WHERE threads.id=checkpoints.thread_id "
            "AND threads.history_epoch=checkpoints.history_epoch)",
            (first_included_seq, checkpoint_id, first_included_seq, first_included_seq),
        )
        if changed.rowcount != 1:
            raise CheckpointTransitionError("cold checkpoint input is no longer admissible")



def adopt_cold(
    checkpoint_id: str, *, db: sqlite3.Connection | None = None,
) -> None:
    """Correct one pressure row from warm to cold before any provider write.

    An over-window warm fork produced no generation, so this is the same
    operation finding its only usable path, not a second attempt. The guard
    keeps that true: the row must still be the running warm pressure row that
    has written nothing.
    """
    with write_transaction(db, scope="checkpoint_adopt_cold") as connection:
        changed = connection.execute(
            "UPDATE checkpoints SET mode='cold' WHERE id=? AND mode='warm' "
            "AND status='running' AND trigger='pressure' "
            "AND first_included_seq IS NULL AND raw_result IS NULL",
            (checkpoint_id,),
        )
        if changed.rowcount != 1:
            raise CheckpointTransitionError(
                "checkpoint is no longer a running warm pressure operation"
            )


def capture(
    checkpoint_id: str, *,
    raw_result: str,
    usage_json: str | None,
    captured_at: str,
    diagnostic: str | None = None,
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord:
    """Persist exact provider bytes and any terminal diagnostic."""
    with write_transaction(db, scope="checkpoint_capture") as connection:
        changed = connection.execute(
            "UPDATE checkpoints SET status='captured',raw_result=?,usage_json="
            "CASE WHEN json_type(usage_json,'$.binding') IS NOT NULL THEN usage_json ELSE ? END,"
            "error=?,captured_at=? WHERE id=? AND status='running'",
            (raw_result, usage_json, diagnostic, captured_at, checkpoint_id),
        )
        if changed.rowcount != 1:
            raise CheckpointTransitionError("checkpoint capture lost its running CAS")
        return _required_record(connection, checkpoint_id)


def install(
    checkpoint_id: str,
    *,
    installed_at: str,
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord:
    """Publish one captured result or retain it as overtaken."""
    from infra.checkpoint_install_preflight import (
        finish_preflight, prepare_install_preflight,
    )

    preflight, preflight_error = prepare_install_preflight(
        checkpoint_id, installed_at, db, _required_record)
    with write_transaction(db, scope="checkpoint_install") as connection:
        record = _required_record(connection, checkpoint_id)
        if record.status != "captured":
            raise CheckpointTransitionError("checkpoint install requires captured")
        thread_row = connection.execute(
            "SELECT history_epoch,model,user_id,project_id FROM threads WHERE id=?",
            (record.thread_id,),
        ).fetchone()
        head = _head(connection, record.thread_id, record.history_epoch)
        boundary = connection.execute(
            "SELECT created_at FROM messages WHERE thread_id=? AND id=? AND seq=?",
            (record.thread_id, record.through_message_id, record.through_seq),
        ).fetchone()
        refusal: str | None = None
        eligible = (
            thread_row is not None
            and int(thread_row["history_epoch"]) == record.history_epoch
            and record.previous_generation_id == (head.id if head else None)
            and advances_or_switch_refreshes(head, record)
            and boundary is not None
            and (refusal := cold_install_refusal(connection, record, head)) is None
        )
        if eligible:
            if preflight_error is not None:
                raise preflight_error
            continuity_snapshot_json = merge_staged_edits(connection, checkpoint_id)
            # Freeze pane rows and plan status beside continuity (#1602).
            from infra.checkpoint_snapshots import capture_checkpoint_snapshots

            artifact_snapshot_json, plan_status_snapshot_json = (
                capture_checkpoint_snapshots(
                    connection, record.thread_id, thread_row=thread_row,
                    artifact_snapshot_json=(preflight[1] if preflight else None),
                )
            )
            connection.execute(
                "UPDATE checkpoints SET status='installed',"
                "continuity_snapshot_json=?,artifact_snapshot_json=?,"
                "plan_status_snapshot_json=?,through_at=?,installed_at=?,ended_at=? "
                "WHERE id=? AND status='captured'",
                (
                    continuity_snapshot_json, artifact_snapshot_json,
                    plan_status_snapshot_json, str(boundary["created_at"]),
                    installed_at, installed_at,
                    checkpoint_id,
                ),
            )
            index_checkpoint_entries(connection, checkpoint_id)
            finish_preflight(
                preflight, record, thread_row, installed_at, connection,
            )
            from infra.input_writes import release_checkpoint_inputs

            release_checkpoint_inputs(
                connection, str(thread_row["model"] or ""), thread_id=record.thread_id,
            )
        else:
            overtaken_error = refusal or "checkpoint boundary was overtaken"
            if record.error:
                overtaken_error = f"{record.error}; {overtaken_error}"
            connection.execute(
                "UPDATE checkpoints SET status='overtaken',error=?,ended_at=? "
                "WHERE id=? AND status='captured'",
                (overtaken_error, installed_at, checkpoint_id),
            )
        return _required_record(connection, checkpoint_id)


def fail(
    checkpoint_id: str,
    *,
    error: str,
    ended_at: str,
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord:
    """Record a terminal failure of an attempt still running or captured.

    A captured row reaches here only when its own install raised; it ends
    failed with that error instead of waiting for a retry.
    """
    with write_transaction(db, scope="checkpoint_fail") as connection:
        changed = connection.execute(
            "UPDATE checkpoints SET status='failed',error=?,ended_at=? "
            "WHERE id=? AND status IN ('running','captured')",
            (error, ended_at, checkpoint_id),
        )
        if changed.rowcount != 1:
            raise CheckpointTransitionError("checkpoint failure lost its nonterminal CAS")
        return _required_record(connection, checkpoint_id)


def cancel(
    checkpoint_id: str,
    *,
    ended_at: str,
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord | None:
    """Let exact cancellation win only before provider bytes are captured."""
    with write_transaction(db, scope="checkpoint_cancel") as connection:
        changed = connection.execute(
            "UPDATE checkpoints SET status='cancelled',ended_at=? "
            "WHERE id=? AND status='running'",
            (ended_at, checkpoint_id),
        )
        return _required_record(connection, checkpoint_id) if changed.rowcount == 1 else None


def reconcile_startup(
    *,
    reconciled_at: str,
    db: sqlite3.Connection | None = None,
) -> tuple[CheckpointRecord, ...]:
    """Fail unpaid running rows and publish already-captured bytes once."""
    with write_transaction(db, scope="checkpoint_reconcile") as connection:
        rows = connection.execute(
            "SELECT id,status FROM checkpoints WHERE status IN ('running','captured') "
            "ORDER BY generation_order"
        ).fetchall()
        reconciled: list[CheckpointRecord] = []
        for row in rows:
            checkpoint_id = str(row["id"])
            if row["status"] == "running":
                connection.execute(
                    "UPDATE checkpoints SET status='failed',error=?,ended_at=? "
                    "WHERE id=? AND status='running'",
                    ("checkpoint interrupted before capture", reconciled_at, checkpoint_id),
                )
                reconciled.append(_required_record(connection, checkpoint_id))
            else:
                reconciled.append(
                    install(
                        checkpoint_id,
                        installed_at=reconciled_at,
                        db=connection,
                    )
                )
        return tuple(reconciled)
