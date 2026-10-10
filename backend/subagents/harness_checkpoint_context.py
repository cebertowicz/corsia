"""Read one external harness's durable checkpoint and continuation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ExternalCheckpointContext:
    """Durable checkpoint facts read in one snapshot for an external harness.

    ``history`` starts after the installed checkpoint when one exists and the
    caller did not ask for exact history; otherwise it is the seq-0 projection.
    """

    thread_row: Any
    target: Any
    history: Any
    boundary: Any
    previous_generation_id: str | None
    working_dir: str
    tail_tokens: int
    minimum_tail_tokens: int
    active_checkpoint: Any = None
    installed_checkpoint: Any = None
    registered_checkpoint_id: str | None = None
    eager_checkpoint_id: str | None = None
    previous: Any = None
    reasoning_effort: str | None = None
    binary: str | None = None
    warm_boundary: Any = None


def _tail_tokens(projection, boundary, *, project_arguments: bool, model: str) -> int:
    from core.checkpoint_fit import foldable_tail_tokens

    return foldable_tail_tokens(
        projection, boundary, project_arguments=project_arguments, model=model,
    )


def _count(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def external_measurement(
    live: Any, harness: str, selected_window: int | None = None,
) -> tuple[int | None, int | None]:
    """Read one external provider's live usage and effective context window."""
    from llm.context_window import effective_usage_window

    window = selected_window if selected_window is not None else getattr(live, "context_window", None)
    if harness == "claude-code":
        from infra.provider_usage import from_anthropic

        usage = from_anthropic(getattr(live, "last_call_usage", None))
        return usage.input_total if usage is not None else None, _count(window)
    peek = getattr(live.session, "peek_active_usage", None)
    usage = peek() if callable(peek) else None
    if not isinstance(usage, dict):
        return None, _count(window)
    return _count(usage.get("input_tokens")), effective_usage_window(
        window, usage.get("model_context_window"),
    )


def _checkpoint_boundary(record):
    from history.types import HistoryBoundary

    return HistoryBoundary.of_checkpoint(record)


def _newer_boundary(boundary, floor):
    if boundary is None or (floor is not None and boundary.seq <= floor.seq):
        return None
    return boundary


def _external_boundary(
    projection,
    floor,
    *,
    current_turn_id: str | None = None,
    step_precedes_start: bool = False,
):
    from history.boundaries import boundary_before_latest_assistant_step
    from history.source import _TERMINAL_TURN_STATES
    from history.turns import assistant_step_batches

    latest = projection.turns[-1] if projection.turns else None
    if (
        current_turn_id is not None
        and latest is not None
        and latest.turn_id != current_turn_id
    ):
        if latest.status in _TERMINAL_TURN_STATES:
            return _newer_boundary(projection.sealed_boundary, floor)
        return None
    running = latest is not None and latest.status == "running"
    batches = assistant_step_batches(latest) if running else ()
    boundary = None if running and not batches else boundary_before_latest_assistant_step(
        projection, floor=floor, step_precedes_start=step_precedes_start,
    )
    own_input = boundary is not None and any(
        message.role == "user"
        and message.seq == boundary.seq
        and message.message_id == boundary.message_id
        for message in (latest.messages if latest is not None else ())
    )
    if running and (not batches or own_input):
        previous = projection.turns[-2].boundary if len(projection.turns) > 1 else None
        return _newer_boundary(previous, floor) or boundary
    return boundary


def _warm_boundary(projection, boundary):
    """Return the one harness-neutral descriptor of the durable through row."""
    if boundary is None:
        return None
    matches = [
        message
        for message in projection.messages
        if message.seq == boundary.seq and message.message_id == boundary.message_id
    ]
    if len(matches) != 1:
        return None
    message = matches[0]
    if message.role not in {"user", "assistant", "tool"}:
        return None
    if message.role == "tool" and not message.tool_call_id:
        return None
    # A harness proves a row by its own message id, then cuts after that
    # message's whole record group; a later row in the same group would land
    # inside the frozen prefix, so only the group's last row is provable.
    if message.provider_message_id and any(
        later.seq > message.seq
        and later.provider_message_id == message.provider_message_id
        for later in projection.messages
    ):
        return None
    assistant_step_id = (
        message.assistant_step_id
        if message.role == "tool"
        else (message.message_id if message.role == "assistant" else None)
    )
    assistant = next(
        (
            candidate
            for candidate in projection.messages
            if assistant_step_id is not None
            and candidate.role == "assistant"
            and candidate.message_id == assistant_step_id
        ),
        None,
    )
    tool_call_ids = tuple(
        str(call["id"])
        for call in (assistant.tool_calls if assistant is not None else ())
        if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"]
    )
    from history.types import DurableBoundaryMessage

    return DurableBoundaryMessage(
        role=message.role,
        content=message.content,
        tool_call_id=message.tool_call_id if message.role == "tool" else None,
        message_id=message.message_id,
        assistant_step_id=assistant_step_id,
        tool_call_ids=tool_call_ids,
        provider_message_id=message.provider_message_id,
    )


def _harness_boundaries(
    harness,
    projection,
    floor,
    *,
    current_turn_id: str | None = None,
    step_precedes_start: bool = False,
):
    boundary = _external_boundary(
        projection,
        floor,
        current_turn_id=current_turn_id,
        step_precedes_start=step_precedes_start,
    )
    warm_boundary = (
        _warm_boundary(projection, boundary)
        if harness in {"codex", "claude-code"}
        else None
    )
    return boundary, warm_boundary


def _read_history(db, thread_id: str, previous, *, exact_history: bool):
    """Read history after the installed checkpoint, or from seq 0 when exact."""
    from history.projection import render_portable_history
    from history.scoped_source import assistant_step_through

    floor = None if previous is None else _checkpoint_boundary(previous)
    start = None if exact_history else floor
    projection = render_portable_history(
        thread_id, db=db, start_exclusive=start, include_unfinished_turn=True,
    )
    precedes = start is not None and assistant_step_through(db, thread_id, start.seq)
    return projection, floor, precedes


def read_external_checkpoint_context(
    thread_id: str,
    target_catalog_id: str,
    harness: str,
    *,
    current_turn_id: str | None = None,
    exact_history: bool = False,
) -> ExternalCheckpointContext | None:
    """Read one harness's checkpoint decision inputs in a single snapshot."""
    from core.checkpoint_render import checkpoint_binary
    from infra.checkpoint_queries import current_installed
    from infra.database import get_db
    from infra.runtime_truth import provider_boundary_snapshot, registered_checkpoint_id
    from llm.reasoning_config import effective_reasoning_effort
    from llm.resolution import resolve_execution_target
    from registry.models import find_for_thread
    from subagents.codex_runtime_support import resolve_working_dir

    db = get_db()
    try:
        db.execute("BEGIN")
        thread_row = db.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,)
        ).fetchone()
        if thread_row is None or str(thread_row["model"] or "") != target_catalog_id:
            return None
        target = resolve_execution_target(target_catalog_id, db=db)
        if target.harness != harness or not target.available:
            return None
        previous = current_installed(thread_id, db=db)
        projection, floor, precedes = _read_history(
            db, thread_id, previous, exact_history=exact_history,
        )
        active, installed, eager = provider_boundary_snapshot(
            thread_id, harness, target.catalog_id, db=db,
        )
        boundary, warm_boundary = _harness_boundaries(
            harness,
            projection,
            floor,
            current_turn_id=current_turn_id,
            step_precedes_start=precedes,
        )
        integration, artifact_type = (
            ("claude-code", "session")
            if harness == "claude-code"
            else ("codex", "thread")
        )
        registration = find_for_thread(
            thread_id, integration, artifact_type, db=db,
        )
        return ExternalCheckpointContext(
            thread_row=thread_row,
            target=target,
            history=projection,
            boundary=boundary,
            previous_generation_id=previous.id if previous else None,
            working_dir=resolve_working_dir(thread_row["project_id"], db),
            tail_tokens=_tail_tokens(
                projection, boundary, project_arguments=False,
                model=target.expected_reported_model or target.transport_model,
            ),
            minimum_tail_tokens=_tail_tokens(
                projection, boundary, project_arguments=True,
                model=target.expected_reported_model or target.transport_model,
            ),
            active_checkpoint=active,
            installed_checkpoint=installed,
            registered_checkpoint_id=registered_checkpoint_id(registration),
            eager_checkpoint_id=eager.id if eager is not None else None,
            previous=previous,
            reasoning_effort=effective_reasoning_effort(
                thread_row["reasoning_effort"], target.catalog_id
            ),
            binary=checkpoint_binary(target, db),
            warm_boundary=warm_boundary,
        )
    finally:
        db.close()


async def read_boundary_contexts(
    read, live, thread_id: str, harness: str,
    measured: int | None, complete_input_tokens: int | None,
):
    """Read a tool boundary's input context and its checkpoint context.

    The first read only feeds the live-input measure, which a complete input count
    answers without it. With a live measurement it is also the checkpoint read,
    argument for argument, so it is skipped and the checkpoint read serves. Without
    one it is a seq-0 render, which validates covered history the checkpoint read
    never loads, so it is kept.
    """
    exact_history = measured is None
    context = (
        None if complete_input_tokens is not None and not exact_history
        else await read(live, thread_id, harness, exact_history=exact_history)
    )
    checkpoint_context = await read(live, thread_id, harness)
    return (checkpoint_context if context is None else context), checkpoint_context
