"""Destination rendering and fit proof for model selection."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from infra.input_render import InputProviderProjection, project_claimed_input
from thread.model_state import (
    ModelSwitchConflict,
    ModelSwitchInputProof,
    ModelSwitchPreparationError,
    ModelSwitchProjectionProof,
    ModelSwitchRenderer,
)


def _projection_proof(selection: Any) -> ModelSwitchProjectionProof:
    return ModelSwitchProjectionProof(
        mode=selection.mode,
        checkpoint_id=selection.checkpoint_generation_id,
        checkpoint_boundary=selection.checkpoint_boundary,
    )


def read_model_switch_fit_input(
    db,
    thread_id: str,
    *,
    destination_model: str,
    destination_target: Any,
    max_items: int | None = None,
) -> InputProviderProjection:
    """Project the exact provider input that will follow the model commit."""
    from core.media_projection import MediaProjectionError

    try:
        return project_model_switch_input(
            db,
            thread_id,
            destination_model=destination_model,
            destination_target=destination_target,
            max_items=max_items,
        )
    except MediaProjectionError as exc:
        raise ModelSwitchPreparationError(
            "model switch queued attachment cannot reach the destination: "
            f"{exc.detail}"
        ) from exc
    except ValueError as exc:
        raise ModelSwitchConflict(str(exc)) from exc


def project_model_switch_input(
    db,
    thread_id: str,
    *,
    destination_model: str,
    destination_target,
    max_items: int | None = None,
) -> InputProviderProjection:
    """Project the destination epoch directly from canonical message rows."""
    from core.message_history import render_agent_envelope
    from infra.thread_inputs import preview_input_batch

    thread_row = db.execute(
        "SELECT * FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    if thread_row is None:
        raise ValueError(f"model switch task does not exist: {thread_id}")
    epoch = thread_row["pending_execution_epoch"]
    model = thread_row["pending_model"]
    if epoch is None or str(model or "") != destination_model:
        raise ValueError("model switch destination is not the pending FIFO epoch")
    batch = preview_input_batch(
        thread_id,
        execution_epoch=int(epoch),
        model=str(model),
        db=db,
    )
    if batch is not None and max_items is not None:
        batch = replace(batch, items=batch.items[:max_items]) if max_items else None
    if batch is None:
        envelope = render_agent_envelope("", None)
        return InputProviderProjection(
            f"model-switch:{thread_id}:empty",
            "",
            "",
            envelope,
            (),
            None,
            (),
        )
    return project_claimed_input(
        db,
        batch,
        thread_row=thread_row,
        target=destination_target,
    )


def read_model_switch_input_proof(db, thread_id: str, fit_input) -> ModelSwitchInputProof:
    """Bind rendered members to the switch operation in the same snapshot."""
    row = db.execute(
        "SELECT switch_operation_id,pending_execution_epoch FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    if row is None or row["switch_operation_id"] is None or row["pending_execution_epoch"] is None:
        raise ModelSwitchConflict("model switch lost its pending input boundary")
    return ModelSwitchInputProof(
        str(row["switch_operation_id"]), int(row["pending_execution_epoch"]),
        tuple((item.message_id, item.input_order, item.revision) for item in fit_input.receipts),
    )


def _validate_switch_destination(
    prepared: Any,
    *,
    destination_target: Any,
    selection: Any,
    fit_input: InputProviderProjection,
) -> None:
    from core.destination_fit import effective_destination_window_tokens

    ledger = prepared.fit_ledger
    if ledger is None:
        raise ModelSwitchPreparationError(
            "model switch renderer returned a mismatched destination projection"
        )
    prepared_mode = (
        "exact_replay" if selection.mode == "exact" else selection.mode
    )
    target_window = destination_target.context_window
    effective_window = effective_destination_window_tokens(target_window)
    replay_proof = (
        _projection_proof(prepared.replay)
        if prepared.replay is not None
        else None
    )
    actual = (
        prepared.mode, prepared.target_catalog_id, ledger.harness,
        ledger.target_catalog_id, ledger.proven_target_window_tokens,
        ledger.effective_window_tokens, prepared.model_context_window,
        prepared.history_epoch, prepared.sealed_boundary, replay_proof,
        ledger.input_identity,
    )
    expected = (
        prepared_mode, destination_target.catalog_id, destination_target.harness,
        destination_target.catalog_id, target_window, effective_window,
        effective_window, selection.history_epoch, selection.sealed_boundary,
        _projection_proof(selection), fit_input.identity,
    )
    if actual != expected:
        raise ModelSwitchPreparationError(
            "model switch renderer returned a mismatched destination projection"
        )


async def _render_model_switch_projection(
    *,
    renderer: ModelSwitchRenderer,
    thread_id: str,
    source_model: str,
    source_selected_model: str | None,
    destination_target: Any,
    selection: Any,
    fit_input: InputProviderProjection,
):
    from core.destination_preflight import (
        attach_destination_fit_ledger,
        result_for_prepared_context,
    )

    try:
        prepared = await renderer(
            thread_id=thread_id,
            current_model=source_model,
            expected_selected_model=source_selected_model,
            target=destination_target,
            replay=selection,
            fit_input=fit_input,
        )
    except Exception as exc:
        raise ModelSwitchPreparationError(
            f"model switch destination rendering failed: {exc}"
        ) from exc
    if prepared.fit_ledger is None:
        prepared = attach_destination_fit_ledger(
            prepared,
            harness=destination_target.harness,
            proven_target_window_tokens=destination_target.context_window,
        )
    _validate_switch_destination(
        prepared,
        destination_target=destination_target,
        selection=selection,
        fit_input=fit_input,
    )
    return result_for_prepared_context(prepared)


async def _default_model_switch_renderer(
    *,
    thread_id: str,
    current_model: str,
    expected_selected_model: str | None,
    target: Any,
    replay: Any,
    fit_input: InputProviderProjection,
):
    """Render through Task 9's one provider-specific destination seam."""
    from infra.database import get_db
    from infra.db_async import run_in_db
    import infra.preflight_render as preflight_render

    def _read_thread_row():
        db = get_db()
        try:
            return db.execute(
                "SELECT * FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        finally:
            db.close()

    thread_row = await run_in_db(_read_thread_row)
    if (
        thread_row is None
        or (str(thread_row["model"] or "").strip() or None) != expected_selected_model
    ):
        raise ModelSwitchConflict(
            "model switch source changed before destination rendering"
        )
    provider = {
        "native": "native",
        "claude-code": "cc",
        "codex": "codex",
    }[target.harness]
    prepared = await preflight_render.render_preflight_selection(
        thread_id=thread_id,
        thread_row=thread_row,
        provider=provider,
        target=target,
        current_user_content=fit_input.content,
        current_user_source_content=(
            fit_input.source_content or fit_input.content
        ),
        agent_envelope=fit_input.agent_envelope,
        attachments=list(fit_input.attachments),
        media_projection=fit_input.media_projection,
        selection=replay,
        live_handle=False,
        expected_selected_model=expected_selected_model or "",
        input_identity=fit_input.identity,
    )
    return prepared


def _live_model_switch_fit(
    *,
    thread_id: str,
    destination_target: Any,
    live_context_usage: int,
    fit_input: InputProviderProjection,
):
    """Admit one measured provider-resume payload through the shared ledger."""
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from core.destination_preflight import (
        attach_destination_fit_ledger,
        result_for_prepared_context,
    )
    from core.token_estimation import (
        _text_tokens,
        estimate_media_projection_tokens,
    )

    current_input_tokens = (
        _text_tokens(fit_input.content, destination_target.expected_reported_model or destination_target.transport_model)
        + estimate_media_projection_tokens(fit_input.media_projection)
    )
    empty_sections = {
        "fixed": 0,
        "checkpoint": 0,
        "tail": 0,
        "current_input": current_input_tokens,
        "tools": 0,
        "provider_wrapper": 0,
    }
    rendered = RenderedDestinationPayload(
        payload={"mode": "provider_resume", "thread_id": thread_id},
        input_tokens=current_input_tokens,
        accounting_payload={
            "mode": "provider_resume",
            "thread_id": thread_id,
            "current_input": fit_input.content,
            "attachments": list(fit_input.attachments),
        },
        fit_section_tokens=empty_sections,
        fit_input_identity=fit_input.identity,
        fit_current_input=fit_input.content,
        fit_components={
            "fixed": {"measured_live_context_tokens": live_context_usage},
            "checkpoint": None,
            "tail": None,
            "current_input": fit_input.content,
            "tools": (),
        },
        fit_retained_provider_tokens=int(live_context_usage),
    )
    prepared = PreparedContextPayload(
        mode="live_handle",
        target_catalog_id=destination_target.catalog_id,
        model_context_window=destination_target.context_window,
        history_epoch=0,
        sealed_boundary=None,
        replay=None,
        rendered=rendered,
    )
    prepared = attach_destination_fit_ledger(
        prepared,
        harness=destination_target.harness,
        proven_target_window_tokens=destination_target.context_window,
    )
    return result_for_prepared_context(prepared)
