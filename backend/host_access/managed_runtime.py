"""Runtime mechanics for durable managed background processes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from typing import TYPE_CHECKING, Any, Awaitable

from host_access.managed_log import (
    open_new_log,
    read_log_tail,
    retain_tail,
    tail_log_path,
)
from host_access.process_launch import close_fd, discard_refused_launch
from host_access.redaction import REDACTED

if TYPE_CHECKING:
    from host_access.processes import BackgroundProcessResult, ProcessRecord, _RuntimeProcess

LONG_RUNNING_MAX_LIFETIME_SECONDS = 24 * 60 * 60
DEFAULT_STALL_AFTER_SECONDS = 900
LONG_RUNNING_POLL_SECONDS = 30.0
LONG_RUNNING_HEAD_BYTES = 256 * 1024
LONG_RUNNING_TAIL_BYTES = 4 * 1024 * 1024
_FRAGMENT_REDACT_BYTES = 8


class ManagedRuntimeMixin:
    """Gate release, watching, stall detection, and bounded log retention."""

    if TYPE_CHECKING:
        _runtimes: dict[str, _RuntimeProcess]
        _clock: Any
        long_running_poll_seconds: float
        long_running_tail_bytes: int
        max_log_bytes: int

        def _start_transition(
            self, runtime: _RuntimeProcess, kind: Any,
        ) -> asyncio.Task[None]: ...

        def _notify_transition(
            self, runtime: _RuntimeProcess, kind: Any,
        ) -> asyncio.Task[None] | None: ...

        def _set_state(
            self, runtime: _RuntimeProcess, terminal_state: Any,
        ) -> ProcessRecord: ...

        async def _terminate_runtime(
            self, runtime: _RuntimeProcess, *, reason: str,
        ) -> ProcessRecord: ...

        def _retain_failure(self, failure: BaseException) -> None: ...

        def _owned_group_exists(self, record: ProcessRecord) -> bool: ...

        def _runtime_marker_processes(
            self, runtime: _RuntimeProcess,
        ) -> Any: ...

        @staticmethod
        async def _join_awaitable(awaitable: Any) -> Any: ...

        @staticmethod
        async def _shield_cleanup(awaitable: Any) -> Any: ...

    async def _persist_and_release_background(
        self,
        runtime: _RuntimeProcess,
        gate_write_fd: int,
        timeout_seconds: float,
    ) -> BackgroundProcessResult:
        """Persist before exec; after go, return without another await."""
        from host_access.processes import BackgroundProcessResult, ProcessOwnerError

        process = runtime.process
        record = runtime.record
        try:
            try:
                persistence = self._start_transition(runtime, "spawned")
            except BaseException:
                await discard_refused_launch(
                    process, pid=record.pid, log_path=record.log_path,
                )
                self._runtimes.pop(record.handle, None)
                raise
            try:
                await asyncio.shield(persistence)
            except asyncio.CancelledError:
                close_fd(gate_write_fd)
                gate_write_fd = -1
                try:
                    await self._shield_cleanup(self._join_awaitable(persistence))
                except BaseException:
                    await discard_refused_launch(
                        process,
                        pid=record.pid,
                        log_path=record.log_path,
                    )
                    self._runtimes.pop(record.handle, None)
                else:
                    self._install_background_tasks(
                        runtime,
                        timeout_seconds=timeout_seconds,
                    )
                    await self._settle_launch_gate_refusal(runtime)
                raise
            except BaseException:
                close_fd(gate_write_fd)
                gate_write_fd = -1
                await discard_refused_launch(
                    process,
                    pid=record.pid,
                    log_path=record.log_path,
                )
                self._runtimes.pop(record.handle, None)
                raise
            self._install_background_tasks(
                runtime,
                timeout_seconds=timeout_seconds,
            )
            try:
                os.write(gate_write_fd, b"G")
            except (BrokenPipeError, OSError) as exc:
                close_fd(gate_write_fd)
                gate_write_fd = -1
                await self._settle_launch_gate_refusal(runtime)
                raise ProcessOwnerError(
                    "managed process launch gate refused command execution"
                ) from exc
            close_fd(gate_write_fd)
            gate_write_fd = -1
            runtime.admitting = False
            return BackgroundProcessResult(handle=record.handle)
        finally:
            close_fd(gate_write_fd)

    def _install_background_tasks(
        self,
        runtime: _RuntimeProcess,
        *,
        timeout_seconds: float,
    ) -> None:
        runtime.spool_task = asyncio.create_task(
            self._spool_background_output(runtime),
            name=f"managed-process-spool:{runtime.record.handle}",
        )
        runtime.watcher_task = asyncio.create_task(
            self._watch_background(runtime, timeout_seconds=timeout_seconds),
            name=f"managed-process-watch:{runtime.record.handle}",
        )

    async def _settle_launch_gate_refusal(self, runtime: _RuntimeProcess) -> None:
        if runtime.watcher_task is not None:
            await self._shield_cleanup(self._join_awaitable(runtime.watcher_task))
        if runtime.transition_task is not None:
            await self._shield_cleanup(self._join_awaitable(runtime.transition_task))

    async def _spool_background_output(self, runtime: _RuntimeProcess) -> None:
        stdout = runtime.process.stdout
        if stdout is None:
            return
        if runtime.record.long_running:
            await self._spool_head_and_tail(runtime, stdout)
        else:
            await self._spool_head_only(runtime, stdout)

    async def _watch_background(
        self,
        runtime: _RuntimeProcess,
        *,
        timeout_seconds: float,
    ) -> None:
        try:
            if runtime.record.long_running:
                await self._supervise_long_running(
                    runtime,
                    self._await_background_exit(runtime),
                )
            else:
                async with asyncio.timeout(timeout_seconds):
                    await self._await_background_exit(runtime)
            async with runtime.cleanup_lock:
                if runtime.record.terminal_state == "running":
                    if runtime.admitting:
                        runtime.pending_reason = "launch_gate_refused"
                    self._set_state(runtime, "exited")
        except asyncio.TimeoutError:
            reason = "max_lifetime" if runtime.record.long_running else "timeout"
            try:
                await self._shield_cleanup(
                    self._terminate_runtime(runtime, reason=reason)
                )
            finally:
                spool = runtime.spool_task
                if spool is not None:
                    if not spool.done():
                        spool.cancel()
                    results = await self._shield_cleanup(
                        asyncio.gather(spool, return_exceptions=True)
                    )
                    for result in results:
                        if isinstance(result, BaseException) and not isinstance(
                            result,
                            asyncio.CancelledError,
                        ):
                            self._retain_failure(result)
        except asyncio.CancelledError:
            if runtime.record.terminal_state == "running":
                await self._shield_cleanup(
                    self._terminate_runtime(runtime, reason="watcher_cancelled")
                )
            raise
        except Exception as exc:
            if runtime.record.terminal_state == "running":
                runtime.pending_reason = "watcher_failed"
                self._set_state(runtime, "death_unverified")
            self._retain_failure(exc)

    @staticmethod
    async def _wait_for_process_exit(
        runtime: _RuntimeProcess,
        timeout: float,
    ) -> None:
        from host_access.processes import ProcessCleanupError

        try:
            await asyncio.wait_for(
                asyncio.shield(runtime.process.wait()),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ProcessCleanupError(
                f"owned process leader {runtime.record.pid} was not reaped"
            ) from exc

    async def _await_background_exit(self, runtime: _RuntimeProcess) -> None:
        await runtime.process.wait()
        if runtime.spool_task is not None:
            await runtime.spool_task
        while runtime.record.terminal_state == "running":
            if self._owned_group_exists(runtime.record):
                await asyncio.sleep(0.05)
                continue
            if self._runtime_marker_processes(runtime):
                await asyncio.sleep(1.0)
                continue
            break

    def _touch_output(self, runtime: _RuntimeProcess, *, truncated: bool) -> None:
        record = runtime.record
        runtime.record = replace(
            record,
            last_output_at=self._clock(),
            stalled_since=None,
            log_truncated=record.log_truncated or truncated,
        )
        if record.stalled_since is not None:
            self._notify_transition(runtime, "resumed")

    def _check_stall(self, runtime: _RuntimeProcess) -> None:
        record = runtime.record
        if record.stalled_since is not None or record.stall_after_seconds is None:
            return
        now = self._clock()
        quiet_since = record.last_output_at or record.started_at
        if now - quiet_since >= record.stall_after_seconds:
            runtime.record = replace(record, stalled_since=now)
            self._notify_transition(runtime, "stalled")

    async def _supervise_long_running(
        self,
        runtime: _RuntimeProcess,
        body: Awaitable[Any],
    ) -> None:
        record = runtime.record
        deadline = record.started_at + float(
            record.max_lifetime_seconds or LONG_RUNNING_MAX_LIFETIME_SECONDS
        )
        task = asyncio.ensure_future(body)
        try:
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError("long-running process reached its lifetime cap")
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=min(self.long_running_poll_seconds, remaining),
                )
                if done:
                    task.result()
                    return
                self._check_stall(runtime)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _spool_head_only(
        self,
        runtime: _RuntimeProcess,
        stdout: asyncio.StreamReader,
    ) -> None:
        cap = self.max_log_bytes
        redactor = _MarkerRedactor(runtime.owner_marker)
        written = 0
        with open_new_log(runtime.record.log_path) as log_file:

            def emit(data: bytes, *, output: bool) -> None:
                nonlocal written
                if output or data:
                    self._touch_output(runtime, truncated=written + len(data) > cap)
                accepted = data[: max(0, cap - written)]
                log_file.write(accepted)
                log_file.flush()
                written += len(accepted)

            try:
                while chunk := await stdout.read(64 * 1024):
                    emit(redactor.feed(chunk), output=True)
            finally:
                emit(redactor.flush(), output=False)

    async def _spool_head_and_tail(
        self,
        runtime: _RuntimeProcess,
        stdout: asyncio.StreamReader,
    ) -> None:
        head_path = runtime.record.log_path
        tail_path = tail_log_path(head_path)
        tail_cap = self.long_running_tail_bytes
        redactor = _MarkerRedactor(runtime.owner_marker)
        head_written = tail_size = total = 0
        tail = open_new_log(tail_path)
        try:
            with open_new_log(head_path) as head:

                def emit(chunk: bytes, *, output: bool) -> None:
                    nonlocal head_written, tail_size, total, tail
                    if not output and not chunk:
                        return
                    total += len(chunk)
                    if head_written < LONG_RUNNING_HEAD_BYTES:
                        accepted = chunk[: LONG_RUNNING_HEAD_BYTES - head_written]
                        head.write(accepted)
                        head.flush()
                        head_written += len(accepted)
                    tail_chunk = chunk[-tail_cap:]
                    retained = tail_cap - len(tail_chunk)
                    if tail_size > retained:
                        tail.close()
                        tail_size = retain_tail(tail_path, retained)
                        tail = tail_path.open("ab")
                    tail.write(tail_chunk)
                    tail.flush()
                    tail_size += len(tail_chunk)
                    self._touch_output(runtime, truncated=total > tail_size)

                try:
                    while raw := await stdout.read(64 * 1024):
                        emit(redactor.feed(raw), output=True)
                finally:
                    emit(redactor.flush(), output=False)
        finally:
            tail.close()


class _MarkerRedactor:
    def __init__(self, marker: str) -> None:
        self._marker = marker.encode()
        self._held = b""

    def feed(self, chunk: bytes) -> bytes:
        if not self._marker:
            return chunk
        data = (self._held + chunk).replace(self._marker, REDACTED.encode())
        keep = next(
            (
                size
                for size in range(min(len(self._marker) - 1, len(data)), 0, -1)
                if data.endswith(self._marker[:size])
            ),
            0,
        )
        self._held = data[len(data) - keep :] if keep else b""
        return data[: len(data) - keep]

    def flush(self) -> bytes:
        held, self._held = self._held, b""
        return REDACTED.encode() if len(held) >= _FRAGMENT_REDACT_BYTES else held
