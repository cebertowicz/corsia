"""Durable background-process lifecycle additions for ``ProcessOwner``.

The process owner remains the only live OS-group owner.  This module adds the
bounded long-running policy, ordered durable transition callbacks, retained
head/tail logs, and the prepare/abort/commit barrier used by thread deletion.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Sequence

from host_access.managed_runtime import (
    DEFAULT_STALL_AFTER_SECONDS,
    LONG_RUNNING_MAX_LIFETIME_SECONDS,
    LONG_RUNNING_POLL_SECONDS,
    LONG_RUNNING_TAIL_BYTES,
    ManagedRuntimeMixin,
)

if TYPE_CHECKING:
    from host_access.processes import ProcessRecord, _RuntimeProcess

log = logging.getLogger(__name__)

MAX_LONG_RUNNING_PER_THREAD = 2
MAX_LONG_RUNNING_TOTAL = 6
TRANSITION_RETRY_DELAYS = (1.0, 2.0)

Transition = Literal["spawned", "stalled", "resumed", "terminal"]
TransitionCallback = Callable[["ProcessRecord", Transition], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LongRunningReservation:
    """One durable or live process that consumes long-running capacity."""

    handle: str
    thread_id: str
    description: str
    state: str


LongRunningCapacityCallback = Callable[
    [], Awaitable[Sequence[LongRunningReservation]]
]


async def _no_durable_long_running() -> Sequence[LongRunningReservation]:
    return ()


@dataclass(frozen=True, slots=True)
class ThreadCloseToken:
    """Exact family barrier and runtimes settled by one deletion prepare."""

    thread_ids: tuple[str, ...]
    handles: tuple[str, ...]



def outcome_for(record: ProcessRecord) -> str | None:
    if record.terminal_state == "running":
        return None
    if record.terminal_state == "death_unverified":
        return "death_unverified"
    if record.reason in {"backend_shutdown", "watcher_cancelled"}:
        return "interrupted_by_restart"
    if record.reason == "managed_stop":
        return "stopped"
    if record.terminal_state == "exited" and record.exit_code == 0:
        return "completed"
    return "failed"


class ManagedLifecycleMixin(ManagedRuntimeMixin):
    """Lifecycle behavior composed into the one live process owner."""

    _runtimes: dict[str, _RuntimeProcess]
    _owner_lock: asyncio.Lock
    _closing_threads: set[str]
    _clock: Callable[[], float]
    _transition_callback: TransitionCallback | None
    _long_running_capacity_callback: LongRunningCapacityCallback = staticmethod(
        _no_durable_long_running
    )
    _transition_tasks: set[asyncio.Task[None]]
    long_running_poll_seconds: float
    long_running_tail_bytes: int
    max_log_bytes: int

    def configure_transition_callback(
        self, callback: TransitionCallback | None,
    ) -> None:
        self._transition_callback = callback

    def configure_long_running_capacity_callback(
        self, callback: LongRunningCapacityCallback,
    ) -> None:
        self._long_running_capacity_callback = callback

    def records_for_thread(self, thread_id: str) -> tuple[ProcessRecord, ...]:
        return tuple(
            runtime.record
            for runtime in self._runtimes.values()
            if runtime.record.thread_id == thread_id
            and runtime.record.mode == "background"
        )

    async def _admit_long_running(self, thread_id: str) -> None:
        from host_access.processes import ProcessCapacityError

        reserved = {
            item.handle: item
            for item in await self._long_running_capacity_callback()
        }
        reserved.update(
            {
                runtime.record.handle: LongRunningReservation(
                    handle=runtime.record.handle,
                    thread_id=runtime.record.thread_id,
                    description=runtime.record.description or runtime.record.handle,
                    state=runtime.record.terminal_state,
                )
                for runtime in self._runtimes.values()
                if runtime.record.long_running
                and runtime.record.terminal_state in {"running", "death_unverified"}
            }
        )
        own = [
            item for item in reserved.values() if item.thread_id == thread_id
        ]
        named = ", ".join(
            f'{item.handle} ("{item.description}", {item.state})' for item in own
        )
        if len(own) >= MAX_LONG_RUNNING_PER_THREAD:
            raise ProcessCapacityError(
                f"this thread already has {len(own)} long-running processes "
                f"(the limit is {MAX_LONG_RUNNING_PER_THREAD}): {named}. Do not "
                "evict user-named work without authorization. Wait for a running "
                "handle to finish; a death_unverified handle will not send a "
                "completion event and can only be released with "
                "managed_process_stop."
            )
        if len(reserved) >= MAX_LONG_RUNNING_TOTAL:
            message = (
                f"{len(reserved)} long-running processes are already running "
                f"across CORSIA (the limit is {MAX_LONG_RUNNING_TOTAL})."
            )
            if own:
                message += (
                    " Wait for any running process to finish, or, if authorized, "
                    "stop one of this thread's own listed handles with "
                    f"managed_process_stop: {named}."
                )
            else:
                message += (
                    " Wait for an owning thread to resolve one, or tell the user "
                    "that instance capacity is blocked; do not stop another "
                    "thread's work."
                )
            raise ProcessCapacityError(message)

    async def _require_process_admission(
        self,
        *,
        thread_id: str,
        turn_id: str,
        lease_id: str | None,
        long_running: bool,
    ) -> None:
        from host_access.processes import ProcessOwnerError

        if self._shutting_down:
            raise ProcessOwnerError("process owner is shutting down")
        if thread_id in self._closing_threads:
            raise ProcessOwnerError("thread process admission is closed")
        if (thread_id, turn_id) in self._closed_turns:
            raise ProcessOwnerError("turn process admission is closed")
        if lease_id is not None and lease_id in self._closed_leases:
            raise ProcessOwnerError("lease process admission is closed")
        if long_running:
            await self._admit_long_running(thread_id)

    def _start_transition(
        self, runtime: _RuntimeProcess, kind: Transition,
    ) -> asyncio.Task[None]:
        callback = self._transition_callback
        if callback is None:
            raise RuntimeError("durable background transition callback is unavailable")
        record = runtime.record
        previous = runtime.transition_task

        async def deliver() -> None:
            if previous is not None:
                await previous
            for delay in TRANSITION_RETRY_DELAYS:
                try:
                    await callback(record, kind)
                    return
                except Exception:
                    log.warning(
                        "managed process %s transition %s failed; retrying in %.1fs",
                        record.handle,
                        kind,
                        delay,
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)
            await callback(record, kind)

        def transition_done(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                return
            exception = done.exception()
            if exception is not None:
                log.error(
                    "managed process %s transition %s was not recorded",
                    record.handle,
                    kind,
                    exc_info=(
                        type(exception),
                        exception,
                        exception.__traceback__,
                    ),
                )
                return
            self._transition_tasks.discard(done)

        task = asyncio.get_running_loop().create_task(
            deliver(),
            name=f"managed-process-transition:{record.handle}:{kind}",
        )
        runtime.transition_task = task
        self._transition_tasks.add(task)
        task.add_done_callback(transition_done)
        return task

    def _notify_transition(
        self, runtime: _RuntimeProcess, kind: Transition,
    ) -> asyncio.Task[None] | None:
        if runtime.record.mode != "background":
            return None
        return self._start_transition(runtime, kind)

    @staticmethod
    def _transition_succeeded(runtime: _RuntimeProcess) -> bool:
        task = runtime.transition_task
        return (
            task is not None
            and task.done()
            and not task.cancelled()
            and task.exception() is None
        )

    async def drain_transitions(self) -> None:
        while self._transition_tasks:
            batch = tuple(self._transition_tasks)
            results = await asyncio.gather(*batch, return_exceptions=True)
            failure = next(
                (result for result in results if isinstance(result, BaseException)),
                None,
            )
            if failure is not None:
                raise failure
            self._transition_tasks.difference_update(batch)

    def _close_record(self, runtime: _RuntimeProcess) -> None:
        record = runtime.record
        if record.ended_at is not None:
            return
        returncode = getattr(runtime.process, "returncode", None)
        runtime.record = replace(
            record,
            reason=runtime.pending_reason or "exit",
            exit_code=None if returncode is None else int(returncode),
            ended_at=self._clock(),
        )
        self._notify_transition(runtime, "terminal")


    async def prepare_thread_close(
        self, thread_ids: Sequence[str],
    ) -> ThreadCloseToken:
        """Fence exact threads, terminate their background work, and join truth."""
        from host_access.processes import ProcessCleanupError

        exact_ids = tuple(dict.fromkeys(str(item) for item in thread_ids))
        async with self._owner_lock:
            self._closing_threads.update(exact_ids)
            runtimes = tuple(
                runtime
                for runtime in self._runtimes.values()
                if runtime.record.thread_id in exact_ids
                and runtime.record.mode == "background"
            )
        await self._terminate_snapshot(runtimes, reason="thread_deleted")
        for runtime in runtimes:
            tasks = tuple(
                task
                for task in (runtime.watcher_task, runtime.spool_task)
                if task is not None and task is not asyncio.current_task()
            )
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                failure = next(
                    (
                        result
                        for result in results
                        if isinstance(result, BaseException)
                        and not isinstance(result, asyncio.CancelledError)
                    ),
                    None,
                )
                if failure is not None:
                    raise ProcessCleanupError(
                        f"managed process {runtime.record.handle} watcher failed"
                    ) from failure
            transition = runtime.transition_task
            if transition is not None:
                await transition
            if (
                runtime.record.terminal_state in {"running", "death_unverified"}
                or runtime.cleanup_paths
                or not self._transition_succeeded(runtime)
            ):
                raise ProcessCleanupError(
                    f"managed process {runtime.record.handle} did not settle durably"
                )
        return ThreadCloseToken(
            thread_ids=exact_ids,
            handles=tuple(runtime.record.handle for runtime in runtimes),
        )

    async def abort_thread_close(self, token: ThreadCloseToken) -> None:
        async with self._owner_lock:
            self._closing_threads.difference_update(token.thread_ids)

    async def commit_thread_close(self, token: ThreadCloseToken) -> None:
        async with self._owner_lock:
            for handle in token.handles:
                runtime = self._runtimes.get(handle)
                if runtime is not None and self._transition_succeeded(runtime):
                    self._runtimes.pop(handle, None)
            self._closing_threads.difference_update(token.thread_ids)
