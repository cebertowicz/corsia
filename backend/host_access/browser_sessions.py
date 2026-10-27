"""One lifecycle owner for stock native agent-browser sessions."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
import socket
import secrets
import time
from typing import Literal, Sequence, TypeAlias

from host_access import (
    browser_cli, browser_identity as identity, browser_launch, browser_reporting,
    browser_session_commands, browser_session_launch as launch,
)
from host_access.browser_session_state import (
    BrowserRuntime, BrowserSessionCleanupError as BrowserSessionCleanupError,
    BrowserSessionError as BrowserSessionError, BrowserSessionRecord,
    BrowserSetupError as BrowserSetupError,
)

logger = logging.getLogger(__name__)

BROWSER_NAMESPACE = "corsia-prod"
CommandResult: TypeAlias = browser_cli.CommandResult
# Which session a command may use: any (start one if needed), only a new one, or only the open one.
SessionAdmission: TypeAlias = Literal["any", "new", "existing"]
_EXECUTABLE_CHANGED = (
    "browser executable identity changed after CORSIA started using it; "
    "close the browser session with browser_close, then restart CORSIA to use the new build"
)

# Session state and errors are defined in browser_session_state.

class BrowserSessionOwner:
    """Serialize admission, observation and producer-mediated close per session."""

    def __init__(self, *, namespace: str, binary: str = "agent-browser",
                 instance_key: str | None = None, close_timeout: float = 10.0) -> None:
        if not isinstance(namespace, str) or identity.NAMESPACE_RE.fullmatch(namespace) is None:
            raise ValueError("browser namespace is invalid")
        if not isinstance(binary, str) or not binary:
            raise ValueError("browser binary is invalid")
        self.namespace = namespace
        self.binary = binary
        self.socket_dir = identity.socket_dir_for_namespace(namespace, instance_key)
        self.close_timeout = max(0.01, float(close_timeout))
        self._native: browser_cli.NativeExecutable | None = None
        self._chrome: str | None = None
        self._user_agent: str | None = None
        self._native_lock = asyncio.Lock()
        self._owner_lock = asyncio.Lock()
        self._sessions: dict[str, BrowserRuntime] = {}
        self._closed_leases: set[str] = set()
        self._shutting_down = False
        self._active_commands = 0

    def live_count(self) -> int:
        """Count current responsibilities, including unresolved startup or close."""
        return len(self._sessions)

    def active_command_count(self) -> int:
        """Return admitted browser work that is running or queued."""
        return self._active_commands

    async def run(self, thread_id: str, args: Sequence[str], *,
                  lease_id: str | None = None, timeout: float = 30.0,
                  show_window: bool | None = None, session: SessionAdmission = "any",
                  pane: bool = False, untrusted: bool = False) -> str:
        """Run one command through the shared-browser command path; `pane` marks the person's own
        command from the browser pane, which the password guard never refuses. An `untrusted` session
        (one that reads outside content for a worker nobody vouches for) never gets the saved sign-in."""
        from host_access.browser_session_commands import run
        return await run(self, thread_id, args, lease_id=lease_id, timeout=timeout, show_window=show_window,
                         session=session, pane=pane, untrusted=untrusted)

    async def page_endpoint(self, thread_id: str) -> str:
        """The thread's shared page target, resolved exactly as for its agent's commands."""
        from host_access.browser_session_commands import page_endpoint
        return await page_endpoint(self, thread_id)

    async def declare_person_step(self, thread_id: str, instruction: str) -> None:
        """Open a system-observed step for the person on the current browser page."""
        from host_access.browser_session_commands import declare_person_step
        await declare_person_step(self, thread_id, instruction)

    async def has_session(self, thread_id: str) -> bool:
        """Report whether this thread has a browser for a pane to show."""
        async with self._owner_lock:
            return thread_id in self._sessions

    async def _run_admitted(self, runtime: BrowserRuntime, args: Sequence[str], *,
                            lease_id: str | None, timeout: float) -> tuple[CommandResult, bool]:
        """Run one command in an admitted session; report whether it left the session unfinished."""
        self._active_commands += 1
        started = False
        try:
            async with runtime.command_lock:
                self._assert_admitted(runtime, lease_id)
                before = await self._observe(runtime)
                if runtime.record.binding is not None and not before.alive:
                    raise BrowserSessionCleanupError("browser daemon exited without close acknowledgement")
                if runtime.record.binding is None and not before.artifacts_absent:
                    raise BrowserSessionCleanupError("browser startup ownership is unresolved")
                if runtime.record.binding is None and runtime.record.copy_of:
                    await launch.seed_copy(self, runtime)
                started = True
                runtime.attempt_started_at = time.time()
                runtime.attempt = CommandResult(-1, "", "", completion="unknown")
                try:
                    result = await self._invoke(self._session_argv(runtime, args), timeout=timeout)
                except browser_cli.CommandCancelled as exc:
                    runtime.attempt = exc.result
                    raise
                runtime.attempt = result
                await self._observe_attempt(runtime)
                # A wait only polls the page, so its timeout says nothing about the session:
                # _observe_attempt proved the daemon alive and the client settled.
                wait_timed_out = result.timed_out and args[0] == "wait"
                unfinished = (result.timed_out or result.completion == "interrupted") and not wait_timed_out
                if unfinished:
                    runtime.record = replace(runtime.record, terminal_state="resetting")
            if unfinished:
                await self._close_runtime(runtime)
            return result, unfinished
        except asyncio.CancelledError:
            if started:
                await asyncio.shield(self._close_one(runtime))
            raise
        except BaseException as exc:
            if not started and (type(exc) is BrowserSessionError or
                                isinstance(exc, BrowserSessionCleanupError) and runtime.record.terminal_state != "running"):
                raise  # Admission rejection has no new session effects.
            async with runtime.command_lock:
                if self._sessions.get(runtime.record.thread_id) is runtime:
                    raise await self._record_failure(runtime, exc) from exc
            raise
        finally:
            self._active_commands -= 1

    async def _admit(self, thread_id: str, *, lease_id: str | None, show_window: bool | None = None,
                     session: SessionAdmission = "any", untrusted: bool = False) -> BrowserRuntime:
        # Resolved before the lock, and only for a session that does not exist yet.
        profile = None if thread_id in self._sessions else await launch.resolve_profile(thread_id, untrusted=untrusted)
        async with self._owner_lock:
            if self._shutting_down or lease_id in self._closed_leases:
                raise BrowserSessionError("browser session admission is closed")
            runtime = self._sessions.get(thread_id)
            if runtime is None and session == "existing":
                raise BrowserSessionError("the browser is no longer open")
            if runtime is None:
                shared, copy_of = launch.assign(self._sessions, profile, thread_id)
                runtime = BrowserRuntime(BrowserSessionRecord(
                    thread_id, secrets.token_urlsafe(12), lease_id, bool(show_window), profile=shared, copy_of=copy_of))
                self._sessions[thread_id] = runtime
            elif session == "new":
                raise BrowserSessionError("the browser is already open; take over in the pane to navigate it")
            if runtime.record.terminal_state != "running":
                return runtime  # The serialized command path reports this responsibility.
            self._assert_admitted(runtime, lease_id)
            if show_window is not None and runtime.record.show_window != show_window:
                current = "visible" if runtime.record.show_window else "headless"
                requested = "true" if show_window else "false"
                raise BrowserSessionError(
                    f"browser session is already running {current}. Call browser_close, "
                    f"then browser_open with show_window={requested}."
                )
            if runtime.record.lease_id is None and lease_id is not None:
                runtime.record = replace(runtime.record, lease_id=lease_id)
            return runtime

    def _assert_admitted(self, runtime: BrowserRuntime, lease_id: str | None) -> None:
        record = runtime.record
        if (self._shutting_down or self._sessions.get(record.thread_id) is not runtime
                or lease_id in self._closed_leases or record.lease_id in self._closed_leases):
            raise BrowserSessionError("browser session admission is closed")
        if record.terminal_state != "running":
            raise BrowserSessionCleanupError("browser session unavailable", recovery="pending" if record.terminal_state == "resetting" else "operator_required")
        if lease_id is not None and record.lease_id not in {None, lease_id}:
            raise BrowserSessionError("browser session is bound to another lease")

    async def _pin_native(self) -> None:
        async with self._native_lock:
            try:
                path, fingerprint = await asyncio.to_thread(browser_cli.resolve_native, self.binary)
                if self._native is not None:
                    if (path, fingerprint) != (self._native.path, self._native.fingerprint):
                        raise identity.BrowserIdentityChanged(_EXECUTABLE_CHANGED)
                    return
                self.socket_dir = await asyncio.to_thread(identity.ensure_private_dir, self.socket_dir)
                result = await self._invoke((path, "--version"), timeout=5.0)
                if result.completion != "exited" or result.timed_out or result.returncode:
                    raise identity.BrowserIdentityError(
                        f"`agent-browser --version` did not succeed; {browser_cli.install_hint()}")
                version = browser_cli.admitted_version(result.stdout)
                if await asyncio.to_thread(browser_cli.resolve_native, self.binary) != (path, fingerprint):
                    raise identity.BrowserIdentityChanged(_EXECUTABLE_CHANGED)
                chrome, source = await launch.select_chrome(self, path)
                self._native = browser_cli.NativeExecutable(path, fingerprint, version)
                self._chrome = chrome
                self._user_agent = browser_launch.user_agent(await asyncio.to_thread(browser_launch.chrome_major, chrome))
                await asyncio.to_thread(launch.prepare_directories)
                logger.info("agent-browser launches Chrome from %s (%s)", self._chrome or "its default", source)
            except identity.BrowserIdentityError as exc:
                # Setup refusal precedes admission; it never enters cleanup state.
                raise BrowserSetupError(str(exc)) from exc

    def _session_argv(self, runtime: BrowserRuntime, args: Sequence[str]) -> tuple[str, ...]:
        assert self._native is not None
        return (self._native.path, "--session", runtime.record.session,
                *(("--executable-path", self._chrome) if self._chrome else ()),
                *(("--headed",) if runtime.record.show_window else ()),
                *(launch.flags(runtime.record, self._user_agent) if runtime.record.binding is None else ()),
                "--json", *args)

    async def _observe(self, runtime: BrowserRuntime, peer_socket: socket.socket | None = None) -> identity.Observation:
        """All identity consumers use this one observation; psutil never blocks the loop."""
        record = runtime.record
        if self._native is None and runtime.attempt is None:
            return identity.Observation(None, False, True)  # No client was admitted.
        assert self._native is not None
        try:
            observed = await asyncio.to_thread(
                identity.observe_session, socket_dir=self.socket_dir, session=record.session,
                binding=record.binding, admitted_at=record.admitted_at,
                attempt_started_at=runtime.attempt_started_at,
                executable=self._native.fingerprint, version=self._native.version, peer_socket=peer_socket,
            )
        except (identity.BrowserIdentityError, identity.BrowserPidAbsent) as exc:
            raise BrowserSessionCleanupError(str(exc)) from exc
        if observed.binding is not None:
            if any(other is not runtime and other.record.binding is not None
                   and other.record.binding.pid == observed.binding.pid for other in self._sessions.values()):
                raise BrowserSessionCleanupError("browser daemon identity is shared by another session")
            runtime.record = replace(record, binding=observed.binding)
        return observed

    async def _observe_attempt(self, runtime: BrowserRuntime) -> None:
        assert runtime.attempt is not None
        runtime.attempt = runtime.attempt.settled()
        observed = await self._observe(runtime)
        if (runtime.record.binding is None and observed.artifacts_absent
                and runtime.attempt.rejected_before_daemon()):
            await self._remove_runtime(runtime)
            return
        if runtime.attempt.completion == "unknown":
            raise BrowserSessionCleanupError("browser client settlement is unresolved")
        if not observed.alive:
            raise BrowserSessionCleanupError("browser startup or daemon exit is unresolved")

    def session_identity(self, thread_id: str) -> str | None:
        """The runtime identity for pane probes; no page or process inspection."""
        runtime = self._sessions.get(thread_id)
        return runtime.record.session if runtime is not None and runtime.record.terminal_state == "running" else None

    async def close_thread(self, thread_id: str) -> str:
        """Guard agent close, then use the shared producer teardown."""
        from host_access.browser_session_commands import close_thread
        return await close_thread(self, thread_id)

    async def _close_runtime(self, runtime: BrowserRuntime) -> None:
        async with runtime.command_lock:
            if runtime.record.terminal_state == "closed":
                return
            runtime.record = replace(runtime.record, terminal_state="resetting")
            try:
                await browser_session_commands.end_session(runtime.record.thread_id, runtime.record.session)
                if runtime.attempt is not None:
                    runtime.attempt = runtime.attempt.settled()
                    if runtime.attempt.completion == "unknown":
                        raise BrowserSessionCleanupError("browser client settlement is unresolved")
                # An in-flight close is only awaited: once acknowledged, its daemon removes
                # sidecars while exiting, so observing here reads a torn exit (#2351).
                if (close := runtime.close_task) is None or close.done() and (close.cancelled() or close.exception() is not None):
                    observed = await self._observe(runtime)
                    if runtime.attempt is None and observed.artifacts_absent and runtime.record.binding is None:
                        await self._remove_runtime(runtime)
                        return
                    if close and not close.cancelled() and observed.alive:
                        runtime.close_task = None
                    if runtime.close_task is None:
                        if not observed.alive or observed.artifacts_absent:
                            raise BrowserSessionCleanupError("browser daemon exited without close acknowledgement")
                        runtime.close_task = asyncio.create_task(self._request_close(runtime))
                assert runtime.close_task is not None
                await asyncio.wait_for(asyncio.shield(runtime.close_task), self.close_timeout)
                await self._wait_closed(runtime)
                await self._remove_runtime(runtime)
            except BaseException as exc:
                if runtime.record.terminal_state == "closed":
                    raise
                failure = await self._record_failure(runtime, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise failure from exc

    async def _record_failure(self, runtime: BrowserRuntime, exc: BaseException) -> BrowserSessionCleanupError:
        settlement = runtime.attempt.settlement if runtime.attempt else None
        close = runtime.close_task
        acknowledged = close is not None and close.done() and not close.cancelled() and close.exception() is None
        draining = acknowledged and (isinstance(exc, asyncio.TimeoutError) or
                                    isinstance(exc, BrowserSessionCleanupError) and exc.recovery == "pending")
        pending = draining or any(task is not None and not task.done() for task in (settlement, close))
        runtime.record = replace(runtime.record, terminal_state="resetting" if pending else "death_unverified")
        if not pending and runtime.report is None:
            try:
                if runtime.report_task is None or runtime.report_task.done() and (runtime.report_task.cancelled() or runtime.report_task.exception() is not None):
                    runtime.report_task = asyncio.create_task(browser_reporting.report_cleanup(runtime.record.thread_id))
                runtime.report = await asyncio.shield(runtime.report_task)
            except Exception:
                logger.error("browser cleanup notice could not be recorded; ownership remains blocked")
        message = str(exc) if isinstance(exc, BrowserSessionCleanupError) else ("browser cleanup is pending" if pending else "browser cleanup could not be verified")
        return BrowserSessionCleanupError(message, recovery="pending" if pending else "operator_required")

    async def _request_close(self, runtime: BrowserRuntime) -> None:
        reader, writer = await asyncio.open_unix_connection(
            str(self.socket_dir / f"{runtime.record.session}.sock"), limit=65536,
        )
        try:
            peer_socket = writer.get_extra_info("socket")
            if peer_socket is None:
                raise BrowserSessionCleanupError("browser connected peer identity is unavailable")
            observed = await self._observe(runtime, peer_socket)
            if not observed.alive or observed.artifacts_absent:
                raise BrowserSessionCleanupError("browser daemon changed before close request")
            await browser_cli.close_request(reader, writer, secrets.token_hex(12))
        finally:
            writer.close()
            await writer.wait_closed()

    async def _wait_closed(self, runtime: BrowserRuntime) -> None:
        deadline = asyncio.get_running_loop().time() + self.close_timeout
        while True:
            try:
                if not (observed := await self._observe(runtime)).alive and observed.artifacts_absent:
                    return
            except BrowserSessionCleanupError as exc:
                identity.require_exit_torn_read(exc)
                if asyncio.get_running_loop().time() >= deadline:
                    raise
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError
            await asyncio.sleep(0.2)

    async def _remove_runtime(self, runtime: BrowserRuntime) -> None:
        async with self._owner_lock:
            runtime.record = replace(runtime.record, terminal_state="closed")
            launch.drop_copy(runtime.record)
            thread_id = runtime.record.thread_id
            if self._sessions.get(thread_id) is runtime:
                self._sessions.pop(thread_id)
        if runtime.report is not None or runtime.report_task is not None:
            try:
                if runtime.report is None and runtime.report_task is not None:
                    runtime.report = await asyncio.shield(runtime.report_task)
                if runtime.report is not None:
                    await browser_reporting.report_cleanup(runtime.record.thread_id, recovered=runtime.report)
            except (Exception, asyncio.CancelledError) as exc:
                logger.error("browser cleanup verified but recovery notice could not be recorded")
                if isinstance(exc, asyncio.CancelledError):
                    raise

    async def _close_one(self, runtime: BrowserRuntime) -> BaseException | None:
        try:
            await self._close_runtime(runtime)
        except BaseException as exc:
            return exc
        return None

    async def terminate_lease(self, lease_id: str) -> None:
        """Fence a lease and close its sessions through the same producer protocol."""
        if not isinstance(lease_id, str) or not lease_id:
            raise BrowserSessionError("browser lease identity is invalid")
        async with self._owner_lock:
            self._closed_leases.add(lease_id)
            runtimes = tuple(runtime for runtime in self._sessions.values() if runtime.record.lease_id == lease_id)
        failures = await asyncio.gather(*(self._close_one(runtime) for runtime in runtimes))
        self._raise_cleanup_failures(failures)

    async def shutdown(self) -> None:
        """Report current unresolved responsibility, never historical failures."""
        async with self._owner_lock:
            self._shutting_down = True
            runtimes = tuple(self._sessions.values())
        failures = await asyncio.gather(*(self._close_one(runtime) for runtime in runtimes))
        self._raise_cleanup_failures(failures)

    @staticmethod
    def _raise_cleanup_failures(failures: Sequence[BaseException | None]) -> None:
        unresolved = [failure for failure in failures if failure is not None]
        if unresolved:
            pending = all(isinstance(failure, BrowserSessionCleanupError) and failure.recovery == "pending" for failure in unresolved)
            raise BrowserSessionCleanupError("browser owner retained unresolved cleanup", recovery="pending" if pending else "operator_required") from unresolved[0]

    def _model_result(self, result: CommandResult, record: BrowserSessionRecord) -> str:
        return browser_cli.render_model_result(result, redactions={
            str(self.socket_dir): "[browser-runtime]", record.session: "[browser-session]",
            record.thread_id: "[browser-thread]", record.lease_id or "": "[browser-lease]",
        })

    async def _invoke(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        return await browser_cli.invoke(argv, socket_dir=self.socket_dir, timeout=timeout)
