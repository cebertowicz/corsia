"""Typed contracts shared by short-lived harness forks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any


ForkHandler = Callable[..., Awaitable[Any]]
ForkToolExecutor = Callable[..., Awaitable[str]]
ForkStepLimit = int | None | Callable[[Any], int | None]
FORK_HARNESSES = frozenset({"native", "claude-code", "codex"})


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
    """Checkpoint Native calls retain the ordinary agent iteration bound."""
    return agent.max_iterations


@dataclass(slots=True)
class ForkHandle:
    task: asyncio.Task
    cancel_event: asyncio.Event
    thread_id: str
    tool_executor: Any = None
    kind: ForkKind | None = None
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
        task.exception()


def cancel_fork(fork_id: str) -> None:
    handle = handles.get(fork_id)
    if handle is not None:
        handle.cancel_event.set()
