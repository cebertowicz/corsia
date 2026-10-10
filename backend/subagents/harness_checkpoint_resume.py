"""Checkpoint and replace one settled external-harness provider handle."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from subagents.harness_checkpoint_cold_admission import build_external_cold_admission
from subagents.harness_checkpoint_context import (
    ExternalCheckpointContext,
    external_measurement,
    read_boundary_contexts,
    read_external_checkpoint_context,
)


async def render_external_selection(
    context: ExternalCheckpointContext,
    identity,
    selection,
    input_identity: str,
):
    from history.adapters import mid_turn_resume_text
    from infra.preflight_render import render_preflight_selection

    anchor = mid_turn_resume_text(selection.tail.resume_presence_line)
    provider = "cc" if context.target.harness == "claude-code" else "codex"
    return await render_preflight_selection(
        thread_id=selection.tail.thread_id,
        thread_row=context.thread_row,
        provider=provider,
        target=context.target,
        current_user_content=anchor,
        current_user_source_content=anchor,
        attachments=[],
        selection=selection,
        live_handle=False,
        expected_selected_model=context.target.catalog_id,
        input_identity=input_identity,
    )


async def _read_external_context(
    live: Any,
    thread_id: str,
    harness: str,
    *,
    current_turn_id: str | None = None,
    exact_history: bool = False,
) -> ExternalCheckpointContext:
    from infra.db_async import run_in_db

    context = await run_in_db(
        read_external_checkpoint_context, thread_id, str(live.catalog_model_id or ""),
        harness, current_turn_id=current_turn_id, exact_history=exact_history,
    )
    if context is None:
        raise RuntimeError("external checkpoint boundary changed before admission")
    return context


async def _prepare_external_checkpoint(
    checkpoint_id: str,
    *,
    context: ExternalCheckpointContext,
    live: Any,
    identity: Any,
    warm_source: Any | None,
    required: bool = True,
):
    boundary = context.boundary
    if warm_source is not None and boundary is not None:
        from dataclasses import replace

        from core.checkpoint_warm_request import build_warm_admission

        admission = build_warm_admission(
            checkpoint_id=checkpoint_id,
            thread_id=boundary.thread_id,
            turn=identity,
            target=context.target,
            boundary=boundary,
            previous_generation_id=context.previous_generation_id,
            working_dir=context.working_dir,
            reasoning_effort=context.reasoning_effort,
            binary=context.binary,
            warm_source=warm_source,
        )
        # Only the provider knows whether this fork can hold its own input.
        # Carry the cold admission this boundary would otherwise have built so
        # an unusable fork corrects its path instead of ending the turn.
        return replace(
            admission,
            provider_request=replace(
                admission.provider_request,
                same_model_cold=partial(
                    asyncio.to_thread, build_external_cold_admission, checkpoint_id,
                    context=context, live=live, identity=identity,
                ),
            ),
        )
    if boundary is None:
        return None
    # An external harness's next fold is its hard wall, which coincides with
    # its own compaction trigger. Without a warm source, skipping this optional
    # fold hands that recovery boundary to the provider; cold-admit it instead.
    # Cold admission reads the database and renders the source; keep it off
    # the event loop, as the warm path's fallback already does.
    return await asyncio.to_thread(
        build_external_cold_admission, checkpoint_id,
        context=context, live=live, identity=identity,
    )


async def _current_external_input(
    context: ExternalCheckpointContext,
    identity: Any,
    measurement: tuple[int | None, int | None],
    complete_input_tokens: int | None = None,
) -> tuple[int | None, Any | None, int | None]:
    """Measure the live call or render the exact context that seeded its handle."""
    from core.context_replay import exact_replay

    measured, measured_window = measurement
    if complete_input_tokens is not None:
        # Current input/output plus actual replies already cover this Step.
        # Do not count it again through the checkpoint-rendered tail.
        return complete_input_tokens, None, measured_window
    if measured is not None:
        return measured + context.tail_tokens, None, measured_window
    installed = context.installed_checkpoint
    if (
        installed is not None
        and context.registered_checkpoint_id == installed.id
    ):
        from core.checkpoint_resume import installed_replay
        from infra.db_async import run_in_db

        selection = await run_in_db(installed_replay, installed)
    else:
        selection = exact_replay(context.history)
    rendered = await render_external_selection(
        context,
        identity,
        selection,
        f"tool-boundary:{identity.turn_id}",
    )
    return None, rendered, measured_window


def _minimum_external_input(prepared, context: ExternalCheckpointContext) -> int:
    fixed = _initial_minimum(prepared) if prepared is not None else 0
    return fixed + context.minimum_tail_tokens


async def _resume_external(
    live: Any,
    thread_id: str,
    prepared,
    harness: str,
) -> None:
    if harness == "claude-code":
        from subagents.cc_runtime import resume_same_turn_after_checkpoint
    else:
        from subagents.codex_runtime import resume_same_turn_after_checkpoint
    await resume_same_turn_after_checkpoint(thread_id, live, prepared)


async def _render_initial_installed(record, *, refresh_prepared):
    from core.checkpoint_resume import (
        CheckpointContinuationError,
        InstalledContinuation,
        installed_replay,
    )
    from infra.db_async import run_in_db

    selection = await run_in_db(installed_replay, record)
    refreshed = await refresh_prepared(selection)
    replay = getattr(refreshed, "replay", None)
    if (
        getattr(refreshed, "mode", None) != "checkpoint_tail"
        or getattr(replay, "checkpoint_generation_id", None) != record.id
    ):
        raise CheckpointContinuationError(
            "checkpoint continuation does not match the installed generation"
        )
    return InstalledContinuation(refreshed.fit_ledger.rendered_input_tokens, refreshed)


def _installed_at_external_boundary(context, correlated, *, must_adopt: bool = False):
    installed = context.installed_checkpoint
    if (
        not must_adopt
        and correlated is None
        and getattr(installed, "id", None) == context.registered_checkpoint_id
    ):
        return None
    return installed


async def _capture_external_tool_boundary_warm_source(
    context, live, thread_id, harness, current_tool_call_id,
):
    """Capture the harness-owned warm source at one settled tool boundary."""
    if harness == "claude-code":
        from core.checkpoint_cc_boundary import capture_tool_boundary_warm_source

        return await capture_tool_boundary_warm_source(
            context, live, thread_id, current_tool_call_id,
        )
    from core.checkpoint_codex_fork import _capture_tool_boundary_warm_source

    return await _capture_tool_boundary_warm_source(context, live, thread_id)


async def maintain_external_tool_boundary(
    live: Any,
    thread_id: str,
    harness: str,
    *,
    current_tool_call_id: str | None = None,
    must_adopt: bool = False,
    complete_input_tokens: int | None = None,
) -> bool:
    """Run the common checkpoint decision after one settled external Step."""
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        InstalledContinuation,
        ProviderBoundary,
        installed_replay,
        maintain_before_provider_call,
    )
    from infra.db_async import run_in_db
    from llm.context_window import effective_usage_window

    identity, scope = live.corsia_turn, getattr(live, "checkpoint_wait_scope", None)
    if identity is None or not isinstance(scope, CheckpointWaitScope):
        raise RuntimeError("external checkpoint boundary has no active Turn scope")
    measurement = external_measurement(live, harness)
    context, checkpoint_context = await read_boundary_contexts(
        _read_external_context, live, thread_id, harness, measurement[0], complete_input_tokens,
    )
    measurement = external_measurement(live, harness, checkpoint_context.target.context_window)
    correlated, current, measured_window = await _current_external_input(
        context, identity, measurement, complete_input_tokens,
    )
    installed = _installed_at_external_boundary(checkpoint_context, correlated, must_adopt=must_adopt)

    async def prepare(checkpoint_id: str, required: bool):
        current_context = await _read_external_context(live, thread_id, harness)
        warm_source = await _capture_external_tool_boundary_warm_source(
            current_context, live, thread_id, harness, current_tool_call_id,
        )
        return await _prepare_external_checkpoint(
            checkpoint_id,
            context=current_context,
            live=live,
            identity=identity,
            warm_source=warm_source,
            required=required,
        )

    async def render(record):
        selection = await run_in_db(installed_replay, record)
        prepared = await render_external_selection(
            checkpoint_context, identity, selection, f"checkpoint:{record.id}"
        )
        return InstalledContinuation(prepared.rendered.input_tokens, prepared)

    window = effective_usage_window(
        checkpoint_context.target.context_window, measured_window,
    )
    continuation = await maintain_before_provider_call(ProviderBoundary(
        thread_id=thread_id,
        turn=identity,
        context_window=window,
        canonical_input_tokens=(
            current.rendered.input_tokens if current is not None else None
        ),
        provider_input_tokens=correlated,
        minimum_input_tokens=_minimum_external_input(current, checkpoint_context),
        adopted_checkpoint_id=checkpoint_context.registered_checkpoint_id,
        active_checkpoint=checkpoint_context.active_checkpoint,
        installed_checkpoint=installed,
        prepare_checkpoint=prepare if checkpoint_context.boundary is not None else None,
        render_installed=render,
        wait_scope=scope,
        eager_checkpoint_id=checkpoint_context.eager_checkpoint_id,
    ), must_adopt=must_adopt)
    if continuation.payload is None:
        return False
    await _resume_external(live, thread_id, continuation.payload, harness)
    return True


def _initial_minimum(prepared) -> int:
    sections = dict(prepared.fit_ledger.section_tokens)
    return sum(int(sections.get(name) or 0) for name in ("current_input", "tools", "provider_wrapper"))


async def _prepare_initial_external_checkpoint(
    checkpoint_id: str, required: bool, *, context: ExternalCheckpointContext,
    live: Any, thread_id: str, identity: Any, prepared,
):
    warm_source = None
    if getattr(prepared, "mode", None) == "provider_resume":
        if context.target.harness == "codex":
            from core.checkpoint_codex_fork import _capture_tool_boundary_warm_source

            warm_source = await _capture_tool_boundary_warm_source(context, live, thread_id)
        elif context.target.harness == "claude-code":
            from core.checkpoint_cc_boundary import capture_boundary_warm_source

            warm_source = await capture_boundary_warm_source(context, live, thread_id)
    elif context.target.harness == "codex":
        from core.checkpoint_render import capture_warm_checkpoint_source

        warm_source = await capture_warm_checkpoint_source(
            thread_id, context.boundary, target=context.target,
            binary=context.binary, reasoning_effort=context.reasoning_effort, live=live,
        )
    else:
        from core.checkpoint_warm_request import warm_capture_refused

        warm_capture_refused(thread_id, context.target.harness, "no_live_handle")
    return await _prepare_external_checkpoint(
        checkpoint_id, context=context, live=live, identity=identity,
        warm_source=warm_source, required=required,
    )


async def maintain_external_initial_boundary(
    live: Any,
    thread_id: str,
    harness: str,
    prepared,
    refresh_prepared,
):
    """Evaluate the initial provider write after its exact Turn is bound."""
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        ProviderBoundary,
        maintain_before_provider_call,
    )
    identity = live.corsia_turn
    scope = getattr(live, "checkpoint_wait_scope", None)
    ledger = getattr(prepared, "fit_ledger", None)
    if identity is None or not isinstance(scope, CheckpointWaitScope):
        raise RuntimeError("external initial boundary has no active Turn scope")
    if ledger is None:
        raise RuntimeError("external initial boundary has no frozen fit ledger")
    context = await _read_external_context(
        live,
        thread_id,
        harness,
        current_turn_id=identity.turn_id,
    )

    async def prepare(checkpoint_id: str, required: bool):
        current_context = await _read_external_context(
            live,
            thread_id,
            harness,
            current_turn_id=identity.turn_id,
        )
        return await _prepare_initial_external_checkpoint(
            checkpoint_id,
            required,
            context=current_context,
            live=live,
            thread_id=thread_id,
            identity=identity,
            prepared=prepared,
        )
    render = partial(_render_initial_installed, refresh_prepared=refresh_prepared)

    replay = getattr(prepared, "replay", None)
    adopted_checkpoint_id = (
        getattr(replay, "checkpoint_generation_id", None)
        if getattr(prepared, "mode", None) != "provider_resume"
        else context.registered_checkpoint_id
    )
    continuation = await maintain_before_provider_call(ProviderBoundary(
        thread_id=thread_id,
        turn=identity,
        context_window=context.target.context_window,
        canonical_input_tokens=ledger.rendered_input_tokens,
        provider_input_tokens=None,
        minimum_input_tokens=_initial_minimum(prepared),
        adopted_checkpoint_id=adopted_checkpoint_id,
        active_checkpoint=context.active_checkpoint,
        installed_checkpoint=context.installed_checkpoint,
        prepare_checkpoint=prepare if context.boundary is not None else None,
        render_installed=render,
        wait_scope=scope,
        eager_checkpoint_id=context.eager_checkpoint_id,
    ))
    return continuation.payload
