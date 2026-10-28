"""Measure the current external input at a tool boundary."""

from typing import Any

from subagents.harness_checkpoint_context import ExternalCheckpointContext


async def current_external_input(
    context: ExternalCheckpointContext,
    identity: Any,
    measurement: tuple[int | None, int | None],
    complete_input_tokens: int | None = None,
) -> tuple[int | None, Any | None, int | None]:
    """Measure the live call or render the exact context that seeded its handle."""
    from core.context_replay import exact_replay
    from subagents.harness_checkpoint_resume import render_external_selection

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


async def recovery_input_tokens(context, identity, window, render):
    """Measure the installed recovery payload for a Codex tool boundary."""
    from core.checkpoint_resume import installed_replay
    from core.fork_fit import at_checkpoint_launch_point, foldable_tail_tokens
    from history.types import HistoryBoundary
    from infra.db_async import run_in_db

    installed = context.installed_checkpoint
    active = context.active_checkpoint
    if (
        context.target.harness != "codex"
        or installed is None
        or getattr(active, "status", None) in {"running", "captured"}
    ):
        return None
    target = context.target
    estimate = foldable_tail_tokens(
        context.history, HistoryBoundary.of_checkpoint(installed), project_arguments=False,
        model=target.expected_reported_model or target.transport_model,
    )
    if at_checkpoint_launch_point(estimate, window):
        return estimate
    selection = await run_in_db(installed_replay, installed)
    prepared = await render(context, identity, selection, f"recovery:{identity.turn_id}")
    return prepared.rendered.input_tokens
