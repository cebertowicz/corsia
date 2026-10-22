"""Resolve one durable model switch at its existing FIFO boundary."""

from __future__ import annotations

from infra.model_switch_types import SwitchRequest, SwitchTransition
from thread.model_prepare import prepare_model_switch
from thread.model_state import (
    ModelSwitchCheckpointRequired,
    ModelSwitchConflict,
    ModelSwitchPreparationError,
    ModelSwitchSequencingRequired,
    ModelSwitchSuperseded,
)


def _request_context(thread_id: str, actor_id: str | None) -> tuple[str, str | None]:
    from infra.database import get_db

    db = get_db()
    try:
        thread_row = db.execute(
            "SELECT user_id FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        if thread_row is None or not str(thread_row["user_id"] or "").strip():
            raise ModelSwitchConflict("pending model switch lost its owning user")
        actor = None
        if actor_id is not None:
            row = db.execute(
                "SELECT title FROM threads WHERE id=?", (actor_id,),
            ).fetchone()
            actor = None if row is None else str(row["title"] or "")
        return str(thread_row["user_id"]), actor
    finally:
        db.close()


async def _commit_fitting_switch(request: SwitchRequest):
    from infra.db_async import run_in_db
    from thread.model import prepare_and_commit_model_switch

    user_id, actor_title = await run_in_db(
        _request_context, request.thread_id, request.actor_id,
    )
    preparation = await prepare_model_switch(
        request.thread_id,
        request.destination_model,
        user_id=user_id,
    )
    if preparation is None:
        raise ModelSwitchConflict("pending model switch became current outside its owner")
    return await prepare_and_commit_model_switch(
        request.thread_id,
        request.destination_model,
        user_id=user_id,
        reason=request.reason,
        actor_thread_id=request.actor_id if request.actor_kind == "agent" else None,
        actor_title=actor_title,
        _prepared=preparation,
    )


async def _drive_destination(thread_id: str) -> None:
    from infra.thread_execution import drive_idle_thread

    await drive_idle_thread(thread_id)


def _checkpoint_state(request: SwitchRequest):
    from infra.checkpoint_queries import active_for_thread
    from thread.model_checkpoint import relevant_checkpoint

    return active_for_thread(request.thread_id), relevant_checkpoint(request)


async def _start_checkpoint(request: SwitchRequest, mode: str):
    from core.checkpoint_runtime import CheckpointTargetLimited, request_checkpoint
    from thread.model_checkpoint import (
        new_checkpoint_id,
        prepare_source_checkpoint,
        prepare_target_checkpoint,
    )

    checkpoint_id = new_checkpoint_id()
    admission = (
        await prepare_source_checkpoint(request, checkpoint_id)
        if mode == "warm"
        else await prepare_target_checkpoint(request, checkpoint_id)
    )
    if admission is None:
        return None, False
    try:
        record = await request_checkpoint(admission.request, admission.provider_request)
    except CheckpointTargetLimited as error:
        if mode != "warm":
            raise
        from loguru import logger

        logger.info(
            "model switch warm fold skipped: cause=source_target_limited thread={} {}",
            request.thread_id, error,
        )
        return None, False
    installed_winner = record.id != checkpoint_id and record.status == "installed"
    return record, installed_winner


async def _fail_switch(
    request: SwitchRequest,
    error: BaseException | str,
) -> SwitchTransition | SwitchRequest | None:
    from infra.db_async import run_in_db
    from infra.model_switches import fail_switch, read_switch
    from thread.model_runtime import broadcast_pending_switch

    detail = str(error).strip() or type(error).__name__
    try:
        failed = await run_in_db(fail_switch, request.operation_id, error=detail)
    except ValueError:
        current = await run_in_db(read_switch, request.thread_id)
        if current is None or current.operation_id != request.operation_id:
            return current  # A replacement's request owns its own preparation.
        raise
    # Publish the cause the database holds; a first failure's cause outranks ours.
    broadcast_pending_switch(request, error=failed.error or detail)
    # Inputs the failure returned to the source have no other wake (R16).
    from infra.thread_execution import drive_thread

    await drive_thread(request.thread_id)
    return failed


async def _fail_preparation(request: SwitchRequest, error: Exception, *, stage: str):
    """Fail one preparation stage, retaining an unexpected error's traceback."""
    from core.fork_fit import ColdFoldInputTooLarge
    from core.checkpoint_runtime import CheckpointTargetLimited

    if not isinstance(error, (ColdFoldInputTooLarge, CheckpointTargetLimited)):
        from loguru import logger

        logger.opt(exception=error).error(
            "model switch preparation failed: stage={} thread={} operation={}",
            stage, request.thread_id, request.operation_id,
        )
    return await _fail_switch(request, error)


async def _resolve_required_checkpoint(request: SwitchRequest):
    """Start required work or signal one installed winner for immediate recheck."""
    from infra.db_async import run_in_db

    active, attempt = await run_in_db(_checkpoint_state, request)
    if active is not None:
        from core.checkpoint_runtime import cancel_checkpoint
        from thread.model_checkpoint import obsolete_source_checkpoint

        if await run_in_db(obsolete_source_checkpoint, request, active):
            cancelled = await cancel_checkpoint(active.id)
            if cancelled.status == "installed":
                return None  # Installation won cancellation: re-prove the destination.
            _started, installed_winner = await _start_checkpoint(request, "cold")
            return None if installed_winner else request
        return request
    if attempt is not None:
        if attempt.mode == "warm" and attempt.status == "failed":
            _started, installed_winner = await _start_checkpoint(request, "cold")
            return None if installed_winner else request
        if attempt.status in {"running", "captured"}:
            return request
        raise ModelSwitchPreparationError(
            attempt.error or f"checkpoint ended {attempt.status}",
        )
    started, installed_winner = await _start_checkpoint(request, "warm")
    if started is None:
        _started, installed_winner = await _start_checkpoint(request, "cold")
    return None if installed_winner else request


async def drive_pending_model_switch(thread_id: str):
    """Make every uncommitted preparation error fail the current switch visibly."""
    from infra.db_async import run_in_db
    from infra.model_switches import advance_pending_switch, read_switch
    from thread.model_runtime import broadcast_switch_inputs

    request = await run_in_db(advance_pending_switch, thread_id)
    await broadcast_switch_inputs(request)
    if request is None:
        return None
    if request.waiting_for_source:
        await _drive_destination(thread_id)
        return request
    rechecked_installed_winner = False
    rechecked_sequencing = False
    while True:
        try:
            outcome = await _commit_fitting_switch(request)
            break
        except ModelSwitchCheckpointRequired as error:
            if rechecked_installed_winner:
                return await _fail_switch(request, error)
            try:
                pending = await _resolve_required_checkpoint(request)
            except (ModelSwitchPreparationError, ModelSwitchConflict) as resolution_error:
                return await _fail_switch(request, resolution_error)
            except Exception as resolution_error:
                return await _fail_preparation(
                    request, resolution_error, stage="required checkpoint resolution",
                )
            if pending is not None:
                return pending
            rechecked_installed_winner = True
        except ModelSwitchSuperseded:
            return await run_in_db(read_switch, thread_id)
        except ModelSwitchSequencingRequired as error:
            try:
                current = await run_in_db(advance_pending_switch, thread_id)
                await broadcast_switch_inputs(current)
            except Exception as reread_error:
                return await _fail_preparation(
                    request, reread_error, stage="sequencing re-read",
                )
            if current is None:
                return None
            if current.waiting_for_source:
                await _drive_destination(thread_id)
                return current  # The active Turn or healthy FIFO head owns the next wake.
            if rechecked_sequencing:
                if current.operation_id != request.operation_id:
                    return current
                return await _fail_switch(current, error)
            request = current
            rechecked_sequencing = True
        except (ModelSwitchPreparationError, ModelSwitchConflict) as error:
            return await _fail_switch(request, error)
        except Exception as error:
            return await _fail_preparation(request, error, stage="commit attempt")
    await _drive_destination(thread_id)
    return outcome
