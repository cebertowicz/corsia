"""Owned host process groups, managed handles, and exact-PID tools.

This module is the sole in-process owner for agent-started host process groups.
Process records are immutable snapshots; mutable asyncio/process handles remain
private to :class:`ProcessOwner`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import time
from typing import Any, Callable, Literal, Mapping, Sequence

import psutil
from host_access.group_probe import live_group_members_after_eperm
from host_access.browser_sessions import BROWSER_NAMESPACE, BrowserSessionOwner
from host_access.managed_lifecycle import (
    DEFAULT_STALL_AFTER_SECONDS,
    LONG_RUNNING_MAX_LIFETIME_SECONDS,
    LONG_RUNNING_POLL_SECONDS,
    LONG_RUNNING_TAIL_BYTES,
    ManagedLifecycleMixin,
)
from host_access.managed_runtime import read_log_tail
from host_access.process_launch import (
    bind_process_launch,
    close_fd,
    gated_launch_spec,
    prepare_spawn,
)
from host_access.redaction import (
    REDACTED,
    redact_process_arguments as _redact_arguments,
)


PROCESS_OWNER_MARKER_ENV = "CORSIA_PROCESS_OWNER_MARKER"
ProcessMode = Literal["foreground", "background"]
TerminalState = Literal[
    "running",
    "exited",
    "terminated",
    "killed",
    "undeclared_background_stopped",
    "death_unverified",
]

_TERMINAL_STATES = frozenset(
    {
        "exited",
        "terminated",
        "killed",
        "undeclared_background_stopped",
        "death_unverified",
    }
)
_OUTSTANDING_STATES = frozenset({"running", "death_unverified"})
_SIGNALS = {
    "interrupt": signal.SIGINT,
    "terminate": signal.SIGTERM,
    "kill": signal.SIGKILL,
}
class ProcessOwnerError(RuntimeError):
    """Base error for an owned-process lifecycle failure."""


class ProcessCapacityError(ProcessOwnerError):
    """The bounded owner cannot admit another live process."""


class ProcessCleanupError(ProcessOwnerError):
    """An owned process lifecycle could not be proved safely terminal."""


class ProcessBindingError(ProcessOwnerError):
    """An exact-PID operation failed identity or structural validation."""


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    """Immutable public snapshot of one process-group ownership record."""

    handle: str
    thread_id: str
    turn_id: str
    lease_id: str | None
    lease_revision: int | None
    permit_id: str | None = field(repr=False)
    pid: int
    pgid: int
    creation_fingerprint: str
    mode: ProcessMode
    log_path: Path
    terminal_state: TerminalState
    long_running: bool = False
    description: str | None = None
    reason: str | None = None
    exit_code: int | None = None
    started_at: float = 0.0
    last_output_at: float | None = None
    stalled_since: float | None = None
    ended_at: float | None = None
    stall_after_seconds: float | None = None
    max_lifetime_seconds: float | None = None
    log_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ForegroundProcessResult:
    """Captured result after a foreground group reaches a verified terminal."""

    record: ProcessRecord
    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool = False
    undeclared_background_stopped: bool = False


@dataclass(frozen=True, slots=True)
class BackgroundProcessResult:
    """Opaque model-safe handle for a managed background process group."""

    handle: str


@dataclass(frozen=True, slots=True)
class ProcessMetadata:
    """Bounded process-discovery result with no environment data."""

    pid: int
    parent_pid: int
    name: str
    executable: str
    arguments: tuple[str, ...]
    create_time: float
    executable_fingerprint: str


@dataclass(slots=True)
class _RuntimeProcess:
    record: ProcessRecord
    process: asyncio.subprocess.Process
    owner_marker: str = field(default="", repr=False)
    marker_bindings: dict[int, str] = field(
        default_factory=dict,
        repr=False,
    )
    cleanup_paths: tuple[Path, ...] = field(default=(), repr=False)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
    spool_task: asyncio.Task[None] | None = None
    watcher_task: asyncio.Task[None] | None = None
    pending_reason: str | None = None
    transition_task: asyncio.Task[None] | None = None
    admitting: bool = False


@dataclass(frozen=True, slots=True)
class _OwnedMarkerProcess:
    pid: int
    creation_fingerprint: str


def _executable_fingerprint(process: psutil.Process) -> tuple[str, str]:
    try:
        executable = process.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as exc:
        raise ProcessBindingError(
            "process executable identity is unavailable"
        ) from exc
    if not executable:
        raise ProcessBindingError("process executable identity is unavailable")
    try:
        metadata = os.stat(executable)
    except OSError as exc:
        raise ProcessBindingError(
            "process executable identity is unavailable"
        ) from exc
    material: dict[str, Any] = {
        "executable": executable,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "size": metadata.st_size,
        "modified_ns": metadata.st_mtime_ns,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return executable, hashlib.sha256(encoded).hexdigest()


def _process_creation_fingerprint(pid: int) -> str:
    try:
        process = psutil.Process(pid)
        create_time = process.create_time()
        if not math.isfinite(create_time):
            raise ProcessBindingError(
                "process creation identity is unavailable"
            )
        # The owner binds PID reuse, not executable replacement. Creation time
        # remains readable after TERM while macOS can already deny ``exe()``.
        material = f"{pid}\0{create_time:.6f}"
    except (psutil.Error, OSError) as exc:
        raise ProcessBindingError(
            "process creation identity is unavailable"
        ) from exc
    return hashlib.sha256(material.encode()).hexdigest()


def _is_zombie(process: psutil.Process) -> bool:
    try:
        return process.status() in {
            psutil.STATUS_ZOMBIE,
            getattr(psutil, "STATUS_DEAD", "dead"),
            "terminated",
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


def _group_pids(pgid: int) -> tuple[int, ...]:
    if pgid <= 1:
        return ()
    # Exact process-group existence does not require broad host enumeration.
    # This is both a tighter ownership proof and works in the protected
    # envelope where macOS denies the sysctl used by ``psutil.pids()``.
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return ()
    except PermissionError:
        return live_group_members_after_eperm(pgid)
    return (pgid,)


def _current_control_plane_pids() -> frozenset[int]:
    protected = {0, 1, os.getpid()}
    current = os.getpid()
    seen: set[int] = set()
    while current > 1 and current not in seen:
        seen.add(current)
        protected.add(current)
        try:
            current = psutil.Process(current).ppid()
        except psutil.Error:
            break
    return frozenset(protected)


def _metadata_for_process(process: psutil.Process) -> ProcessMetadata:
    try:
        arguments = process.cmdline()
        parent_pid = process.ppid()
        name = process.name()
        create_time = process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        raise ProcessBindingError("process metadata is unavailable") from exc
    if not math.isfinite(create_time):
        raise ProcessBindingError("process creation time is unavailable")
    executable, executable_fingerprint = _executable_fingerprint(process)
    return ProcessMetadata(
        pid=process.pid,
        parent_pid=parent_pid,
        name=name[:200],
        executable=executable[:1024],
        arguments=_redact_arguments(arguments),
        create_time=create_time,
        executable_fingerprint=executable_fingerprint,
    )


def discover_processes(
    *,
    pid: int | None = None,
    query: str | None = None,
    limit: int = 20,
) -> tuple[ProcessMetadata, ...]:
    """Return bounded process metadata without reading process environments."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ProcessBindingError("process discovery limit is invalid")
    if pid is not None:
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise ProcessBindingError("process discovery PID is invalid")
        if pid <= 1:
            raise ProcessBindingError(
                "protected control-plane PID cannot be discovered"
            )
    normalized_query = (query or "").strip().casefold()
    if len(normalized_query) > 200:
        raise ProcessBindingError("process discovery query is too long")
    protected_pids = _current_control_plane_pids()

    if pid is not None:
        if pid in protected_pids:
            raise ProcessBindingError(
                "protected control-plane PID cannot be discovered"
            )
        try:
            exact_process = psutil.Process(pid)
        except psutil.NoSuchProcess as exc:
            raise ProcessBindingError("bound process no longer exists") from exc
        candidates: Sequence[psutil.Process] = (exact_process,)
    else:
        candidates = tuple(psutil.process_iter(["pid"]))
    results: list[ProcessMetadata] = []
    for process in candidates:
        if process.pid <= 1 or process.pid in protected_pids:
            continue
        try:
            metadata = _metadata_for_process(process)
        except (ProcessBindingError, psutil.Error):
            continue
        searchable = " ".join(
            (
                metadata.name,
                metadata.executable,
                *metadata.arguments,
            )
        ).casefold()
        if normalized_query and normalized_query not in searchable:
            continue
        results.append(metadata)
        if len(results) >= limit:
            break
    return tuple(results)


def _validate_bound_process(
    *,
    pid: int,
    create_time: float,
    executable_fingerprint: str,
) -> psutil.Process:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid in _current_control_plane_pids()
        or pid <= 1
    ):
        raise ProcessBindingError("protected control-plane PID cannot be signaled")
    if (
        isinstance(create_time, bool)
        or not isinstance(create_time, (int, float))
        or not math.isfinite(float(create_time))
        or float(create_time) < 0
    ):
        raise ProcessBindingError("bound process creation time is invalid")
    if (
        not isinstance(executable_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", executable_fingerprint) is None
    ):
        raise ProcessBindingError("bound process executable identity is invalid")
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess as exc:
        raise ProcessBindingError("bound process no longer exists") from exc
    try:
        observed_create_time = process.create_time()
    except psutil.Error as exc:
        raise ProcessBindingError("bound process creation time is unavailable") from exc
    if abs(observed_create_time - float(create_time)) > 0.001:
        raise ProcessBindingError("bound process creation identity changed")
    _executable, observed_fingerprint = _executable_fingerprint(process)
    if observed_fingerprint != executable_fingerprint:
        raise ProcessBindingError("bound process executable identity changed")
    return process


def describe_bound_process(
    *,
    pid: int,
    create_time: float,
    executable_fingerprint: str,
) -> ProcessMetadata:
    """Return sanitized metadata for one already identity-bound signal target."""
    process = _validate_bound_process(
        pid=pid,
        create_time=create_time,
        executable_fingerprint=executable_fingerprint,
    )
    return _metadata_for_process(process)


def signal_bound_process(
    *,
    pid: int,
    create_time: float,
    executable_fingerprint: str,
    signal_name: str,
) -> None:
    """Revalidate and signal exactly one PID from a discovery binding."""
    selected = _SIGNALS.get(signal_name)
    if selected is None:
        raise ProcessBindingError("process signal is unsupported")
    process = _validate_bound_process(
        pid=pid,
        create_time=create_time,
        executable_fingerprint=executable_fingerprint,
    )
    try:
        process.send_signal(selected)
    except psutil.NoSuchProcess as exc:
        raise ProcessBindingError("bound process no longer exists") from exc
    except (psutil.AccessDenied, PermissionError) as exc:
        raise ProcessBindingError("bound process signal was denied") from exc


class ProcessOwner(ManagedLifecycleMixin):
    """One bounded owner for all CORSIA-agent process groups."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        term_grace_seconds: float = 2.0,
        kill_grace_seconds: float = 2.0,
        max_records: int = 128,
        max_log_bytes: int = 1_000_000,
        browser_owner: BrowserSessionOwner | None = None,
        clock: Callable[[], float] = time.time,
        long_running_poll_seconds: float = LONG_RUNNING_POLL_SECONDS,
        long_running_tail_bytes: int = LONG_RUNNING_TAIL_BYTES,
    ) -> None:
        self._clock = clock
        self.long_running_poll_seconds = max(0.01, float(long_running_poll_seconds))
        self.long_running_tail_bytes = max(128, int(long_running_tail_bytes))
        self._transition_callback = None
        self._transition_tasks: set[asyncio.Task[None]] = set()
        self.runtime_dir = Path(runtime_dir).expanduser().resolve(strict=False)
        self.process_dir = self.runtime_dir / "managed-processes"
        self.term_grace_seconds = max(0.01, float(term_grace_seconds))
        self.kill_grace_seconds = max(0.01, float(kill_grace_seconds))
        self.max_records = max(1, int(max_records))
        self.max_log_bytes = max(128, int(max_log_bytes))
        self._browser_owner = browser_owner
        self._runtimes: dict[str, _RuntimeProcess] = {}
        self._owner_lock = asyncio.Lock()
        self._closed_turns: set[tuple[str, str]] = set()
        self._closed_leases: set[str] = set()
        self._closing_threads: set[str] = set()
        self._failures: list[BaseException] = []
        self._shutting_down = False

    def live_count(self) -> int:
        process_count = self.active_process_count()
        browser_count = (
            self._browser_owner.live_count()
            if self._browser_owner is not None
            else 0
        )
        return process_count + browser_count

    def active_process_count(self) -> int:
        """Return owned process groups that still require lifecycle cleanup."""
        return sum(
            runtime.record.terminal_state in _OUTSTANDING_STATES
            for runtime in self._runtimes.values()
        )

    @staticmethod
    def _lifecycle_cleanup_pending(runtime: _RuntimeProcess) -> bool:
        transition_pending = (
            runtime.record.mode == "background"
            and (
                runtime.transition_task is None
                or not runtime.transition_task.done()
                or runtime.transition_task.cancelled()
                or runtime.transition_task.exception() is not None
            )
        )
        return (
            runtime.record.terminal_state in _OUTSTANDING_STATES
            or bool(runtime.cleanup_paths)
            or transition_pending
        )

    def active_browser_command_count(self) -> int:
        """Return admitted browser commands that are running or queued."""
        return (
            self._browser_owner.active_command_count()
            if self._browser_owner is not None
            else 0
        )

    def running_records(self) -> tuple[ProcessRecord, ...]:
        return tuple(
            runtime.record
            for runtime in self._runtimes.values()
            if runtime.record.terminal_state == "running"
        )

    def record(self, handle: str) -> ProcessRecord:
        runtime = self._runtimes.get(handle)
        if runtime is None:
            raise KeyError("managed process handle is unknown")
        return runtime.record

    def _owned_runtime(self, handle: str, thread_id: str) -> _RuntimeProcess:
        runtime = self._runtimes.get(handle)
        if runtime is None:
            raise KeyError("managed process handle is unknown")
        if runtime.record.thread_id != thread_id:
            raise PermissionError("managed process handle is owned by another thread")
        return runtime

    def status(self, handle: str, *, thread_id: str) -> ProcessRecord:
        return self._owned_runtime(handle, thread_id).record

    def read_logs(
        self,
        handle: str,
        *,
        thread_id: str,
        limit_bytes: int = 20_000,
    ) -> str:
        runtime = self._owned_runtime(handle, thread_id)
        record = runtime.record
        cap = self.long_running_tail_bytes if record.long_running else self.max_log_bytes
        bounded = max(1, min(int(limit_bytes), cap))
        payload = read_log_tail(
            record.log_path,
            long_running=record.long_running,
            limit_bytes=bounded,
        )
        payload = self._redact_owner_marker(payload, runtime.owner_marker)
        if len(payload) > bounded:
            payload = payload[-bounded:]
        return payload.decode("utf-8", errors="replace")

    async def run_browser(
        self,
        thread_id: str,
        args: Sequence[str],
        *,
        lease_id: str | None = None,
        timeout: float = 30.0, show_window: bool | None = None,
        navigation_url: str | None = None,
    ) -> str:
        """Delegate one browser command to the composed session owner."""
        options = {"navigation_url": navigation_url} if navigation_url is not None else {}
        return await self.browser_sessions().run(
            thread_id, args, lease_id=lease_id, timeout=timeout, show_window=show_window, **options,
        )

    async def close_browser(self, thread_id: str) -> str:
        """Close only the composed browser session for one exact thread."""
        return await self.browser_sessions().close_thread(thread_id)

    def browser_sessions(self) -> BrowserSessionOwner:
        if self._browser_owner is None:
            raise ProcessOwnerError("browser session owner is unavailable")
        return self._browser_owner

    def _log_path_for(self, handle: str) -> Path:
        self.process_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.process_dir, 0o700)
        return self.process_dir / f"{handle}.log"

    def _prune_terminal_records(self) -> None:
        while len(self._runtimes) >= self.max_records:
            terminal = next(
                (
                    (handle, runtime)
                    for handle, runtime in self._runtimes.items()
                    if runtime.record.terminal_state in _TERMINAL_STATES
                    and not self._lifecycle_cleanup_pending(runtime)
                ),
                None,
            )
            if terminal is None:
                raise ProcessCapacityError(
                    "managed process capacity is exhausted by live or unverified owners"
                )
            handle, runtime = terminal
            self._runtimes.pop(handle, None)
            if runtime.record.mode == "foreground":
                runtime.record.log_path.unlink(missing_ok=True)

    def _cleanup_runtime_paths(self, runtime: _RuntimeProcess) -> None:
        """Remove private files whose lifetime is owned by this process."""
        retained: list[Path] = []
        for path in runtime.cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                retained.append(path)
        runtime.cleanup_paths = tuple(retained)
        if retained:
            raise ProcessCleanupError(
                f"owned process retained {len(retained)} cleanup path(s)"
            )

    def _set_state(
        self,
        runtime: _RuntimeProcess,
        terminal_state: TerminalState,
    ) -> ProcessRecord:
        runtime.record = replace(
            runtime.record,
            terminal_state=terminal_state,
        )
        if terminal_state in _TERMINAL_STATES and terminal_state != "death_unverified":
            if runtime.record.mode == "background":
                self._close_record(runtime)
            self._cleanup_runtime_paths(runtime)
        return runtime.record

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        thread_id: str,
        turn_id: str,
        cwd: Path,
        cwd_fd: int | None = None,
        env: Mapping[str, str],
        timeout_seconds: float,
        background: bool,
        cleanup_paths: Sequence[Path] = (),
        lease_id: str | None = None,
        lease_revision: int | None = None,
        permit_id: str | None = None,
        long_running: bool = False,
        description: str | None = None,
        stall_after_seconds: float = DEFAULT_STALL_AFTER_SECONDS,
        max_lifetime_seconds: float = LONG_RUNNING_MAX_LIFETIME_SECONDS,
    ) -> ForegroundProcessResult | BackgroundProcessResult:
        """Spawn one owned group; persist background identity before exec."""
        spec = prepare_spawn(
            argv, cwd=cwd, cwd_fd=cwd_fd, cleanup_paths=cleanup_paths,
            error_type=ProcessOwnerError,
        )
        if not thread_id or not turn_id:
            raise ProcessOwnerError("owned process requires exact session and turn")
        if timeout_seconds <= 0:
            raise ProcessOwnerError("owned process timeout is invalid")
        if long_running and (not background or not (description or "").strip()):
            raise ProcessOwnerError("a long-running process is background and described")
        if background and self._transition_callback is None:
            raise ProcessOwnerError(
                "durable background process persistence is unavailable"
            )
        launch_argv, launch_cwd = spec.argv, spec.cwd
        spawn_options, owned_cleanup_paths = spec.options, spec.cleanup_paths
        gate_read_fd: int | None = None
        gate_write_fd: int | None = None

        async with self._owner_lock:
            await self._require_process_admission(
                thread_id=thread_id,
                turn_id=turn_id,
                lease_id=lease_id,
                long_running=long_running,
            )
            self._prune_terminal_records()
            handle = secrets.token_urlsafe(24)
            owner_marker = secrets.token_urlsafe(32)
            log_path = self._log_path_for(handle)
            if background:
                try:
                    (
                        launch_argv,
                        spawn_options,
                        gate_read_fd,
                        gate_write_fd,
                    ) = gated_launch_spec(launch_argv, spawn_options)
                except BaseException:
                    log_path.unlink(missing_ok=True)
                    raise
            child_env = dict(env)
            child_env[PROCESS_OWNER_MARKER_ENV] = owner_marker
            try:
                process = await asyncio.create_subprocess_exec(
                    *launch_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=(
                        asyncio.subprocess.STDOUT
                        if background
                        else asyncio.subprocess.PIPE
                    ),
                    cwd=launch_cwd,
                    env=child_env,
                    start_new_session=True,
                    **spawn_options,
                )
            except BaseException:
                close_fd(gate_read_fd)
                close_fd(gate_write_fd)
                log_path.unlink(missing_ok=True)
                raise
            close_fd(gate_read_fd)
            gate_read_fd = None
            pid = int(process.pid)
            if pid <= 1:
                close_fd(gate_write_fd)
                try:
                    process.kill()
                finally:
                    await process.wait()
                    log_path.unlink(missing_ok=True)
                raise ProcessOwnerError("spawn returned an invalid process identity")
            try:
                creation_fingerprint = await bind_process_launch(
                    process,
                    handle=handle,
                    pid=pid,
                    log_path=log_path,
                    fingerprint=_process_creation_fingerprint,
                )
            except BaseException:
                close_fd(gate_write_fd)
                gate_write_fd = None
                raise
            record = ProcessRecord(
                handle=handle,
                thread_id=thread_id,
                turn_id=turn_id,
                lease_id=lease_id,
                lease_revision=lease_revision,
                permit_id=permit_id,
                pid=pid,
                pgid=pid,
                creation_fingerprint=creation_fingerprint,
                mode="background" if background else "foreground",
                log_path=log_path,
                terminal_state="running",
                long_running=long_running,
                description=(description or "").strip() or None,
                started_at=self._clock(),
                stall_after_seconds=stall_after_seconds if long_running else None,
                max_lifetime_seconds=(
                    max_lifetime_seconds if long_running else None
                ),
            )
            runtime = _RuntimeProcess(
                record=record,
                process=process,
                owner_marker=owner_marker,
                cleanup_paths=owned_cleanup_paths,
                admitting=background,
            )
            self._runtimes[handle] = runtime
            if background:
                assert gate_write_fd is not None
                return await self._persist_and_release_background(
                    runtime, gate_write_fd, float(timeout_seconds),
                )
            runtime.communicate_task = asyncio.create_task(
                process.communicate(),
                name=f"managed-process-output:{handle}",
            )

        return await self._wait_foreground(
            runtime, timeout_seconds=float(timeout_seconds),
        )

    def _write_foreground_log(
        self,
        record: ProcessRecord,
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        payload = stdout + (b"\nSTDERR:\n" + stderr if stderr else b"")
        descriptor = os.open(
            record.log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
        )
        with os.fdopen(descriptor, "wb") as log_file:
            log_file.write(payload[: self.max_log_bytes])

    async def _wait_foreground(
        self,
        runtime: _RuntimeProcess,
        *,
        timeout_seconds: float,
    ) -> ForegroundProcessResult:
        communicate = runtime.communicate_task
        assert communicate is not None
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            await self._shield_cleanup(
                self._terminate_runtime(runtime, reason="timeout")
            )
            stdout, stderr = await asyncio.shield(communicate)
        except asyncio.CancelledError:
            try:
                await self._shield_cleanup(
                    self._terminate_runtime(runtime, reason="cancelled")
                )
                await self._shield_cleanup(
                    self._join_awaitable(communicate)
                )
            except asyncio.CancelledError:
                pass
            raise

        stdout = self._redact_owner_marker(stdout, runtime.owner_marker)
        stderr = self._redact_owner_marker(stderr, runtime.owner_marker)
        self._write_foreground_log(runtime.record, stdout, stderr)
        undeclared = False
        if not timed_out:
            undeclared = await self._finish_normal_foreground(runtime)
        return ForegroundProcessResult(
            record=runtime.record,
            stdout=stdout,
            stderr=stderr,
            returncode=int(runtime.process.returncode or 0),
            timed_out=timed_out,
            undeclared_background_stopped=undeclared,
        )

    async def _finish_normal_foreground(
        self,
        runtime: _RuntimeProcess,
    ) -> bool:
        async with runtime.cleanup_lock:
            if runtime.record.terminal_state != "running":
                return False
            try:
                group_exists = self._owned_group_exists(runtime.record)
                marker_exists = bool(self._runtime_marker_processes(runtime))
            except ProcessCleanupError as exc:
                self._set_state(runtime, "death_unverified")
                self._retain_failure(exc)
                raise
            if not group_exists and not marker_exists:
                self._set_state(runtime, "exited")
                return False
            await self._terminate_locked(
                runtime,
                reason="undeclared_background",
            )
            if runtime.record.terminal_state == "death_unverified":
                raise ProcessCleanupError(
                    f"owned process group {runtime.record.pgid} survived cleanup"
                )
            self._set_state(runtime, "undeclared_background_stopped")
            return True

    def _assert_group_binding(self, record: ProcessRecord) -> None:
        """Refuse a PID/PGID occupant that is not the group we spawned."""
        try:
            process = psutil.Process(record.pid)
        except psutil.NoSuchProcess:
            # The leader may be reaped while ordinary descendants retain the
            # original process group. A later reuse necessarily recreates PID
            # == PGID, at which point this check observes it.
            return
        if _is_zombie(process):
            return
        try:
            observed = _process_creation_fingerprint(record.pid)
        except ProcessBindingError as exc:
            # An absent leader may be reaped after status; still probe its group for descendants.
            if not psutil.pid_exists(record.pid):
                return
            raise ProcessCleanupError(f"owned process group {record.pgid} identity is unavailable") from exc
        if observed != record.creation_fingerprint:
            raise ProcessCleanupError(
                f"owned process group {record.pgid} identity changed"
            )

    def _owned_group_exists(self, record: ProcessRecord) -> bool:
        self._assert_group_binding(record)
        return bool(_group_pids(record.pgid))

    @staticmethod
    def _redact_owner_marker(payload: bytes, owner_marker: str) -> bytes:
        if not payload or not owner_marker:
            return payload
        return payload.replace(owner_marker.encode(), REDACTED.encode())

    def _owned_marker_processes(
        self,
        owner_marker: str,
        *,
        known_bindings: Sequence[_OwnedMarkerProcess] = (),
    ) -> tuple[_OwnedMarkerProcess, ...]:
        """Bind same-user processes carrying this owner's private marker."""
        if not owner_marker:
            return ()
        current_uid = os.getuid()
        protected = _current_control_plane_pids()
        bindings: list[_OwnedMarkerProcess] = []
        known_pids: set[int] = set()
        for binding in known_bindings:
            if self._owned_marker_binding_is_live(binding):
                known_pids.add(binding.pid)
                bindings.append(binding)
        try:
            candidates = psutil.process_iter(["pid"])
        except (psutil.Error, OSError) as exc:
            raise ProcessCleanupError("owned process marker scan failed") from exc
        for process in candidates:
            if (
                process.pid <= 1
                or process.pid in protected
                or process.pid in known_pids
            ):
                continue
            marker_matched = False
            try:
                if process.uids().real != current_uid:
                    continue
                environment = process.environ()
                if environment.get(PROCESS_OWNER_MARKER_ENV) != owner_marker:
                    continue
                marker_matched = True
                status = process.status()
                if status in {
                    psutil.STATUS_ZOMBIE,
                    getattr(psutil, "STATUS_DEAD", "dead"),
                    "terminated",
                }:
                    continue
                creation_fingerprint = _process_creation_fingerprint(
                    process.pid
                )
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.AccessDenied, SystemError) as exc:
                if marker_matched:
                    raise ProcessCleanupError(
                        "owned process marker scan could not prove process identity"
                    ) from exc
                # Unrelated same-user hardened or just-exited processes can be
                # opaque on macOS. Known marker bindings were revalidated
                # above; only an unreadable unbound process is skipped here.
                continue
            except (ProcessBindingError, OSError) as exc:
                raise ProcessCleanupError(
                    "owned process marker scan could not prove process identity"
                ) from exc
            bindings.append(
                _OwnedMarkerProcess(
                    pid=process.pid,
                    creation_fingerprint=creation_fingerprint,
                )
            )
        return tuple(bindings)

    def _owned_marker_binding_is_live(
        self,
        binding: _OwnedMarkerProcess,
    ) -> bool:
        try:
            observed = _process_creation_fingerprint(binding.pid)
        except ProcessBindingError as exc:
            if not psutil.pid_exists(binding.pid):
                return False
            raise ProcessCleanupError(
                "owned process marker scan lost a known process identity"
            ) from exc
        if observed != binding.creation_fingerprint:
            return False
        try:
            status = psutil.Process(binding.pid).status()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False
        except (psutil.AccessDenied, OSError) as exc:
            raise ProcessCleanupError(
                "owned process marker scan lost a known process identity"
            ) from exc
        return status not in {
            psutil.STATUS_ZOMBIE,
            getattr(psutil, "STATUS_DEAD", "dead"),
            "terminated",
        }

    def _runtime_marker_processes(
        self,
        runtime: _RuntimeProcess,
    ) -> tuple[_OwnedMarkerProcess, ...]:
        known = tuple(
            _OwnedMarkerProcess(
                pid=pid,
                creation_fingerprint=fingerprint,
            )
            for pid, fingerprint in runtime.marker_bindings.items()
        )
        bindings = self._owned_marker_processes(
            runtime.owner_marker,
            known_bindings=known,
        )
        runtime.marker_bindings.update(
            {
                binding.pid: binding.creation_fingerprint
                for binding in bindings
            }
        )
        return bindings

    def _signal_owned_marker_process(
        self,
        binding: _OwnedMarkerProcess,
        selected_signal: signal.Signals,
    ) -> bool:
        """Revalidate one marker-bound PID immediately before signaling."""
        if binding.pid <= 1:
            raise ProcessCleanupError(
                "refusing to signal an invalid marker-bound process"
            )
        try:
            observed = _process_creation_fingerprint(binding.pid)
        except ProcessBindingError as exc:
            if not psutil.pid_exists(binding.pid):
                return False
            raise ProcessCleanupError(
                "marker-bound process identity became unavailable"
            ) from exc
        if observed != binding.creation_fingerprint:
            # PID reuse proves the owned process is gone and, critically,
            # prevents the replacement process from being signaled.
            return False
        try:
            os.kill(binding.pid, selected_signal)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise ProcessCleanupError(
                f"marker-bound process {binding.pid} signal was denied"
            ) from exc
        return True

    async def _terminate_owned_marker_processes(
        self,
        runtime: _RuntimeProcess,
    ) -> tuple[bool, bool]:
        """Stop every visible process retaining one private owner marker."""
        observed_any = False
        killed_any = False
        phases = (
            (signal.SIGTERM, self.term_grace_seconds),
            (signal.SIGKILL, self.kill_grace_seconds),
        )
        for selected_signal, grace_seconds in phases:
            signaled: set[tuple[int, str]] = set()
            deadline = asyncio.get_running_loop().time() + grace_seconds
            while True:
                bindings = self._runtime_marker_processes(runtime)
                if not bindings:
                    return observed_any, killed_any
                observed_any = True
                for binding in bindings:
                    identity = (
                        binding.pid,
                        binding.creation_fingerprint,
                    )
                    if identity in signaled:
                        continue
                    if self._signal_owned_marker_process(
                        binding,
                        selected_signal,
                    ):
                        signaled.add(identity)
                        if selected_signal == signal.SIGKILL:
                            killed_any = True
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.02)
        survivors = self._runtime_marker_processes(runtime)
        if survivors:
            raise ProcessCleanupError(
                "marker-bound process survived TERM and KILL"
            )
        return observed_any, killed_any

    async def _wait_for_group_absent(
        self,
        record: ProcessRecord,
        timeout: float,
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._owned_group_exists(record):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    async def _signal_owned_group(
        self, runtime: _RuntimeProcess, selected_signal: signal.Signals,
    ) -> bool:
        """Signal the bound group: True only when the signal was issued.

        False means the group is proven absent, so the caller must not probe or
        signal that id again. macOS denies a signal to a group whose only
        members have exited and are not yet reaped. asyncio is the leader's
        sole reaper, so a denial stands unless that bounded reap, then the
        ordinary absence grace, proves the group gone.
        """
        record = runtime.record
        if record.pgid <= 1:
            raise ProcessCleanupError("refusing to signal an invalid process group")
        denied = ProcessCleanupError(f"owned process group {record.pgid} signal was denied")
        for reaped in (False, True):
            self._assert_group_binding(record)
            try:
                os.killpg(record.pgid, selected_signal)
            except ProcessLookupError:
                return False
            except PermissionError as exc:
                denied.__cause__ = exc
                if reaped:
                    break
                try:
                    await self._wait_for_process_exit(runtime, self.kill_grace_seconds)
                except ProcessCleanupError:
                    raise denied
                if not self._owned_group_exists(record):
                    return False
            else:
                return True
        killing = selected_signal == signal.SIGKILL
        grace = self.kill_grace_seconds if killing else self.term_grace_seconds
        if await self._wait_for_group_absent(record, grace):
            return False
        raise denied

    async def _terminate_locked(
        self,
        runtime: _RuntimeProcess,
        *,
        reason: str,
    ) -> ProcessRecord:
        runtime.pending_reason = reason
        if (
            runtime.record.terminal_state != "running"
            and runtime.record.terminal_state != "death_unverified"
        ):
            self._cleanup_runtime_paths(runtime)
            return runtime.record
        record = runtime.record
        group_killed = False
        try:
            if self._owned_group_exists(record):
                term_sent = await self._signal_owned_group(runtime, signal.SIGTERM)
                if term_sent and not await self._wait_for_group_absent(
                    record, self.term_grace_seconds,
                ):
                    kill_sent = await self._signal_owned_group(runtime, signal.SIGKILL)
                    await self._wait_for_process_exit(runtime, self.kill_grace_seconds)
                    if kill_sent and not await self._wait_for_group_absent(
                        record, self.kill_grace_seconds,
                    ):
                        raise ProcessCleanupError(
                            f"owned process group {record.pgid} survived TERM and KILL"
                        )
                    group_killed = kill_sent
            _markers_found, markers_killed = (
                await self._terminate_owned_marker_processes(runtime)
            )
            terminal_state: TerminalState = (
                "killed" if group_killed or markers_killed else "terminated"
            )
            await self._wait_for_process_exit(runtime, self.kill_grace_seconds)
        except ProcessCleanupError:
            self._set_state(runtime, "death_unverified")
            raise
        self._set_state(runtime, terminal_state)
        return runtime.record

    async def _terminate_runtime(
        self,
        runtime: _RuntimeProcess,
        *,
        reason: str,
    ) -> ProcessRecord:
        async with runtime.cleanup_lock:
            try:
                return await self._terminate_locked(runtime, reason=reason)
            except ProcessCleanupError as exc:
                self._retain_failure(exc)
                raise

    async def stop(
        self,
        handle: str,
        *,
        thread_id: str,
        reason: str = "managed_stop",
    ) -> ProcessRecord:
        runtime = self._owned_runtime(handle, thread_id)
        if runtime.record.mode != "background":
            raise ProcessOwnerError("only a managed background handle can be stopped")
        return await self._shield_cleanup(
            self._terminate_runtime(runtime, reason=reason)
        )

    async def terminate_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        foreground_only: bool = True,
        reason: str = "turn_interrupted",
    ) -> tuple[ProcessRecord, ...]:
        return await self._shield_cleanup(
            self._terminate_turn(
                thread_id,
                turn_id,
                foreground_only=foreground_only,
                reason=reason,
            )
        )

    async def _terminate_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        foreground_only: bool,
        reason: str,
    ) -> tuple[ProcessRecord, ...]:
        async with self._owner_lock:
            self._closed_turns.add((thread_id, turn_id))
            runtimes = tuple(
                runtime
                for runtime in self._runtimes.values()
                if runtime.record.thread_id == thread_id
                and runtime.record.turn_id == turn_id
                and self._lifecycle_cleanup_pending(runtime)
                and (not foreground_only or runtime.record.mode == "foreground")
            )
        return await self._terminate_snapshot(runtimes, reason=reason)

    async def terminate_lease(
        self,
        lease_id: str,
        *,
        reason: str = "lease_terminal",
    ) -> tuple[ProcessRecord, ...]:
        return await self._shield_cleanup(
            self._terminate_lease(lease_id, reason=reason)
        )

    async def _terminate_lease(
        self,
        lease_id: str,
        *,
        reason: str,
    ) -> tuple[ProcessRecord, ...]:
        async with self._owner_lock:
            self._closed_leases.add(lease_id)
            runtimes = tuple(
                runtime
                for runtime in self._runtimes.values()
                if runtime.record.lease_id == lease_id
                and self._lifecycle_cleanup_pending(runtime)
            )

        async def terminate_processes() -> tuple[ProcessRecord, ...] | BaseException:
            try:
                return await self._terminate_snapshot(
                    runtimes,
                    reason=reason,
                )
            except BaseException as exc:
                return exc

        browser_result, process_result = await self._shield_cleanup(
            asyncio.gather(
                self._browser_outcome(lease_id),
                terminate_processes(),
            )
        )
        if browser_result is not None:
            raise browser_result
        if isinstance(process_result, BaseException):
            raise process_result
        return process_result

    async def _terminate_snapshot(
        self,
        runtimes: Sequence[_RuntimeProcess],
        *,
        reason: str,
    ) -> tuple[ProcessRecord, ...]:
        async def terminate_one(
            runtime: _RuntimeProcess,
        ) -> ProcessRecord | BaseException:
            try:
                return await self._shield_cleanup(
                    self._terminate_runtime(runtime, reason=reason)
                )
            except BaseException as exc:
                self._retain_failure(exc)
                return exc

        outcomes = await self._shield_cleanup(
            asyncio.gather(
                *(terminate_one(runtime) for runtime in runtimes)
            )
        )
        results: list[ProcessRecord] = []
        first_failure: BaseException | None = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                first_failure = first_failure or outcome
            else:
                results.append(outcome)
        if first_failure is not None:
            raise first_failure
        return tuple(results)

    async def _browser_outcome(self, lease_id: str | None) -> BaseException | None:
        """Return the current browser cleanup outcome, never retained history.

        BrowserSessionOwner owns unresolved responsibility, so a later verified
        browser_close clears it for both lease termination and shutdown.
        """
        if self._browser_owner is None:
            return None
        try:
            if lease_id is None:
                await self._browser_owner.shutdown()
            else:
                await self._browser_owner.terminate_lease(lease_id)
        except BaseException as exc:
            return exc
        return None

    def _retain_failure(self, failure: BaseException) -> None:
        if not any(existing is failure for existing in self._failures):
            self._failures.append(failure)

    async def shutdown(self) -> None:
        await self._shield_cleanup(self._shutdown())

    async def _shutdown(self) -> None:
        async with self._owner_lock:
            self._shutting_down = True
            runtimes = tuple(
                runtime
                for runtime in self._runtimes.values()
                if self._lifecycle_cleanup_pending(runtime)
            )

        async def terminate_processes() -> BaseException | None:
            try:
                await self._terminate_snapshot(
                    runtimes,
                    reason="backend_shutdown",
                )
            except BaseException as exc:
                self._retain_failure(exc)
                return exc
            return None

        outcomes = await self._shield_cleanup(
            asyncio.gather(
                terminate_processes(),
                self._browser_outcome(None),
            )
        )
        watchers = tuple(
            runtime.watcher_task
            for runtime in self._runtimes.values()
            if runtime.watcher_task is not None
        )
        for watcher in watchers:
            if not watcher.done():
                watcher.cancel()
        if watchers:
            await self._shield_cleanup(
                asyncio.gather(*watchers, return_exceptions=True)
            )
        try:
            await self._shield_cleanup(self.drain_transitions())
        except BaseException as exc:
            self._retain_failure(exc)
        if self.live_count():
            raise ProcessCleanupError(
                f"process owner retained {self.live_count()} live group(s)"
            )
        failure = next((item for item in (*self._failures, *outcomes) if item is not None), None)
        if failure is not None:
            raise ProcessCleanupError(
                "process owner retained an unverified lifecycle failure: "
                f"{failure}"
            ) from failure

    @staticmethod
    async def _join_awaitable(awaitable: Any) -> Any:
        return await awaitable

    @staticmethod
    async def _shield_cleanup(awaitable: Any) -> Any:
        """Finish exact cleanup even if this owner is itself cancelled."""
        task = asyncio.ensure_future(awaitable)
        pending_cancel: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                pending_cancel = pending_cancel or exc
        result = task.result()
        if pending_cancel is not None:
            raise pending_cancel
        return result


def _default_process_owner() -> ProcessOwner:
    from infra.config import RUNTIME_DIR

    return ProcessOwner(
        runtime_dir=RUNTIME_DIR,
        browser_owner=BrowserSessionOwner(
            namespace=BROWSER_NAMESPACE,
            instance_key=os.environ.get("CORSIA_BROWSER_INSTANCE_KEY"),
        ),
    )


PROCESS_OWNER = _default_process_owner()
