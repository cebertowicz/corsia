"""Final compare-and-swap for one prepared model selection."""

from __future__ import annotations

from infra.database import write_transaction
from infra.time import utc_now_iso
from thread.model_projection import read_model_switch_fit_input
from thread.model_row import (
    _insert_model_switch_breadcrumb,
    _model_switch_transcript_version,
    _strictly_newer_model_version,
    _model_switch_boundary_is_clear,
)
from thread import model_source
from thread.model_state import (
    ModelSwitchCommit,
    ModelSwitchConflict,
    ModelSwitchPreparation,
    ModelSwitchPreparationError,
    ModelSwitchSequencingRequired,
    _model_switch_gate,
    _require_current_model_switch_generation,
)


def _checkpoint_proof_is_current(
    db,
    preparation: ModelSwitchPreparation,
) -> bool:
    proof = preparation.projection_proof
    if proof.mode != "checkpoint_tail":
        return True
    from history.types import HistoryBoundary
    from infra.checkpoint_queries import current_installed

    boundary = proof.checkpoint_boundary
    if boundary is None or proof.checkpoint_id is None:
        return False
    record = current_installed(preparation.thread_id, db=db)
    if record is None:
        return False
    installed_boundary = HistoryBoundary(
        record.thread_id,
        record.history_epoch,
        record.through_seq,
        record.through_message_id,
    )
    return (
        record.id == proof.checkpoint_id
        and installed_boundary == boundary
        and record.history_epoch == preparation.history_epoch
    )


def _require_frozen_source(row, preparation, user_id: str | None) -> None:
    if row is None:
        raise ModelSwitchConflict("model switch thread vanished before commit")
    if user_id is not None and row["user_id"] != user_id:
        raise ModelSwitchConflict("model switch thread ownership changed before commit")
    if (str(row["model"] or "").strip() or None) != preparation.source_selected_model:
        raise ModelSwitchConflict("model switch source model changed before commit")
    if int(row["history_epoch"]) != preparation.history_epoch:
        raise ModelSwitchConflict("model switch history epoch changed before commit")
    if int(row["runtime_context_epoch"]) != preparation.source_runtime_context_epoch:
        raise ModelSwitchConflict("model switch runtime context changed before commit")


def _require_pending_boundary(row, preparation) -> None:
    if (
        row["switch_operation_id"] is None
        or row["switch_status"] not in {"preparing", "ready"}
        or str(row["pending_model"] or "") != preparation.destination_model
        or int(row["pending_execution_epoch"] or -1)
        != int(row["execution_epoch"]) + 1
    ):
        # The durable boundary is authoritative and this preparation is stale:
        # re-read it rather than failing the switch on the stale snapshot.
        raise ModelSwitchSequencingRequired(
            "model switch pending boundary changed before commit"
        )


def _require_frozen_history(db, preparation) -> None:
    if not _model_switch_boundary_is_clear(
        db,
        preparation.thread_id,
        preparation.destructive_version,
    ):
        raise ModelSwitchSequencingRequired(
            "model switch history boundary changed before commit"
        )
    if (
        _model_switch_transcript_version(
            db,
            preparation.thread_id,
        )
        != preparation.transcript_version
    ):
        raise ModelSwitchSequencingRequired(
            "model switch transcript changed before commit"
        )


def _commit_row(db, thread_id: str):
    return db.execute(
        "SELECT user_id,model,project_id,history_epoch,runtime_context_epoch,"
        "execution_epoch,next_message_seq,pending_model,pending_execution_epoch,"
        "switch_operation_id,switch_status,updated_at "
        "FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()


def _validated_commit_row(
    db,
    preparation: ModelSwitchPreparation,
    *,
    user_id: str | None,
):
    """Recheck every frozen source and destination predicate in one place."""
    row = _commit_row(db, preparation.thread_id)
    _require_frozen_source(row, preparation, user_id)
    _require_pending_boundary(row, preparation)
    _require_frozen_history(db, preparation)
    _require_current_fit_input(db, preparation)
    if not _checkpoint_proof_is_current(db, preparation):
        raise ModelSwitchConflict(
            "model switch prepared projection changed before commit"
        )
    _require_current_destination(db, preparation, row)
    return row


def _compact_commit_row(
    db,
    preparation: ModelSwitchPreparation,
    *,
    user_id: str | None,
):
    """Revalidate the prepared boundary after taking the SQLite writer lock."""
    row = _commit_row(db, preparation.thread_id)
    _require_frozen_source(row, preparation, user_id)
    _require_pending_boundary(row, preparation)
    _require_frozen_history(db, preparation)
    _require_current_input_proof(db, preparation, row)
    if not _checkpoint_proof_is_current(db, preparation):
        raise ModelSwitchConflict(
            "model switch prepared projection changed before commit"
        )
    _require_current_destination(db, preparation, row)
    return row


def _require_current_destination(db, preparation: ModelSwitchPreparation, row) -> None:
    if row["project_id"] != preparation.project_id:
        raise ModelSwitchConflict("model switch project scope changed before commit")
    prepared = preparation.destination_target
    try:
        current = model_source._current_switch_destination(
            db, preparation.destination_model, prepared,
            thread_id=preparation.thread_id, stage="before commit",
        )
    except ModelSwitchPreparationError as exc:
        raise ModelSwitchConflict(
            f"model switch destination changed before commit: {exc}"
        ) from exc
    if current.context_window < prepared.context_window:
        # Fit was proved against the larger window; a growth keeps that proof.
        raise ModelSwitchSequencingRequired(
            "model switch destination window shrank before commit"
        )


def _require_current_fit_input(
    db,
    preparation: ModelSwitchPreparation,
) -> None:
    if preparation.input_proof is None:
        raise ModelSwitchSequencingRequired("model switch lacks its selected input proof")
    fit_input = read_model_switch_fit_input(
        db,
        preparation.thread_id,
        destination_model=preparation.destination_model,
        destination_target=preparation.destination_target,
        max_items=len(preparation.input_proof.members),
    )
    if preparation.destination_fit_ledger.input_identity != fit_input.identity:
        raise ModelSwitchSequencingRequired(
            "model switch FIFO input changed before commit"
        )


def _require_current_input_proof(db, preparation, row) -> None:
    """Check only compact FIFO facts under the existing commit writer lock."""
    proof = preparation.input_proof
    if proof is None or (proof.operation_id, proof.execution_epoch) != (
        row["switch_operation_id"], row["pending_execution_epoch"],
    ):
        raise ModelSwitchSequencingRequired("model switch selected input boundary changed")
    members = db.execute(
        "SELECT id,input_order,delivery_revision,active_turn_id,seq,write_started_at,"
        "write_outcome,target_model FROM messages WHERE thread_id=? "
        "AND target_execution_epoch=? AND delivery_state='queued' "
        "ORDER BY input_order LIMIT ?",
        (preparation.thread_id, proof.execution_epoch, len(proof.members)),
    ).fetchall()
    current = tuple((str(item["id"]), int(item["input_order"]), int(item["delivery_revision"])) for item in members)
    if current != proof.members or any(
        item["target_model"] != preparation.destination_model
        or any(item[key] is not None for key in ("active_turn_id", "seq", "write_started_at", "write_outcome"))
        for item in members
    ):
        raise ModelSwitchSequencingRequired("model switch selected FIFO input changed")


def _commit_model_switch_transaction(
    preparation: ModelSwitchPreparation,
    *,
    user_id: str | None,
    now: str,
    reason: str | None,
    actor_thread_id: str | None,
    actor_title: str | None,
) -> ModelSwitchCommit:
    from infra.database import get_db

    db = get_db()
    try:
        _validated_commit_row(
            db,
            preparation,
            user_id=user_id,
        )
        with write_transaction(db, scope="model_switch_commit"):
            row = _compact_commit_row(
                db,
                preparation,
                user_id=user_id,
            )
            committed_at = _strictly_newer_model_version(now, row["updated_at"])
            db.execute(
                "UPDATE threads SET switch_status='ready' "
                "WHERE id=? AND switch_operation_id=?",
                (preparation.thread_id, row["switch_operation_id"]),
            )
            from infra.model_switches import activate_ready_switch

            transition = activate_ready_switch(
                db,
                preparation.thread_id,
                committed_at=committed_at,
            )
            if transition is None:
                raise ModelSwitchConflict(
                    "model switch compare-and-swap lost before commit"
                )
            breadcrumb = _insert_model_switch_breadcrumb(
                db,
                preparation.thread_id,
                preparation.destination_model,
                preparation.source_selected_model,
                now=committed_at,
                reason=reason,
                actor_thread_id=actor_thread_id,
                actor_title=actor_title,
                projection_mode=preparation.projection_mode,
            )
            return ModelSwitchCommit(
                breadcrumb=breadcrumb,
                committed_at=committed_at,
            )
    finally:
        db.close()


async def commit_model_switch(
    preparation: ModelSwitchPreparation,
    *,
    user_id: str | None,
    now: str | None = None,
    reason: str | None = None,
    actor_thread_id: str | None = None,
    actor_title: str | None = None,
) -> ModelSwitchCommit:
    """Commit one prepared switch iff every captured predicate still matches."""
    from infra.db_async import run_in_db

    if user_id != preparation.user_id:
        raise ModelSwitchConflict(
            "model switch commit user does not match its preparation"
        )
    gate = _model_switch_gate(preparation.thread_id)
    async with gate:
        _require_current_model_switch_generation(
            preparation.thread_id,
            preparation.generation,
        )
        return await run_in_db(
            _commit_model_switch_transaction,
            preparation,
            user_id=user_id,
            now=now or utc_now_iso(),
            reason=reason,
            actor_thread_id=actor_thread_id,
            actor_title=actor_title,
        )
