"""Context accounting and fit phases for the Native iteration loop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
import inspect
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from loguru import logger

from history.types import HistoryBoundary
from infra.db_async import run_in_db
from infra.runtime_truth import provider_boundary_snapshot
from core.checkpoint_native_admission import effective_input_tokens

_effective_input_tokens = effective_input_tokens


def bound_native_turn_identity(cancel_event, prepared_context):
    """Resolve managed Turn identity and require its precomposed context."""
    try:
        identity = cancel_event.turn_identity
    except (AttributeError, RuntimeError):
        return None
    if prepared_context is None:
        raise RuntimeError("managed Native Turn requires prepared context")
    return identity


async def await_transition(callback: Callable[[], Awaitable[None] | None]) -> None:
    """Await a callback that may complete synchronously or asynchronously."""
    outcome = callback()
    if inspect.isawaitable(outcome):
        await outcome


@dataclass(frozen=True, slots=True)
class NativeSafeBoundaryInput:
    """One claimed user frame awaiting the next Native inference."""

    content: str | list[dict[str, Any]]
    input_identity: str
    before_provider_write: Callable[[], Awaitable[None] | None]
    settle_delivery: Callable[[], Awaitable[None] | None]
    release_before_write: Callable[[], Awaitable[None] | None]
    mark_ambiguous: Callable[[], Awaitable[None] | None]


def adopt_native_context(state, prepared) -> None:
    """Replace only the Native provider context beneath the active Turn."""
    state.messages = deepcopy(list(prepared.messages))
    state.context_usage = deepcopy(prepared.context_usage)
    state.tool_definitions = deepcopy(list(prepared.tool_definitions))
    state.context_identity = prepared.context_identity
    state.fit_ledger = prepared.fit_ledger
    state.fit_current_input = current_fit_input(state.messages)
    state.last_measured_call_usage = None
    state.last_measured_rendered_input_tokens = None
    state.checkpoint_source_messages = []


@dataclass(frozen=True, slots=True)
class _NativeCheckpointContext:
    target: object
    boundary: object
    previous: object | None
    projection: object
    previous_generation_id: str | None
    working_dir: str


def _read_native_checkpoint_context(
    thread_id: str,
    target_catalog_id: str,
) -> _NativeCheckpointContext | None:
    from history.boundaries import boundary_before_latest_assistant_step
    from history.projection import render_portable_history
    from history.scoped_source import assistant_step_through
    from infra.checkpoint_queries import current_installed
    from infra.database import get_db
    from llm.resolution import resolve_thread_target
    from subagents.codex_runtime_support import resolve_working_dir

    db = get_db()
    try:
        db.execute("BEGIN")
        thread_row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if thread_row is None:
            return None
        target = resolve_thread_target(str(thread_row["model"] or ""), thread_id=thread_id, db=db)
        if target.catalog_id != target_catalog_id:
            return None
        if target.harness != "native" or not target.available:
            return None
        previous = current_installed(thread_id, db=db)
        floor = None if previous is None else HistoryBoundary.of_checkpoint(previous)
        projection = render_portable_history(
            thread_id, db=db, start_exclusive=floor, include_unfinished_turn=True
        )
        boundary = boundary_before_latest_assistant_step(
            projection,
            floor=floor,
            step_precedes_start=(
                floor is not None and assistant_step_through(db, thread_id, floor.seq)
            ),
        )
        if boundary is None:
            return None
        return _NativeCheckpointContext(
            target, boundary, previous, projection, previous.id if previous is not None else None,
            resolve_working_dir(thread_row["project_id"], db),
        )
    finally:
        db.close()


def _correlated_native_input(state) -> int | None:
    measured = effective_input_tokens(state.last_measured_call_usage)
    baseline = state.last_measured_rendered_input_tokens
    ledger = state.fit_ledger
    if measured is None or baseline is None or ledger is None:
        return None
    return measured + max(0, int(ledger.rendered_input_tokens) - baseline)


def remaining_native_input(state) -> int:
    """Budget a boundary input without deducting unused future output reservations."""
    if state.fit_ledger is None:
        return 0
    measured = _correlated_native_input(state)
    occupied = measured if measured is not None else state.fit_ledger.rendered_input_tokens
    return max(0, state.fit_ledger.effective_window_tokens - occupied)


async def _refresh_native_window(state, thread_id: str) -> int | None:
    """Rebind the same payload to a freshly resolved cap at its next call boundary.

    The resolution is the thread's, so a model its locality policy now forbids stops the
    whole Turn here, before the call: later input then holds at admission on that cause.
    Returns the configured window when the resolved one is a live memory fit, else None.
    """
    from llm.context_window import get_context_window_or_none
    from llm.resolution import resolve_locality_permitted

    ledger = state.fit_ledger
    target = await run_in_db(partial(resolve_locality_permitted, ledger.target_catalog_id, thread_id=thread_id))
    if target.context_window is None:
        raise ValueError("Native provider call has no resolved context window")
    if target.context_window != ledger.effective_window_tokens:
        state.fit_ledger = refit_provider_payload(
            replace(ledger, effective_window_tokens=target.context_window),
            state.messages, state.tool_definitions, input_identity=ledger.input_identity,
            current_input=state.fit_current_input,
            model=target.expected_reported_model or target.transport_model,
        )
        state.context_usage["model_context_window"] = target.context_window
    if target.context_window_source != "memory":
        return None
    return await run_in_db(partial(get_context_window_or_none, ledger.target_catalog_id, live=False))


def _minimum_native_input(ledger) -> int:
    sections = dict(ledger.section_tokens)
    return sum(int(sections.get(name) or 0) for name in ("current_input", "tools", "provider_wrapper"))


async def _render_native_installed(
    record,
    *,
    agent,
    state,
    thread_id: str,
    turn_identity,
    turn_model: str,
    project_id: str | None,
):
    from llm.base import sanitize_empty_content, sanitize_messages

    from core.checkpoint_resume import InstalledContinuation, installed_replay
    from core.continuity_accounting import with_continuity
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.native_context_prefix import build_real_send_prefix
    from core.turn_context import NativePreparedContext

    selection = await run_in_db(installed_replay, record)
    prefix, info = await build_real_send_prefix(
        agent=agent,
        thread_id=thread_id,
        turn_model=turn_model,
        fresh_context=False,
        project_id=project_id,
        model_context_window=state.fit_ledger.effective_window_tokens,
        replay_selection=selection,
        tool_definitions=state.tool_definitions,
        selected_model=state.fit_ledger.target_catalog_id,
    )
    # Delivered history cannot contain the FIFO frame this Turn still owns.
    # Preserve its already-rendered text/media without rebuilding or resequencing it.
    pending = (
        not state.provider_delivery_settled or state.active_boundary_input is not None
    )
    messages = sanitize_messages(sanitize_empty_content([
        *prefix, *([deepcopy(state.messages[-1])] if pending else []),
    ]))
    fit = attach_native_provider_fit_ledger(
        model=turn_model,
        target_catalog_id=state.fit_ledger.target_catalog_id,
        proven_target_window_tokens=state.fit_ledger.effective_window_tokens,
        messages=messages,
        tools=state.tool_definitions,
        input_identity=state.fit_ledger.input_identity,
        current_input=current_fit_input(messages),
    ).fit_ledger
    # The install is a second Native delivery step: it replaces the accounting.
    usage = with_continuity(
        deepcopy(state.context_usage), info["continuity_accounting"],
    )
    usage.update(
        continuity_mode="checkpoint_tail",
        checkpoint_generation_id=record.id,
        rendered_input_tokens=fit.rendered_input_tokens,
        history_messages=len(selection.tail.messages),
        source_artifacts=info.get("source_artifacts") or [],
    )
    prepared = NativePreparedContext(
        messages=tuple(deepcopy(messages)),
        context_usage=usage,
        tool_definitions=tuple(deepcopy(state.tool_definitions)),
        context_identity=state.context_identity,
        fit_ledger=fit,
        installed=info.get("installed_context"),
        snapshot=info.get("effective_snapshot"),
    )
    return InstalledContinuation(fit.rendered_input_tokens, prepared)


async def maintain_native_checkpoint(
    *,
    agent,
    state,
    thread_id: str,
    turn_identity,
    cancel_event,
    turn_model: str,
    reasoning_effort: str | None,
    project_id: str | None,
):
    """Apply the shared checkpoint boundary before one Native provider call."""
    if turn_identity is None or state.fit_ledger is None:
        return None
    configured_window = await _refresh_native_window(state, thread_id)
    from core.checkpoint_native_admission import fixed_input_tokens
    from core.checkpoint_resume import (
        CheckpointWaitScope,
        ProviderBoundary,
        maintain_before_provider_call,
    )
    scope = getattr(cancel_event, "checkpoint_wait_scope", None)
    if not isinstance(scope, CheckpointWaitScope):
        raise RuntimeError("managed Native Turn has no checkpoint wait scope")

    async def prepare(checkpoint_id: str, required: bool):
        from core.checkpoint_native_admission import (
            prepare_native_checkpoint,
            skip_optional_cold_fold,
        )

        skip_optional_cold_fold(state, required)
        context = await run_in_db(
            _read_native_checkpoint_context, thread_id,
            state.fit_ledger.target_catalog_id,
        )
        if context is None:
            return None
        return await prepare_native_checkpoint(
            checkpoint_id, thread_id, state, context, turn_identity,
            reasoning_effort,
        )

    render = partial(
        _render_native_installed, agent=agent, state=state, thread_id=thread_id,
        turn_identity=turn_identity, turn_model=turn_model, project_id=project_id,
    )

    ledger = state.fit_ledger
    active, installed, eager = await run_in_db(
        provider_boundary_snapshot, thread_id, "native", ledger.target_catalog_id,
    )
    adopted = state.context_usage.get("checkpoint_generation_id")
    if state.overflow_retry_pending and installed is not None and installed.id == adopted:
        installed = None  # the refused payload carried it; the overflow retry needs a newer one
    continuation = await maintain_before_provider_call(ProviderBoundary(
        thread_id=thread_id,
        turn=turn_identity,
        context_window=ledger.effective_window_tokens,
        canonical_input_tokens=ledger.rendered_input_tokens,
        provider_input_tokens=_correlated_native_input(state),
        minimum_input_tokens=_minimum_native_input(ledger),
        adopted_checkpoint_id=adopted,
        active_checkpoint=active,
        installed_checkpoint=installed,
        prepare_checkpoint=prepare,
        render_installed=render,
        wait_scope=scope,
        eager_checkpoint_id=eager.id if eager is not None else None,
        fixed_input_tokens=fixed_input_tokens(state),
        configured_window=configured_window,
    ), must_adopt=state.overflow_retry_pending)
    if continuation.payload is None and not ledger.fits:
        state.fit_ledger = replace(ledger, fits=True, block_reason=None)
    if continuation.payload is not None:
        await _retain_continuation(
            thread_id, turn_identity.turn_id, continuation.payload,
            selector=ledger.target_catalog_id, transport_model=turn_model,
        )
    return continuation.payload


async def _retain_continuation(
    thread_id: str, turn_id: str, payload, *, selector: str, transport_model: str,
) -> None:
    """Retain what a continuation the Turn adopts next delivers."""
    from core.guidance_manifest import record_continuation_manifest
    from core.installed_context import persist_prepared_installed_context

    await run_in_db(persist_prepared_installed_context, thread_id, payload)
    # Later calls in this Turn deliver the new list; earlier ones keep theirs.
    await run_in_db(partial(
        record_continuation_manifest, thread_id, turn_id, payload,
        selector=selector, transport_model=transport_model,
    ))


def current_fit_input(messages: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Return the rendered FIFO head value the provider barrier must compare.

    The ledger digests that rendered message, not raw user text; handing the
    barrier different bytes would reject the call before provider contact.
    """
    return messages[-1] if messages else None


def refit_provider_payload(ledger, messages, tools, *, input_identity, current_input, model=None):
    """Regenerate exact fit proof after tool or safe-boundary payload changes."""
    from core.destination_preflight import attach_native_provider_fit_ledger

    return attach_native_provider_fit_ledger(
        model=model,
        target_catalog_id=ledger.target_catalog_id,
        proven_target_window_tokens=ledger.effective_window_tokens,
        messages=messages, tools=tools, input_identity=input_identity,
        current_input=current_input).fit_ledger
