"""Preparation of an already-valid destination model projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from core.checkpoint_fit import at_checkpoint_launch_point, soft_pressure_remaining
from thread.model_projection import (
    _default_model_switch_renderer,
    _live_model_switch_fit,
    _projection_proof,
    _render_model_switch_projection,
)
from thread.model_source import (
    _capture_model_switch_source_owner,
    _live_same_harness_context_usage,
    _read_model_switch_candidates,
    _read_model_switch_start,
    _unresolved_source_harness,
)
from thread.model_state import (
    ModelSwitchCheckpointRequired,
    ModelSwitchInputProof,
    ModelSwitchPreparation,
    ModelSwitchPreparationError,
    ModelSwitchProjectionProof,
    ModelSwitchRenderer,
    _advance_model_switch_generation,
    _model_switch_gate,
)


@dataclass(frozen=True, slots=True)
class _PreparationContext:
    thread_id: str
    user_id: str | None
    generation: int
    source_model: str
    source_selected_model: str | None
    source_target: Any
    source_harness: str
    destination_model: str
    destination_target: Any
    project_id: str | None
    source_runtime_context_epoch: int
    destructive_version: tuple[int, int, int]
    source_active_turn_id: str | None
    input_proof: ModelSwitchInputProof | None = None

    def build(
        self,
        *,
        history_epoch: int,
        sealed_boundary,
        transcript_version,
        projection_mode: str,
        projection_proof: ModelSwitchProjectionProof,
        destination_fit_ledger,
        source_runtime_owner=None,
        source_context_usage_floor=None,
    ) -> ModelSwitchPreparation:
        return ModelSwitchPreparation(
            thread_id=self.thread_id,
            user_id=self.user_id,
            generation=self.generation,
            source_model=self.source_model,
            source_selected_model=self.source_selected_model,
            source_harness=self.source_harness,
            destination_model=self.destination_model,
            destination_target=self.destination_target,
            project_id=self.project_id,
            source_runtime_context_epoch=self.source_runtime_context_epoch,
            source_runtime_owner=source_runtime_owner,
            history_epoch=history_epoch,
            sealed_boundary=sealed_boundary,
            destructive_version=self.destructive_version,
            transcript_version=transcript_version,
            source_active_turn_id=self.source_active_turn_id,
            projection_mode=projection_mode,
            projection_proof=projection_proof,
            destination_fit_ledger=destination_fit_ledger,
            source_context_usage_floor=source_context_usage_floor,
            input_proof=self.input_proof,
        )


def _provider_resume_proof() -> ModelSwitchProjectionProof:
    return ModelSwitchProjectionProof(
        mode="provider_resume",
        checkpoint_id=None,
        checkpoint_boundary=None,
    )


async def _live_provider_resume(
    context: _PreparationContext,
    *,
    candidates,
    transcript_version,
    fit_input,
    live_context_usage,
    allowed: bool,
    step: list[str],
) -> ModelSwitchPreparation | None:
    if not allowed:
        step.append("live_not_allowed")
        return None
    if live_context_usage is None:
        step.append("no_live_usage")
        return None
    live_fit = _live_model_switch_fit(
        thread_id=context.thread_id,
        destination_target=context.destination_target,
        live_context_usage=live_context_usage,
        fit_input=fit_input,
    )
    if live_fit.status != "allowed":
        # The ledger's own input and effective window decide the refusal; the
        # catalog window alone can read as fitting.
        ledger = live_fit.prepared_context.fit_ledger
        step.append(
            f"live_fit_blocked(status={live_fit.status} used={live_context_usage}"
            f" input={ledger.rendered_input_tokens}"
            f" effective_window={ledger.effective_window_tokens}"
            f" window={context.destination_target.context_window})"
        )
        raise ModelSwitchCheckpointRequired(
            context.source_model,
            context.destination_model,
        )
    owner = await _capture_model_switch_source_owner(
        context.thread_id,
        context.source_harness,
    )
    if owner is None:
        step.append("no_live_owner")
        return None
    prepared = live_fit.prepared_context
    if prepared is None or prepared.fit_ledger is None:
        raise ModelSwitchPreparationError(
            "model switch live fit omitted its destination ledger"
        )
    return context.build(
        history_epoch=candidates.history_epoch,
        sealed_boundary=candidates.sealed_boundary,
        transcript_version=transcript_version,
        projection_mode="provider_resume",
        projection_proof=_provider_resume_proof(),
        destination_fit_ledger=prepared.fit_ledger,
        source_runtime_owner=owner,
        source_context_usage_floor=live_context_usage,
    )


async def _prepared_projection(
    context: _PreparationContext,
    *,
    renderer: ModelSwitchRenderer,
    selection,
    transcript_version,
    fit_input,
    step: list[str],
) -> ModelSwitchPreparation | None:
    if selection is None:
        return None
    result = await _render_model_switch_projection(
        renderer=renderer,
        thread_id=context.thread_id,
        source_model=context.source_model,
        source_selected_model=context.source_selected_model,
        destination_target=context.destination_target,
        selection=selection,
        fit_input=fit_input,
    )
    prepared = result.prepared_context
    if prepared is None:
        step.append("replay_unrendered")
        return None
    ledger = prepared.fit_ledger
    if ledger is None:
        raise ModelSwitchPreparationError(
            "model switch renderer omitted its destination fit ledger"
        )
    window = ledger.effective_window_tokens
    launch_point = window - soft_pressure_remaining(window)
    if at_checkpoint_launch_point(prepared.rendered.input_tokens, window):
        step.append(
            f"replay_over_launch_point(mode={selection.mode}"
            f" input={prepared.rendered.input_tokens}"
            f" launch_point={launch_point} effective_window={window})"
        )
        return None
    return context.build(
        history_epoch=selection.history_epoch,
        sealed_boundary=selection.sealed_boundary,
        transcript_version=transcript_version,
        projection_mode=selection.mode,
        projection_proof=_projection_proof(selection),
        destination_fit_ledger=ledger,
    )


async def _read_preparation_context(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    allow_active_turn: bool,
) -> _PreparationContext | None:
    from infra.db_async import run_in_db

    gate = _model_switch_gate(thread_id)
    async with gate:
        start = await run_in_db(
            _read_model_switch_start,
            thread_id,
            destination_model,
            user_id=user_id,
            allow_active_turn=allow_active_turn,
        )
        generation = _advance_model_switch_generation(thread_id)
    (
        source_selected_model,
        source_model,
        source_target,
        destination_target,
        project_id,
        source_runtime_context_epoch,
        destructive_version,
        source_active_turn_id,
    ) = start
    if source_selected_model == destination_model:
        return None
    source_harness = (
        source_target.harness
        if source_target is not None
        else _unresolved_source_harness(thread_id)
    )
    return _PreparationContext(
        thread_id=thread_id,
        user_id=user_id,
        generation=generation,
        source_model=source_model,
        source_selected_model=source_selected_model,
        source_target=source_target,
        source_harness=source_harness,
        destination_model=destination_model,
        destination_target=destination_target,
        project_id=project_id,
        source_runtime_context_epoch=source_runtime_context_epoch,
        destructive_version=destructive_version,
        source_active_turn_id=source_active_turn_id,
    )


async def _require_live_destination_fit(
    context: _PreparationContext,
) -> int | float | None:
    live_context_usage = await _live_same_harness_context_usage(
        context.thread_id,
        source_harness=context.source_harness,
        source_target=context.source_target,
        destination_target=context.destination_target,
        source_runtime_context_epoch=context.source_runtime_context_epoch,
    )
    return live_context_usage


async def _prepare_replay_candidate(
    context: _PreparationContext,
    *,
    renderer: ModelSwitchRenderer,
    candidates,
    transcript_version,
    fit_input,
    step: list[str],
) -> ModelSwitchPreparation | None:
    selection = candidates.checkpoint_tail or candidates.exact
    if selection is None:
        last_resort = getattr(candidates.last_resort, "tail", None)
        stubs = len(getattr(last_resort, "retention_stub_seqs", ()) or ())
        step.append(f"no_replay_candidate(stubs={stubs})")
        return None
    return await _prepared_projection(
        context,
        renderer=renderer,
        selection=selection,
        transcript_version=transcript_version,
        fit_input=fit_input,
        step=step,
    )


def _checkpoint_required(
    context: _PreparationContext, reasons: list[str]
) -> ModelSwitchCheckpointRequired:
    # Counts, modes and model names only; never message text.
    logger.info(
        "model switch needs checkpoint: thread={} source={} destination={} "
        "lengths={} reasons={}",
        context.thread_id,
        context.source_model,
        context.destination_model,
        len(reasons),
        reasons,
    )
    return ModelSwitchCheckpointRequired(
        context.source_model,
        context.destination_model,
        reasons=tuple(reasons),
    )


async def prepare_model_switch(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    render_destination: ModelSwitchRenderer | None = None,
    allow_live_same_harness: bool = True,
    allow_active_turn: bool = False,
) -> ModelSwitchPreparation | None:
    """Prepare only an already-valid destination without starting paid work."""
    destination_model = str(destination_model or "").strip()
    if not destination_model:
        raise ModelSwitchPreparationError("model switch destination model is required")
    renderer = render_destination or _default_model_switch_renderer
    context = await _read_preparation_context(
        thread_id,
        destination_model,
        user_id=user_id,
        allow_active_turn=allow_active_turn,
    )
    if context is None:
        return None
    from infra.db_async import run_in_db

    candidates, transcript_version, fit_input, input_proof, destination_target = await run_in_db(
        _read_model_switch_candidates,
        context.thread_id,
        user_id=context.user_id,
        source_selected_model=context.source_selected_model,
        source_model=context.source_model,
        project_id=context.project_id,
        expected_version=context.destructive_version,
        destination_model=context.destination_model,
        destination_target=context.destination_target,
        allow_active_turn=allow_active_turn,
    )
    # A catalog refresh may have moved the window since the start read; fit uses this one.
    context = replace(context, destination_target=destination_target)
    live_context_usage = await _require_live_destination_fit(context)
    # Preserve the ordinary Send rule: a shortened prefix ends at apply_now,
    # never at queued-only context. The full existing projection remains first.
    lengths = [len(fit_input.receipts)] + [
        index for index in range(len(fit_input.receipts) - 1, 0, -1)
        if fit_input.receipts[index - 1].intent == "apply_now"
    ]
    reasons: list[str] = []
    for index, length in enumerate(lengths):
        step: list[str] = []
        if index:
            candidates, transcript_version, fit_input, input_proof, destination_target = await run_in_db(
                _read_model_switch_candidates, context.thread_id,
                user_id=context.user_id, source_selected_model=context.source_selected_model,
                source_model=context.source_model, project_id=context.project_id,
                expected_version=context.destructive_version,
                destination_model=context.destination_model, destination_target=context.destination_target,
                allow_active_turn=allow_active_turn, max_items=length,
            )
        context = replace(context, destination_target=destination_target, input_proof=input_proof)
        try:
            live_resume = await _live_provider_resume(
                context, candidates=candidates, transcript_version=transcript_version,
                fit_input=fit_input, live_context_usage=live_context_usage,
                allowed=allow_live_same_harness, step=step,
            )
        except ModelSwitchCheckpointRequired:
            reasons.append("+".join(step))
            continue
        if live_resume is not None:
            return live_resume
        prepared = await _prepare_replay_candidate(
            context, renderer=renderer, candidates=candidates,
            transcript_version=transcript_version, fit_input=fit_input, step=step,
        )
        if prepared is not None:
            return prepared
        reasons.append("+".join(step))
    raise _checkpoint_required(context, reasons)
