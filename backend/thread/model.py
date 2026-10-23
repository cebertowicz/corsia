"""The single public door for every thread model selection."""

from __future__ import annotations

import logging
import uuid

from infra.model_switch_types import SwitchRequest
from infra.time import utc_now_iso
from thread.model_commit import commit_model_switch
from thread.model_prepare import prepare_model_switch
from thread.model_row import (
    _model_switch_noop_outcome,
    _read_noop_model_switch_version,
    select_first_model,
)
from thread.model_source import _capture_model_switch_source_owner
from thread.model_state import (
    ModelSwitchConflict,
    ModelSwitchOutcome,
    ModelSwitchPreparation,
    ModelSwitchPreparationError,
    _model_switch_gate,
)
from thread.model_runtime import post_model_switch_effects

log = logging.getLogger(__name__)


async def _read_noop_model_switch_outcome(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    now: str | None,
) -> ModelSwitchOutcome:
    from infra.db_async import run_in_db

    async with _model_switch_gate(thread_id):
        committed_at = await run_in_db(
            _read_noop_model_switch_version,
            thread_id,
            destination_model,
            user_id=user_id,
            now=now or utc_now_iso(),
        )
    return _model_switch_noop_outcome(
        destination_model,
        committed_at=committed_at,
    )


async def _refresh_source_owner(
    preparation: ModelSwitchPreparation,
) -> tuple[ModelSwitchPreparation, object | None]:
    current = await _capture_model_switch_source_owner(
        preparation.thread_id,
        preparation.source_harness,
    )
    prepared = preparation.source_runtime_owner
    if prepared is None or current == prepared:
        return preparation, current
    refreshed = await prepare_model_switch(
        preparation.thread_id,
        preparation.destination_model,
        user_id=preparation.user_id,
        allow_live_same_harness=False,
    )
    if refreshed is None:
        raise ModelSwitchConflict("model switch became a no-op during commit")
    current = await _capture_model_switch_source_owner(
        refreshed.thread_id,
        refreshed.source_harness,
    )
    return refreshed, current


async def _commit_and_apply(
    preparation: ModelSwitchPreparation,
    *,
    user_id: str | None,
    requested_at: str,
    reason: str | None,
    actor_thread_id: str | None,
    actor_title: str | None,
) -> tuple[object, str | None]:
    preparation, owner = await _refresh_source_owner(preparation)
    commit = await commit_model_switch(
        preparation,
        user_id=user_id,
        now=requested_at,
        reason=reason,
        actor_thread_id=actor_thread_id,
        actor_title=actor_title,
    )
    try:
        await post_model_switch_effects(
            preparation.thread_id,
            new_model=preparation.destination_model,
            user_id=user_id,
            source_harness=preparation.source_harness,
            source_runtime_owner=owner,
            destination_target=preparation.destination_target,
            operation_id=(
                preparation.input_proof.operation_id
                if preparation.input_proof is not None
                else None
            ),
            source_runtime_context_epoch=preparation.source_runtime_context_epoch,
            expected_context_usage_floor=preparation.source_context_usage_floor,
            preserve_claude_thread=(
                preparation.projection_mode == "provider_resume"
                and preparation.source_harness == "claude-code"
            ),
            preserve_codex_thread=(
                preparation.projection_mode == "provider_resume"
                and preparation.source_harness == "codex"
            ),
            breadcrumb=commit.breadcrumb,
            committed_at=commit.committed_at,
        )
    except Exception as error:
        log.exception("model switch post-commit effects failed: %s", preparation.thread_id)
        return (preparation, commit), str(error)
    return (preparation, commit), None


async def prepare_and_commit_model_switch(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    now: str | None = None,
    reason: str | None = None,
    actor_thread_id: str | None = None,
    actor_title: str | None = None,
    _prepared: ModelSwitchPreparation | None = None,
) -> ModelSwitchOutcome:
    """Commit one already-fitting destination and then transition its runtime."""
    preparation = _prepared or await prepare_model_switch(
        thread_id,
        destination_model,
        user_id=user_id,
    )
    if preparation is None:
        return await _read_noop_model_switch_outcome(
            thread_id,
            destination_model,
            user_id=user_id,
            now=now,
        )
    (preparation, commit), post_error = await _commit_and_apply(
        preparation,
        user_id=user_id,
        requested_at=now or utc_now_iso(),
        reason=reason,
        actor_thread_id=actor_thread_id,
        actor_title=actor_title,
    )
    return ModelSwitchOutcome(
        changed=True,
        old_model=preparation.source_selected_model,
        new_model=destination_model,
        preparation=preparation,
        breadcrumb=commit.breadcrumb,
        committed_at=commit.committed_at,
        post_effects_error=post_error,
    )


async def _inline_first_run_wake(thread_id: str, model: str) -> str | None:
    """The first-run wake with its instructions and facts, when ``thread_id`` holds one.

    One path: every fact renders, an unavailable one with the read that gets it
    (see ``setup_facts``), and the instructions are the served file. The facts
    are bounded and read before the model-switch gate is taken. An error that
    still escapes fails the pick with a logged traceback, never a different wake.
    """
    from infra.database import get_db
    from infra.db_async import run_in_db
    from infra.onboarding_events import held_first_run_path
    from onboarding import first_run_wake

    def _held_path() -> str | None:
        db = get_db()
        try:
            return held_first_run_path(thread_id, db=db)
        finally:
            db.close()

    path = await run_in_db(_held_path)
    if path is None:
        return None
    if path == "app":
        return await first_run_wake.render_for(model, path, thread_id=thread_id)
    return await first_run_wake.render_for(model, path)


async def _apply_first_selection(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    now: str,
    account_id: str | None = None,
    actor_thread_id: str | None = None,
) -> ModelSwitchOutcome | None:
    """Commit a thread's first model directly, then publish and wake it."""
    from infra.db_async import run_in_db
    from infra.thread_execution import drive_thread
    from thread.model_runtime import publish_first_model_selection

    first_run_wake = await _inline_first_run_wake(thread_id, destination_model.strip())
    async with _model_switch_gate(thread_id):
        selected = await run_in_db(
            select_first_model,
            thread_id,
            destination_model.strip(),
            user_id=user_id,
            now=now,
            account_id=account_id,
            actor_thread_id=actor_thread_id,
            first_run_wake=first_run_wake,
        )
    if not selected:
        return None
    from infra.onboarding_events import note_first_model_selected
    from onboarding.first_run_opening import opening_message_id
    from onboarding.local_download_wake import flush_pending_local_download
    from infra.send_broadcast import broadcast_message_rows

    post_error: str | None = None
    if first_run_wake is not None:
        # Publish the whole committed opening before this path drives the wake.
        # Script/restored paths with no app opening simply project no row.
        try:
            await broadcast_message_rows(thread_id, (opening_message_id(thread_id),))
        except Exception:
            log.exception("first-run opening broadcast failed: %s", thread_id)
            post_error = "Your model is selected, but this chat didn't refresh. Reopen it to see the latest."

    # ``select_first_model`` committed the rendered first-run wake above. A setup
    # download that ended while the person was still in setup can now be delivered.
    await flush_pending_local_download(thread_id)
    await run_in_db(note_first_model_selected, thread_id, destination_model.strip())
    publish_first_model_selection(
        thread_id, new_model=destination_model.strip(), user_id=user_id, committed_at=now,
    )
    await drive_thread(thread_id)
    return ModelSwitchOutcome(
        changed=True,
        old_model=None,
        new_model=destination_model.strip(),
        preparation=None,
        breadcrumb=None,
        committed_at=now,
        post_effects_error=post_error,
    )


async def switch_model(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    actor_kind: str,
    actor_id: str | None,
    reason: str,
    checkpoint_override: str | None = None,
    account_id: str | None = None,
) -> ModelSwitchOutcome | SwitchRequest | object:
    """Freeze one FIFO cut, then resolve it now or at its next natural wake."""
    from infra.db_async import run_in_db
    from infra.model_switches import request_switch
    from thread.model_runtime import broadcast_pending_switch, broadcast_switch_inputs

    requested_at = utc_now_iso()
    first = await _apply_first_selection(
        thread_id, destination_model, user_id=user_id, now=requested_at,
        account_id=account_id, actor_thread_id=actor_id if actor_kind == "agent" else None,
    )
    if first is not None:
        return first
    choice = "target" if checkpoint_override == "target" else "current"
    try:
        request = await run_in_db(
            request_switch,
            thread_id,
            destination_model,
            operation_id=str(uuid.uuid4()),
            requested_at=requested_at,
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason=reason,
            checkpoint_choice=choice,
            user_id=user_id,
            account_id=account_id,
        )
    except ValueError as error:
        raise ModelSwitchPreparationError(str(error)) from error
    except RuntimeError as error:
        raise ModelSwitchConflict(str(error)) from error
    if request is None:
        return await _read_noop_model_switch_outcome(
            thread_id,
            destination_model,
            user_id=user_id,
            now=requested_at,
        )
    broadcast_pending_switch(request)
    await broadcast_switch_inputs(request)
    if request.waiting_for_source:
        from infra.thread_execution import drive_idle_thread

        await drive_idle_thread(thread_id)
        return request
    from thread.model_pending import drive_pending_model_switch

    return await drive_pending_model_switch(thread_id) or request
