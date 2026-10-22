"""Run one started registry tool call, keeping its completed result on failure.

``tools.dispatch_ownership`` owns the plan-dispatch side of a started call
(the row recorded before the tool runs and settled when it ends). This module
owns the call boundary itself: it runs the tool, converts a post-execution
failure into a typed ``CompletedToolFailure`` that keeps every completed call,
and keeps a completed effect out of the Turn's failure path. Recording
failures are never swallowed: a recorder failure stops the call before the tool
runs.

Settlement after a completed call follows
``tools.dispatch_ownership.settle_completed_call``; only a cancellation during it
raises a ``CompletedToolFailure`` that carries the result.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
import uuid

from tools.dispatch_ownership import (
    is_plans_catalog_tool,
    record_dispatch,
    settle_completed_call,
    settle_failed_call,
    settle_interrupted_call,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompletedToolCall:
    """Known completed output, with only the identity/durability actually observed."""

    name: str
    result: Any = field(repr=False)
    arguments: dict[str, Any] | None = field(default=None, repr=False)
    thread_id: str | None = None
    turn_id: str | None = None
    dispatch_id: str | None = None
    invocation_id: str | None = None
    provider_tool_call_id: str | None = None
    assistant_step_id: str | None = None
    fork_id: str | None = None
    canonical: Any = field(default=None, repr=False)


class CompletedToolFailure(RuntimeError):
    """Post-execution infrastructure failure; never a model-readable tool error.

    Keep result bytes out of exception/terminal text. This deliberately does not
    inherit ToolResultBindingError: a completed effect cannot be skipped by a
    transport's per-result binding-error handler. ``detail`` replaces the
    generic text only with an already user-facing cause, such as a checkpoint
    refusal's reason.
    """

    def __init__(
        self, completed: tuple[CompletedToolCall, ...], detail: str | None = None,
    ) -> None:
        self.completed = completed
        self.detail = detail
        super().__init__(
            detail
            or "Tool execution completed, but result settlement or publication failed."
        )


def checkpoint_refusal_detail(error: BaseException) -> str | None:
    """Return a checkpoint refusal's reason, the one cause users already see."""
    from core.checkpoint_types import CheckpointUnavailable

    return str(error) if isinstance(error, CheckpointUnavailable) else None


def completed_tool_failure(completed: tuple[CompletedToolCall, ...], error: BaseException,
                           *, harness: str) -> CompletedToolFailure:
    """Preserve and diagnose one failure after tool effects have completed."""
    failure = CompletedToolFailure(completed, checkpoint_refusal_detail(error))
    last_call = completed[-1]
    call_id = last_call.provider_tool_call_id or last_call.invocation_id or last_call.dispatch_id
    log.error(
        "completed tool failure harness=%s completed_count=%d thread=%r turn=%r "
        "last_tool=%r last_call=%r cause=%s: %s",
        harness, len(completed), last_call.thread_id, last_call.turn_id, last_call.name,
        call_id, type(error).__name__, error,
        exc_info=(type(error), error, error.__traceback__),
    )
    return failure


def _present_identity(value: Any) -> bool:
    """Treat only non-empty identity values as producer-known evidence."""
    return value is not None and value != ""


def _merge_known_value(current: Any, outer: Any) -> Any:
    """Fill absent producer metadata and refuse a contradictory outer value."""
    if not _present_identity(outer):
        return current
    if not _present_identity(current):
        return outer
    if current != outer:
        raise ValueError("completed tool identity conflict")
    return current


def enrich_completed_failure(
    failure: CompletedToolFailure,
    *,
    prefix: tuple[CompletedToolCall, ...] = (),
    name: str,
    arguments: dict[str, Any],
    thread_id: str | None = None,
    turn_id: str | None = None,
    invocation_id: str | None = None,
    provider_tool_call_id: str | None = None,
    assistant_step_id: str | None = None,
    fork_id: str | None = None,
) -> CompletedToolFailure:
    """Add outer call identity without ever rebinding producer-known evidence.

    A contradiction stays a ``CompletedToolFailure`` carrying the untouched
    producer calls and any earlier completed prefix. It deliberately never
    becomes ``ToolResultBindingError``, which Codex isolates per result.
    """
    original = tuple(prefix) + tuple(failure.completed)
    enriched: list[CompletedToolCall] = []
    try:
        for call in failure.completed:
            if call.name != name:
                raise ValueError("completed tool name conflict")
            merged_arguments = _merge_known_value(call.arguments, arguments)
            enriched.append(CompletedToolCall(
                name=call.name,
                result=call.result,
                arguments=dict(merged_arguments) if merged_arguments is not None else None,
                thread_id=_merge_known_value(call.thread_id, thread_id),
                turn_id=_merge_known_value(call.turn_id, turn_id),
                dispatch_id=call.dispatch_id,
                invocation_id=_merge_known_value(call.invocation_id, invocation_id),
                provider_tool_call_id=_merge_known_value(
                    call.provider_tool_call_id, provider_tool_call_id,
                ),
                assistant_step_id=_merge_known_value(
                    call.assistant_step_id, assistant_step_id,
                ),
                fork_id=_merge_known_value(call.fork_id, fork_id),
                canonical=call.canonical,
            ))
    except ValueError:
        failure.completed = original
        return failure
    failure.completed = tuple(prefix) + tuple(enriched)
    return failure


def _media_metadata(result: Any) -> Any:
    reference = getattr(result, "media", None)
    if reference is None:
        return None
    as_metadata = getattr(reference, "as_metadata", None)
    return as_metadata() if callable(as_metadata) else reference


def _same_completed_call(left: CompletedToolCall, right: CompletedToolCall) -> bool:
    """Compare retained calls without letting string-like media hide identity."""
    return (
        left.name == right.name
        and left.result == right.result
        and _media_metadata(left.result) == _media_metadata(right.result)
        and left.arguments == right.arguments
        and left.thread_id == right.thread_id
        and left.turn_id == right.turn_id
        and left.dispatch_id == right.dispatch_id
        and left.invocation_id == right.invocation_id
        and left.provider_tool_call_id == right.provider_tool_call_id
        and left.assistant_step_id == right.assistant_step_id
        and left.fork_id == right.fork_id
        and getattr(left.canonical, "message_id", None)
        == getattr(right.canonical, "message_id", None)
    )


def combine_completed_failures(
    current: BaseException | None,
    incoming: CompletedToolFailure,
) -> CompletedToolFailure:
    """Retain ordered independent completions, deduplicating exact repeats."""
    known = current if isinstance(current, CompletedToolFailure) else None
    if known is None:
        return incoming
    completed = list(known.completed)
    for call in incoming.completed:
        if not any(_same_completed_call(existing, call) for existing in completed):
            completed.append(call)
    combined = CompletedToolFailure(tuple(completed), known.detail or incoming.detail)
    combined.__cause__ = known.__cause__
    return combined


async def execute_recorded_call(
    tool: Any,
    name: str,
    execution_params: dict[str, Any],
    thread_context: dict[str, Any],
) -> Any:
    """Execute the tool as one started call with durable plan ownership.

    A catalog ``plans`` tool runs without a dispatch row. Every other call is
    recorded under the thread's valid selection, if it has one. The dispatch
    id exists before recording, so a cancellation or failure during or right
    after the recording commit still settles that row by id (a no-op when no
    row was written) before the error propagates.

    Returns:
        The tool's own result, unchanged.

    Raises:
        RuntimeError: A failed call's dispatch could not be settled.
        CompletedToolFailure: A completed call's settlement was cancelled; it
            carries the result.
    """
    if is_plans_catalog_tool(name):
        return await tool.execute(**execution_params)
    thread_id = thread_context["id"]
    turn_identity = thread_context.get("_host_access_turn_identity")
    turn_id = turn_identity.turn_id if turn_identity is not None else None
    dispatch_id = uuid.uuid4().hex
    recorded: bool | None = None
    try:
        recorded = await record_dispatch(name, thread_id, turn_id, dispatch_id)
        result = await tool.execute(**execution_params)
    except Exception as call_error:
        if recorded is not False:
            await settle_failed_call(name, thread_id, dispatch_id, call_error)
        raise
    except BaseException:
        if recorded is not False:
            await settle_interrupted_call(name, thread_id, dispatch_id)
        raise
    if recorded:
        try:
            await settle_completed_call(name, thread_id, dispatch_id, result, turn_id)
        except asyncio.CancelledError as error:
            raise completed_tool_failure((CompletedToolCall(
                name=name, result=result, thread_id=thread_id, turn_id=turn_id,
                dispatch_id=dispatch_id,
            ),), error, harness="recorded-call") from error
    return result
