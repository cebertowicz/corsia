"""Typed contracts shared by short-lived harness forks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any


ForkHandler = Callable[..., Awaitable[Any]]
ForkToolExecutor = Callable[..., Awaitable[str]]
ForkUsageBinder = Callable[[Any], Awaitable[Any]]
ForkProviderFailure = Callable[[str, str], None]
ForkStepLimit = int | None | Callable[[Any], int | None]
FORK_HARNESSES = frozenset({"native", "claude-code", "codex"})
logger = logging.getLogger(__name__)


class ForkKindDefinitionError(ValueError):
    """A kind omitted a policy every harness must enforce."""


class ForkStepLimitReached(RuntimeError):
    """The fork exhausted the configured job budget."""


class ForkStepLimitError(ValueError):
    """A kind supplied an unusable step limit for one harness."""


@dataclass(frozen=True, slots=True)
class ForkKind:
    """The policy that varies between otherwise identical disposable forks."""

    name: str
    tool_allowlist: frozenset[str]
    tool_refusal: str
    #: Tail of a tool result that no longer fits, formatted with ``excess`` tokens.
    over_room_text: str
    tool_executor: ForkToolExecutor
    step_limits: Mapping[str, ForkStepLimit]
    end_handler: ForkHandler
    limit_or_failure_handler: ForkHandler
    usage_binder: ForkUsageBinder
    shutdown_handler: ForkHandler
    provider_failure: ForkProviderFailure

    def __post_init__(self) -> None:
        missing = FORK_HARNESSES - self.step_limits.keys()
        if missing:
            names = ", ".join(sorted(missing))
            raise ForkKindDefinitionError(
                f"fork kind {self.name!r} is missing step limits for: {names}"
            )
        if not self.tool_allowlist:
            raise ForkKindDefinitionError(f"fork kind {self.name!r} has no allowed tools")
        if not self.tool_refusal.strip():
            raise ForkKindDefinitionError(f"fork kind {self.name!r} has no tool refusal text")

    def step_limit(self, harness: str, context: Any = None) -> int | None:
        if harness not in FORK_HARNESSES:
            raise ForkStepLimitError(f"unknown fork harness: {harness}")
        value = self.step_limits[harness]
        result = value(context) if callable(value) else value
        if result is not None and (
            isinstance(result, bool) or not isinstance(result, int) or result < 1
        ):
            raise ForkStepLimitError(
                f"fork kind {self.name!r} has invalid {harness} step limit: {result!r}"
            )
        return result


def native_iteration_limit(agent: Any) -> int:
    """Use the ordinary agent iteration bound for a kind's Native calls."""
    return agent.max_iterations


@dataclass(slots=True)
class ForkHandle:
    task: asyncio.Task
    cancel_event: asyncio.Event
    thread_id: str
    kind: ForkKind
    tool_executor: Any = None
    request: Any = None
    tool_registry: Any = None
    tool_token: str = ""


handles: dict[str, ForkHandle] = {}


def running_fork(thread_id: str, fork_id: str) -> ForkHandle | None:
    handle = handles.get(fork_id)
    if (handle is None or handle.thread_id != thread_id
            or handle.task.done() or handle.cancel_event.is_set()):
        return None
    return handle


def forget(fork_id: str, task: asyncio.Task) -> None:
    handle = handles.get(fork_id)
    if handle is not None and handle.task is task:
        handles.pop(fork_id, None)
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.error("fork %s ended with an exception", fork_id, exc_info=error)


async def shutdown_forks() -> None:
    """Cancel and join every live disposable fork before provider shutdown."""
    active = tuple(handles.items())
    for _fork_id, handle in active:
        handle.cancel_event.set()
    for fork_id, handle in active:
        await handle.kind.shutdown_handler(fork_id)
    await asyncio.gather(
        *(handle.task for _fork_id, handle in active), return_exceptions=True,
    )


def fork_task_count() -> int:
    """Expose live cancellation-handle count for lifecycle verification."""
    return len(handles)


async def invoke_fork(
    request: Any,
    cancel_event: asyncio.Event,
    *,
    kind: ForkKind,
    on_cold_input=None,
    on_tool_executor=None,
) -> Any:
    """Dispatch each disposable fork through its sole harness adapter."""
    from core.fork_cc import invoke_fork as invoke_cc
    from core.fork_codex import invoke_fork as invoke_codex
    from core.fork_native import invoke_fork as invoke_native

    adapters = {
        "native": invoke_native,
        "claude-code": invoke_cc,
        "codex": invoke_codex,
    }
    try:
        adapter = adapters[request.target.harness]
    except KeyError as error:
        raise ValueError(f"unsupported fork harness: {request.target.harness}") from error
    if request.target.harness == "claude-code":
        return await adapter(
            request, cancel_event, kind=kind, on_cold_input=on_cold_input,
            on_tool_executor=on_tool_executor,
        )
    return await adapter(request, cancel_event, kind=kind, on_cold_input=on_cold_input)


def cancel_fork(fork_id: str) -> None:
    handle = handles.get(fork_id)
    if handle is not None and not handle.task.done():
        handle.task.get_loop().call_soon_threadsafe(handle.cancel_event.set)
